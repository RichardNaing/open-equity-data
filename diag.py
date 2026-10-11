"""Print raw responses/errors from each live source so failures are visible in CI logs."""
import asyncio
import os
import traceback

import httpx
import yfinance as yf

UA = os.environ.get("SEC_USER_AGENT", "")


async def sec_probe():
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}, timeout=20) as c:
        for u in ["https://www.sec.gov/files/company_tickers.json", "https://data.sec.gov/submissions/CIK0000789019.json",
                  "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"]:
            try:
                r = await c.get(u)
                print("SEC", r.status_code, u, r.headers.get("content-type"), r.text[:160].replace("\n", " "))
            except Exception as e:  # noqa: BLE001
                print("SEC EXC", u, repr(e))
        r = await c.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": "DGS10", "cosd": "2026-09-01"})
        print("FRED", r.status_code, r.text[:120].replace("\n", " | "))


def yahoo_probe():
    t = yf.Ticker("MSFT")
    for name, fn in [("fast_info", lambda: {k: t.fast_info.get(k) for k in ("last_price", "previous_close", "year_high", "market_cap", "shares")}),
                     ("history", lambda: t.history(period="5d").tail(2).to_dict()),
                     ("info.price", lambda: {k: t.info.get(k) for k in ("currentPrice", "regularMarketPrice", "nextFiscalYearEnd")}),
                     ("earnings_estimate", lambda: t.earnings_estimate.to_dict()),
                     ("news", lambda: [n.get("content", n).get("title") for n in t.get_news(count=3)]),
                     ("splits", lambda: t.splits.tail(3).to_dict())]:
        try:
            print("YAHOO", name, str(fn())[:300])
        except Exception:  # noqa: BLE001
            print("YAHOO EXC", name, traceback.format_exc().splitlines()[-1])


print("UA:", UA)
asyncio.run(sec_probe())
yahoo_probe()
