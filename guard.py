"""Live tilt guard: watches your Binance futures account while you trade and warns you in Telegram
the moment you break one of your rules — not the next morning.

It needs a READ-ONLY Binance API key in .env (BINANCE_API_KEY / BINANCE_API_SECRET). Before using a key the
guard asks Binance what the key is allowed to do, and refuses any key that can trade or withdraw.

Checks, every 20 seconds:
  • revenge  — a new position within COOLDOWN_MIN of a losing close
  • flip     — the opposite side on the same coin right after a loss on it
  • lev_up   — higher leverage than the trade that just lost
  • size_up  — a position 2x+ bigger than your previous one, right after a loss
  • count    — more than MAX_TRADES_PER_DAY positions today
  • limit    — the day has given back DAILY_LOSS_LIMIT from its high
  • no_stop  — a position open 60s+ with no stop-loss order
  • hold     — a losing position still open after MAX_HOLD_MIN
"""
import hashlib
import hmac
import time
from urllib.parse import urlencode

import requests

FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"
STOP_TYPES = {"STOP_MARKET", "STOP", "TRAILING_STOP_MARKET"}


class Client:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret.encode()
        self.s = requests.Session()
        self.s.headers["X-MBX-APIKEY"] = key

    def signed(self, base, path, **params):
        params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 10000}
        q = urlencode(params)
        sig = hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()
        r = self.s.get(f"{base}{path}?{q}&signature={sig}", timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def restrictions(self):
        return self.signed(SAPI, "/sapi/v1/account/apiRestrictions")

    def positions(self):
        return [p for p in self.signed(FAPI, "/fapi/v2/positionRisk") or [] if float(p["positionAmt"]) != 0]

    def stop_symbols(self):
        """Coins that have a stop-loss order working (regular or the newer conditional 'algo' orders)."""
        out = set()
        for o in self.signed(FAPI, "/fapi/v1/openOrders") or []:
            if o.get("type") in STOP_TYPES or o.get("origType") in STOP_TYPES:
                out.add(o["symbol"])
        try:
            algo = self.signed(FAPI, "/fapi/v1/openAlgoOrders") or []
            algo = algo.get("orders", []) if isinstance(algo, dict) else algo
            for o in algo:
                if (o.get("orderType") or o.get("type")) in STOP_TYPES:
                    out.add(o["symbol"])
        except Exception:
            pass
        return out

    def income(self, start_ms):
        return self.signed(FAPI, "/fapi/v1/income", startTime=start_ms, limit=1000) or []


def check_key(restr):
    """A key is only accepted if it can read and can't trade or withdraw. Returns (ok, reason)."""
    if not isinstance(restr, dict):
        return False, "Binance didn't say what this key can do."
    danger = [k for k in ("enableWithdrawals", "enableFutures", "enableSpotAndMarginTrading", "enableMargin",
                          "enableInternalTransfer", "permitsUniversalTransfer", "enableVanillaOptions",
                          "enablePortfolioMarginTrading") if restr.get(k)]
    if danger:
        return False, ("this key can do more than read (" + ", ".join(danger) + "). Delete it in Binance → API "
                       "Management and make a new one with only “Enable Reading” ticked.")
    if not restr.get("enableReading"):
        return False, "this key can't read your account. Tick “Enable Reading”."
    return True, ""


class Guard:
    """Pure rule logic — fed snapshots, returns alert texts. Kept separate from Binance so it can be tested."""

    def __init__(self, cooldown_min=15, max_trades=3, loss_limit=5.0, max_hold_min=10, tz_offset_h=3, last_entry=None):
        self.cooldown = int(cooldown_min * 60_000)
        self.max_trades, self.loss_limit, self.max_hold = int(max_trades), float(loss_limit), int(max_hold_min * 60_000)
        self.tz = tz_offset_h * 3_600_000
        self.open = {}            # key -> position info
        self.closed = []          # recent closed positions with pnl
        self.sent = set()         # (alert, position key) already sent
        self.day = None
        self.opens_today = 0
        self.started = False
        self.last_entry = last_entry  # "HH:MM" — no new trades after this (None = off)
        self.closed_today = 0

    def _day(self, ms):
        return (ms + self.tz) // 86_400_000

    def day_pnl(self, income, now):
        start = self._day(now) * 86_400_000 - self.tz
        rows = sorted((x for x in income if int(x["time"]) >= start and x.get("incomeType") in
                       ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")), key=lambda x: int(x["time"]))
        run, peak = 0.0, 0.0
        for x in rows:
            run += float(x["income"]); peak = max(peak, run)
        return run, peak

    def update(self, positions, stops, income, now):
        alerts = []
        if self._day(now) != self.day:
            self.day, self.opens_today, self.closed_today = self._day(now), 0, 0
            self.sent = {s for s in self.sent if s[0] not in ("limit", "count", "done", "over", "late")}
        cur = {}
        for p in positions:
            amt = float(p["positionAmt"])
            side = "LONG" if amt > 0 else "SHORT"
            key = (p["symbol"], side)
            px = float(p.get("markPrice") or p.get("entryPrice") or 0)
            cur[key] = {"symbol": p["symbol"], "side": side, "lev": int(float(p.get("leverage", 0) or 0)),
                        "notional": abs(amt) * px, "upnl": float(p.get("unRealizedProfit") or 0), "seen": self.open.get(key, {}).get("seen", now)}
        # positions that closed since last look: book their realised P&L
        for key, info in self.open.items():
            if key not in cur:
                pnl = sum(float(x["income"]) for x in income if x.get("symbol") == info["symbol"]
                          and x.get("incomeType") in ("REALIZED_PNL", "COMMISSION") and int(x["time"]) >= info["seen"] - 5_000)
                self.closed.append({**info, "pnl": pnl, "closed": now})
                if self.started:
                    self.closed_today += 1
        self.closed = [c for c in self.closed if now - c["closed"] < 6 * 3_600_000]
        # new positions
        for key, info in cur.items():
            if key in self.open or not self.started:
                continue
            self.opens_today += 1
            sym = info["symbol"].replace("USDT", "")
            loss = next((c for c in reversed(self.closed) if c["pnl"] < 0 and now - c["closed"] < self.cooldown), None)
            if loss:
                mins = (now - loss["closed"]) / 60_000
                if loss["symbol"] == info["symbol"] and loss["side"] != info["side"]:
                    alerts.append(f"🔄 <b>Flip after a loss</b> — you just went {info['side'].lower()} {sym} "
                                  f"{'seconds' if mins < 1 else f'{mins:.0f} min'} after losing {loss['pnl']:.2f} on the {loss['side'].lower()}. "
                                  "Flips after a loss are your most expensive habit. Close it unless you'd take this "
                                  "trade on a calm day.")
                else:
                    alerts.append(f"⏱ <b>Revenge window</b> — {sym} {info['side'].lower()} opened {mins:.0f} min after a "
                                  f"{loss['pnl']:.2f} loss on {loss['symbol'].replace('USDT', '')}. Your rule: wait "
                                  f"{self.cooldown // 60_000} min after a loss.")
                if loss["lev"] and info["lev"] > loss["lev"]:
                    alerts.append(f"📈 <b>Leverage up after a loss</b> — {sym} at {info['lev']}× (the losing trade was "
                                  f"{loss['lev']}×).")
                if loss["notional"] and info["notional"] >= 2 * loss["notional"]:
                    alerts.append(f"💣 <b>Size up after a loss</b> — {sym} is {info['notional'] / loss['notional']:.1f}× "
                                  "bigger than the trade you just lost. That's trying to win it back.")
            if self.opens_today > self.max_trades and ("count", self.day) not in self.sent:
                self.sent.add(("count", self.day))
                alerts.append(f"🔢 <b>Trade {self.opens_today} today</b> — your plan is {self.max_trades}. Your first "
                              f"{self.max_trades} trades of the day made money; everything after them lost.")
            if self.last_entry and ("late", self.day) not in self.sent:
                hh, mm = map(int, self.last_entry.split(":"))
                local_min = ((now + self.tz) // 60_000) % 1440
                if local_min >= hh * 60 + mm:
                    self.sent.add(("late", self.day))
                    alerts.append(f"🕛 <b>New trade after {self.last_entry}</b> — your plan is mornings only. "
                                  "Your afternoons have lost money so far.")
        run, peak = self.day_pnl(income, now)
        if peak - run >= self.loss_limit and ("limit", self.day) not in self.sent:
            self.sent.add(("limit", self.day))
            alerts.append(f"🛑 <b>Daily stop hit</b> — you're {run:+.2f} today, {peak - run:.2f} below the day's high. "
                          "Your rule says done for today. Close the app.")
        # checks on every open position
        for key, info in cur.items():
            sym = info["symbol"].replace("USDT", "")
            age = now - info["seen"]
            if age >= 60_000 and info["symbol"] not in stops and ("no_stop", key, info["seen"]) not in self.sent:
                self.sent.add(("no_stop", key, info["seen"]))
                alerts.append(f"🚫 <b>No stop on {sym}</b> {info['side'].lower()} {info['lev']}×. Set one now: a stop is what keeps "
                              "one bad trade from turning into −100% ROI.")
            if age >= self.max_hold and info["upnl"] < 0 and ("hold", key, info["seen"]) not in self.sent:
                self.sent.add(("hold", key, info["seen"]))
                alerts.append(f"⌛ <b>{sym} losing after {age // 60_000} min</b> ({info['upnl']:+.2f}) — your rule: close "
                              f"any losing trade still open after {self.max_hold // 60_000} min.")
        if (self.closed_today >= self.max_trades and not cur and ("done", self.day) not in self.sent
                and self.opens_today >= 1):
            self.sent.add(("done", self.day))
            alerts.append(f"✅ <b>That's {self.closed_today} trades — you're done for today.</b> Close the app. "
                          "Tomorrow's setups will still be there.")
        if alerts and any(not a.startswith("✅") for a in alerts) and ("over", self.day) not in self.sent:
            self.sent.add(("over", self.day))
            alerts.append("⛔ <b>Rule broken — the day is over.</b> Your trades make money until the first broken rule "
                          "of the day and lose after it. Close what's open, put a stop on anything you keep, and close "
                          "the app.")
        self.open, self.started = cur, True
        return alerts


class Live:
    """Connects Guard to Binance. Call poll() every ~20 seconds; it returns alerts to send."""

    def __init__(self, key, secret, **rules):
        self.client = Client(key, secret)
        self.guard = Guard(**rules)
        self.ok, self.reason, self.checked = None, "", 0

    def verify(self):
        try:
            self.ok, self.reason = check_key(self.client.restrictions())
        except requests.HTTPError as e:
            self.ok, self.reason = False, f"Binance refused the key ({e.response.status_code}). Check it was pasted fully."
        self.checked = time.time()
        return self.ok, self.reason

    def poll(self):
        if self.ok is None or time.time() - self.checked > 6 * 3600:   # re-check the key's permissions now and then
            self.verify()
        if not self.ok:
            return []
        now = int(time.time() * 1000)
        positions = self.client.positions()
        stops = self.client.stop_symbols() if positions else set()
        income = self.client.income(now - 26 * 3_600_000)
        return self.guard.update(positions, stops, income, now)
