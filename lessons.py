"""Lessons and rings: learn from your own mistakes.

Rings (one set per day, like an activity tracker):
  • Calm  — share of trades taken calmly (no revenge, flip, size/leverage-up, overtrading or past-the-stop entries)
  • Wins  — share of trades that made money
  • Rules — share of your six rules you kept that day

Lessons rank your mistakes by what they cost, give one rule for today, and count your clean-day streak.
"""
import io
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

import review
import tilt

CALM_C, WIN_C, RULE_C = "#2ee6a6", "#f0b90b", "#6aa8ff"
BG, PANEL, INK, MUTED, LINE = "#0e1524", "#141d30", "#e8edf6", "#8a94a8", "#26324a"
UP, DN = "#2ee6a6", "#ff5c7a"

MISTAKES = {
    "flip": ("Flipping after a loss",
             "Reversing straight after a loss is an emotional bet that the market owes you. You're trading the loss, "
             "not a setup.",
             "After a loss, no trade on that coin in the other direction for the rest of the day."),
    "revenge": ("Revenge entries",
                "Trades opened inside your cooldown after a loss. They're taken to win the money back, so you skip "
                "your checks and enter late.",
                "After any loss, wait {cooldown} minutes. Stand up, then re-check the setup before entering."),
    "size_up": ("Bigger size after a loss",
                "Doubling up to win it back means your worst decisions carry your biggest risk.",
                "After a loss, the next trade is the same size or smaller. Never bigger."),
    "lev_up": ("Higher leverage after a loss",
               "Raising leverage after a loss shrinks the distance to liquidation exactly when your judgement is worst.",
               "Leverage only goes up on a green day, never right after a loss."),
    "past_limit": ("Trading past your daily stop",
                   "Once you've given back your daily limit, every extra trade is taken while tilted.",
                   "When the day is {limit} USDT below its high, close the app. Tomorrow's setups will still be there."),
    "over_count": ("Too many trades",
                   "Trades beyond your daily cap are usually lower quality. The best setups come early.",
                   "Maximum {max_trades} trades today. Count them."),
    "held_loser": ("Holding losers too long",
                   "A scalp that's losing after {hold} minutes rarely comes back. Holding turns small losses into big ones.",
                   "Every trade gets a stop when you enter. Losing after {hold} minutes means close it."),
    "coin_repeat": ("Fighting the same coin",
                    "Losing on a coin, then trying it again and again. The coin isn't doing what you think.",
                    "Two losses on a coin means you're done with it today."),
}
RULE_KEYS = ["revenge", "flip", "over_count", "past_limit", "held_loser", "coin_repeat"]


def tag_trades(positions, cooldown=15, max_trades=8, loss_limit=5.0, max_hold=10, max_losses=2):
    """tilt.analyse rows plus held_loser / coin_repeat flags and a calm flag."""
    a = tilt.analyse(positions, cooldown, max_trades, loss_limit)
    losses = defaultdict(int)
    for r in a["rows"]:
        m = review.minutes(r)
        r["held_loser"] = r["pnl"] < 0 and m is not None and m > max_hold
        r["coin_repeat"] = losses[r["symbol"]] >= max_losses
        if r["pnl"] < 0:
            losses[r["symbol"]] += 1
        r["revenge_only"] = r["revenge"] and not r["flip"]
        r["calm"] = not any(r[k] for k in ("revenge", "flip", "lev_up", "size_up", "over_count", "past_limit"))
    return a


def day_rings(rows):
    n = len(rows)
    if not n:
        return None
    kept = sum(1 for k in RULE_KEYS if not any(r[k] for r in rows))
    return {"n": n, "pnl": sum(r["pnl"] for r in rows),
            "calm": sum(r["calm"] for r in rows) / n, "calm_n": sum(r["calm"] for r in rows),
            "win": sum(r["pnl"] > 0 for r in rows) / n, "win_n": sum(r["pnl"] > 0 for r in rows),
            "rules": kept / len(RULE_KEYS), "rules_n": kept,
            "clean": not any(r["revenge"] or r["flip"] or r["past_limit"] for r in rows)}


def mistakes(rows):
    """Each mistake with how often it happened and what those trades made in total, costliest first."""
    out = []
    for key in MISTAKES:
        col = "revenge_only" if key == "revenge" else key
        hit = [r for r in rows if r.get(col)]
        if hit:
            out.append({"key": key, "n": len(hit), "pnl": sum(r["pnl"] for r in hit)})
    return sorted(out, key=lambda m: m["pnl"])


