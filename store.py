"""SQLite storage for positions and daily P&L."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("trades.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  day         TEXT NOT NULL,            -- YYYY-MM-DD (local time shown in the Binance app)
  day_guessed INTEGER DEFAULT 0,        -- 1 if the date had to be inferred
  symbol      TEXT NOT NULL,
  side        TEXT NOT NULL,            -- LONG / SHORT
  leverage    INTEGER,
  pnl         REAL NOT NULL,
  roi         REAL,
  entry       REAL NOT NULL,
  close       REAL NOT NULL,
  qty         REAL NOT NULL,
  opened_at   TEXT,                     -- YYYY-MM-DD HH:MM:SS or NULL
  closed_at   TEXT,
  sort_ts     TEXT,                     -- best-known ordering time
  batch       TEXT,                     -- import batch id (for /undo)
  UNIQUE(symbol, side, entry, close, qty)
);
CREATE TABLE IF NOT EXISTS daily (
  day   TEXT PRIMARY KEY,
  pnl   REAL NOT NULL,
  batch TEXT
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path=DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def get_setting(con, k, default=None):
    r = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_setting(con, k, v):
    con.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    con.commit()


def _secs(ts):
    from datetime import datetime
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return None


def _fuzzy_match(con, p):
    """The same position logged from a different source (screenshot vs export) has slightly different averages.
    Match it by coin + side + day, and either an open/close time within 90 s, or the same size with prices
    within 0.3%."""
    if not p.get("day"):
        return None
    rows = con.execute("SELECT * FROM positions WHERE symbol=? AND side=? AND day=?",
                       (p["symbol"], p["side"], p["day"])).fetchall()
    for r in rows:
        for f in ("opened_at", "closed_at"):
            a, b = _secs(p.get(f)), _secs(r[f])
            if a is not None and b is not None and abs(a - b) <= 90:
                return r
        if r["qty"] and abs(r["qty"] - p["qty"]) <= 0.001 * r["qty"] \
                and abs(r["entry"] - p["entry"]) <= 0.003 * r["entry"] \
                and abs(r["close"] - p["close"]) <= 0.003 * max(r["close"], 1e-12):
            return r
    return None


def upsert_position(con, p, batch):
    """Insert a position, or fill in times on a copy we already have. Returns 'new', 'updated' or 'dup'."""
    key = (p["symbol"], p["side"], p["entry"], p["close"], p["qty"])
    old = con.execute("SELECT * FROM positions WHERE symbol=? AND side=? AND entry=? AND close=? AND qty=?", key).fetchone()
    if old is None:
        old = _fuzzy_match(con, p)
    if old is None:
        con.execute(
            """INSERT INTO positions(day,day_guessed,symbol,side,leverage,pnl,roi,entry,close,qty,opened_at,closed_at,sort_ts,batch)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["day"], int(p.get("day_guessed", 0)), p["symbol"], p["side"], p.get("leverage"), p["pnl"], p.get("roi"),
             p["entry"], p["close"], p["qty"], p.get("opened_at"), p.get("closed_at"), p.get("sort_ts"), batch))
        return "new"
    # a later screenshot may show times that were cut off before
    changes = {}
    for f in ("opened_at", "closed_at"):
        if old[f] is None and p.get(f):
            changes[f] = p[f]
    if old["day_guessed"] and not p.get("day_guessed"):
        changes["day"] = p["day"]; changes["day_guessed"] = 0
    if changes:
        changes["sort_ts"] = changes.get("opened_at") or changes.get("closed_at") or old["sort_ts"]
        sets = ", ".join(f"{k}=?" for k in changes)
        con.execute(f"UPDATE positions SET {sets} WHERE id=?", (*changes.values(), old["id"]))
        return "updated"
    return "dup"


def upsert_daily(con, day, pnl, batch):
    con.execute("INSERT INTO daily(day,pnl,batch) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET pnl=excluded.pnl, batch=excluded.batch",
                (day, pnl, batch))


def positions_for(con, day_from, day_to=None):
    day_to = day_to or day_from
    rows = con.execute("SELECT * FROM positions WHERE day BETWEEN ? AND ? ORDER BY day, sort_ts, id", (day_from, day_to)).fetchall()
    return [dict(r) for r in rows]


def daily_for(con, day_from, day_to):
    return {r["day"]: r["pnl"] for r in con.execute("SELECT * FROM daily WHERE day BETWEEN ? AND ?", (day_from, day_to))}


def undo_batch(con, batch):
    n = con.execute("DELETE FROM positions WHERE batch=?", (batch,)).rowcount
    n += con.execute("DELETE FROM daily WHERE batch=?", (batch,)).rowcount
    con.commit()
    return n
