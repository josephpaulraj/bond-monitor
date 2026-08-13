#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.4

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
import urllib.error
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
    "bond-monitor/3.4 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

HTTP_TIMEOUT = 90
HTTP_RETRIES = 5
HTTP_RETRY_DELAY = 4


# ============================================================================
# Official source URLs
# ============================================================================

TREASURY_URL = (
    "https://home.treasury.gov/resource-center/"
    "data-chart-center/interest-rates/pages/xml"
)

HKMA_URL = (
    "https://api.hkma.gov.hk/public/"
    "market-data-and-statistics/daily-monetary-statistics/"
    "efbn-indicative-price"
    "?segment=IndicativePrice&offset=0"
)

MAS_URLS = [
    # Current MAS SGS benchmark page.
    "https://eservices.mas.gov.sg/Statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx",

    # Alternate MAS route.
    "https://eservices.mas.gov.sg/Statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx/BenchmarkPricesAndYields.aspx",

    # Older/alternate route.
    "https://eservices.mas.gov.sg/Statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx/BondPricesAndYields.aspx",
]


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
) -> bytes:
    """
    Fetch a URL with retries and exponential backoff.

    This is intentionally more tolerant of temporary HTTP 502/503/504
    errors because public government sites can occasionally return
    transient gateway errors.
    """

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": (
                        "text/html,application/json,"
                        "application/xml,text/xml,*/*"
                    ),
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=timeout,
                context=CTX,
            ) as response:
                return response.read()

        except urllib.error.HTTPError as exc:
            last_error = exc

            # Retry transient gateway/server errors.
            if exc.code in {429, 500, 502, 503, 504}:
                if attempt < retries:
                    delay = HTTP_RETRY_DELAY * attempt
                    time.sleep(delay)
                    continue

            raise

        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as exc:
            last_error = exc

            if attempt < retries:
                delay = HTTP_RETRY_DELAY * attempt
                time.sleep(delay)
                continue

    raise RuntimeError(str(last_error))


def fetch_first_working(
    urls: list[str],
    timeout: int = HTTP_TIMEOUT,
    retries: int = HTTP_RETRIES,
) -> tuple[bytes, str]:
    """
    Try multiple official URLs and return the first successful response.
    """

    errors: list[str] = []

    for url in urls:
        try:
            raw = fetch(
                url,
                timeout=timeout,
                retries=retries,
            )

            return raw, url

        except Exception as exc:
            errors.append(
                f"{url}: {exc}"
            )

    raise RuntimeError(
        "All configured source URLs failed: "
        + " | ".join(errors)
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
    )

    try:
        return float(text)
    except ValueError:
        return None


def norm_market(
    value: Any,
) -> str:
    return str(
        value or ""
    ).strip().lower()


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

        if not isinstance(obj, dict):
            log.append(
                f"{market}: ERROR {path.name} "
                f"is not a JSON object"
            )
            continue

        records = obj.get(
            "records",
            [],
        )

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
        f"from Phase 1 country files."
    )

    return instruments


# ============================================================================
# USA - U.S. Treasury
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
        timeout=90,
        retries=5,
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

        old_value = instrument.get(
            "yield"
        )

        if old_value is not None:
            instrument["previousYield"] = (
                old_value
            )

        instrument["yield"] = value
        instrument["liveYield"] = value
        instrument["liveDate"] = date

        instrument["dataStatus"] = (
            "live U.S. Treasury benchmark yield"
        )


# ============================================================================
# Hong Kong - HKMA
# ============================================================================

