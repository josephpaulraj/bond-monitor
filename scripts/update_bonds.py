#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.9

Purpose
-------
Load the original Bond Monitor instrument universe from:

    data/usa.json
    data/singapore.json
    data/hongkong.json
    data/india.json

Then update live values only when explicitly available from
official sources.

Important design principles
---------------------------
1. Never fabricate a price or yield.
2. Never delete an existing value because a source temporarily fails.
3. Never replace a valid existing value with null because parsing failed.
4. Preserve all 22 Phase 1 instruments.
5. Use short HTTP timeouts so GitHub Actions cannot hang for many minutes.
6. Singapore MAS parsing is based on the actual SGS benchmark table.
7. Singapore matching is based primarily on issue_code already present
   in singapore.json, with known instrument metadata used only as fallback.
8. Write:
       data/live.json
       data/last-update.json
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
    "bond-monitor/3.9 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

# Keep these deliberately short.
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
    "https://api.hkma.gov.hk/public/"
    "market-data-and-statistics/"
    "daily-monetary-statistics/"
    "efbn-indicative-price"
    "?segment=IndicativePrice&offset=0"
)

# This is the actual MAS SGS Daily Prices / Benchmarks page.
MAS_URL = (
    "https://eservices.mas.gov.sg/"
    "Statistics/fdanet/"
    "SgsBenchmarkIssuePrices.aspx"
)


# ============================================================================
# TRACKED SINGAPORE SGS ISSUE CODES
# ============================================================================

# These are the benchmark issues used by our Phase 1 Singapore universe.