def build(con_positions_by_day, last_day, settings, n_days=7):
    """con_positions_by_day: {day_iso: [positions]}. Returns everything the text and the picture need."""
    days = [(last_day - timedelta(days=i)).isoformat() for i in range(n_days - 1, -1, -1)]
    per_day, all_rows = {}, []
    for d in days:
        a = tag_trades(con_positions_by_day.get(d, []), settings["cooldown"], settings["max_trades"],
                       settings["loss_limit"], settings["max_hold"], settings["max_losses"])
        per_day[d] = {"rows": a["rows"], "rings": day_rings(a["rows"])}
        all_rows += a["rows"]
    streak = 0
    for d in reversed(days):
        r = per_day[d]["rings"]
        if r is None:
            continue
        if not r["clean"]:
            break
        streak += 1
    calm = [r for r in all_rows if r["calm"]]
    tilted = [r for r in all_rows if not r["calm"]]
    return {"days": days, "per_day": per_day, "last": last_day.isoformat(), "mistakes": mistakes(all_rows),
            "today_mistakes": mistakes(per_day[last_day.isoformat()]["rows"]), "streak": streak,
            "calm_pnl": sum(r["pnl"] for r in calm), "calm_n": len(calm),
            "tilt_pnl": sum(r["pnl"] for r in tilted), "tilt_n": len(tilted), "settings": settings}


def rule_for_today(data):
    if not data["mistakes"]:
        return None
    k = data["mistakes"][0]["key"]
    s = data["settings"]
    return MISTAKES[k][2].format(cooldown=f"{s['cooldown']:g}", limit=f"{s['loss_limit']:g}",
                                 max_trades=s["max_trades"], hold=f"{s['max_hold']:g}")


def text(data):
    s = data["settings"]
    fmt = dict(cooldown=f"{s['cooldown']:g}", limit=f"{s['loss_limit']:g}", max_trades=s["max_trades"],
               hold=f"{s['max_hold']:g}")
    last = data["per_day"][data["last"]]["rings"]
    lines = ["<b>📚 Lessons from your trades</b>"]
    rule = rule_for_today(data)
    if rule:
        lines.append(f"\n<b>Rule for today:</b> {rule}")
    lines.append(f"\n🔥 Clean-day streak: <b>{data['streak']}</b> day{'s' if data['streak'] != 1 else ''} "
                 "(no revenge, no flips, stopped at your daily limit)")
    if last:
        lines.append(f"\n<b>{datetime.fromisoformat(data['last']):%a %d %b}</b>: {last['n']} trades, {last['pnl']:+.2f} · "
                     f"calm {last['calm_n']}/{last['n']} · wins {last['win_n']}/{last['n']} · rules kept "
                     f"{last['rules_n']}/{len(RULE_KEYS)}")
    lines.append(f"\n<b>Last 7 days</b>: calm trades {data['calm_n']} → <b>{data['calm_pnl']:+.2f}</b> · "
                 f"tilted trades {data['tilt_n']} → <b>{data['tilt_pnl']:+.2f}</b>")
    if data["mistakes"]:
        lines.append("\n<b>What your mistakes cost (7 days)</b>")
        for i, m in enumerate(data["mistakes"][:5], 1):
            name, why, _ = MISTAKES[m["key"]]
            lines.append(f"{i}. <b>{name}</b>: {m['n']} trade{'s' if m['n'] != 1 else ''}, {m['pnl']:+.2f}\n"
                         f"   <i>{why.format(**fmt)}</i>")
        lines.append("\n<i>A trade can show up under more than one mistake.</i>")
    else:
        lines.append("\nNo mistakes logged in the last 7 days. Keep doing exactly that.")
    return "\n".join(lines)


# ---------------- the picture ----------------
def _rings(ax, cx, cy, r, vals, width, label_mid=None, mid_size=14, mid_col=INK, sub=None):
    from matplotlib.patches import Wedge, Circle
    for i, (v, col) in enumerate(zip(vals, (CALM_C, WIN_C, RULE_C))):
        rr = r - i * (width * 1.18)
        ax.add_patch(Circle((cx, cy), rr - width / 2, fill=False, ec=col, lw=0, alpha=0))
        ax.add_patch(Wedge((cx, cy), rr, 0, 360, width=width, fc=col, alpha=0.16, lw=0))
        if v is not None and v > 0:
            ax.add_patch(Wedge((cx, cy), rr, 90 - 360 * min(v, 1), 90, width=width, fc=col, lw=0))
            # rounded caps
            for ang in (90, 90 - 360 * min(v, 1)):
                a = math.radians(ang)
                ax.add_patch(Circle((cx + (rr - width / 2) * math.cos(a), cy + (rr - width / 2) * math.sin(a)),
                                    width / 2, fc=col, lw=0))
    if label_mid is not None:
        ax.text(cx, cy + (0.04 * r if sub else 0), label_mid, ha="center", va="center", color=mid_col,
                fontsize=mid_size, fontweight="bold")
    if sub:
        ax.text(cx, cy - 0.17 * r, sub, ha="center", va="center", color=MUTED, fontsize=mid_size * 0.5)


