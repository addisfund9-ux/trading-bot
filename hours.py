"""Time of day: when the market is busy, and when YOU make or lose money.

market_profile(): average volume per hour (Addis time) over the last N days for the most-traded coins,
                  each coin scaled to its own average so big coins don't drown the rest (1.0 = a normal hour).
my_profile():     your logged trades grouped by the hour you opened them.
"""
import io
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import scanner as sc

TZ_H = 3   # Addis Ababa = UTC+3


def market_profile(days=14, coins=40):
    tick = sc.get("/fapi/v1/ticker/24hr")
    syms = [t["symbol"] for t in sorted((t for t in tick if t["symbol"].endswith("USDT") and float(t["quoteVolume"]) >= 2e7),
                                        key=lambda t: -float(t["quoteVolume"]))][:coins]

    def one(s):
        k = sc.safe(sc.get, "/fapi/v1/klines", symbol=s, interval="1h", limit=24 * days)
        if not isinstance(k, list) or len(k) < 48:
            return None
        q = [float(x[7]) for x in k]
        a = sum(q) / len(q) or 1
        return [((int(x[0]) // 3_600_000 + TZ_H) % 24, float(x[7]) / a, (float(x[2]) / float(x[3]) - 1) * 100) for x in k]

    vol, rng = defaultdict(list), defaultdict(list)
    with ThreadPoolExecutor(4) as ex:
        for rows in ex.map(one, syms):
            for h, v, r in rows or []:
                vol[h].append(v); rng[h].append(r)
    return {h: {"vol": sum(vol[h]) / len(vol[h]), "range": sum(rng[h]) / len(rng[h])} for h in range(24) if vol[h]}


def my_profile(positions):
    out = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for p in positions:
        t = p.get("opened_at")
        if not t:
            continue
        b = out[int(t[11:13])]
        b["n"] += 1; b["pnl"] += p["pnl"]; b["wins"] += p["pnl"] > 0
    return dict(out)


def window(mine, start, end):
    """Totals for your trades opened between start and end (hours, end exclusive)."""
    hs = [h for h in mine if start <= h < end]
    n = sum(mine[h]["n"] for h in hs)
    return {"n": n, "pnl": sum(mine[h]["pnl"] for h in hs), "wins": sum(mine[h]["wins"] for h in hs)}


def busy_hours(mkt, k=4):
    return sorted(sorted(mkt, key=lambda h: -mkt[h]["vol"])[:k])


def text(mkt, mine):
    busy = busy_hours(mkt)
    lines = ["<b>🕒 When the market moves — and when you make money</b>",
             "Market volume by hour (Addis time), last 14 days, 40 most-traded coins. 1.0× = a normal hour.", ""]
    top = sorted(mkt, key=lambda h: -mkt[h]["vol"])[:5]
    lines.append("<b>Busiest hours:</b> " + ", ".join(f"{h:02d}:00 ({mkt[h]['vol']:.1f}×)" for h in top))
    quiet = sorted(mkt, key=lambda h: mkt[h]["vol"])[:3]
    lines.append("<b>Quietest:</b> " + ", ".join(f"{h:02d}:00 ({mkt[h]['vol']:.1f}×)" for h in quiet))
    lines += ["", "<b>Your trades by when you opened them</b>"]
    for name, a, b in (("Night 00–08", 0, 8), ("Morning 08–12", 8, 12), ("Afternoon 12–16", 12, 16),
                       ("US open 16–19", 16, 19), ("Evening 19–24", 19, 24)):
        w = window(mine, a, b)
        if w["n"]:
            lines.append(f"{name}: {w['n']} trades · {w['pnl']:+.2f} · {w['wins'] / w['n'] * 100:.0f}% won")
    us = window(mine, 16, 19)
    if us["n"] >= 5 and us["pnl"] < 0:
        lines += ["", f"⚠️ The busiest hours of the day (the US open, 16:00–19:00) are your worst: "
                      f"{us['pnl']:+.2f} from {us['n']} trades. More volume means bigger, faster swings — "
                      "great when you're calm, brutal when you're chasing. Your money is made in the quiet morning."]
    return "\n".join(lines)


def chart(mkt, mine):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    BG, INK, MUTED, LINE, UP, DN, AMBER = "#0e1524", "#e8edf6", "#8a94a8", "#26324a", "#2ee6a6", "#ff5c7a", "#f0b90b"
    hrs = list(range(24))
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(8, 6.4), dpi=130, sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    fig.patch.set_facecolor(BG)
    v = [mkt.get(h, {}).get("vol", 0) for h in hrs]
    busy = busy_hours(mkt)
    a1.bar(hrs, v, color=[AMBER if h in busy else "#3a4a6b" for h in hrs], width=0.8)
    a1.axhline(1, color=LINE, lw=1)
    a1.set_title("Market volume by hour (Addis time) — 1.0× is a normal hour", color=INK, fontsize=11, loc="left")
    for h, lab in ((3, "Asia day starts"), (16, "US open")):
        if h in mkt:
            a1.annotate(lab, (h, mkt[h]["vol"]), xytext=(0, 6), textcoords="offset points", ha="center", color=MUTED, fontsize=8)
    p = [mine.get(h, {}).get("pnl", 0) for h in hrs]
    a2.bar(hrs, p, color=[UP if x >= 0 else DN for x in p], width=0.8)
    a2.axhline(0, color=LINE, lw=1)
    a2.set_title("Your result by the hour you opened the trade (USDT)", color=INK, fontsize=11, loc="left")
    for ax in (a1, a2):
        ax.set_facecolor(BG); ax.tick_params(colors=MUTED, labelsize=8)
        for s in ax.spines.values():
            s.set_color(LINE)
        ax.grid(axis="y", color="#1c2638", lw=0.8); ax.set_axisbelow(True)
    a2.set_xticks(hrs); a2.set_xticklabels([f"{h:02d}" for h in hrs])
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=BG); plt.close(fig)
    return buf.getvalue()
