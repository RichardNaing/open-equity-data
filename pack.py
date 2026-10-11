"""Assemble a complete, page-ready research pack from SEC EDGAR, Yahoo (yfinance) and FRED.

Money fields in `annual`, `balance` and `estimates` are USD millions; per-share fields are USD and
restated for later stock splits so they line up with today's share count and split-adjusted prices.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

import fred
import sec
import yahoo

M = 1e6


def _mm(v: float | None) -> float | None:
    return round(v / M, 2) if v is not None else None


def _r(v: float | None, n: int = 2) -> float | None:
    return round(v, n) if v is not None else None


def _ok(x: Any) -> bool:
    return x is not None and not isinstance(x, BaseException)


async def _finnhub_news(sym: str, limit: int) -> list[dict]:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    to = date.today()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://finnhub.io/api/v1/company-news",
                        params={"symbol": sym, "from": (to - timedelta(days=30)).isoformat(), "to": to.isoformat(), "token": key})
        r.raise_for_status()
        items = r.json() or []
    return [{"date": datetime.fromtimestamp(i["datetime"], timezone.utc).date().isoformat(), "title": i.get("headline"),
             "source": i.get("source"), "url": i.get("url"), "summary": (i.get("summary") or "")[:400]}
            for i in items[:limit] if i.get("headline")]


def _ceo(info: dict) -> str | None:
    for o in info.get("companyOfficers") or []:
        if "CEO" in (o.get("title") or "") or "Chief Executive" in (o.get("title") or ""):
            return o.get("name")
    return None


def _price_at(hist: list[dict], end: str) -> float | None:
    p = None
    for row in hist:
        if row["d"] <= end:
            p = row["close"]
        else:
            break
    return p


def _fy_label(end: str) -> str:
    return "FY" + end[:4]


async def research_pack(symbol: str, include_macro: bool = True, news_limit: int = 8) -> dict:
    sym = sec.norm_symbol(symbol)
    warnings: list[str] = []
    sources: dict[str, str] = {}

    q, info, cik, splits, street, hist2y, histmax, news = await asyncio.gather(
        yahoo.quote(sym), yahoo.info(sym), sec.cik_for(sym), yahoo.splits(sym), yahoo.street(sym),
        yahoo.history(sym, "2y", "1wk"), yahoo.history(sym, "max", "1mo"), yahoo.news(sym, news_limit),
        return_exceptions=True)

    if not _ok(q) or not (q or {}).get("price"):
        if not _ok(cik) or cik is None:
            why = f"quote: {q!r}"[:300] + f" | sec: {cik!r}"[:300]
            return {"symbol": sym, "error": "unknown_symbol", "message": f"No quote or SEC registration found for {sym}. {why}"}
        warnings.append(f"Yahoo quote unavailable ({str(q)[:160]}); price fields are empty.")
        q = {}
    else:
        sources["quote"] = "Yahoo Finance (yfinance)"
    if not _ok(cik):
        warnings.append(f"SEC ticker lookup failed: {str(cik)[:160]}")
        cik = None
    info = info if _ok(info) else {}
    splits = splits if _ok(splits) else []
    if not _ok(splits):
        warnings.append("Split history unavailable; older per-share figures are as reported.")
    street = street if _ok(street) else {}
    hist2y = hist2y if _ok(hist2y) else []
    histmax = histmax if _ok(histmax) else []

    # ---------------- financials: SEC first, Yahoo fallback
    parsed: dict = {"annual": [], "balance": {}, "balanceHistory": {}}
    facts = subs = None
    if _ok(cik) and cik:
        facts, subs = await asyncio.gather(sec.company_facts(cik), sec.submissions(cik), return_exceptions=True)
        if _ok(facts):
            parsed = sec.parse_annual(facts, years=11)
            if parsed["annual"]:
                sources["financials"] = "SEC EDGAR XBRL (10-K)"
        else:
            warnings.append(f"SEC company facts unavailable: {str(facts)[:120]}")
    if not parsed["annual"]:
        try:
            st = await yahoo.statements(sym)
            parsed = {"annual": st["annual"], "balance": st["balance"], "balanceHistory": {st["balance"].get("end"): st["balance"]} if st["balance"] else {}}
            if parsed["annual"]:
                sources["financials"] = "Yahoo Finance statements (yfinance)"
        except Exception as e:  # noqa: BLE001
            warnings.append(f"No financial statements available: {str(e)[:120]}")

    annual_rows = parsed["annual"]
    for r in annual_rows:
        f = yahoo.split_factor(splits, r["end"])
        r["splitFactor"] = f
        r["eps"] = r["epsRaw"] / f if r.get("epsRaw") is not None else None

    # ---------------- valuation history (fiscal year-end multiples)
    vh = []
    for r in annual_rows[-10:]:
        price = _price_at(histmax, r["end"])
        eps = r.get("eps")
        bal = parsed["balanceHistory"].get(r["end"], {})
        shares = r["netIncome"] / eps if r.get("netIncome") and eps else None
        mcap = price * shares if price and shares and shares > 0 else None
        ev = mcap + (bal.get("debt") or 0) - (bal.get("cash") or 0) if mcap else None
        vh.append({"year": r["end"][:4], "fyEnd": r["end"], "price": _r(price),
                   "pe": _r(price / eps, 1) if price and eps and eps > 0 else None,
                   "evEbitda": _r(ev / r["ebitda"], 1) if ev and r.get("ebitda") and r["ebitda"] > 0 else None,
                   "evSales": _r(ev / r["revenue"], 2) if ev and r.get("revenue") else None,
                   "fcfYield": _r(r["fcf"] / mcap * 100, 2) if mcap and r.get("fcf") is not None else None})

    # ---------------- ratios on the latest fiscal year
    L = annual_rows[-1] if annual_rows else {}
    bal = parsed["balance"] or {}
    debt, cash, eq = bal.get("debt"), bal.get("cash"), bal.get("equity")
    tax_rate = 0.21
    if L.get("tax") is not None and L.get("pretax"):
        tr = L["tax"] / L["pretax"]
        if 0 <= tr <= 0.4:
            tax_rate = tr
    invested = (debt or 0) + (eq or 0) - (cash or 0)
    ratios = {
        "roe": _r(L["netIncome"] / eq * 100, 1) if L.get("netIncome") and eq and eq > 0 else None,
        "roic": _r(L["opIncome"] * (1 - tax_rate) / invested * 100, 1) if L.get("opIncome") and invested > 0 else None,
        "debtToEquity": _r(debt / eq, 2) if debt is not None and eq and eq > 0 else None,
        "currentRatio": _r(bal["currentAssets"] / bal["currentLiabilities"], 2) if bal.get("currentAssets") and bal.get("currentLiabilities") else None,
        "interestCoverage": _r(L["opIncome"] / L["interestExpense"], 1) if L.get("opIncome") and L.get("interestExpense") else None,
        "netDebtToEbitda": _r(((debt or 0) - (cash or 0)) / L["ebitda"], 2) if L.get("ebitda") and L["ebitda"] > 0 else None,
        "effectiveTaxRate": _r(tax_rate * 100, 1),
    }

    # ---------------- consensus estimates
    last_fy = int(L["end"][:4]) if L.get("end") else date.today().year - 1
    nfy = info.get("nextFiscalYearEnd")
    y0 = datetime.fromtimestamp(int(nfy), timezone.utc).year if isinstance(nfy, (int, float)) else last_fy + 1
    estimates = []
    for per, off in (("0y", 0), ("+1y", 1)):
        e = (street.get("estimates") or {}).get(per)
        fy = y0 + off
        if not e or fy <= last_fy:
            continue
        estimates.append({"fy": f"FY{fy}E", "period": per, "revenue": _mm(e.get("revenue")), "eps": _r(e.get("eps")),
                          "epsLow": _r(e.get("epsLow")), "epsHigh": _r(e.get("epsHigh")), "analysts": e.get("epsAnalysts"),
                          "revenueGrowthPct": _r(e["revenueGrowth"] * 100, 1) if e.get("revenueGrowth") is not None else None,
                          "epsGrowthPct": _r(e["epsGrowth"] * 100, 1) if e.get("epsGrowth") is not None else None})
    if estimates:
        sources["estimates"] = "Yahoo Finance consensus (yfinance)"
    if street.get("targets"):
        sources["targets"] = "Yahoo Finance analyst targets (yfinance)"

    # ---------------- profile
    sprof = sec.profile_from_subs(subs) if _ok(subs) and subs else {}
    price = q.get("price")
    shares_out = q.get("sharesOut") or (sec.shares_outstanding(facts) if _ok(facts) and facts else None)
    if not shares_out and q.get("marketCap") and price:
        shares_out = q["marketCap"] / price
    div_rate = yahoo._num(info.get("dividendRate"))
    profile = {
        "name": info.get("longName") or info.get("shortName") or sprof.get("name") or sym,
        "exchange": info.get("fullExchangeName") or info.get("exchange") or sprof.get("exchange") or "",
        "sector": info.get("sector") or "", "industry": info.get("industry") or sprof.get("sic") or "",
        "hq": ", ".join(x for x in (info.get("city"), info.get("state") or info.get("country")) if x) or sprof.get("hq") or "",
        "employees": info.get("fullTimeEmployees"), "ceo": _ceo(info), "website": (info.get("website") or "").replace("https://", "").replace("http://", ""),
        "description": (info.get("longBusinessSummary") or "")[:900], "fiscalYearEnd": sprof.get("fiscalYearEnd"),
        "currency": info.get("financialCurrency") or q.get("currency") or "USD", "cik": cik if _ok(cik) else None,
    }

    # ---------------- news & filings
    news_list = news if _ok(news) else []
    if not news_list:
        try:
            news_list = await _finnhub_news(sym, news_limit)
            if news_list:
                sources["news"] = "Finnhub"
        except Exception as e:  # noqa: BLE001
            warnings.append(f"News unavailable: {str(e)[:100]}")
    elif news_list:
        sources["news"] = "Yahoo Finance news (yfinance)"
    filings = sec.parse_filings(subs) if _ok(subs) and subs else []
    if filings:
        sources["filings"] = "SEC EDGAR submissions"

    macro = []
    if include_macro:
        try:
            macro = await fred.macro()
            if macro:
                sources["macro"] = "FRED, Federal Reserve Bank of St. Louis"
        except Exception as e:  # noqa: BLE001
            warnings.append(f"FRED unavailable: {str(e)[:100]}")

    return {
        "symbol": sym, "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": sources, "warnings": warnings,
        "profile": profile,
        "quote": {**{k: _r(v, 4) if isinstance(v, float) else v for k, v in q.items()},
                  "sharesOut": shares_out, "beta": yahoo._num(info.get("beta")),
                  "dividendYieldPct": _r(div_rate / price * 100, 2) if div_rate and price else None,
                  "forwardPE": _r(yahoo._num(info.get("forwardPE")), 1), "trailingPE": _r(yahoo._num(info.get("trailingPE")), 1),
                  "evToEbitda": _r(yahoo._num(info.get("enterpriseToEbitda")), 1)},
        "annual": [{"fy": _fy_label(r["end"]), "end": r["end"], **{k: _mm(r.get(k)) for k in
                    ("revenue", "grossProfit", "ebitda", "opIncome", "netIncome", "fcf", "ocf", "capex", "sbc", "rnd", "buybacks", "dividends", "acquisitions", "dna")},
                    "eps": _r(r.get("eps")), "epsAsReported": _r(r.get("epsRaw")), "splitFactor": r.get("splitFactor")}
                   for r in annual_rows[-6:]],
        "balance": {"asOf": bal.get("end"), **{k: _mm(bal.get(k)) for k in ("cash", "debt", "equity", "totalAssets", "currentAssets", "currentLiabilities")}},
        "ratios": ratios, "valuationHistory": vh, "estimates": estimates,
        "targets": street.get("targets") or {}, "recommendations": street.get("recommendations") or {},
        "nextEarnings": street.get("nextEarnings"),
        "priceHistory": hist2y, "news": news_list, "filings": filings, "macro": macro,
    }
