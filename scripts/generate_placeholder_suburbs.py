"""
Generate a placeholder suburb dataset for the Sydney dashboard.
=================================================================

Real geography, synthetic prices. This script downloads a public Australia
Post / ABS postcode dataset, keeps every NSW locality within a given radius
of Sydney CBD, and models a plausible-looking (but NOT real) median/min/max
price, 1yr/5yr growth, and days-on-market for each one — separately for
houses and units, and further split by bedroom count for price/growth — as
a function of distance from the CBD, plus deterministic per-suburb noise.

It exists to give `dashboard.html` full, realistic-looking coverage while
`nsw_sales_pipeline.py` can't be run against the actual NSW Valuer General
(no outbound access to nsw.gov.au from this environment). Suburb names,
their ABS SA3 "region", and their distance from the CBD are real. Every
price, growth, sales-volume, "recent sales" and development-score figure
is NOT — they're synthetic placeholders for laying out the dashboard.

`nsw_sales_pipeline.py`'s real output can replace the house/unit median,
min, max and growth figures here (that script computes real min/max from
the actual sale prices in its window) — but NOT the bedroom breakdown or
days-on-market. Bedroom count isn't a field in the VG dataset at all, and
neither is a listing date (see the "NO BEDROOM COUNTS, NO DAYS-ON-MARKET"
note in that script). Those two stay a synthetic modeling choice specific
to this placeholder generator, however good `nsw_sales_pipeline.py` gets.

DAYS ON MARKET
----------------
`dom_days` (per suburb, per dwelling type) is entirely synthetic — there is
no real-world data source wired into either script for it. It's modeled as
a base that rises gently with distance from the CBD, pulled down by strong
5yr growth (more buyer demand assumed to mean faster sales), and pushed up
~25% for the WATERFRONT_SUBURBS list (a thinner buyer pool for niche/
expensive listings), clamped to 14-95 days. Treat it as illustrative only.

MIN / MAX PRICE
-----------------
Per bedroom bucket (including "overall"), min/max are modeled as a spread
around that bucket's own synthetic median (min ~0.55-0.70x, max ~1.35-1.80x)
representing condition/land-size/aspect variation within the bucket. Same
placeholder status as the median itself.

BEDROOM PRICE MODEL
--------------------
For each suburb, a distance-based "house 3-bed" price is the anchor (same
curve as before: floor + amplitude * exp(-distance/tau)). Other buckets are
fixed multipliers off that anchor — a modeling simplification, not a fitted
relationship:

  houses: 2-bed x0.82, 3-bed x1.00 (anchor), 4-bed x1.28, 5-bed+ x1.62
  units:  1-bed x0.68, 2-bed x1.00 (unit anchor), 3-bed+ x1.35

The unit anchor itself is a fraction of the house anchor that RISES with
distance from the CBD (0.45 near the CBD, approaching ~0.69 at 60km) — the
premise being that the house/unit price gap is proportionally wider in the
inner ring (harbourside land value, heritage terraces) than in the outer
suburbs where houses and units are closer in scale. This is a plausible
shape, not a measured one.

WATERFRONT OVERRIDE
---------------------
Distance-from-CBD alone badly underprices small, low-density waterfront
enclaves that happen to sit further out — e.g. Burraneer (~26km, on Port
Hacking) came out priced like a generic mid-ring suburb before this was
added, when real waterfront houses there run well above that. A hand-curated
list of ~80 well-known Sydney Harbour / Middle Harbour / river / Pittwater /
Port Hacking / Georges River localities (WATERFRONT_SUBURBS — real places,
not exhaustive, not derived from any dataset) gets a price multiplier
(1.85x houses, 1.30x units) and a turnover discount (0.55x sample size, for
their typically smaller and lower-density dwelling stock). Still a
placeholder, still not real sales data — just a less wrong one for suburbs
this specific curve handles badly.

DEVELOPMENT FAVOURABILITY SCORE (0-10)
---------------------------------------
A transparent, documented heuristic — NOT a real planning/zoning
assessment. It has no access to actual LEP zoning, floor-space ratios,
lot sizes, heritage overlays, flood/bushfire mapping, or council DA
approval rates, none of which are available from this environment. It
combines four proxy signals, each scored 0-10 and weighted:

  * growth (35%)       — trailing 5yr price growth, clamped/scaled
  * location (30%)     — a bell curve peaking ~18km from the CBD, on the
                          premise that middle-ring, transit-linked suburbs
                          are the more common rezoning/redevelopment
                          target vs. built-out inner suburbs or
                          infrastructure-light fringe growth areas
  * affordability (20%) — cheaper entry price relative to the priciest
                          suburb in the set, as a rough margin proxy for
                          knockdown-rebuild/townhouse economics
  * turnover (15%)      — trailing-12mo sales volume, as a liquidity proxy

This is a reasonable starting shape for a real score, but every weight and
curve here is a guess. Do not use it for actual investment or development
decisions without professional and council verification.

USAGE
-----
    uv run python scripts/generate_placeholder_suburbs.py

Writes data/sydney_suburbs_placeholder.json.
"""

