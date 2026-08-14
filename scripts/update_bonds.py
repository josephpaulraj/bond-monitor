#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.7

Purpose
-------
Load the Phase 1 instrument universe and update live values only when
they can be obtained safely from official sources.

Markets
-------
USA       : U.S. Treasury daily Treasury par yield curve
Hong Kong : HKMA EFBN indicative prices
Singapore : MAS SGS benchmark prices/yields
India     : Phase 1 RBI instrument universe; no fabricated quotes

Important design principles
---------------------------
1. Never fabricate a price or yield.
2. Never replace an existing value with null because a source failed.
3. Preserve all Phase 1 instruments.
4. Keep network retries short.
5. Treat Singapore MAS HTML as unstable and use several safe parsing
   strategies instead of depending on one exact HTML structure.
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
# VERSION
# ============================================================================

VERSION = "3.7"


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
    "bond-monitor/3.7 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

# Keep retries deliberately short.
HTTP_TIMEOUT = 30
HTTP_RETRIES = 2
HTTP_RETRY_DELAY = 2


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
    "daily-monetary-statistics/"
    "efbn-indicative-price"
    "?segment=IndicativePrice&offset=0"
)

# Primary MAS benchmark page.
MAS_URL = (
    "https://eservices.mas.gov.sg/"
    "Statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx"
)


# ============================================================================
# PHASE 1 COUNTRY FILES
# ============================================================================

COUNTRY_FILES = {
    "United States": DATA / "usa.json",
    "Singapore": DATA / "singapore.json",
    "Hong Kong": DATA / "hongkong.json",
    "India": DATA / "india.json",
}


# ============================================================================
# SINGAPORE TRACKED ISSUE CODES
# ============================================================================

SGS_ISSUE_CODES = {
    "N523100W": {
        "bond": "2.875% SGS 2028",
        "isin": "SGXF51035222",
        "tenor": "2Y",
        "has_price": True,
    },
    "NX21100N": {
        "bond": "1.625% SGS 2031",
        "isin": "SGXF76205099",
        "tenor": "5Y",
        "has_price": True,
    },
    "NZ16100X": {
        "bond": "2.250% SGS 2036",
        "tenor": "10Y",
        "has_price": True,
    },
    "NY25200N": {
        "bond": "2.250% SGS 2040",
        "tenor": "15Y",
        "has_price": True,
    },
    "NA16100H": {
        "bond": "2.750% SGS 2046",
        "tenor": "30Y",
        "has_price": True,
    },
    "NC22300W": {
        "bond": "3.000% SGS 2072",
        "tenor": "50Y",
        "has_price": False,
    },
}


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
        .replace("\xa0", " ")
        .strip()
    )

    if text in {"-", "—", "–", "N/A", "NA"}:
        return None

    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def norm_market(value: Any) -> str:
    return str(value or "").strip().lower()


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


def fetch(
    url: str,
    log: list[str],
    label: str,
    timeout: int = HTTP_TIMEOUT,
    retries: int = HTTP_RETRIES,
) -> bytes:

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):

        log.append(
            f"{label}: HTTP attempt {attempt}/{retries}"
        )

        try:

            request = urllib.request.Request(
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
                request,
                timeout=timeout,
                context=CTX,
            ) as response:

                return response.read()

        except Exception as exc:

            last_error = exc

            log.append(
                f"{label}: connection/timeout error {exc}"
            )

            if attempt < retries:
                time.sleep(HTTP_RETRY_DELAY)

    raise RuntimeError(
        str(last_error)
    )


# ============================================================================
# LOAD PHASE 1 UNIVERSE
# ============================================================================

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

        obj = load_json(path, {})

        if not isinstance(obj, dict):

            log.append(
                f"{market}: ERROR {path.name} "
                "is not a JSON object"
            )

            continue

        records = obj.get("records", [])

        if not isinstance(records, list):

            log.append(
                f"{market}: ERROR records[] missing "
                f"in {path.name}"
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
            f"{market}: loaded {count} instruments "
            f"from {path.name}"
        )

    log.append(
        f"Loaded {len(instruments)} instruments "
        "from Phase 1 country files."
    )

    return instruments


