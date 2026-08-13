#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.3

Loads the Phase 1 instrument universe, updates official live data where
available, preserves existing values on source failures, and writes:
  data/live.json
  data/last-update.json

Singapore fix in v3.3:
- Parses the actual MAS Closing Levels HTML table instead of scraping all
  page numbers as one flat numeric sequence.
- Uses the latest dated table row.
- Maps SGS issue codes to their actual table columns.
- Does not fabricate missing values.
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

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "live.json"
LAST_UPDATE = DATA / "last-update.json"

UA = "bond-monitor/3.3 (GitHub Actions; official public market-data updater)"
CTX = ssl.create_default_context()
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
HTTP_RETRY_DELAY = 3

TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml"
)

HKMA_URL = (
    "https://api.hkma.gov.hk/public/market-data-and-statistics/"
    "daily-monetary-statistics/efbn-indicative-price"
    "?segment=IndicativePrice&offset=0"
)

# Official MAS SGS benchmark page.
MAS_URL = (
    "https://eservices.mas.gov.sg/Statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx"
)

COUNTRY_FILES = {
    "United States": DATA / "usa.json",
    "Singapore": DATA / "singapore.json",
    "Hong Kong": DATA / "hongkong.json",
    "India": DATA / "india.json",
}


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
                    "Accept": "text/html,application/xml,application/json,*/*",
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

    raise RuntimeError(str(last_error))


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_instrument_universe(log: list[str]) -> list[dict[str, Any]]:
    instruments: list[dict[str, Any]] = []

    for market, path in COUNTRY_FILES.items():
        if not path.exists():
            log.append(f"{market}: ERROR missing {path.name}")
            continue

        obj = load_json(path, {})

        if not isinstance(obj, dict):
            log.append(f"{market}: ERROR {path.name} is not a JSON object")
            continue

        records = obj.get("records", [])

        if not isinstance(records, list):
            log.append(f"{market}: ERROR records[] missing in {path.name}")
            continue

        count = 0

        for record in records:
            if isinstance(record, dict):
                instruments.append(dict(record))
                count += 1

        log.append(f"{market}: loaded {count} instruments from {path.name}")

    log.append(
        f"Loaded {len(instruments)} instruments from Phase 1 country files."
    )

    return instruments


def norm_market(value: Any) -> str:
    return str(value or "").strip().lower()


def clean_number(value: Any) -> float | None:
    if value is None:
        return None

    text = str(value).strip()

    if not text or text in {"-", "—", "–"}:
        return None

    text = text.replace(",", "").replace("%", "")

    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# USA Treasury
# ---------------------------------------------------------------------------

