#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater.

Purpose:
- Fetch machine-readable market data from official/public sources where available.
- Keep the existing instrument universe intact.
- Write normalized data/live.json consumed by the dashboard.
- Never invent a price or yield when the source does not provide one.

Sources:
- U.S. Treasury daily Treasury par yield curve XML.
- HKMA EFBN indicative price API.
- MAS Daily SGS Prices page.
- India: preserve instrument universe and source metadata; no fabricated
  instrument-level prices/yields.

Run locally:
    python scripts/update_bonds.py
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths / HTTP configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "live.json"

UA = "bond-monitor/3.1 (+GitHub Actions; public market-data updater)"

CTX = ssl.create_default_context()


# ---------------------------------------------------------------------------
# Official source URLs
# ---------------------------------------------------------------------------

TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml"
    "?data=daily_treasury_yield_curve"
    f"&field_tdr_date_value={datetime.now(timezone.utc).year}"
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

def fetch(url: str, timeout: int = 30) -> bytes:
    """Fetch a URL using urllib only."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
        },
    )

    with urllib.request.urlopen(
        req,
        timeout=timeout,
        context=CTX,
    ) as response:
        return response.read()


def load_json(path: Path, default: Any) -> Any:
    """Load JSON safely."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    """Write formatted UTF-8 JSON."""
    path.write_text(
        json.dumps(
            obj,
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )


def norm_market(value: str) -> str:
    return str(value or "").strip().lower()


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def to_float(value: Any) -> float | None:
    """Convert a value to float without raising."""
    if value is None:
        return None

    text = clean_text(value)
    if not text:
        return None

    text = text.replace("%", "").replace(",", "")

    if text.upper() in {
        "N/A",
        "NA",
        "-",
        "--",
        "NULL",
        "NONE",
    }:
        return None

    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def load_existing_instruments() -> list[dict[str, Any]]:
    """
    Load the existing instrument universe.

    We deliberately ignore generated files so that live.json does not
    become the source of instruments for the next run.
    """

    instruments: list[dict[str, Any]] = []

    for path in sorted(DATA.glob("*.json")):
        if path.name in {
            "live.json",
            "last-update.json",
            "sources.json",
        }:
            continue

        obj = load_json(path, [])

        if isinstance(obj, dict):
            rows = (
                obj.get("instruments")
                or obj.get("bonds")
                or []
            )
        elif isinstance(obj, list):
            rows = obj
        else:
            rows = []

        for row in rows:
            if isinstance(row, dict):
                instruments.append(dict(row))

    return instruments


# ---------------------------------------------------------------------------
# USA - U.S. Treasury
# ---------------------------------------------------------------------------

TREASURY_FIELDS = {
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


def xml_local_name(tag: str) -> str:
    """
    Return XML local name regardless of namespace.

    Examples:
        {namespace}NEW_DATE -> NEW_DATE
        d:NEW_DATE         -> NEW_DATE
    """
    if "}" in tag:
        tag = tag.rsplit("}", 1)[-1]

    if ":" in tag:
        tag = tag.rsplit(":", 1)[-1]

    return tag


def treasury_record_to_dict(entry: ET.Element) -> dict[str, str]:
    """Flatten a Treasury XML entry into local-name -> text."""

    values: dict[str, str] = {}

    for element in entry.iter():
        name = xml_local_name(element.tag)

        if element is entry:
            continue

        if element.text is None:
            continue

        text = clean_text(element.text)

        if text:
            values[name] = text

    return values


def update_us_curve(log: list[str]) -> dict[str, Any]:
    """
    Read the latest U.S. Treasury par yield curve.

    The Treasury XML uses namespace-qualified fields such as:
        NEW_DATE
        BC_1MONTH
        BC_2YEAR
        BC_10YEAR
        BC_30YEAR

    We intentionally parse XML using the standard library only.
    """

    raw = fetch(TREASURY_URL)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(
            f"Treasury XML parse failed: {exc}"
        ) from exc

    entries: list[dict[str, str]] = []

    for element in root.iter():
        if xml_local_name(element.tag).lower() == "entry":
            record = treasury_record_to_dict(element)

            if record:
                entries.append(record)

    # Fallback: some feed variants may not expose Atom <entry> cleanly.
    if not entries:
        all_records: list[dict[str, str]] = []

        current: dict[str, str] = {}

        for element in root.iter():
            name = xml_local_name(element.tag)

            if name == "NEW_DATE":
                if current:
                    all_records.append(current)
                    current = {}

                current["NEW_DATE"] = clean_text(element.text)

            elif name.startswith("BC_"):
                if element.text:
                    current[name] = clean_text(element.text)

        if current:
            all_records.append(current)

        entries = all_records

    dated_entries: list[tuple[str, dict[str, str]]] = []

    for record in entries:
        date_value = (
            record.get("NEW_DATE")
            or record.get("NEW_DATE_x")
            or record.get("NEW_DATE_y")
        )

        if date_value:
            dated_entries.append(
                (clean_text(date_value), record)
            )

    if not dated_entries:
        raise RuntimeError(
            "Treasury feed returned no dated records"
        )

    # Treasury dates are MM/DD/YYYY.
    # ISO conversion gives us reliable chronological sorting.
    def treasury_date_key(item: tuple[str, dict[str, str]]) -> datetime:
        value = item[0]

        for fmt in (
            "%m/%d/%Y",
            "%Y-%m-%d",
            "%m-%d-%Y",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                pass

        return datetime.min

    dated_entries.sort(key=treasury_date_key)

    latest_date, latest_record = dated_entries[-1]

    curve: dict[str, float] = {}

    for label, field_name in TREASURY_FIELDS.items():
        value = to_float(latest_record.get(field_name))

        if value is not None:
            curve[label] = value

    if not curve:
        available_fields = sorted(
            key
            for key in latest_record.keys()
            if key.startswith("BC_")
        )

        raise RuntimeError(
            "Treasury latest record found, but no yield-curve fields "
            f"were parsed. Available BC fields: {available_fields}"
        )

    log.append(
        f"USA: Treasury curve updated for {latest_date}; "
        f"{len(curve)} tenors."
    )

    return {
        "source": "U.S. Treasury",
        "status": "ok",
        "date": latest_date,
        "curve": curve,
    }


# ---------------------------------------------------------------------------
# Hong Kong - HKMA
# ---------------------------------------------------------------------------

def update_hk(log: list[str]) -> list[dict[str, Any]]:
    """Read latest HKMA EFBN indicative prices."""

    raw = fetch(HKMA_URL)

    try:
        obj = json.loads(
            raw.decode("utf-8")
        )
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"HKMA JSON parse failed: {exc}"
        ) from exc

    result = obj.get("result") or {}

    rows = (
        result.get("records")
        or result.get("data")
        or []
    )

    if not rows:
        log.append(
            "Hong Kong: HKMA returned no indicative-price rows."
        )
        return []

    output: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        output.append(
            {
                "date": row.get("end_of_date"),
                "term": row.get("term"),
                "issue_no": (
                    row.get("issue_no")
                    or row.get("issueNo")
                ),
                "yield": to_float(row.get("yield")),
                "price": to_float(row.get("price")),
                "source": "HKMA",
            }
        )

    log.append(
        f"Hong Kong: HKMA returned "
        f"{len(output)} latest-business-day rows."
    )

    return output


# ---------------------------------------------------------------------------
# Singapore - MAS HTML parser
# ---------------------------------------------------------------------------

class TableParser(HTMLParser):
    """
    Small dependency-free HTML table parser.

    We keep the parser deliberately generic because the MAS page uses
    nested tables and changes markup periodically.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)

        self.rows: list[list[str]] = []

        self._inside_row = False
        self._inside_cell = False
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

        self._skip_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()

        if tag in ("script", "style"):
            self._skip_depth += 1
            return

        if self._skip_depth:
            return

        if tag == "tr":
            self._inside_row = True
            self._current_row = []

        elif tag in ("td", "th") and self._inside_row:
            self._inside_cell = True
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in ("script", "style"):
            if self._skip_depth:
                self._skip_depth -= 1
            return

        if self._skip_depth:
            return

        if tag in ("td", "th"):
            if self._inside_cell:
                text = clean_text(
                    "".join(self._current_cell)
                )

                self._current_row.append(text)

            self._inside_cell = False
            self._current_cell = []

        elif tag == "tr":
            if self._current_row:
                self.rows.append(
                    list(self._current_row)
                )

            self._inside_row = False
            self._current_row = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return

        if self._inside_cell:
            self._current_cell.append(data)