import csv
import io
import json
import math
import re
from pathlib import Path

import requests

POSTCODE_CSV_URL = (
    "https://raw.githubusercontent.com/matthewproctor/australianpostcodes/"
    "master/australian_postcodes.csv"
)
CBD_LAT, CBD_LON = -33.8688, 151.2093
RADIUS_KM = 60
OUTPUT_PATH = Path("data/sydney_suburbs_placeholder.json")

SPECIAL_PREFIXES = re.compile(r"\b(Mc)([a-z])", re.IGNORECASE)
NON_RESIDENTIAL_SUFFIX = re.compile(r"\s(DC|BC|MC)$")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def title_case(name: str) -> str:
    words = name.title().split(" ")
    fixed = [SPECIAL_PREFIXES.sub(lambda m: "Mc" + m.group(2).upper(), w) for w in words]
    return " ".join(fixed)


def str_hash(s: str) -> int:
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return h


def pseudo_rand(seed: int, salt: int) -> float:
    """Deterministic 0..1 pseudo-random value from a seed (LCG-style)."""
    x = (seed ^ (salt * 2654435761)) & 0xFFFFFFFF
    x = (1103515245 * x + 12345) & 0xFFFFFFFF
    return x / 0xFFFFFFFF


def is_residential_locality(name_raw: str) -> bool:
    if NON_RESIDENTIAL_SUFFIX.search(name_raw):
        return False  # Australia Post facility, not a suburb
    upper = name_raw.upper()
    if upper.startswith("HMAS ") or "AIRPORT" in upper or upper == "DARLING ISLAND":
        return False  # military base / airport / commercial-only precinct
    return True


def fetch_nsw_localities() -> dict[str, tuple[str, float, str]]:
    """Returns {UPPERCASE_NAME: (display_name, distance_km, region)} for NSW
    localities within RADIUS_KM of the CBD, deduped to the nearest postcode
    entry per locality."""
    resp = requests.get(POSTCODE_CSV_URL, timeout=60)
    resp.raise_for_status()

    best: dict[str, tuple[str, float, str]] = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        if row["state"] != "NSW" or row.get("type") != "Delivery Area":
            continue
        name_raw = row["locality"].strip()
        if not is_residential_locality(name_raw):
            continue
        try:
            lat, lon = float(row["lat"]), float(row["long"])
        except (ValueError, TypeError):
            continue
        if lat == 0 or lon == 0:
            continue
        distance = haversine_km(CBD_LAT, CBD_LON, lat, lon)
        if distance > RADIUS_KM:
            continue
        region = row.get("sa3name", "").strip() or row.get("sa4name", "").strip() or "Greater Sydney"
        key = name_raw.upper()
        if key not in best or distance < best[key][1]:
            best[key] = (name_raw, distance, region)
    return best


HOUSE_BED_MULTIPLIERS = {2: 0.82, 3: 1.00, 4: 1.28, 5: 1.62}  # 5 == "5+"
UNIT_BED_MULTIPLIERS = {1: 0.68, 2: 1.00, 3: 1.35}  # 3 == "3+"

# Rough real-world bedroom mix, used only to weight synthetic sample counts
# and to pick realistic "recent sale" bedroom counts. Not measured. Shares
# for a dwelling type sum to ~1.0 so its bedroom buckets sum to roughly its
# "overall" (share=1.0) sample size, not multiply past it.
HOUSE_BED_SHARE = {2: 0.18, 3: 0.42, 4: 0.30, 5: 0.10}
UNIT_BED_SHARE = {1: 0.30, 2: 0.50, 3: 0.20}

