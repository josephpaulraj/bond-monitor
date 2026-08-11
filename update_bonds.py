#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater.

Purpose:
- Fetch machine-readable market data from official/public sources where available.
- Keep the existing instrument universe intact.
- Write a normalized data/live.json file consumed by the dashboard.
- Never invent a price or yield when the source does not provide one.

Sources:
- U.S. Treasury daily Treasury par yield curve.
- HKMA EFBN indicative price API.
- MAS Daily SGS Prices page (best-effort HTML extraction).
- India: preserve the instrument universe and source metadata; current
  instrument-level prices/yields require a stable machine-readable RBI/FBIL
  endpoint. The updater does NOT fabricate them.

Run locally:
  python scripts/update_bonds.py
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "live.json"

UA = "bond-monitor/3.0 (+GitHub Actions; public market-data updater)"
CTX = ssl.create_default_context()

TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml?data=daily_treasury_yield_curve"
)
HKMA_URL = (
    "https://api.hkma.gov.hk/public/market-data-and-statistics/"
    "daily-monetary-statistics/efbn-indicative-price"
    "?segment=IndicativePrice&offset=0"
)
MAS_URL = "https://eservices.mas.gov.sg/statistics/fdanet/SgsBenchmarkIssuePrices.aspx"

def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def load_existing_instruments() -> list[dict[str, Any]]:
    instruments = []
    for p in sorted(DATA.glob("*.json")):
        if p.name in {"live.json", "last-update.json", "sources.json"}:
            continue
        obj = load_json(p, [])
        if isinstance(obj, dict):
            rows = obj.get("instruments") or obj.get("bonds") or []
        else:
            rows = obj if isinstance(obj, list) else []
        for row in rows:
            if isinstance(row, dict):
                instruments.append(dict(row))
    return instruments

def norm_market(s: str) -> str:
    return str(s or "").strip().lower()

def update_us_curve(records: list[dict[str, Any]], log: list[str]) -> dict[str, Any]:
    raw = fetch(TREASURY_URL).decode("utf-8", errors="replace")
    # Treasury XML uses namespace-prefixed elements. We intentionally parse
    # with regex so the workflow has no third-party dependency.
    dates = re.findall(r"<d:NEW_DATE>(.*?)</d:NEW_DATE>", raw)
    if not dates:
        dates = re.findall(r"<NEW_DATE>(.*?)</NEW_DATE>", raw)
    latest = dates[-1] if dates else None
    if not latest:
        raise RuntimeError("Treasury feed returned no date")

    block = raw[raw.rfind("<entry"): ] if "<entry" in raw else raw
    # Find the last entry containing the latest date.
    pos = raw.rfind(latest)
    block = raw[max(0, raw.rfind("<entry", 0, pos)): raw.find("</entry>", pos) + 8] if pos >= 0 else raw

    tenors = {
        "1 Mo": "1_month", "1.5 Mo": "1_5_month", "2 Mo": "2_month",
        "3 Mo": "3_month", "4 Mo": "4_month", "6 Mo": "6_month",
        "1 Yr": "1_year", "2 Yr": "2_year", "3 Yr": "3_year",
        "5 Yr": "5_year", "7 Yr": "7_year", "10 Yr": "10_year",
        "20 Yr": "20_year", "30 Yr": "30_year",
    }
    curve = {}
    for label, key in tenors.items():
        patterns = [
            rf"<d:{re.escape(key)}>(.*?)</d:{re.escape(key)}>",
            rf"<{re.escape(key)}>(.*?)</{re.escape(key)}>",
        ]
        val = None
        for pat in patterns:
            m = re.search(pat, block)
            if m:
                try: val = float(m.group(1))
                except ValueError: pass
                break
        if val is not None:
            curve[label] = val

    log.append(f"USA: Treasury curve updated for {latest}; {len(curve)} tenors.")
    return {"date": latest, "curve": curve, "source": "U.S. Treasury"}

def update_hk(records: list[dict[str, Any]], log: list[str]) -> list[dict[str, Any]]:
    obj = json.loads(fetch(HKMA_URL).decode("utf-8"))
    rows = ((obj.get("result") or {}).get("records")
            or (obj.get("result") or {}).get("data")
            or [])
    if not rows:
        log.append("Hong Kong: HKMA returned no indicative-price rows.")
        return []
    out = []
    for r in rows:
        out.append({
            "date": r.get("end_of_date"),
            "term": r.get("term"),
            "issue_no": r.get("issue_no"),
            "yield": r.get("yield"),
            "price": r.get("price"),
            "source": "HKMA",
        })
    log.append(f"Hong Kong: HKMA returned {len(out)} latest-business-day rows.")
    return out

class TextTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_td = False
        self.in_th = False
        self.rows = []
        self.row = []
        self.buf = []
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.in_td = tag == "td"
            self.in_th = tag == "th"
            self.buf = []
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and (self.in_td or self.in_th):
            self.row.append(" ".join("".join(self.buf).split()))
            self.in_td = self.in_th = False
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
    def handle_data(self, data):
        if self.in_td or self.in_th:
            self.buf.append(data)

def update_mas(log: list[str]) -> dict[str, Any]:
    html = fetch(MAS_URL).decode("utf-8", errors="replace")
    parser = TextTableParser()
    parser.feed(html)
    # MAS changes page markup from time to time. Rather than silently mapping
    # the wrong column, only accept rows whose headers clearly contain Yield.
    header = next((r for r in parser.rows if any("yield" in c.lower() for c in r)), None)
    if not header:
        log.append("Singapore: MAS page fetched, but no unambiguous yield table was found.")
        return {"source": "MAS", "status": "fetched", "rows": []}

    yield_idx = next(i for i,c in enumerate(header) if "yield" in c.lower())
    date_idx = next((i for i,c in enumerate(header) if "date" in c.lower()), None)
    rows = []
    for r in parser.rows:
        if r is header or len(r) <= yield_idx:
            continue
        y = r[yield_idx].replace("%","").strip()
        try:
            yv = float(y)
        except ValueError:
            continue
        rows.append({
            "date": r[date_idx] if date_idx is not None and date_idx < len(r) else None,
            "yield": yv,
            "source": "MAS",
        })
    log.append(f"Singapore: MAS page parsed; {len(rows)} yield rows found.")
    return {"source": "MAS", "status": "fetched", "rows": rows}

def merge_market_values(instruments, us, hk, mas, log):
    # Current instrument-level updates:
    # - HKMA issue numbers can be matched directly.
    # - U.S. Treasury is a curve, not an instrument quote; attach benchmark
    #   yield by nearest maturity tenor where possible.
    # - MAS parser is conservative; instrument matching can be expanded once
    #   the live page's exact column layout is verified in the repository.
    for x in instruments:
        x["liveYield"] = None
        x["livePrice"] = None
        x["liveDate"] = None
        x["dataStatus"] = "source-backed / quote unavailable"

        market = norm_market(x.get("market"))
        if market in ("hong kong", "hongkong", "hk"):
            issue = str(x.get("issueNo") or x.get("issue_no") or "")
            hit = next((r for r in hk if str(r.get("issue_no")) == issue), None)
            if hit:
                x["liveYield"] = hit.get("yield")
                x["livePrice"] = hit.get("price")
                x["liveDate"] = hit.get("date")
                x["dataStatus"] = "live latest business day"

        elif market in ("united states", "usa", "us"):
            # Store the full curve separately; individual bond quotes are not
            # inferred from a Treasury par curve.
            x["dataStatus"] = "Treasury curve available; instrument quote not inferred"

        elif market == "singapore":
            x["dataStatus"] = "MAS source configured; instrument quote mapping pending verification"

        elif market == "india":
            x["dataStatus"] = "RBI source universe; instrument quote endpoint not configured"

    return instruments

def main():
    DATA.mkdir(exist_ok=True)
    log = []
    instruments = load_existing_instruments()

    try:
        us = update_us_curve(instruments, log)
    except Exception as e:
        us = {"source": "U.S. Treasury", "status": "error", "error": str(e), "curve": {}}
        log.append(f"USA: ERROR {e}")

    try:
        hk = update_hk(instruments, log)
    except Exception as e:
        hk = []
        log.append(f"Hong Kong: ERROR {e}")

    try:
        mas = update_mas(log)
    except Exception as e:
        mas = {"source": "MAS", "status": "error", "error": str(e), "rows": []}
        log.append(f"Singapore: ERROR {e}")

    instruments = merge_market_values(instruments, us, hk, mas, log)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
    payload = {
        "schemaVersion": "3.0",
        "updatedAt": now,
        "status": "automated",
        "sources": {
            "usa": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
            "singapore": "https://eservices.mas.gov.sg/statistics/fdanet/SgsBenchmarkIssuePrices.aspx",
            "hongkong": "https://apidocs.hkma.gov.hk/documentation/market-data-and-statistics/daily-monetary-statistics/efbn-indicative-price",
            "india": "https://data.rbi.org.in/",
        },
        "usaCurve": us,
        "hongkongIndicative": hk,
        "singaporeBenchmark": mas,
        "instruments": instruments,
        "log": log,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (DATA / "last-update.json").write_text(
        json.dumps({"updatedAt": now, "log": log}, indent=2) + "\n",
        encoding="utf-8"
    )
    print("\n".join(log))
    print(f"Wrote {OUT}")

if __name__ == "__main__":
    main()