def update_hkma(
    log: list[str],
) -> list[dict[str, Any]]:

    """
    Retrieve HKMA EFBN indicative pricing.

    The HKMA documentation confirms that this endpoint provides:
      end_of_date
      term
      issue_no
      yield
      price

    It provides the latest business day's data.
    """

    raw = fetch(
        HKMA_URL,
        timeout=120,
        retries=6,
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

    if not output:
        raise RuntimeError(
            "HKMA returned zero indicative-price rows"
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
            instrument.get("bond")
            or ""
        )

        isin = str(
            instrument.get("isin")
            or ""
        )

        hit = None

        # --------------------------------------------------------
        # Direct issue-number/ISIN match.
        # --------------------------------------------------------

        if isin and isin != "—":

            hit = next(
                (
                    row
                    for row in hk_rows
                    if str(
                        row.get("issue_no")
                        or ""
                    ).strip()
                    == isin.strip()
                ),
                None,
            )

        # --------------------------------------------------------
        # Exchange Fund Note / Bill fallback.
        # --------------------------------------------------------

        if hit is None:

            bond_lower = bond.lower()

            if "exchange fund note" in bond_lower:

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

            elif "exchange fund bill" in bond_lower:

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

        if hit.get("yield") is not None:

            old_value = instrument.get(
                "yield"
            )

            if old_value is not None:
                instrument["previousYield"] = (
                    old_value
                )

            instrument["yield"] = (
                hit["yield"]
            )

            instrument["liveYield"] = (
                hit["yield"]
            )

            changed = True

        if hit.get("price") is not None:

            old_value = instrument.get(
                "price"
            )

            if old_value is not None:
                instrument["previousPrice"] = (
                    old_value
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
# Singapore - MAS
# ============================================================================

class HTMLTableParser(HTMLParser):
    """
    Generic HTML table parser.

    Unlike the previous version, this parser preserves the actual
    table structure instead of flattening the entire page into text.
    """

    def __init__(self) -> None:
        super().__init__()

        self.in_table = False
        self.table_depth = 0

        self.in_row = False
        self.in_cell = False

        self.current_row: list[str] = []
        self.current_cell: list[str] = []

        self.tables: list[list[list[str]]] = []
        self.current_table: list[list[str]] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:

        tag = tag.lower()

        if tag == "table":

            if not self.in_table:
                self.in_table = True
                self.table_depth = 1
                self.current_table = []
            else:
                self.table_depth += 1

            return

        if not self.in_table:
            return

        if tag == "tr":

            self.in_row = True
            self.current_row = []

            return

        if tag in ("td", "th"):

            if self.in_row:

                self.in_cell = True
                self.current_cell = []

    def handle_endtag(
        self,
        tag: str,
    ) -> None:

        tag = tag.lower()

        if tag in ("td", "th"):

            if self.in_cell:

                text = " ".join(
                    "".join(
                        self.current_cell
                    ).split()
                )

                self.current_row.append(
                    text
                )

                self.current_cell = []
                self.in_cell = False

            return

        if tag == "tr":

            if (
                self.in_row
                and self.current_row
                and self.current_table is not None
            ):
                self.current_table.append(
                    self.current_row
                )

            self.current_row = []
            self.in_row = False

            return

        if tag == "table":

            if self.in_table:

                self.table_depth -= 1

                if self.table_depth == 0:

                    if self.current_table is not None:
                        self.tables.append(
                            self.current_table
                        )

                    self.current_table = None
                    self.in_table = False

    def handle_data(
        self,
        data: str,
    ) -> None:

        if self.in_cell:
            self.current_cell.append(
                data
            )


SGS_ISSUE_CODES = {
    "N523100W",
    "NX21100N",
    "NZ16100X",
    "NY25200N",
    "NA16100H",
    "NC22300W",
}


def normalize_cell(
    value: Any,
) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(value or "")
    ).strip()


def is_date_cell(
    value: str,
) -> bool:
    return bool(
        re.fullmatch(
            r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}",
            normalize_cell(value),
        )
    )


def find_mas_benchmark_table(
    parser: HTMLTableParser,
) -> list[list[str]] | None:
    """
    Find the MAS table containing the tracked SGS issue codes.

    The current official MAS page exposes the Closing Levels table
    with issue codes such as N523100W, NX21100N, NZ16100X, etc.
    """

    best_table = None
    best_score = 0

    for table in parser.tables:

        flat = [
            normalize_cell(cell).upper()
            for row in table
            for cell in row
        ]

        score = sum(
            1
            for code in SGS_ISSUE_CODES
            if code in flat
        )

        if score > best_score:

            best_score = score
            best_table = table

    return best_table


def extract_mas_header_codes(
    table: list[list[str]],
) -> dict[str, int]:
    """
    Return issue-code -> column position.

    The MAS table has the benchmark issue codes in a header row,
    followed by the corresponding coupon/maturity information.
    """

    mapping: dict[str, int] = {}

    for row in table:

        for index, cell in enumerate(row):

            code = normalize_cell(
                cell
            ).upper()

            if code in SGS_ISSUE_CODES:

                mapping[code] = index

        if len(mapping) >= 5:
            break

    return mapping


