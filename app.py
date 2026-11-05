"""Local equity research dashboard — a lightweight Yahoo Finance-style tool.

Run:  .venv/bin/python app.py   then open http://127.0.0.1:5050
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests
import yfinance as yf
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

RSS_FEEDS = {
    "Bloomberg": "https://feeds.bloomberg.com/markets/news.rss",
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
}

MARKET_STRIP = [
    ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow 30"),
    ("^RUT", "Russell 2000"), ("^VIX", "VIX"), ("^TNX", "10Y Yield"),
    ("CL=F", "Crude Oil"), ("GC=F", "Gold"), ("BTC-USD", "Bitcoin"),
]

# range key -> (yfinance period, interval)
RANGES = {
    "1d": ("1d", "5m"),
    "5d": ("5d", "30m"),
    "1mo": ("1mo", "1d"),
    "3mo": ("3mo", "1d"),
    "ytd": ("ytd", "1d"),
    "1y": ("1y", "1d"),
    "5y": ("5y", "1wk"),
}

# ---------------------------------------------------------------- tiny TTL cache
_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def clean(v):
    """Make values JSON-safe (NaN/inf -> None)."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ---------------------------------------------------------------- news
def _fetch_rss(source, url):
    try:
        r = requests.get(url, headers=UA, timeout=8)
        feed = feedparser.parse(r.content)
    except Exception:
        return []
    items = []
    for e in feed.entries[:25]:
        ts = None
        if e.get("published"):
            try:
                ts = parsedate_to_datetime(e.published).timestamp()
            except Exception:
                pass
        thumb = None
        media = e.get("media_content") or e.get("media_thumbnail")
        if media and isinstance(media, list) and media[0].get("url"):
            thumb = media[0]["url"]
        items.append({
            "source": source, "title": e.get("title", "").strip(),
            "link": e.get("link"), "ts": ts, "thumb": thumb,
            "summary": _strip_html(e.get("summary", ""))[:280],
        })
    return items


def _strip_html(s):
    import re
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _yahoo_news(symbol, source_label="Yahoo Finance"):
    try:
        raw = yf.Ticker(symbol).news or []
    except Exception:
        return []
    items = []
    for n in raw:
        c = n.get("content", n)
        link = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") \
            or c.get("previewUrl")
        ts = None
        if c.get("pubDate"):
            try:
                ts = datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        thumb = None
        if c.get("thumbnail") and c["thumbnail"].get("resolutions"):
            res = sorted(c["thumbnail"]["resolutions"], key=lambda r: r.get("width", 0))
            thumb = next((r["url"] for r in res if r.get("width", 0) >= 170), res[-1]["url"])
        provider = (c.get("provider") or {}).get("displayName")
        items.append({
            "source": source_label, "provider": provider, "title": c.get("title", ""),
            "link": link, "ts": ts, "thumb": thumb, "summary": (c.get("summary") or "")[:280],
        })
    return items


def _all_news():
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_fetch_rss, s, u) for s, u in RSS_FEEDS.items()]
        futures.append(ex.submit(_yahoo_news, "^GSPC"))
        items = [i for f in futures for i in f.result()]
    seen, out = set(), []
    for i in sorted(items, key=lambda x: x["ts"] or 0, reverse=True):
        if i["title"] and i["title"] not in seen:
            seen.add(i["title"])
            out.append(i)
    return out


@app.get("/api/news")
def news():
    return jsonify(cached("news", 300, _all_news))


@app.get("/api/news/<symbol>")
def ticker_news(symbol):
    symbol = symbol.upper()
    return jsonify(cached(f"news:{symbol}", 300, lambda: _yahoo_news(symbol)))


