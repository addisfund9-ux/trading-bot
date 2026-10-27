"""Pump trigger: Dagi's idea, tested before trusting it.

  Direction (slow): 30m EMA 9/21, 1h EMA 20/50 and 4h EMA 50/200 all point the same way.
  Trigger (fast):   1m, 5m and 15m EMA 9/21 all line up with that direction — the minute they first do.
  Early signs:      volume shift (1m and 5m volume vs the last 30 minutes, taker buy share).
  Reversal warning: EMA gap — how far EMA9 is from EMA21 on 15m, in ATRs.

Like the live charts, the EMAs use the candle that is still forming. Every trigger is replayed on 1-minute
candles with a grid of stops/targets/hold times, fees included, and saved so other ideas can be tested.
"""
import bisect
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import backtest as bt
import scanner as sc

MIN = 60_000
HOUR = 60 * MIN
FEE = 0.001
STOPS = (1.0, 1.5, 2.0)          # x 15m ATR
TARGETS = (1.0, 1.5, 2.0)        # R
HOLDS = (30, 120, 240)           # minutes
OUT_DIR = bt.OUT_DIR


# ---------------- data ----------------
def k1m(sym, start, end, tf="1m"):
    ms = {"1m": MIN, "1h": HOUR, "4h": 4 * HOUR}[tf]
    rows, t = [], start
    while t < end:
        k = bt.get("/fapi/v1/klines", symbol=sym, interval=tf, startTime=t, endTime=end, limit=1500)
        if not k:
            break
        rows += k
        nxt = k[-1][0] + ms
        if nxt <= t or len(k) < 1500:
            break
        t = nxt
    seen, out = set(), []
    for x in rows:
        if x[0] not in seen:
            seen.add(x[0]); out.append(x)
    a = np.array([[float(v) for v in x[:11]] for x in out]) if out else np.zeros((0, 11))
    return {"t": a[:, 0].astype(np.int64), "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4],
            "q": a[:, 7], "tbq": a[:, 10]}


def ema_np(x, n):
    k = 2 / (n + 1)
    out = np.empty_like(x)
    if len(x) == 0:
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def bucket_closes(m, minutes):
    """Closed-bar closes of an N-minute chart built from 1m candles, and each minute's bucket index."""
    b = (m["t"] // (minutes * MIN)).astype(np.int64)
    starts = np.r_[0, np.nonzero(np.diff(b))[0] + 1]
    ends = np.r_[starts[1:] - 1, len(b) - 1]
    closes = m["c"][ends]
    highs = np.maximum.reduceat(m["h"], starts)
    lows = np.minimum.reduceat(m["l"], starts)
    idx = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(b)]))
    return closes, highs, lows, idx


def forming_trend(price, closes, idx, fast, slow):
    """Trend per minute using the still-forming candle (as on the live chart)."""
    ef, es = ema_np(closes, fast), ema_np(closes, slow)
    kf, ks = 2 / (fast + 1), 2 / (slow + 1)
    prev_f = np.r_[closes[0], ef[:-1]][idx]
    prev_s = np.r_[closes[0], es[:-1]][idx]
    f = prev_f * (1 - kf) + price * kf
    s = prev_s * (1 - ks) + price * ks
    tr = np.where((price > f) & (f > s), 1, np.where((price < f) & (f < s), -1, 0))
    return tr, f, s


def higher_trend(price, tmin, k, fast, slow, ms):
    """Same, for 1h/4h candles fetched directly."""
    c = k["c"]
    ef, es = ema_np(c, fast), ema_np(c, slow)
    j = np.searchsorted(k["t"], tmin, side="right") - 1          # the candle each minute belongs to
    j = np.clip(j, 1, len(c) - 1)
    kf, ks = 2 / (fast + 1), 2 / (slow + 1)
    f = ef[j - 1] * (1 - kf) + price * kf
    s = es[j - 1] * (1 - ks) + price * ks
    warm = j >= slow
    tr = np.where((price > f) & (f > s), 1, np.where((price < f) & (f < s), -1, 0))
    return np.where(warm, tr, 0)


