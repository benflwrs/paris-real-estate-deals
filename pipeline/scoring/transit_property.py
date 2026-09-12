"""Property-level transit-line score.

Ben's brief: instead of "time to Chatelet", score a property by which
GOOD transit lines are within walking distance:
  - within a ~12 min walk of a stop -> add the line's full quality score
  - 12-20 min walk -> add half the line's quality score
  - beyond 20 min -> that line contributes nothing

If a property is near multiple stops on the same line, only the nearest
stop for that line counts (no double-counting a single line via two stops).
If it's near stops on different lines, every distinct line contributes.

Walking speed assumption: 4.5 km/h (75 m/min), a standard urban-walking-time
estimate -- so 12 min ~= 900m as-the-crow-flies (real walking distance is
usually a bit longer than straight-line, but this stays a simple, fast,
explainable model; can be swapped for a real routing engine later without
changing the scoring formula).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import httpx

from pipeline.scoring.transit_lines import compute_line_scores

IDFM_STOPS_URL = "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets/arrets-lignes/records"

WALK_SPEED_M_PER_MIN = 75.0  # ~4.5 km/h, standard urban walking pace
FULL_CREDIT_MAX_MIN = 12
HALF_CREDIT_MAX_MIN = 20

RELEVANT_MODES = ["Metro", "RapidTransit", "Tramway"]

# Maps IDFM's `mode` facet value to our transit_lines.py `mode` key.
MODE_MAP = {
    "Metro": "metro",
    "RapidTransit": "rer",
    "Tramway": "tram",
}


@dataclass
class TransitStop:
    mode: str  # 'metro' | 'rer' | 'tram'
    line_id: str  # e.g. '1', 'A', 'T3a'
    stop_name: str
    lat: float
    lon: float


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def fetch_all_stops(client: Optional[httpx.Client] = None) -> list[TransitStop]:
    """Download every Metro/RER/Tram stop from the free IDF Mobilités API
    (no auth needed). ~1600 rows total across the three modes -- small
    enough to fetch in full and cache, rather than querying per-property."""
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    stops: list[TransitStop] = []
    try:
        for mode in RELEVANT_MODES:
            offset = 0
            limit = 100
            while True:
                resp = client.get(
                    IDFM_STOPS_URL,
                    params={"limit": limit, "offset": offset, "where": f"mode='{mode}'"},
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break
                for r in results:
                    lat = r.get("stop_lat") or (r.get("pointgeo") or {}).get("lat")
                    lon = r.get("stop_lon") or (r.get("pointgeo") or {}).get("lon")
                    line_id = r.get("shortname")
                    stop_name = r.get("stop_name")
                    if lat is None or lon is None or not line_id:
                        continue
                    stops.append(
                        TransitStop(
                            mode=MODE_MAP[mode],
                            line_id=line_id,
                            stop_name=stop_name,
                            lat=float(lat),
                            lon=float(lon),
                        )
                    )
                offset += limit
                if offset >= data.get("total_count", 0):
                    break
        return stops
    finally:
        if own_client:
            client.close()


def walk_minutes(distance_m: float) -> float:
    return distance_m / WALK_SPEED_M_PER_MIN


def compute_transit_line_score(
    lat: float,
    lon: float,
    stops: list[TransitStop],
    line_scores: Optional[dict[str, float]] = None,
    search_radius_m: float = 1600.0,  # ~20min walk + margin
) -> dict:
    """Score one property's location against the full stop list.

    Returns a dict with the total score, and a breakdown of contributing
    lines (for transparency/debugging -- useful when reviewing why a
    property scored the way it did).
    """
    line_scores = line_scores or compute_line_scores()

    # For each distinct (mode, line_id), find the walking time to the
    # nearest stop on that line.
    nearest_by_line: dict[str, float] = {}
    for stop in stops:
        key = f"{stop.mode}:{stop.line_id}"
        dist = haversine_m(lat, lon, stop.lat, stop.lon)
        if dist > search_radius_m:
            continue
        minutes = walk_minutes(dist)
        if key not in nearest_by_line or minutes < nearest_by_line[key]:
            nearest_by_line[key] = minutes

    total = 0.0
    breakdown = []
    for key, minutes in nearest_by_line.items():
        base_score = line_scores.get(key)
        if base_score is None:
            continue
        if minutes <= FULL_CREDIT_MAX_MIN:
            contribution = base_score
            credit = "full"
        elif minutes <= HALF_CREDIT_MAX_MIN:
            contribution = base_score / 2
            credit = "half"
        else:
            continue
        total += contribution
        breakdown.append(
            {"line": key, "walk_min": round(minutes, 1), "credit": credit, "contribution": round(contribution, 1)}
        )

    breakdown.sort(key=lambda b: -b["contribution"])
    return {"transit_line_score": round(total, 1), "breakdown": breakdown}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    args = parser.parse_args()

    all_stops = fetch_all_stops()
    print(f"loaded {len(all_stops)} stops")
    result = compute_transit_line_score(args.lat, args.lon, all_stops)
    print(json.dumps(result, indent=2))
