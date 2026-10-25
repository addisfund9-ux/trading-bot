"""Read Binance Futures export files (CSV or Excel) without any AI.

Handles two kinds of export, recognised by their column names:
  • Position History — one row per closed position (entry, close, size, realized P&L, open/close time)
  • Trade History    — one row per fill (time, symbol, BUY/SELL, price, qty, realized profit);
                       fills are grouped into positions: a position opens when the coin's net size
                       leaves 0 and closes when it returns to 0.
Times in Binance exports are UTC unless the header says otherwise; they are converted to your time zone.
"""
import csv
import io
import re
from datetime import datetime, timezone

FMT = "%Y-%m-%d %H:%M:%S"


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9eE+\-.]", "", str(v).replace(",", ""))  # strips units like "USDT", "%"
    try:
        return float(s)
    except ValueError:
        return None


def _time(v, tz, utc=True):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        if re.fullmatch(r"\d{12,13}", s):                       # epoch milliseconds
            dt = datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        else:
            dt = None
            for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                      "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(s[:19], f)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if utc else tz)
    return dt.astimezone(tz).strftime(FMT)


def read_rows(data: bytes, filename: str):
    """List of dicts keyed by normalised header."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
    else:
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    # header = first row that has at least 4 non-empty cells and mentions a symbol
    start = 0
    for i, r in enumerate(rows[:15]):
        cells = [_norm(c) for c in r]
        if sum(1 for c in cells if c) >= 4 and any("symbol" in c or "contract" in c or "pair" in c for c in cells):
            start = i
            break
    header = [_norm(c) for c in rows[start]] if rows else []
    raw_header = [str(c or "") for c in rows[start]] if rows else []
    out = []
    for r in rows[start + 1:]:
        if not any(c not in (None, "") for c in r):
            continue
        out.append({header[i]: r[i] for i in range(min(len(header), len(r))) if header[i]})
    return header, raw_header, out


def _col(header, *candidates, avoid=()):
    for cand in candidates:
        for h in header:
            if cand in h and not any(a in h for a in avoid):
                return h
    return None


def detect(header):
    has = lambda *c: _col(header, *c) is not None
    if has("entryprice", "avgentry", "openprice") and has("closeprice", "avgclose", "exitprice"):
        return "positions"
    if has("realizedprofit", "realizedpnl", "realisedprofit") and has("side", "direction") and has("price"):
        return "trades"
    return None


def _symbol(v):
    s = str(v or "").upper().replace("/", "").replace("-", "").replace(" ", "")
    s = s.replace("PERPETUAL", "").replace("PERP", "")
    return s


def parse_positions(header, rows, tz, utc):
    c = {
        "sym": _col(header, "symbol", "contract", "pair"),
        "side": _col(header, "side", "direction", "positionside", "type"),
        "entry": _col(header, "avgentryprice", "entryprice", "openprice", "avgentry"),
        "close": _col(header, "avgcloseprice", "closeprice", "exitprice", "avgclose"),
        "qty": _col(header, "closedvol", "closevolume", "closedquantity", "closingquantity", "quantity", "size", "maxoi",
                    "volume", avoid=("usdt", "value")),
        "pnl": _col(header, "realizedpnl", "realisedpnl", "realizedprofit", "pnl", avoid=("roi", "%")),
        "roi": _col(header, "roi"),
        "open": _col(header, "opentime", "opened", "openedtime", "opendate"),
        "closed": _col(header, "closetime", "closed", "closedtime", "closedate", "updatetime"),
        "lev": _col(header, "leverage"),
    }
    out = []
    for r in rows:
        try:
            side_raw = str(r.get(c["side"], "") or "").upper()
            entry, close, pnl = _num(r.get(c["entry"])), _num(r.get(c["close"])), _num(r.get(c["pnl"]))
            if side_raw.startswith(("L", "B")):
                side = "LONG"
            elif side_raw.startswith("S"):
                side = "SHORT"
            elif entry and close and pnl is not None and close != entry:
                side = "LONG" if (pnl >= 0) == (close > entry) else "SHORT"
            else:
                continue
            p = {"symbol": _symbol(r.get(c["sym"])), "side": side, "entry": entry, "close": close,
                 "qty": abs(_num(r.get(c["qty"])) or 0), "pnl": pnl, "roi": _num(r.get(c["roi"])) if c["roi"] else None,
                 "leverage": int(_num(r.get(c["lev"]))) if c["lev"] and _num(r.get(c["lev"])) else None,
                 "opened_at": _time(r.get(c["open"]), tz, utc) if c["open"] else None,
                 "closed_at": _time(r.get(c["closed"]), tz, utc) if c["closed"] else None}
        except (TypeError, ValueError):
            continue
        if not p["symbol"] or not p["entry"] or p["close"] is None or p["pnl"] is None or not p["qty"]:
            continue
        p["sort_ts"] = p["opened_at"] or p["closed_at"]
        p["day"] = (p["sort_ts"] or "")[:10] or None
        if p["day"]:
            out.append(p)
    return out


def parse_trades(header, rows, tz, utc):
    c = {
        "time": _col(header, "dateutc", "date", "time"),
        "sym": _col(header, "symbol", "contract", "pair"),
        "side": _col(header, "side", "direction"),
        "price": _col(header, "price", avoid=("avg", "stop", "trigger")),
        "qty": _col(header, "quantity", "qty", "executed", "filled", "size", avoid=("quote",)),
        "pnl": _col(header, "realizedprofit", "realizedpnl", "realisedprofit"),
        "fee": _col(header, "fee", avoid=("coin", "asset")),
    }
    fills = []
    for r in rows:
        t = _time(r.get(c["time"]), tz, utc)
        sym = _symbol(r.get(c["sym"]))
        side = str(r.get(c["side"], "") or "").upper()
        price, qty = _num(r.get(c["price"])), _num(r.get(c["qty"]))
        if not t or not sym or side[:1] not in ("B", "S") or not price or not qty:
            continue
        fills.append({"t": t, "sym": sym, "buy": side.startswith("B"), "price": price, "qty": abs(qty),
                      "pnl": _num(r.get(c["pnl"])) or 0.0})
    fills.sort(key=lambda f: f["t"])
    out, open_pos = [], {}
    eps = 1e-12
    for f in fills:
        s = f["sym"]
        signed = f["qty"] if f["buy"] else -f["qty"]
        pos = open_pos.get(s)
        if pos is None:
            open_pos[s] = {"side": "LONG" if f["buy"] else "SHORT", "net": signed, "open": f["t"],
                           "in_qty": f["qty"], "in_cost": f["qty"] * f["price"], "out_qty": 0.0, "out_cost": 0.0,
                           "pnl": f["pnl"], "max": abs(signed)}
            continue
        adding = (pos["net"] > 0) == (signed > 0)
        if adding:
            pos["in_qty"] += f["qty"]; pos["in_cost"] += f["qty"] * f["price"]
            pos["net"] += signed; pos["pnl"] += f["pnl"]; pos["max"] = max(pos["max"], abs(pos["net"]))
            continue
        # reducing (and maybe flipping)
        close_qty = min(f["qty"], abs(pos["net"]))
        pos["out_qty"] += close_qty; pos["out_cost"] += close_qty * f["price"]; pos["pnl"] += f["pnl"]
        pos["net"] += signed if f["qty"] <= abs(pos["net"]) + eps else (close_qty if signed > 0 else -close_qty)
        if abs(pos["net"]) <= eps * max(1.0, pos["max"]):
            out.append({"symbol": s, "side": pos["side"], "entry": pos["in_cost"] / pos["in_qty"],
                        "close": pos["out_cost"] / pos["out_qty"], "qty": round(pos["out_qty"], 8),
                        "pnl": round(pos["pnl"], 4), "roi": None, "leverage": None,
                        "opened_at": pos["open"], "closed_at": f["t"], "sort_ts": pos["open"], "day": pos["open"][:10]})
            del open_pos[s]
            leftover = f["qty"] - close_qty
            if leftover > eps:   # flipped straight into the other side
                open_pos[s] = {"side": "LONG" if f["buy"] else "SHORT", "net": leftover if f["buy"] else -leftover,
                               "open": f["t"], "in_qty": leftover, "in_cost": leftover * f["price"], "out_qty": 0.0,
                               "out_cost": 0.0, "pnl": 0.0, "max": leftover}
    return out, len(open_pos)


def parse_file(data: bytes, filename: str, tz):
    """Returns (kind, positions, note). kind is 'positions', 'trades' or None if not recognised."""
    header, raw_header, rows = read_rows(data, filename)
    kind = detect(header)
    utc = not any("local" in h for h in header) and not any("utc+" in h.lower() for h in raw_header if "utc+0" not in h.lower())
    if kind == "positions":
        return kind, parse_positions(header, rows, tz, utc), ""
    if kind == "trades":
        pos, still_open = parse_trades(header, rows, tz, utc)
        note = f"{still_open} position(s) still open at the end of the file were skipped." if still_open else ""
        return kind, pos, note
    return None, [], "Columns found: " + ", ".join(h for h in raw_header if h)[:300]
