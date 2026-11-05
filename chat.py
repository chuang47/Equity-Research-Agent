"""AI research assistant: streams Claude answers grounded in the app's live market data.

Claude gets:
  * a context block describing the ticker the user is viewing (quote, returns, recent
    price action, headlines) - rebuilt fresh for each new question;
  * web search (Anthropic server tool) for recent developments and "why is it moving";
  * a get_stock_data tool to pull the same data for any other ticker (comparisons).

The API key is read from the ANTHROPIC_API_KEY env var or a local `.env` file.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import anthropic
from flask import Response, request, stream_with_context

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 6


def _load_dotenv():
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

SYSTEM = """You are an equity research assistant built into a personal stock-research dashboard.

Each user message may begin with a <market_context> block. It holds live data the dashboard \
just pulled (quote, valuation, returns, recent price action, headlines) for the ticker the user \
is viewing. Treat it as current and authoritative for prices and stats, and quote its numbers \
rather than numbers from memory. The user cannot see this block; refer to it as "the data on \
your screen" or just use the figures.

Tools:
- web_search: use it for anything time-sensitive the context does not settle - recent \
developments, why a stock is moving, earnings results, guidance, analyst actions, product \
news, macro events. Prefer reputable financial sources and recent dates.
- get_stock_data: live quote, valuation and returns for any other ticker. Use it for \
comparisons with peers or the broader market.

