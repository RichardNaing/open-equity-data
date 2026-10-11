"""Yahoo Finance via the open-source yfinance library: quotes, prices, splits, estimates,
targets, recommendations, news, peer metrics and a statement fallback for non-SEC filers.

yfinance is unofficial; every call here is defensive and returns partial data rather than raising.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import anyio
import yfinance as yf

from cache import cached


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


async def _run(fn, *a):
    return await anyio.to_thread.run_sync(lambda: fn(*a))


def _t(sym: str) -> yf.Ticker:
    return yf.Ticker(sym)


# ------------------------------------------------------------------ quote & info
def _quote(sym: str) -> dict:
    fi = _t(sym).fast_info
    g = lambda k: _num(fi.get(k)) if hasattr(fi, "get") else _num(getattr(fi, k, None))  # noqa: E731
    price, prev = g("last_price"), g("previous_close") or g("regular_market_previous_close")
    return {
        "price": price, "prevClose": prev,
        "changePct": (price / prev - 1) * 100 if price and prev else None,
        "high52": g("year_high"), "low52": g("year_low"),
        "avg50": g("fifty_day_average"), "avg200": g("two_hundred_day_average"),
        "marketCap": g("market_cap"), "sharesOut": g("shares"),
        "currency": fi.get("currency") if hasattr(fi, "get") else None,
        "ts": int(datetime.now(timezone.utc).timestamp()),
    }


async def quote(sym: str) -> dict:
    return await cached(f"y:q:{sym}", 20, lambda: _run(_quote, sym))


def _info(sym: str) -> dict:
    return dict(_t(sym).info or {})


async def info(sym: str) -> dict:
    return await cached(f"y:info:{sym}", 3600, lambda: _run(_info, sym))


# ------------------------------------------------------------------ prices & splits
def _history(sym: str, period: str, interval: str) -> list[dict]:
    df = _t(sym).history(period=period, interval=interval, auto_adjust=False, actions=False)
    out = []
    for idx, row in df.iterrows():
        c = _num(row.get("Close"))
        if c is not None:
            out.append({"d": idx.date().isoformat(), "close": round(c, 4)})
    return out


async def history(sym: str, period: str = "2y", interval: str = "1wk") -> list[dict]:
    ttl = 900 if interval in ("1d", "1wk") else 6 * 3600
    return await cached(f"y:h:{sym}:{period}:{interval}", ttl, lambda: _run(_history, sym, period, interval))


def _splits(sym: str) -> list[tuple[str, float]]:
    s = _t(sym).splits
    return [(idx.date().isoformat(), float(v)) for idx, v in s.items() if _num(v)]


async def splits(sym: str) -> list[tuple[str, float]]:
    return await cached(f"y:splits:{sym}", 24 * 3600, lambda: _run(_splits, sym))


def split_factor(splits_: list[tuple[str, float]], after: str) -> float:
    """Cumulative split ratio for splits effective after a date (to restate old per-share data)."""
    f = 1.0
    for d, r in splits_:
        if d > after and r > 0:
            f *= r
    return f


# ------------------------------------------------------------------ street data
def _street(sym: str) -> dict:
    t = _t(sym)
    out: dict[str, Any] = {"estimates": {}, "targets": {}, "recommendations": {}, "nextEarnings": None}
    try:
        ee, re_ = t.earnings_estimate, t.revenue_estimate
        for per in ("0y", "+1y"):
            row: dict[str, Any] = {}
            if ee is not None and not ee.empty and per in ee.index:
                r = ee.loc[per]
                row.update(eps=_num(r.get("avg")), epsLow=_num(r.get("low")), epsHigh=_num(r.get("high")),
                           epsAnalysts=_num(r.get("numberOfAnalysts")), epsGrowth=_num(r.get("growth")))
            if re_ is not None and not re_.empty and per in re_.index:
                r = re_.loc[per]
                row.update(revenue=_num(r.get("avg")), revenueLow=_num(r.get("low")), revenueHigh=_num(r.get("high")),
                           revenueGrowth=_num(r.get("growth")))
            if row:
                out["estimates"][per] = row
    except Exception as e:  # noqa: BLE001
        out["estimatesError"] = str(e)[:200]
    try:
        out["targets"] = {k: _num(v) for k, v in (t.analyst_price_targets or {}).items()}
    except Exception as e:  # noqa: BLE001
        out["targetsError"] = str(e)[:200]
    try:
        rec = t.recommendations
        if rec is not None and not rec.empty:
            r = rec.iloc[0]
            out["recommendations"] = {k: int(_num(r.get(k)) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception as e:  # noqa: BLE001
        out["recommendationsError"] = str(e)[:200]
    try:
        cal = t.calendar or {}
        ed = cal.get("Earnings Date")
        if isinstance(ed, list) and ed:
            out["nextEarnings"] = str(ed[0])
    except Exception:  # noqa: BLE001
        pass
    return out


async def street(sym: str) -> dict:
    return await cached(f"y:street:{sym}", 3 * 3600, lambda: _run(_street, sym))


# ------------------------------------------------------------------ news
def _news(sym: str, limit: int) -> list[dict]:
    raw = _t(sym).get_news(count=limit) or []
    out = []
    for it in raw:
        c = it.get("content", it)
        title = c.get("title")
        if not title:
            continue
        prov = c.get("provider") or {}
        url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or c.get("link")
        when = c.get("pubDate") or c.get("displayTime")
        if not when and c.get("providerPublishTime"):
            when = datetime.fromtimestamp(int(c["providerPublishTime"]), timezone.utc).isoformat()
        out.append({"date": (when or "")[:10], "time": when, "title": title,
                    "source": prov.get("displayName") or c.get("publisher") or "Yahoo Finance",
                    "url": url, "summary": (c.get("summary") or c.get("description") or "")[:400]})
    return out[:limit]


async def news(sym: str, limit: int = 10) -> list[dict]:
    return await cached(f"y:news:{sym}:{limit}", 600, lambda: _run(_news, sym, limit))


# ------------------------------------------------------------------ peers
def _peer(sym: str) -> dict:
    i = _info(sym)
    pct = lambda v: _num(v) * 100 if _num(v) is not None else None  # noqa: E731
    return {"ticker": sym, "name": i.get("shortName") or i.get("longName"), "price": _num(i.get("currentPrice") or i.get("regularMarketPrice")),
            "mktCap_b": _num(i.get("marketCap")) / 1e9 if _num(i.get("marketCap")) else None,
            "pe_fwd": _num(i.get("forwardPE")), "pe_ttm": _num(i.get("trailingPE")),
            "ev_ebitda": _num(i.get("enterpriseToEbitda")), "ev_sales": _num(i.get("enterpriseToRevenue")),
            "revGrowth": pct(i.get("revenueGrowth")), "opMargin": pct(i.get("operatingMargins")),
            "grossMargin": pct(i.get("grossMargins")), "fcfYield": (_num(i.get("freeCashflow")) / _num(i.get("marketCap")) * 100)
            if _num(i.get("freeCashflow")) and _num(i.get("marketCap")) else None}


async def peer(sym: str) -> dict:
    return await cached(f"y:peer:{sym}", 3600, lambda: _run(_peer, sym))


# ------------------------------------------------------------------ statement fallback (non-SEC filers)
def _statements(sym: str) -> dict:
    t = _t(sym)
    inc, cf, bs = t.income_stmt, t.cashflow, t.balance_sheet

    def val(df, rows, col):
        for r in rows:
            if df is not None and r in df.index:
                v = _num(df.at[r, col])
                if v is not None:
                    return v
        return None

    annual = []
    if inc is not None and not inc.empty:
        for col in sorted(inc.columns):
            end = col.date().isoformat()
            ocf = val(cf, ["Operating Cash Flow"], col) if cf is not None and col in cf.columns else None
            capex = val(cf, ["Capital Expenditure"], col) if cf is not None and col in cf.columns else None
            annual.append({
                "end": end, "revenue": val(inc, ["Total Revenue", "Operating Revenue"], col),
                "grossProfit": val(inc, ["Gross Profit"], col), "opIncome": val(inc, ["Operating Income", "EBIT"], col),
                "netIncome": val(inc, ["Net Income", "Net Income Common Stockholders"], col),
                "ebitda": val(inc, ["EBITDA", "Normalized EBITDA"], col), "rnd": val(inc, ["Research And Development"], col),
                "interestExpense": val(inc, ["Interest Expense"], col), "pretax": val(inc, ["Pretax Income"], col),
                "tax": val(inc, ["Tax Provision"], col), "epsRaw": val(inc, ["Diluted EPS", "Basic EPS"], col),
                "ocf": ocf, "capex": abs(capex) if capex is not None else None,
                "fcf": ocf - abs(capex) if ocf is not None and capex is not None else None,
                "sbc": val(cf, ["Stock Based Compensation"], col) if cf is not None and col in cf.columns else None,
                "buybacks": abs(val(cf, ["Repurchase Of Capital Stock"], col) or 0) if cf is not None and col in cf.columns else None,
                "dividends": abs(val(cf, ["Cash Dividends Paid", "Common Stock Dividend Paid"], col) or 0) if cf is not None and col in cf.columns else None,
            })
    balance = {}
    if bs is not None and not bs.empty:
        col = max(bs.columns)
        balance = {"end": col.date().isoformat(),
                   "cash": val(bs, ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"], col),
                   "debt": val(bs, ["Total Debt"], col), "equity": val(bs, ["Stockholders Equity", "Common Stock Equity"], col),
                   "totalAssets": val(bs, ["Total Assets"], col), "currentAssets": val(bs, ["Current Assets"], col),
                   "currentLiabilities": val(bs, ["Current Liabilities"], col)}
    return {"annual": annual, "balance": balance}


async def statements(sym: str) -> dict:
    return await cached(f"y:stmts:{sym}", 12 * 3600, lambda: _run(_statements, sym))
