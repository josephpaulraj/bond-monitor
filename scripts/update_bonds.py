#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.6

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
- Never replace an existing value with null because of a temporary source failure.
- Write normalized data/live.json and data/last-update.json.

v3.6 changes
-------------
Singapore MAS parser was rewritten to avoid the v3.5 problem where
coupon values were incorrectly interpreted as live yields.

The MAS Closing Levels table is parsed structurally.

Important MAS table layout:

6M Yield
1Y Yield
2Y Price | 2Y Yield
5Y Price | 5Y Yield
10Y Price | 10Y Yield
15Y Price | 15Y Yield
20Y Price | 20Y Yield
30Y Price | 30Y Yield
50Y Yield

For the six tracked SGS benchmark issues:

N523100W -> 2Y  -> price/yield
NX21100N -> 5Y  -> price/yield
NZ16100X -> 10Y -> price/yield
NY25200N -> 15Y -> price/yield
NA16100H -> 30Y -> price/yield
NC22300W -> 50Y -> yield only

The parser:
- Finds the actual Closing Levels table.
- Finds the latest dated market row.
- Uses the correct Price/Yield ordering.
- Does NOT use the MAS webpage footer date.
- Rejects rows with impossible values.
- Rejects suspicious results where every tracked yield equals
  the corresponding coupon.
- Does not overwrite existing live data with invalid data.

Official sources
----------------
USA:
U.S. Treasury daily Treasury par yield curve

Singapore:
Monetary Authority of Singapore SGS benchmark prices/yields

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
# Paths
# ============================================================================

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

OUT = DATA / "live.json"
LAST_UPDATE = DATA / "last-update.json"


# ============================================================================
# HTTP configuration
# ============================================================================

UA = (
    "bond-monitor/3.6 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

HTTP_TIMEOUT = 45
HTTP_RETRIES = 2
HTTP_RETRY_DELAY = 2


# ============================================================================
# Official source URLs
# ============================================================================

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
    "https://eservices.mas.gov.sg/Statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx"
)