def chart(data):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    last = data["per_day"][data["last"]]
    R = last["rings"]
    fig = plt.figure(figsize=(7.2, 9.6), dpi=140)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 72); ax.set_ylim(0, 96); ax.axis("off")
    ax.set_aspect("equal")
    d = datetime.fromisoformat(data["last"])
    ax.text(4, 91.5, f"{d:%A %d %B}", color=MUTED, fontsize=11)
    ax.text(4, 87, "Your trading rings", color=INK, fontsize=19, fontweight="bold")

    # --- today's rings
    if R:
        _rings(ax, 20, 66, 15, [R["calm"], R["win"], R["rules"]], 2.7, f"{R['pnl']:+.2f}", 15,
               UP if R["pnl"] >= 0 else DN, "USDT today")
        rows = [("CALM", f"{R['calm_n']}/{R['n']}", "trades taken calmly", CALM_C),
                ("WINS", f"{R['win_n']}/{R['n']}", "trades in profit", WIN_C),
                ("RULES", f"{R['rules_n']}/{len(RULE_KEYS)}", "rules kept", RULE_C)]
        for i, (k, v, sub, col) in enumerate(rows):
            y = 76 - i * 9.5
            ax.text(41, y, k, color=col, fontsize=10, fontweight="bold")
            ax.text(41, y - 4.3, v, color=INK, fontsize=21, fontweight="bold")
            ax.text(53, y - 3.6, sub, color=MUTED, fontsize=9)
    else:
        _rings(ax, 20, 66, 15, [0, 0, 0], 2.7, "0", 15, MUTED, "trades yet")
        ax.text(41, 68, "No trades logged", color=INK, fontsize=14, fontweight="bold")
        ax.text(41, 63.5, "A day off never breaks\nyour streak.", color=MUTED, fontsize=9.5, va="top")

    # --- trade beads
    ax.add_patch(matplotlib.patches.FancyBboxPatch((3, 31), 66, 17, boxstyle="round,pad=0,rounding_size=2",
                                                   fc=PANEL, ec=LINE, lw=1))
    ax.text(5.5, 44.8, "Every trade, in order", color=INK, fontsize=10.5, fontweight="bold")
    ax.text(66.5, 44.8, "○ calm    ● tilted", color=MUTED, fontsize=8.5, ha="right")
    rows = last["rows"]
    if not rows:
        ax.text(36, 38.8, "Nothing yet", color=MUTED, fontsize=10, ha="center", va="center")
    if rows:
        n = len(rows)
        step = 58 / max(1, n - 1) if n > 1 else 0
        cap = min(2.4, max(0.6, step * 0.45)) if n > 1 else 2.4
        big = max(abs(r["pnl"]) for r in rows) or 1
        mid = 38.8
        ax.plot([5.5, 66.5], [mid, mid], color=LINE, lw=1, zorder=0)
        for i, r in enumerate(rows):
            x = 7 + step * i if n > 1 else 36
            sz = 0.45 + (cap - 0.45) * math.sqrt(abs(r["pnl"]) / big)
            col = UP if r["pnl"] >= 0 else DN
            yy = mid + (cap + 0.3 if r["pnl"] >= 0 else -(cap + 0.3))
            ax.plot([x, x], [mid, yy], color=col, lw=0.8, alpha=0.5, zorder=0)
            if r["calm"]:
                ax.add_patch(plt.Circle((x, yy), sz, fc=BG, ec=col, lw=1.6))
            else:
                ax.add_patch(plt.Circle((x, yy), sz, fc=col, ec=col, lw=1.2))
            if n <= 12:  # coin names under the beads
                ax.text(x, 32.4, r["symbol"].replace("USDT", ""), color=MUTED, fontsize=6.5, ha="center")

    # --- week
    ax.text(4, 27.5, "Last 7 days", color=INK, fontsize=10.5, fontweight="bold")
    ax.text(68, 27.5, f"clean-day streak: {data['streak']}",
            color=MUTED, fontsize=9, ha="right")
    for i, dd in enumerate(data["days"]):
        x = 8.5 + i * 9.2
        rr = data["per_day"][dd]["rings"]
        _rings(ax, x, 18, 4.1, [rr["calm"], rr["win"], rr["rules"]] if rr else [0, 0, 0], 0.85)
        dt = datetime.fromisoformat(dd)
        ax.text(x, 11.6, f"{dt:%a}"[0] + f"{dt:%a}"[1:3].lower(), color=INK if dd == data["last"] else MUTED,
                fontsize=8.5, ha="center", fontweight="bold" if dd == data["last"] else "normal")
        ax.text(x, 8.6, "–" if not rr else f"{rr['pnl']:+.1f}", color=MUTED if not rr else (UP if rr["pnl"] >= 0 else DN),
                fontsize=8, ha="center")

    rule = rule_for_today(data)
    if rule:
        import textwrap
        ax.add_patch(matplotlib.patches.FancyBboxPatch((3, 1.6), 66, 5.4, boxstyle="round,pad=0,rounding_size=1.6",
                                                       fc="#1d2a1f" if False else PANEL, ec=RULE_C, lw=1))
        ax.text(5.5, 5.4, "RULE FOR TODAY", color=RULE_C, fontsize=7.5, fontweight="bold")
        ax.text(5.5, 3.0, textwrap.shorten(rule, 110), color=INK, fontsize=8.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