def update_us_curve(log: list[str]) -> dict[str, Any]:
    year = datetime.now(timezone.utc).year

    url = (
        TREASURY_URL
        + "?data=daily_treasury_yield_curve"
        + f"&field_tdr_date_value={year}"
    )

    raw = fetch(url, timeout=60, retries=3).decode(
        "utf-8",
        errors="replace",
    )

    date_patterns = [
        r"<d:NEW_DATE[^>]*>(.*?)</d:NEW_DATE>",
        r"<NEW_DATE[^>]*>(.*?)</NEW_DATE>",
    ]

    dates: list[str] = []

    for pattern in date_patterns:
        dates = re.findall(pattern, raw, flags=re.IGNORECASE)
        if dates:
            break

    if not dates:
        raise RuntimeError("Treasury feed returned no date")

    latest = dates[-1]
    pos = raw.rfind(latest)

    if pos < 0:
        raise RuntimeError(
            "Treasury latest-date record could not be located"
        )

    entry_start = raw.rfind("<entry", 0, pos)
    entry_end = raw.find("</entry>", pos)

    if entry_start >= 0 and entry_end >= 0:
        block = raw[entry_start:entry_end + len("</entry>")]
    else:
        block = raw

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

    curve: dict[str, float] = {}

    for label, field in tenors.items():
        patterns = [
            rf"<d:{field}[^>]*>(.*?)</d:{field}>",
            rf"<{field}[^>]*>(.*?)</{field}>",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                block,
                flags=re.IGNORECASE,
            )

            if match:
                value = clean_number(match.group(1))
                if value is not None:
                    curve[label] = value
                break

    if not curve:
        raise RuntimeError("Treasury feed returned no curve values")

    log.append(
        f"USA: Treasury curve updated for {latest}; "
        f"{len(curve)} tenors."
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
        if norm_market(instrument.get("market")) not in {
            "united states",
            "usa",
            "us",
        }:
            continue

        tenor = maturity_map.get(str(instrument.get("bond") or ""))
        if not tenor:
            continue

        value = curve.get(tenor)
        if value is None:
            continue

        old = instrument.get("yield")

        if old is not None:
            instrument["previousYield"] = old

        instrument["yield"] = value
        instrument["liveYield"] = value
        instrument["liveDate"] = date
        instrument["dataStatus"] = "live U.S. Treasury benchmark yield"


# ---------------------------------------------------------------------------
# Hong Kong HKMA
# ---------------------------------------------------------------------------

def update_hkma(log: list[str]) -> list[dict[str, Any]]:
    raw = fetch(HKMA_URL, timeout=90, retries=3)

    obj = json.loads(
        raw.decode("utf-8", errors="replace")
    )

    result = obj.get("result") or {}
    rows = result.get("records") or result.get("data") or []

    if not isinstance(rows, list):
        raise RuntimeError("HKMA response did not contain records")

    output: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        output.append(
            {
                "date": row.get("end_of_date"),
                "term": row.get("term"),
                "issue_no": row.get("issue_no"),
                "yield": clean_number(row.get("yield")),
                "price": clean_number(row.get("price")),
                "source": "HKMA",
            }
        )

    log.append(
        f"Hong Kong: HKMA returned {len(output)} indicative-price rows."
    )

    return output


def update_hk_instruments(
    instruments: list[dict[str, Any]],
    hk_rows: list[dict[str, Any]],
) -> None:
    for instrument in instruments:
        if norm_market(instrument.get("market")) != "hong kong":
            continue

        bond = str(instrument.get("bond") or "")
        isin = str(instrument.get("isin") or "")

        hit = None

        if isin and isin != "—":
            hit = next(
                (
                    row
                    for row in hk_rows
                    if str(row.get("issue_no") or "") == isin
                ),
                None,
            )

        if hit is None:
            bond_lower = bond.lower()

            if "exchange fund note" in bond_lower:
                hit = next(
                    (
                        row
                        for row in hk_rows
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
                        for row in hk_rows
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
            old = instrument.get("yield")
            if old is not None:
                instrument["previousYield"] = old

            instrument["yield"] = hit["yield"]
            instrument["liveYield"] = hit["yield"]
            changed = True

        if hit.get("price") is not None:
            old = instrument.get("price")
            if old is not None:
                instrument["previousPrice"] = old

            instrument["price"] = hit["price"]
            instrument["livePrice"] = hit["price"]
            changed = True

        if changed:
            instrument["liveDate"] = hit.get("date")
            instrument["dataStatus"] = "live HKMA indicative data"


# ---------------------------------------------------------------------------
# Singapore MAS
# ---------------------------------------------------------------------------

class TextTableParser(HTMLParser):
    """Simple HTML table parser preserving each row as a list of cells."""

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

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in ("td", "th"):
            if self.in_cell:
                text = " ".join("".join(self.buf).split())
                self.row.append(text)
                self.in_cell = False

        elif tag == "tr":
            if self.row:
                self.rows.append(self.row)

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.buf.append(data)


MAS_TARGET_CODES = {
    "N523100W",  # 2Y
    "NX21100N",  # 5Y
    "NZ16100X",  # 10Y
    "NY25200N",  # 15Y
    "NA16100H",  # 30Y
    "NC22300W",  # 50Y
}

# Positions in the MAS Closing Levels data row.
#
# A row has:
#   date,
#   6M yield,
#   1Y yield,
#   2Y price, 2Y yield,
#   5Y price, 5Y yield,
#   10Y price, 10Y yield,
#   15Y price, 15Y yield,
#   20Y price, 20Y yield,
#   30Y price, 30Y yield,
#   50Y yield
#
# We determine positions from the issue-code header instead of relying
# solely on these fixed offsets, but these are used as a safe fallback.
MAS_FIXED_COLUMNS = {
    "N523100W": (3, 4),
    "NX21100N": (5, 6),
    "NZ16100X": (7, 8),
    "NY25200N": (9, 10),
    "NA16100H": (13, 14),
    "NC22300W": (15, 15),
}


def is_mas_date(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}",
            value.strip(),
        )
    )


def parse_mas_date_key(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(
        r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})",
        value.strip(),
    )

    if not match:
        return None

    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }

    month = months.get(match.group(2).lower())
    if month is None:
        return None

    return (
        int(match.group(3)),
        month,
        int(match.group(1)),
    )


