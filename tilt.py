"""Tilt check: spot revenge trading and overtrading in your own trade log.

Definitions (all adjustable in .env):
  • Revenge trade   — opened less than COOLDOWN_MIN minutes after a losing trade closed.
  • Flip            — a revenge trade on the same coin in the opposite direction.
  • Escalation      — after a loss, the next trade uses higher leverage or a much bigger position.
  • Tilt session    — 3 or more trades chained together by revenge entries.
  • Overtrading     — trades beyond MAX_TRADES_PER_DAY, and trades taken after giving back DAILY_LOSS_LIMIT
                      from the day's best point (so a green day that turns red is caught too).
"""
import io
from datetime import datetime

FMT = "%Y-%m-%d %H:%M:%S"


def _t(ts):
    try:
        return datetime.strptime(ts[:19], FMT)
    except (TypeError, ValueError):
        return None


def _order_key(p):
    return (p.get("opened_at") or (p.get("sort_ts") or "").rstrip("~") or p.get("closed_at") or "")


def _notional(p):
    return abs((p.get("qty") or 0) * (p.get("entry") or 0))


def _bad(chain):
    """A chain of revenge entries counts as a tilt session if it is 3+ trades and it lost money or had 3+ losses."""
    return len(chain) >= 3 and (sum(x["pnl"] for x in chain) < 0 or sum(1 for x in chain if x["pnl"] < 0) >= 3)


def analyse(positions, cooldown=15, max_trades=8, loss_limit=5.0):
    seq = sorted(positions, key=_order_key)
    rows, run_pnl, peak = [], 0.0, 0.0
    for i, p in enumerate(seq):
        prev = seq[i - 1] if i else None
        opened, prev_closed = _t(p.get("opened_at")), _t(prev.get("closed_at")) if prev else None
        gap = (opened - prev_closed).total_seconds() / 60 if opened and prev_closed else None
        after_loss = bool(prev and prev["pnl"] < 0)
        revenge = after_loss and gap is not None and gap < cooldown
        flip = revenge and prev["symbol"] == p["symbol"] and prev["side"] != p["side"]
        lev, plev = p.get("leverage"), prev.get("leverage") if prev else None
        lev_up = after_loss and lev and plev and lev > plev
        size_up = after_loss and _notional(prev) > 0 and _notional(p) >= 2.0 * _notional(prev)
        rows.append({**p, "n": i + 1, "gap": gap, "revenge": revenge, "flip": flip,
                     "lev_up": bool(lev_up), "size_up": bool(size_up),
                     "over_count": i + 1 > max_trades, "past_limit": peak - run_pnl >= abs(loss_limit),
                     "pnl_before": run_pnl, "peak_before": peak})
        run_pnl += p["pnl"]
        peak = max(peak, run_pnl)

    # tilt sessions: chains of revenge entries (a session starts at the losing trade that set it off)
    sessions, cur = [], []
    for i, r in enumerate(rows):
        if r["revenge"]:
            if not cur:
                cur = [rows[i - 1]]
            cur.append(r)
        else:
            if _bad(cur):
                sessions.append(cur)
            cur = []
    if _bad(cur):
        sessions.append(cur)

    def group(sel):
        g = [r for r in rows if sel(r)]
        w = sum(1 for r in g if r["pnl"] > 0)
        return {"n": len(g), "pnl": round(sum(r["pnl"] for r in g), 2), "wins": w,
                "win_rate": w / len(g) if g else None}

    out = {
        "rows": rows, "total": group(lambda r: True),
        "revenge": group(lambda r: r["revenge"]), "calm": group(lambda r: not r["revenge"]),
        "flips": group(lambda r: r["flip"]),
        "escalations": [r for r in rows if r["lev_up"] or r["size_up"]],
        "over_count": group(lambda r: r["over_count"]), "within_count": group(lambda r: not r["over_count"]),
        "past_limit": group(lambda r: r["past_limit"]),
        "sessions": [{"start": s[0].get("opened_at") or s[0].get("closed_at"),
                      "end": s[-1].get("closed_at") or s[-1].get("opened_at"),
                      "n": len(s), "pnl": round(sum(x["pnl"] for x in s), 2),
                      "losses": sum(1 for x in s if x["pnl"] < 0),
                      "coins": sorted({x["symbol"].replace("USDT", "") for x in s})} for s in sessions],
        "settings": {"cooldown": cooldown, "max_trades": max_trades, "loss_limit": loss_limit},
    }
    # a simple 0–100 tilt score: share of trades that broke a discipline rule, weighted
    n = len(rows) or 1
    bad = sum(1 for r in rows if r["revenge"]) + sum(1 for r in rows if r["flip"]) * .5 \
        + len(out["escalations"]) + sum(1 for r in rows if r["over_count"] or r["past_limit"]) * .5
    out["score"] = min(100, round(100 * bad / (n * 1.5)))
    out["label"] = "calm" if out["score"] < 20 else "warning" if out["score"] < 45 else "tilted"
    return out


