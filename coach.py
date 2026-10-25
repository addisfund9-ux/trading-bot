"""Claude reads the day's numbers and writes a short, plain-spoken summary for the morning brief."""
import html
import json

import review

PROMPT = """You are a trading-journal coach for a crypto futures day trader. He scalps Binance USDT-M perpetuals at
high leverage and picks coins each morning from the top movers using EMA alignment, volume and BTC direction.
His two rules: close any losing trade still open after {max_hold:g} minutes; stop trading a coin after {max_losses}
losses in a row on it.

Below is his trade log for {day} and the rule check results. Write his morning summary:
- 4 to 6 short lines, plain English, second person ("you").
- Line 1: the result in one sentence (net P&L, trades, win rate).
- Then what worked (specific trades or habits, with numbers).
- Then the single biggest leak (specific trade or pattern, with the number it cost).
- Last line starts with "Today:" and gives one concrete focus for today based on the data.
- Only use numbers that appear in the data. Don't predict prices, don't recommend coins, don't give financial advice.
- No headings, no bullet symbols, no emojis, no markdown.

Rule check results:
{rules}

Trades in time order (P&L in USDT, minutes held where known):
{trades}

Daily total from the Binance calendar: {calendar}
"""


def build_prompt(day, positions, calendar_pnl, max_hold, max_losses):
    r = review.rule_checks(positions, max_hold, max_losses)
    s = review.stats(positions)
    rules = {
        "losers_held_too_long": len(r["held"]), "cost_of_held_losers": r["held_pnl"],
        "trades_after_loss_streak": len(r["after"]), "result_of_those_trades": r["after_pnl"],
        "side_flips_same_coin": r["flips"],
        "wins": s["wins"], "losses": s["losses"], "net_of_logged_trades": s["net"],
        "avg_win": round(s["avg_win"], 2) if s["avg_win"] is not None else None,
        "avg_loss": round(s["avg_loss"], 2) if s["avg_loss"] is not None else None,
    }
    trades = [{
        "coin": review.coin(p["symbol"]), "side": p["side"], "opened": (p.get("opened_at") or "")[11:16] or None,
        "minutes": round(review.minutes(p), 1) if review.minutes(p) is not None else None,
        "pnl": p["pnl"], "roi_pct": p.get("roi"),
    } for p in positions]
    return PROMPT.format(max_hold=max_hold, max_losses=max_losses, day=day, rules=json.dumps(rules),
                         trades=json.dumps(trades), calendar="not sent" if calendar_pnl is None else f"{calendar_pnl:+.2f}")


def rule_summary(day, positions, calendar_pnl, max_hold, max_losses):
    """A plain summary written from the numbers alone (used when no Claude API key is set)."""
    s = review.stats(positions)
    r = review.rule_checks(positions, max_hold, max_losses)
    net = calendar_pnl if calendar_pnl is not None else s["net"]
    lines = [f"You {'made' if net >= 0 else 'lost'} {abs(net):.2f} USDT on {s['n']} trades"
             + (f", winning {s['win_rate']:.0%} of them." if s["win_rate"] is not None else ".")]
    if s["avg_win"] is not None and s["avg_loss"] is not None:
        if s["avg_win"] > -s["avg_loss"]:
            lines.append(f"Your winners ({s['avg_win']:+.2f} avg) were bigger than your losers ({s['avg_loss']:+.2f} avg) — keep cutting losses early.")
        else:
            lines.append(f"Your losers ({s['avg_loss']:+.2f} avg) were bigger than your winners ({s['avg_win']:+.2f} avg) — that is the main thing to fix.")
    best = max(positions, key=lambda p: p["pnl"])
    if best["pnl"] > 0:
        m = review.minutes(best)
        lines.append(f"Best trade: {review.coin(best['symbol'])} {best['side'].lower()} {best['pnl']:+.2f}"
                     + (f", held {m:.0f} min." if m is not None else "."))
    # the single biggest leak
    leaks = []
    if r["held"]:
        leaks.append((r["held_pnl"], f"holding {len(r['held'])} loser(s) past {max_hold:g} minutes cost {r['held_pnl']:+.2f}"))
    if r["after"] and r["after_pnl"] < 0:
        leaks.append((r["after_pnl"], f"trading on after {max_losses} losses in a row cost {r['after_pnl']:+.2f}"))
    worst = min(positions, key=lambda p: p["pnl"])
    if worst["pnl"] < 0:
        m = review.minutes(worst)
        leaks.append((worst["pnl"], f"the {review.coin(worst['symbol'])} {worst['side'].lower()} lost {worst['pnl']:+.2f}"
                      + (f" after {m:.0f} min" if m is not None else "")))
    if leaks:
        lines.append("Biggest leak: " + min(leaks, key=lambda x: x[0])[1] + ".")
    if r["held"]:
        focus = f"close any losing trade at {max_hold:g} minutes, no exceptions."
    elif r["after"] and r["after_pnl"] < 0:
        focus = f"after {max_losses} losses in a row on a coin, switch coins or stop."
    elif r["flips"] >= 4:
        focus = "pick one direction per coin from the scanner and avoid flipping long/short."
    else:
        focus = "repeat what worked — same rules, same size."
    lines.append("Today: " + focus)
    return html.escape("\n".join(lines))


def summary(client, model, day, positions, calendar_pnl, max_hold, max_losses):
    """Returns HTML-safe text, or None if there is nothing to summarise. Without a Claude client it uses
    the rule-based summary."""
    if not positions:
        return None
    if client is None:
        return rule_summary(day, positions, calendar_pnl, max_hold, max_losses)
    msg = client.messages.create(model=model, max_tokens=600,
                                 messages=[{"role": "user", "content": build_prompt(day, positions, calendar_pnl,
                                                                                    max_hold, max_losses)}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return html.escape(text) if text else None