# Well-known Sydney Harbour / Middle Harbour / Parramatta & Lane Cove River /
# Pittwater / Port Hacking / Georges River waterfront localities. Real
# places, hand-curated from general knowledge of Sydney geography — NOT
# exhaustive, and not derived from any dataset. They exist because the
# distance-from-CBD price curve alone badly underprices small, low-density
# waterfront enclaves (e.g. Burraneer, 26km out on Port Hacking, priced like
# a generic mid-ring suburb by distance alone when real waterfront houses
# there run well into 7 figures above that). WATERFRONT_PRICE_MULT and
# WATERFRONT_SAMPLE_MULT below are equally hand-picked, not fitted.
WATERFRONT_SUBURBS = {
    "VAUCLUSE", "WATSONS BAY", "POINT PIPER", "DARLING POINT", "ELIZABETH BAY",
    "ROSE BAY", "ROSE BAY NORTH", "DOUBLE BAY", "KIRRIBILLI", "MILSONS POINT",
    "KURRABA POINT", "CREMORNE POINT", "MOSMAN", "CLIFTON GARDENS", "BALMORAL",
    "CASTLECRAG", "NORTHBRIDGE", "BEAUTY POINT", "SEAFORTH", "CLONTARF",
    "BALGOWLAH HEIGHTS", "FAIRLIGHT", "HUNTERS HILL", "HUNTERS HILL WEST",
    "WOOLWICH", "HENLEY", "HUNTLEYS POINT", "HUNTLEYS COVE", "RIVERVIEW",
    "LONGUEVILLE", "NORTHWOOD", "LINLEY POINT", "GREENWICH", "WOLLSTONECRAFT",
    "WAVERTON", "BIRCHGROVE", "BALMAIN EAST", "DRUMMOYNE", "CHISWICK",
    "ABBOTSFORD", "CABARITA", "RODD POINT", "RUSSELL LEA", "MORTLAKE",
    "PUTNEY", "TENNYSON POINT", "BORONIA PARK", "GLADESVILLE",
    "PALM BEACH", "WHALE BEACH", "CAREEL BAY", "AVALON BEACH", "BILGOLA",
    "NEWPORT", "BAYVIEW", "CHURCH POINT", "ELVINA BAY", "LOVETT BAY",
    "SCOTLAND ISLAND", "CLAREVILLE", "TAYLORS POINT",
    "BURRANEER", "DOLANS BAY", "YOWIE BAY", "LILLI PILLI", "PORT HACKING",
    "WOOLOOWARE", "GYMEA BAY", "GRAYS POINT", "BONNET BAY", "COMO",
    "OYSTER BAY", "ILLAWONG", "VOYAGER POINT", "SYLVANIA WATERS",
    "KANGAROO POINT", "CARAVAN HEAD",
    "OATLEY", "CONNELLS POINT", "BLAKEHURST", "CARSS PARK", "KYLE BAY",
    "SANS SOUCI", "SANDRINGHAM", "DOLLS POINT", "RAMSGATE BEACH",
    "BEROWRA WATERS", "DANGAR ISLAND", "ST HUBERTS ISLAND",
}
WATERFRONT_PRICE_MULT = {"house": 1.85, "unit": 1.30}
WATERFRONT_SAMPLE_MULT = 0.55  # smaller, lower-turnover dwelling stock


def _bucket_stats(seed: int, salt_base: int, anchor_median: float, anchor_g5: float,
                   anchor_sample: float, multiplier: float, share: float) -> dict:
    """One bedroom-bucket's median/g1/g5/sample_size/min/max, derived from
    the dwelling-type anchor with deterministic per-bucket noise. min/max
    model the spread of sale prices within the bucket (condition, land
    size, aspect) as a fraction of that bucket's own median — same
    modeling status as everything else here: plausible, not measured."""
    median = round(anchor_median * multiplier * (0.94 + 0.12 * pseudo_rand(seed, salt_base)) / 5000) * 5000
    g5 = max(-8.0, min(65.0, anchor_g5 + (pseudo_rand(seed, salt_base + 1) - 0.5) * 6))
    g1 = max(-4.5, min(9.5, g5 / 8.5 + (pseudo_rand(seed, salt_base + 2) - 0.5) * 3))
    sample = int(round(max(4, anchor_sample * share * (0.8 + 0.4 * pseudo_rand(seed, salt_base + 3)))))
    min_price = round(median * (0.55 + 0.15 * pseudo_rand(seed, salt_base + 4)) / 5000) * 5000
    max_price = round(median * (1.35 + 0.45 * pseudo_rand(seed, salt_base + 5)) / 5000) * 5000
    return {
        "median": int(median), "g1": round(g1, 1), "g5": round(g5, 1), "sample_size": sample,
        "min": int(min_price), "max": int(max_price),
    }


