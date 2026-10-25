"""Draw a day's trades on the coin's price chart: entries, exits and the path between them."""
import io
from datetime import datetime, timedelta, timezone

import scanner  # reuses its Binance session

FMT = "%Y-%m-%d %H:%M:%S"
BG, PANEL, GRID, MUTED, TEXT = "#0b0e11", "#14181d", "#262c35", "#8a93a0", "#e6e8eb"
UP, DOWN, ACCENT = "#16c784", "#ea3943", "#f0b90b"


def _local(ts, tz):
    return datetime.strptime(ts, FMT).replace(tzinfo=tz)


def candles(symbol, start, end, interval="5m"):
    """Klines between two aware datetimes (max 1500 bars per request)."""
    k = scanner.get("/fapi/v1/klines", symbol=symbol, interval=interval,
                    startTime=int(start.timestamp() * 1000), endTime=int(end.timestamp() * 1000), limit=1500)
    return [(datetime.fromtimestamp(x[0] / 1000, tz=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
            for x in k]


def pick_interval(span: timedelta):
    hours = span.total_seconds() / 3600
    return "1m" if hours <= 2 else "5m" if hours <= 10 else "15m" if hours <= 30 else "1h"


def chart_symbol(symbol, positions, tz, fetch=candles):
    """PNG bytes for one coin. positions: that coin's trades for the day (need opened_at/closed_at for markers)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    timed = [p for p in positions if p.get("opened_at") or p.get("closed_at")]
    if not timed:
        return None
    times = [_local(t, tz) for p in timed for t in (p.get("opened_at"), p.get("closed_at")) if t]
    t0, t1 = min(times), max(times)
    pad = max(timedelta(minutes=20), (t1 - t0) * 0.08)
    start, end = t0 - pad, t1 + pad
    interval = pick_interval(end - start)
    bars = fetch(symbol, start, end, interval)
    if not bars:
        return None
    step = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[interval]
    width = timedelta(minutes=step) * 0.7

    fig, (ax, axp) = plt.subplots(2, 1, figsize=(9, 5.6), dpi=150, sharex=True,
                                  gridspec_kw={"height_ratios": [4, 1.1], "hspace": 0.05})
    fig.patch.set_facecolor(BG)
    for a in (ax, axp):
        a.set_facecolor(BG)
        for s in ("top", "right", "left", "bottom"):
            a.spines[s].set_visible(False)
        a.tick_params(colors=MUTED, labelsize=7)
        a.grid(color=GRID, lw=.5)

    for t, o, h, l, c in bars:
        t = t.astimezone(tz)
        col = UP if c >= o else DOWN
        ax.vlines(t, l, h, color=col, lw=.6, alpha=.55)
        ax.bar(t, max(abs(c - o), (h - l) * 0.02 or 1e-12), bottom=min(o, c), width=width, color=col, alpha=.55, lw=0)

    for p in timed:
        win = p["pnl"] > 0
        col = UP if win else DOWN if p["pnl"] < 0 else MUTED
        ot = _local(p["opened_at"], tz) if p.get("opened_at") else None
        ct = _local(p["closed_at"], tz) if p.get("closed_at") else None
        long_ = p["side"] == "LONG"
        if ot:
            ax.scatter(ot, p["entry"], marker="^" if long_ else "v", s=70, color=UP if long_ else DOWN,
                       edgecolor=TEXT, linewidth=.6, zorder=5)
        if ct:
            ax.scatter(ct, p["close"], marker="o", s=26, facecolor=BG, edgecolor=col, linewidth=1.4, zorder=5)
        if ot and ct:
            ax.plot([ot, ct], [p["entry"], p["close"]], color=col, lw=1.6, alpha=.9, zorder=4)
        lab_t, lab_p = (ct, p["close"]) if ct else (ot, p["entry"])
        ax.annotate(f"{p['pnl']:+.2f}", (lab_t, lab_p), textcoords="offset points", xytext=(4, 6 if win else -11),
                    color=col, fontsize=7, fontweight="bold", zorder=6)

    # running P&L through the day — every trade counts; ones with hidden times use their best-known order time
    def when(p):
        t = p.get("closed_at") or p.get("opened_at") or (p.get("sort_ts") or "").rstrip("~")
        try:
            return min(max(_local(t[:19], tz), start), end)
        except (ValueError, TypeError):
            return end
    seq = sorted(positions, key=when)
    xs, ys, run = [start], [0.0], 0.0
    for p in seq:
        run += p["pnl"]
        xs.append(when(p)); ys.append(run)
    xs.append(end); ys.append(run)
    axp.step(xs, ys, where="post", color=ACCENT, lw=1.6)
    axp.axhline(0, color=GRID, lw=1)
    axp.fill_between(xs, ys, 0, step="post", color=ACCENT, alpha=.08)
    axp.set_ylabel("P&L", color=MUTED, fontsize=7)
    lo, hi = min(ys), max(ys)
    axp.set_ylim(lo - (hi - lo) * .35 - .1, hi + (hi - lo) * .35 + .1)
    axp.annotate(f"{run:+.2f} USDT", (xs[-1], run), textcoords="offset points", xytext=(-4, 5 if run >= 0 else -12),
                 ha="right", color=ACCENT, fontsize=8, fontweight="bold")
    hidden = len(positions) - len(timed) + sum(1 for p in timed if not (p.get("opened_at") and p.get("closed_at")))
    if hidden:
        axp.annotate(f"{hidden} trade(s) had hidden times: counted in P&L, not fully drawn above",
                     (0, 0), xycoords="axes fraction", xytext=(2, -26), textcoords="offset points",
                     color=MUTED, fontsize=6.5)

    total = sum(p["pnl"] for p in positions)
    w = sum(p["pnl"] > 0 for p in positions); l = sum(p["pnl"] < 0 for p in positions)
    day = (positions[0].get("day") or "")
    ax.set_title(f"{symbol.replace('USDT', '')} · {day} · {len(positions)} trades · {w}W/{l}L · {total:+.2f} USDT"
                 f"   ({interval} candles)", color=TEXT, fontsize=9, loc="left", pad=22)
    axp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    # legend
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], marker="^", ls="", color=UP, label="Long entry"),
               Line2D([], [], marker="v", ls="", color=DOWN, label="Short entry"),
               Line2D([], [], marker="o", ls="", mfc=BG, mec=MUTED, label="Exit"),
               Line2D([], [], color=UP, label="Win"), Line2D([], [], color=DOWN, label="Loss")]
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=6.5, frameon=False, ncol=5,
              labelcolor=MUTED, borderaxespad=0.2, handletextpad=0.3, columnspacing=1.2)
    # keep markers off the top/bottom edges
    ys_all = [b[2] for b in bars] + [b[3] for b in bars] + [p["entry"] for p in timed] + [p["close"] for p in timed]
    span = max(ys_all) - min(ys_all)
    ax.set_ylim(min(ys_all) - span * .06, max(ys_all) + span * .06)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def charts_for_day(positions, tz, fetch=candles, max_coins=4):
    """[(symbol, png)] for the coins traded that day, biggest absolute P&L first."""
    by = {}
    for p in positions:
        by.setdefault(p["symbol"], []).append(p)
    order = sorted(by, key=lambda s: -abs(sum(p["pnl"] for p in by[s])))[:max_coins]
    out = []
    for s in order:
        try:
            png = chart_symbol(s, by[s], tz, fetch)
        except Exception:
            png = None
        if png:
            out.append((s, png))
    return out
