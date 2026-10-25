"""Turn logged positions into a daily review, rule checks, a week summary, a chart and a CSV."""
import csv
import io
from datetime import datetime

FMT = "%Y-%m-%d %H:%M:%S"


def minutes(p):
    if p.get("opened_at") and p.get("closed_at"):
        try:
            return (datetime.strptime(p["closed_at"], FMT) - datetime.strptime(p["opened_at"], FMT)).total_seconds() / 60
        except ValueError:
            return None
    return None


def coin(sym):
    return sym[:-4] if sym.endswith("USDT") else sym


def stats(positions):
    w = [p for p in positions if p["pnl"] > 0]
    l = [p for p in positions if p["pnl"] < 0]
    gw, gl = sum(p["pnl"] for p in w), sum(p["pnl"] for p in l)
    return {
        "n": len(positions), "wins": len(w), "losses": len(l),
        "net": round(gw + gl, 2),
        "win_rate": len(w) / (len(w) + len(l)) if (w or l) else None,
        "avg_win": gw / len(w) if w else None, "avg_loss": gl / len(l) if l else None,
        "pf": gw / -gl if gl else None,
    }


def rule_checks(positions, max_hold=10, max_losses=2):
    """The two rules plus flip counting. Positions must be in time order."""
    held = [p for p in positions if p["pnl"] < 0 and (minutes(p) or 0) > max_hold]
    after = []                       # trades taken on a coin after `max_losses` losses in a row on it
    streak, flips, last_side = {}, 0, {}
    for p in positions:
        s = p["symbol"]
        if streak.get(s, 0) >= max_losses:
            after.append(p)
        streak[s] = streak.get(s, 0) + 1 if p["pnl"] < 0 else 0
        if s in last_side and last_side[s] != p["side"]:
            flips += 1
        last_side[s] = p["side"]
    return {
        "held": held, "held_pnl": round(sum(p["pnl"] for p in held), 2),
        "after": after, "after_pnl": round(sum(p["pnl"] for p in after), 2),
        "flips": flips,
    }


def by_hold(positions, max_hold=10):
    quick = [p for p in positions if minutes(p) is not None and minutes(p) <= max_hold]
    slow = [p for p in positions if minutes(p) is not None and minutes(p) > max_hold]
    return round(sum(p["pnl"] for p in quick), 2), len(quick), round(sum(p["pnl"] for p in slow), 2), len(slow)


def money(v):
    return f"{v:+.2f}"


def day_review(day, positions, calendar_pnl=None, max_hold=10, max_losses=2):
    """Plain-text review for one day (Telegram HTML)."""
    if not positions:
        if calendar_pnl is not None:
            return f"<b>{day}</b>: {money(calendar_pnl)} USDT (calendar only, no trade details)."
        return f"<b>{day}</b>: no trades logged."
    s = stats(positions)
    r = rule_checks(positions, max_hold, max_losses)
    q, qn, sl, sn = by_hold(positions, max_hold)
    coins = {}
    for p in positions:
        coins.setdefault(coin(p["symbol"]), []).append(p["pnl"])
    worst = min(positions, key=lambda p: p["pnl"])
    best = max(positions, key=lambda p: p["pnl"])
    lines = [f"<b>📒 {day}</b>"]
    net = calendar_pnl if calendar_pnl is not None else s["net"]
    lines.append(f"Net <b>{money(net)} USDT</b> · {s['n']} trades · {s['wins']}W / {s['losses']}L"
                 + (f" ({s['win_rate']:.0%})" if s["win_rate"] is not None else ""))
    if s["avg_win"] is not None and s["avg_loss"] is not None:
        verdict = "✅ winners bigger than losers" if s["avg_win"] > -s["avg_loss"] else "⚠️ losers bigger than winners"
        lines.append(f"Avg win {money(s['avg_win'])} vs avg loss {money(s['avg_loss'])} — {verdict}")
    lines.append("Coins: " + ", ".join(f"{c} {money(sum(v))} ({len(v)})" for c, v in sorted(coins.items(), key=lambda x: -sum(x[1]))))
    lines.append(f"Best: {coin(best['symbol'])} {best['side'].lower()} {money(best['pnl'])} · "
                 f"Worst: {coin(worst['symbol'])} {worst['side'].lower()} {money(worst['pnl'])}"
                 + (f" (held {minutes(worst):.0f} min)" if minutes(worst) is not None else ""))
    if qn or sn:
        lines.append(f"Held ≤{max_hold:g} min: {money(q)} ({qn}) · longer: {money(sl)} ({sn})")
    lines.append("")
    lines.append("<b>Rules</b>")
    lines.append(("❌" if r["held"] else "✅") + f" Losers held over {max_hold:g} min: {len(r['held'])}"
                 + (f" → {money(r['held_pnl'])}" if r["held"] else ""))
    lines.append(("❌" if r["after"] else "✅") + f" Trades after {max_losses} losses in a row on a coin: {len(r['after'])}"
                 + (f" → {money(r['after_pnl'])}" if r["after"] else ""))
    if r["flips"] >= 4:
        lines.append(f"⚠️ Flipped long/short {r['flips']} times on the same coin")
    saved = -(r["held_pnl"] + sum(p["pnl"] for p in r["after"] if p not in r["held"]))
    if saved > 0.005:
        lines.append(f"Following both rules would have kept about <b>{saved:.2f} USDT</b>.")
    if calendar_pnl is not None and abs(calendar_pnl - s["net"]) > 0.15:
        lines.append(f"\nℹ️ Logged trades add up to {money(s['net'])} but the calendar says {money(calendar_pnl)} — "
                     "some positions may be missing. Send the rest of the Position History.")
    guessed = [p for p in positions if p.get("day_guessed")]
    if guessed:
        lines.append(f"ℹ️ {len(guessed)} trade(s) had no visible date; I filed them under this day.")
    return "\n".join(lines)


