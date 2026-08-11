#!/usr/bin/env python3
"""
Bond Monitor Phase 3 updater v3.1

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


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "live.json"
LAST_UPDATE = DATA / "last-update.json"


# ---------------------------------------------------------------------------
# HTTP configuration
# ---------------------------------------------------------------------------

UA = (
    "bond-monitor/3.1 "
    "(GitHub Actions; official public market-data updater)"
)

CTX = ssl.create_default_context()

HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
HTTP_RETRY_DELAY = 3


# ---------------------------------------------------------------------------
# Official source URLs
# ---------------------------------------------------------------------------

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
    "BondPricesAndYields.aspx"
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

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
                    "Accept": "*/*",
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

    raise RuntimeError(
        f"{last_error}"
    )


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


# ---------------------------------------------------------------------------
# Phase 1 instrument universe
# ---------------------------------------------------------------------------

COUNTRY_FILES = {
    "United States": DATA / "usa.json",
    "Singapore": DATA / "singapore.json",
    "Hong Kong": DATA / "hongkong.json",
    "India": DATA / "india.json",
}


def load_instrument_universe(log: list[str]) -> list[dict[str, Any]]:

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
                f"{market}: ERROR {path.name} is not a JSON object"
            )

            continue

        records = obj.get("records", [])

        if not isinstance(records, list):

            log.append(
                f"{market}: ERROR records[] missing in {path.name}"
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
            f"{market}: loaded {count} instruments from {path.name}"
        )

    log.append(
        f"Loaded {len(instruments)} instruments from Phase 1 country files."
    )

    return instruments


# ---------------------------------------------------------------------------
# Market normalization
# ---------------------------------------------------------------------------

def norm_market(value: Any) -> str:

    return str(value or "").strip().lower()


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
    )

    try:
        return float(text)

    except ValueError:
        return None


# ---------------------------------------------------------------------------
# USA Treasury
# ---------------------------------------------------------------------------

def update_us_curve(log: list[str]) -> dict[str, Any]:

    # Treasury requires the year parameter for the XML feed.
    year = datetime.now(timezone.utc).year

    url = (
        TREASURY_URL
        + f"?data=daily_treasury_yield_curve"
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

    # Treasury XML normally contains:
    # <d:NEW_DATE ...>2026-08-10T00:00:00</d:NEW_DATE>
    date_patterns = [
        r"<d:NEW_DATE[^>]*>(.*?)</d:NEW_DATE>",
        r"<NEW_DATE[^>]*>(.*?)</NEW_DATE>",
    ]

    dates = []

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

    # Locate the latest Treasury entry.
    pos = raw.rfind(latest)

    if pos < 0:
        raise RuntimeError(
            "Treasury latest-date record could not be located"
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

    if entry_start >= 0 and entry_end >= 0:

        block = raw[
            entry_start:
            entry_end + len("</entry>")
        ]

    else:

        block = raw

    # IMPORTANT:
    # Treasury field names are not simply "1_month", etc.
    # They use names such as:
    #
    # BC_1MONTH
    # BC_1_5MONTH
    # BC_2MONTH
    # BC_3MONTH
    # BC_4MONTH
    # BC_6MONTH
    # BC_1YEAR
    # BC_2YEAR
    # BC_3YEAR
    # BC_5YEAR
    # BC_7YEAR
    # BC_10YEAR
    # BC_20YEAR
    # BC_30YEAR

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

    curve = {}

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

    curve = us.get("curve", {})
    date = us.get("date")

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

        tenor = maturity_map.get(bond)

        if not tenor:
            continue

        value = curve.get(tenor)

        if value is None:
            continue

        # Preserve the previous value before replacing it.
        instrument["previousYield"] = instrument.get(
            "yield"
        )

        instrument["yield"] = value
        instrument["liveYield"] = value
        instrument["liveDate"] = date

        instrument["dataStatus"] = (
            "live U.S. Treasury benchmark yield"
        )
# ---------------------------------------------------------------------------
# Hong Kong HKMA
# ---------------------------------------------------------------------------

def update_hkma(log: list[str]) -> list[dict[str, Any]]:

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

    result = obj.get("result") or {}

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
                "date": row.get("end_of_date"),
                "term": row.get("term"),
                "issue_no": row.get("issue_no"),
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

        # Exchange Fund instruments don't have ISINs.
        # Match them using their descriptive bond name/term.
        candidates = hk_rows

        hit = None

        if isin and isin != "—":

            hit = next(
                (
                    row
                    for row in candidates
                    if str(
                        row.get("issue_no") or ""
                    ) == isin
                ),
                None,
            )

        if hit is None:

            bond_lower = bond.lower()

            if "exchange fund note" in bond_lower:

                hit = next(
                    (
                        row
                        for row in candidates
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
                        for row in candidates
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

            instrument["previousYield"] = instrument.get(
                "yield"
            )

            instrument["yield"] = hit["yield"]
            instrument["liveYield"] = hit["yield"]

            changed = True

        if hit.get("price") is not None:

            instrument["previousPrice"] = instrument.get(
                "price"
            )

            instrument["price"] = hit["price"]
            instrument["livePrice"] = hit["price"]

            changed = True

        if changed:

            instrument["liveDate"] = hit.get(
                "date"
            )

            instrument["dataStatus"] = (
                "live HKMA indicative data"
            )


# ---------------------------------------------------------------------------
# Singapore MAS
# ---------------------------------------------------------------------------

class TextTableParser(HTMLParser):

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

    def handle_endtag(
        self,
        tag: str,
    ) -> None:

        tag = tag.lower()

        if tag in ("td", "th"):

            if self.in_cell:

                text = " ".join(
                    "".join(
                        self.buf
                    ).split()
                )

                self.row.append(text)

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

            self.buf.append(data)

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

    # ---------------------------------------------------------------
    # MAS publishes SGS benchmark data as a matrix.
    #
    # The page contains:
    #
    # Issue Code
    # Coupon Rate
    # Maturity Date
    #
    # followed by daily rows where each benchmark has either:
    #
    # Yield
    #
    # or:
    #
    # Price | Yield
    #
    # We only need the latest daily row.
    # ---------------------------------------------------------------

    parser = TextTableParser()
    parser.feed(html)

    # The HTML parser may produce multiple tables.
    # Find the table containing our known SGS issue codes.

    target_codes = {
        "N523100W",
        "NX21100N",
        "NZ16100X",
        "NY25200N",
        "NA16100H",
        "NC22300W",
    }

    target_rows = []

    for row in parser.rows:

        normalized = {
            str(cell).strip().upper()
            for cell in row
        }

        if normalized.intersection(target_codes):

            target_rows.append(row)

    # ---------------------------------------------------------------
    # If the generic parser does not expose the table rows cleanly,
    # use the raw HTML text as a fallback.
    # ---------------------------------------------------------------

    if not target_rows:

        text = re.sub(
            r"<[^>]+>",
            " ",
            html,
        )

        text = re.sub(
            r"\s+",
            " ",
            text,
        )

        # We don't fabricate values here.
        # The explicit failure message tells us the MAS markup
        # changed if this fallback is reached.

        log.append(
            "Singapore: MAS page fetched, "
            "but benchmark issue-code rows could not be parsed."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_table",
            "rows": [],
        }

    # ---------------------------------------------------------------
    # Build a simple issue-code index from the HTML.
    #
    # The current MAS benchmark page places the issue code and
    # associated maturity information together in the table.
    # ---------------------------------------------------------------

    issue_index = {}

    for row in target_rows:

        cells = [
            str(cell).strip()
            for cell in row
        ]

        for i, cell in enumerate(cells):

            code = cell.upper()

            if code not in target_codes:
                continue

            issue_index[code] = {
                "row": cells,
                "index": i,
            }

    # ---------------------------------------------------------------
    # Latest MAS closing data
    #
    # MAS currently exposes the latest daily benchmark row as:
    #
    # date | ... | Yield | Price | Yield | Price ...
    #
    # The benchmark columns occur in this order:
    #
    # 2Y  -> Price / Yield
    # 5Y  -> Price / Yield
    # 10Y -> Price / Yield
    # 15Y -> Price / Yield
    # 20Y -> Price / Yield
    # 30Y -> Price / Yield
    # 50Y -> Price / Yield
    #
    # We therefore map our issue codes to their benchmark column.
    # ---------------------------------------------------------------

    benchmark_map = {
        "N523100W": "2Y",
        "NX21100N": "5Y",
        "NZ16100X": "10Y",
        "NY25200N": "15Y",
        "NA16100H": "30Y",
        "NC22300W": "50Y",
    }

    # ---------------------------------------------------------------
    # Extract the latest daily values directly from the visible
    # MAS page text.
    # ---------------------------------------------------------------

    visible_text = re.sub(
        r"<script.*?</script>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    visible_text = re.sub(
        r"<style.*?</style>",
        " ",
        visible_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    visible_text = re.sub(
        r"<[^>]+>",
        " ",
        visible_text,
    )

    visible_text = re.sub(
        r"\s+",
        " ",
        visible_text,
    ).strip()

    # ---------------------------------------------------------------
    # Locate the latest date appearing in the Closing Levels section.
    #
    # MAS uses DD Mon YYYY, for example:
    #
    # 11 Aug 2026
    # ---------------------------------------------------------------

    date_matches = re.findall(
        r"\b\d{2}\s+[A-Za-z]{3}\s+\d{4}\b",
        visible_text,
    )

    if not date_matches:

        log.append(
            "Singapore: MAS page fetched, "
            "but no closing-level date was detected."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_date",
            "rows": [],
        }

    latest_date = date_matches[-1]

    # ---------------------------------------------------------------
    # The current MAS page exposes the latest benchmark row as a
    # sequence of values.
    #
    # For 11 Aug 2026 the page currently shows:
    #
    # 2Y:
    #   1.71 / 98.22
    #
    # 5Y:
    #   2.01 / 99.41
    #
    # 10Y:
    #   2.32 / 98.61
    #
    # 15Y:
    #   2.37 / 99.88
    #
    # 20Y:
    #   2.38 / 115.91
    #
    # 30Y:
    #   2.46 / 110.92
    #
    # 50Y:
    #   2.59
    #
    # However, because MAS can change the page layout, we don't
    # hard-code those values. We parse the latest row.
    # ---------------------------------------------------------------

    rows = []

    # Find the portion of the page after "Closing Levels".
    closing_pos = visible_text.lower().find(
        "closing levels"
    )

    if closing_pos < 0:

        log.append(
            "Singapore: MAS Closing Levels section not found."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_closing_section",
            "rows": [],
        }

    closing_text = visible_text[
        closing_pos:
    ]

    # Look for the latest date in the closing section.
    latest_match = re.search(
        r"\b(\d{2}\s+[A-Za-z]{3}\s+\d{4})\b",
        closing_text,
    )

    if not latest_match:

        log.append(
            "Singapore: MAS latest closing date not found."
        )

        return {
            "source": "MAS",
            "status": "fetched_no_date",
            "rows": [],
        }

    latest_date = latest_match.group(1)

    # ---------------------------------------------------------------
    # Extract the latest numeric sequence following the latest date.
    #
    # MAS's current page exposes the latest row in plain text.
    # ---------------------------------------------------------------

    after_date = closing_text[
        latest_match.end():
    ]

    numbers = re.findall(
        r"\b\d+(?:\.\d+)?\b",
        after_date,
    )

    numeric_values = []

    for value in numbers:

        try:

            numeric_values.append(
                float(value)
            )

        except ValueError:
            continue

    # We need the benchmark values only.
    #
    # The current benchmark sequence is:
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
    # Therefore:
    #
    # N523100W -> positions 2,3
    # NX21100N -> positions 4,5
    # NZ16100X -> positions 6,7
    # NY25200N -> positions 8,9
    # NA16100H -> positions 12,13
    # NC22300W -> position 14
    #
    # NOTE:
    # We validate the values before accepting them.
    # ---------------------------------------------------------------

    if len(numeric_values) < 15:

        log.append(
            "Singapore: MAS latest row did not contain "
            "the expected benchmark values."
        )

        return {
            "source": "MAS",
            "status": "fetched_incomplete",
            "rows": [],
        }

    latest_values = numeric_values[:15]

    # Map benchmark positions.
    benchmark_values = {
        "N523100W": {
            "yield": latest_values[2],
            "price": latest_values[3],
        },
        "NX21100N": {
            "yield": latest_values[4],
            "price": latest_values[5],
        },
        "NZ16100X": {
            "yield": latest_values[6],
            "price": latest_values[7],
        },
        "NY25200N": {
            "yield": latest_values[8],
            "price": latest_values[9],
        },
        "NA16100H": {
            "yield": latest_values[12],
            "price": latest_values[13],
        },
        "NC22300W": {
            "yield": latest_values[14],
            "price": None,
        },
    }

    # ---------------------------------------------------------------
    # Sanity-check values.
    #
    # SGS yields should normally be between 0 and 20%.
    # Prices should normally be between 50 and 150.
    #
    # If a page-layout change causes nonsense values, reject them.
    # ---------------------------------------------------------------

    for issue_code, values in benchmark_values.items():

        y = values.get("yield")
        p = values.get("price")

        if (
            y is not None
            and not 0 <= y <= 20
        ):

            values["yield"] = None

        if (
            p is not None
            and not 50 <= p <= 150
        ):

            values["price"] = None

        if (
            values.get("yield") is not None
            or values.get("price") is not None
        ):

            rows.append(
                {
                    "issue_code": issue_code,
                    "yield": values.get("yield"),
                    "price": values.get("price"),
                    "date": latest_date,
                    "source": "MAS",
                }
            )

    log.append(
        f"Singapore: MAS closing levels parsed for "
        f"{len(rows)}/6 tracked benchmark issues "
        f"({latest_date})."
    )

    return {
        "source": "MAS",
        "status": (
            "success"
            if rows
            else "fetched_incomplete"
        ),
        "date": latest_date,
        "rows": rows,
    }

def update_singapore_instruments(
    instruments: list[dict[str, Any]],
    mas: dict[str, Any],
) -> None:

    rows = mas.get("rows", [])

    # MAS issue-code <-> ISIN mapping for the instruments
    # in our Phase 1 Singapore universe.
    #
    # These mappings are confirmed against the MAS SGS page.

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

        # If the JSON already contains issue_code, use it.
        #
        # Otherwise derive it from ISIN.
        if not issue_code and isin:

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

        # -----------------------------------------------------------
        # Yield
        # -----------------------------------------------------------

        if hit.get("yield") is not None:

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

        # -----------------------------------------------------------
        # Price
        # -----------------------------------------------------------

        if hit.get("price") is not None:

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

        # -----------------------------------------------------------
        # Date
        # -----------------------------------------------------------

        if changed:

            instrument["liveDate"] = (
                hit.get("date")
            )

            instrument["dataStatus"] = (
                "live MAS SGS data"
            )

            # Keep the issue code available in live.json
            # even for records that originally only had ISIN.

            if not instrument.get(
                "issue_code"
            ):

                instrument["issue_code"] = (
                    issue_code
                )

            updated_count += 1

    # Store a summary for the dashboard/log.
    mas["matchedInstruments"] = (
        updated_count
    )

# ---------------------------------------------------------------------------
# India
# ---------------------------------------------------------------------------

def update_india_instruments(
    instruments: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

        if norm_market(
            instrument.get("market")
        ) != "india":
            continue

        # India instrument-level live values are deliberately
        # not fabricated. Preserve the Phase 1 source-backed
        # instrument information.

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


# ---------------------------------------------------------------------------
# Final normalization
# ---------------------------------------------------------------------------

def initialize_live_fields(
    instruments: list[dict[str, Any]],
) -> None:

    for instrument in instruments:

        # Preserve the original Phase 1 values.

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:

    DATA.mkdir(
        exist_ok=True
    )

    log: list[str] = []

    # ---------------------------------------------------------------
    # 1. Load the original Phase 1 universe.
    # ---------------------------------------------------------------

    instruments = load_instrument_universe(
        log
    )

    if not instruments:

        log.append(
            "ERROR: No instruments were loaded. "
            "Existing country files were not modified."
        )

        payload = {
            "schemaVersion": "3.1",
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
                "updatedAt": payload["updatedAt"],
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

    # ---------------------------------------------------------------
    # 2. USA Treasury
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # 3. Hong Kong HKMA
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # 4. Singapore MAS
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # 5. India
    # ---------------------------------------------------------------

    try:

        update_india_instruments(
            instruments
        )

        log.append(
            "India: Phase 1 RBI instrument universe preserved; "
            "no unverified live quote was inferred."
        )

    except Exception as exc:

        log.append(
            f"India: ERROR {exc}"
        )

    # ---------------------------------------------------------------
    # 6. Final status
    # ---------------------------------------------------------------

    status = calculate_status(
        log,
        instruments,
    )

    updated_at = now_utc()

    payload = {
        "schemaVersion": "3.1",
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
                "statistics/fdanet/"
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

    # ---------------------------------------------------------------
    # IMPORTANT:
    # Only write live.json after instruments have been confirmed.
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # Console summary
    # ---------------------------------------------------------------

    print(
        "=========================================="
    )

    print(
        " Bond Monitor Phase 3 Update v3.1"
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
