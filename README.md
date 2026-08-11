# Bond Monitor — Phase 2

Replaces V1 demo data with a source-aware multi-market data model for India, Singapore, Hong Kong and the United States.

### Data rule
If an official public source does not provide a current instrument-level quote, the value is stored as `null` and the UI displays `—`. This avoids presenting stale or invented prices/yields as live.

### Fields
Market, issuer, bond, ISIN/CUSIP/issue code, type, currency, coupon, yield, price, maturity, rating, source, source URL and verification date.

### Next
Phase 3: automated daily ingestion, normalized prices/yields, FX conversion, historical snapshots and GitHub Actions updates.
