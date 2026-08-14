#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.8

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

v3.8 changes
------------
1. Uses the current official MAS SGS Benchmark Prices and Yields page:
   https://eservices.mas.gov.sg/Statistics/fdanet/BenchmarkPricesAndYields.aspx

2. Removes dependency on the old "Closing Levels" page/section.

3. Parses the MAS benchmark table using:
      Issue Code
      Coupon Rate
      Maturity Date
      Date
      Yield / Price

4. Supports the six tracked SGS benchmark instruments.

5. Never fabricates a price or yield.

6. Never replaces an existing value with null because a source failed.

7. Preserves all 22 Phase 1 instruments.

8. Keeps network retries short.
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

VERSION = "3.8"


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
    "bond-monitor/3.8 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

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

# IMPORTANT:
# This is the current MAS SGS Benchmark Prices and Yields page.
MAS_URL = (
    "https://eservices.mas.gov.sg/"
    "Statistics/fdanet/"
    "BenchmarkPricesAndYields.aspx"
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
# SINGAPORE TRACKED SGS ISSUE CODES
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
        "isin": "SG31A9000002",
        "tenor": "10Y",
        "has_price": True,
    },

    "NY25200N": {
        "bond": "2.250% SGS 2040",
        "isin": "SGXF29838152",
        "tenor": "15Y",
        "has_price": True,
    },

    "NA16100H": {
        "bond": "2.750% SGS 2046",
        "isin": "SG31A7000004",
        "tenor": "30Y",
        "has_price": True,
    },

    "NC22300W": {
        "bond": "3.000% SGS 2072",
        "isin": "SGXF47639806",
        "tenor": "50Y",
        "has_price": True,
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

    if text in {
        "-",
        "—",
        "–",
        "N/A",
        "NA",
        "",
    }:
        return None

    try:
        return float(text)

    except (
        ValueError,
        TypeError,
    ):
        return None


def norm_market(value: Any) -> str:
    return str(value or "").strip().lower()


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


def fetch(
    url: str,
    log: list[str],
    label: str,
    timeout: int = HTTP_TIMEOUT,
    retries: int = HTTP_RETRIES,
) -> bytes:

    last_error: Exception | None = None

    for attempt in range(
        1,
        retries + 1,
    ):

        log.append(
            f"{label}: HTTP attempt "
            f"{attempt}/{retries}"
        )

        try:

            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": (
                        "text/html,"
                        "application/xhtml+xml,"
                        "application/xml,"
                        "text/xml,"
                        "*/*"
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
                f"{label}: connection/timeout "
                f"error {exc}"
            )

            if attempt < retries:
                time.sleep(
                    HTTP_RETRY_DELAY
                )

    raise RuntimeError(
        str(last_error)
    )


# ============================================================================
# LOAD PHASE 1 UNIVERSE
# ============================================================================

def load_instrument_universe(
    log: list[str],
) -> list[dict[str, Any]]:

    instruments: list[
        dict[str, Any]
    ] = []

    for market, path in COUNTRY_FILES.items():

        if not path.exists():

            log.append(
                f"{market}: ERROR missing "
                f"{path.name}"
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
                f"{market}: ERROR "
                f"{path.name} is not a JSON object"
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
            f"{market}: loaded {count} "
            f"instruments from {path.name}"
        )

    log.append(
        f"Loaded {len(instruments)} "
        "instruments from Phase 1 country files."
    )

    return instruments


# ============================================================================
# INITIALIZE LIVE FIELDS
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

    curve: dict[
        str,
        float,
    ] = {}

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
            "Treasury feed returned no "
            "curve values"
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
            instrument.get(
                "market"
            )
        ) not in {
            "united states",
            "usa",
            "us",
        }:
            continue

        bond = str(
            instrument.get(
                "bond"
            )
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

    output: list[
        dict[str, Any]
    ] = []

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
        f"{len(output)} "
        "indicative-price rows."
    )

    return output


def update_hk_instruments(
    instruments: list[dict[str, Any]],
    hk_rows: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

        if norm_market(
            instrument.get(
                "market"
            )
        ) != "hong kong":
            continue

        bond = str(
            instrument.get(
                "bond"
            )
            or ""
        ).lower()

        isin = str(
            instrument.get(
                "isin"
            )
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
                    ).strip()
                    == isin
                ),
                None,
            )

        if hit is None:

            if (
                "exchange fund note"
                in bond
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
                in bond
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
# MAS HTML TABLE PARSER
# ============================================================================

class MASHTMLParser(
    HTMLParser
):

    def __init__(
        self,
    ) -> None:

        super().__init__(
            convert_charrefs=True
        )

        self.rows: list[
            list[str]
        ] = []

        self.current_row: list[
            str
        ] = []

        self.current_cell: list[
            str
        ] = []

        self.in_cell = False

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

    def handle_data(
        self,
        data: str,
    ) -> None:

        if self.in_cell:

            self.current_cell.append(
                data
            )


# ============================================================================
# MAS HELPERS
# ============================================================================

def validate_sgs_yield(
    value: float | None,
) -> float | None:

    if value is None:
        return None

    if not 0 <= value <= 20:
        return None

    return value


def validate_sgs_price(
    value: float | None,
) -> float | None:

    if value is None:
        return None

    if not 50 <= value <= 150:
        return None

    return value


def find_sgs_codes(
    text: str,
) -> list[str]:

    upper = text.upper()

    found: list[str] = []

    for code in SGS_ISSUE_CODES:

        if code in upper:

            found.append(
                code
            )

    return found


def extract_mas_date(
    text: str,
) -> str | None:

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

    if not dates:
        return None

    return dates[-1]


def row_numbers(
    row: list[str],
) -> list[float]:

    values: list[float] = []

    for cell in row:

        # Ignore dates and issue codes.
        if re.search(
            r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}",
            cell,
        ):
            continue

        if any(
            code in cell.upper()
            for code in SGS_ISSUE_CODES
        ):
            continue

        value = clean_number(
            cell
        )

        if value is not None:

            values.append(
                value
            )

    return values


