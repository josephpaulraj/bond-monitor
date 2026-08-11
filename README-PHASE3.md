# Bond Monitor — Phase 3

## What Phase 3 adds

Phase 3 introduces an automated GitHub Actions data-refresh pipeline.

**Flow**

Official source → GitHub Actions → `scripts/update_bonds.py` → `data/live.json` → dashboard

The workflow is scheduled for **01:17 Asia/Singapore, Monday–Friday**, and can also be run manually from GitHub Actions.

### Official sources

- **United States:** U.S. Treasury daily Treasury rate data.
- **Hong Kong:** HKMA Exchange Fund Bills & Notes indicative-price API.
- **Singapore:** MAS Daily SGS Prices.
- **India:** RBI source universe is retained. Instrument-level live quote ingestion is intentionally not fabricated; the updater records the source and leaves unavailable quotes blank until a stable machine-readable official endpoint is confirmed.

### Data integrity rule

The updater never creates a price or yield merely because a number is convenient. If an official source does not provide an instrument-level quote, the dashboard shows an unavailable value rather than a guessed value.

## Install

Upload these Phase 3 files/folders into the root of the existing `bond-monitor` repository:

```text
.github/workflows/update-bond-data.yml
scripts/update_bonds.py
data/live.json
data/last-update.json
```

Then commit.

## First run

Go to:

**GitHub → bond-monitor → Actions → Update bond market data → Run workflow**

The workflow will fetch the sources and commit changes to `data/`.

## Important GitHub Pages note

GitHub scheduled workflows run on the default branch. GitHub documents scheduled workflows using POSIX cron and supports timezone-aware schedules. Public repositories can have scheduled workflows automatically disabled after 60 days without repository activity, so check Actions occasionally.

## Phase 3 scope

This phase deliberately separates:

1. **Instrument universe** — what the bond is.
2. **Market observation** — today's/latest source quote or benchmark.
3. **Source metadata** — where the observation came from.
4. **Update timestamp** — when the updater last ran.

That separation is important before we build investment analytics in Phase 4.

## Current limitations

- U.S. Treasury publishes an official yield curve; it is not an individual secondary-market quote for every Treasury security.
- HKMA's EFBN API provides latest-business-day indicative pricing and yield.
- MAS's Daily SGS Prices page is official, but its HTML layout can change. The parser therefore fails safely instead of silently assigning the wrong column.
- India's RBI site provides official government-security information and market-trading information, but a stable public machine-readable instrument-level quote endpoint still needs to be confirmed before automatic ingestion is enabled.

## Sources

- U.S. Treasury: https://home.treasury.gov/resource-center/data-chart-center/interest-rates
- MAS: https://eservices.mas.gov.sg/statistics/fdanet/SgsBenchmarkIssuePrices.aspx
- HKMA API documentation: https://apidocs.hkma.gov.hk/documentation/market-data-and-statistics/daily-monetary-statistics/efbn-indicative-price
- RBI: https://data.rbi.org.in/