# ============================================================================
# LIVE FIELD INITIALIZATION
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

        instrument.setdefault(
            "dataStatus",
            "source-backed seed",
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
        log,
        "USA Treasury",
        timeout=30,
        retries=2,
    ).decode(
        "utf-8",
        errors="replace",
    )

    date_patterns = [
        r"<d:NEW_DATE[^>]*>(.*?)</d:NEW_DATE>",
        r"<NEW_DATE[^>]*>(.*?)</NEW_DATE>",
        r"<NEW_DATE[^>]*>\s*(.*?)\s*</NEW_DATE>",
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

    pos = raw.rfind(latest)

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
        {},
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

        previous = instrument.get(
            "yield"
        )

        if previous is not None:
            instrument[
                "previousYield"
            ] = previous

        instrument[
            "yield"
        ] = value

        instrument[
            "liveYield"
        ] = value

        instrument[
            "liveDate"
        ] = date

        instrument[
            "dataStatus"
        ] = (
            "live U.S. Treasury "
            "benchmark yield"
        )


# ============================================================================
# HONG KONG HKMA
# ============================================================================

def update_hkma(
    log: list[str],
) -> list[dict[str, Any]]:

    raw = fetch(
        HKMA_URL,
        log,
        "Hong Kong HKMA",
        timeout=30,
        retries=2,
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
                    row.get(
                        "yield"
                    )
                ),
                "price": clean_number(
                    row.get(
                        "price"
                    )
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
        ).lower()

        isin = str(
            instrument.get("isin")
            or ""
        ).strip()

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
                    ) == isin
                ),
                None,
            )

        if hit is None:

            if "exchange fund note" in bond:

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

            elif "exchange fund bill" in bond:

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

        live_yield = hit.get(
            "yield"
        )

        live_price = hit.get(
            "price"
        )

        if live_yield is not None:

            previous = instrument.get(
                "yield"
            )

            if previous is not None:
                instrument[
                    "previousYield"
                ] = previous

            instrument[
                "yield"
            ] = live_yield

            instrument[
                "liveYield"
            ] = live_yield

            changed = True

        if live_price is not None:

            previous = instrument.get(
                "price"
            )

            if previous is not None:
                instrument[
                    "previousPrice"
                ] = previous

            instrument[
                "price"
            ] = live_price

            instrument[
                "livePrice"
            ] = live_price

            changed = True

        if changed:

            instrument[
                "liveDate"
            ] = hit.get(
                "date"
            )

            instrument[
                "dataStatus"
            ] = (
                "live HKMA "
                "indicative data"
            )


# ============================================================================
# SIMPLE HTML TABLE PARSER
# ============================================================================

class HTMLTableParser(
    HTMLParser
):

    def __init__(
        self,
    ) -> None:

        super().__init__(
            convert_charrefs=True
        )

        self.rows: list[list[str]] = []

        self.current_row: list[str] = []

        self.current_cell: list[str] = []

        self.in_cell = False

        self.table_depth = 0

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

        if tag == "table":

            self.table_depth += 1

        elif tag == "tr":

            self.current_row = []

        elif tag in {
            "td",
            "th",
        }:

            self.in_cell = True
            self.current_cell = []

    def handle_endtag(
        self,
        tag: str,
    ) -> None:

        tag = tag.lower()

        if tag in {
            "td",
            "th",
        }:

            if self.in_cell:

                value = " ".join(
                    "".join(
                        self.current_cell
                    ).split()
                )

                self.current_row.append(
                    value
                )

                self.in_cell = False

        elif tag == "tr":

            if self.current_row:

                self.rows.append(
                    self.current_row
                )

            self.current_row = []

        elif tag == "table":

            if self.table_depth > 0:
                self.table_depth -= 1

    def handle_data(
        self,
        data: str,
    ) -> None:

        if self.in_cell:

            self.current_cell.append(
                data
            )


# ============================================================================
# HTML NORMALIZATION HELPERS
# ============================================================================

