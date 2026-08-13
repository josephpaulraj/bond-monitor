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

v3.5 changes
-------------
1. More defensive HTTP handling.
2. Shorter retry behavior to avoid long GitHub Actions runs.
3. HKMA failure does not destroy the Phase 1 universe.
4. MAS parsing no longer depends solely on the literal
   "Closing Levels" heading.
5. MAS issue-code detection is independent of closing-section detection.
6. MAS parser supports HTML table parsing and visible-text fallback.
7. Existing yield/price values are preserved when live data cannot
   be safely extracted.
8. Final console status is explicit and consistent.
9. No fragile nested f-string expressions.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import unescape
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

# Keep the updater fast.
#
# A failed official endpoint should not make the GitHub Action hang for
# several minutes.
HTTP_TIMEOUT = 20
HTTP_RETRIES = 2
HTTP_RETRY_DELAY = 1


# ============================================================================
# Official source URLs
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

# IMPORTANT:
# Keep this as the MAS page URL.
#
# If MAS changes the page in the future, update ONLY this constant.
MAS_URL = (
    "https://eservices.mas.gov.sg/"
    "Statistics/fdanet/"
    "BondPricesAndYields.aspx"
)


# ============================================================================
# Singapore tracked SGS issue codes
# ============================================================================

SGS_ISSUE_CODES = {
    "N523100W",
    "NX21100N",
    "NZ16100X",
    "NY25200N",
    "NA16100H",
    "NC22300W",
}


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
    log: list[str] | None = None,
    label: str = "HTTP",
) -> bytes:
    """
    Fetch a URL with a small number of retries.

    The function deliberately keeps retry delays short so a bad external
    endpoint does not make the GitHub Action run indefinitely.
    """

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):

        if log is not None:
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

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:

            last_error = exc

            if log is not None:
                log.append(
                    f"{label}: connection/HTTP error {exc}"
                )

            if attempt < retries:
                time.sleep(HTTP_RETRY_DELAY)

        except Exception as exc:

            last_error = exc

            if log is not None:
                log.append(
                    f"{label}: unexpected error {exc}"
                )

            if attempt < retries:
                time.sleep(HTTP_RETRY_DELAY)

    if last_error is None:
        raise RuntimeError("Unknown HTTP error")

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
        .replace("−", "-")
        .replace("—", "")
        .strip()
    )

    if not text:
        return None

    try:
        return float(text)

    except ValueError:
        return None


def normalize_text(
    value: str,
) -> str:

    value = unescape(
        value or ""
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


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
            f"{market}: loaded {count} "
            f"instruments from {path.name}"
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
        timeout=HTTP_TIMEOUT,
        retries=HTTP_RETRIES,
        log=log,
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

        previous = instrument.get(
            "yield"
        )

        if previous is not None:
            instrument["previousYield"] = previous

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
        timeout=HTTP_TIMEOUT,
        retries=HTTP_RETRIES,
        log=log,
        label="Hong Kong HKMA",
    )

    try:

        obj = json.loads(
            raw.decode(
                "utf-8",
                errors="replace",
            )
        )

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            f"HKMA returned invalid JSON: {exc}"
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

            previous = instrument.get(
                "yield"
            )

            if previous is not None:
                instrument["previousYield"] = previous

            instrument["yield"] = hit[
                "yield"
            ]

            instrument["liveYield"] = hit[
                "yield"
            ]

            changed = True

        if hit.get(
            "price"
        ) is not None:

            previous = instrument.get(
                "price"
            )

            if previous is not None:
                instrument["previousPrice"] = previous

            instrument["price"] = hit[
                "price"
            ]

            instrument["livePrice"] = hit[
                "price"
            ]

            changed = True

        if changed:

            instrument["liveDate"] = hit.get(
                "date"
            )

            instrument["dataStatus"] = (
                "live HKMA indicative data"
            )


# ============================================================================
# MAS HTML parser
# ============================================================================