MAS_TRACKED_ISSUES = {
    "N523100W": {
        "tenor": "2Y",
        "instrument_name": "2.875% SGS 2028",
    },
    "NX21100N": {
        "tenor": "5Y",
        "instrument_name": "1.625% SGS 2031",
    },
    "NZ16100X": {
        "tenor": "10Y",
        "instrument_name": "2.250% SGS 2036",
    },
    "NY25200N": {
        "tenor": "15Y",
        "instrument_name": "2.250% SGS 2040",
    },
    "NA16100H": {
        "tenor": "30Y",
        "instrument_name": "2.750% SGS 2046",
    },
    "NC22300W": {
        "tenor": "50Y",
        "instrument_name": "3.000% SGS 2072",
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


def fetch(
    url: str,
    label: str,
    timeout: int = HTTP_TIMEOUT,
    retries: int = HTTP_RETRIES,
) -> bytes:

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):

        print(
            f"{label}: HTTP attempt "
            f"{attempt}/{retries}"
        )

        try:

            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml,application/json,*/*"
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

        except Exception as exc:

            last_error = exc

            print(
                f"{label}: connection/timeout error "
                f"{exc}"
            )

            if attempt < retries:
                time.sleep(
                    HTTP_RETRY_DELAY
                )

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
        .strip()
    )

    if text in {
        "-",
        "—",
        "–",
        "n/a",
        "na",
        "null",
    }:

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
        f"instruments from Phase 1 country files."
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

    year = (
        datetime.now(
            timezone.utc
        ).year
    )

    url = (
        TREASURY_URL
        + "?data=daily_treasury_yield_curve"
        + f"&field_tdr_date_value={year}"
    )

    raw = fetch(
        url,
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
# HONG KONG HKMA
# ============================================================================

def update_hkma(
    log: list[str],
) -> list[dict[str, Any]]:

    raw = fetch(
        HKMA_URL,
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

    result = obj.get(
        "result"
    ) or {}

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
            "HKMA response did not "
            "contain records"
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

            bond_lower = (
                bond.lower()
            )

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
                hit.get("date")
            )

            instrument["dataStatus"] = (
                "live HKMA indicative data"
            )


# ============================================================================
# HTML TABLE PARSER
# ============================================================================

class MASHTMLParser(
    HTMLParser
):

    def __init__(
        self,
    ) -> None:

        super().__init__()

        self.tables: list[
            list[list[str]]
        ] = []

        self.current_table: list[
            list[str]
        ] | None = None

        self.current_row: list[
            str
        ] | None = None

        self.in_cell = False

        self.cell_buffer: list[
            str
        ] = []

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

            self.current_table = []

        elif (
            tag == "tr"
            and self.current_table
            is not None
        ):

            self.current_row = []

        elif (
            tag in {"td", "th"}
            and self.current_row
            is not None
        ):

            self.in_cell = True
            self.cell_buffer = []

    def handle_endtag(
        self,
        tag: str,
    ) -> None:

        tag = tag.lower()

        if (
            tag in {"td", "th"}
            and self.in_cell
        ):

            text = " ".join(
                "".join(
                    self.cell_buffer
                ).split()
            )

            if self.current_row is not None:

                self.current_row.append(
                    text
                )

            self.in_cell = False
            self.cell_buffer = []

        elif (
            tag == "tr"
            and self.current_row
            is not None
        ):

            if self.current_table is not None:

                if self.current_row:

                    self.current_table.append(
                        self.current_row
                    )

            self.current_row = None

        elif tag == "table":

            if self.current_table is not None:

                self.tables.append(
                    self.current_table
                )

            self.current_table = None

    def handle_data(
        self,
        data: str,
    ) -> None:

        if self.in_cell:

            self.cell_buffer.append(
                data
            )


# ============================================================================
# MAS HELPERS
# ============================================================================

def normalize_cell(
    value: Any,
) -> str:

    return re.sub(
        r"\s+",
        " ",
        str(
            value or ""
        ).strip()
    )


def row_contains_issue_code(
    row: list[str],
) -> bool:

    joined = " ".join(
        normalize_cell(
            cell
        ).upper()
        for cell in row
    )

    return any(
        code in joined
        for code in MAS_TRACKED_ISSUES
    )


def table_contains_mas_benchmarks(
    table: list[list[str]],
) -> bool:

    code_count = 0

    for row in table:

        for cell in row:

            cell_upper = (
                normalize_cell(
                    cell
                ).upper()
            )

            for code in MAS_TRACKED_ISSUES:

                if code in cell_upper:

                    code_count += 1

    return code_count >= 3


def parse_date_cell(
    value: str,
) -> datetime | None:

    value = normalize_cell(
        value
    )

    formats = [
        "%d %b %Y",
        "%d %B %Y",
        "%d-%b-%Y",
        "%d/%m/%Y",
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


def is_date_string(
    value: str,
) -> bool:

    return (
        parse_date_cell(
            value
        )
        is not None
    )


def extract_numeric_cells(
    row: list[str],
) -> list[float | None]:

    output: list[
        float | None
    ] = []

    for cell in row:

        value = normalize_cell(
            cell
        )

        if value in {
            "",
            "-",
            "—",
            "–",
        }:

            output.append(
                None
            )

            continue

        number = clean_number(
            value
        )

        output.append(
            number
        )

    return output


# ============================================================================
# MAS TABLE PARSER
# ============================================================================

def parse_mas_benchmark_table(
    html: str,
    log: list[str],
) -> dict[str, Any]:

    parser = MASHTMLParser()

    parser.feed(
        html
    )

    benchmark_tables: list[
        list[list[str]]
    ] = []

    for table in parser.tables:

        if table_contains_mas_benchmarks(
            table
        ):

            benchmark_tables.append(
                table
            )

    detected_codes: set[str] = set()

    for table in benchmark_tables:

        for row in table:

            for cell in row:

                upper = normalize_cell(
                    cell
                ).upper()

                for code in MAS_TRACKED_ISSUES:

                    if code in upper:

                        detected_codes.add(
                            code
                        )

    log.append(
        "Singapore MAS: page fetched; "
        f"found {len(detected_codes)}/"
        f"{len(MAS_TRACKED_ISSUES)} "
        "tracked issue codes."
    )

    if not benchmark_tables:

        # The table parser did not recognize the structure.
        # Try a conservative text fallback only to diagnose
        # whether the issue codes are actually present.

        visible = re.sub(
            r"<script.*?</script>",
            " ",
            html,
            flags=(
                re.IGNORECASE
                | re.DOTALL
            ),
        )

        visible = re.sub(
            r"<style.*?</style>",
            " ",
            visible,
            flags=(
                re.IGNORECASE
                | re.DOTALL
            ),
        )

        visible = re.sub(
            r"<[^>]+>",
            " ",
            visible,
        )

        visible = re.sub(
            r"\s+",
            " ",
            visible,
        )

        fallback_codes = {
            code
            for code in MAS_TRACKED_ISSUES
            if code in visible.upper()
        }

        if fallback_codes:

            log.append(
                "Singapore MAS: issue codes are "
                "present in the page text, but "
                "the benchmark HTML table could "
                "not be parsed safely."
            )

        else:

            log.append(
                "Singapore MAS: benchmark issue "
                "codes could not be detected."
            )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
            "detectedIssueCodes": sorted(
                fallback_codes
            ),
        }

    # ------------------------------------------------------------------
    # MAS benchmark table layout
    #
    # The official Closing Levels table is:
    #
    # 6-Mth
    # 1-Year
    # 2-Year
    # 5-Year
    # 10-Year
    # 15-Year
    # 20-Year
    # 30-Year
    # 50-Year
    #
    # Data row:
    #
    # 6M Yield
    # 1Y Yield
    # 2Y Yield
    # 2Y Price
    # 5Y Yield
    # 5Y Price
    # 10Y Yield
    # 10Y Price
    # 15Y Yield
    # 15Y Price
    # 20Y Yield
    # 20Y Price
    # 30Y Yield
    # 30Y Price
    # 50Y Yield
    #
    # We do NOT hard-code actual market values.
    # We only map the structure.
    # ------------------------------------------------------------------

    # First identify the benchmark table containing
    # at least one date row.

    candidate_rows: list[
        list[str]
    ] = []

    for table in benchmark_tables:

        for row in table:

            if not row:
                continue

            first = normalize_cell(
                row[0]
            )

            if is_date_string(
                first
            ):

                candidate_rows.append(
                    row
                )

    if not candidate_rows:

        log.append(
            "Singapore MAS: benchmark issue "
            "codes were found, but no dated "
            "Closing Levels row could be parsed."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_data_row",
            "rows": [],
            "detectedIssueCodes": sorted(
                detected_codes
            ),
        }

    # ------------------------------------------------------------------
    # Choose the latest actual MAS date row.
    # ------------------------------------------------------------------

    dated_rows: list[
        tuple[
            datetime,
            list[str],
        ]
    ] = []

    for row in candidate_rows:

        dt = parse_date_cell(
            normalize_cell(
                row[0]
            )
        )

        if dt is not None:

            dated_rows.append(
                (
                    dt,
                    row,
                )
            )

    if not dated_rows:

        log.append(
            "Singapore MAS: no usable dated "
            "benchmark row was found."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_date",
            "rows": [],
        }

    dated_rows.sort(
        key=lambda item: item[0]
    )

    latest_dt, latest_row = (
        dated_rows[-1]
    )

    latest_date = latest_dt.strftime(
        "%Y-%m-%d"
    )

    # ------------------------------------------------------------------
    # Convert latest row into numeric values.
    # ------------------------------------------------------------------

    cells = [
        normalize_cell(
            cell
        )
        for cell in latest_row
    ]

    # Remove the date.
    values = extract_numeric_cells(
        cells[1:]
    )

    # MAS normally supplies 15 benchmark values.
    #
    # However, some cells may be omitted by HTML layout or
    # represented as '-' and therefore appear as None.
    #
    # We require at least the 2Y/5Y/10Y/15Y/30Y/50Y positions
    # to be addressable.
    if len(values) < 15:

        log.append(
            "Singapore MAS: latest benchmark "
            "row is incomplete; expected at "
            f"least 15 value columns, got "
            f"{len(values)}."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "rows": [],
            "date": latest_date,
        }

    # ------------------------------------------------------------------
    # Official benchmark column positions.
    #
    # values[0]  = 6M yield
    # values[1]  = 1Y yield
    #
    # values[2]  = 2Y yield
    # values[3]  = 2Y price
    #
    # values[4]  = 5Y yield
    # values[5]  = 5Y price
    #
    # values[6]  = 10Y yield
    # values[7]  = 10Y price
    #
    # values[8]  = 15Y yield
    # values[9]  = 15Y price
    #
    # values[10] = 20Y yield
    # values[11] = 20Y price
    #
    # values[12] = 30Y yield
    # values[13] = 30Y price
    #
    # values[14] = 50Y yield
    # ------------------------------------------------------------------

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

    rows: list[
        dict[str, Any]
    ] = []

    for issue_code, data in (
        benchmark_values.items()
    ):

        yield_value = data.get(
            "yield"
        )

        price_value = data.get(
            "price"
        )

        # Conservative sanity checks.
        #
        # SGS benchmark yields are expected to be
        # ordinary percentage yields.
        #
        # Prices are normally quoted around par and
        # should be well inside this broad range.
        if (
            yield_value is not None
            and not (
                0
                <= yield_value
                <= 20
            )
        ):

            yield_value = None

        if (
            price_value is not None
            and not (
                50
                <= price_value
                <= 150
            )
        ):

            price_value = None

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
            "Singapore MAS: issue codes were "
            "found, but no safe benchmark data "
            "row could be parsed."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_safe_data",
            "rows": [],
            "date": latest_date,
        }

    log.append(
        "Singapore: MAS live benchmark data "
        f"parsed for {len(rows)}/"
        f"{len(MAS_TRACKED_ISSUES)} "
        f"tracked issues ({latest_date})."
    )

    return {
        "source": "MAS",
        "status": "success",
        "date": latest_date,
        "rows": rows,
        "detectedIssueCodes": sorted(
            detected_codes
        ),
    }


# ============================================================================
# SINGAPORE INSTRUMENT UPDATE
# ============================================================================

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

    # ------------------------------------------------------------------
    # IMPORTANT:
    #
    # We no longer use the previous incorrect hard-coded ISIN mapping.
    #
    # The Phase 1 Singapore JSON already contains issue_code for the
    # tracked SGS instruments where available.
    #
    # If issue_code is missing, we use the bond name only as a safe
    # fallback.
    # ------------------------------------------------------------------

    name_to_issue = {
        "2.875% SGS 2028": "N523100W",
        "1.625% SGS 2031": "NX21100N",
        "2.250% SGS 2036": "NZ16100X",
        "2.250% SGS 2040": "NY25200N",
        "2.750% SGS 2046": "NA16100H",
        "3.000% SGS 2072": "NC22300W",
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

        bond = str(
            instrument.get(
                "bond"
            )
            or ""
        ).strip()

        # Safe fallback from bond name.
        if not issue_code:

            issue_code = (
                name_to_issue.get(
                    bond,
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

        # --------------------------------------------------------------
        # Yield
        # --------------------------------------------------------------

        live_yield = hit.get(
            "yield"
        )

        if live_yield is not None:

            instrument["previousYield"] = (
                instrument.get(
                    "yield"
                )
            )

            instrument["yield"] = (
                live_yield
            )

            instrument["liveYield"] = (
                live_yield
            )

            changed = True

        # --------------------------------------------------------------
        # Price
        # --------------------------------------------------------------

        live_price = hit.get(
            "price"
        )

        if live_price is not None:

            instrument["previousPrice"] = (
                instrument.get(
                    "price"
                )
            )

            instrument["price"] = (
                live_price
            )

            instrument["livePrice"] = (
                live_price
            )

            changed = True

        # --------------------------------------------------------------
        # Date
        # --------------------------------------------------------------

        if changed:

            instrument["liveDate"] = (
                hit.get(
                    "date"
                )
            )

            instrument["dataStatus"] = (
                "live MAS SGS data"
            )

            # Keep issue_code visible in live.json.
            if not instrument.get(
                "issue_code"
            ):

                instrument["issue_code"] = (
                    issue_code
                )

            matched += 1

    mas["matchedInstruments"] = (
        matched
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

        # India live quote extraction is deliberately not
        # fabricated. Preserve the Phase 1 RBI universe.

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

    # ------------------------------------------------------------------
    # 1. Load Phase 1 universe
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
            "schemaVersion": "3.9",
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
    # 2. USA
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
    # 3. Hong Kong
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
    # 4. Singapore
    # ------------------------------------------------------------------

    try:

        mas = parse_mas_benchmark_table(
            fetch(
                MAS_URL,
                "Singapore MAS",
                timeout=30,
                retries=2,
            ).decode(
                "utf-8",
                errors="replace",
            ),
            log,
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
    # 6. Final status
    # ------------------------------------------------------------------

    status = calculate_status(
        log,
        instruments,
    )

    updated_at = now_utc()

    payload = {
        "schemaVersion": "3.9",

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
    # Write outputs
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------------

    print(
        "=========================================="
    )

    print(
        " Bond Monitor Phase 3 Update v3.9"
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
        + (
            "OK"
            if isinstance(hk, list)
            and hk
            else "ERROR/PARTIAL"
        )
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