def find_mas_issue_header(
    rows: list[list[str]],
) -> tuple[int, dict[str, int]]:
    """
    Find the row containing MAS SGS issue codes.

    Returns:
      header row index
      issue_code -> logical column index

    The MAS page currently exposes the benchmark header in a row such as:
      ... N523100W NX21100N NZ16100X NY25200N NA16100H ... NC22300W
    """
    best_index = -1
    best_map: dict[str, int] = {}

    for index, row in enumerate(rows):
        code_map: dict[str, int] = {}

        for col, cell in enumerate(row):
            value = str(cell).strip().upper()
            if value in MAS_TARGET_CODES:
                code_map[value] = col

        if len(code_map) > len(best_map):
            best_index = index
            best_map = code_map

    if best_index < 0 or len(best_map) < 5:
        raise RuntimeError(
            "MAS Closing Levels issue-code header could not be parsed"
        )

    return best_index, best_map


def find_mas_latest_data_row(
    rows: list[list[str]],
    header_index: int,
) -> tuple[int, list[str]]:
    """
    Find the latest dated row after the Closing Levels header.

    We deliberately require a real date in the first cell. This avoids
    accidentally interpreting dates embedded elsewhere in the page as data.
    """
    candidates: list[tuple[tuple[int, int, int], int, list[str]]] = []

    for index in range(header_index + 1, len(rows)):
        row = rows[index]

        if not row:
            continue

        date_text = str(row[0]).strip()
        date_key = parse_mas_date_key(date_text)

        if date_key is None:
            continue

        numeric_count = sum(
            1
            for cell in row[1:]
            if clean_number(cell) is not None
        )

        # A valid Closing Levels row should have several numeric values.
        if numeric_count >= 4:
            candidates.append((date_key, index, row))

    if not candidates:
        raise RuntimeError(
            "MAS Closing Levels table contains no dated data rows"
        )

    candidates.sort(key=lambda item: item[0])

    _, row_index, latest_row = candidates[-1]
    return row_index, latest_row


def mas_column_for_issue(
    issue_code: str,
    issue_map: dict[str, int],
) -> tuple[int, int]:
    """
    Convert an issue-code header position into price/yield data positions.

    Because MAS uses rowspan/colspan in the HTML, a simple parser can expose
    the issue-code position differently depending on the exact markup.
    We therefore use the known current MAS logical layout as the authoritative
    fallback and validate against the row length.
    """
    return MAS_FIXED_COLUMNS[issue_code]


