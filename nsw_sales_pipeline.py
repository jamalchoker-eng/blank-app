"""
NSW Valuer General Bulk Property Sales -> Sydney suburb screener data pipeline
================================================================================

WHAT THIS DOES
--------------
1. Downloads the free weekly "Bulk Property Sales Information" zip files the
   NSW Valuer General publishes per Local Government Area (LGA) at:
       https://valuation.property.nsw.gov.au/embed/propertySalesInformation
   (Free, Creative Commons Attribution, address-level sales back to 1990.)
2. Parses the pipe-delimited .DAT files inside each zip (record type B = a
   single property sale).
3. Classifies each sale as a house or a unit (see `classify_dwelling_type()`),
   keeps the Greater Sydney LGAs, and drops everything else (vacant land,
   commercial, etc).
4. Computes, per suburb and per dwelling type: median, min and max sale
   price over the trailing 12 months, and the median from ~1yr and ~5yr ago
   (each its own trailing-12-month window), to get 1yr/5yr growth for
   houses and units separately. min/max are real extremes of the same
   purchase_price values, not a placeholder.
5. Writes data/sydney_suburbs.json in a stable, simple shape a downstream UI
   (e.g. a React screener) can consume directly.

NO BEDROOM COUNTS, NO DAYS-ON-MARKET
--------------------------------------
The NSW Valuer General bulk sales file does not include bedroom count (or
any other internal dwelling attribute like bathrooms), and it has no
listing date — only `contract_date` (when contract was exchanged) and
`settlement_date` (conveyancing settlement, ~42 days later by convention).
Neither gap tells you how long a property sat on the market before going
under contract. `median`/`min`/`max`/`g1`/`g5` here are per suburb and
dwelling type only. If you need a bedroom-level cut or days-on-market for
*real* sales, this dataset can't give you either — you'd have to join it
against a listings source (e.g. Domain, realestate.com.au, or a paid
CoreLogic/PropTrack feed) by address, which is a different pipeline.
`dashboard.html`'s bedroom breakdown and days-on-market figure are built
entirely from `scripts/generate_placeholder_suburbs.py`'s synthetic model
for exactly this reason — neither is something this script can produce
from real data.

BEFORE YOU RUN THIS
--------------------
The NSW Valuer General has changed this file format before, and this
environment has no outbound network access to nsw.gov.au domains (confirmed:
the egress proxy rejects the connection), so the *current* column layout and
download endpoint below could not be verified against a live file. The field
order in `parse_b_record()` is the documented legacy PSI format (record type
B, ~22 pipe-delimited fields). The Valuer General publishes a short "Property
Sales Data File Format" note alongside the downloads on that page — open one
of the actual files you download, look at the header/footer rows (record
types A and Z), and confirm the B-record column order matches before
trusting the output. If it's drifted, it's a one-file edit to
`parse_b_record()`, not a rewrite. Likewise, `fetch_lga_sales()`'s download
URL is a placeholder pattern — confirm the real per-LGA link on the portal
page before running this for real.

USAGE
-----
    uv run python nsw_sales_pipeline.py

(or: pip install requests && python nsw_sales_pipeline.py)

Requires normal internet access — run this locally, in Claude Code, or as a
scheduled job. It will NOT run inside a sandbox without outbound access to
nsw.gov.au domains.
"""

import io
import json
import statistics
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 1. Config: which LGAs to pull (Greater Sydney), and where files live.
# ---------------------------------------------------------------------------

# The portal lists a zip per LGA (named by LGA, e.g. "SYDNEY.zip",
# "WAVERLEY.zip"). Confirm exact filenames/links on the portal page and
# adjust this list — LGA boundaries occasionally get renamed.
SYDNEY_LGAS = [
    "SYDNEY", "WAVERLEY", "WOOLLAHRA", "RANDWICK", "BAYSIDE",
    "INNER WEST", "CANADA BAY", "STRATHFIELD", "BURWOOD",
    "NORTH SYDNEY", "MOSMAN", "WILLOUGHBY", "LANE COVE",
    "KU-RING-GAI", "HORNSBY", "RYDE", "HUNTERS HILL",
    "NORTHERN BEACHES", "PARRAMATTA", "THE HILLS SHIRE",
    "CUMBERLAND", "BLACKTOWN", "PENRITH", "FAIRFIELD",
    "LIVERPOOL", "CANTERBURY-BANKSTOWN", "GEORGES RIVER",
    "SUTHERLAND SHIRE", "CAMPBELLTOWN", "CAMDEN",
    "WOLLONDILLY", "HAWKESBURY", "BLUE MOUNTAINS",
]

