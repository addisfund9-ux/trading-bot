"""Bot picks: a paper-trading journal the bot keeps by itself.

Every 15 minutes:
  1. Scanner: coins with all 3 timeframes aligned, score 60+ and no warning flags ("clean").
  2. Pump watch: of those, the ones whose 1m/5m/15m EMA 9/21 trigger is lined up the same way, with
     30m/1h/4h direction agreeing (state "trigger" = fresh, "aligned" = held for a while).
  3. Check a trade (the Time Machine from the Mini App, ported): 10,000 same-hour replays + this coin's
     history of the same setup → odds and a PASS / WARN / BLOCK verdict.
  4. Every coin in both lists is booked as a PAPER trade: entry now, stop 1.5× the 15m ATR, target 1.5R,
     out after 2 hours. No money involved. The bot checks it every 5 minutes and records the result.

The journal answers one question honestly, going forward: do the bot's picks make money, and does the
Time Machine verdict pick the better ones? Fees (0.1% round trip) are included, like in the backtests.
"""
import io
import math
import random
import time
from datetime import datetime

import scanner as sc

TZ_H = 3
STOP_ATR, TARGET_R, HOLD_MIN, FEE = 1.5, 1.5, 120, 0.001
PATHS = 10000
CLEAN_BLOCK = {"Stretched", "Crowded", "OI falling", "Squeeze", "Late"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol    TEXT NOT NULL,
  dir       INTEGER NOT NULL,          -- 1 long, -1 short
  opened_ms INTEGER NOT NULL,
  entry     REAL NOT NULL,
  stop      REAL NOT NULL,
  target    REAL NOT NULL,
  risk      REAL NOT NULL,
  score     INTEGER,
  pump      TEXT,                      -- trigger / aligned
  verdict   TEXT,                      -- PASS / WARN / BLOCK (market checks only)
  p_target  REAL, p_stop REAL, exp_r REAL,
  hist_n    INTEGER, hist_r REAL,
  busy      INTEGER,                   -- opened in a busy market hour
  why       TEXT,
  status    TEXT DEFAULT 'open',       -- open / target / stop / expired
  closed_ms INTEGER,
  close     REAL,
  r         REAL,
  path      TEXT                       -- 5m closes after entry, for the result charts
);
"""


def ensure(con):
    con.executescript(SCHEMA)


def local_hour(ms):
    return (ms // 3_600_000 + TZ_H) % 24


# ---------------- pump watch (same rules as the Mini App) ----------------
PUMP_TFS = [("1m", 9, 21, 150), ("5m", 9, 21, 120), ("15m", 9, 21, 120),
            ("30m", 9, 21, 120), ("1h", 20, 50, 150), ("4h", 50, 200, 400)]


def _k(sym, tf, limit):
    k = sc.get("/fapi/v1/klines", symbol=sym, interval=tf, limit=limit)
    return {"t": [int(x[0]) for x in k], "o": [float(x[1]) for x in k], "h": [float(x[2]) for x in k],
            "l": [float(x[3]) for x in k], "c": [float(x[4]) for x in k], "q": [float(x[7]) for x in k],
            "tb": [float(x[10]) for x in k]}


def _pema(v, n):
    k, out, e = 2 / (n + 1), [], v[0]
    for x in v:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def _forming(k, fast, slow, j, price):
    kf_, ks_ = "_ef%d" % fast, "_es%d" % slow
    if kf_ not in k:
        k[kf_] = _pema(k["c"], fast)
    if ks_ not in k:
        k[ks_] = _pema(k["c"], slow)
    ef, es = k[kf_], k[ks_]
    if j < 1 or j < slow:
        return 0
    kf, ks = 2 / (fast + 1), 2 / (slow + 1)
    f = ef[j - 1] * (1 - kf) + price * kf
    s = es[j - 1] * (1 - ks) + price * ks
    return 1 if price > f > s else -1 if price < f < s else 0


def _idx(ts, t):
    lo, hi = 0, len(ts) - 1
    while lo < hi:
        m = (lo + hi + 1) >> 1
        if ts[m] <= t:
            lo = m
        else:
            hi = m - 1
    return lo


def pump_state(sym):
    K = {tf: _k(sym, tf, lim) for tf, _, _, lim in PUMP_TFS}
    m = K["1m"]
    n = len(m["c"])

    def at(i):
        t, p = m["t"][i], m["c"][i]
        return [_forming(K[tf], f, s, _idx(K[tf]["t"], t), p) for tf, f, s, _ in PUMP_TFS]
    now = at(n - 1)
    d = now[5] if now[3] == now[4] == now[5] else 0
    ok = lambda tr: d != 0 and all(x == d for x in tr)
    held = 0
    for i in range(n - 1, max(0, n - 60) - 1, -1):
        if not ok(at(i)):
            break
        held += 1
    state = "none" if not d else ("trigger" if held <= 3 else "aligned") if held >= 1 else "waiting"
    return {"dir": d, "state": state, "held": held}


# ---------------- Time Machine (port of tmAnalyse / riskCheck) ----------------
def tm_fetch(sym):
    end, rows = int(time.time() * 1000), []
    for _ in range(3):
        k = sc.get("/fapi/v1/klines", symbol=sym, interval="5m", limit=1500, endTime=end)
        if not k:
            break
        rows = k + rows
        end = int(k[0][0]) - 1
        if len(k) < 1500:
            break
    return {"t": [int(x[0]) for x in rows], "h": [float(x[2]) for x in rows], "l": [float(x[3]) for x in rows],
            "c": [float(x[4]) for x in rows], "q": [float(x[7]) for x in rows]}


def _sim(k, i, d, risk, tR, bars):
    e = k["c"][i]
    stop, tgt, fee_r = e - d * risk, e + d * risk * tR, FEE * e / risk
    last = min(i + bars, len(k["c"]) - 1)
    for j in range(i + 1, last + 1):
        if (k["l"][j] <= stop) if d > 0 else (k["h"][j] >= stop):
            return -1 - fee_r
        if (k["h"][j] >= tgt) if d > 0 else (k["l"][j] <= tgt):
            return tR - fee_r
    return d * (k["c"][last] - e) / risk - fee_r


def tm_analyse(k, d, busy_vol=None, paths=PATHS):
    n, bars = len(k["c"]), round(HOLD_MIN / 5)
    a5 = sc.atr(k["h"], k["l"], k["c"])
    atr15 = a5 * math.sqrt(3)
    risk = STOP_ATR * atr15
    e = k["c"][-1]
    by_h = [[] for _ in range(24)]
    for i in range(1, n):
        p = k["c"][i - 1]
        by_h[local_hour(k["t"][i])].append([math.log(k["c"][i] / p), math.log(k["h"][i] / p), math.log(k["l"][i] / p)])
    for b in by_h:
        if b:
            mu = sum(x[0] for x in b) / len(b)
            for x in b:
                x[0] -= mu; x[1] -= mu; x[2] -= mu
    stop_l, tgt_l = math.log((e - d * risk) / e), math.log((e + d * risk * TARGET_R) / e)
    rnd = random.Random(12345)
    win = loss = 0
    sum_r = 0.0
    t0 = k["t"][-1] + 300_000
    hrs = [by_h[local_hour(t0 + s * 300_000)] for s in range(bars)]
    for _ in range(paths):
        x, done = 0.0, False
        for b in hrs:
            if not b:
                continue
            r = b[int(rnd.random() * len(b))]
            hi, lo = x + r[1], x + r[2]
            if (lo <= stop_l) if d > 0 else (hi >= stop_l):
                loss += 1; sum_r -= 1; done = True; break
            if (hi >= tgt_l) if d > 0 else (lo <= tgt_l):
                win += 1; sum_r += TARGET_R; done = True; break
            x += r[0]
        if not done:
            sum_r += d * (math.exp(x) - 1) * e / risk
    mc = {"p_target": win / paths, "p_stop": loss / paths, "exp_r": sum_r / paths - FEE * e / risk}
    # history of the EMA trigger on this coin (5m / 15m / 1h aligned, fresh)
    c15 = list(range(2, n, 3)); c60 = list(range(11, n, 12))
    cl15 = [k["c"][i] for i in c15]; cl60 = [k["c"][i] for i in c60]
    e5f, e5s = sc.ema(k["c"], 9), sc.ema(k["c"], 21)
    e15f, e15s, e60f, e60s = sc.ema(cl15, 9), sc.ema(cl15, 21), sc.ema(cl60, 20), sc.ema(cl60, 50)
    i15, i60, q15, q60 = [-1] * n, [-1] * n, 0, 0
    for j in range(n):
        while q15 + 1 < len(c15) and c15[q15 + 1] <= j:
            q15 += 1
        i15[j] = q15 if c15 and c15[q15] <= j else -1
        while q60 + 1 < len(c60) and c60[q60 + 1] <= j:
            q60 += 1
        i60[j] = q60 if c60 and c60[q60] <= j else -1

    def tr(p, f, s):
        return 0 if f is None or s is None else 1 if p > f > s else -1 if p < f < s else 0

    def aligned(j):
        a, b = i15[j], i60[j]
        if a < 0 or b < 0 or not e15f or not e60f:
            return False
        return tr(k["c"][j], e5f[j], e5s[j]) == d and tr(cl15[a], e15f[a], e15s[a]) == d and tr(cl60[b], e60f[b], e60s[b]) == d
    res_all, res_busy, res_calm, until = [], [], [], -1
    prev = aligned(59) if n > 60 else False
    for j in range(60, n - bars):
        cur = aligned(j)
        fire, prev = cur and not prev, cur
        if j <= until or not fire:
            continue
        r15 = sc.atr(k["h"][j - 60:j + 1], k["l"][j - 60:j + 1], k["c"][j - 60:j + 1]) * math.sqrt(3)
        if not r15:
            continue
        R = _sim(k, j, d, STOP_ATR * r15, TARGET_R, bars)
        res_all.append(R)
        (res_busy if busy_vol and busy_vol.get(local_hour(k["t"][j]), 0) >= 1.4 else res_calm).append(R)
        until = j + bars
    st = lambda v: {"n": len(v), "win": sum(x > 0 for x in v) / len(v), "avg_r": sum(v) / len(v)} if v else {"n": 0}
    # stretch from the 1h EMA20, in 1h ATRs
    h60 = [max(k["h"][(c60[q - 1] + 1 if q else 0):i + 1]) for q, i in enumerate(c60)]
    l60 = [min(k["l"][(c60[q - 1] + 1 if q else 0):i + 1]) for q, i in enumerate(c60)]
    atr60, e20 = sc.atr(h60, l60, cl60), sc.ema(cl60, 20)
    stretch = (e - e20[-1]) / atr60 if atr60 and e20 and e20[-1] else None
    next_hours = sorted({local_hour(t0 + s * 300_000) for s in range(bars)})
    return {"entry": e, "atr15": atr15, "risk": risk, "stop": e - d * risk, "target": e + d * risk * TARGET_R,
            "mc": mc, "hist": {"all": st(res_all), "busy": st(res_busy), "calm": st(res_calm)},
            "stretch": stretch, "next_busy": bool(busy_vol) and any(busy_vol.get(h, 0) >= 1.4 for h in next_hours)}


def verdict(tm, d, lev=10):
    """The market half of the Mini App's risk check (time-of-day rules are about you, not the coin)."""
    items = []
    stop_pct = tm["risk"] / tm["entry"] * 100
    if 100 / lev * 0.9 <= stop_pct:
        items.append(("block", f"liquidation at {lev}× comes before the stop"))
    h, mc = tm["hist"]["all"], tm["mc"]
    if h["n"] >= 10 and h["avg_r"] < -0.2:
        items.append(("block", f"setup lost on this coin ({h['n']}×, {h['avg_r']:+.2f}R)"))
    elif h["n"] >= 10 and h["avg_r"] < 0:
        items.append(("warn", f"setup slightly lost on this coin ({h['n']}×, {h['avg_r']:+.2f}R)"))
    elif h["n"] < 10:
        items.append(("warn", f"only {h['n']} past examples"))
    if tm["stretch"] is not None and d * tm["stretch"] > 3:
        items.append(("warn", f"{tm['stretch']:.1f} ATR from the 1h EMA20"))
    if mc["exp_r"] < -0.1:
        items.append(("warn", f"replay leans against it ({mc['exp_r']:+.2f}R)"))
    if tm["next_busy"]:
        items.append(("warn", "runs into the busy hours"))
    v = "BLOCK" if any(x[0] == "block" for x in items) else "WARN" if items else "PASS"
    return v, "; ".join(x[1] for x in items)