def week_summary(days, daily_map, pos_by_day):
    """days: list of YYYY-MM-DD. Uses the calendar figure where present, else the sum of logged trades."""
    rows, total = [], 0.0
    for d in days:
        v = daily_map.get(d)
        src = "cal"
        if v is None:
            ps = pos_by_day.get(d, [])
            v = round(sum(p["pnl"] for p in ps), 2) if ps else None
            src = "trades"
        rows.append((d, v, src))
        total += v or 0
    allp = [p for d in days for p in pos_by_day.get(d, [])]
    return rows, round(total, 2), stats(allp) if allp else None


def week_text(rows, total, s):
    lines = [f"<b>📊 Last {len(rows)} days: {money(total)} USDT</b>"]
    for d, v, _ in rows:
        bar = "" if v is None else ("🟩" if v > 0 else "🟥" if v < 0 else "⬜")
        lines.append(f"<code>{d[5:]}</code> {bar} {'—' if v is None else money(v)}")
    if s and s["n"]:
        lines.append(f"\nLogged trades: {s['n']} · win rate {s['win_rate']:.0%}" if s["win_rate"] is not None else "")
        if s["avg_win"] is not None and s["avg_loss"] is not None:
            lines.append(f"Avg win {money(s['avg_win'])} · avg loss {money(s['avg_loss'])}"
                         + (f" · profit factor {s['pf']:.2f}" if s["pf"] else ""))
    return "\n".join(x for x in lines if x is not None)


def week_chart(rows):
    """PNG bytes: daily bars + running total."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    labels = [d[5:] for d, _, _ in rows]
    vals = [v or 0 for _, v, _ in rows]
    cum, c = [], 0
    for v in vals:
        c += v; cum.append(c)
    fig, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
    fig.patch.set_facecolor("#0b0e11"); ax.set_facecolor("#0b0e11")
    ax.bar(labels, vals, color=["#16c784" if v >= 0 else "#ea3943" for v in vals], width=0.6, alpha=.85)
    ax.plot(labels, cum, color="#f0b90b", lw=2, marker="o", ms=4)
    ax.annotate(f"{cum[-1]:+.2f}", (len(labels) - 1, cum[-1]), textcoords="offset points", xytext=(0, 8),
                ha="center", color="#f0b90b", fontsize=9, fontweight="bold")
    ax.axhline(0, color="#3a4250", lw=1)
    for s in ("top", "right", "left", "bottom"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors="#8a93a0", labelsize=8)
    ax.grid(axis="y", color="#262c35", lw=.6)
    ax.set_title("Daily P&L (bars) and running total (line), USDT", color="#e6e8eb", fontsize=10, loc="left")
    buf = io.BytesIO()
    fig.tight_layout(); fig.savefig(buf, format="png", facecolor=fig.get_facecolor()); plt.close(fig)
    return buf.getvalue()


def to_csv(positions):
    buf = io.StringIO()
    cols = ["day", "symbol", "side", "leverage", "opened_at", "closed_at", "entry", "close", "qty", "pnl", "roi"]
    w = csv.writer(buf)
    w.writerow(cols + ["minutes"])
    for p in positions:
        m = minutes(p)
        w.writerow([p.get(c) for c in cols] + ["" if m is None else round(m, 1)])
    return buf.getvalue().encode()
