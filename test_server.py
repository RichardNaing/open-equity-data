"""Offline tests: SEC XBRL parsing, split restatement, FRED parsing, full pack assembly and the real
MCP streamable-HTTP transport. Network sources are replaced with fixtures in the documented formats."""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

import cache
import fred
import pack
import sec
import yahoo


def _row(val, end, start=None, form="10-K", filed="2026-07-29", fp="FY"):
    r = {"end": end, "val": val, "form": form, "filed": filed, "fp": fp, "accn": "0000950170-26-000001"}
    if start:
        r["start"] = start
    return r


def make_facts():
    yrs = [(2021, 168088, 115856, 69916, 61271, 8.05), (2022, 198270, 135620, 83383, 72738, 9.65),
           (2023, 211915, 146052, 88523, 72361, 9.68), (2024, 245122, 171008, 109433, 88136, 11.80),
           (2025, 281724, 193893, 128528, 101832, 13.64), (2026, 331839, 225465, 155237, 133749, 17.95)]
    rev_old, rev_new, gp, op, ni, eps, dna, ocf, capex = ([] for _ in range(9))
    for y, r, g, o, n, e in yrs:
        s, en = f"{y-1}-07-01", f"{y}-06-30"
        (rev_old if y <= 2022 else rev_new).append(_row(r * 1e6, en, s, filed=f"{y}-07-30"))
        gp.append(_row(g * 1e6, en, s)); op.append(_row(o * 1e6, en, s)); ni.append(_row(n * 1e6, en, s))
        # EPS reported pre-split for 2021-2022 (2:1 split in 2023 for the test)
        eps.append(_row(e * (2 if y <= 2022 else 1), en, s))
        dna.append(_row(o * 0.25 * 1e6, en, s)); ocf.append(_row(n * 1.35 * 1e6, en, s)); capex.append(_row(n * 0.6 * 1e6, en, s))
    # noise that must be ignored: a quarter, a 10-Q, and an older (superseded) restatement
    rev_new.append(_row(80e9, "2026-03-31", "2026-01-01", form="10-Q", fp="Q3"))
    rev_new.append(_row(70e9, "2025-06-30", "2025-04-01"))
    rev_new.append(_row(1e9, "2025-06-30", "2024-07-01", filed="2025-01-01"))  # older filing, loses to restated value
    inst = lambda v, y: _row(v, f"{y}-06-30")  # noqa: E731
    facts = {"cik": 789019, "entityName": "MICROSOFT CORP", "facts": {
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [_row(7.43e9, "2026-07-20", form="10-K")]}}},
        "us-gaap": {
            "SalesRevenueNet": {"units": {"USD": rev_old}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rev_new}},
            "GrossProfit": {"units": {"USD": gp}}, "OperatingIncomeLoss": {"units": {"USD": op}},
            "NetIncomeLoss": {"units": {"USD": ni}}, "EarningsPerShareDiluted": {"units": {"USD/shares": eps}},
            "DepreciationDepletionAndAmortization": {"units": {"USD": dna}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": ocf}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": capex}},
            "IncomeTaxExpenseBenefit": {"units": {"USD": [_row(32185e6, "2026-06-30", "2025-07-01")]}},
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {"units": {"USD": [_row(165934e6, "2026-06-30", "2025-07-01")]}},
            "InterestExpense": {"units": {"USD": [_row(3051e6, "2026-06-30", "2025-07-01")]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [inst(20935e6, 2026), inst(30242e6, 2025)]}},
            "ShortTermInvestments": {"units": {"USD": [inst(55908e6, 2026), inst(40000e6, 2025)]}},
            "LongTermDebtNoncurrent": {"units": {"USD": [inst(31067e6, 2026), inst(40000e6, 2025)]}},
            "LongTermDebtCurrent": {"units": {"USD": [inst(9227e6, 2026)]}},
            "FinanceLeaseLiability": {"units": {"USD": [inst(88514e6, 2026)]}},
            "StockholdersEquity": {"units": {"USD": [inst(442387e6, 2026), inst(343000e6, 2025)]}},
            "Assets": {"units": {"USD": [inst(758376e6, 2026)]}},
            "AssetsCurrent": {"units": {"USD": [inst(207710e6, 2026)]}},
            "LiabilitiesCurrent": {"units": {"USD": [inst(168825e6, 2026)]}},
        }}}
    return facts