class TextTableParser(
    HTMLParser
):

    def __init__(
        self,
    ) -> None:

        super().__init__(
            convert_charrefs=True
        )

        self.in_cell = False
        self.in_row = False

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

            self.in_row = True
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

                text = normalize_text(
                    "".join(
                        self.buf
                    )
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

            self.row = []
            self.in_row = False

    def handle_data(
        self,
        data: str,
    ) -> None:

        if self.in_cell:

            self.buf.append(
                data
            )


# ============================================================================
# MAS helpers
# ============================================================================

def mas_visible_text(
    html: str,
) -> str:

    text = re.sub(
        r"<script.*?</script>",
        " ",
        html,
        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )

    text = re.sub(
        r"<style.*?</style>",
        " ",
        text,
        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )

    text = re.sub(
        r"<noscript.*?</noscript>",
        " ",
        text,
        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    text = unescape(
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def find_mas_issue_codes(
    html: str,
    parser: TextTableParser,
) -> set[str]:

    found: set[str] = set()

    upper_html = html.upper()

    for code in SGS_ISSUE_CODES:

        if code in upper_html:
            found.add(
                code
            )

    for row in parser.rows:

        for cell in row:

            cell_upper = str(
                cell
            ).upper().strip()

            for code in SGS_ISSUE_CODES:

                if code == cell_upper:
                    found.add(
                        code
                    )

    return found


def parse_decimal_tokens(
    text: str,
) -> list[float]:

    values: list[float] = []

    # Supports:
    #   2.31
    #   2.31%
    #   99.42
    #   -0.12
    #   1,234.56
    #
    # Dates are filtered later.

    matches = re.findall(
        r"(?<![\w/.-])-?\d+(?:,\d{3})*(?:\.\d+)?%?",
        text,
    )

    for item in matches:

        cleaned = (
            item
            .replace(",", "")
            .replace("%", "")
        )

        try:

            values.append(
                float(cleaned)
            )

        except ValueError:
            continue

    return values


def looks_like_yield(
    value: float | None,
) -> bool:

    if value is None:
        return False

    return 0 <= value <= 20


def looks_like_price(
    value: float | None,
) -> bool:

    if value is None:
        return False

    return 50 <= value <= 150


# ============================================================================
# MAS parser
# ============================================================================

def update_mas(
    log: list[str],
) -> dict[str, Any]:

    html = fetch(
        MAS_URL,
        timeout=HTTP_TIMEOUT,
        retries=HTTP_RETRIES,
        log=log,
        label="Singapore MAS",
    ).decode(
        "utf-8",
        errors="replace",
    )

    parser = TextTableParser()

    try:

        parser.feed(
            html
        )

        parser.close()

    except Exception as exc:

        log.append(
            "Singapore: MAS HTML parser "
            f"warning: {exc}"
        )

    found_codes = find_mas_issue_codes(
        html,
        parser,
    )

    log.append(
        "Singapore: MAS page fetched; "
        f"found {len(found_codes)}/"
        f"{len(SGS_ISSUE_CODES)} "
        "tracked issue codes."
    )

    visible = mas_visible_text(
        html
    )

    # ------------------------------------------------------------------------
    # First attempt:
    # Find rows in the HTML table that contain an issue code.
    # ------------------------------------------------------------------------

    issue_rows: dict[str, list[str]] = {}

    for row in parser.rows:

        row_text = " ".join(
            row
        )

        row_upper = row_text.upper()

        for code in SGS_ISSUE_CODES:

            if code in row_upper:

                issue_rows[
                    code
                ] = row

    # ------------------------------------------------------------------------
    # Extract a date.
    #
    # We accept:
    #   12 Aug 2026
    #   11 Aug 2026
    #   2026-08-12
    #   12/08/2026
    # ------------------------------------------------------------------------

    date_patterns = [
        (
            r"\b"
            r"\d{1,2}\s+"
            r"[A-Za-z]{3,9}\s+"
            r"\d{4}"
            r"\b"
        ),
        (
            r"\b"
            r"\d{4}-\d{2}-\d{2}"
            r"\b"
        ),
        (
            r"\b"
            r"\d{1,2}/"
            r"\d{1,2}/"
            r"\d{4}"
            r"\b"
        ),
    ]

    dates: list[str] = []

    for pattern in date_patterns:

        matches = re.findall(
            pattern,
            visible,
        )

        if matches:
            dates.extend(
                matches
            )

    latest_date = (
        dates[-1]
        if dates
        else None
    )

    # ------------------------------------------------------------------------
    # Candidate benchmark order.
    #
    # We deliberately do NOT assume that "Closing Levels" must exist.
    #
    # MAS has changed the presentation of this page over time. We therefore
    # inspect the page for numeric values and only accept values that pass
    # strict sanity checks.
    # ------------------------------------------------------------------------

    benchmark_order = [
        "N523100W",  # 2Y
        "NX21100N",  # 5Y
        "NZ16100X",  # 10Y
        "NY25200N",  # 15Y
        "NA16100H",  # 30Y
        "NC22300W",  # 50Y
    ]

    rows: list[dict[str, Any]] = []

    # ------------------------------------------------------------------------
    # Strategy 1:
    # If a row contains an issue code and enough numeric values, use the
    # numeric values from that row.
    #
    # This is intentionally conservative.
    # ------------------------------------------------------------------------

    for code, row in issue_rows.items():

        numeric = []

        for cell in row:

            numeric.extend(
                parse_decimal_tokens(
                    cell
                )
            )

        # Remove obvious coupon-like / year-like values and dates.
        numeric = [
            value
            for value in numeric
            if abs(value) < 10000
        ]

        yield_value = None
        price_value = None

        # Look for a sensible yield and price pair.
        #
        # Prefer the first sensible yield followed by a sensible price.
        for index, value in enumerate(
            numeric
        ):

            if not looks_like_yield(
                value
            ):
                continue

            if (
                index + 1
                < len(numeric)
            ):

                next_value = numeric[
                    index + 1
                ]

                if looks_like_price(
                    next_value
                ):

                    yield_value = value
                    price_value = next_value
                    break

        if yield_value is None:

            for value in numeric:

                if looks_like_yield(
                    value
                ):

                    yield_value = value
                    break

        if (
            yield_value is not None
            or price_value is not None
        ):

            rows.append(
                {
                    "issue_code": code,
                    "yield": yield_value,
                    "price": price_value,
                    "date": latest_date,
                    "source": "MAS",
                }
            )

    # ------------------------------------------------------------------------
    # Strategy 2:
    # Some MAS page versions place the issue-code universe separately from
    # the daily benchmark values.
    #
    # In that situation, use the latest numeric benchmark sequence only if
    # it contains enough values and passes sanity checks.
    # ------------------------------------------------------------------------

    if not rows:

        # Try to locate a likely benchmark section.
        section_markers = [
            "Closing Levels",
            "Closing level",
            "Benchmark",
            "SGS",
            "Government Securities",
        ]

        section_text = ""

        visible_lower = visible.lower()

        for marker in section_markers:

            position = visible_lower.find(
                marker.lower()
            )

            if position >= 0:

                section_text = visible[
                    position:
                ]

                break

        if not section_text:

            section_text = visible

        numeric_values = (
            parse_decimal_tokens(
                section_text
            )
        )

        # Filter out obvious years and dates.
        filtered_values: list[float] = []

        for value in numeric_values:

            if (
                1900
                <= value
                <= 2200
            ):
                continue

            filtered_values.append(
                value
            )

        # We need at least 15 values for the historical MAS benchmark
        # sequence used by this updater:
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
        # We only accept this strategy if the values make sense.

        if len(
            filtered_values
        ) >= 15:

            candidate = (
                filtered_values[:15]
            )

            benchmark_values = {
                "N523100W": {
                    "yield": candidate[2],
                    "price": candidate[3],
                },
                "NX21100N": {
                    "yield": candidate[4],
                    "price": candidate[5],
                },
                "NZ16100X": {
                    "yield": candidate[6],
                    "price": candidate[7],
                },
                "NY25200N": {
                    "yield": candidate[8],
                    "price": candidate[9],
                },
                "NA16100H": {
                    "yield": candidate[12],
                    "price": candidate[13],
                },
                "NC22300W": {
                    "yield": candidate[14],
                    "price": None,
                },
            }

            for code, values in (
                benchmark_values.items()
            ):

                y = values.get(
                    "yield"
                )

                p = values.get(
                    "price"
                )

                if not looks_like_yield(
                    y
                ):

                    y = None

                if not looks_like_price(
                    p
                ):

                    p = None

                if (
                    y is not None
                    or p is not None
                ):

                    rows.append(
                        {
                            "issue_code": code,
                            "yield": y,
                            "price": p,
                            "date": latest_date,
                            "source": "MAS",
                        }
                    )

    # ------------------------------------------------------------------------
    # Remove duplicate issue codes.
    # ------------------------------------------------------------------------

    unique_rows: dict[
        str,
        dict[str, Any]
    ] = {}

    for row in rows:

        code = str(
            row.get(
                "issue_code"
            )
            or ""
        ).upper()

        if not code:
            continue

        existing = unique_rows.get(
            code
        )

        if existing is None:

            unique_rows[
                code
            ] = row

            continue

        # Prefer a row that contains more actual values.
        existing_count = sum(
            1
            for key in (
                "yield",
                "price",
            )
            if existing.get(key) is not None
        )

        current_count = sum(
            1
            for key in (
                "yield",
                "price",
            )
            if row.get(key) is not None
        )

        if current_count > existing_count:

            unique_rows[
                code
            ] = row

    rows = list(
        unique_rows.values()
    )

    # ------------------------------------------------------------------------
    # Final MAS status.
    # ------------------------------------------------------------------------

    if rows:

        log.append(
            "Singapore: MAS live benchmark data "
            f"parsed for {len(rows)}/"
            f"{len(SGS_ISSUE_CODES)} tracked issues."
        )

        return {
            "source": "MAS",
            "status": "success",
            "date": latest_date,
            "foundIssueCodes": sorted(
                found_codes
            ),
            "rows": rows,
        }

    # We deliberately distinguish:
    #
    # 1. No issue codes
    # 2. Issue codes found but values not parsed
    # 3. No recognizable closing section
    #
    # This makes future troubleshooting much easier.

    if found_codes:

        log.append(
            "Singapore: MAS issue codes detected, "
            "but no safe live yield/price values "
            "could be parsed."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_values",
            "date": latest_date,
            "foundIssueCodes": sorted(
                found_codes
            ),
            "rows": [],
        }

    log.append(
        "Singapore: MAS page fetched, "
        "but no tracked SGS issue codes were detected."
    )

    return {
        "source": "MAS",
        "status": "fetched_no_table",
        "date": latest_date,
        "foundIssueCodes": [],
        "rows": [],
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

    if not isinstance(
        rows,
        list,
    ):
        return

    # Phase 1 ISIN -> MAS issue-code mapping.
    #
    # Keep both the existing issue_code values and ISIN fallback because the
    # Phase 1 JSON files may not all contain the issue_code field.
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

            previous = instrument.get(
                "yield"
            )

            if previous is not None:
                instrument["previousYield"] = previous

            instrument["yield"] = hit[
                "yield"
            ]

            instrument["liveYield"] = hit[
                "yield"
            ]

            changed = True

        if hit.get(
            "price"
        ) is not None:

            previous = instrument.get(
                "price"
            )

            if previous is not None:
                instrument["previousPrice"] = previous

            instrument["price"] = hit[
                "price"
            ]

            instrument["livePrice"] = hit[
                "price"
            ]

            changed = True

        if changed:

            instrument["liveDate"] = hit.get(
                "date"
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
        #
        # Existing Phase 1 yield/price values are preserved.

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
# Status calculation
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
# Main
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
            "schemaVersion": "3.5",
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
                "updatedAt": payload[
                    "updatedAt"
                ],
                "status": "error",
                "instrumentCount": 0,
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

    hk: list[dict[str, Any]] = []

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
        f"Instruments    : {len(instruments)}"
    )

    print(
        "USA curve      : "
        f"{str(us.get('status', 'unknown')).upper()}"
    )

    if hk:
        hk_status = "OK"
    else:
        hk_status = "ERROR/PARTIAL"

    print(
        "Hong Kong      : "
        f"{hk_status}"
    )

    print(
        "Singapore      : "
        f"{str(mas.get('status', 'unknown')).upper()}"
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