# ---------------------------------------------------------------- markets strip
def _markets():
    syms = [s for s, _ in MARKET_STRIP]
    daily = yf.download(syms, period="5d", interval="1d", progress=False, auto_adjust=False)["Close"]
    intra = yf.download(syms, period="1d", interval="15m", progress=False, auto_adjust=False)["Close"]
    out = []
    for sym, name in MARKET_STRIP:
        d = daily[sym].dropna()
        if len(d) < 2:
            continue
        last, prev = float(d.iloc[-1]), float(d.iloc[-2])
        spark = [round(float(x), 4) for x in intra[sym].dropna().tolist()] if sym in intra else []
        out.append({"symbol": sym, "name": name, "price": last, "change": last - prev,
                    "changePct": (last - prev) / prev * 100, "spark": spark})
    return out


@app.get("/api/markets")
def markets():
    return jsonify(cached("markets", 60, _markets))


def _movers():
    out = {}
    for key in ("day_gainers", "day_losers", "most_actives"):
        try:
            quotes = yf.screen(key, count=8)["quotes"]
        except Exception:
            quotes = []
        out[key] = [{
            "symbol": q.get("symbol"), "name": q.get("shortName") or q.get("longName"),
            "price": clean(q.get("regularMarketPrice")),
            "changePct": clean(q.get("regularMarketChangePercent")),
            "volume": q.get("regularMarketVolume"),
        } for q in quotes]
    return out


@app.get("/api/movers")
def movers():
    return jsonify(cached("movers", 120, _movers))


# ---------------------------------------------------------------- lookup
@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    def run():
        try:
            res = yf.Search(q, max_results=8, news_count=0).quotes
        except Exception:
            return []
        return [{"symbol": r.get("symbol"), "name": r.get("shortname") or r.get("longname"),
                 "type": r.get("typeDisp") or r.get("quoteType"), "exchange": r.get("exchDisp")}
                for r in res if r.get("symbol")]
    return jsonify(cached(f"search:{q.lower()}", 600, run))


QUOTE_FIELDS = [
    "longName", "shortName", "symbol", "currency", "exchange", "fullExchangeName", "quoteType",
    "currentPrice", "regularMarketPrice", "previousClose", "regularMarketPreviousClose", "open",
    "dayLow", "dayHigh", "fiftyTwoWeekLow", "fiftyTwoWeekHigh", "volume", "averageVolume",
    "marketCap", "beta", "trailingPE", "forwardPE", "trailingEps", "forwardEps", "pegRatio",
    "priceToBook", "dividendYield", "dividendRate", "exDividendDate", "earningsTimestamp",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice", "recommendationKey",
    "numberOfAnalystOpinions", "profitMargins", "operatingMargins", "returnOnEquity",
    "revenueGrowth", "earningsGrowth", "totalRevenue", "grossMargins", "ebitda", "totalCash",
    "totalDebt", "freeCashflow", "enterpriseValue", "enterpriseToEbitda", "sharesOutstanding",
    "sector", "industry", "website", "fullTimeEmployees", "city", "state", "country",
    "longBusinessSummary", "preMarketPrice", "postMarketPrice", "marketState",
]


def _quote(symbol):
    t = yf.Ticker(symbol)
    info = t.info or {}
    if not info or (info.get("regularMarketPrice") is None and info.get("currentPrice") is None
                    and info.get("previousClose") is None):
        return None
    q = {k: clean(info.get(k)) for k in QUOTE_FIELDS}
    try:  # fast_info is fresher for live price
        fi = t.fast_info
        q["price"] = clean(float(fi.last_price))
        q["prevClose"] = clean(float(fi.previous_close))
    except Exception:
        q["price"] = q.get("currentPrice") or q.get("regularMarketPrice")
        q["prevClose"] = q.get("regularMarketPreviousClose") or q.get("previousClose")
    if q["price"] is not None and q["prevClose"]:
        q["change"] = q["price"] - q["prevClose"]
        q["changePct"] = q["change"] / q["prevClose"] * 100
    return q


@app.get("/api/quote/<symbol>")
def quote(symbol):
    symbol = symbol.upper()
    q = cached(f"quote:{symbol}", 30, lambda: _quote(symbol))
    if q is None:
        _cache.pop(f"quote:{symbol}", None)
        return jsonify({"error": f"No data found for '{symbol}'"}), 404
    return jsonify(q)


