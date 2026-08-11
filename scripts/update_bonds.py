#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.1

Purpose
-------
- Load the original Bond Monitor instrument universe from:
    data/usa.json
    data/singapore.json
    data/hongkong.json
    data/india.json
- Preserve all instruments even when an external source fails.
- Update live values only when explicitly available from an official source.
- Never fabricate a price or yield.
- Never replace existing values with null because of a temporary source failure.
- Write normalized data/live.json and data/last-update.json.

Official sources
----------------
USA:
    U.S. Treasury daily Treasury par yield curve

Singapore:
    Monetary Authority of Singapore SGS prices/yields

Hong Kong:
    Hong Kong Monetary Authority EFBN indicative prices

India:
    RBI source metadata retained.
    Instrument-level live quotes are not fabricated when a stable
    machine-readable official endpoint is unavailable.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "live.json"
LAST_UPDATE = DATA / "last-update.json"


# ---------------------------------------------------------------------------
# HTTP configuration
# ---------------------------------------------------------------------------

UA = (
    "bond-monitor/3.1 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
HTTP_RETRY_DELAY = 3


# ---------------------------------------------------------------------------
# Official source URLs
# ---------------------------------------------------------------------------

TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml"
)

HKMA_URL = (
    "https://api.hkma.gov.hk/public/market-data-and-statistics/"
    "daily-monetary-statistics/efbn-indicative-price"
    "?segment=IndicativePrice&offset=0"
)

MAS_URL = (
    "https://eservices.mas.gov.sg/statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx"
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def fetch(
    url: str,
    timeout: int = HTTP_TIMEOUT,
    retries: int = HTTP_RETRIES,
) -> bytes:

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "*/*",
                    "Cache-Control": "no-cache",
                },
            )

            with urllib.request.urlopen(
                req,
                timeout=timeout,
                context=CTX,
            ) as response:

                return response.read()

        except Exception as exc:

            last_error = exc

            if attempt < retries:
                time.sleep(HTTP_RETRY_DELAY)

    raise RuntimeError(
        f"{last_error}"
    )