def build_sgs_rows_from_numbers(
    date: str,
    numbers: list[float],
) -> list[dict[str, Any]]:

    """
    MAS benchmark sequence can contain:

        2Y Price
        2Y Yield
        5Y Price
        5Y Yield
        10Y Price
        10Y Yield
        15Y Price
        15Y Yield
        20Y Price
        20Y Yield
        30Y Price
        30Y Yield
        50Y Yield

    Some MAS HTML representations include additional
    leading columns. Therefore the final 13 values
    are treated as the SGS benchmark block.
    """

    if len(numbers) < 13:

        return []

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
            "price": block[10],
            "yield": block[11],
        },

        "NC22300W": {
            "price": block[12],
            "yield": None,
        },
    }

    output: list[
        dict[str, Any]
    ] = []

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
                "date": date,
                "source": "MAS",
            }
        )

    return output


# ============================================================================
# MAS PARSER
# ============================================================================

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
            "Singapore: MAS benchmark issue "
            "codes could not be detected."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
            "matchedIssueCodes": 0,
        }

    # ------------------------------------------------------------------
    # Strategy 1: Parse actual HTML table rows.
    # ------------------------------------------------------------------

    parser = MASHTMLParser()

    try:

        parser.feed(
            html
        )

    except Exception:

        pass

    candidate_rows: list[
        tuple[
            str,
            list[str],
        ]
    ] = []

    for row in parser.rows:

        joined = " ".join(
            row
        )

        date_match = re.search(
            r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b",
            joined,
        )

        if not date_match:
            continue

        numbers = row_numbers(
            row
        )

        if len(numbers) >= 13:

            candidate_rows.append(
                (
                    date_match.group(1),
                    row,
                )
            )

    if candidate_rows:

        latest_date, latest_row = (
            candidate_rows[-1]
        )

        numbers = row_numbers(
            latest_row
        )

        rows = build_sgs_rows_from_numbers(
            latest_date,
            numbers,
        )

        if rows:

            matched = len(
                {
                    row[
                        "issue_code"
                    ]
                    for row in rows
                }
            )

            log.append(
                "Singapore: MAS live benchmark "
                "data parsed for "
                f"{matched}/"
                f"{len(SGS_ISSUE_CODES)} "
                "tracked issues."
            )

            return {
                "source": "MAS",
                "status": "success",
                "date": latest_date,
                "rows": rows,
                "matchedIssueCodes": matched,
            }

    # ------------------------------------------------------------------
    # Strategy 2: Visible-text fallback.
    # ------------------------------------------------------------------

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
    ).strip()

    date_pattern = (
        r"\b\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\b"
    )

    date_matches = list(
        re.finditer(
            date_pattern,
            text,
        )
    )

    # Work backwards through dates.
    for date_match in reversed(
        date_matches
    ):

        date = date_match.group()

        after = text[
            date_match.end():
        ]

        # Limit the search window.
        after = after[:4000]

        numbers: list[
            float
        ] = []

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

        if len(numbers) < 13:
            continue

        rows = build_sgs_rows_from_numbers(
            date,
            numbers,
        )

        if not rows:
            continue

        matched = len(
            {
                row[
                    "issue_code"
                ]
                for row in rows
            }
        )

        log.append(
            "Singapore: MAS live benchmark "
            "data parsed for "
            f"{matched}/"
            f"{len(SGS_ISSUE_CODES)} "
            "tracked issues using "
            "visible-text fallback."
        )

        return {
            "source": "MAS",
            "status": "success",
            "date": date,
            "rows": rows,
            "matchedIssueCodes": matched,
        }

    # ------------------------------------------------------------------
    # Safe failure.
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


# ============================================================================
# UPDATE SINGAPORE INSTRUMENTS
# ============================================================================

def update_singapore_instruments(
    instruments: list[dict[str, Any]],
    mas: dict[str, Any],
) -> None:

    rows = mas.get(
        "rows",
        [],
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
            instrument.get(
                "market"
            )
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
            instrument.get(
                "market"
            )
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
    # 1. Load Phase 1 universe
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
                    payload[
                        "updatedAt"
                    ],
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
    # 2. USA
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
    # 3. Hong Kong
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

            # IMPORTANT:
            # Current official MAS benchmark page.
            "singapore":
                MAS_URL,

            "hongkong": (
                "https://apidocs.hkma.gov.hk/"
                "documentation/market-data-and-statistics/"
                "daily-monetary-statistics/"
                "efbn-indicative-price"
            ),

            "india":
                "https://data.rbi.org.in/",
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
        f"Instruments    : "
        f"{len(instruments)}"
    )

    print(
        "USA curve      : "
        f"{us.get('status', 'unknown').upper()}"
    )

    if (
        isinstance(hk, list)
        and hk
    ):

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


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()
