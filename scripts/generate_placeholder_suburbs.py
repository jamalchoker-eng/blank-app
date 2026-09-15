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
price, g1 (1yr growth) and g5 (5yr growth) are NOT — they're synthetic
placeholders for laying out the dashboard, and should be replaced by
`nsw_sales_pipeline.py`'s real output as soon as that can run.

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
        records.append(record)

    records.sort(key=lambda r: r["distance_km"])

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(records, indent=1))
    print(f"Wrote {len(records)} suburbs to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