def load_json(path: Path, default: Any) -> Any:

    try:
        return json.loads(
            path.read_text(encoding="utf-8")
        )

    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:

    path.write_text(
        json.dumps(
            obj,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Phase 1 instrument universe
# ---------------------------------------------------------------------------

COUNTRY_FILES = {
    "United States": DATA / "usa.json",
    "Singapore": DATA / "singapore.json",
    "Hong Kong": DATA / "hongkong.json",
    "India": DATA / "india.json",
}


def load_instrument_universe(log: list[str]) -> list[dict[str, Any]]:

    instruments: list[dict[str, Any]] = []

    for market, path in COUNTRY_FILES.items():

        if not path.exists():

            log.append(
                f"{market}: ERROR missing {path.name}"
            )

            continue

        obj = load_json(path, {})

        if not isinstance(obj, dict):

            log.append(
                f"{market}: ERROR {path.name} is not a JSON object"
            )

            continue

        records = obj.get("records", [])

        if not isinstance(records, list):

            log.append(
                f"{market}: ERROR records[] missing in {path.name}"
            )

            continue

        count = 0

        for record in records:

            if isinstance(record, dict):

                instruments.append(
                    dict(record)
                )

                count += 1

        log.append(
            f"{market}: loaded {count} instruments from {path.name}"
        )

    log.append(
        f"Loaded {len(instruments)} instruments from Phase 1 country files."
    )

    return instruments


# ---------------------------------------------------------------------------
# Market normalization
# ---------------------------------------------------------------------------

def norm_market(value: Any) -> str:

    return str(value or "").strip().lower()


def clean_number(value: Any) -> float | None:

    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = (
        text
        .replace(",", "")
        .replace("%", "")
    )

    try:
        return float(text)

    except ValueError:
        return None


# ---------------------------------------------------------------------------
# USA Treasury
# ---------------------------------------------------------------------------

def update_us_curve(log: list[str]) -> dict[str, Any]:

    # Treasury requires the year parameter for the XML feed.
    year = datetime.now(timezone.utc).year

    url = (
        TREASURY_URL
        + f"?data=daily_treasury_yield_curve"
        + f"&field_tdr_date_value={year}"
    )

    raw = fetch(
        url,
        timeout=60,
        retries=3,
    ).decode(
        "utf-8",
        errors="replace",
    )

    # Treasury XML normally contains:
    # <d:NEW_DATE ...>2026-08-10T00:00:00</d:NEW_DATE>
    date_patterns = [
        r"<d:NEW_DATE[^>]*>(.*?)</d:NEW_DATE>",
        r"<NEW_DATE[^>]*>(.*?)</NEW_DATE>",
    ]

    dates = []

    for pattern in date_patterns:

        dates = re.findall(
            pattern,
            raw,
            flags=re.IGNORECASE,
        )

        if dates:
            break

    if not dates:
        raise RuntimeError(
            "Treasury feed returned no date"
        )

    latest = dates[-1]

    # Locate the latest Treasury entry.
    pos = raw.rfind(latest)

    if pos < 0:
        raise RuntimeError(
            "Treasury latest-date record could not be located"
        )

    entry_start = raw.rfind(
        "<entry",
        0,
        pos,
    )

    entry_end = raw.find(
        "</entry>",
        pos,
    )

    if entry_start >= 0 and entry_end >= 0:

        block = raw[
            entry_start:
            entry_end + len("</entry>")
        ]

    else:

        block = raw

    # IMPORTANT:
    # Treasury field names are not simply "1_month", etc.
    # They use names such as:
    #
    # BC_1MONTH
    # BC_1_5MONTH
    # BC_2MONTH
    # BC_3MONTH
    # BC_4MONTH
    # BC_6MONTH
    # BC_1YEAR
    # BC_2YEAR
    # BC_3YEAR
    # BC_5YEAR
    # BC_7YEAR
    # BC_10YEAR
    # BC_20YEAR
    # BC_30YEAR

    tenors = {
        "1 Mo": "BC_1MONTH",
        "1.5 Mo": "BC_1_5MONTH",
        "2 Mo": "BC_2MONTH",
        "3 Mo": "BC_3MONTH",
        "4 Mo": "BC_4MONTH",
        "6 Mo": "BC_6MONTH",
        "1 Yr": "BC_1YEAR",
        "2 Yr": "BC_2YEAR",
        "3 Yr": "BC_3YEAR",
        "5 Yr": "BC_5YEAR",
        "7 Yr": "BC_7YEAR",
        "10 Yr": "BC_10YEAR",
        "20 Yr": "BC_20YEAR",
        "30 Yr": "BC_30YEAR",
    }

    curve = {}

    for label, field in tenors.items():

        patterns = [
            rf"<d:{field}[^>]*>(.*?)</d:{field}>",
            rf"<{field}[^>]*>(.*?)</{field}>",
        ]

        value = None

        for pattern in patterns:

            match = re.search(
                pattern,
                block,
                flags=re.IGNORECASE,
            )

            if match:

                value = clean_number(
                    match.group(1)
                )

                break

        if value is not None:

            curve[label] = value

    if not curve:

        raise RuntimeError(
            "Treasury feed returned no curve values"
        )

    log.append(
        f"USA: Treasury curve updated for "
        f"{latest}; {len(curve)} tenors."
    )

    return {
        "source": "U.S. Treasury",
        "status": "success",
        "date": latest,
        "curve": curve,
    }

def update_us_instruments(
    instruments: list[dict[str, Any]],
    us: dict[str, Any],
) -> None:

    curve = us.get("curve", {})
    date = us.get("date")

    maturity_map = {
        "5-Year Treasury benchmark": "5 Yr",
        "10-Year Treasury benchmark": "10 Yr",
        "30-Year Treasury benchmark": "30 Yr",
    }

    for instrument in instruments:

        if norm_market(
            instrument.get("market")
        ) not in {
            "united states",
            "usa",
            "us",
        }:
            continue

        bond = str(
            instrument.get("bond") or ""
        )

        tenor = maturity_map.get(bond)

        if not tenor:
            continue

        value = curve.get(tenor)

        if value is None:
            continue

        # Preserve the previous value before replacing it.
        instrument["previousYield"] = instrument.get(
            "yield"
        )

        instrument["yield"] = value
        instrument["liveYield"] = value
        instrument["liveDate"] = date

        instrument["dataStatus"] = (
            "live U.S. Treasury benchmark yield"
        )
# ---------------------------------------------------------------------------
# Hong Kong HKMA
# ---------------------------------------------------------------------------

def update_hkma(log: list[str]) -> list[dict[str, Any]]:

    raw = fetch(
        HKMA_URL,
        timeout=90,
        retries=3,
    )

    obj = json.loads(
        raw.decode(
            "utf-8",
            errors="replace",
        )
    )

    result = obj.get("result") or {}

    rows = (
        result.get("records")
        or result.get("data")
        or []
    )

    if not isinstance(rows, list):

        raise RuntimeError(
            "HKMA response did not contain records"
        )

    output: list[dict[str, Any]] = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        output.append(
            {
                "date": row.get("end_of_date"),
                "term": row.get("term"),
                "issue_no": row.get("issue_no"),
                "yield": clean_number(
                    row.get("yield")
                ),
                "price": clean_number(
                    row.get("price")
                ),
                "source": "HKMA",
            }
        )

    log.append(
        f"Hong Kong: HKMA returned "
        f"{len(output)} indicative-price rows."
    )

    return output


def update_hk_instruments(
    instruments: list[dict[str, Any]],
    hk_rows: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

        if norm_market(
            instrument.get("market")
        ) != "hong kong":
            continue

        bond = str(
            instrument.get("bond") or ""
        )

        isin = str(
            instrument.get("isin") or ""
        )

        # Exchange Fund instruments don't have ISINs.
        # Match them using their descriptive bond name/term.
        candidates = hk_rows

        hit = None

        if isin and isin != "—":

            hit = next(
                (
                    row
                    for row in candidates
                    if str(
                        row.get("issue_no") or ""
                    ) == isin
                ),
                None,
            )

        if hit is None:

            bond_lower = bond.lower()

            if "exchange fund note" in bond_lower:

                hit = next(
                    (
                        row
                        for row in candidates
                        if "note" in str(
                            row.get("term") or ""
                        ).lower()
                    ),
                    None,
                )

            elif "exchange fund bill" in bond_lower:

                hit = next(
                    (
                        row
                        for row in candidates
                        if "bill" in str(
                            row.get("term") or ""
                        ).lower()
                    ),
                    None,
                )

        if not hit:
            continue

        changed = False

        if hit.get("yield") is not None:

            instrument["previousYield"] = instrument.get(
                "yield"
            )

            instrument["yield"] = hit["yield"]
            instrument["liveYield"] = hit["yield"]

            changed = True

        if hit.get("price") is not None:

            instrument["previousPrice"] = instrument.get(
                "price"
            )

            instrument["price"] = hit["price"]
            instrument["livePrice"] = hit["price"]

            changed = True

        if changed:

            instrument["liveDate"] = hit.get(
                "date"
            )

            instrument["dataStatus"] = (
                "live HKMA indicative data"
            )


# ---------------------------------------------------------------------------
# Singapore MAS
# ---------------------------------------------------------------------------

class TextTableParser(HTMLParser):

    def __init__(self) -> None:

        super().__init__()

        self.in_cell = False
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.buf: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:

        tag = tag.lower()

        if tag == "tr":

            self.row = []

        elif tag in ("td", "th"):

            self.in_cell = True
            self.buf = []

    def handle_endtag(
        self,
        tag: str,
    ) -> None:

        tag = tag.lower()

        if tag in ("td", "th"):

            if self.in_cell:

                text = " ".join(
                    "".join(
                        self.buf
                    ).split()
                )

                self.row.append(text)

                self.in_cell = False

        elif tag == "tr":

            if self.row:

                self.rows.append(
                    self.row
                )

    def handle_data(
        self,
        data: str,
    ) -> None:

        if self.in_cell:

            self.buf.append(data)


def update_mas(
    log: list[str],
) -> dict[str, Any]:

    html = fetch(
        MAS_URL,
        timeout=90,
        retries=3,
    ).decode(
        "utf-8",
        errors="replace",
    )

    parser = TextTableParser()

    parser.feed(html)

    # Look for a row containing both a likely issue-code
    # heading and a price/yield heading.
    header = None

    for row in parser.rows:

        text = " ".join(row).lower()

        if (
            "yield" in text
            or "price" in text
        ) and (
            "issue" in text
            or "code" in text
            or "isin" in text
        ):

            header = row
            break

    if not header:

        log.append(
            "Singapore: MAS page fetched, "
            "but no SGS issue codes were detected."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
        }

    issue_idx = None
    yield_idx = None
    price_idx = None
    date_idx = None

    for i, column in enumerate(header):

        c = column.lower()

        if issue_idx is None and (
            "issue" in c
            or "code" in c
            or "isin" in c
        ):
            issue_idx = i

        if yield_idx is None and "yield" in c:
            yield_idx = i

        if price_idx is None and "price" in c:
            price_idx = i

        if date_idx is None and "date" in c:
            date_idx = i

    rows: list[dict[str, Any]] = []

    for row in parser.rows:

        if row is header:
            continue

        if issue_idx is None:
            continue

        if len(row) <= issue_idx:
            continue

        issue_code = row[issue_idx].strip()

        if not issue_code:
            continue

        yield_value = None
        price_value = None
        date_value = None

        if (
            yield_idx is not None
            and len(row) > yield_idx
        ):

            yield_value = clean_number(
                row[yield_idx]
            )

        if (
            price_idx is not None
            and len(row) > price_idx
        ):

            price_value = clean_number(
                row[price_idx]
            )

        if (
            date_idx is not None
            and len(row) > date_idx
        ):

            date_value = row[date_idx]

        if (
            yield_value is None
            and price_value is None
        ):
            continue

        rows.append(
            {
                "issue_code": issue_code,
                "yield": yield_value,
                "price": price_value,
                "date": date_value,
                "source": "MAS",
            }
        )

    log.append(
        f"Singapore: MAS parser found "
        f"{len(rows)} SGS rows."
    )

    return {
        "source": "MAS",
        "status": "success",
        "rows": rows,
    }


def update_singapore_instruments(
    instruments: list[dict[str, Any]],
    mas: dict[str, Any],
) -> None:

    rows = mas.get("rows", [])

    for instrument in instruments:

        if norm_market(
            instrument.get("market")
        ) != "singapore":
            continue

        issue_code = str(
            instrument.get("issue_code")
            or ""
        ).strip()

        if not issue_code:
            continue

        hit = next(
            (
                row
                for row in rows
                if str(
                    row.get("issue_code") or ""
                ).strip()
                == issue_code
            ),
            None,
        )

        if not hit:
            continue

        changed = False

        if hit.get("yield") is not None:

            instrument["previousYield"] = instrument.get(
                "yield"
            )

            instrument["yield"] = hit["yield"]
            instrument["liveYield"] = hit["yield"]

            changed = True

        if hit.get("price") is not None:

            instrument["previousPrice"] = instrument.get(
                "price"
            )

            instrument["price"] = hit["price"]
            instrument["livePrice"] = hit["price"]

            changed = True

        if changed:

            instrument["liveDate"] = hit.get(
                "date"
            )

            instrument["dataStatus"] = (
                "live MAS SGS data"
            )


# ---------------------------------------------------------------------------
# India
# ---------------------------------------------------------------------------

def update_india_instruments(
    instruments: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

        if norm_market(
            instrument.get("market")
        ) != "india":
            continue

        # India instrument-level live values are deliberately
        # not fabricated. Preserve the Phase 1 source-backed
        # instrument information.

        instrument.setdefault(
            "liveYield",
            None,
        )

        instrument.setdefault(
            "livePrice",
            None,
        )

        instrument.setdefault(
            "liveDate",
            None,
        )

        instrument["dataStatus"] = (
            "RBI source configured; "
            "instrument quote endpoint unavailable"
        )


# ---------------------------------------------------------------------------
# Final normalization
# ---------------------------------------------------------------------------

def initialize_live_fields(
    instruments: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

        # Preserve the original Phase 1 values.

        instrument.setdefault(
            "liveYield",
            None,
        )

        instrument.setdefault(
            "livePrice",
            None,
        )

        instrument.setdefault(
            "liveDate",
            None,
        )

        if not instrument.get(
            "dataStatus"
        ):

            instrument["dataStatus"] = (
                "source-backed seed"
            )


def calculate_status(
    log: list[str],
    instruments: list[dict[str, Any]],
) -> str:

    errors = [
        entry
        for entry in log
        if ": ERROR" in entry
    ]

    if not instruments:
        return "error"

    if errors:
        return "partial"

    return "success"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:

    DATA.mkdir(
        exist_ok=True
    )

    log: list[str] = []

    # ---------------------------------------------------------------
    # 1. Load the original Phase 1 universe.
    # ---------------------------------------------------------------

    instruments = load_instrument_universe(
        log
    )

    if not instruments:

        log.append(
            "ERROR: No instruments were loaded. "
            "Existing country files were not modified."
        )

        payload = {
            "schemaVersion": "3.1",
            "updatedAt": now_utc(),
            "status": "error",
            "sources": {},
            "instruments": [],
            "log": log,
        }

        save_json(
            OUT,
            payload,
        )

        save_json(
            LAST_UPDATE,
            {
                "updatedAt": payload["updatedAt"],
                "status": "error",
                "log": log,
            },
        )

        raise SystemExit(
            "No instruments loaded."
        )

    initialize_live_fields(
        instruments
    )

    # ---------------------------------------------------------------
    # 2. USA Treasury
    # ---------------------------------------------------------------

    try:

        us = update_us_curve(
            log
        )

        update_us_instruments(
            instruments,
            us,
        )

    except Exception as exc:

        us = {
            "source": "U.S. Treasury",
            "status": "error",
            "error": str(exc),
            "curve": {},
        }

        log.append(
            f"USA: ERROR {exc}"
        )

    # ---------------------------------------------------------------
    # 3. Hong Kong HKMA
    # ---------------------------------------------------------------

    try:

        hk = update_hkma(
            log
        )

        update_hk_instruments(
            instruments,
            hk,
        )

    except Exception as exc:

        hk = []

        log.append(
            f"Hong Kong: ERROR {exc}"
        )

    # ---------------------------------------------------------------
    # 4. Singapore MAS
    # ---------------------------------------------------------------

    try:

        mas = update_mas(
            log
        )

        update_singapore_instruments(
            instruments,
            mas,
        )

    except Exception as exc:

        mas = {
            "source": "MAS",
            "status": "error",
            "error": str(exc),
            "rows": [],
        }

        log.append(
            f"Singapore: ERROR {exc}"
        )

    # ---------------------------------------------------------------
    # 5. India
    # ---------------------------------------------------------------

    try:

        update_india_instruments(
            instruments
        )

        log.append(
            "India: Phase 1 RBI instrument universe preserved; "
            "no unverified live quote was inferred."
        )

    except Exception as exc:

        log.append(
            f"India: ERROR {exc}"
        )

    # ---------------------------------------------------------------
    # 6. Final status
    # ---------------------------------------------------------------

    status = calculate_status(
        log,
        instruments,
    )

    updated_at = now_utc()

    payload = {
        "schemaVersion": "3.1",
        "updatedAt": updated_at,
        "status": status,

        "sources": {
            "usa": (
                "https://home.treasury.gov/"
                "resource-center/data-chart-center/"
                "interest-rates"
            ),

            "singapore": (
                "https://eservices.mas.gov.sg/"
                "statistics/fdanet/"
                "SgsBenchmarkIssuePrices.aspx"
            ),

            "hongkong": (
                "https://apidocs.hkma.gov.hk/"
                "documentation/market-data-and-statistics/"
                "daily-monetary-statistics/"
                "efbn-indicative-price"
            ),

            "india": (
                "https://data.rbi.org.in/"
            ),
        },

        "usaCurve": us,

        "hongkongIndicative": hk,

        "singaporeBenchmark": mas,

        "instrumentCount": len(
            instruments
        ),

        "instruments": instruments,

        "log": log,
    }

    # ---------------------------------------------------------------
    # IMPORTANT:
    # Only write live.json after instruments have been confirmed.
    # ---------------------------------------------------------------

    save_json(
        OUT,
        payload,
    )

    save_json(
        LAST_UPDATE,
        {
            "updatedAt": updated_at,
            "status": status,
            "instrumentCount": len(
                instruments
            ),
            "log": log,
        },
    )

    # ---------------------------------------------------------------
    # Console summary
    # ---------------------------------------------------------------

    print(
        "=========================================="
    )

    print(
        " Bond Monitor Phase 3 Update v3.1"
    )

    print(
        "=========================================="
    )

    print(
        f"Overall status : {status}"
    )

    print(
        f"Instruments    : {len(instruments)}"
    )

    print(
        f"USA curve      : "
        f"{us.get('status', 'unknown').upper()}"
    )

    print(
        "Hong Kong      : "
        f"{'OK' if isinstance(hk, list) and hk else 'ERROR/PARTIAL'}"
    )

    print(
        "Singapore      : "
        f"{mas.get('status', 'unknown').upper()}"
    )

    print(
        "------------------------------------------"
    )

    for entry in log:
        print(entry)

    print(
        "------------------------------------------"
    )

    print(
        f"Wrote {OUT}"
    )

    print(
        f"Wrote {LAST_UPDATE}"
    )


if __name__ == "__main__":
    main()