def synthesize_dom(seed: int, salt: int, distance_km: float, g5: float, is_waterfront: bool) -> int:
    """Median "days on market" (illustrative — see the DAYS ON MARKET note
    at the top of this file): a base that rises gently with distance from
    the CBD, pulled down for suburbs with strong 5yr growth (more buyer
    demand -> faster sales), pushed up for waterfront/niche suburbs (a
    thinner buyer pool), plus noise. Clamped to a plausible 14-95 day
    range. Not derived from any real listings data."""
    base = 22 + distance_km * 0.45
    base -= (g5 - 25) * 0.3
    base += (pseudo_rand(seed, salt) - 0.5) * 16
    if is_waterfront:
        base *= 1.25
    return int(round(max(14, min(95, base))))


def synthesize_housing(name_raw: str, distance_km: float) -> dict:
    """Models plausible house and unit price/growth curves by distance from
    the CBD, each split by bedroom count, with deterministic per-suburb
    noise. See the BEDROOM PRICE MODEL note at the top of this file. Not
    real sales data."""
    seed = str_hash(name_raw.upper())

    floor, amplitude, tau = 550_000, 3_200_000, 18.0
    house_anchor = (floor + amplitude * math.exp(-distance_km / tau)) * (0.85 + 0.30 * pseudo_rand(seed, 1))

    house_g5_base = 55 - (house_anchor - floor) / 3_200_000 * 42
    house_g5 = max(-8.0, min(65.0, house_g5_base + (pseudo_rand(seed, 2) - 0.5) * 16))

    sample_base = 20 + (3_750_000 - house_anchor) / 3_200_000 * 55
    house_sample_anchor = max(8.0, sample_base + (pseudo_rand(seed, 4) - 0.5) * 30)

    unit_ratio = 0.45 + 0.24 * (1 - math.exp(-distance_km / 20))
    unit_anchor = house_anchor * unit_ratio
    unit_g5 = max(-8.0, min(65.0, house_g5 * 0.85 - 2 + (pseudo_rand(seed, 5) - 0.5) * 6))
    unit_sample_anchor = house_sample_anchor * (0.7 + 0.5 * (1 - math.exp(-distance_km / 15)))

    is_waterfront = name_raw.upper() in WATERFRONT_SUBURBS
    if is_waterfront:
        house_anchor *= WATERFRONT_PRICE_MULT["house"]
        unit_anchor *= WATERFRONT_PRICE_MULT["unit"]
        house_sample_anchor *= WATERFRONT_SAMPLE_MULT
        unit_sample_anchor *= WATERFRONT_SAMPLE_MULT

    houses = {
        "overall": _bucket_stats(seed, 10, house_anchor, house_g5, house_sample_anchor, 1.0, 1.0),
        "by_beds": {
            str(beds): _bucket_stats(seed, 20 + beds * 4, house_anchor, house_g5, house_sample_anchor, mult, HOUSE_BED_SHARE[beds])
            for beds, mult in HOUSE_BED_MULTIPLIERS.items()
        },
        "dom_days": synthesize_dom(seed, 90, distance_km, house_g5, is_waterfront),
    }
    units = {
        "overall": _bucket_stats(seed, 60, unit_anchor, unit_g5, unit_sample_anchor, 1.0, 1.0),
        "by_beds": {
            str(beds): _bucket_stats(seed, 70 + beds * 4, unit_anchor, unit_g5, unit_sample_anchor, mult, UNIT_BED_SHARE[beds])
            for beds, mult in UNIT_BED_MULTIPLIERS.items()
        },
        "dom_days": synthesize_dom(seed, 95, distance_km, unit_g5, is_waterfront),
    }
    return {"houses": houses, "units": units}


