"""Read Binance Futures screenshots with the Claude API and turn them into structured trades."""
import base64
import json
import re
from datetime import date

import anthropic

PROMPT = """You are reading a screenshot from the Binance app (USDⓈ-M Futures). Today is {today}.
It is one of two kinds of screen:

1. "Position History" — a list of closed positions. Each card has: a badge "B" (long) or "S" (short), the symbol
   (e.g. JCTUSDT), leverage (e.g. "Cross 12x"), Realized PNL (USDT), ROI %, Closed Vol., Avg. Entry Price,
   Avg. Close Price, Max OI, and "Opened" / "Closed" timestamps.
2. "Futures PNL Analysis" — a calendar with a P&L number under each day.

Return ONLY a JSON object, no other text:
{{
  "positions": [
    {{"symbol": "JCTUSDT", "side": "LONG" | "SHORT", "leverage": 12, "pnl": 1.35, "roi": 68.77,
      "entry": 0.007268, "close": 0.0068465, "qty": 3243,
      "opened_at": "2026-10-25 13:22:04" | null, "closed_at": "2026-10-25 13:31:49" | null}}
  ],
  "daily": [{{"date": "2026-10-25", "pnl": 8.44}}]
}}

Rules:
- List positions in the order they appear, top to bottom.
- "B" badge = LONG, "S" badge = SHORT.
- Copy every digit of prices exactly as shown (e.g. 0.0063817). Negative P&L and ROI are negative numbers.
- qty is the "Closed Vol." number. Remove thousands separators.
- If a card is cut off, include it only if symbol, side, pnl, entry, close and qty are all readable;
  use null for any timestamp that is not visible.
- For the calendar, include only days that show a number (including 0.00). Work out the month and year from
  the screen or, if it is hidden, from today's date.
- If the screenshot is neither screen, return {{"positions": [], "daily": []}}.
"""


def _media_type(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def parse_reply(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"positions": [], "daily": []}
    data = json.loads(m.group(0))
    return {"positions": data.get("positions") or [], "daily": data.get("daily") or []}


def read_screenshot(client: anthropic.Anthropic, model: str, image: bytes, today: date) -> dict:
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": _media_type(image),
                                             "data": base64.standard_b64encode(image).decode()}},
                {"type": "text", "text": PROMPT.format(today=today.isoformat())},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return parse_reply(text)


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace(",", "").replace("%", "").strip())


def clean_positions(raw: list, today: date) -> list:
    """Validate, normalise and date each position. Cards with no visible time borrow the date of a neighbour
    in the same screenshot (the list is in time order), else today's date flagged as a guess."""
    out = []
    for r in raw:
        try:
            p = {
                "symbol": str(r["symbol"]).upper().replace("/", "").strip(),
                "side": "LONG" if str(r["side"]).upper().startswith(("L", "B")) else "SHORT",
                "leverage": int(_num(r.get("leverage"))) if r.get("leverage") not in (None, "") else None,
                "pnl": _num(r["pnl"]), "roi": _num(r.get("roi")),
                "entry": _num(r["entry"]), "close": _num(r["close"]), "qty": _num(r["qty"]),
                "opened_at": r.get("opened_at") or None, "closed_at": r.get("closed_at") or None,
            }
        except (KeyError, TypeError, ValueError):
            continue
        if not p["symbol"] or p["entry"] in (None, 0) or p["close"] is None or not p["qty"]:
            continue
        p["sort_ts"] = p["opened_at"] or p["closed_at"]
        p["day"] = (p["sort_ts"] or "")[:10] or None
        out.append(p)
    known = [(i, p) for i, p in enumerate(out) if p["sort_ts"]]
    for i, p in enumerate(out):
        if p["day"]:
            continue
        # The list is newest first. Prefer the next OLDER card: this trade came after it, so sort just after
        # its close. Otherwise use the next NEWER card and sort just before its open.
        older = next(((j, q) for j, q in known if j > i), None)
        newer = next(((j, q) for j, q in reversed(known) if j < i), None)
        if older:
            j, q = older
            p["sort_ts"] = (q["closed_at"] or q["opened_at"] + "~")
        elif newer:
            j, q = newer
            p["sort_ts"] = (q["opened_at"] or q["closed_at"])[:-1]
        else:
            p["day"], p["day_guessed"] = today.isoformat(), 1
            continue
        p["day"] = q["sort_ts"][:10]
        p["day_guessed"] = 1 if abs(j - i) > 1 else 0
    return out


def clean_daily(raw: list) -> list:
    out = []
    for r in raw:
        try:
            d = str(r["date"])[:10]
            date.fromisoformat(d)
            out.append((d, _num(r["pnl"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out
