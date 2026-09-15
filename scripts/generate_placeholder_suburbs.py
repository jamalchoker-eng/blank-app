"""
Generate a placeholder suburb dataset for the Sydney dashboard.
=================================================================

Real geography, synthetic prices. This script downloads a public Australia
Post / ABS postcode dataset, keeps every NSW locality within a given radius
of Sydney CBD, and models a plausible-looking (but NOT real) median house
price and 1yr/5yr growth for each one as a function of distance from the
CBD, plus deterministic per-suburb noise.

It exists to give `dashboard.html` full, realistic-looking coverage while
`nsw_sales_pipeline.py` can't be run against the actual NSW Valuer General
(no outbound access to nsw.gov.au from this environment). Suburb names,
their ABS SA3 "region", and their distance from the CBD are real. Median
price, g1 (1yr growth), g5 (5yr growth), the "recent sales" feed, and the
development favourability score are NOT — they're synthetic placeholders
for laying out the dashboard, and should be replaced by
`nsw_sales_pipeline.py`'s real output as soon as that can run.

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


def synthesize_metrics(name_raw: str, distance_km: float) -> dict:
    """Models a plausible median price / growth curve by distance from the
    CBD, with deterministic per-suburb noise. Not real sales data."""
    seed = str_hash(name_raw.upper())

    floor, amplitude, tau = 550_000, 3_200_000, 18.0
    base_price = floor + amplitude * math.exp(-distance_km / tau)
    noise = 0.85 + 0.30 * pseudo_rand(seed, 1)  # +/- 15%
    median = round(base_price * noise / 5000) * 5000

    g5_base = 55 - (median - floor) / 3_200_000 * 42
    g5 = max(-8, min(65, g5_base + (pseudo_rand(seed, 2) - 0.5) * 16))

    g1 = max(-4.5, min(9.5, g5 / 8.5 + (pseudo_rand(seed, 3) - 0.5) * 4))

    sample_base = 20 + (3_750_000 - median) / 3_200_000 * 55
    sample_size = int(round(max(8, sample_base + (pseudo_rand(seed, 4) - 0.5) * 30)))

    return {
        "median": int(median),
        "g1": round(g1, 1),
        "g5": round(g5, 1),
        "sample_size": sample_size,
    }


def synthesize_recent_sales(name_raw: str, median: float, count: int = 6) -> list[dict]:
    """A short illustrative list of "recent sales" for a suburb: date offset,
    price, and bedroom count only — no addresses, agents, or vendor details,
    since these are not real transactions."""
    seed = str_hash(name_raw.upper())
    sales = []
    for i in range(count):
        days_ago = int(3 + pseudo_rand(seed, 100 + i) * 115)  # within ~last 4 months
        price = round(median * (0.82 + pseudo_rand(seed, 200 + i) * 0.42) / 5000) * 5000
        beds = 2 + int(pseudo_rand(seed, 300 + i) * 4)  # 2-5 bedrooms
        sales.append({"days_ago": days_ago, "price": int(price), "beds": beds})
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
        record = {
            "name": title_case(name_raw),
            "region": region,
            "distance_km": round(distance_km, 1),
        }
        record.update(synthesize_metrics(name_raw, distance_km))
        record["recent_sales"] = synthesize_recent_sales(name_raw, record["median"])
        records.append(record)

    medians = [r["median"] for r in records]
    min_median, max_median = min(medians), max(medians)
    for r in records:
        dev = synthesize_dev_score(r["distance_km"], r["median"], r["g5"], r["sample_size"],
                                    min_median, max_median)
        r["dev_score"] = dev["score"]
        r["dev_breakdown"] = dev["breakdown"]

    records.sort(key=lambda r: r["distance_km"])

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(records, indent=1))
    print(f"Wrote {len(records)} suburbs to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
