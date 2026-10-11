---
title: Open Equity Data
emoji: 📈
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# Open Equity Data

An open-source MCP server that gives Claude (and the Equity Research Terminal) free market data:

| Data | Source | Key needed |
|---|---|---|
| Annual financial statements, balance sheet, ratios | **SEC EDGAR XBRL** (10-K filings) | No |
| Recent filings, with 8-K items labelled | **SEC EDGAR** submissions | No |
| Live quote, 52-week range, moving averages, price history, splits | **Yahoo Finance** via `yfinance` | No |
| Consensus EPS and revenue estimates, price targets, ratings | Yahoo Finance via `yfinance` | No |
| Company news headlines | Yahoo Finance via `yfinance` (Finnhub fallback) | Optional `FINNHUB_API_KEY` |
| Macro: rates, curve, CPI, core PCE, jobs, GDP, credit spreads, dollar, oil, VIX | **FRED** (St. Louis Fed) | No |
| Fiscal-year-end P/E, EV/EBITDA, EV/Sales history | Computed from SEC + Yahoo prices | No |

Per-share figures are restated for later stock splits. Money fields are USD millions.

## Tools

- `research_pack(symbol, include_macro=true, news_limit=8)`: everything above in one call
- `quote(symbol)`: lightweight live quote, for refreshing prices
- `peer_metrics(symbols[])`: live multiples and margins for up to 10 peers
- `news(symbol, limit)`: headlines plus recent SEC filings
- `macro()`: FRED indicator board

## Deploy free on Hugging Face Spaces (about 5 minutes)

1. Sign in at huggingface.co and open **New Space**.
2. Name it `open-equity-data`, choose SDK **Docker** with the **Blank** template and **Public** visibility, then create it.
   The Space must be public so Claude's servers can reach it.
3. Choose **Files → Upload files** and drop in everything from this folder, then commit.
4. Open **Settings → Variables and secrets** and add the variable `SEC_USER_AGENT` with your name and email, e.g. `Jane Doe jane@example.com`.
   The SEC requires a contact address.
   Optionally add the secret `FINNHUB_API_KEY` (a free key from finnhub.io) as a news backup.
5. Wait until the Space shows **Running**. Opening `https://<your-username>-open-equity-data.hf.space/` should show `{"ok": true, ...}`.

## Connect it to Claude

1. In claude.ai, open **Settings → Connectors → Add custom connector**.
2. Name it exactly **Open Equity Data**. The terminal looks for this name.
3. Set the URL to `https://<your-username>-open-equity-data.hf.space/mcp`, then save.
4. Open the Equity Research Terminal, build a ticker, and choose **Allow** when asked about Open Equity Data.

## Other hosts

- **Google Cloud Run:** `gcloud run deploy open-equity-data --source . --allow-unauthenticated --set-env-vars SEC_USER_AGENT="Name email"`. Cloud Run supplies `PORT` automatically.
- **Local:** `pip install -r requirements.txt && python server.py`, then point an MCP client at `http://localhost:7860/mcp`.

## Test

```
pip install pytest && python -m pytest -q          # offline: fixtures + real MCP HTTP round trip
python live_check.py MSFT NVDA JPM                # live: real SEC, Yahoo and FRED through the MCP server
```

GitHub Actions (`.github/workflows/live-test.yml`) runs both on every push and each weekday morning; results appear in the run summary.

## Notes and limits

- `yfinance` is unofficial. Yahoo can rate-limit shared cloud IPs. Responses are cached (quotes 20 s, statements 12 h, macro 1 h) to stay well under limits.
- SEC financials cover US filers (10-K). Foreign filers fall back to Yahoo statements.
- Free consensus data is thinner than paid feeds: estimates cover the current and next fiscal year.
- Data is for research and education. It is not investment advice.