MAS_ISSUE_CODE_RE = re.compile(
    r"^[A-Z]{2}\d{5,6}[A-Z]$"
)

MAS_DATE_RE = re.compile(
    r"^\d{1,2}\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+\d{4}$",
    re.IGNORECASE,
)


def find_mas_issue_codes(
    rows: list[list[str]],
) -> list[str]:
    """
    Find the current MAS issue-code sequence.

    Example:
        BS26115N
        BY26102T
        N523100W
        NX21100N
        ...
    """

    candidates: list[list[str]] = []

    for row in rows:
        codes = [
            clean_text(cell)
            for cell in row
            if MAS_ISSUE_CODE_RE.match(
                clean_text(cell)
            )
        ]

        if len(codes) >= 5:
            candidates.append(codes)

    if not candidates:
        return []

    # The Closing Levels table appears before the High/Low table.
    # We use the first substantial issue-code row.
    return candidates[0]


def find_mas_closing_rows(
    rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    """
    Locate the Closing Levels section and return:

        (issue_codes, daily_rows)

    A MAS closing row looks conceptually like:

        Date | TBillYield | TBillYield |
             Price | Yield | Price | Yield | ...

    We don't hard-code today's date.
    """

    issue_codes = find_mas_issue_codes(rows)

    if not issue_codes:
        return [], []

    # Locate the row containing the Yield/Price column headers.
    header_index: int | None = None

    for index, row in enumerate(rows):
        normalized = [
            clean_text(cell).lower()
            for cell in row
        ]

        yield_count = sum(
            1 for cell in normalized
            if cell == "yield"
        )

        price_count = sum(
            1 for cell in normalized
            if cell == "price"
        )

        if yield_count >= 2 and price_count >= 1:
            header_index = index
            break

    if header_index is None:
        return issue_codes, []

    daily_rows: list[list[str]] = []

    for row in rows[header_index + 1:]:
        if not row:
            continue

        first = clean_text(row[0])

        if MAS_DATE_RE.match(first):
            daily_rows.append(row)

        # Stop when the page moves to High / Low Levels.
        if first.lower() in {
            "high",
            "high / low levels",
        }:
            break

    return issue_codes, daily_rows


def parse_mas_value(
    row: list[str],
    column_index: int,
) -> float | None:
    if column_index >= len(row):
        return None

    return to_float(row[column_index])


def update_mas(log: list[str]) -> dict[str, Any]:
    """
    Parse the current MAS SGS Closing Levels table.

    The current MAS table contains:
        - 2 Treasury-bill columns
        - bond Price/Yield pairs

    We map values according to the live issue-code order published by MAS.
    """

    html = fetch(MAS_URL).decode(
        "utf-8",
        errors="replace",
    )

    parser = TableParser()
    parser.feed(html)

    if not parser.rows:
        log.append(
            "Singapore: MAS page fetched, but no HTML table rows found."
        )

        return {
            "source": "MAS",
            "status": "fetched",
            "rows": [],
        }

    issue_codes, daily_rows = find_mas_closing_rows(
        parser.rows
    )

    if not issue_codes:
        log.append(
            "Singapore: MAS page fetched, but no SGS issue codes "
            "were detected."
        )

        return {
            "source": "MAS",
            "status": "fetched",
            "rows": [],
        }

    if not daily_rows:
        log.append(
            "Singapore: MAS page fetched, issue codes found, "
            "but no Closing Levels rows were detected."
        )

        return {
            "source": "MAS",
            "status": "fetched",
            "issueCodes": issue_codes,
            "rows": [],
        }

    latest_row = daily_rows[-1]
    latest_date = clean_text(latest_row[0])

    output: list[dict[str, Any]] = []

    # First two columns are Treasury Bill yields.
    # Remaining columns are Price/Yield pairs for bonds.
    #
    # Example:
    #
    #   col 0 = date
    #   col 1 = 6M T-bill yield
    #   col 2 = 1Y T-bill yield
    #   col 3 = 2Y bond price
    #   col 4 = 2Y bond yield
    #   col 5 = 5Y bond price
    #   col 6 = 5Y bond yield
    #   ...
    #
    # This matches the current MAS Closing Levels layout.

    for issue_index, issue_code in enumerate(issue_codes):
        if issue_index < 2:
            # Treasury bill instruments.
            column_index = 1 + issue_index

            value = parse_mas_value(
                latest_row,
                column_index,
            )

            output.append(
                {
                    "issueCode": issue_code,
                    "date": latest_date,
                    "yield": value,
                    "price": None,
                    "source": "MAS",
                }
            )

        else:
            bond_index = issue_index - 2

            price_column = 3 + (
                bond_index * 2
            )

            yield_column = price_column + 1

            price = parse_mas_value(
                latest_row,
                price_column,
            )

            yield_value = parse_mas_value(
                latest_row,
                yield_column,
            )

            output.append(
                {
                    "issueCode": issue_code,
                    "date": latest_date,
                    "yield": yield_value,
                    "price": price,
                    "source": "MAS",
                }
            )

    usable = [
        row
        for row in output
        if row.get("yield") is not None
        or row.get("price") is not None
    ]

    if not usable:
        log.append(
            "Singapore: MAS page parsed, but no usable "
            "yield/price values were found."
        )

        return {
            "source": "MAS",
            "status": "fetched",
            "issueCodes": issue_codes,
            "rows": [],
        }

    log.append(
        f"Singapore: MAS benchmark data updated for "
        f"{latest_date}; {len(usable)} instruments."
    )

    return {
        "source": "MAS",
        "status": "ok",
        "date": latest_date,
        "issueCodes": issue_codes,
        "rows": usable,
    }


# ---------------------------------------------------------------------------
# Instrument matching
# ---------------------------------------------------------------------------

def get_issue_number(instrument: dict[str, Any]) -> str:
    """
    Support multiple naming conventions used by the existing data files.
    """

    value = (
        instrument.get("issueNo")
        or instrument.get("issue_no")
        or instrument.get("issueCode")
        or instrument.get("issue_code")
        or instrument.get("isin")
        or ""
    )

    return clean_text(value)


def get_maturity_years(
    instrument: dict[str, Any],
) -> float | None:
    """
    Try to derive maturity in years from existing instrument metadata.

    This is currently informational and intentionally does not infer a
    Treasury quote for a specific instrument.
    """

    maturity = (
        instrument.get("maturity")
        or instrument.get("maturityDate")
        or instrument.get("maturity_date")
    )

    if not maturity:
        return None

    text = clean_text(maturity)

    for fmt in (
        "%Y-%m-%d",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            maturity_date = datetime.strptime(
                text,
                fmt,
            )

            now = datetime.now()

            return (
                maturity_date - now
            ).days / 365.25

        except ValueError:
            continue

    return None


def merge_market_values(
    instruments: list[dict[str, Any]],
    us: dict[str, Any],
    hk: list[dict[str, Any]],
    mas: dict[str, Any],
    log: list[str],
) -> list[dict[str, Any]]:
    """
    Attach live source-backed values to the existing instrument universe.

    Important:
    - No value is fabricated.
    - USA Treasury par curve is kept separately.
    - HKMA is matched by issue number.
    - MAS is matched by issue code.
    """

    hk_lookup: dict[str, dict[str, Any]] = {}

    for row in hk:
        issue = clean_text(
            row.get("issue_no")
        )

        if issue:
            hk_lookup[issue.upper()] = row

    mas_lookup: dict[str, dict[str, Any]] = {}

    for row in mas.get("rows", []):
        issue = clean_text(
            row.get("issueCode")
        )

        if issue:
            mas_lookup[issue.upper()] = row

    usa_ok = (
        isinstance(us, dict)
        and us.get("status") == "ok"
    )

    mas_ok = (
        isinstance(mas, dict)
        and mas.get("status") == "ok"
    )

    for instrument in instruments:
        instrument["liveYield"] = None
        instrument["livePrice"] = None
        instrument["liveDate"] = None
        instrument["dataStatus"] = (
            "source-backed / quote unavailable"
        )

        market = norm_market(
            instrument.get("market")
        )

        issue = get_issue_number(
            instrument
        ).upper()

        # ---------------------------------------------------------------
        # Hong Kong
        # ---------------------------------------------------------------

        if market in {
            "hong kong",
            "hongkong",
            "hk",
        }:
            hit = hk_lookup.get(issue)

            if hit:
                instrument["liveYield"] = hit.get(
                    "yield"
                )

                instrument["livePrice"] = hit.get(
                    "price"
                )

                instrument["liveDate"] = hit.get(
                    "date"
                )

                instrument["dataStatus"] = (
                    "live latest business day"
                )

            else:
                instrument["dataStatus"] = (
                    "HKMA source available; instrument quote not found"
                )

        # ---------------------------------------------------------------
        # Singapore
        # ---------------------------------------------------------------

        elif market == "singapore":
            hit = mas_lookup.get(issue)

            if hit:
                instrument["liveYield"] = hit.get(
                    "yield"
                )

                instrument["livePrice"] = hit.get(
                    "price"
                )

                instrument["liveDate"] = hit.get(
                    "date"
                )

                instrument["dataStatus"] = (
                    "live MAS closing level"
                )

            elif mas_ok:
                instrument["dataStatus"] = (
                    "MAS data available; instrument issue code not found"
                )

            else:
                instrument["dataStatus"] = (
                    "MAS source unavailable"
                )

        # ---------------------------------------------------------------
        # USA
        # ---------------------------------------------------------------

        elif market in {
            "united states",
            "united states of america",
            "usa",
            "us",
        }:
            if usa_ok:
                instrument["dataStatus"] = (
                    "Treasury par curve available; "
                    "instrument quote not inferred"
                )
            else:
                instrument["dataStatus"] = (
                    "Treasury source unavailable"
                )

        # ---------------------------------------------------------------
        # India
        # ---------------------------------------------------------------

        elif market == "india":
            instrument["dataStatus"] = (
                "India source universe; "
                "instrument quote endpoint not configured"
            )

    return instruments


# ---------------------------------------------------------------------------
# Overall status
# ---------------------------------------------------------------------------

def determine_overall_status(
    us: dict[str, Any],
    hk: list[dict[str, Any]],
    mas: dict[str, Any],
) -> str:
    """
    Determine whether this run was complete or partial.

    India is intentionally excluded because it is not yet an automated
    live-price source.
    """

    usa_ok = (
        isinstance(us, dict)
        and us.get("status") == "ok"
    )

    hk_ok = len(hk) > 0

    mas_ok = (
        isinstance(mas, dict)
        and mas.get("status") == "ok"
        and len(mas.get("rows", [])) > 0
    )

    if usa_ok and hk_ok and mas_ok:
        return "full"

    if usa_ok or hk_ok or mas_ok:
        return "partial"

    return "error"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    DATA.mkdir(
        exist_ok=True
    )

    log: list[str] = []

    instruments = load_existing_instruments()

    log.append(
        f"Loaded {len(instruments)} instruments "
        "from existing data files."
    )

    # -------------------------------------------------------------------
    # USA
    # -------------------------------------------------------------------

    try:
        us = update_us_curve(log)

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

    # -------------------------------------------------------------------
    # Hong Kong
    # -------------------------------------------------------------------

    try:
        hk = update_hk(log)

    except Exception as exc:
        hk = []

        log.append(
            f"Hong Kong: ERROR {exc}"
        )

    # -------------------------------------------------------------------
    # Singapore
    # -------------------------------------------------------------------

    try:
        mas = update_mas(log)

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

    # -------------------------------------------------------------------
    # Merge source-backed values
    # -------------------------------------------------------------------

    instruments = merge_market_values(
        instruments,
        us,
        hk,
        mas,
        log,
    )

    # -------------------------------------------------------------------
    # Overall status
    # -------------------------------------------------------------------

    overall_status = determine_overall_status(
        us,
        hk,
        mas,
    )

    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    # -------------------------------------------------------------------
    # Final normalized payload
    # -------------------------------------------------------------------

    payload = {
        "schemaVersion": "3.1",
        "updatedAt": now,
        "status": overall_status,

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
            "india": (
                "https://data.rbi.org.in/"
            ),
        },

        "usaCurve": us,

        "hongkongIndicative": hk,

        "singaporeBenchmark": mas,

        "instruments": instruments,

        "log": log,
    }

    save_json(
        OUT,
        payload,
    )

    save_json(
        DATA / "last-update.json",
        {
            "updatedAt": now,
            "status": overall_status,
            "log": log,
        },
    )

    # -------------------------------------------------------------------
    # Console output for GitHub Actions
    # -------------------------------------------------------------------

    print("")
    print("==========================================")
    print(" Bond Monitor Phase 3 Update")
    print("==========================================")
    print(
        f"Overall status : {overall_status}"
    )
    print(
        f"Instruments    : {len(instruments)}"
    )
    print(
        f"USA curve      : "
        f"{'OK' if us.get('status') == 'ok' else 'ERROR'}"
    )
    print(
        f"Hong Kong      : "
        f"{'OK' if hk else 'ERROR'}"
    )
    print(
        f"Singapore      : "
        f"{'OK' if mas.get('status') == 'ok' else 'ERROR'}"
    )
    print("------------------------------------------")

    for message in log:
        print(message)

    print("------------------------------------------")
    print(
        f"Wrote {OUT}"
    )
    print(
        f"Wrote {DATA / 'last-update.json'}"
    )
    print("==========================================")


if __name__ == "__main__":
    main()