def _history(symbol, rng):
    period, interval = RANGES[rng]
    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
    df = df.dropna(subset=["Close"])
    if rng == "1d" and df.empty:  # market closed today/weekend → show last session
        df = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=False).dropna(subset=["Close"])
        if not df.empty:
            last_day = df.index[-1].date()
            df = df[df.index.date == last_day]
    pts = [{
        "t": int(idx.timestamp() * 1000),
        "o": clean(round(float(r.Open), 4)), "h": clean(round(float(r.High), 4)),
        "l": clean(round(float(r.Low), 4)), "c": clean(round(float(r.Close), 4)),
        "v": int(r.Volume) if not math.isnan(r.Volume) else 0,
    } for idx, r in df.iterrows()]
    base = None
    if rng == "1d":
        daily = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
        if pts and len(daily) >= 2:
            # previous close relative to the session being shown
            session = datetime.fromtimestamp(pts[0]["t"] / 1000, tz=timezone.utc).date()
            prior = daily[daily.index.date < session]
            base = float(prior["Close"].iloc[-1]) if len(prior) else None
    if base is None and pts:
        base = pts[0]["o"] or pts[0]["c"]
    return {"symbol": symbol, "range": rng, "interval": interval, "base": base, "points": pts}


@app.get("/api/history/<symbol>")
def history(symbol):
    symbol = symbol.upper()
    rng = request.args.get("range", "1d").lower()
    if rng not in RANGES:
        return jsonify({"error": "bad range"}), 400
    ttl = 60 if rng in ("1d", "5d") else 600
    return jsonify(cached(f"hist:{symbol}:{rng}", ttl, lambda: _history(symbol, rng)))


def _performance(symbol):
    """% change over each standard range (cached)."""
    def run():
        df = yf.Ticker(symbol).history(period="5y", interval="1d", auto_adjust=False).dropna(subset=["Close"])
        if df.empty:
            return {}
        last = float(df["Close"].iloc[-1])
        now = df.index[-1]
        out = {}
        offsets = {"5d": 5, "1mo": 21, "3mo": 63, "1y": 252, "5y": None}
        for k, n in offsets.items():
            if n is None:
                ref = float(df["Close"].iloc[0])
            elif len(df) > n:
                ref = float(df["Close"].iloc[-1 - n])
            else:
                continue
            out[k] = (last - ref) / ref * 100
        ytd = df[df.index.year == now.year]
        prior = df[df.index.year < now.year]
        if len(prior):
            ref = float(prior["Close"].iloc[-1])
            out["ytd"] = (last - ref) / ref * 100
        elif len(ytd):
            out["ytd"] = (last - float(ytd["Close"].iloc[0])) / float(ytd["Close"].iloc[0]) * 100
        return out
    return cached(f"perf:{symbol}", 600, run)


@app.get("/api/performance/<symbol>")
def performance(symbol):
    return jsonify(_performance(symbol.upper()))


# ---------------------------------------------------------------- AI chat
from chat import register_chat  # noqa: E402

register_chat(app, sources={
    "quote": lambda s: cached(f"quote:{s}", 30, lambda: _quote(s)),
    "performance": _performance,
    "history": lambda s, r: cached(f"hist:{s}:{r}", 60 if r in ("1d", "5d") else 600, lambda: _history(s, r)),
    "news": lambda s: cached(f"news:{s}", 300, lambda: _yahoo_news(s)),
    "markets": lambda: cached("markets", 60, _markets),
})


# ---------------------------------------------------------------- static
@app.get("/")
def index():
    return send_from_directory("static", "index.html")


def _warm():
    """Prefetch home-page data so the first page load is fast."""
    for key, ttl, fn in (("markets", 60, _markets), ("news", 300, _all_news), ("movers", 120, _movers)):
        try:
            cached(key, ttl, fn)
        except Exception:
            pass


if __name__ == "__main__":
    import threading
    threading.Thread(target=_warm, daemon=True).start()
    print("\n  Equity Research running at  http://127.0.0.1:5050\n")
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
