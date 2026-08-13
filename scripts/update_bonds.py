#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.5

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

v3.5 changes
------------

1. HKMA timeout reduced significantly.
   A failed HKMA connection must NOT make the GitHub Action run for
   several minutes.

2. Singapore MAS parsing no longer depends on searching the extracted
   text for the literal "Closing Levels" heading.

3. Singapore parser identifies the actual daily closing row from the
   HTML table using dates and numeric values.

4. Singapore parser maps the six tracked SGS benchmark issue codes:
       N523100W  -> 2Y
       NX21100N  -> 5Y
       NZ16100X  -> 10Y
       NY25200N  -> 15Y
       NA16100H  -> 30Y
       NC22300W  -> 50Y

5. Existing Phase 1 values are always preserved if live data cannot
   be obtained.

6. The complete update should normally finish quickly even if HKMA
   is unavailable.
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
    "bond-monitor/3.5 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

# IMPORTANT:
# Keep these short so a single unavailable source cannot block the
# entire GitHub Action for several minutes.
HTTP_TIMEOUT = 15
HTTP_RETRIES = 1
HTTP_RETRY_DELAY = 2


# ============================================================================
# Official source URLs
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

MAS_URL = (
    "https://eservices.mas.gov.sg/"
    "Statistics/fdanet/"
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
            f"{label}: HTTP attempt "
            f"{attempt}/{retries}"
        )

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

        except urllib.error.HTTPError as exc:

            last_error = exc

            print(
                f"{label}: HTTP error "
                f"{exc.code} {exc.reason}"
            )

        except urllib.error.URLError as exc:

            last_error = exc

            print(
                f"{label}: connection error "
                f"{exc.reason}"
            )

        except TimeoutError as exc:

            last_error = exc

            print(
                f"{label}: timeout "
                f"{exc}"
            )

        except Exception as exc:

            last_error = exc

            print(
                f"{label}: connection/timeout error "
                f"{exc}"
            )

        if attempt < retries:
            time.sleep(HTTP_RETRY_DELAY)

    raise RuntimeError(
        str(last_error)
        if last_error
        else "Unknown HTTP error"
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
                f"{market}: ERROR missing "
                f"{path.name}"
            )

            continue

        obj = load_json(
            path,
            {},
        )

        if not isinstance(obj, dict):

            log.append(
                f"{market}: ERROR "
                f"{path.name} is not a JSON object"
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

            if isinstance(
                record,
                dict,
            ):

                instruments.append(
                    dict(record)
                )

                count += 1

        log.append(
            f"{market}: loaded "
            f"{count} instruments from "
            f"{path.name}"
        )

    log.append(
        f"Loaded {len(instruments)} instruments "
        f"from Phase 1 country files."
    )

    return instruments


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
        timeout=15,
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

    # IMPORTANT:
    # HKMA is currently the least reliable endpoint in this workflow.
    # Use only one short attempt so the workflow cannot hang.
    raw = fetch(
        HKMA_URL,
        timeout=15,
        retries=1,
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

        if isin and isin != "—":

            hit = next(
                (
                    row
                    for row in hk_rows
                    if str(
                        row.get("issue_no")
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
# Singapore MAS HTML parser
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
            tuple[str, str | None]
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
# Singapore MAS
# ============================================================================

MAS_ISSUE_CODES = {
    "N523100W": "2Y",
    "NX21100N": "5Y",
    "NZ16100X": "10Y",
    "NY25200N": "15Y",
    "NA16100H": "30Y",
    "NC22300W": "50Y",
}


def parse_mas_date(
    value: str,
) -> datetime | None:

    value = value.strip()

    formats = [
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
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


def extract_mas_rows(
    parser: TextTableParser,
) -> list[
    tuple[datetime, str, list[str]]
]:

    candidates: list[
        tuple[
            datetime,
            str,
            list[str],
        ]
    ] = []

    date_pattern = re.compile(
        r"^\s*"
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
        r"\s*$",
        re.IGNORECASE,
    )

    for row in parser.rows:

        if not row:
            continue

        date_value = None

        # The daily closing row normally begins with the date.
        for cell in row[:2]:

            match = date_pattern.match(
                str(cell)
            )

            if match:

                date_value = (
                    match.group(1)
                )

                break

        if not date_value:
            continue

        parsed_date = parse_mas_date(
            date_value
        )

        if parsed_date is None:
            continue

        candidates.append(
            (
                parsed_date,
                date_value,
                row,
            )
        )

    return candidates


def numeric_cells(
    row: list[str],
) -> list[float | None]:

    result: list[
        float | None
    ] = []

    for cell in row:

        text = str(
            cell or ""
        ).strip()

        if not text:
            result.append(None)
            continue

        if text in {
            "-",
            "—",
            "–",
            "NA",
            "N/A",
        }:

            result.append(None)
            continue

        result.append(
            clean_number(text)
        )

    return result


def update_mas(
    log: list[str],
) -> dict[str, Any]:

    html = fetch(
        MAS_URL,
        timeout=20,
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

    # ---------------------------------------------------------------
    # Confirm that the six tracked issue codes are present.
    # ---------------------------------------------------------------

    html_upper = html.upper()

    found_codes = [
        code
        for code in MAS_ISSUE_CODES
        if code in html_upper
    ]

    log.append(
        "Singapore: MAS page fetched; "
        f"found {len(found_codes)}/"
        f"{len(MAS_ISSUE_CODES)} "
        "tracked issue codes."
    )

    if not found_codes:

        return {
            "source": "MAS",
            "status": "fetched_no_issue_codes",
            "rows": [],
        }

    # ---------------------------------------------------------------
    # Find daily rows directly from parsed HTML tables.
    #
    # This avoids depending on the literal "Closing Levels"
    # heading being present in extracted text.
    # ---------------------------------------------------------------

    date_rows = extract_mas_rows(
        parser
    )

    if not date_rows:

        log.append(
            "Singapore: MAS daily closing rows "
            "could not be parsed."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
        }

    # ---------------------------------------------------------------
    # Select the newest daily row.
    # ---------------------------------------------------------------

    date_rows.sort(
        key=lambda item: item[0]
    )

    latest_date_obj, latest_date_text, latest_row = (
        date_rows[-1]
    )

    values = numeric_cells(
        latest_row
    )

    # ---------------------------------------------------------------
    # MAS closing table structure:
    #
    # 6M Yield
    # 1Y Yield
    # 2Y Price
    # 2Y Yield
    # 5Y Price
    # 5Y Yield
    # 10Y Price
    # 10Y Yield
    # 15Y Price
    # 15Y Yield
    # 20Y Price
    # 20Y Yield
    # 30Y Price
    # 30Y Yield
    # 50Y Yield
    #
    # The first cell is the date.
    #
    # Depending on HTML colspan/rowspan handling, there may be
    # empty cells. Therefore we don't assume the date is part of
    # the numeric sequence.
    # ---------------------------------------------------------------

    numeric_values = [
        value
        for value in values[1:]
        if value is not None
    ]

    # We need at least the 15 benchmark numeric values.
    if len(numeric_values) < 15:

        log.append(
            "Singapore: MAS latest daily row contains "
            f"only {len(numeric_values)} numeric values; "
            "expected at least 15."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "rows": [],
        }

    # ---------------------------------------------------------------
    # The MAS page has:
    #
    # 6M yield
    # 1Y yield
    # 2Y price
    # 2Y yield
    # 5Y price
    # 5Y yield
    # 10Y price
    # 10Y yield
    # 15Y price
    # 15Y yield
    # 20Y price
    # 20Y yield
    # 30Y price
    # 30Y yield
    # 50Y yield
    #
    # We extract the six tracked issues.
    # ---------------------------------------------------------------

    benchmark_values = {
        "N523100W": {
            "yield": numeric_values[3],
            "price": numeric_values[2],
        },

        "NX21100N": {
            "yield": numeric_values[5],
            "price": numeric_values[4],
        },

        "NZ16100X": {
            "yield": numeric_values[7],
            "price": numeric_values[6],
        },

        "NY25200N": {
            "yield": numeric_values[9],
            "price": numeric_values[8],
        },

        "NA16100H": {
            "yield": numeric_values[13],
            "price": numeric_values[12],
        },

        "NC22300W": {
            "yield": numeric_values[14],
            "price": None,
        },
    }

    rows: list[
        dict[str, Any]
    ] = []

    # ---------------------------------------------------------------
    # Sanity-check values.
    # ---------------------------------------------------------------

    for issue_code, data in (
        benchmark_values.items()
    ):

        yield_value = data.get(
            "yield"
        )

        price_value = data.get(
            "price"
        )

        if (
            yield_value is not None
            and not 0 <= yield_value <= 20
        ):

            yield_value = None

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
                    "date": latest_date_text,
                    "source": "MAS",
                }
            )

    if not rows:

        log.append(
            "Singapore: MAS daily row found, "
            "but no valid benchmark values "
            "passed validation."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "date": latest_date_text,
            "rows": [],
        }

    log.append(
        "Singapore: MAS closing levels parsed for "
        f"{len(rows)}/6 tracked benchmark issues "
        f"({latest_date_text})."
    )

    return {
        "source": "MAS",
        "status": "success",
        "date": latest_date_text,
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

    instruments = load_instrument_universe(
        log
    )

    if not instruments:

        log.append(
            "ERROR: No instruments were loaded. "
            "Existing country files were not modified."
        )

        payload = {
            "schemaVersion": "3.5",
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
            "source": "U.S. Treasury",
            "status": "error",
            "error": str(exc),
            "curve": {},
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

        # IMPORTANT:
        # HKMA failure is recorded but does not stop the update.
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
            "source": "MAS",
            "status": "error",
            "error": str(exc),
            "rows": [],
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
            "India: Phase 1 RBI instrument universe "
            "preserved; no unverified live quote "
            "was inferred."
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
        "schemaVersion": "3.5",
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
                "https://api.hkma.gov.hk/"
                "public/market-data-and-statistics/"
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
    # 7. Write output files
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
    # 8. Console summary
    # ------------------------------------------------------------------------

    print(
        "=========================================="
    )

    print(
        " Bond Monitor Phase 3 Update v3.5"
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
        f"{'OK' if isinstance(hk, list) and hk "
        "else 'ERROR/PARTIAL'}"
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
