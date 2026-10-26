"""Python version of the Morning Futures Scanner (same rules and scoring as the v3 web page)."""
from concurrent.futures import ThreadPoolExecutor

import requests

API = "https://fapi.binance.com"
TFS = [("15m", 9, 21), ("1h", 20, 50), ("4h", 50, 200)]
LEAD = ("BTCUSDT", "ETHUSDT")
session = requests.Session()


def get(path, **params):
    r = session.get(API + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:
        return None


def ema(vals, n):
    if len(vals) < n:
        return []
    k = 2 / (n + 1)
    out = [None] * len(vals)
    out[n - 1] = sum(vals[:n]) / n
    for i in range(n, len(vals)):
        out[i] = vals[i] * k + out[i - 1] * (1 - k)
    return out


def trend(closes, fast, slow):
    f, s = ema(closes, fast), ema(closes, slow)
    if not f or not s or f[-1] is None or s[-1] is None:
        return 0
    p = closes[-1]
    if p > f[-1] > s[-1]:
        return 1
    if p < f[-1] < s[-1]:
        return -1
    return 0


def atr(h, l, c, n=14):
    if len(c) <= n:
        return 0
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    for i in range(n + 1, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
    return a


def pearson(a, b):
    n = min(len(a), len(b))
    if n < 5:
        return 0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a)
    db = sum((y - mb) ** 2 for y in b)
    return num / (da * db) ** 0.5 if da and db else 0


def returns(c):
    return [c[i] / c[i - 1] - 1 for i in range(1, len(c))]


def pct_change(c, bars):
    return (c[-1] / c[-1 - bars] - 1) * 100 if len(c) > bars else 0


def clamp01(x):
    return max(0.0, min(1.0, x))


def avg(a):
    return sum(a) / len(a) if a else 0


def klines(sym, tf, limit=300):
    k = get("/fapi/v1/klines", symbol=sym, interval=tf, limit=limit)
    return {"high": [float(x[2]) for x in k], "low": [float(x[3]) for x in k],
            "close": [float(x[4]) for x in k], "qvol": [float(x[7]) for x in k]}


def derivs(sym):
    oi = safe(get, "/futures/data/openInterestHist", symbol=sym, period="1h", limit=25)
    tk = safe(get, "/futures/data/takerlongshortRatio", symbol=sym, period="1h", limit=3)
    ls = safe(get, "/futures/data/globalLongShortAccountRatio", symbol=sym, period="1h", limit=1)
    out = {"oi_ch4": None, "taker": None, "lsr": None}
    if isinstance(oi, list) and len(oi) >= 5:
        v = [float(x["sumOpenInterestValue"]) for x in oi]
        out["oi_ch4"] = (v[-1] / v[-5] - 1) * 100
    if isinstance(tk, list) and tk:
        b = sum(float(x["buyVol"]) for x in tk)
        s = sum(float(x["sellVol"]) for x in tk)
        out["taker"] = b / s if s else None
    if isinstance(ls, list) and ls:
        out["lsr"] = float(ls[-1]["longShortRatio"])
    return out


def analyse(sym):
    data = {tf: klines(sym, tf) for tf, _, _ in TFS}
    trends = [trend(data[tf]["close"], f, s) for tf, f, s in TFS]
    h1 = data["1h"]
    closed = h1["qvol"][:-1]
    base = avg(closed[-27:-3])
    spike = avg(closed[-3:]) / base if base else 1
    price = h1["close"][-1]
    a = atr(h1["high"], h1["low"], h1["close"])
    e20 = ema(h1["close"], 20)
    ext = (price - e20[-1]) / a if a and e20 else 0
    return {"trends": trends, "spike": spike, "price": price, "atr": a, "ext": ext,
            "h1close": h1["close"], "ch4": pct_change(h1["close"], 4), **derivs(sym)}


def score(c, btc):
    tsum = sum(c["trends"])
    d = 1 if tsum > 0 else -1 if tsum < 0 else (1 if c["chg"] >= 0 else -1)
    full = abs(tsum) == 3
    agree = sum(1 for t in c["trends"] if t == d)
    bsum = sum(btc["trends"])
    bdir = 1 if bsum == 3 else -1 if bsum == -3 else 0
    flags = []
    if bdir == d:
        btc_pts = 8
    elif bdir == -d:  # BTC fully against, even if the coin hasn't followed it yet
        btc_pts = 3 if abs(c["corr"]) < 0.3 else 0
        flags.append("vs BTC")
    else:
        btc_pts = 5.6 if abs(c["corr"]) < 0.3 else 4
    oi_pts = 6
    if c["oi_ch4"] is not None:
        with_move = d * c["ch4"] > 0
        if c["oi_ch4"] <= 0:
            oi_pts = 0
            if with_move:
                flags.append("OI falling")
        else:
            oi_pts = 4 + 8 * clamp01(c["oi_ch4"] / 8) if with_move else 3
    tk_pts = 5 if c["taker"] is None else 10 * clamp01((c["taker"] - 0.95) / 0.25 if d > 0 else (1.05 - c["taker"]) / 0.25)
    f_pct = c["funding"] * 100 if c["funding"] is not None else None
    crowd_f = f_pct is not None and (f_pct >= 0.05 if d > 0 else f_pct <= -0.05)
    crowd_l = c["lsr"] is not None and (c["lsr"] >= 2.5 if d > 0 else c["lsr"] <= 0.6)
    crowd_pts = 5 - (3 if crowd_f else 0) - (2 if crowd_l else 0)
    if crowd_f or crowd_l:
        flags.append("Crowded")
    stretched = d * c["ext"] > 3
    if stretched:
        flags.append("Stretched")
    squeeze = f_pct is not None and abs(f_pct) >= 0.1  # one side being forced out; can snap back
    if squeeze:
        flags.append("Squeeze")
    late = d * c["chg"] >= 20  # already 20%+ in this direction today
    if late:
        flags.append("Late")
    pts = (35 * agree / 3 + oi_pts + c["vol_pct"] * 12 + clamp01((c["spike"] - 1) / 2) * 10 + tk_pts
           + clamp01(d * c["rs4"] / 5) * 8 + btc_pts + crowd_pts + (-10 if stretched else 0)
           - (8 if squeeze else 0) - (8 if late else 0))
    return {**c, "dir": d, "full": full, "score": max(0, min(100, round(pts))), "flags": flags}


def scan(n=30, min_vol_m=20, include_losers=True, workers=4):
    info = get("/fapi/v1/exchangeInfo")
    tickers = get("/fapi/v1/ticker/24hr")
    prem = safe(get, "/fapi/v1/premiumIndex") or []
    live = {s["symbol"] for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    fund = {p["symbol"]: float(p["lastFundingRate"]) for p in prem if p.get("lastFundingRate") not in (None, "")}
    pool0 = [{"sym": t["symbol"], "chg": float(t["priceChangePercent"]), "qv": float(t["quoteVolume"])}
             for t in tickers if t["symbol"] in live and t["symbol"] not in LEAD and float(t["quoteVolume"]) >= min_vol_m * 1e6]
    gainers = sorted(pool0, key=lambda x: -x["chg"])[:n]
    picks = list(gainers)
    if include_losers:
        g = {x["sym"] for x in gainers}
        picks += [x for x in sorted(pool0, key=lambda x: x["chg"])[: (n + 1) // 2] if x["sym"] not in g]
    leaders = {}
    for s in LEAD:
        a = analyse(s)
        t = next((x for x in tickers if x["symbol"] == s), None)
        leaders[s] = {**a, "sym": s, "chg": float(t["priceChangePercent"]) if t else 0}
    btc = leaders["BTCUSDT"]
    btc_ret = returns(btc["h1close"][-49:])
    by_vol = sorted(picks, key=lambda x: x["qv"])
    for p in picks:
        p["vol_pct"] = by_vol.index(p) / (len(picks) - 1) if len(picks) > 1 else 1

    def one(p):
        try:
            a = analyse(p["sym"])
            corr = pearson(returns(a["h1close"][-49:]), btc_ret)
            return score({**p, **a, "corr": corr, "rs4": a["ch4"] - btc["ch4"], "funding": fund.get(p["sym"])}, btc)
        except Exception:
            return None

    with ThreadPoolExecutor(workers) as ex:
        coins = [c for c in ex.map(one, picks) if c]
    coins.sort(key=lambda c: -c["score"])
    return {"leaders": leaders, "coins": coins}


def clean_picks(result, k=5, min_score=60):
    bad = {"Stretched", "Crowded", "OI falling", "Squeeze", "Late"}
    return [c for c in result["coins"] if c["full"] and c["score"] >= min_score and not bad & set(c["flags"])][:k]


def leader_line(l):
    s = sum(l["trends"])
    state = "🟢 bullish" if s == 3 else "🔴 bearish" if s == -3 else "⚪ mixed"
    tf = " ".join(f"{tf}{'▲' if t > 0 else '▼' if t < 0 else '–'}" for (tf, _, _), t in zip(TFS, l["trends"]))
    return f"{l['sym'][:-4]} {l['chg']:+.2f}% · {tf} · {state}"


def tv_url(sym):
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}.P"


def scan_text(result, k=5):
    lines = ["<b>🔎 Market</b>"] + [leader_line(l) for l in result["leaders"].values()]
    picks = clean_picks(result, k)
    lines.append("")
    if picks:
        lines.append(f"<b>Clean setups</b> (all 3 timeframes aligned, score ≥ 60, no warning flags)")
        for c in picks:
            side = "🟢 LONG" if c["dir"] > 0 else "🔴 SHORT"
            oi = "–" if c["oi_ch4"] is None else f"{c['oi_ch4']:+.1f}%"
            lines.append(f"{side} <a href=\"{tv_url(c['sym'])}\">{c['sym'][:-4]}</a> · score {c['score']} · "
                         f"24h {c['chg']:+.1f}% · vol {c['spike']:.1f}× · OI 4h {oi}")
    else:
        lines.append("No clean setups right now — nothing scores 60+ with all timeframes aligned and no warning flags.")
    flagged = [c for c in result["coins"] if c["full"] and c["score"] >= 60 and c not in picks][:3]
    if flagged:
        lines.append("\n<i>Strong but flagged:</i> " + ", ".join(f"{c['sym'][:-4]} ({', '.join(c['flags'])})" for c in flagged))
    return "\n".join(lines)
