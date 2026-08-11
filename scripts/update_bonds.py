#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.2

Purpose
-------

- Load the original Bond Monitor instrument universe from:
    data/usa.json
    data/singapore.json
    data/hongkong.json
    data/india.json

- Preserve all instruments even when an external source fails.
- Update live values only when explicitly available from official sources.
- Never fabricate a price or yield.
- Never replace an existing value with null because of a temporary
  source failure.
- Write normalized:
    data/live.json
    data/last-update.json

Official sources
----------------

USA:
    U.S. Treasury daily Treasury par yield curve

Singapore:
    Monetary Authority of Singapore SGS benchmark closing levels

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


# ============================================================================
# PATHS
# ============================================================================

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

OUT = DATA / "live.json"
LAST_UPDATE = DATA / "last-update.json"


# ============================================================================
# HTTP CONFIGURATION
# ============================================================================

UA = (
    "bond-monitor/3.2 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

HTTP_TIMEOUT = 90
HTTP_RETRIES = 3
HTTP_RETRY_DELAY = 3


# ============================================================================
# OFFICIAL SOURCE URLS
# ============================================================================

TREASURY_URL = (
    "https://home.treasury.gov/"
    "resource-center/data-chart-center/"
    "interest-rates/pages/xml"
)

HKMA_URL = (
    "https://api.hkma.gov.hk/"
    "public/market-data-and-statistics/"
    "daily-monetary-statistics/efbn-indicative-price"
    "?segment=IndicativePrice&offset=0"
)

# IMPORTANT:
# This is the MAS SGS benchmark page.
#
# Do NOT change this to the generic BondPricesAndYields.aspx page unless
# the MAS site changes its structure again.
#
# Official MAS page:
# https://eservices.mas.gov.sg/Statistics/fdanet/SgsBenchmarkIssuePrices.aspx
MAS_URL = (
    "https://eservices.mas.gov.sg/"
    "Statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx"
)


# ============================================================================
# GENERIC HELPERS
# ============================================================================

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
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml,text/xml,*/*"
                    ),
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


def load_json(
    path: Path,
    default: Any,
) -> Any:

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return default


def save_json(
    path: Path,
    obj: Any,
) -> None:

    path.write_text(
        json.dumps(
            obj,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def norm_market(
    value: Any,
) -> str:

    return str(
        value or ""
    ).strip().lower()


def clean_number(
    value: Any,
) -> float | None:

    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = (
        text
        .replace(",", "")
        .replace("%", "")
        .replace("—", "")
        .replace("-", "")
        .strip()
    )

    if not text:
        return None

    try:
        return float(text)

    except ValueError:
        return None


# ============================================================================
# PHASE 1 INSTRUMENT UNIVERSE
# ============================================================================

COUNTRY_FILES = {
    "United States": DATA / "usa.json",
    "Singapore": DATA / "singapore.json",
    "Hong Kong": DATA / "hongkong.json",
    "India": DATA / "india.json",
}


def load_instrument_universe(
    log: list[str],
) -> list[dict[str, Any]]:

    instruments: list[dict[str, Any]] = []

    for market, path in COUNTRY_FILES.items():

        if not path.exists():

            log.append(
                f"{market}: ERROR missing {path.name}"
            )

            continue

        obj = load_json(
            path,
            {},
        )

        if not isinstance(obj, dict):

            log.append(
                f"{market}: ERROR {path.name} "
                "is not a JSON object"
            )

            continue

        records = obj.get(
            "records",
            [],
        )

        if not isinstance(records, list):

            log.append(
                f"{market}: ERROR records[] "
                f"missing in {path.name}"
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
            f"{market}: loaded {count} "
            f"instruments from {path.name}"
        )

    log.append(
        f"Loaded {len(instruments)} instruments "
        "from Phase 1 country files."
    )

    return instruments


# ============================================================================
# INITIAL LIVE FIELDS
# ============================================================================

def initialize_live_fields(
    instruments: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

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


# ============================================================================
# USA TREASURY
# ============================================================================

def update_us_curve(
    log: list[str],
) -> dict[str, Any]:

    year = datetime.now(
        timezone.utc
    ).year

    url = (
        TREASURY_URL
        + "?data=daily_treasury_yield_curve"
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

    date_patterns = [
        r"<d:NEW_DATE[^>]*>(.*?)</d:NEW_DATE>",
        r"<NEW_DATE[^>]*>(.*?)</NEW_DATE>",
    ]

    dates: list[str] = []

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

    pos = raw.rfind(
        latest
    )

    if pos < 0:

        raise RuntimeError(
            "Treasury latest-date record "
            "could not be located"
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

    if (
        entry_start >= 0
        and entry_end >= 0
    ):

        block = raw[
            entry_start:
            entry_end
            + len("</entry>")
        ]

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

    curve = us.get(
        "curve",
        {}
    )

    date = us.get(
        "date"
    )

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

        tenor = maturity_map.get(
            bond
        )

        if not tenor:
            continue

        value = curve.get(
            tenor
        )

        if value is None:
            continue

        instrument["previousYield"] = (
            instrument.get("yield")
        )

        instrument["yield"] = value
        instrument["liveYield"] = value
        instrument["liveDate"] = date

        instrument["dataStatus"] = (
            "live U.S. Treasury benchmark yield"
        )


# ============================================================================
# HONG KONG HKMA
# ============================================================================

def update_hkma(
    log: list[str],
) -> list[dict[str, Any]]:

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

    result = (
        obj.get("result")
        or {}
    )

    rows = (
        result.get("records")
        or result.get("data")
        or []
    )

    if not isinstance(
        rows,
        list,
    ):

        raise RuntimeError(
            "HKMA response did not contain records"
        )

    output: list[dict[str, Any]] = []

    for row in rows:

        if not isinstance(
            row,
            dict,
        ):
            continue

        output.append(
            {
                "date": row.get(
                    "end_of_date"
                ),
                "term": row.get(
                    "term"
                ),
                "issue_no": row.get(
                    "issue_no"
                ),
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

        hit = None

        if (
            isin
            and isin != "—"
        ):

            hit = next(
                (
                    row
                    for row in hk_rows
                    if str(
                        row.get(
                            "issue_no"
                        )
                        or ""
                    )
                    == isin
                ),
                None,
            )

        if hit is None:

            bond_lower = bond.lower()

            if (
                "exchange fund note"
                in bond_lower
            ):

                hit = next(
                    (
                        row
                        for row in hk_rows
                        if "note"
                        in str(
                            row.get(
                                "term"
                            )
                            or ""
                        ).lower()
                    ),
                    None,
                )

            elif (
                "exchange fund bill"
                in bond_lower
            ):

                hit = next(
                    (
                        row
                        for row in hk_rows
                        if "bill"
                        in str(
                            row.get(
                                "term"
                            )
                            or ""
                        ).lower()
                    ),
                    None,
                )

        if not hit:
            continue

        changed = False

        if hit.get(
            "yield"
        ) is not None:

            instrument["previousYield"] = (
                instrument.get("yield")
            )

            instrument["yield"] = (
                hit["yield"]
            )

            instrument["liveYield"] = (
                hit["yield"]
            )

            changed = True

        if hit.get(
            "price"
        ) is not None:

            instrument["previousPrice"] = (
                instrument.get("price")
            )

            instrument["price"] = (
                hit["price"]
            )

            instrument["livePrice"] = (
                hit["price"]
            )

            changed = True

        if changed:

            instrument["liveDate"] = (
                hit.get("date")
            )

            instrument["dataStatus"] = (
                "live HKMA indicative data"
            )


# ============================================================================
# SINGAPORE MAS HTML PARSER
# ============================================================================

class MASHTMLParser(
    HTMLParser
):

    def __init__(self) -> None:

        super().__init__()

        self.current_table: list[
            list[str]
        ] = []

        self.current_row: list[str] = []

        self.current_cell: list[str] = []

        self.in_cell = False

        self.tables: list[
            list[list[str]]
        ] = []

        self.in_table = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[
            tuple[str, str | None]
        ],
    ) -> None:

        tag = tag.lower()

        if tag == "table":

            self.in_table = True
            self.current_table = []

        elif (
            self.in_table
            and tag == "tr"
        ):

            self.current_row = []

        elif (
            self.in_table
            and tag in {
                "td",
                "th",
            }
        ):

            self.in_cell = True
            self.current_cell = []

    def handle_endtag(
        self,
        tag: str,
    ) -> None:

        tag = tag.lower()

        if (
            self.in_table
            and tag in {
                "td",
                "th",
            }
        ):

            if self.in_cell:

                text = " ".join(
                    "".join(
                        self.current_cell
                    ).split()
                )

                self.current_row.append(
                    text
                )

                self.in_cell = False

        elif (
            self.in_table
            and tag == "tr"
        ):

            if self.current_row:

                self.current_table.append(
                    self.current_row
                )

        elif (
            tag == "table"
            and self.in_table
        ):

            if self.current_table:

                self.tables.append(
                    self.current_table
                )

            self.current_table = []
            self.in_table = False

    def handle_data(
        self,
        data: str,
    ) -> None:

        if (
            self.in_table
            and self.in_cell
        ):

            self.current_cell.append(
                data
            )


# ============================================================================
# MAS HELPERS
# ============================================================================

MAS_TARGET_CODES = {
    "N523100W",
    "NX21100N",
    "NZ16100X",
    "NY25200N",
    "NA16100H",
    "NC22300W",
}


def normalize_mas_cell(
    value: Any,
) -> str:

    return " ".join(
        str(value or "")
        .strip()
        .upper()
        .split()
    )


def table_contains_mas_codes(
    table: list[list[str]],
) -> bool:

    for row in table:

        for cell in row:

            if (
                normalize_mas_cell(cell)
                in MAS_TARGET_CODES
            ):

                return True

    return False


def find_mas_closing_table(
    tables: list[list[list[str]]],
) -> list[list[str]] | None:

    candidates = []

    for table in tables:

        if not table_contains_mas_codes(
            table
        ):
            continue

        flattened = " ".join(
            normalize_mas_cell(cell)
            for row in table
            for cell in row
        )

        candidates.append(
            (
                table,
                flattened,
            )
        )

    if not candidates:
        return None

    # Prefer a table that contains both the target issue codes
    # and the Yield / Price headers.
    for table, flattened in candidates:

        if (
            "YIELD" in flattened
            and "PRICE" in flattened
        ):

            return table

    return candidates[0][0]


def parse_mas_closing_table(
    table: list[list[str]],
) -> tuple[
    str | None,
    dict[str, dict[str, float | None]]
]:

    """
    Parse the MAS SGS benchmark closing-level table.

    The official MAS page currently exposes:

        Closing Levels
        Treasury Bills / Bonds
        Issue Code
        Coupon Rate
        Maturity Date
        ...
        Yield / Price columns

    We do not hard-code today's market values.

    Instead we:
      1. identify the issue-code header row;
      2. identify the Yield / Price header row;
      3. identify the latest date/value row;
      4. map the tracked benchmark issue codes to columns.
    """

    issue_positions: dict[
        str,
        int,
    ] = {}

    issue_row_index = -1

    for row_index, row in enumerate(
        table
    ):

        for index, cell in enumerate(
            row
        ):

            code = normalize_mas_cell(
                cell
            )

            if code in MAS_TARGET_CODES:

                issue_positions[
                    code
                ] = index

                issue_row_index = max(
                    issue_row_index,
                    row_index,
                )

    if not issue_positions:

        return None, {}

    # ------------------------------------------------------------------
    # Find the header row containing Yield / Price.
    # ------------------------------------------------------------------

    value_header_index = -1

    for row_index, row in enumerate(
        table
    ):

        normalized = [
            normalize_mas_cell(cell)
            for cell in row
        ]

        if (
            "YIELD" in normalized
            or "PRICE" in normalized
        ):

            if row_index >= issue_row_index:

                value_header_index = (
                    row_index
                )

                break

    # ------------------------------------------------------------------
    # Find date/value rows after the header.
    #
    # MAS uses dates such as:
    # 11 Aug 2026
    # ------------------------------------------------------------------

    date_pattern = re.compile(
        r"^\d{1,2}\s+"
        r"[A-Za-z]{3,9}\s+"
        r"\d{4}$"
    )

    date_rows: list[
        tuple[
            int,
            str,
            list[str],
        ]
    ] = []

    search_start = max(
        value_header_index + 1,
        issue_row_index + 1,
    )

    for row_index in range(
        search_start,
        len(table),
    ):

        row = table[row_index]

        for cell_index, cell in enumerate(
            row
        ):

            text = " ".join(
                str(cell).split()
            )

            if date_pattern.match(
                text
            ):

                date_rows.append(
                    (
                        row_index,
                        text,
                        row,
                    )
                )

                break

    if not date_rows:

        return None, {}

    # The last date row is the latest row exposed by MAS.
    _, latest_date, latest_row = (
        date_rows[-1]
    )

    # ------------------------------------------------------------------
    # Build column/value mapping.
    #
    # The HTML parser gives us the actual table cells, so we don't
    # depend on a fixed count of whitespace-separated numbers.
    # ------------------------------------------------------------------

    results: dict[
        str,
        dict[str, float | None],
    ] = {}

    # Determine the position of the date in the latest row.
    date_index = -1

    for index, cell in enumerate(
        latest_row
    ):

        if date_pattern.match(
            " ".join(
                str(cell).split()
            )
        ):

            date_index = index
            break

    # ------------------------------------------------------------------
    # MAS benchmark order:
    #
    # 6M
    # 1Y
    # 2Y
    # 5Y
    # 10Y
    # 15Y
    # 20Y
    # 30Y
    # 50Y
    #
    # For benchmark bonds:
    #
    # 2Y  = Yield / Price
    # 5Y  = Yield / Price
    # 10Y = Yield / Price
    # 15Y = Yield / Price
    # 20Y = Yield / Price
    # 30Y = Yield / Price
    # 50Y = Yield
    #
    # We first try to use the table's physical column positions.
    # ------------------------------------------------------------------

    # Locate the "Yield / Price" sequence in the header.
    #
    # We create a list of header cells, then infer each target
    # issue's column from the issue-code row.

    header_row: list[str] = []

    if (
        value_header_index >= 0
        and value_header_index < len(table)
    ):

        header_row = table[
            value_header_index
        ]

    # ------------------------------------------------------------------
    # Primary strategy:
    #
    # The issue-code row and latest-value row generally have matching
    # positions. If so, use those positions directly.
    # ------------------------------------------------------------------

    for code, issue_index in issue_positions.items():

        if date_index < 0:
            continue

        # Difference between issue-code table position and latest
        # value row position can occur because of merged cells.
        #
        # First try direct position.
        candidate_indices = [
            issue_index,
            issue_index - 1,
            issue_index + 1,
        ]

        chosen_index = None

        for candidate in candidate_indices:

            if (
                0 <= candidate
                < len(latest_row)
            ):

                value = clean_number(
                    latest_row[candidate]
                )

                if value is not None:

                    chosen_index = candidate
                    break

        if chosen_index is None:
            continue

        first_value = clean_number(
            latest_row[chosen_index]
        )

        second_value = None

        if (
            chosen_index + 1
            < len(latest_row)
        ):

            second_value = clean_number(
                latest_row[
                    chosen_index + 1
                ]
            )

        # For a bond benchmark the first number is normally yield
        # and the second number is price.
        #
        # Validate before accepting.
        if (
            first_value is not None
            and 0 <= first_value <= 20
        ):

            result = {
                "yield": first_value,
                "price": None,
            }

            if (
                second_value is not None
                and 50 <= second_value <= 150
            ):

                result["price"] = (
                    second_value
                )

            results[code] = result

    # ------------------------------------------------------------------
    # Fallback strategy:
    #
    # If physical table positions did not produce enough values,
    # parse the latest row's numeric cells and map them according
    # to the official benchmark order.
    # ------------------------------------------------------------------

    if len(results) < len(
        MAS_TARGET_CODES
    ):

        numeric_values: list[
            float
        ] = []

        for index, cell in enumerate(
            latest_row
        ):

            if index == date_index:
                continue

            value = clean_number(
                cell
            )

            if value is not None:

                numeric_values.append(
                    value
                )

        # A normal MAS closing row has:
        #
        # 6M yield
        # 1Y yield
        # 2Y yield
        # 2Y price
        # 5Y yield
        # 5Y price
        # 10Y yield
        # 10Y price
        # 15Y yield
        # 15Y price
        # 20Y yield
        # 20Y price
        # 30Y yield
        # 30Y price
        # 50Y yield
        #
        if len(numeric_values) >= 15:

            fallback = {
                "N523100W": {
                    "yield": numeric_values[2],
                    "price": numeric_values[3],
                },
                "NX21100N": {
                    "yield": numeric_values[4],
                    "price": numeric_values[5],
                },
                "NZ16100X": {
                    "yield": numeric_values[6],
                    "price": numeric_values[7],
                },
                "NY25200N": {
                    "yield": numeric_values[8],
                    "price": numeric_values[9],
                },
                "NA16100H": {
                    "yield": numeric_values[12],
                    "price": numeric_values[13],
                },
                "NC22300W": {
                    "yield": numeric_values[14],
                    "price": None,
                },
            }

            for code, values in fallback.items():

                y = values.get(
                    "yield"
                )

                p = values.get(
                    "price"
                )

                if (
                    y is not None
                    and not 0 <= y <= 20
                ):

                    y = None

                if (
                    p is not None
                    and not 50 <= p <= 150
                ):

                    p = None

                if (
                    y is not None
                    or p is not None
                ):

                    results[code] = {
                        "yield": y,
                        "price": p,
                    }

    return latest_date, results


# ============================================================================
# SINGAPORE MAS
# ============================================================================

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

    # ------------------------------------------------------------------
    # Parse the actual HTML tables first.
    # ------------------------------------------------------------------

    parser = MASHTMLParser()

    try:

        parser.feed(
            html
        )

    except Exception as exc:

        log.append(
            "Singapore: MAS HTML parser error: "
            f"{exc}"
        )

    table = find_mas_closing_table(
        parser.tables
    )

    if table is None:

        # ------------------------------------------------------------------
        # Fallback: inspect raw HTML/page text.
        # ------------------------------------------------------------------

        plain_text = re.sub(
            r"<script.*?</script>",
            " ",
            html,
            flags=(
                re.IGNORECASE
                | re.DOTALL
            ),
        )

        plain_text = re.sub(
            r"<style.*?</style>",
            " ",
            plain_text,
            flags=(
                re.IGNORECASE
                | re.DOTALL
            ),
        )

        plain_text = re.sub(
            r"<[^>]+>",
            " ",
            plain_text,
        )

        plain_text = re.sub(
            r"\s+",
            " ",
            plain_text,
        ).strip()

        codes_found = [
            code
            for code in MAS_TARGET_CODES
            if code in plain_text.upper()
        ]

        if codes_found:

            log.append(
                "Singapore: MAS page fetched; "
                f"found {len(codes_found)}/"
                f"{len(MAS_TARGET_CODES)} "
                "tracked issue codes, but the "
                "Closing Levels table could not "
                "be parsed."
            )

        else:

            log.append(
                "Singapore: MAS page fetched, "
                "but SGS benchmark table could "
                "not be detected."
            )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
        }

    latest_date, values = (
        parse_mas_closing_table(
            table
        )
    )

    if not values:

        log.append(
            "Singapore: MAS Closing Levels "
            "table found, but no valid "
            "benchmark values were parsed."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "rows": [],
        }

    rows: list[
        dict[str, Any]
    ] = []

    for issue_code, value in values.items():

        if not isinstance(
            value,
            dict,
        ):
            continue

        yield_value = value.get(
            "yield"
        )

        price_value = value.get(
            "price"
        )

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
                "date": latest_date,
                "source": "MAS",
            }
        )

    if not rows:

        log.append(
            "Singapore: MAS Closing Levels "
            "table found, but no valid "
            "benchmark rows were extracted."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "rows": [],
        }

    log.append(
        "Singapore: MAS closing levels parsed "
        f"for {len(rows)}/6 tracked benchmark "
        f"issues ({latest_date})."
    )

    return {
        "source": "MAS",
        "status": "success",
        "date": latest_date,
        "rows": rows,
    }


def update_singapore_instruments(
    instruments: list[dict[str, Any]],
    mas: dict[str, Any],
) -> None:

    rows = mas.get(
        "rows",
        []
    )

    # ------------------------------------------------------------------
    # Phase 1 Singapore instrument mapping.
    #
    # The first two instruments already have ISINs.
    # The remaining benchmark records use the issue codes from MAS.
    # ------------------------------------------------------------------

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

        if norm_market(
            instrument.get("market")
        ) != "singapore":
            continue

        issue_code = str(
            instrument.get(
                "issue_code"
            )
            or ""
        ).strip().upper()

        isin = str(
            instrument.get(
                "isin"
            )
            or ""
        ).strip().upper()

        if (
            not issue_code
            and isin
        ):

            issue_code = (
                isin_to_issue_code.get(
                    isin,
                    "",
                )
            )

        if not issue_code:
            continue

        hit = next(
            (
                row
                for row in rows
                if str(
                    row.get(
                        "issue_code"
                    )
                    or ""
                ).strip().upper()
                == issue_code
            ),
            None,
        )

        if not hit:
            continue

        changed = False

        if hit.get(
            "yield"
        ) is not None:

            instrument["previousYield"] = (
                instrument.get(
                    "yield"
                )
            )

            instrument["yield"] = (
                hit["yield"]
            )

            instrument["liveYield"] = (
                hit["yield"]
            )

            changed = True

        if hit.get(
            "price"
        ) is not None:

            instrument["previousPrice"] = (
                instrument.get(
                    "price"
                )
            )

            instrument["price"] = (
                hit["price"]
            )

            instrument["livePrice"] = (
                hit["price"]
            )

            changed = True

        if changed:

            instrument["liveDate"] = (
                hit.get(
                    "date"
                )
            )

            instrument["dataStatus"] = (
                "live MAS SGS data"
            )

            if not instrument.get(
                "issue_code"
            ):

                instrument["issue_code"] = (
                    issue_code
                )

            updated_count += 1

    mas["matchedInstruments"] = (
        updated_count
    )


# ============================================================================
# INDIA
# ============================================================================

def update_india_instruments(
    instruments: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

        if norm_market(
            instrument.get("market")
        ) != "india":
            continue

        # Do not fabricate India live quotes.
        # Preserve the Phase 1 RBI instrument universe.

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


# ============================================================================
# STATUS
# ============================================================================

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


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:

    DATA.mkdir(
        exist_ok=True
    )

    log: list[str] = []

    # ------------------------------------------------------------------
    # 1. Load Phase 1 instrument universe.
    # ------------------------------------------------------------------

    instruments = (
        load_instrument_universe(
            log
        )
    )

    if not instruments:

        log.append(
            "ERROR: No instruments were loaded. "
            "Existing country files were not modified."
        )

        payload = {
            "schemaVersion": "3.2",
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
                "updatedAt": payload[
                    "updatedAt"
                ],
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

    # ------------------------------------------------------------------
    # 2. USA Treasury
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 3. Hong Kong HKMA
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 4. Singapore MAS
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 5. India
    # ------------------------------------------------------------------

    try:

        update_india_instruments(
            instruments
        )

        log.append(
            "India: Phase 1 RBI instrument "
            "universe preserved; no unverified "
            "live quote was inferred."
        )

    except Exception as exc:

        log.append(
            f"India: ERROR {exc}"
        )

    # ------------------------------------------------------------------
    # 6. Calculate final status.
    # ------------------------------------------------------------------

    status = calculate_status(
        log,
        instruments,
    )

    updated_at = now_utc()

    # ------------------------------------------------------------------
    # 7. Final payload.
    # ------------------------------------------------------------------

    payload = {
        "schemaVersion": "3.2",

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
                "Statistics/fdanet/"
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

    # ------------------------------------------------------------------
    # 8. Write live.json.
    # ------------------------------------------------------------------

    save_json(
        OUT,
        payload,
    )

    # ------------------------------------------------------------------
    # 9. Write last-update.json.
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 10. Console summary.
    # ------------------------------------------------------------------

    print(
        "=========================================="
    )

    print(
        " Bond Monitor Phase 3 Update v3.2"
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
        "USA curve      : "
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
