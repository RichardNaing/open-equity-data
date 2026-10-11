"""SEC EDGAR: ticker->CIK, XBRL company facts (annual financials) and recent filings.

All endpoints are free and keyless. The SEC requires a descriptive User-Agent with a
contact address (set SEC_USER_AGENT) and allows up to 10 requests per second.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from cache import cached

UA = os.environ.get("SEC_USER_AGENT", "OpenEquityData/1.0 (set SEC_USER_AGENT to 'Name email@domain')")
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=20.0, follow_redirects=True)
    return _client


async def _get_json(url: str) -> Any:
    r = await client().get(url)
    r.raise_for_status()
    return r.json()


def norm_symbol(sym: str) -> str:
    return sym.strip().upper().replace(".", "-").replace("/", "-")


async def ticker_map() -> dict[str, dict]:
    async def load():
        raw = await _get_json("https://www.sec.gov/files/company_tickers.json")
        return {norm_symbol(v["ticker"]): {"cik": int(v["cik_str"]), "title": v["title"]} for v in raw.values()}

    return await cached("sec:tickers", 24 * 3600, load)


async def cik_for(symbol: str) -> int | None:
    m = await ticker_map()
    hit = m.get(norm_symbol(symbol))
    return hit["cik"] if hit else None


async def company_facts(cik: int) -> dict:
    return await cached(f"sec:facts:{cik}", 12 * 3600, lambda: _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))


async def submissions(cik: int) -> dict:
    return await cached(f"sec:subs:{cik}", 3600, lambda: _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))


# ---------------------------------------------------------------- XBRL parsing
ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A"}

FLOW_TAGS: dict[str, list[str]] = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense"],
    "costOfRevenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices"],
    "grossProfit": ["GrossProfit"],
    "rnd": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "opIncome": ["OperatingIncomeLoss"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "netIncome": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "interestExpense": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "dna": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization", "Depreciation"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "acquisitions": ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
}
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"]
INSTANT_TAGS: dict[str, list[str]] = {
    "cashOnly": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"],
    "stInvest": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "AvailableForSaleSecuritiesCurrent"],
    "ltDebtTotal": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"],
    "ltDebtNoncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligationsNoncurrent"],
    "ltDebtCurrent": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "stBorrow": ["ShortTermBorrowings", "CommercialPaper", "DebtCurrent"],
    "finLease": ["FinanceLeaseLiability"],
    "finLeaseNoncurrent": ["FinanceLeaseLiabilityNoncurrent"],
    "finLeaseCurrent": ["FinanceLeaseLiabilityCurrent"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "assets": ["Assets"],
    "currentAssets": ["AssetsCurrent"],
    "currentLiabilities": ["LiabilitiesCurrent"],
}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _tag_series(gaap: dict, tag: str, unit: str, instant: bool) -> dict[str, float]:
    node = gaap.get(tag)
    if not node:
        return {}
    out: dict[str, tuple[float, str]] = {}
    for row in node.get("units", {}).get(unit, []):
        if row.get("form") not in ANNUAL_FORMS:
            continue
        end = row.get("end")
        if not end:
            continue
        if instant:
            if "start" in row:
                continue
        else:
            start = row.get("start")
            if not start or not 340 <= _days(start, end) <= 380:
                continue
        filed = row.get("filed", "")
        prev = out.get(end)
        if prev is None or filed > prev[1]:  # latest filing wins (restatements)
            out[end] = (float(row["val"]), filed)
    return {k: v[0] for k, v in out.items()}


def _merged(gaap: dict, tags: list[str], unit: str = "USD", instant: bool = False) -> dict[str, float]:
    """Per period end, take the highest-priority tag that reports it."""
    merged: dict[str, float] = {}
    for tag in tags:
        for end, val in _tag_series(gaap, tag, unit, instant).items():
            merged.setdefault(end, val)
    return merged


def _near(series: dict[str, float], end: str, tol: int = 10) -> float | None:
    if end in series:
        return series[end]
    for k, v in series.items():
        if abs(_days(k, end)) <= tol:
            return v
    return None


def parse_annual(facts: dict, years: int = 10) -> dict:
    """Return annual flows (USD), EPS, and the latest balance sheet from companyfacts JSON."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    if not gaap:
        return {"annual": [], "balance": {}, "balanceHistory": {}}
    flows = {k: _merged(gaap, tags) for k, tags in FLOW_TAGS.items()}
    eps = _merged(gaap, EPS_TAGS, unit="USD/shares")
    anchor = flows["revenue"] or flows["netIncome"]
    ends = sorted(anchor)[-years:]
    rows = []
    for end in ends:
        g = lambda k: _near(flows[k], end)  # noqa: E731
        rev, cor, gp = g("revenue"), g("costOfRevenue"), g("grossProfit")
        if gp is None and rev is not None and cor is not None:
            gp = rev - cor
        op, dna, ocf, capex = g("opIncome"), g("dna"), g("ocf"), g("capex")
        rows.append({
            "end": end,
            "revenue": rev, "grossProfit": gp, "opIncome": op, "netIncome": g("netIncome"),
            "ebitda": op + dna if op is not None and dna is not None else None,
            "dna": dna, "rnd": g("rnd"), "pretax": g("pretax"), "tax": g("tax"), "interestExpense": g("interestExpense"),
            "ocf": ocf, "capex": abs(capex) if capex is not None else None,
            "fcf": ocf - abs(capex) if ocf is not None and capex is not None else None,
            "sbc": g("sbc"), "buybacks": g("buybacks"), "dividends": g("dividends"), "acquisitions": g("acquisitions"),
            "epsRaw": _near(eps, end),
        })
    inst = {k: _merged(gaap, tags, instant=True) for k, tags in INSTANT_TAGS.items()}

    def balance_at(end: str) -> dict:
        b = lambda k: _near(inst[k], end)  # noqa: E731
        cash = (b("cashOnly") or 0) + (b("stInvest") or 0) if b("cashOnly") is not None else None
        lt = b("ltDebtTotal")
        if lt is None and (b("ltDebtNoncurrent") is not None or b("ltDebtCurrent") is not None):
            lt = (b("ltDebtNoncurrent") or 0) + (b("ltDebtCurrent") or 0)
        lease = b("finLease")
        if lease is None and (b("finLeaseNoncurrent") is not None or b("finLeaseCurrent") is not None):
            lease = (b("finLeaseNoncurrent") or 0) + (b("finLeaseCurrent") or 0)
        debt = None
        if any(x is not None for x in (lt, b("stBorrow"), lease)):
            debt = (lt or 0) + (b("stBorrow") or 0) + (lease or 0)
        return {"end": end, "cash": cash, "debt": debt, "equity": b("equity"), "totalAssets": b("assets"),
                "currentAssets": b("currentAssets"), "currentLiabilities": b("currentLiabilities")}

    hist = {r["end"]: balance_at(r["end"]) for r in rows}
    latest = hist[rows[-1]["end"]] if rows else {}
    return {"annual": rows, "balance": latest, "balanceHistory": hist}