def parse_mas_closing_levels(
    html: str,
    log: list[str],
) -> dict[str, Any]:
    """
    Parse the MAS Closing Levels table directly.

    Expected benchmark layout:

      6-Mth Yield
      1-Year Yield
      2-Year Price/Yield
      5-Year Price/Yield
      10-Year Price/Yield
      15-Year Price/Yield
      20-Year Price/Yield
      30-Year Price/Yield
      50-Year Yield

    The resulting numeric sequence therefore has 15 values.
    """

    parser = HTMLTableParser()
    parser.feed(html)

    table = find_mas_benchmark_table(
        parser
    )

    if not table:

        log.append(
            "Singapore: MAS page fetched, "
            "but no tracked SGS issue codes "
            "were detected."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
        }

    found_codes = {
        normalize_cell(cell).upper()
        for row in table
        for cell in row
    }.intersection(
        SGS_ISSUE_CODES
    )

    log.append(
        f"Singapore: MAS page fetched; "
        f"found {len(found_codes)}/6 tracked "
        f"issue codes."
    )

    # ------------------------------------------------------------
    # Find rows containing dates.
    # ------------------------------------------------------------

    date_rows: list[
        tuple[str, list[str]]
    ] = []

    for row in table:

        for cell in row:

            text = normalize_cell(
                cell
            )

            if is_date_cell(text):

                date_rows.append(
                    (
                        text,
                        row,
                    )
                )

                break

    if not date_rows:

        log.append(
            "Singapore: MAS Closing Levels table "
            "found, but no dated closing row "
            "could be parsed."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_date",
            "rows": [],
        }

    # ------------------------------------------------------------
    # The latest row is normally the final dated row.
    # Parse all rows and select the latest actual date.
    # ------------------------------------------------------------

    def parse_date(
        text: str,
    ) -> datetime | None:

        try:
            return datetime.strptime(
                text,
                "%d %b %Y",
            )
        except ValueError:
            return None

    dated = []

    for date_text, row in date_rows:

        parsed = parse_date(
            date_text
        )

        if parsed is not None:

            dated.append(
                (
                    parsed,
                    date_text,
                    row,
                )
            )

    if not dated:

        return {
            "source": "MAS",
            "status": "fetched_no_date",
            "rows": [],
        }

    dated.sort(
        key=lambda item: item[0]
    )

    _, latest_date, latest_row = dated[-1]

    # ------------------------------------------------------------
    # Extract numeric cells from the latest row.
    # ------------------------------------------------------------

    numeric_values: list[float] = []

    for cell in latest_row:

        value = normalize_cell(
            cell
        )

        # Date itself is not a numeric market value.
        if is_date_cell(value):
            continue

        # Ignore placeholders.
        if value in {
            "",
            "-",
            "—",
            "–",
            "NA",
            "N/A",
        }:
            continue

        number = clean_number(
            value
        )

        if number is not None:
            numeric_values.append(
                number
            )

    # ------------------------------------------------------------
    # The MAS benchmark row contains:
    #
    # 6M yield
    # 1Y yield
    # 2Y price/yield
    # 5Y price/yield
    # 10Y price/yield
    # 15Y price/yield
    # 20Y price/yield
    # 30Y price/yield
    # 50Y yield
    #
    # = 15 numeric values.
    # ------------------------------------------------------------

    if len(numeric_values) < 15:

        log.append(
            "Singapore: MAS Closing Levels row "
            f"for {latest_date} contained only "
            f"{len(numeric_values)} numeric values; "
            "expected at least 15."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "date": latest_date,
            "rows": [],
        }

    values = numeric_values[:15]

    benchmark_values = {
        "N523100W": {
            "yield": values[2],
            "price": values[3],
        },
        "NX21100N": {
            "yield": values[4],
            "price": values[5],
        },
        "NZ16100X": {
            "yield": values[6],
            "price": values[7],
        },
        "NY25200N": {
            "yield": values[8],
            "price": values[9],
        },
        "NA16100H": {
            "yield": values[12],
            "price": values[13],
        },
        "NC22300W": {
            "yield": values[14],
            "price": None,
        },
    }

    rows: list[dict[str, Any]] = []

    for issue_code, data in benchmark_values.items():

        yield_value = data.get(
            "yield"
        )

        price_value = data.get(
            "price"
        )

        # SGS yields should be reasonable percentages.
        if (
            yield_value is not None
            and not 0 <= yield_value <= 20
        ):
            yield_value = None

        # SGS clean prices should normally be within this
        # broad sanity range.
        if (
            price_value is not None
            and not 50 <= price_value <= 150
        ):
            price_value = None

        if (
            yield_value is not None
            or price_value is not None
        ):

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
            "Singapore: MAS Closing Levels table "
            "was found, but no valid benchmark "
            "values passed validation."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "date": latest_date,
            "rows": [],
        }

    log.append(
        f"Singapore: MAS closing levels parsed "
        f"for {len(rows)}/6 tracked benchmark "
        f"issues ({latest_date})."
    )

    return {
        "source": "MAS",
        "status": "success",
        "date": latest_date,
        "rows": rows,
    }