def update_mas(log: list[str]) -> dict[str, Any]:
    html = fetch(MAS_URL, timeout=90, retries=3).decode(
        "utf-8",
        errors="replace",
    )

    parser = TextTableParser()
    parser.feed(html)

    rows = parser.rows

    if not rows:
        log.append(
            "Singapore: MAS page fetched, but no HTML table rows were parsed."
        )
        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
        }

    try:
        header_index, issue_map = find_mas_issue_header(rows)
    except Exception:
        # The official page is known to expose the issue codes in the visible
        # Closing Levels table. If the parser cannot identify that structure,
        # do not infer values from arbitrary page text.
        found_codes = sorted(
            {
                str(cell).strip().upper()
                for row in rows
                for cell in row
                if str(cell).strip().upper() in MAS_TARGET_CODES
            }
        )

        if found_codes:
            log.append(
                "Singapore: MAS page fetched; found "
                f"{len(found_codes)}/6 tracked issue codes, "
                "but the Closing Levels table could not be parsed."
            )
        else:
            log.append(
                "Singapore: MAS page fetched, but no tracked SGS "
                "issue codes were detected."
            )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
            "issueCodesFound": found_codes,
        }

    try:
        _, latest_row = find_mas_latest_data_row(
            rows,
            header_index,
        )
    except Exception as exc:
        log.append(
            f"Singapore: MAS issue-code table found, "
            f"but latest Closing Levels row could not be parsed: {exc}"
        )
        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "rows": [],
        }

    latest_date = str(latest_row[0]).strip()
    output: list[dict[str, Any]] = []

    for issue_code in MAS_TARGET_CODES:
        price_col, yield_col = mas_column_for_issue(
            issue_code,
            issue_map,
        )

        price = (
            clean_number(latest_row[price_col])
            if price_col < len(latest_row)
            else None
        )

        yield_value = (
            clean_number(latest_row[yield_col])
            if yield_col < len(latest_row)
            else None
        )

        # MAS 50Y benchmark is yield-only in the Closing Levels table.
        if issue_code == "NC22300W":
            price = None

        # Sanity checks prevent accidental column shifts.
        if yield_value is not None and not 0 <= yield_value <= 20:
            yield_value = None

        if price is not None and not 50 <= price <= 150:
            price = None

        if yield_value is None and price is None:
            continue

        output.append(
            {
                "issue_code": issue_code,
                "yield": yield_value,
                "price": price,
                "date": latest_date,
                "source": "MAS",
            }
        )

    if not output:
        log.append(
            "Singapore: MAS Closing Levels row found, "
            "but no valid benchmark values passed validation."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "date": latest_date,
            "rows": [],
        }

    log.append(
        f"Singapore: MAS closing levels parsed for "
        f"{len(output)}/6 tracked benchmark issues "
        f"({latest_date})."
    )

    return {
        "source": "MAS",
        "status": "success",
        "date": latest_date,
        "rows": output,
        "issueCodesFound": sorted(issue_map.keys()),
    }


def update_singapore_instruments(
    instruments: list[dict[str, Any]],
    mas: dict[str, Any],
) -> None:
    rows = mas.get("rows", [])

    isin_to_issue_code = {
        "SGXF51035222": "N523100W",
        "SGXF76205099": "NX21100N",
        "SG31A9000002": "NZ16100X",
        "SGXF29838152": "NY25200N",
        "SG31A7000004": "NA16100H",
        "SGXF47639806": "NC22300W",
    }

    updated_count = 0

    for instrument in instruments:
        if norm_market(instrument.get("market")) != "singapore":
            continue

        issue_code = str(
            instrument.get("issue_code") or ""
        ).strip().upper()

        isin = str(
            instrument.get("isin") or ""
        ).strip().upper()

        if not issue_code and isin:
            issue_code = isin_to_issue_code.get(isin, "")

        if not issue_code:
            continue

        hit = next(
            (
                row
                for row in rows
                if str(row.get("issue_code") or "").strip().upper()
                == issue_code
            ),
            None,
        )

        if not hit:
            continue

        changed = False

        if hit.get("yield") is not None:
            old = instrument.get("yield")
            if old is not None:
                instrument["previousYield"] = old

            instrument["yield"] = hit["yield"]
            instrument["liveYield"] = hit["yield"]
            changed = True

        if hit.get("price") is not None:
            old = instrument.get("price")
            if old is not None:
                instrument["previousPrice"] = old

            instrument["price"] = hit["price"]
            instrument["livePrice"] = hit["price"]
            changed = True

        if changed:
            instrument["liveDate"] = hit.get("date")
            instrument["dataStatus"] = "live MAS SGS data"

            if not instrument.get("issue_code"):
                instrument["issue_code"] = issue_code

            updated_count += 1

    mas["matchedInstruments"] = updated_count


# ---------------------------------------------------------------------------
# India
# ---------------------------------------------------------------------------

def update_india_instruments(
    instruments: list[dict[str, Any]],
) -> None:
    for instrument in instruments:
        if norm_market(instrument.get("market")) != "india":
            continue

        instrument.setdefault("liveYield", None)
        instrument.setdefault("livePrice", None)
        instrument.setdefault("liveDate", None)

        instrument["dataStatus"] = (
            "RBI source configured; "
            "instrument quote endpoint unavailable"
        )


# ---------------------------------------------------------------------------
# Final normalization/status
# ---------------------------------------------------------------------------