How to answer:
- Lead with the direct answer, then the supporting evidence. Be specific: dates, figures, \
percentages, named catalysts.
- When explaining a move, separate company-specific drivers from sector or market-wide \
moves (compare against the index data in the context), and say plainly when a move has no \
clear catalyst.
- Keep answers tight: short paragraphs or bullets, Markdown headings only for longer analyses.
- Distinguish facts from interpretation. Do not give personalized buy/sell instructions; \
you can lay out bull and bear cases, valuation, and risks."""

TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {
        "name": "get_stock_data",
        "description": "Get live quote, key valuation stats, and trailing returns "
                       "(5D/1M/3M/YTD/1Y/5Y) for a ticker symbol such as MSFT, ^GSPC or BTC-USD.",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker symbol"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
]

QUOTE_KEYS = [
    "longName", "symbol", "currency", "fullExchangeName", "sector", "industry", "marketState",
    "price", "prevClose", "change", "changePct", "open", "dayLow", "dayHigh",
    "fiftyTwoWeekLow", "fiftyTwoWeekHigh", "volume", "averageVolume", "marketCap",
    "trailingPE", "forwardPE", "trailingEps", "forwardEps", "pegRatio", "priceToBook",
    "enterpriseToEbitda", "beta", "dividendYield", "totalRevenue", "revenueGrowth",
    "earningsGrowth", "grossMargins", "operatingMargins", "profitMargins", "returnOnEquity",
    "freeCashflow", "totalCash", "totalDebt", "targetMeanPrice", "targetHighPrice",
    "targetLowPrice", "recommendationKey", "numberOfAnalystOpinions", "earningsTimestamp",
    "postMarketPrice", "preMarketPrice",
]


def _r(v):
    return round(v, 4) if isinstance(v, float) else v


def register_chat(app, sources):
    def stock_snapshot(symbol):
        symbol = symbol.strip().upper()
        q = sources["quote"](symbol)
        if not q:
            return {"error": f"No data found for {symbol}"}
        snap = {k: _r(q.get(k)) for k in QUOTE_KEYS if q.get(k) is not None}
        if snap.get("earningsTimestamp"):
            ts = datetime.fromtimestamp(snap.pop("earningsTimestamp"))
            snap["nextEarnings" if ts > datetime.now() else "mostRecentEarnings"] = ts.strftime("%Y-%m-%d")
        try:
            snap["returnsPct"] = {k: round(v, 2) for k, v in sources["performance"](symbol).items()}
        except Exception:
            pass
        return snap

    def build_context(symbol):
        now = datetime.now().astimezone()
        parts = [f"Current date/time: {now:%A, %B %d, %Y %I:%M %p %Z}"]
        try:
            parts.append("Market indices today: " + "; ".join(
                f"{m['name']} {m['price']:,.2f} ({m['changePct']:+.2f}%)" for m in sources["markets"]()))
        except Exception:
            pass
        if symbol:
            try:
                snap = stock_snapshot(symbol)
                parts.append(f"Ticker on screen: {symbol}\n" + json.dumps(snap, default=str))
            except Exception as e:
                parts.append(f"Ticker on screen: {symbol} (data unavailable: {e})")
            try:  # recent daily closes for price-action questions
                pts = sources["history"](symbol, "3mo")["points"][-30:]
                parts.append("Last 30 daily closes (date: close, volume): " + ", ".join(
                    f"{datetime.fromtimestamp(p['t'] / 1000):%m-%d}: {p['c']:.2f} ({p['v'] / 1e6:.1f}M)"
                    for p in pts))
            except Exception:
                pass
            try:
                h = sources["history"](symbol, "1d")
                pts = h["points"]
                if pts:
                    hi = max(p["h"] or p["c"] for p in pts)
                    lo = min(p["l"] or p["c"] for p in pts)
                    parts.append(f"Latest session intraday: open {pts[0]['o']}, high {hi}, low {lo}, "
                                 f"last {pts[-1]['c']}, prior close {h['base']}")
            except Exception:
                pass
            try:
                news = sources["news"](symbol)[:10]
                parts.append("Recent headlines:\n" + "\n".join(
                    f"- {datetime.fromtimestamp(n['ts']):%Y-%m-%d} | {n.get('provider') or n['source']} | "
                    f"{n['title']}" + (f" - {n['summary'][:200]}" if n.get("summary") else "")
                    for n in news if n.get("ts")))
            except Exception:
                pass
        return "<market_context>\n" + "\n\n".join(parts) + "\n</market_context>"

    def run_tool(name, args):
        if name == "get_stock_data":
            if not (isinstance(args, dict) and isinstance(args.get("symbol"), str) and args["symbol"].strip()):
                return json.dumps({"error": "INVALID_INPUT: expected {\"symbol\": string}"}), True
            try:
                return json.dumps(stock_snapshot(args["symbol"]), default=str), False
            except Exception as e:
                return json.dumps({"error": str(e)}), True
        return json.dumps({"error": f"unknown tool {name}"}), True

    @app.post("/api/chat")
    def chat():
        body = request.get_json(force=True) or {}
        history = body.get("messages") or []
        symbol = (body.get("symbol") or "").strip().upper() or None

        def sse(event, data):
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        def generate():
            if not history or history[-1].get("role") != "user":
                yield sse("error", {"message": "No question received."})
                return
            try:
                client = anthropic.Anthropic()
            except Exception:
                yield sse("error", {"message": "No Anthropic API key found. Add ANTHROPIC_API_KEY=... to a "
                                               "`.env` file in the project folder and restart the app."})
                return

            # Prior turns are plain text; attach fresh context to the newest question only,
            # and hand it back so the client stores it (keeps earlier turns byte-stable).
            question = history[-1]["content"]
            user_content = f"{build_context(symbol)}\n\n{question}"
            yield sse("context", {"content": user_content})
            messages = [{"role": m["role"], "content": m["content"]} for m in history[:-1]]
            messages.append({"role": "user", "content": user_content})

            sources_seen = {}
            rounds = 0
            try:
                while True:
                    rounds += 1
                    with client.beta.messages.stream(
                        model=MODEL,
                        max_tokens=16000,
                        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                        output_config={"effort": "medium"},
                        tools=TOOLS,
                        messages=messages,
                        betas=["server-side-fallback-2026-07-01"],
                        fallbacks="default",
                    ) as stream:
                        for event in stream:
                            if event.type == "text":
                                yield sse("text", {"text": event.text})
                            elif event.type == "content_block_start":
                                kind = event.content_block.type
                                if kind == "server_tool_use":
                                    yield sse("status", {"text": "Searching the web…"})
                                elif kind == "tool_use":
                                    yield sse("status", {"text": "Pulling market data…"})
                                elif kind == "thinking":
                                    yield sse("status", {"text": "Thinking…"})
                                elif kind == "text":
                                    yield sse("status", {"text": ""})
                            elif event.type == "content_block_stop":
                                block = getattr(event, "content_block", None)
                                if block is not None and block.type == "server_tool_use":
                                    q = (block.input or {}).get("query") if isinstance(block.input, dict) else None
                                    if q:
                                        yield sse("status", {"text": f"Searching: “{q}”"})
                        final = stream.get_final_message()

                    for block in final.content:
                        if block.type == "text":
                            for c in block.citations or []:
                                url = getattr(c, "url", None)
                                if url and url not in sources_seen:
                                    sources_seen[url] = getattr(c, "title", None) or url

                    if final.stop_reason == "refusal":
                        yield sse("error", {"message": "The model declined to answer that request."})
                        break
                    if final.stop_reason == "pause_turn":
                        messages.append({"role": "assistant", "content": final.content})
                        continue
                    tool_uses = [b for b in final.content if b.type == "tool_use"]
                    if not tool_uses or final.stop_reason != "tool_use":
                        if final.stop_reason == "max_tokens":
                            yield sse("text", {"text": "\n\n*(Response cut off: length limit reached.)*"})
                        break
                    if rounds >= MAX_TOOL_ROUNDS:
                        yield sse("text", {"text": "\n\n*(Stopped after too many tool calls.)*"})
                        break
                    results = []
                    for b in tool_uses:
                        out, is_err = run_tool(b.name, b.input)
                        results.append({"type": "tool_result", "tool_use_id": b.id, "content": out,
                                        "is_error": is_err})
                    messages.append({"role": "assistant", "content": final.content})
                    messages.append({"role": "user", "content": results})
                    yield sse("text", {"text": "\n\n"})
            except anthropic.AuthenticationError:
                yield sse("error", {"message": "Your Anthropic API key was rejected. Check ANTHROPIC_API_KEY in `.env`."})
                return
            except anthropic.RateLimitError:
                yield sse("error", {"message": "Rate limited by the Anthropic API. Try again in a minute."})
                return
            except anthropic.APIStatusError as e:
                yield sse("error", {"message": f"API error ({e.status_code}): {e.message}"})
                return
            except anthropic.APIConnectionError:
                yield sse("error", {"message": "Couldn't reach the Anthropic API. Check your connection."})
                return
            except (anthropic.AnthropicError, TypeError) as e:
                msg = str(e)
                if "api_key" in msg.lower() or "auth" in msg.lower() or "credential" in msg.lower():
                    msg = ("No Anthropic API key found. Add ANTHROPIC_API_KEY=... to a `.env` file "
                           "in the project folder and restart the app.")
                yield sse("error", {"message": msg})
                return

            if sources_seen:
                yield sse("sources", {"items": [{"url": u, "title": t} for u, t in list(sources_seen.items())[:8]]})
            yield sse("done", {})

        return Response(stream_with_context(generate()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