# ============================================================================
# Generic helpers
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
    label: str = "HTTP",
) -> bytes:

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):

        print(
            f"{label}: HTTP attempt {attempt}/{retries}"
        )

        try:

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml,application/json,*/*"
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

            print(
                f"{label}: connection/timeout error {exc}"
            )

            if attempt < retries:
                time.sleep(HTTP_RETRY_DELAY)

    raise RuntimeError(
        str(last_error)
    )


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

    text = str(
        value
    ).strip()

    if not text:
        return None

    text = (
        text
        .replace(",", "")
        .replace("%", "")
    )

    if text in {
        "-",
        "—",
        "–",
        "N/A",
        "NA",
    }:
        return None

    try:

        return float(text)

    except ValueError:

        return None


# ============================================================================
# Phase 1 instrument universe
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

        if not isinstance(
            obj,
            dict,
        ):

            log.append(
                f"{market}: ERROR {path.name} "
                "is not a JSON object"
            )

            continue

        records = obj.get(
            "records",
            [],
        )

        if not isinstance(
            records,
            list,
        ):

            log.append(
                f"{market}: ERROR records[] "
                f"missing in {path.name}"
            )

            continue

        count = 0

        for record in records:

            if isinstance(
                record,
                dict,
            ):

                instruments.append(
                    dict(record)
                )

                count += 1

        log.append(
            f"{market}: loaded {count} instruments "
            f"from {path.name}"
        )

    log.append(
        f"Loaded {len(instruments)} instruments "
        "from Phase 1 country files."
    )

    return instruments


# ============================================================================
# Initialize live fields
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
# USA Treasury
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
        timeout=45,
        retries=2,
        label="USA Treasury",
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
            entry_end + len("</entry>")
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

            rf"<d:{field}[^>]*>"
            rf"(.*?)"
            rf"</d:{field}>",

            rf"<{field}[^>]*>"
            rf"(.*?)"
            rf"</{field}>",
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
        "USA: Treasury curve updated for "
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

        "5-Year Treasury benchmark":
            "5 Yr",

        "10-Year Treasury benchmark":
            "10 Yr",

        "30-Year Treasury benchmark":
            "30 Yr",
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
            instrument.get("bond")
            or ""
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
# Hong Kong HKMA
# ============================================================================

def update_hkma(
    log: list[str],
) -> list[dict[str, Any]]:

    raw = fetch(
        HKMA_URL,
        timeout=45,
        retries=2,
        label="Hong Kong HKMA",
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
        "Hong Kong: HKMA returned "
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
            instrument.get("bond")
            or ""
        )

        isin = str(
            instrument.get("isin")
            or ""
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
                        row.get("issue_no")
                        or ""
                    ) == isin
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
                            row.get("term")
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
                            row.get("term")
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
# HTML table parser
# ============================================================================

class TextTableParser(
    HTMLParser
):

    def __init__(
        self,
    ) -> None:

        super().__init__()

        self.in_cell = False
        self.rows: list[list[str]] = []

        self.row: list[str] = []

        self.buf: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[
            tuple[
                str,
                str | None,
            ]
        ],
    ) -> None:

        tag = tag.lower()

        if tag == "tr":

            self.row = []

        elif tag in (
            "td",
            "th",
        ):

            self.in_cell = True
            self.buf = []

    def handle_endtag(
        self,
        tag: str,
    ) -> None:

        tag = tag.lower()

        if tag in (
            "td",
            "th",
        ):

            if self.in_cell:

                text = " ".join(
                    "".join(
                        self.buf
                    ).split()
                )

                self.row.append(
                    text
                )

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

            self.buf.append(
                data
            )


# ============================================================================
# Singapore MAS v3.6
# ============================================================================

MAS_ISSUE_CODES = {

    "N523100W": {
        "tenor": "2Y",
        "yield_index": 3,
        "price_index": 2,
        "coupon": 2.875,
    },

    "NX21100N": {
        "tenor": "5Y",
        "yield_index": 5,
        "price_index": 4,
        "coupon": 1.625,
    },

    "NZ16100X": {
        "tenor": "10Y",
        "yield_index": 7,
        "price_index": 6,
        "coupon": 2.250,
    },

    "NY25200N": {
        "tenor": "15Y",
        "yield_index": 9,
        "price_index": 8,
        "coupon": 2.250,
    },

    "NA16100H": {
        "tenor": "30Y",
        "yield_index": 13,
        "price_index": 12,
        "coupon": 2.750,
    },

    "NC22300W": {
        "tenor": "50Y",
        "yield_index": 14,
        "price_index": None,
        "coupon": 3.000,
    },
}


def parse_mas_date(
    value: str,
) -> datetime | None:

    value = value.strip()

    formats = [
        "%d %b %Y",
        "%d %B %Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%Y-%m-%d",
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                value,
                fmt,
            )

        except ValueError:

            continue

    return None


def extract_date_from_row(
    row: list[str],
) -> tuple[str, datetime] | None:

    for cell in row:

        text = " ".join(
            str(cell).split()
        ).strip()

        if not text:
            continue

        match = re.search(
            r"\b"
            r"(\d{1,2}\s+"
            r"[A-Za-z]{3,9}\s+"
            r"\d{4})"
            r"\b",
            text,
        )

        if not match:
            continue

        date_text = match.group(1)

        parsed = parse_mas_date(
            date_text
        )

        if parsed:

            return (
                date_text,
                parsed,
            )

    return None


def row_contains_issue_code(
    row: list[str],
) -> bool:

    normalized = {
        str(cell)
        .strip()
        .upper()
        for cell in row
    }

    return bool(
        normalized.intersection(
            MAS_ISSUE_CODES.keys()
        )
    )


def find_mas_closing_table_rows(
    parser: TextTableParser,
) -> tuple[
    list[str] | None,
    list[list[str]],
]:

    """
    Find the table containing the tracked SGS issue codes.

    Returns:
        header row,
        all rows from the table.
    """

    rows = parser.rows

    code_row_index = None

    for index, row in enumerate(
        rows
    ):

        if row_contains_issue_code(
            row
        ):

            code_row_index = index
            break

    if code_row_index is None:

        return (
            None,
            [],
        )

    # Search backwards for the row containing
    # the Closing Levels heading.
    start_index = max(
        0,
        code_row_index - 15,
    )

    closing_start = start_index

    for index in range(
        code_row_index,
        start_index - 1,
        -1,
    ):

        joined = " ".join(
            row
            for row in rows[index]
        ).lower()

        if "closing" in joined:

            closing_start = index
            break

    # Search forward until High / Low Levels.
    end_index = len(rows)

    for index in range(
        code_row_index + 1,
        len(rows),
    ):

        joined = " ".join(
            rows[index]
        ).lower()

        if (
            "high" in joined
            and "low" in joined
        ):

            end_index = index
            break

    table_rows = rows[
        closing_start:
        end_index
    ]

    return (
        rows[code_row_index],
        table_rows,
    )


def parse_mas_closing_rows(
    parser: TextTableParser,
) -> tuple[
    list[dict[str, Any]],
    str | None,
]:

    header, table_rows = (
        find_mas_closing_table_rows(
            parser
        )
    )

    if not table_rows:

        return (
            [],
            None,
        )

    # Find every row containing a genuine
    # dated market observation.
    dated_rows: list[
        tuple[
            str,
            datetime,
            list[str],
        ]
    ] = []

    for row in table_rows:

        date_info = extract_date_from_row(
            row
        )

        if not date_info:
            continue

        date_text, parsed_date = (
            date_info
        )

        # Ignore dates that occur before
        # the benchmark issue-code header.
        if row is header:
            continue

        dated_rows.append(
            (
                date_text,
                parsed_date,
                row,
            )
        )

    if not dated_rows:

        return (
            [],
            None,
        )

    # The latest actual market-data date wins.
    dated_rows.sort(
        key=lambda item: item[1]
    )

    latest_date_text = dated_rows[
        -1
    ][0]

    latest_row = dated_rows[
        -1
    ][2]

    # Remove the date cell from the numeric sequence.
    numeric_cells: list[
        str
    ] = []

    date_removed = False

    for cell in latest_row:

        text = str(
            cell
        ).strip()

        if not date_removed:

            parsed = extract_date_from_row(
                [text]
            )

            if parsed:

                date_removed = True
                continue

        numeric_cells.append(
            text
        )

    # Some HTML versions put the date and
    # values into the same first cell.
    if not date_removed:

        numeric_cells = [
            re.sub(
                r"\b"
                r"\d{1,2}\s+"
                r"[A-Za-z]{3,9}\s+"
                r"\d{4}"
                r"\b",
                "",
                str(cell),
            ).strip()
            for cell in latest_row
        ]

    # The browser/text representation may place
    # the entire numeric sequence into one cell.
    expanded: list[str] = []

    for cell in numeric_cells:

        if not cell:
            continue

        matches = re.findall(
            r"(?<![A-Za-z])"
            r"(?:\d+(?:\.\d+)?|[-—–])"
            r"(?![A-Za-z])",
            cell,
        )

        if matches:

            expanded.extend(
                matches
            )

        else:

            expanded.append(
                cell
            )

    numeric_values: list[
        float | None
    ] = []

    for value in expanded:

        numeric_values.append(
            clean_number(
                value
            )
        )

    # The expected MAS closing row contains:
    #
    # 0  = 6M yield
    # 1  = 1Y yield
    # 2  = 2Y price
    # 3  = 2Y yield
    # 4  = 5Y price
    # 5  = 5Y yield
    # 6  = 10Y price
    # 7  = 10Y yield
    # 8  = 15Y price
    # 9  = 15Y yield
    # 10 = 20Y price
    # 11 = 20Y yield
    # 12 = 30Y price
    # 13 = 30Y yield
    # 14 = 50Y yield
    #
    # Therefore we need at least 15 values.
    #
    if len(
        numeric_values
    ) < 15:

        return (
            [],
            latest_date_text,
        )

    values = numeric_values[
        :15
    ]

    output: list[
        dict[str, Any]
    ] = []

    for issue_code, config in (
        MAS_ISSUE_CODES.items()
    ):

        yi = config[
            "yield_index"
        ]

        pi = config[
            "price_index"
        ]

        yield_value = (
            values[yi]
            if yi < len(values)
            else None
        )

        price_value = None

        if pi is not None:

            price_value = (
                values[pi]
                if pi < len(values)
                else None
            )

        coupon = float(
            config["coupon"]
        )

        # --------------------------------------------------------
        # Sanity checks
        # --------------------------------------------------------

        if (
            yield_value is not None
            and not (
                0.0
                <= yield_value
                <= 20.0
            )
        ):

            yield_value = None

        if (
            price_value is not None
            and not (
                50.0
                <= price_value
                <= 150.0
            )
        ):

            price_value = None

        output.append(
            {
                "issue_code": issue_code,
                "tenor": config["tenor"],
                "yield": yield_value,
                "price": price_value,
                "date": latest_date_text,
                "source": "MAS",
                "coupon": coupon,
            }
        )

    return (
        output,
        latest_date_text,
    )


def validate_mas_rows(
    rows: list[dict[str, Any]],
    log: list[str],
) -> list[dict[str, Any]]:

    if not rows:

        return []

    # ------------------------------------------------------------
    # Check 1:
    # A real market row must have an actual date.
    # ------------------------------------------------------------

    for row in rows:

        if not row.get(
            "date"
        ):

            log.append(
                "Singapore: MAS row rejected "
                "because no market-data date was present."
            )

            return []

    # ------------------------------------------------------------
    # Check 2:
    # Reject suspicious all-coupon results.
    #
    # This specifically protects against the v3.5 failure where
    # coupon values were accidentally parsed as live yields.
    # ------------------------------------------------------------

    matched = 0
    equal_coupon = 0

    for row in rows:

        y = row.get(
            "yield"
        )

        coupon = row.get(
            "coupon"
        )

        if y is None:
            continue

        matched += 1

        if (
            coupon is not None
            and abs(
                float(y)
                - float(coupon)
            ) < 0.0001
        ):

            equal_coupon += 1

    if (
        matched >= 4
        and equal_coupon == matched
    ):

        log.append(
            "Singapore: MAS data rejected; "
            "all parsed yields equal coupon rates."
        )

        return []

    # ------------------------------------------------------------
    # Check 3:
    # Require at least 4 valid tracked yields.
    # ------------------------------------------------------------

    valid_yields = sum(
        1
        for row in rows
        if row.get("yield")
        is not None
    )

    if valid_yields < 4:

        log.append(
            "Singapore: MAS data rejected; "
            f"only {valid_yields}/6 valid yields."
        )

        return []

    return rows


def update_mas(
    log: list[str],
) -> dict[str, Any]:

    html = fetch(
        MAS_URL,
        timeout=45,
        retries=2,
        label="Singapore MAS",
    ).decode(
        "utf-8",
        errors="replace",
    )

    parser = TextTableParser()

    parser.feed(
        html
    )

    found_codes = set()

    for row in parser.rows:

        for cell in row:

            code = str(
                cell
            ).strip().upper()

            if code in MAS_ISSUE_CODES:

                found_codes.add(
                    code
                )

    log.append(
        "Singapore: MAS page fetched; "
        f"found {len(found_codes)}/"
        f"{len(MAS_ISSUE_CODES)} "
        "tracked issue codes."
    )

    if len(
        found_codes
    ) < len(
        MAS_ISSUE_CODES
    ):

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
        }

    rows, latest_date = (
        parse_mas_closing_rows(
            parser
        )
    )

    if not rows:

        log.append(
            "Singapore: MAS Closing Levels "
            "table could not be parsed safely."
        )

        return {
            "source": "MAS",
            "status": "fetched_unverified",
            "rows": [],
        }

    rows = validate_mas_rows(
        rows,
        log,
    )

    if not rows:

        return {
            "source": "MAS",
            "status": "fetched_unverified",
            "rows": [],
        }

    # ------------------------------------------------------------
    # Ensure we have a current-looking market date.
    #
    # The MAS webpage footer may say:
    # "Last updated on 26 Feb 2020"
    #
    # That is NOT the market-data date.
    #
    # We use only the actual dated Closing Levels row.
    # ------------------------------------------------------------

    parsed_market_date = parse_mas_date(
        str(
            latest_date
            or ""
        )
    )

    if not parsed_market_date:

        log.append(
            "Singapore: MAS closing row "
            "has no valid market date."
        )

        return {
            "source": "MAS",
            "status": "fetched_unverified",
            "rows": [],
        }

    log.append(
        "Singapore: MAS live benchmark data "
        f"parsed for {len(rows)}/"
        f"{len(MAS_ISSUE_CODES)} tracked issues "
        f"({latest_date})."
    )

    return {
        "source": "MAS",
        "status": "success",
        "date": latest_date,
        "rows": rows,
    }


# ============================================================================
# Singapore instrument update
# ============================================================================

def update_singapore_instruments(
    instruments: list[dict[str, Any]],
    mas: dict[str, Any],
) -> None:

    rows = mas.get(
        "rows",
        []
    )

    if not rows:
        return

    isin_to_issue_code = {

        "SGXF51035222":
            "N523100W",

        "SGXF76205099":
            "NX21100N",

        "SG31A9000002":
            "NZ16100X",

        "SGXF29838152":
            "NY25200N",

        "SG31A7000004":
            "NA16100H",

        "SGXF47639806":
            "NC22300W",
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

        # --------------------------------------------------------
        # Yield
        # --------------------------------------------------------

        if hit.get(
            "yield"
        ) is not None:

            instrument[
                "previousYield"
            ] = instrument.get(
                "yield"
            )

            instrument[
                "yield"
            ] = hit[
                "yield"
            ]

            instrument[
                "liveYield"
            ] = hit[
                "yield"
            ]

            changed = True

        # --------------------------------------------------------
        # Price
        # --------------------------------------------------------

        if hit.get(
            "price"
        ) is not None:

            instrument[
                "previousPrice"
            ] = instrument.get(
                "price"
            )

            instrument[
                "price"
            ] = hit[
                "price"
            ]

            instrument[
                "livePrice"
            ] = hit[
                "price"
            ]

            changed = True

        # --------------------------------------------------------
        # Date
        # --------------------------------------------------------

        if changed:

            instrument[
                "liveDate"
            ] = hit.get(
                "date"
            )

            instrument[
                "dataStatus"
            ] = (
                "live MAS SGS data"
            )

            if not instrument.get(
                "issue_code"
            ):

                instrument[
                    "issue_code"
                ] = issue_code

            updated_count += 1

    mas[
        "matchedInstruments"
    ] = updated_count


# ============================================================================
# India
# ============================================================================

def update_india_instruments(
    instruments: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

        if norm_market(
            instrument.get("market")
        ) != "india":

            continue

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

        instrument[
            "dataStatus"
        ] = (
            "RBI source configured; "
            "instrument quote endpoint unavailable"
        )


# ============================================================================
# Status
# ============================================================================

def calculate_status(
    log: list[str],
    instruments: list[
        dict[str, Any]
    ],
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

    # ------------------------------------------------------------
    # v3.6:
    #
    # A source may be fetched but deliberately unverified.
    # That should not be treated as a fatal updater error.
    # ------------------------------------------------------------

    return "success"


# ============================================================================
# Main
# ============================================================================

def main() -> None:

    DATA.mkdir(
        exist_ok=True
    )

    log: list[str] = []

    # ------------------------------------------------------------------------
    # 1. Load Phase 1 universe
    # ------------------------------------------------------------------------

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

            "schemaVersion": "3.6",

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
                "updatedAt":
                    payload["updatedAt"],

                "status":
                    "error",

                "instrumentCount":
                    0,

                "log":
                    log,
            },
        )

        raise SystemExit(
            "No instruments loaded."
        )

    initialize_live_fields(
        instruments
    )

    # ------------------------------------------------------------------------
    # 2. USA Treasury
    # ------------------------------------------------------------------------

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

            "source":
                "U.S. Treasury",

            "status":
                "error",

            "error":
                str(exc),

            "curve":
                {},
        }

        log.append(
            f"USA: ERROR {exc}"
        )

    # ------------------------------------------------------------------------
    # 3. Hong Kong HKMA
    # ------------------------------------------------------------------------

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

    # ------------------------------------------------------------------------
    # 4. Singapore MAS
    # ------------------------------------------------------------------------

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

            "source":
                "MAS",

            "status":
                "error",

            "error":
                str(exc),

            "rows":
                [],
        }

        log.append(
            f"Singapore: ERROR {exc}"
        )

    # ------------------------------------------------------------------------
    # 5. India
    # ------------------------------------------------------------------------

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

    # ------------------------------------------------------------------------
    # 6. Final status
    # ------------------------------------------------------------------------

    status = calculate_status(
        log,
        instruments,
    )

    updated_at = now_utc()

    payload = {

        "schemaVersion":
            "3.6",

        "updatedAt":
            updated_at,

        "status":
            status,

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

        "usaCurve":
            us,

        "hongkongIndicative":
            hk,

        "singaporeBenchmark":
            mas,

        "instrumentCount":
            len(instruments),

        "instruments":
            instruments,

        "log":
            log,
    }

    # ------------------------------------------------------------------------
    # 7. Write output
    # ------------------------------------------------------------------------

    save_json(
        OUT,
        payload,
    )

    save_json(
        LAST_UPDATE,
        {
            "updatedAt":
                updated_at,

            "status":
                status,

            "instrumentCount":
                len(instruments),

            "log":
                log,
        },
    )

    # ------------------------------------------------------------------------
    # 8. Console summary
    # ------------------------------------------------------------------------

    print(
        "=========================================="
    )

    print(
        " Bond Monitor Phase 3 Update v3.6"
    )

    print(
        "=========================================="
    )

    print(
        f"Overall status : {status}"
    )

    print(
        f"Instruments    : "
        f"{len(instruments)}"
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

        print(
            entry
        )

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