# Base portal URL — confirm the current download endpoint on the page above;
# historically it has been served from a path like this per-LGA zip pattern.
PORTAL_BASE = "https://valuation.property.nsw.gov.au/embed/propertySalesInformation"

OUTPUT_PATH = Path("data/sydney_suburbs.json")
MIN_SAMPLE_SIZE = 5  # skip a suburb/period whose median would rest on too few sales


# ---------------------------------------------------------------------------
# 2. Parsing a single record-type-B line (one property sale).
# ---------------------------------------------------------------------------
# Documented legacy PSI field order (0-indexed AFTER the leading "B"):
FIELDS = [
    "district_code", "property_id", "sale_counter", "download_datetime",
    "property_name", "unit_number", "house_number", "street_name",
    "suburb", "postcode", "area", "area_type", "contract_date",
    "settlement_date", "purchase_price", "zoning", "nature_of_property",
    "primary_purpose", "strata_lot_number", "component_code",
    "sale_code", "interest_of_sale", "dealing_number",
]


def parse_b_record(line: str) -> dict | None:
    parts = line.strip().split("|")
    if not parts or parts[0] != "B":
        return None
    values = parts[1:]
    if len(values) < len(FIELDS):
        return None  # malformed / truncated row — skip rather than guess
    row = dict(zip(FIELDS, values))
    return row


def classify_dwelling_type(row: dict) -> str | None:
    """Returns "house", "unit", or None (skip: vacant land, commercial, etc).

    Primary purpose codes vary by release. A strata lot number present is
    the more reliable unit/townhouse signal; "RESIDENCE"/"RESIDENTIAL"
    without one is treated as a house."""
    purpose = (row.get("primary_purpose") or "").strip().upper()
    strata = (row.get("strata_lot_number") or "").strip()
    if purpose not in {"RESIDENCE", "RESIDENTIAL"}:
        return None
    return "unit" if strata else "house"


# ---------------------------------------------------------------------------
# 3. Download + extract one LGA's sales, return parsed sale rows.
# ---------------------------------------------------------------------------

def fetch_lga_sales(lga_name: str) -> list[dict]:
    """
    Downloads and parses the bulk sales zip for one LGA.
    NOTE: confirm the actual per-LGA download URL pattern on the portal —
    this assumes a predictable query/slug; adjust if the portal uses a
    different scheme (some NSW open-data portals require a POST/session).
    """
    slug = lga_name.replace(" ", "%20")
    url = f"{PORTAL_BASE}?la={slug}"  # placeholder pattern — verify on the site

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    rows = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in zf.namelist():
            if not name.upper().endswith(".DAT"):
                continue
            with zf.open(name) as f:
                for raw_line in io.TextIOWrapper(f, encoding="latin-1"):
                    row = parse_b_record(raw_line)
                    if not row:
                        continue
                    dwelling_type = classify_dwelling_type(row)
                    if dwelling_type:
                        row["dwelling_type"] = dwelling_type
                        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 4. Aggregate sales -> suburb-level median price + 1yr/5yr growth.
# ---------------------------------------------------------------------------