def atr_closed(h, l, c, n=14):
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.r_[c[0], c[:-1]]), np.abs(l - np.r_[c[0], c[:-1]])))
    out = np.empty_like(c)
    out[:] = np.nan
    if len(c) > n:
        a = tr[1:n + 1].mean(); out[n] = a
        for i in range(n + 1, len(c)):
            a = (a * (n - 1) + tr[i]) / n; out[i] = a
    return out


# ---------------- replay one coin ----------------
def replay(sym, m, k1h, k4h, btc_slow, start, movers_at, horizon=max(HOLDS)):
    price, tmin = m["c"], m["t"] + MIN                              # time = minute close
    trs = {}
    for name, mins in (("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30)):
        if mins == 1:
            ef, es = ema_np(price, 9), ema_np(price, 21)
            trs[name] = np.where((price > ef) & (ef > es), 1, np.where((price < ef) & (ef < es), -1, 0))
            continue
        closes, highs, lows, idx = bucket_closes(m, mins)
        tr, f, s = forming_trend(price, closes, idx, 9, 21)
        trs[name] = tr
        if mins == 15:
            atr15 = atr_closed(highs, lows, closes)
            atr_prev = np.r_[np.nan, atr15[:-1]][idx]                # last closed 15m ATR
            gap15 = np.abs(f - s) / atr_prev
    trs["1h"] = higher_trend(price, tmin, k1h, 20, 50, HOUR)
    trs["4h"] = higher_trend(price, tmin, k4h, 50, 200, 4 * HOUR)
    slow = (trs["30m"] == trs["1h"]) & (trs["1h"] == trs["4h"]) & (trs["4h"] != 0)
    d = trs["4h"]
    fast = (trs["1m"] == d) & (trs["5m"] == d) & (trs["15m"] == d) & slow
    trig = fast & ~np.r_[False, fast[:-1]]
    q = m["q"]; tb = m["tbq"]
    cq = np.cumsum(np.r_[0, q]); ctb = np.cumsum(np.r_[0, tb])
    sig, busy = [], 0
    for i in np.nonzero(trig)[0]:
        if i < 60 or tmin[i] < start or i + horizon >= len(price) or tmin[i] < busy:
            continue
        a = atr_prev[i]
        if not np.isfinite(a) or a <= 0:
            continue
        hour = tmin[i] // HOUR * HOUR
        mv = movers_at.get(hour, {}).get(sym)
        base = (cq[i - 2] - cq[i - 32]) / 30 or 1e-9
        v1 = q[i] / base
        v5 = (cq[i + 1] - cq[i - 4]) / 5 / base
        tk = (ctb[i + 1] - ctb[i - 4]) / max(cq[i + 1] - cq[i - 4], 1e-9)
        dd = int(d[i])
        entry = price[i]
        hi, lo = m["h"][i + 1:i + 1 + horizon], m["l"][i + 1:i + 1 + horizon]
        res = {}
        for st in STOPS:
            risk = st * a
            fee_r = FEE * entry / risk
            hit_stop = (lo <= entry - risk) if dd > 0 else (hi >= entry + risk)
            for tg in TARGETS:
                hit_tgt = (hi >= entry + tg * risk) if dd > 0 else (lo <= entry - tg * risk)
                for hd in HOLDS:
                    s_i = np.argmax(hit_stop[:hd]) if hit_stop[:hd].any() else hd
                    t_i = np.argmax(hit_tgt[:hd]) if hit_tgt[:hd].any() else hd
                    if s_i <= t_i and s_i < hd:
                        r = -1.0
                    elif t_i < hd:
                        r = tg
                    else:
                        r = dd * (price[i + hd] - entry) / risk
                    res[f"{st}|{tg}|{hd}"] = round(r - fee_r, 3)
        # forward move regardless of exits: did a pump actually follow?
        fwd = {h: round(dd * (price[i + h] / entry - 1) * 100, 3) for h in (15, 60, 240)}
        mfe = round(float(((hi[:60].max() / entry - 1) if dd > 0 else (1 - lo[:60].min() / entry)) * 100), 3)
        sig.append({"time": int(tmin[i]), "sym": sym, "dir": dd, "mover": mv is not None,
                    "chg": None if mv is None else round(mv, 2), "v1": round(float(v1), 2), "v5": round(float(v5), 2),
                    "taker": round(float(tk), 3), "gap15": round(float(gap15[i]), 2) if np.isfinite(gap15[i]) else None,
                    "atr_pct": round(float(a / entry * 100), 3), "btc_with": bool(btc_slow is not None and
                    btc_slow[min(i, len(btc_slow) - 1)] == dd), "fwd": fwd, "mfe60": mfe, "r": res})
        busy = tmin[i] + 30 * MIN          # don't count the same move twice within 30 minutes
    return sig


# ---------------- run ----------------
def run(days=14, max_coins=100, progress=lambda m: None):
    days = max(2, min(25, int(days)))
    now = int(time.time() * 1000) // MIN * MIN
    start = now - days * 24 * HOUR
    info = bt.get("/fapi/v1/exchangeInfo")
    syms = [s["symbol"] for s in info["symbols"] if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
    tick = {t["symbol"]: float(t["quoteVolume"]) for t in bt.get("/fapi/v1/ticker/24hr")}
    syms = [s for s in syms if tick.get(s, 0) >= 5e6 and s not in sc.LEAD]
    progress(f"Pump test 1/3: finding top movers among {len(syms)} coins…")
    with ThreadPoolExecutor(bt.WORKERS) as ex:
        h1 = dict(zip(syms, ex.map(lambda s: sc.safe(bt.klines_range, s, "1h", start - 30 * HOUR, now), syms)))
    movers_at, count = {}, {}
    for hour in range(start // HOUR * HOUR, now, HOUR):
        pool = []
        for s, k in h1.items():
            if not k:
                continue
            i = bisect.bisect_left(k["t"], hour) - 1
            if i < 24 or k["t"][i] + HOUR != hour:
                continue
            qv = sum(k["q"][i - 23:i + 1])
            if qv >= bt.MIN_VOL:
                pool.append((s, (k["c"][i] / k["c"][i - 24] - 1) * 100))
        g = sorted(pool, key=lambda x: -x[1])[:bt.N_GAIN]
        l = [x for x in sorted(pool, key=lambda x: x[1])[:bt.N_LOSE] if x not in g]
        movers_at[hour] = {s: ch for s, ch in g + l}
        for s, _ in g + l:
            count[s] = count.get(s, 0) + 1
    cands = [s for s, _ in sorted(count.items(), key=lambda x: -x[1])[:max_coins]]
    progress(f"Pump test 2/3: 1-minute candles for {len(cands)} coins (the slow part)…")
    warm = 25 * 4 * HOUR

    def load(s):
        try:
            return s, {"m": k1m(s, start - 2 * HOUR, now), "1h": k1m(s, start - 60 * HOUR, now, "1h"),
                       "4h": k1m(s, start - 210 * 4 * HOUR, now, "4h")}
        except Exception as e:
            progress(f"(skipped {s}: {e.__class__.__name__})")
            return s, None

    btc = load("BTCUSDT")[1]
    btc_slow = None
    if btc:
        p = btc["m"]["c"]; tm = btc["m"]["t"] + MIN
        c30, _, _, idx = bucket_closes(btc["m"], 30)
        t30 = forming_trend(p, c30, idx, 9, 21)[0]
        t1 = higher_trend(p, tm, btc["1h"], 20, 50, HOUR); t4 = higher_trend(p, tm, btc["4h"], 50, 200, 4 * HOUR)
        btc_slow = np.where((t30 == t1) & (t1 == t4), t4, 0)
        btc_t = tm
    signals = []
    with ThreadPoolExecutor(bt.WORKERS) as ex:
        for n, (s, data) in enumerate(ex.map(load, cands)):
            if data and len(data["m"]["t"]) > 300:
                bs = None
                if btc_slow is not None:
                    j = np.clip(np.searchsorted(btc_t, data["m"]["t"] + MIN), 0, len(btc_slow) - 1)
                    bs = btc_slow[j]
                try:
                    signals += replay(s, data["m"], data["1h"], data["4h"], bs, start, movers_at)
                except Exception as e:
                    progress(f"(replay failed {s}: {e})")
            if n % 10 == 9:
                progress(f"Pump test 2/3: {n + 1}/{len(cands)} coins, {len(signals)} triggers so far")
    progress("Pump test 3/3: scoring…")
    res = summarise(signals, days, start, now, len(cands))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "pump_signals.json"), "w", encoding="utf-8") as fh:
        json.dump({"from": start, "to": now, "days": days, "signals": signals}, fh, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "pump_latest.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    return res


def _stats(vals):
    if not vals:
        return {"n": 0, "win": None, "avg": None, "total": 0}
    return {"n": len(vals), "win": sum(v > 0 for v in vals) / len(vals), "avg": sum(vals) / len(vals), "total": sum(vals)}


def summarise(sig, days, start, end, n_coins):
    mid = (start + end) // 2
    out = {"days": days, "coins": n_coins, "triggers": len(sig), "configs": {}}
    pick = lambda f: [s for s in sig if f(s)]
    groups = {"all": pick(lambda s: True), "movers": pick(lambda s: s["mover"]),
              "movers+volume": pick(lambda s: s["mover"] and s["v5"] >= 2 and (s["taker"] >= 0.55 if s["dir"] > 0 else s["taker"] <= 0.45)),
              "movers+small gap": pick(lambda s: s["mover"] and s["gap15"] is not None and s["gap15"] < 0.5),
              "movers+big gap": pick(lambda s: s["mover"] and s["gap15"] is not None and s["gap15"] >= 1.5),
              "movers+BTC with": pick(lambda s: s["mover"] and s["btc_with"])}
    for gname, g in groups.items():
        cfg = {}
        for key in (g[0]["r"].keys() if g else []):
            a = [s["r"][key] for s in g if s["time"] < mid]; b = [s["r"][key] for s in g if s["time"] >= mid]
            cfg[key] = {"all": _stats(a + b), "h1": _stats(a), "h2": _stats(b)}
        best = max(cfg.items(), key=lambda kv: min(kv[1]["h1"]["avg"] or -9, kv[1]["h2"]["avg"] or -9)) if cfg else None
        out["configs"][gname] = {"n": len(g), "best": best[0] if best else None,
                                 "best_stats": best[1] if best else None,
                                 "default": cfg.get("1.5|1.5|120"),
                                 "fwd60": _stats([s["fwd"][60] for s in g])}
    return out


def _r(v):
    return "–" if v is None else f"{v:+.2f}R"


def text(res):
    lines = [f"<b>🚀 Pump trigger test — last {res['days']} days</b>",
             f"{res['triggers']} triggers on {res['coins']} coins: 30m/1h/4h set the direction, then 1m/5m/15m "
             "EMA 9/21 all line up. Results per trade, fees included.", ""]
    names = {"all": "Every trigger", "movers": "Only top movers", "movers+volume": "Top movers + volume shift",
             "movers+small gap": "Top movers + small EMA gap", "movers+big gap": "Top movers + big EMA gap",
             "movers+BTC with": "Top movers + BTC same way"}
    for k, v in res["configs"].items():
        d = v["default"]
        if not v["n"] or not d:
            lines.append(f"{names[k]}: no triggers"); continue
        a = d["all"]
        lines.append(f"<b>{names[k]}</b>: {a['n']} trades · win {a['win'] * 100:.0f}% · avg {a['avg']:+.2f}R · "
                     f"total {a['total']:+.1f}R · price 1h later {v['fwd60']['avg']:+.2f}% on average")
        b = v["best_stats"]
        st, tg, hd = v["best"].split("|")
        lines.append(f"   best exit (stop {st}× ATR, target {tg}R, max {int(hd) // 60 or hd}{'h' if int(hd) >= 60 else 'm'}): "
                     f"first half {_r(b['h1']['avg'])}, second half {_r(b['h2']['avg'])}")
    lines += ["", "<i>Default exit: stop 1.5× 15m ATR, target 1.5R, max 2h. A result only counts if it holds in both "
              "halves of the period.</i>"]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    r = run(int(sys.argv[1]) if len(sys.argv) > 1 else 14, progress=print)
    print(text(r))