def _m(v):
    return f"{v:+.2f}"


def _wr(g):
    return f"{g['wins']}/{g['n']} won" if g["n"] else "—"


def report(day, a):
    """Telegram HTML text."""
    s, cd = a["settings"], a["settings"]["cooldown"]
    icon = {"calm": "🟢", "warning": "🟠", "tilted": "🔴"}[a["label"]]
    L = [f"<b>🧠 Tilt check — {day}</b>  {icon} {a['label'].capitalize()} ({a['score']}/100)", ""]
    rv, cm = a["revenge"], a["calm"]
    if rv["n"]:
        L.append(f"Revenge trades (opened &lt;{cd:g} min after a loss): <b>{rv['n']}</b> → {_m(rv['pnl'])} USDT, {_wr(rv)}")
        L.append(f"Calm trades: {cm['n']} → {_m(cm['pnl'])} USDT, {_wr(cm)}")
    else:
        L.append(f"✅ No revenge trades — you waited at least {cd:g} min after every loss.")
    if a["flips"]["n"]:
        L.append(f"Flipped direction straight after a loss: {a['flips']['n']}× → {_m(a['flips']['pnl'])}")
    for r in a["escalations"][:4]:
        what = []
        prev = a["rows"][r["n"] - 2]
        if r["lev_up"]:
            what.append(f"leverage {prev.get('leverage')}→{r.get('leverage')}×")
        if r["size_up"]:
            what.append(f"size ×{_notional(r) / max(_notional(prev), 1e-9):.1f}")
        L.append(f"⚠️ After a loss you raised {', '.join(what)} on {r['symbol'].replace('USDT', '')} → {_m(r['pnl'])}")
    for ses in a["sessions"]:
        a_, b_ = (ses["start"] or "")[11:16], (ses["end"] or "")[11:16]
        L.append(f"🔴 Tilt session {a_}–{b_}: {ses['n']} trades, {ses['losses']} losses, {_m(ses['pnl'])} USDT "
                 f"({', '.join(ses['coins'])})")
    L.append("")
    t = a["total"]["n"]
    if t > s["max_trades"]:
        L.append(f"Overtrading: <b>{t} trades</b> (your limit is {s['max_trades']}). "
                 f"Trades 1–{s['max_trades']}: {_m(a['within_count']['pnl'])} · trades {s['max_trades'] + 1}+: {_m(a['over_count']['pnl'])}")
    else:
        L.append(f"Trades: {t} (limit {s['max_trades']}) ✅")
    if a["past_limit"]["n"]:
        L.append(f"Kept trading after giving back {s['loss_limit']:g} USDT from the day's high: "
                 f"{a['past_limit']['n']} trades → {_m(a['past_limit']['pnl'])}")
    saved = -sum(r["pnl"] for r in a["rows"] if (r["revenge"] or r["over_count"] or r["past_limit"]) and r["pnl"] < 0)
    if saved > 0.005:
        L.append(f"\nSkipping the rule-breaking losers would have kept <b>{saved:.2f} USDT</b>.")
    L.append(f"\n<i>Rules: wait {cd:g} min after any loss · max {s['max_trades']} trades a day · "
             f"stop after giving back {s['loss_limit']:g} USDT · never raise leverage after a loss.</i>")
    return "\n".join(L)


