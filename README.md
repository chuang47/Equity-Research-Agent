# Equity Research

A local Yahoo Finance–style dashboard.

**Start:** `./run.sh`, then open http://127.0.0.1:5050 (run.sh opens it for you). Stop it with Ctrl+C.

## What's in it
- **Market strip**: S&P 500, Nasdaq, Dow, Russell 2000, VIX, 10Y yield, crude, gold, Bitcoin, each with an intraday sparkline. Refreshes every 60s.
- **Trending news**: Bloomberg, CNBC, MarketWatch (RSS) plus Yahoo Finance, merged newest-first and filterable by source.
- **Market movers**: top gainers, losers and most active (Yahoo screeners).
- **Ticker lookup**: search by symbol or company name (press `/` to focus the search box), then see:
  - a price chart for 1D / 5D / 1M / 3M / YTD / 1Y / 5Y, with a crosshair tooltip and volume bars
  - returns over 5D, 1M, 3M, YTD, 1Y and 5Y
  - key statistics: valuation, margins, balance sheet, dividends, next earnings date
  - 52-week range, the analyst price target and rating, the company profile, and news for that ticker
  - a Watchlist and a Recently-viewed list, saved in your browser

## AI research assistant
Click **Ask AI** (bottom right, or the button next to a ticker's name) to open the chat. It knows which ticker you're viewing and gets that stock's live quote, stats, returns, last 30 daily closes, today's price action and recent headlines, plus the major indices. It can also search the web for recent news and pull data for other tickers to compare.

Example questions: "recent developments for AAPL", "why is it down today?", "bull vs bear case", "how does its valuation compare to MSFT and GOOGL?"

**Setup:** copy `.env.example` to `.env`, paste your key from https://console.anthropic.com/settings/keys, then restart the app. It uses Claude Opus 5.5 (`MODEL` in `chat.py`). Each question costs a few cents in API usage; questions that trigger web searches cost a bit more.

## Data sources
- Prices and fundamentals: [`yfinance`](https://github.com/ranaroussi/yfinance) (unofficial Yahoo Finance API; free, no key needed). Quotes can be delayed by about 15 minutes.
- News: the public RSS feeds listed in `RSS_FEEDS` in `app.py`. Add or remove feeds there.

## Files
- `app.py`: Flask backend; JSON endpoints under `/api/*`, with an in-memory cache
- `chat.py`: AI assistant (Claude API; streams answers, web search plus a stock-data tool)
- `static/index.html`: the whole frontend (Chart.js)