def parse_date(s: str):
    for fmt in ("%Y%m%d", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def _prices_in_window(sales: list[dict], start: datetime, end: datetime) -> list[float]:
    return [s["price"] for s in sales if start <= s["date"] < end]


def _median_in_window(sales: list[dict], start: datetime, end: datetime) -> tuple[float | None, int]:
    """Median purchase price for sales with start <= date < end, plus the sample size."""
    prices = _prices_in_window(sales, start, end)
    if len(prices) < MIN_SAMPLE_SIZE:
        return None, len(prices)
    return statistics.median(prices), len(prices)


def _dwelling_stats(sales: list[dict], window_now, window_1y_ago, window_5y_ago) -> dict | None:
    """median/g1/g5/sample_size/min/max over the trailing-12mo window.

    min/max are genuinely computable from this dataset (they're just the
    extremes of the same purchase_price values the median comes from), so
    unlike bedroom count or days-on-market, this is not a placeholder.

    NOTE ON "DAYS ON MARKET": don't be tempted to derive it from
    settlement_date - contract_date. That gap is conveyancing/settlement
    lag (typically a fixed ~42 days by convention, sometimes negotiated
    longer), not how long the property sat listed before going under
    contract. The VG bulk sales file has no listing date at all, so days-
    on-market isn't derivable from this dataset — same limitation as
    bedroom count (see the NO BEDROOM COUNTS note above). It would need a
    listings source (Domain, realestate.com.au, CoreLogic/PropTrack).
    """
    prices_now = _prices_in_window(sales, *window_now)
    if len(prices_now) < MIN_SAMPLE_SIZE:
        return None  # too few recent sales for a reliable current median

    median_now = statistics.median(prices_now)
    median_1y_ago, _ = _median_in_window(sales, *window_1y_ago)
    median_5y_ago, _ = _median_in_window(sales, *window_5y_ago)

    g1 = round((median_now / median_1y_ago - 1) * 100, 1) if median_1y_ago else None
    g5 = round((median_now / median_5y_ago - 1) * 100, 1) if median_5y_ago else None

    return {
        "median": round(median_now),
        "g1": g1,
        "g5": g5,
        "sample_size": len(prices_now),
        "min": round(min(prices_now)),
        "max": round(max(prices_now)),
    }


def aggregate_by_suburb(all_rows: list[dict]) -> list[dict]:
    now = datetime.now()

    # Three trailing-12-month windows: this year, the year before it (for
    # 1yr growth), and the year ending 5 years ago (for 5yr growth). Using a
    # full 12-month window at each anchor (rather than a narrow point-in-time
    # slice) keeps sample sizes reasonable even for smaller suburbs.
    window_now = (now - timedelta(days=365), now)
    window_1y_ago = (now - timedelta(days=365 * 2), now - timedelta(days=365))
    window_5y_ago = (now - timedelta(days=365 * 6), now - timedelta(days=365 * 5))

    # {suburb: {"house": [...], "unit": [...]}}
    by_suburb: dict[str, dict[str, list[dict]]] = {}
    for row in all_rows:
        suburb = (row.get("suburb") or "").strip().title()
        if not suburb:
            continue
        date = parse_date(row.get("contract_date", ""))
        try:
            price = float(row.get("purchase_price", "0") or 0)
        except ValueError:
            price = 0
        if not date or price < 50_000:  # drop unparseable/junk rows
            continue
        bucket = by_suburb.setdefault(suburb, {"house": [], "unit": []})
        bucket[row["dwelling_type"]].append({"date": date, "price": price})

    results = []
    for suburb, by_type in by_suburb.items():
        houses = _dwelling_stats(by_type["house"], window_now, window_1y_ago, window_5y_ago)
        units = _dwelling_stats(by_type["unit"], window_now, window_1y_ago, window_5y_ago)
        if houses is None and units is None:
            continue  # neither dwelling type had enough recent sales

        results.append({"name": suburb, "houses": houses, "units": units})

    def sort_key(r):
        return (r["houses"] or r["units"])["median"]

    return sorted(results, key=sort_key, reverse=True)


# ---------------------------------------------------------------------------
# 5. Run it.
# ---------------------------------------------------------------------------

def main():
    all_rows = []
    for lga in SYDNEY_LGAS:
        try:
            print(f"Fetching {lga}...")
            all_rows.extend(fetch_lga_sales(lga))
        except Exception as e:
            print(f"  skipped {lga}: {e}")

    suburbs = aggregate_by_suburb(all_rows)

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(suburbs, indent=2))
    print(f"Wrote {len(suburbs)} suburbs to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
