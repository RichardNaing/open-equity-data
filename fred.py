"""FRED (Federal Reserve Bank of St. Louis) macro series via the keyless CSV endpoint."""
from __future__ import annotations

import asyncio
import csv
import io
from datetime import date, timedelta

import httpx

from cache import cached

# id, display name, unit, transform ("level" or "yoy"), impact on equities when rising
SERIES = [
    ("DFF", "Fed funds rate", "%", "level", "negative"),
    ("DGS10", "10Y Treasury yield", "%", "level", "negative"),
    ("T10Y2Y", "10Y-2Y curve", "pp", "level", "positive"),
    ("CPIAUCSL", "CPI inflation", "% y/y", "yoy", "negative"),
    ("PCEPILFE", "Core PCE inflation", "% y/y", "yoy", "negative"),
    ("UNRATE", "Unemployment rate", "%", "level", "negative"),
    ("A191RL1Q225SBEA", "Real GDP growth", "% q/q ann.", "level", "positive"),
    ("BAMLH0A0HYM2", "High-yield credit spread", "pp", "level", "negative"),
    ("DTWEXBGS", "Broad US dollar index", "index", "level", "negative"),
    ("DCOILWTICO", "WTI crude oil", "$/bbl", "level", "neutral"),
    ("VIXCLS", "VIX volatility", "index", "level", "negative"),
]


def parse_csv(text: str) -> list[tuple[str, float]]:
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) < 2 or not rec[0][:1].isdigit():
            continue
        try:
            rows.append((rec[0], float(rec[1])))
        except ValueError:
            continue  # "." = missing
    return rows


async def _fetch(sid: str, client: httpx.AsyncClient) -> list[tuple[str, float]]:
    start = (date.today() - timedelta(days=800)).isoformat()
    r = await client.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid, "cosd": start})
    r.raise_for_status()
    return parse_csv(r.text)


def _at_or_before(rows: list[tuple[str, float]], d: str) -> float | None:
    v = None
    for day, val in rows:
        if day <= d:
            v = val
        else:
            break
    return v


def summarize(sid: str, name: str, unit: str, transform: str, impact_up: str, rows: list[tuple[str, float]]) -> dict | None:
    if not rows:
        return None
    if transform == "yoy":
        yoy = []
        for i, (d, v) in enumerate(rows):
            prior = _at_or_before(rows[: i + 1], (date.fromisoformat(d) - timedelta(days=365)).isoformat())
            if prior and i >= 12:
                yoy.append((d, (v / prior - 1) * 100))
        rows = yoy
        if not rows:
            return None
    last_d, last_v = rows[-1]
    ago = lambda days: _at_or_before(rows, (date.fromisoformat(last_d) - timedelta(days=days)).isoformat())  # noqa: E731
    v3, v12 = ago(91), ago(365)
    chg3 = last_v - v3 if v3 is not None else None
    tol = max(0.05, abs(last_v) * 0.02)
    trend = "flat" if chg3 is None or abs(chg3) < tol else ("up" if chg3 > 0 else "down")
    if trend == "flat" or impact_up == "neutral":
        impact = "neutral"
    else:
        impact = impact_up if trend == "up" else ("positive" if impact_up == "negative" else "negative")
    return {"id": sid, "name": name, "unit": unit, "value": round(last_v, 2), "date": last_d,
            "change3m": round(chg3, 2) if chg3 is not None else None,
            "change12m": round(last_v - v12, 2) if v12 is not None else None,
            "trend": trend, "impact": impact, "url": f"https://fred.stlouisfed.org/series/{sid}"}


async def macro() -> list[dict]:
    async def load():
        async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "OpenEquityData/1.0"}) as c:
            res = await asyncio.gather(*(_fetch(s[0], c) for s in SERIES), return_exceptions=True)
        out = []
        for spec, rows in zip(SERIES, res):
            if isinstance(rows, Exception):
                continue
            s = summarize(*spec, rows)
            if s:
                out.append(s)
        return out

    return await cached("fred:macro", 3600, load)