SUBS = {"cik": "789019", "name": "MICROSOFT CORP", "sicDescription": "Services-Prepackaged Software", "fiscalYearEnd": "0630",
        "exchanges": ["Nasdaq"], "addresses": {"business": {"city": "REDMOND", "stateOrCountry": "WA"}},
        "filings": {"recent": {"accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3"], "filingDate": ["2026-10-02", "2026-09-30", "2026-07-29"],
                               "form": ["8-K", "4", "10-K"], "primaryDocument": ["a.htm", "b.xml", "msft-10k.htm"],
                               "primaryDocDescription": ["8-K", "4", "10-K"], "items": ["5.02,9.01", "", ""]}}}


def test_parse_annual():
    p = sec.parse_annual(make_facts(), years=10)
    a = p["annual"]
    assert [r["end"][:4] for r in a] == ["2021", "2022", "2023", "2024", "2025", "2026"]
    assert a[0]["revenue"] == 168088e6           # older tag used for early years
    assert a[-2]["revenue"] == 281724e6          # restated value wins, quarterly row ignored
    assert a[-1]["fcf"] == pytest.approx(133749e6 * 1.35 - 133749e6 * 0.6)
    assert a[-1]["ebitda"] == pytest.approx(155237e6 * 1.25)
    b = p["balance"]
    assert b["cash"] == pytest.approx(76843e6)
    assert b["debt"] == pytest.approx(31067e6 + 9227e6 + 88514e6)


def test_split_factor():
    s = [("2023-02-01", 2.0)]
    assert yahoo.split_factor(s, "2022-06-30") == 2.0
    assert yahoo.split_factor(s, "2024-06-30") == 1.0


def test_filings():
    f = sec.parse_filings(SUBS)
    assert [x["form"] for x in f] == ["8-K", "10-K"]
    assert f[0]["description"] == "Director / officer change"
    assert f[1]["url"].endswith("/789019/0001261/msft-10k.htm".replace("0001261", "0001263"))


def test_fred_parse_and_summarize():
    csv_new = "observation_date,DGS10\n" + "\n".join(f"2025-{m:02d}-01,{3.5 + m*0.05:.2f}" for m in range(1, 13)) + "\n2026-01-01,.\n2026-02-02,4.30\n"
    rows = fred.parse_csv(csv_new)
    assert rows[-1] == ("2026-02-02", 4.30) and len(rows) == 13
    s = fred.summarize("DGS10", "10Y", "%", "level", "negative", rows)
    assert s["trend"] == "up" and s["impact"] == "negative"
    cpi = [(f"{2024 + (m-1)//12}-{(m-1)%12+1:02d}-01", 300 * (1.003 ** m)) for m in range(1, 25)]
    s2 = fred.summarize("CPIAUCSL", "CPI", "% y/y", "yoy", "negative", cpi)
    assert 3.4 < s2["value"] < 3.8


@pytest.fixture
def offline(monkeypatch):
    cache.clear()

    async def f_quote(sym):
        if sym == "ZZZZ":
            raise RuntimeError("404")
        return {"price": 535.07, "prevClose": 522.61, "changePct": 2.38, "high52": 553.72, "low52": 349.2, "avg50": 500.4,
                "avg200": 434.0, "marketCap": 3.973e12, "sharesOut": 7.43e9, "currency": "USD", "ts": 1791576001}

    async def f_info(sym):
        return {"longName": "Microsoft Corporation", "sector": "Technology", "industry": "Software - Infrastructure", "city": "Redmond",
                "state": "WA", "fullTimeEmployees": 223000, "website": "https://www.microsoft.com", "beta": 1.1, "dividendRate": 3.64,
                "forwardPE": 27.1, "trailingPE": 29.8, "enterpriseToEbitda": 19.4, "nextFiscalYearEnd": 1814313600,
                "companyOfficers": [{"name": "Satya Nadella", "title": "Chairman & CEO"}], "longBusinessSummary": "Microsoft..."}

    async def f_cik(sym):
        return None if sym == "ZZZZ" else 789019

    async def f_splits(sym):
        return [("2023-02-01", 2.0)]

    async def f_street(sym):
        return {"estimates": {"0y": {"eps": 21.1, "epsLow": 20.0, "epsHigh": 22.5, "epsAnalysts": 40, "revenue": 385e9, "revenueGrowth": 0.16, "epsGrowth": 0.175},
                              "+1y": {"eps": 24.9, "revenue": 445e9}},
                "targets": {"mean": 640.0, "high": 725.0, "low": 440.0, "median": 630.0, "current": 535.07},
                "recommendations": {"strongBuy": 15, "buy": 40, "hold": 5, "sell": 0, "strongSell": 0}, "nextEarnings": "2026-10-28"}

    async def f_hist(sym, period="2y", interval="1wk"):
        if interval == "1mo":
            out, p = [], 200.0
            for y in range(2016, 2027):
                for m in range(1, 13):
                    p *= 1.012
                    out.append({"d": f"{y}-{m:02d}-01", "close": round(p, 2)})
            return out
        return [{"d": f"2025-{m:02d}-01", "close": 400 + m * 10} for m in range(1, 13)]

    async def f_news(sym, limit=8):
        return [{"date": "2026-10-09", "title": "Microsoft shares rise on AI demand", "source": "Reuters", "url": "https://example.com/a", "summary": "..."}]

    async def f_facts(cik):
        return make_facts()

    async def f_subs(cik):
        return SUBS

    async def f_macro():
        return [{"id": "DGS10", "name": "10Y Treasury yield", "value": 4.1, "trend": "down", "impact": "positive"}]

    async def f_peer(sym):
        return {"ticker": sym, "name": sym + " Inc", "pe_fwd": 25.0, "ev_ebitda": 15.0, "revGrowth": 10.0, "opMargin": 30.0, "mktCap_b": 500.0}

    for mod, name, fn in [(yahoo, "quote", f_quote), (yahoo, "info", f_info), (sec, "cik_for", f_cik), (yahoo, "splits", f_splits),
                          (yahoo, "street", f_street), (yahoo, "history", f_hist), (yahoo, "news", f_news), (sec, "company_facts", f_facts),
                          (sec, "submissions", f_subs), (fred, "macro", f_macro), (yahoo, "peer", f_peer)]:
        monkeypatch.setattr(mod, name, fn)