def update_mas(
    log: list[str],
) -> dict[str, Any]:

    errors: list[str] = []

    for url in MAS_URLS:

        try:

            html = fetch(
                url,
                timeout=120,
                retries=4,
            ).decode(
                "utf-8",
                errors="replace",
            )

            result = parse_mas_closing_levels(
                html,
                log,
            )

            # A successful parse wins immediately.
            if result.get("rows"):
                result["url"] = url
                return result

            # Keep trying alternate official URLs.
            errors.append(
                f"{url}: "
                f"{result.get('status')}"
            )

        except Exception as exc:

            errors.append(
                f"{url}: {exc}"
            )

    # Do not convert a temporary MAS failure into
    # an overall updater ERROR. Existing Phase 1 values
    # remain untouched.
    log.append(
        "Singapore: MAS source could not provide "
        "a parseable Closing Levels table; "
        "existing instrument values were preserved."
    )

    return {
        "source": "MAS",
        "status": "fetched_no_table",
        "rows": [],
        "errors": errors,
    }


def update_singapore_instruments(
    instruments: list[dict[str, Any]],
    mas: dict[str, Any],
) -> None:

    rows = mas.get(
        "rows",
        []
    )

    if not isinstance(rows, list):
        return

    # ------------------------------------------------------------
    # Confirmed MAS SGS issue-code / ISIN mappings used by the
    # Phase 1 Singapore instrument universe.
    # ------------------------------------------------------------

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
            instrument.get("issue_code")
            or ""
        ).strip().upper()

        isin = str(
            instrument.get("isin")
            or ""
        ).strip().upper()

        if (
            not issue_code
            and isin
        ):
            issue_code = isin_to_issue_code.get(
                isin,
                "",
            )

        if not issue_code:
            continue

        hit = next(
            (
                row
                for row in rows
                if str(
                    row.get("issue_code")
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

        if hit.get("yield") is not None:

            old_value = instrument.get(
                "yield"
            )

            if old_value is not None:
                instrument["previousYield"] = (
                    old_value
                )

            instrument["yield"] = (
                hit["yield"]
            )

            instrument["liveYield"] = (
                hit["yield"]
            )

            changed = True

        # --------------------------------------------------------
        # Price
        # --------------------------------------------------------

        if hit.get("price") is not None:

            old_value = instrument.get(
                "price"
            )

            if old_value is not None:
                instrument["previousPrice"] = (
                    old_value
                )

            instrument["price"] = (
                hit["price"]
            )

            instrument["livePrice"] = (
                hit["price"]
            )

            changed = True

        # --------------------------------------------------------
        # Date
        # --------------------------------------------------------

        if changed:

            instrument["liveDate"] = (
                hit.get("date")
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

        # Deliberately do not fabricate live values.

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
# Final normalization
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
# Main
# ============================================================================

def main() -> None:

    DATA.mkdir(
        exist_ok=True
    )

    log: list[str] = []

    # ------------------------------------------------------------------------
    # 1. Load Phase 1 instrument universe.
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
            "schemaVersion": "3.4",
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

    # ------------------------------------------------------------------------
    # 2. USA Treasury.
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
            "source": "U.S. Treasury",
            "status": "error",
            "error": str(exc),
            "curve": {},
        }

        log.append(
            f"USA: ERROR {exc}"
        )

    # ------------------------------------------------------------------------
    # 3. Hong Kong HKMA.
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
    # 4. Singapore MAS.
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
            "source": "MAS",
            "status": "error",
            "error": str(exc),
            "rows": [],
        }

        log.append(
            f"Singapore: ERROR {exc}"
        )

    # ------------------------------------------------------------------------
    # 5. India.
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
    # 6. Final status.
    # ------------------------------------------------------------------------

    status = calculate_status(
        log,
        instruments,
    )

    updated_at = now_utc()

    payload = {
        "schemaVersion": "3.4",

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

    # ------------------------------------------------------------------------
    # IMPORTANT:
    #
    # live.json is written only after the full 22-instrument universe
    # has been confirmed.
    # ------------------------------------------------------------------------

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

    # ------------------------------------------------------------------------
    # Console summary.
    # ------------------------------------------------------------------------

    print(
        "=========================================="
    )

    print(
        " Bond Monitor Phase 3 Update v3.4"
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

    if isinstance(hk, list) and hk:
        hk_status = "OK"
    else:
        hk_status = "ERROR/PARTIAL"

    print(
        "Hong Kong      : "
        f"{hk_status}"
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
