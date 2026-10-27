"""Backtest: would the scanner's Clean signals have made money?

Every hour of the last N days (Binance keeps open-interest and taker history for ~30 days), replay the
scanner on that hour's top movers exactly as it would have looked then, take every Clean signal, and
trade it with the scanner's own plan:
  entry  = price at the signal
  stop   = 1.5 x 1h ATR away
  target = 1.5R
  exit   = target, stop, or 24 hours, whichever comes first (stop wins if both hit in the same 15m candle)
  fees   = 0.1% of the position per round trip, taken off every trade
One trade per coin at a time. It runs on your PC because it needs Binance's public data.
"""
import bisect
import json
import os
import time
from datetime import datetime, timezone

import requests

import scanner as sc

API = sc.API
HOUR, Q15 = 3_600_000, 900_000
STOP_ATR, T1_R, MAX_HOLD_H, FEE = 1.5, 1.5, 24, 0.001
N_GAIN, N_LOSE, MIN_VOL = 30, 15, 20e6
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest")
session = requests.Session()


# ---------- fetching (paced so Binance never rate-limits us) ----------
def get(path, **params):
    for attempt in range(6):
        r = session.get(API + path, params=params, timeout=20)
        if r.status_code in (418, 429):
            time.sleep(int(r.headers.get("Retry-After", 30)) + 1)
            continue
        if r.status_code >= 500:
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(0.12 if path.startswith("/fapi") else 0.32)
        return r.json()
    raise RuntimeError(f"Binance kept refusing {path}")


def klines_range(sym, tf, start, end):
    ms = {"15m": Q15, "1h": HOUR, "4h": 4 * HOUR}[tf]
    out, t = [], start
    while t < end:
        k = get("/fapi/v1/klines", symbol=sym, interval=tf, startTime=t, endTime=end, limit=1500)
        if not k:
            break
        out += k
        nxt = k[-1][0] + ms
        if nxt <= t or len(k) < 1500:
            break
        t = nxt
    seen, rows = set(), []
    for x in out:
        if x[0] not in seen:
            seen.add(x[0]); rows.append(x)
    return {"t": [x[0] for x in rows], "o": [float(x[1]) for x in rows], "h": [float(x[2]) for x in rows],
            "l": [float(x[3]) for x in rows], "c": [float(x[4]) for x in rows], "q": [float(x[7]) for x in rows]}


def data_range(path, sym, start, end, extra=None):
    out, t = [], start
    while t < end:
        k = sc.safe(get, path, symbol=sym, period="1h", startTime=t, endTime=end, limit=500, **(extra or {}))
        if not k:
            break
        out += k
        nxt = int(k[-1]["timestamp"]) + HOUR
        if nxt <= t or len(k) < 500:
            break
        t = nxt
    return out


def funding_range(sym, start, end):
    out, t = [], start
    while t < end:
        k = sc.safe(get, "/fapi/v1/fundingRate", symbol=sym, startTime=t, endTime=end, limit=1000)
        if not k:
            break
        out += k
        if len(k) < 1000:
            break
        t = int(k[-1]["fundingTime"]) + 1
    return out


# ---------- indicator series over the whole history ----------
def ema_series(v, n):
    return sc.ema(v, n) or [None] * len(v)


