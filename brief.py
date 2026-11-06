"""The morning brief's first message: yesterday in two lines, today's slow-and-steady plan, and the tracker.

Kept free of Telegram and the database so it can be tested on its own.
"""
from datetime import date, datetime

BROKE = ("revenge", "flip", "lev_up", "size_up", "over_count", "past_limit")


def split_at_first_break(rows):
    """P&L before the first broken rule of the day and from it on (rows from tilt.analyse, in time order)."""
    i = next((k for k, r in enumerate(rows) if any(r.get(f) for f in BROKE)), None)
    if i is None:
        return sum(r["pnl"] for r in rows), None, None
    when = (rows[i].get("opened_at") or "")[11:16]
    return sum(r["pnl"] for r in rows[:i]), sum(r["pnl"] for r in rows[i:]), when


def is_rest_day(yday_pnl, loss_limit, enabled=True):
    """After a day that went past the daily stop, the next day is a rest day."""
    return enabled and yday_pnl is not None and yday_pnl <= -abs(loss_limit)


def text(now: datetime, focus: str, yday: date, yday_rows, s: dict, tracker: dict, rule: str | None):
    """s: max_pos, max_lev, risk, max_trades, last_entry, loss_limit. tracker: start, pnl, days, clean."""
    L = [f"☀️ <b>Good morning, Dagi</b> · {now:%A %d %B}"]
    if focus:
        L += ["", focus]

    ypnl = sum(r["pnl"] for r in yday_rows) if yday_rows else None
    L.append("")
    if yday_rows:
        before, after, when = split_at_first_break(yday_rows)
        L.append(f"📒 <b>Yesterday</b> ({yday:%a}): {ypnl:+.2f} from {len(yday_rows)} trade{'s' if len(yday_rows) != 1 else ''}")
        if after is None:
            L.append("Every trade inside your rules. That's the whole job. 👏")
        else:
            L.append(f"Before the first broken rule: <b>{before:+.2f}</b> · from {when} on: <b>{after:+.2f}</b>")
    else:
        L.append(f"📒 <b>Yesterday</b> ({yday:%a}): no trades. A rest day counts as a clean day.")

    rest = is_rest_day(ypnl, s["loss_limit"], s.get("rest_day", True))
    L.append("")
    if rest:
        L += ["🛌 <b>Today is a rest day.</b>",
              f"Yesterday went past your −{s['loss_limit']:g} daily stop, so today there are no trades. "
              "Nothing to win back: the market will be here tomorrow. Put the morning into applications."]
    else:
        last = f", entries before {s['last_entry']}" if s.get("last_entry") else ""
        L += ["🐢 <b>Slow and steady: today's plan</b>",
              f"1. Position size up to <b>${s['max_pos']:g}</b>, leverage up to <b>{s['max_lev']}×</b>.",
              f"2. Stop set <i>before</i> you enter, past the last swing. Risk about <b>${s['risk']:g}</b>.",
              f"3. At most <b>{s['max_trades']}</b> trades{last}.",
              "4. Day green? You're done. Close the app.",
              "5. Two losses in a row? Done for the day.",
              "6. Don't enter on the big candle. Wait for the pullback, or skip it.",
              "7. Feel the heat? Put the phone down for 15 minutes before anything else."]
        if rule:
            L += ["", f"<b>Rule for today:</b> {rule}"]

    L.append("")
    st = tracker
    L.append(f"📈 <b>Slow and steady since {st['start']:%a %d %b}</b>: {st['pnl']:+.2f} · "
             f"clean days {st['clean']}/{st['days']}")
    return "\n".join(L), rest


def tracker(positions_by_day, start: date, today: date, clean_fn):
    """positions_by_day: {day_iso: [positions]} for start..yesterday. clean_fn(rows) -> bool."""
    pnl, days, clean = 0.0, 0, 0
    for d, rows in sorted(positions_by_day.items()):
        if not (start.isoformat() <= d < today.isoformat()):
            continue
        days += 1
        pnl += sum(p["pnl"] for p in rows)
        clean += bool(clean_fn(rows))
    return {"start": start, "pnl": pnl, "days": days, "clean": clean}