def html_to_visible_text(
    html: str,
) -> str:

    text = re.sub(
        r"<script\b.*?</script>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"<style\b.*?</style>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    text = (
        text
        .replace("&nbsp;", " ")
        .replace("&#160;", " ")
        .replace("&amp;", "&")
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def find_sgs_codes(
    html: str,
) -> list[str]:

    upper = html.upper()

    found = []

    for code in SGS_ISSUE_CODES:

        if code in upper:

            found.append(
                code
            )

    return found


def extract_dates(
    text: str,
) -> list[str]:

    patterns = [
        r"\b\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\b",
        r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b",
    ]

    dates: list[str] = []

    for pattern in patterns:

        for value in re.findall(
            pattern,
            text,
        ):

            if value not in dates:

                dates.append(
                    value
                )

    return dates


# ============================================================================
# SINGAPORE MAS
# ============================================================================

def validate_sgs_yield(
    value: float | None,
) -> float | None:

    if value is None:
        return None

    # SGS yield is quoted as % p.a.
    if not 0 <= value <= 20:
        return None

    return value


def validate_sgs_price(
    value: float | None,
) -> float | None:

    if value is None:
        return None

    # Clean bond price should be within a broad,
    # deliberately conservative range.
    if not 50 <= value <= 150:
        return None

    return value


def parse_mas_rows_from_html(
    html: str,
    log: list[str],
) -> list[dict[str, Any]]:

    """
    Strategy 1:
    Parse normal HTML table rows.

    We specifically look for a row containing the SGS issue codes
    and then identify the corresponding data rows by date.
    """

    parser = HTMLTableParser()

    try:
        parser.feed(html)
    except Exception:
        pass

    tracked = find_sgs_codes(
        html
    )

    if not tracked:

        return []

    # Look for rows that contain tracked issue codes.
    code_header_rows = []

    for row in parser.rows:

        row_upper = " ".join(
            row
        ).upper()

        if any(
            code in row_upper
            for code in SGS_ISSUE_CODES
        ):

            code_header_rows.append(
                row
            )

    # We need actual daily rows, not merely the issue-code header.
    #
    # A daily closing row normally contains:
    # date + numeric/string values.
    #
    # Search all parsed rows for a date.
    dated_rows = []

    for row in parser.rows:

        joined = " ".join(
            row
        )

        if re.search(
            r"\b\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\b",
            joined,
        ):

            dated_rows.append(
                row
            )

    if not dated_rows:

        return []

    # Use the latest parsed date.
    dated_with_date = []

    for row in dated_rows:

        joined = " ".join(
            row
        )

        match = re.search(
            r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b",
            joined,
        )

        if match:

            dated_with_date.append(
                (
                    match.group(1),
                    row,
                )
            )

    if not dated_with_date:

        return []

    latest_date, latest_row = dated_with_date[-1]

    # Numeric values in a normal Closing Levels row.
    numbers = []

    for cell in latest_row:

        value = clean_number(
            cell
        )

        if value is not None:

            numbers.append(
                value
            )

    if len(numbers) < 6:

        return []

    # We do not blindly trust arbitrary numeric positions.
    #
    # Instead, the MAS benchmark page has the following
    # tracked bond columns:
    #
    # 2Y  = Price / Yield
    # 5Y  = Price / Yield
    # 10Y = Price / Yield
    # 15Y = Price / Yield
    # 20Y = Price / Yield
    # 30Y = Price / Yield
    # 50Y = Yield
    #
    # If a row has 13 or more numeric values, the last 13
    # values are used as the benchmark block.
    #
    # This accommodates unrelated leading Treasury Bill values.

    if len(numbers) >= 13:

        block = numbers[-13:]

        mapping = {
            "N523100W": {
                "price": block[0],
                "yield": block[1],
            },
            "NX21100N": {
                "price": block[2],
                "yield": block[3],
            },
            "NZ16100X": {
                "price": block[4],
                "yield": block[5],
            },
            "NY25200N": {
                "price": block[6],
                "yield": block[7],
            },
            "NA16100H": {
                "price": block[8],
                "yield": block[9],
            },
            "NC22300W": {
                "yield": block[10],
                "price": None,
            },
        }

    else:

        # Fallback for a row containing only yield/price
        # values for the tracked instruments.

        if len(numbers) < 11:
            return []

        mapping = {
            "N523100W": {
                "price": numbers[0],
                "yield": numbers[1],
            },
            "NX21100N": {
                "price": numbers[2],
                "yield": numbers[3],
            },
            "NZ16100X": {
                "price": numbers[4],
                "yield": numbers[5],
            },
            "NY25200N": {
                "price": numbers[6],
                "yield": numbers[7],
            },
            "NA16100H": {
                "price": numbers[8],
                "yield": numbers[9],
            },
            "NC22300W": {
                "yield": numbers[10],
                "price": None,
            },
        }

    output = []

    for code, values in mapping.items():

        yield_value = validate_sgs_yield(
            clean_number(
                values.get(
                    "yield"
                )
            )
        )

        price_value = validate_sgs_price(
            clean_number(
                values.get(
                    "price"
                )
            )
        )

        if (
            yield_value is None
            and price_value is None
        ):
            continue

        output.append(
            {
                "issue_code": code,
                "yield": yield_value,
                "price": price_value,
                "date": latest_date,
                "source": "MAS",
            }
        )

    return output


def parse_mas_from_visible_text(
    html: str,
    log: list[str],
) -> list[dict[str, Any]]:

    """
    Strategy 2:
    MAS can expose the benchmark table in an HTML representation
    where the normal table parser does not see the expected rows.

    Therefore convert the page to visible text and identify:
      - the six issue codes
      - the latest dated data row
      - the numeric benchmark sequence
    """

    text = html_to_visible_text(
        html
    )

    tracked = find_sgs_codes(
        text
    )

    if not tracked:

        return []

    dates = extract_dates(
        text
    )

    if not dates:

        return []

    # Find the final dated row/section.
    #
    # Use the last date because the MAS page is chronological.
    latest_date = dates[-1]

    pos = text.rfind(
        latest_date
    )

    if pos < 0:

        return []

    after = text[
        pos:
    ]

    # Stop before a subsequent date if one exists.
    next_dates = re.search(
        r"\b\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\b",
        after[len(latest_date):],
    )

    if next_dates:

        after = after[
            :len(latest_date)
            + next_dates.start()
        ]

    numbers = []

    for value in re.findall(
        r"(?<![A-Za-z])"
        r"\d+(?:\.\d+)?"
        r"(?![A-Za-z])",
        after,
    ):

        number = clean_number(
            value
        )

        if number is not None:

            numbers.append(
                number
            )

    # The visible text can contain navigation numbers.
    # Only accept a sufficiently large benchmark block.

    if len(numbers) < 11:

        return []

    if len(numbers) >= 13:

        block = numbers[-13:]

        mapping = {
            "N523100W": {
                "price": block[0],
                "yield": block[1],
            },
            "NX21100N": {
                "price": block[2],
                "yield": block[3],
            },
            "NZ16100X": {
                "price": block[4],
                "yield": block[5],
            },
            "NY25200N": {
                "price": block[6],
                "yield": block[7],
            },
            "NA16100H": {
                "price": block[8],
                "yield": block[9],
            },
            "NC22300W": {
                "yield": block[10],
                "price": None,
            },
        }

    else:

        mapping = {
            "N523100W": {
                "price": numbers[0],
                "yield": numbers[1],
            },
            "NX21100N": {
                "price": numbers[2],
                "yield": numbers[3],
            },
            "NZ16100X": {
                "price": numbers[4],
                "yield": numbers[5],
            },
            "NY25200N": {
                "price": numbers[6],
                "yield": numbers[7],
            },
            "NA16100H": {
                "price": numbers[8],
                "yield": numbers[9],
            },
            "NC22300W": {
                "yield": numbers[10],
                "price": None,
            },
        }

    output = []

    for code, values in mapping.items():

        y = validate_sgs_yield(
            clean_number(
                values.get(
                    "yield"
                )
            )
        )

        p = validate_sgs_price(
            clean_number(
                values.get(
                    "price"
                )
            )
        )

        if (
            y is None
            and p is None
        ):
            continue

        output.append(
            {
                "issue_code": code,
                "yield": y,
                "price": p,
                "date": latest_date,
                "source": "MAS",
            }
        )

    return output


def parse_mas(
    html: str,
    log: list[str],
) -> dict[str, Any]:

    tracked = find_sgs_codes(
        html
    )

    log.append(
        "Singapore: MAS page fetched; "
        f"found {len(tracked)}/"
        f"{len(SGS_ISSUE_CODES)} "
        "tracked issue codes."
    )

    if not tracked:

        log.append(
            "Singapore: MAS benchmark issue codes "
            "could not be detected in downloaded content."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
            "matchedIssueCodes": 0,
        }

    # ------------------------------------------------------------------
    # Strategy 1: HTML table parser
    # ------------------------------------------------------------------

    rows = parse_mas_rows_from_html(
        html,
        log,
    )

    if rows:

        matched = len(
            {
                row["issue_code"]
                for row in rows
            }
        )

        log.append(
            "Singapore: MAS live benchmark data "
            f"parsed for {matched}/"
            f"{len(SGS_ISSUE_CODES)} tracked issues "
            "using HTML table parsing."
        )

        return {
            "source": "MAS",
            "status": "success",
            "rows": rows,
            "matchedIssueCodes": matched,
        }

    # ------------------------------------------------------------------
    # Strategy 2: visible text parser
    # ------------------------------------------------------------------

    rows = parse_mas_from_visible_text(
        html,
        log,
    )

    if rows:

        matched = len(
            {
                row["issue_code"]
                for row in rows
            }
        )

        log.append(
            "Singapore: MAS live benchmark data "
            f"parsed for {matched}/"
            f"{len(SGS_ISSUE_CODES)} tracked issues "
            "using visible-text fallback."
        )

        return {
            "source": "MAS",
            "status": "success",
            "rows": rows,
            "matchedIssueCodes": matched,
        }

    # ------------------------------------------------------------------
    # Safe failure
    # ------------------------------------------------------------------

    log.append(
        "Singapore: MAS issue codes were found, "
        "but no safe benchmark data row could be parsed."
    )

    return {
        "source": "MAS",
        "status": "fetched_no_table",
        "rows": [],
        "matchedIssueCodes": len(tracked),
    }


def update_singapore_instruments(
    instruments: list[dict[str, Any]],
    mas: dict[str, Any],
) -> None:

    rows = mas.get(
        "rows",
        []
    )

    if not isinstance(
        rows,
        list,
    ):
        return

    isin_to_issue_code = {
        "SGXF51035222": "N523100W",
        "SGXF76205099": "NX21100N",
        "SG31A9000002": "NZ16100X",
        "SGXF29838152": "NY25200N",
        "SG31A7000004": "NA16100H",
        "SGXF47639806": "NC22300W",
    }

    matched = 0

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

        y = hit.get(
            "yield"
        )

        p = hit.get(
            "price"
        )

        if y is not None:

            previous = instrument.get(
                "yield"
            )

            if previous is not None:
                instrument[
                    "previousYield"
                ] = previous

            instrument[
                "yield"
            ] = y

            instrument[
                "liveYield"
            ] = y

            changed = True

        if p is not None:

            previous = instrument.get(
                "price"
            )

            if previous is not None:
                instrument[
                    "previousPrice"
                ] = previous

            instrument[
                "price"
            ] = p

            instrument[
                "livePrice"
            ] = p

            changed = True

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

            matched += 1

    mas[
        "matchedInstruments"
    ] = matched


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

        # Do not fabricate live values.
        #
        # Existing Phase 1 yield/price values remain untouched.

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
# STATUS
# ============================================================================

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

    return "success"


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:

    DATA.mkdir(
        exist_ok=True
    )

    log: list[str] = []

    # ------------------------------------------------------------------------
    # 1. Load Phase 1 instrument universe
    # ------------------------------------------------------------------------

    instruments = load_instrument_universe(
        log
    )

    if not instruments:

        log.append(
            "ERROR: No instruments were loaded. "
            "Existing country files were not modified."
        )

        payload = {
            "schemaVersion": VERSION,
            "updatedAt": now_utc(),
            "status": "error",
            "sources": {},
            "instrumentCount": 0,
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

        html = fetch(
            MAS_URL,
            log,
            "Singapore MAS",
            timeout=30,
            retries=2,
        ).decode(
            "utf-8",
            errors="replace",
        )

        mas = parse_mas(
            html,
            log,
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
            VERSION,

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
        f" Bond Monitor Phase 3 Update v{VERSION}"
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

    if isinstance(
        hk,
        list,
    ) and hk:

        hk_status = "OK"

    else:

        hk_status = "ERROR/PARTIAL"

    print(
        f"Hong Kong      : {hk_status}"
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


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()
