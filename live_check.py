"""Live end-to-end check: boots the real MCP server, calls it over streamable HTTP with the official
client, and validates real SEC EDGAR, Yahoo Finance and FRED responses.

Usage: python live_check.py MSFT NVDA JPM
Writes live_check.json and a Markdown summary (to $GITHUB_STEP_SUMMARY when set). Exits 1 on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import server


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def check_pack(p: dict) -> list[str]:
    errs = []
    if p.get("error"):
        return [f"error: {p.get('message')}"]
    q = p.get("quote") or {}
    if not q.get("price"):
        errs.append("no live price")
    a = p.get("annual") or []
    if len(a) < 3:
        errs.append(f"only {len(a)} annual rows")
    elif not a[-1].get("revenue"):
        errs.append("latest revenue missing")
    if not p.get("valuationHistory"):
        errs.append("no valuation history")
    if not p.get("priceHistory"):
        errs.append("no price history")
    if not p.get("estimates"):
        errs.append("no consensus estimates")
    if not p.get("filings"):
        errs.append("no SEC filings")
    if len(p.get("macro") or []) < 5:
        errs.append(f"macro indicators: {len(p.get('macro') or [])}")
    return errs


def summarize(p: dict) -> str:
    q, a = p.get("quote") or {}, (p.get("annual") or [{}])
    L = a[-1] if a else {}
    est = ", ".join(f"{e['fy']} EPS {e.get('eps')}" for e in p.get("estimates") or [])
    return (f"price {q.get('price')} ({q.get('changePct') and round(q['changePct'], 2)}%), {L.get('fy')} revenue ${L.get('revenue')}M, "
            f"EPS {L.get('eps')}, FCF ${L.get('fcf')}M | estimates: {est or 'none'} | news {len(p.get('news') or [])}, "
            f"filings {len(p.get('filings') or [])}, macro {len(p.get('macro') or [])} | sources: {', '.join(sorted((p.get('sources') or {}).keys()))}")


async def run(symbols: list[str], port: int) -> tuple[dict, bool]:
    results, ok = {}, True
    async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            results["_tools"] = tools
            for i, sym in enumerate(symbols):
                t0 = time.time()
                res = await s.call_tool("research_pack", {"symbol": sym, "include_macro": i == 0 or True})
                p = res.structuredContent or {}
                errs = check_pack(p) if not res.isError else ["tool error"]
                results[sym] = {"seconds": round(time.time() - t0, 1), "errors": errs, "summary": summarize(p),
                                "warnings": p.get("warnings"), "sources": p.get("sources"),
                                "sample": {"annual": p.get("annual", [])[-2:], "estimates": p.get("estimates"),
                                           "news": (p.get("news") or [])[:3], "filings": (p.get("filings") or [])[:3],
                                           "macro": (p.get("macro") or [])[:4], "ratios": p.get("ratios"),
                                           "valuationHistory": (p.get("valuationHistory") or [])[-3:]}}
                ok = ok and not errs
            peers = await s.call_tool("peer_metrics", {"symbols": ["GOOGL", "AMZN", "ORCL"]})
            pm = (peers.structuredContent or {}).get("peers") or []
            results["_peer_metrics"] = pm
            if len(pm) < 2:
                ok = False
                results["_peer_metrics_error"] = "peer metrics missing"
    return results, ok


def main() -> int:
    symbols = [s.upper() for s in sys.argv[1:]] or ["MSFT", "NVDA", "JPM"]
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(server.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        results, ok = asyncio.run(run(symbols, port))
    finally:
        srv.should_exit = True
    with open("live_check.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    lines = [f"## Open Equity Data live check: {'PASS' if ok else 'FAIL'}", "", f"Tools: {', '.join(results['_tools'])}", ""]
    for sym in symbols:
        r = results[sym]
        lines.append(f"- **{sym}** ({r['seconds']}s) {'OK' if not r['errors'] else 'ERRORS: ' + '; '.join(r['errors'])}")
        lines.append(f"  - {r['summary']}")
        if r.get("warnings"):
            lines.append(f"  - warnings: {r['warnings']}")
    lines.append(f"- **peer_metrics**: {len(results['_peer_metrics'])} peers, e.g. {results['_peer_metrics'][:1]}")
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