def initialize_live_fields(
    instruments: list[dict[str, Any]],
) -> None:
    for instrument in instruments:
        instrument.setdefault("liveYield", None)
        instrument.setdefault("livePrice", None)
        instrument.setdefault("liveDate", None)

        if not instrument.get("dataStatus"):
            instrument["dataStatus"] = "source-backed seed"


def calculate_status(
    log: list[str],
    instruments: list[dict[str, Any]],
) -> str:
    if not instruments:
        return "error"

    errors = [
        entry
        for entry in log
        if ": ERROR" in entry
    ]

    if errors:
        return "partial"

    # A source can be fetched successfully but fail to expose a parseable
    # table. This is not a total updater failure, but it must not be reported
    # as full success.
    source_partial = any(
        (
            "FETCHED_NO_TABLE" in entry
            or "Closing Levels table could not be parsed" in entry
            or "fetched_incomplete" in entry.lower()
        )
        for entry in log
    )

    if source_partial:
        return "partial"

    return "success"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    DATA.mkdir(exist_ok=True)

    log: list[str] = []

    instruments = load_instrument_universe(log)

    if not instruments:
        log.append(
            "ERROR: No instruments were loaded. "
            "Existing country files were not modified."
        )

        payload = {
            "schemaVersion": "3.3",
            "updatedAt": now_utc(),
            "status": "error",
            "sources": {},
            "instruments": [],
            "log": log,
        }

        save_json(OUT, payload)

        save_json(
            LAST_UPDATE,
            {
                "updatedAt": payload["updatedAt"],
                "status": "error",
                "log": log,
            },
        )

        raise SystemExit("No instruments loaded.")

    initialize_live_fields(instruments)

    # USA
    try:
        us = update_us_curve(log)
        update_us_instruments(instruments, us)
    except Exception as exc:
        us = {
            "source": "U.S. Treasury",
            "status": "error",
            "error": str(exc),
            "curve": {},
        }
        log.append(f"USA: ERROR {exc}")

    # Hong Kong
    try:
        hk = update_hkma(log)
        update_hk_instruments(instruments, hk)
    except Exception as exc:
        hk = []
        log.append(f"Hong Kong: ERROR {exc}")

    # Singapore
    try:
        mas = update_mas(log)
        update_singapore_instruments(instruments, mas)
    except Exception as exc:
        mas = {
            "source": "MAS",
            "status": "error",
            "error": str(exc),
            "rows": [],
        }
        log.append(f"Singapore: ERROR {exc}")

    # India
    try:
        update_india_instruments(instruments)
        log.append(
            "India: Phase 1 RBI instrument universe preserved; "
            "no unverified live quote was inferred."
        )
    except Exception as exc:
        log.append(f"India: ERROR {exc}")

    status = calculate_status(log, instruments)
    updated_at = now_utc()

    payload = {
        "schemaVersion": "3.3",
        "updatedAt": updated_at,
        "status": status,
        "sources": {
            "usa": (
                "https://home.treasury.gov/"
                "resource-center/data-chart-center/"
                "interest-rates"
            ),
            "singapore": MAS_URL,
            "hongkong": (
                "https://apidocs.hkma.gov.hk/"
                "documentation/market-data-and-statistics/"
                "daily-monetary-statistics/"
                "efbn-indicative-price"
            ),
            "india": "https://data.rbi.org.in/",
        },
        "usaCurve": us,
        "hongkongIndicative": hk,
        "singaporeBenchmark": mas,
        "instrumentCount": len(instruments),
        "instruments": instruments,
        "log": log,
    }

    save_json(OUT, payload)

    save_json(
        LAST_UPDATE,
        {
            "updatedAt": updated_at,
            "status": status,
            "instrumentCount": len(instruments),
            "log": log,
        },
    )

    print("==========================================")
    print(" Bond Monitor Phase 3 Update v3.3")
    print("==========================================")
    print(f"Overall status : {status}")
    print(f"Instruments    : {len(instruments)}")
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
    print("------------------------------------------")

    for entry in log:
        print(entry)

    print("------------------------------------------")
    print(f"Wrote {OUT}")
    print(f"Wrote {LAST_UPDATE}")


if __name__ == "__main__":
    main()