# ---------------- one round ----------------
def candidates(scan_result):
    return [c for c in scan_result["coins"] if c["full"] and c["score"] >= 60 and not CLEAN_BLOCK & set(c["flags"])]


def run_round(con, busy_vol=None, now_ms=None, scan_result=None, log=print):
    """Scan, cross-check with the pump watch, check the odds, book paper picks. Returns the new picks."""
    ensure(con)
    now_ms = now_ms or int(time.time() * 1000)
    res = scan_result or sc.scan(30, 20, True)
    clean = candidates(res)
    open_or_recent = {r["symbol"] for r in con.execute(
        "SELECT symbol FROM picks WHERE status='open' OR opened_ms > ?", (now_ms - HOLD_MIN * 60_000,))}
    out = []
    for c in clean:
        sym = c["sym"]
        if sym in open_or_recent:
            continue
        try:
            p = pump_state(sym)
        except Exception as e:
            log(f"pump {sym}: {e}")
            continue
        if p["dir"] != c["dir"] or p["state"] not in ("trigger", "aligned"):
            continue
        try:
            tm = tm_analyse(tm_fetch(sym), c["dir"], busy_vol)
        except Exception as e:
            log(f"time machine {sym}: {e}")
            continue
        if not tm["risk"]:
            continue
        v, why = verdict(tm, c["dir"])
        row = (sym, c["dir"], now_ms, tm["entry"], tm["stop"], tm["target"], tm["risk"], c["score"], p["state"], v,
               tm["mc"]["p_target"], tm["mc"]["p_stop"], tm["mc"]["exp_r"], tm["hist"]["all"]["n"],
               tm["hist"]["all"].get("avg_r"), int(bool(busy_vol) and busy_vol.get(local_hour(now_ms), 0) >= 1.4), why)
        cur = con.execute("INSERT INTO picks(symbol,dir,opened_ms,entry,stop,target,risk,score,pump,verdict,p_target,"
                          "p_stop,exp_r,hist_n,hist_r,busy,why) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        con.commit()
        out.append(dict(con.execute("SELECT * FROM picks WHERE id=?", (cur.lastrowid,)).fetchone()))
    return out


def resolve(con, now_ms=None):
    """Play every open pick forward on 1-minute candles: stop first if both are hit in one minute."""
    ensure(con)
    now_ms = now_ms or int(time.time() * 1000)
    closed = []
    for p in [dict(r) for r in con.execute("SELECT * FROM picks WHERE status='open'")]:
        end = p["opened_ms"] + HOLD_MIN * 60_000
        k = sc.get("/fapi/v1/klines", symbol=p["symbol"], interval="1m", startTime=p["opened_ms"], limit=HOLD_MIN + 1)
        k = [x for x in k if int(x[0]) >= p["opened_ms"] - 60_000 and int(x[6]) <= min(now_ms, end + 60_000)]
        d, status, px, at = p["dir"], None, None, None
        for x in k:
            hi, lo = float(x[2]), float(x[3])
            if (lo <= p["stop"]) if d > 0 else (hi >= p["stop"]):
                status, px, at = "stop", p["stop"], int(x[6]); break
            if (hi >= p["target"]) if d > 0 else (lo <= p["target"]):
                status, px, at = "target", p["target"], int(x[6]); break
        if not status and now_ms >= end + 60_000 and k:
            status, px, at = "expired", float(k[-1][4]), int(k[-1][6])
        if not status:
            continue
        r = d * (px - p["entry"]) / p["risk"] - FEE * p["entry"] / p["risk"]
        closes = [float(x[4]) for x in k if int(x[6]) <= at][::5]
        con.execute("UPDATE picks SET status=?, closed_ms=?, close=?, r=?, path=? WHERE id=?",
                    (status, at, px, round(r, 3), ",".join(f"{c:.8g}" for c in closes), p["id"]))
        con.commit()
        closed.append({**p, "status": status, "close": px, "r": r})
    return closed


# ---------------- results ----------------
def stats(rows):
    done = [r for r in rows if r["status"] != "open"]
    if not done:
        return {"n": 0}
    return {"n": len(done), "win": sum(r["r"] > 0 for r in done) / len(done), "avg_r": sum(r["r"] for r in done) / len(done),
            "total_r": sum(r["r"] for r in done)}


def _fmt_px(p):
    return f"{p:.2f}" if p >= 100 else f"{p:.4f}" if p >= 1 else f"{p:.4g}"


def text(con, n_list=8):
    ensure(con)
    rows = [dict(r) for r in con.execute("SELECT * FROM picks ORDER BY opened_ms")]
    L = ["<b>📓 Bot picks (paper journal, no money)</b>",
         "Coins that were clean in the scanner <i>and</i> lined up in the pump watch, checked by the Time Machine "
         f"and booked automatically: stop {STOP_ATR:g}× 15m ATR, target {TARGET_R:g}R, out after {HOLD_MIN // 60}h, fees in.", ""]
    if not rows:
        L.append("No picks yet. The bot checks every 15 minutes; most rounds nothing qualifies, and that's fine.")
        return "\n".join(L)
    s = stats(rows)
    if s["n"]:
        L.append(f"<b>All closed picks:</b> {s['n']} · won {s['win'] * 100:.0f}% · avg <b>{s['avg_r']:+.2f}R</b> · "
                 f"total {s['total_r']:+.1f}R")
        for v in ("PASS", "WARN", "BLOCK"):
            sv = stats([r for r in rows if r["verdict"] == v])
            if sv["n"]:
                L.append(f"  {v}: {sv['n']} · won {sv['win'] * 100:.0f}% · avg {sv['avg_r']:+.2f}R")
        for name, b in (("calm hours", 0), ("busy hours", 1)):
            sv = stats([r for r in rows if r["busy"] == b])
            if sv["n"]:
                L.append(f"  {name}: {sv['n']} · avg {sv['avg_r']:+.2f}R")
        if s["n"] < 30:
            L.append(f"<i>{s['n']} closed so far. Under ~30 the average can swing a lot: give it time before trusting it.</i>")
    op = [r for r in rows if r["status"] == "open"]
    if op:
        L.append(f"\n<b>Open now ({len(op)}):</b> " + ", ".join(f"{r['symbol'][:-4]} {'long' if r['dir'] > 0 else 'short'}" for r in op))
    L.append("\n<b>Latest</b>")
    icon = {"target": "🎯", "stop": "🛑", "expired": "⌛", "open": "⏳"}
    for r in rows[::-1][:n_list]:
        res = "open" if r["status"] == "open" else f"{r['r']:+.2f}R"
        L.append(f"{icon[r['status']]} {r['symbol'][:-4]} {'long' if r['dir'] > 0 else 'short'} · "
                 f"{datetime.utcfromtimestamp(r['opened_ms'] / 1000 + TZ_H * 3600):%a %H:%M} · {r['verdict']} · "
                 f"odds {r['p_target'] * 100:.0f}/{r['p_stop'] * 100:.0f} · {res}")
    return "\n".join(L)


def summary_line(con):
    ensure(con)
    s = stats([dict(r) for r in con.execute("SELECT * FROM picks")])
    if not s["n"]:
        return None
    return (f"📓 <b>Bot picks (paper):</b> {s['n']} closed · won {s['win'] * 100:.0f}% · avg {s['avg_r']:+.2f}R · "
            f"total {s['total_r']:+.1f}R · /picks")


def chart(con, n_coins=6):
    """Top: cumulative R of closed picks (all, and PASS only). Bottom: the last few picks' price paths."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ensure(con)
    rows = [dict(r) for r in con.execute("SELECT * FROM picks WHERE status!='open' ORDER BY closed_ms")]
    if not rows:
        return None
    BG, INK, MUTED, LINE, UP, DN, AMBER = "#0e1524", "#e8edf6", "#8a94a8", "#26324a", "#2ee6a6", "#ff5c7a", "#f0b90b"
    recent = [r for r in rows if r["path"]][-n_coins:]
    cols = 3
    nrow = 1 + (math.ceil(len(recent) / cols) if recent else 0)
    fig = plt.figure(figsize=(9, 3.2 + 2.3 * (nrow - 1)), dpi=130)
    fig.patch.set_facecolor(BG)
    gs = fig.add_gridspec(nrow, cols, height_ratios=[1.5] + [1] * (nrow - 1), hspace=.55, wspace=.25)
    ax = fig.add_subplot(gs[0, :])
    cum, cp, x = [0], [0], list(range(len(rows) + 1))
    for r in rows:
        cum.append(cum[-1] + r["r"])
        cp.append(cp[-1] + (r["r"] if r["verdict"] == "PASS" else 0))
    ax.bar(x[1:], [r["r"] for r in rows], color=[UP if r["r"] > 0 else DN for r in rows], width=.7, alpha=.45)
    ax.plot(x, cum, color=INK, lw=2, label="all picks")
    if any(r["verdict"] == "PASS" for r in rows):
        ax.plot(x, cp, color=AMBER, lw=1.6, ls="--", label="PASS only")
    ax.axhline(0, color=LINE, lw=1)
    ax.set_title(f"Bot picks, paper: {len(rows)} closed · total {cum[-1]:+.1f}R", color=INK, fontsize=11, loc="left")
    ax.set_ylabel("R (1R = the stop)", color=MUTED, fontsize=8)
    ax.legend(facecolor=BG, edgecolor=LINE, labelcolor=INK, fontsize=8, loc="upper left")
    axes = [ax]
    for i, r in enumerate(recent):
        a = fig.add_subplot(gs[1 + i // cols, i % cols])
        path = [r["entry"]] + [float(v) for v in r["path"].split(",") if v] + [r["close"]]
        col = UP if r["r"] > 0 else DN
        a.plot(range(len(path)), path, color=col, lw=1.6)
        a.axhline(r["entry"], color=MUTED, lw=.8, ls=":")
        a.axhline(r["stop"], color=DN, lw=.8, ls="--")
        a.axhline(r["target"], color=UP, lw=.8, ls="--")
        a.set_title(f"{r['symbol'][:-4]} {'long' if r['dir'] > 0 else 'short'} · {r['verdict']} · {r['r']:+.2f}R",
                    color=INK, fontsize=8.5, loc="left")
        a.set_xticks([])
        axes.append(a)
    for a in axes:
        a.set_facecolor(BG); a.tick_params(colors=MUTED, labelsize=7)
        for s_ in a.spines.values():
            s_.set_color(LINE)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def fresh_text(p):
    return (f"📓 <b>Paper pick</b> (not a signal): {p['symbol'][:-4]} {'long' if p['dir'] > 0 else 'short'} at "
            f"{_fmt_px(p['entry'])} · stop {_fmt_px(p['stop'])} · target {_fmt_px(p['target'])} · {p['verdict']}"
            + (f" ({p['why']})" if p["why"] else "") + f" · odds {p['p_target'] * 100:.0f}% target / {p['p_stop'] * 100:.0f}% stop")