def atr_series(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def prep(k, tf):
    f, s = {t: (fa, sl) for t, fa, sl in sc.TFS}[tf]
    k["ef"], k["es"] = ema_series(k["c"], f), ema_series(k["c"], s)
    k["close_t"] = [t + {"15m": Q15, "1h": HOUR, "4h": 4 * HOUR}[tf] for t in k["t"]]
    return k


def trend_at(k, j):
    if j < 0 or k["ef"][j] is None or k["es"][j] is None:
        return 0
    p, f, s = k["c"][j], k["ef"][j], k["es"][j]
    return 1 if p > f > s else -1 if p < f < s else 0


def last_closed(k, t):
    """Index of the last candle that had closed by time t."""
    return bisect.bisect_right(k["close_t"], t) - 1


# ---------- the replay ----------
def score_v1(c, btc):
    """The scanner's rules before 26 Oct (no Squeeze/Late flags, softer vs-BTC), to compare old vs new."""
    tsum = sum(c["trends"])
    d = 1 if tsum > 0 else -1 if tsum < 0 else (1 if c["chg"] >= 0 else -1)
    full = abs(tsum) == 3
    agree = sum(1 for t in c["trends"] if t == d)
    bsum = sum(btc["trends"]); bdir = 1 if bsum == 3 else -1 if bsum == -3 else 0
    flags = []
    if bdir == d: btc_pts = 8
    elif abs(c["corr"]) < 0.3: btc_pts = 5.6
    elif bdir == 0: btc_pts = 4
    else: btc_pts = 0
    oi_pts = 6
    if c["oi_ch4"] is not None:
        with_move = d * c["ch4"] > 0
        if c["oi_ch4"] <= 0:
            oi_pts = 0
            if with_move: flags.append("OI falling")
        else:
            oi_pts = 4 + 8 * sc.clamp01(c["oi_ch4"] / 8) if with_move else 3
    tk_pts = 5 if c["taker"] is None else 10 * sc.clamp01((c["taker"] - 0.95) / 0.25 if d > 0 else (1.05 - c["taker"]) / 0.25)
    f_pct = c["funding"] * 100 if c["funding"] is not None else None
    crowd_f = f_pct is not None and (f_pct >= 0.05 if d > 0 else f_pct <= -0.05)
    crowd_l = c["lsr"] is not None and (c["lsr"] >= 2.5 if d > 0 else c["lsr"] <= 0.6)
    if crowd_f or crowd_l: flags.append("Crowded")
    stretched = d * c["ext"] > 3
    if stretched: flags.append("Stretched")
    pts = (35 * agree / 3 + oi_pts + c["vol_pct"] * 12 + sc.clamp01((c["spike"] - 1) / 2) * 10 + tk_pts
           + sc.clamp01(d * c["rs4"] / 5) * 8 + btc_pts + 5 - (3 if crowd_f else 0) - (2 if crowd_l else 0)
           + (-10 if stretched else 0))
    score = max(0, min(100, round(pts)))
    return full and score >= 60 and not flags


def is_clean(c):
    bad = {"Stretched", "Crowded", "OI falling", "Squeeze", "Late"}
    return c["full"] and c["score"] >= 60 and not bad & set(c["flags"])


def simulate(k15, t, d, entry, risk):
    """Walk 15m candles after the signal. Returns (R before fees, exit time, outcome)."""
    i = bisect.bisect_left(k15["t"], t)
    stop, tgt = entry - d * risk, entry + d * risk * T1_R
    end = t + MAX_HOLD_H * HOUR
    last = None
    while i < len(k15["t"]) and k15["t"][i] < end:
        hi, lo = k15["h"][i], k15["l"][i]
        hit_stop = lo <= stop if d > 0 else hi >= stop
        hit_tgt = hi >= tgt if d > 0 else lo <= tgt
        if hit_stop:
            return -1.0, k15["t"][i] + Q15, "stop"
        if hit_tgt:
            return T1_R, k15["t"][i] + Q15, "target"
        last = i
        i += 1
    if last is None:
        return None, None, "no data"
    if k15["t"][last] + Q15 < end:
        return None, None, "open"          # 24h not over yet
    return d * (k15["c"][last] - entry) / risk, end, "time"


def run(days=28, progress=lambda msg: None):
    days = max(3, min(29, int(days)))
    now = int(time.time() * 1000) // HOUR * HOUR
    start = now - days * 24 * HOUR
    warm1h = start - 60 * HOUR
    info = get("/fapi/v1/exchangeInfo")
    syms = [s["symbol"] for s in info["symbols"] if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
    tick = {t["symbol"]: float(t["quoteVolume"]) for t in get("/fapi/v1/ticker/24hr")}
    # coins that never trade $5M a day now are very unlikely to have been $20M movers
    syms = [s for s in syms if tick.get(s, 0) >= 5e6 or s in sc.LEAD]
    progress(f"Step 1/3: hourly prices for {len(syms)} coins…")
    h1 = {}
    for n, s in enumerate(syms):
        k = sc.safe(klines_range, s, "1h", warm1h, now)
        if k and len(k["t"]) > 30:
            h1[s] = k
        if n % 100 == 99:
            progress(f"Step 1/3: {n + 1}/{len(syms)} coins")
    hours = list(range(start, now - HOUR + 1, HOUR))         # signal times (an hour has just closed)

    # which coins were top movers at each hour?
    movers = {}
    for t in hours:
        pool = []
        for s, k in h1.items():
            if s in sc.LEAD:
                continue
            i = bisect.bisect_left(k["t"], t) - 1            # candle that closed at t
            if i < 24 or k["t"][i] + HOUR != t:
                continue
            qv = sum(k["q"][i - 23:i + 1])
            if qv < MIN_VOL:
                continue
            pool.append((s, (k["c"][i] / k["c"][i - 24] - 1) * 100, qv))
        g = sorted(pool, key=lambda x: -x[1])[:N_GAIN]
        gs = {x[0] for x in g}
        picks = g + [x for x in sorted(pool, key=lambda x: x[1])[:N_LOSE] if x[0] not in gs]
        by_vol = sorted(picks, key=lambda x: x[2])
        movers[t] = [{"sym": s, "chg": ch, "qv": qv, "vol_pct": by_vol.index((s, ch, qv)) / max(1, len(picks) - 1)}
                     for s, ch, qv in picks]
    cands = sorted({p["sym"] for ps in movers.values() for p in ps})
    progress(f"Step 2/3: detail data for {len(cands)} coins that were top movers…")

    def detail(s):
        d = {"1h": prep(h1[s], "1h"),
             "15m": prep(klines_range(s, "15m", start - 30 * Q15, now), "15m"),
             "4h": prep(klines_range(s, "4h", start - 320 * 4 * HOUR, now), "4h")}
        d["1h"]["atr"] = atr_series(d["1h"]["h"], d["1h"]["l"], d["1h"]["c"])
        d["1h"]["e20"] = ema_series(d["1h"]["c"], 20)
        return d

    btc = detail("BTCUSDT")
    det = {}
    for n, s in enumerate(cands):
        try:
            d = detail(s)
            oi = data_range("/futures/data/openInterestHist", s, start - 6 * HOUR, now)
            d["oi"] = ([int(x["timestamp"]) for x in oi], [float(x["sumOpenInterestValue"]) for x in oi])
            tk = data_range("/futures/data/takerlongshortRatio", s, start - 4 * HOUR, now)
            d["tk"] = ([int(x["timestamp"]) + HOUR for x in tk], [float(x["buyVol"]) for x in tk], [float(x["sellVol"]) for x in tk])
            ls = data_range("/futures/data/globalLongShortAccountRatio", s, start - 2 * HOUR, now)
            d["ls"] = ([int(x["timestamp"]) for x in ls], [float(x["longShortRatio"]) for x in ls])
            fr = funding_range(s, start - 9 * HOUR, now)
            d["fr"] = ([int(x["fundingTime"]) for x in fr], [float(x["fundingRate"]) for x in fr])
            det[s] = d
        except Exception as e:  # a delisted or brand-new coin: skip it
            progress(f"(skipped {s}: {e.__class__.__name__})")
        if n % 25 == 24:
            progress(f"Step 2/3: {n + 1}/{len(cands)} coins")

    progress("Step 3/3: replaying every hour…")

    def state(d, t):
        j = [last_closed(d[tf], t) for tf, _, _ in sc.TFS]
        return [trend_at(d[tf], jj) for (tf, _, _), jj in zip(sc.TFS, j)]

    trades, busy_new, busy_old, seen_signals = [], {}, {}, 0
    for t in hours:
        bi = last_closed(btc["1h"], t)
        if bi < 50:
            continue
        b = {"trends": state(btc, t), "ch4": (btc["1h"]["c"][bi] / btc["1h"]["c"][bi - 4] - 1) * 100}
        b_ret = sc.returns(btc["1h"]["c"][bi - 48:bi + 1])
        for p in movers[t]:
            s = p["sym"]
            d = det.get(s)
            if not d:
                continue
            k = d["1h"]; i = last_closed(k, t)
            if i < 50 or k["atr"][i] is None or k["e20"][i] is None:
                continue
            closed = k["q"][:i + 1]
            base = sc.avg(closed[-27:-3])
            price, a = k["c"][i], k["atr"][i]
            oi_t, oi_v = d["oi"]; oj = bisect.bisect_right(oi_t, t)
            tk_t = d["tk"][0]; tj = bisect.bisect_right(tk_t, t)
            ls_t, ls_v = d["ls"]; lj = bisect.bisect_right(ls_t, t)
            fr_t, fr_v = d["fr"]; fj = bisect.bisect_right(fr_t, t)
            sell = sum(d["tk"][2][max(0, tj - 3):tj])
            c = {**p, "trends": state(d, t), "spike": sc.avg(closed[-3:]) / base if base else 1, "price": price, "atr": a,
                 "ext": (price - k["e20"][i]) / a if a else 0, "ch4": (price / k["c"][i - 4] - 1) * 100,
                 "oi_ch4": (oi_v[oj - 1] / oi_v[oj - 5] - 1) * 100 if oj >= 5 else None,
                 "taker": sum(d["tk"][1][max(0, tj - 3):tj]) / sell if tj >= 1 and sell else None,
                 "lsr": ls_v[lj - 1] if lj else None,
                 "funding": fr_v[fj - 1] if fj else None,
                 "corr": sc.pearson(sc.returns(k["c"][i - 48:i + 1]), b_ret)}
            c["rs4"] = c["ch4"] - b["ch4"]
            new = sc.score(c, b)
            clean_new, clean_old = is_clean(new), score_v1(c, b)
            if not (clean_new or clean_old):
                continue
            seen_signals += 1
            take_new = clean_new and busy_new.get(s, 0) <= t     # one trade per coin at a time, per rule set
            take_old = clean_old and busy_old.get(s, 0) <= t
            if not (take_new or take_old):
                continue
            risk = STOP_ATR * a
            r, exit_t, outcome = simulate(d["15m"], t, new["dir"], price, risk)
            if r is None:
                continue
            fee_r = FEE * price / risk
            if take_new: busy_new[s] = exit_t
            if take_old: busy_old[s] = exit_t
            trades.append({"time": t, "sym": s, "side": "long" if new["dir"] > 0 else "short", "score": new["score"],
                           "flags": new["flags"], "new": take_new, "old": take_old, "blocked": clean_old and not clean_new,
                           "outcome": outcome,
                           "r": round(r - fee_r, 3), "fee_r": round(fee_r, 3), "stop_pct": round(risk / price * 100, 2),
                           "hours": round((exit_t - t) / HOUR, 1), "chg": round(p["chg"], 1),
                           "funding": new["funding"]})
    return summarise(trades, days, start, now, len(cands))


# ---------- results ----------
def stats(ts):
    n = len(ts)
    if not n:
        return {"n": 0, "win": None, "avg": None, "total": 0, "target": 0, "stop": 0, "time": 0, "maxdd": 0}
    eq, peak, dd = 0, 0, 0
    for x in sorted(ts, key=lambda x: x["time"]):
        eq += x["r"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    return {"n": n, "win": sum(x["r"] > 0 for x in ts) / n, "avg": sum(x["r"] for x in ts) / n,
            "total": sum(x["r"] for x in ts), "target": sum(x["outcome"] == "target" for x in ts),
            "stop": sum(x["outcome"] == "stop" for x in ts), "time": sum(x["outcome"] == "time" for x in ts),
            "maxdd": dd}


def summarise(trades, days, start, end, n_coins):
    new = [x for x in trades if x["new"]]
    old = [x for x in trades if x["old"]]
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": days,
           "from": start, "to": end, "coins_checked": n_coins,
           "rules": {"stop_atr": STOP_ATR, "target_r": T1_R, "max_hold_h": MAX_HOLD_H, "fee_pct": FEE * 100},
           "new": stats(new), "old": stats(old),
           "new_long": stats([x for x in new if x["side"] == "long"]),
           "new_short": stats([x for x in new if x["side"] == "short"]),
           "by_score": {b: stats([x for x in new if lo <= x["score"] < hi])
                        for b, lo, hi in (("60-69", 60, 70), ("70-79", 70, 80), ("80+", 80, 101))},
           "blocked_by_new_flags": stats([x for x in old if x["blocked"]]),
           "trades": trades}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    return res


def pct(x):
    return "–" if x is None else f"{x * 100:.0f}%"


def r_(x):
    return "–" if x is None else f"{x:+.2f}R"


def line(name, s):
    if not s["n"]:
        return f"{name}: no trades"
    return (f"{name}: <b>{s['n']}</b> trades · win {pct(s['win'])} · avg {r_(s['avg'])} · "
            f"total <b>{s['total']:+.1f}R</b>")


def text(res):
    n, o = res["new"], res["old"]
    lines = [f"<b>🧪 Backtest — last {res['days']} days</b>",
             f"Every hour, the top movers ({res['coins_checked']} coins in total) were scored as the scanner would have "
             f"seen them, and every Clean signal was traded: stop 1.5× ATR, target 1.5R, max 24h, fees included.", ""]
    if n["n"]:
        lines.append(line("Current rules", n))
        lines.append(f"  {n['target']} hit target · {n['stop']} stopped · {n['time']} closed at 24h · worst run of losses −{n['maxdd']:.1f}R")
        verdict = ("✅ profitable" if n["avg"] > 0.05 else "➖ about break-even" if n["avg"] > -0.05 else "❌ losing")
        lines.append(f"  → {verdict} over this period")
    else:
        lines.append("Current rules: no Clean signals in this period.")
    lines += ["", line("Longs", res["new_long"]), line("Shorts", res["new_short"]), "",
              "<b>By score</b>"] + [line(b, s) for b, s in res["by_score"].items()]
    lines += ["", line("Rules before the ERA fix", o),
              line("Signals the new Squeeze/Late/vs BTC flags blocked", res["blocked_by_new_flags"]), "",
              "<i>1R = what you lose when the stop is hit. If you risk 1 USDT per trade, +10R = +10 USDT. "
              "Past results don't guarantee future ones, and a month is a small sample.</i>"]
    return "\n".join(lines)


def chart(res, path=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter
    path = path or os.path.join(OUT_DIR, "latest.png")
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=130)
    fig.patch.set_facecolor("#0e1524"); ax.set_facecolor("#0e1524")
    for key, col, lab in (("old", "#8a94a8", "Rules before the ERA fix"), ("new", "#f0b90b", "Current rules")):
        ts = sorted([x for x in res["trades"] if x[key]], key=lambda x: x["time"])
        if not ts:
            continue
        xs = [datetime.fromtimestamp(ts[0]["time"] / 1000)] + [datetime.fromtimestamp(x["time"] / 1000) for x in ts]
        ys = [0]
        for x in ts:
            ys.append(ys[-1] + x["r"])
        ax.step(xs, ys, where="post", color=col, lw=2 if key == "new" else 1.4, label=f"{lab} ({ys[-1]:+.1f}R)")
    ax.axhline(0, color="#3a4660", lw=1)
    ax.set_title("Scanner backtest: running result in R (fees included)", color="white", fontsize=11, loc="left")
    ax.tick_params(colors="#8a94a8", labelsize=8)
    for sp in ax.spines.values():
        sp.set_color("#26324a")
    ax.xaxis.set_major_formatter(DateFormatter("%d %b"))
    ax.grid(color="#1c2638", lw=0.8)
    ax.legend(facecolor="#141d30", edgecolor="#26324a", labelcolor="white", fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


if __name__ == "__main__":
    import sys
    r = run(int(sys.argv[1]) if len(sys.argv) > 1 else 28, progress=print)
    print(text(r).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    print(chart(r))