def synthesize_recent_sales(name_raw: str, houses: dict, units: dict, count: int = 8) -> list[dict]:
    """A short illustrative list of "recent sales" for a suburb, mixing
    houses and units: date offset, price, dwelling type, and bedroom count
    only — no addresses, agents, or vendor details, since these are not
    real transactions."""
    seed = str_hash(name_raw.upper())
    sales = []
    for i in range(count):
        is_unit = pseudo_rand(seed, 400 + i) < 0.4
        bed_shares = UNIT_BED_SHARE if is_unit else HOUSE_BED_SHARE
        multipliers = UNIT_BED_MULTIPLIERS if is_unit else HOUSE_BED_MULTIPLIERS
        anchor = units["overall"]["median"] if is_unit else houses["overall"]["median"]

        r = pseudo_rand(seed, 450 + i)
        cumulative = 0.0
        beds = next(iter(bed_shares))
        for b, share in bed_shares.items():
            cumulative += share
            if r <= cumulative:
                beds = b
                break

        days_ago = int(3 + pseudo_rand(seed, 500 + i) * 115)  # within ~last 4 months
        price = round(anchor * multipliers[beds] * (0.88 + pseudo_rand(seed, 550 + i) * 0.24) / 5000) * 5000
        sales.append({
            "days_ago": days_ago,
            "price": int(price),
            "type": "unit" if is_unit else "house",
            "beds": beds,
        })
    sales.sort(key=lambda s: s["days_ago"])
    return sales


def synthesize_dev_score(distance_km: float, median: int, g5: float, sample_size: int,
                          min_median: int, max_median: int) -> dict:
    """See the DEVELOPMENT FAVOURABILITY SCORE note at the top of this file —
    a documented, weighted heuristic over four 0-10 proxy signals. Not a real
    planning/zoning assessment."""
    growth = max(0.0, min(10.0, (g5 + 10) / 70 * 10))

    peak_km, sigma_km = 18.0, 12.0
    location = 10.0 * math.exp(-((distance_km - peak_km) ** 2) / (2 * sigma_km ** 2))

    span = max(1, max_median - min_median)
    affordability = max(0.0, min(10.0, (max_median - median) / span * 10))

    turnover = max(0.0, min(10.0, sample_size / 80 * 10))

    weights = {"growth": 0.35, "location": 0.30, "affordability": 0.20, "turnover": 0.15}
    total = (
        weights["growth"] * growth
        + weights["location"] * location
        + weights["affordability"] * affordability
        + weights["turnover"] * turnover
    )
    return {
        "score": round(max(0.0, min(10.0, total)), 1),
        "breakdown": {
            "growth": round(growth, 1),
            "location": round(location, 1),
            "affordability": round(affordability, 1),
            "turnover": round(turnover, 1),
        },
    }


def main():
    localities = fetch_nsw_localities()

    records = []
    for name_raw, distance_km, region in localities.values():
        housing = synthesize_housing(name_raw, distance_km)
        record = {
            "name": title_case(name_raw),
            "region": region,
            "distance_km": round(distance_km, 1),
            "houses": housing["houses"],
            "units": housing["units"],
        }
        record["recent_sales"] = synthesize_recent_sales(name_raw, housing["houses"], housing["units"])
        records.append(record)

    # Dev score and its affordability normalization are keyed off the house
    # market (3-bed-equivalent "overall" figure) regardless of dwelling type,
    # since suburb-level redevelopment potential is conventionally assessed
    # against house-zoned land.
    house_medians = [r["houses"]["overall"]["median"] for r in records]
    min_median, max_median = min(house_medians), max(house_medians)
    for r in records:
        house_overall = r["houses"]["overall"]
        dev = synthesize_dev_score(r["distance_km"], house_overall["median"], house_overall["g5"],
                                    house_overall["sample_size"], min_median, max_median)
        r["dev_score"] = dev["score"]
        r["dev_breakdown"] = dev["breakdown"]

    records.sort(key=lambda r: r["distance_km"])

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(records, indent=1))
    print(f"Wrote {len(records)} suburbs to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