def shares_outstanding(facts: dict) -> float | None:
    node = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if not node:
        return None
    rows = node.get("units", {}).get("shares", [])
    if not rows:
        return None
    # sum share classes reported on the latest date
    latest = max(r["end"] for r in rows)
    return float(sum(r["val"] for r in rows if r["end"] == latest and r.get("form", "").startswith("10-")) or
                 max(rows, key=lambda r: r["end"])["val"])


# ---------------------------------------------------------------- filings feed
ITEM_8K = {
    "1.01": "Material agreement", "1.02": "Agreement terminated", "1.03": "Bankruptcy", "1.05": "Cybersecurity incident",
    "2.01": "Acquisition or disposition", "2.02": "Results of operations", "2.03": "New debt obligation",
    "2.05": "Restructuring / exit costs", "2.06": "Impairment", "3.01": "Listing notice", "3.02": "Unregistered equity sale",
    "4.01": "Auditor change", "4.02": "Non-reliance on financials", "5.01": "Change in control",
    "5.02": "Director / officer change", "5.03": "Bylaw / charter amendment", "5.07": "Shareholder vote",
    "7.01": "Reg FD disclosure", "8.01": "Other material event", "9.01": "Exhibits",
}
SKIP_FORMS = {"4", "3", "5", "144", "SC 13G", "SC 13G/A", "424B2", "FWP", "S-8", "11-K"}


def parse_filings(subs: dict, limit: int = 12) -> list[dict]:
    rec = subs.get("filings", {}).get("recent", {})
    cik = int(subs.get("cik", 0) or 0)
    out = []
    for i, form in enumerate(rec.get("form", [])):
        if form in SKIP_FORMS:
            continue
        accn = rec["accessionNumber"][i]
        doc = rec.get("primaryDocument", [""] * (i + 1))[i]
        items = (rec.get("items", [""] * (i + 1))[i] or "")
        labels = [ITEM_8K[x.strip()] for x in items.split(",") if x.strip() in ITEM_8K and x.strip() != "9.01"]
        desc = rec.get("primaryDocDescription", [""] * (i + 1))[i] or ""
        out.append({
            "date": rec["filingDate"][i], "form": form,
            "description": "; ".join(labels) if labels else desc,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc}" if doc else
                   f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
        })
        if len(out) >= limit:
            break
    return out


def profile_from_subs(subs: dict) -> dict:
    biz = subs.get("addresses", {}).get("business", {}) or {}
    fye = subs.get("fiscalYearEnd") or ""
    month = None
    if len(fye) == 4 and fye[:2].isdigit():
        import calendar
        month = calendar.month_name[int(fye[:2])] if 1 <= int(fye[:2]) <= 12 else None
    return {"name": subs.get("name"), "sic": subs.get("sicDescription"),
            "hq": ", ".join(x for x in (biz.get("city"), biz.get("stateOrCountryDescription") or biz.get("stateOrCountry")) if x),
            "fiscalYearEnd": month, "exchange": (subs.get("exchanges") or [None])[0]}
