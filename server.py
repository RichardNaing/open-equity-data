"""Open Equity Data: an open-source MCP server for equity research.

Sources (all free): SEC EDGAR (financial statements, filings), Yahoo Finance via yfinance
(quotes, prices, consensus, targets, news), FRED (macro). Optional: FINNHUB_API_KEY for news fallback.

Run:  python server.py            -> streamable HTTP on 0.0.0.0:$PORT/mcp (default 7860)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

import fred
import pack
import sec
import yahoo

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)

mcp = FastMCP(
    "Open Equity Data",
    instructions=(
        "Free, open-source market data for equity research. Use research_pack for a full company pack "
        "(SEC financials, Yahoo quotes/consensus/news, FRED macro). Money in annual/balance/estimates is USD millions; "
        "per-share values are split-adjusted to today's share count."
    ),
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "7860")),
    stateless_http=True,
    json_response=True,
)


@mcp.tool(annotations=READ, structured_output=True)
async def research_pack(symbol: str, include_macro: bool = True, news_limit: int = 8) -> dict[str, Any]:
    """Complete research pack for one US-listed stock: profile, live quote, 6 years of SEC-reported annual
    financials (USD millions), latest balance sheet, ratios, 10 years of fiscal-year-end multiples, consensus
    EPS/revenue estimates, analyst price targets and ratings, 2 years of weekly prices, latest news headlines,
    recent SEC filings (8-K items labelled) and, optionally, FRED macro indicators."""
    return await pack.research_pack(symbol, include_macro=include_macro, news_limit=max(1, min(int(news_limit), 20)))


@mcp.tool(annotations=READ, structured_output=True)
async def quote(symbol: str) -> dict[str, Any]:
    """Latest price, daily change %, 52-week range, 50/200-day averages and market cap for one symbol."""
    sym = sec.norm_symbol(symbol)
    try:
        return {"symbol": sym, **(await yahoo.quote(sym))}
    except Exception as e:  # noqa: BLE001
        return {"symbol": sym, "error": "unavailable", "message": str(e)[:200]}


@mcp.tool(annotations=READ, structured_output=True)
async def peer_metrics(symbols: list[str]) -> dict[str, Any]:
    """Live valuation and operating metrics for up to 10 peer tickers: market cap (USD bn), forward and trailing
    P/E, EV/EBITDA, EV/Sales, revenue growth %, gross and operating margin %, FCF yield %."""
    syms = [sec.norm_symbol(s) for s in symbols][:10]
    res = await asyncio.gather(*(yahoo.peer(s) for s in syms), return_exceptions=True)
    return {"peers": [r for r in res if isinstance(r, dict)],
            "missing": [s for s, r in zip(syms, res) if not isinstance(r, dict)]}


@mcp.tool(annotations=READ, structured_output=True)
async def news(symbol: str, limit: int = 10) -> dict[str, Any]:
    """Latest news headlines for a symbol (Yahoo Finance; Finnhub fallback if configured) plus recent SEC filings."""
    sym = sec.norm_symbol(symbol)
    items: list = []
    try:
        items = await yahoo.news(sym, max(1, min(int(limit), 20)))
    except Exception:  # noqa: BLE001
        pass
    if not items:
        try:
            items = await pack._finnhub_news(sym, limit)
        except Exception:  # noqa: BLE001
            items = []
    filings: list = []
    try:
        cik = await sec.cik_for(sym)
        if cik:
            filings = sec.parse_filings(await sec.submissions(cik))
    except Exception:  # noqa: BLE001
        pass
    return {"symbol": sym, "news": items, "filings": filings}


@mcp.tool(annotations=READ, structured_output=True)
async def macro() -> dict[str, Any]:
    """Latest US macro indicators from FRED with 3- and 12-month changes, trend and equity impact."""
    return {"indicators": await fred.macro()}


@mcp.custom_route("/", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "server": "Open Equity Data", "mcp": "/mcp",
                         "tools": ["research_pack", "quote", "peer_metrics", "news", "macro"]})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