def week_report(days_analyses):
    """[(day, analysis)] → short multi-day summary."""
    rv = sum(a["revenge"]["n"] for _, a in days_analyses)
    rvp = sum(a["revenge"]["pnl"] for _, a in days_analyses)
    cm = sum(a["calm"]["n"] for _, a in days_analyses)
    cmp_ = sum(a["calm"]["pnl"] for _, a in days_analyses)
    ov = sum(a["over_count"]["pnl"] for _, a in days_analyses)
    ses = sum(len(a["sessions"]) for _, a in days_analyses)
    L = ["<b>🧠 Tilt over the last 7 days</b>"]
    for d, a in days_analyses:
        if a["total"]["n"]:
            icon = {"calm": "🟢", "warning": "🟠", "tilted": "🔴"}[a["label"]]
            L.append(f"<code>{d[5:]}</code> {icon} {a['total']['n']} trades · revenge {a['revenge']['n']} · {_m(a['total']['pnl'])}")
    L.append("")
    L.append(f"Revenge trades: {rv} → {_m(rvp)} USDT · calm trades: {cm} → {_m(cmp_)} USDT")
    L.append(f"Trades beyond your daily trade limit: {_m(ov)} USDT · tilt sessions: {ses}")
    if rv and rvp < 0 < cmp_:
        L.append("\nYour calm trades make money; your revenge trades give it back. The cooldown is the fix.")
    return "\n".join(L)


def chart(day, a):
    """PNG: the day's trades on a timeline — running P&L, each trade as a dot, tilt sessions shaded."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    BG, GRID, MUTED, TEXT, UP, DOWN, AMBER = "#0e1524", "#26324f", "#8e99b3", "#e9edf5", "#2fd3a5", "#ff6b6b", "#ffb547"
    def when(r):
        return _t(r.get("closed_at") or r.get("opened_at") or (r.get("sort_ts") or "").rstrip("~"))
    rows = [r for r in a["rows"] if when(r)]          # trades with hidden times use their best-known time
    if not rows:
        return None
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=150)
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(color=GRID, lw=.6, axis="y")
    for ses in a["sessions"]:
        s0, s1 = _t(ses["start"]), _t(ses["end"])
        if s0 and s1:
            ax.axvspan(s0, s1, color=DOWN, alpha=.13, lw=0)
            ax.text(s0, 1.0, f" tilt: {ses['n']} trades {_m(ses['pnl'])}", transform=ax.get_xaxis_transform(),
                    color=DOWN, fontsize=8, va="top", fontweight="bold")
    xs, ys, run = [], [], 0.0
    for r in rows:
        run += r["pnl"]; xs.append(when(r)); ys.append(run)
    ax.step([xs[0]] + xs, [0.0] + ys, where="post", color=AMBER, lw=1.8, zorder=3)   # day starts at 0
    ax.axhline(0, color=GRID, lw=1)
    sizes = [max(18, min(160, _notional(r) * 0.6)) for r in rows]
    cols = [UP if r["pnl"] > 0 else DOWN if r["pnl"] < 0 else MUTED for r in rows]
    edge = [TEXT if r["revenge"] else BG for r in rows]
    ax.scatter(xs, ys, s=sizes, c=cols, edgecolors=edge, linewidths=1.2, zorder=4)
    for r, x, y in zip(rows, xs, ys):
        if r["lev_up"] or r["size_up"]:
            ax.annotate(f"{r.get('leverage') or ''}×", (x, y), textcoords="offset points", xytext=(0, 9),
                        ha="center", color=AMBER, fontsize=7, fontweight="bold")
    ax.annotate(f"{run:+.2f} USDT", (xs[-1], run), textcoords="offset points", xytext=(6, 0),
                color=AMBER, fontsize=9, fontweight="bold", va="center")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_title(f"{day} — running P&L. White ring = revenge trade, shaded = tilt session, dot size = position size",
                 color=TEXT, fontsize=9, loc="left")
    buf = io.BytesIO()
    fig.tight_layout(); fig.savefig(buf, format="png", facecolor=BG); plt.close(fig)
    return buf.getvalue()
