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


def summary(client, model, day, positions, calendar_pnl, max_hold, max_losses):
    """Returns HTML-safe text, or None if there is nothing to summarise or the call fails."""
    if not positions:
        return None
    msg = client.messages.create(model=model, max_tokens=600,
                                 messages=[{"role": "user", "content": build_prompt(day, positions, calendar_pnl,
                                                                                    max_hold, max_losses)}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return html.escape(text) if text else None