def test_research_pack(offline):
    p = asyncio.run(pack.research_pack("msft"))
    assert p["symbol"] == "MSFT" and not p.get("error")
    a = p["annual"]
    assert a[-1]["fy"] == "FY2026" and a[-1]["revenue"] == 331839.0
    assert a[0]["eps"] == pytest.approx(8.05)            # pre-split EPS restated
    assert a[0]["epsAsReported"] == pytest.approx(16.10)
    assert [e["fy"] for e in p["estimates"]] == ["FY2027E", "FY2028E"]
    assert p["estimates"][0]["revenue"] == 385000.0
    assert p["ratios"]["roe"] == pytest.approx(30.2, abs=0.1)
    assert p["balance"]["debt"] == pytest.approx(128808.0, abs=1)
    vh = p["valuationHistory"]
    assert len(vh) == 6 and all(v["pe"] and v["pe"] > 0 for v in vh)
    assert p["filings"][0]["form"] == "8-K" and p["news"] and p["macro"]
    assert p["quote"]["dividendYieldPct"] == pytest.approx(0.68, abs=0.01)
    assert set(p["sources"]) >= {"quote", "financials", "estimates", "news", "filings", "macro"}


def test_unknown(offline):
    assert asyncio.run(pack.research_pack("ZZZZ"))["error"] == "unknown_symbol"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_http_transport(offline):
    """Boot the real streamable-HTTP app and call tools with the official MCP client."""
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client  # noqa

    import server
    port = _free_port()
    cfg = uvicorn.Config(server.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)

    async def go():
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name: t for t in (await s.list_tools()).tools}
                assert set(tools) == {"research_pack", "quote", "peer_metrics", "news", "macro"}
                assert tools["research_pack"].annotations.readOnlyHint is True
                res = await s.call_tool("research_pack", {"symbol": "MSFT", "include_macro": True})
                assert not res.isError and res.structuredContent["annual"][-1]["revenue"] == 331839.0
                peers = await s.call_tool("peer_metrics", {"symbols": ["GOOGL", "AMZN"]})
                assert len(peers.structuredContent["peers"]) == 2
                return True

    try:
        assert asyncio.run(go())
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def test_dump_pack_for_ui(offline, tmp_path_factory):
    """Writes the pack the UI test consumes (only when OED_DUMP is set)."""
    import json
    import os
    out = os.environ.get("OED_DUMP")
    if not out:
        pytest.skip("OED_DUMP not set")
    p = asyncio.run(pack.research_pack("MSFT"))
    peers = {"peers": [asyncio.run(yahoo.peer(s)) for s in ["GOOGL", "AMZN", "ORCL"]], "missing": []}
    with open(out, "w") as f:
        json.dump({"pack": p, "peers": peers}, f, default=str)
