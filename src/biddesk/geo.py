"""Distance from a firm to a notice's place of performance, stdlib only.

Two CSVs under `data/geo/` back this (see `data/SOURCES.md` for sources and licence):
`us_zip_centroids.csv` (zip,lat,lon,city,state) and `us_state_centroids.csv` (state,lat,lon).
Both load once, lazily, on first lookup.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Optional

Precision = str  # "zip" | "city" | "state"

_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "geo"
_ZIP_CSV = _DATA_DIR / "us_zip_centroids.csv"
_STATE_CSV = _DATA_DIR / "us_state_centroids.csv"

_EARTH_RADIUS_MILES = 3958.7613

# Country values SAM.gov uses for the United States; None/blank means the field was not filled in.
_US_COUNTRY = {"", "US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}

_zip_index: Optional[dict[str, tuple[float, float]]] = None
_city_index: Optional[dict[tuple[str, str], tuple[float, float]]] = None
_state_index: Optional[dict[str, tuple[float, float]]] = None


def _load() -> None:
    """Read both CSVs into module-level dicts. Idempotent; called by every lookup."""
    global _zip_index, _city_index, _state_index
    if _zip_index is not None:
        return
    zips: dict[str, tuple[float, float]] = {}
    cities: dict[tuple[str, str], tuple[float, float]] = {}
    with _ZIP_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                point = (float(row["lat"]), float(row["lon"]))
            except (TypeError, ValueError):
                continue
            code = (row["zip"] or "").strip()
            if len(code) == 5:
                zips.setdefault(code, point)
            key = ((row["city"] or "").strip().lower(), (row["state"] or "").strip().upper())
            if key[0] and key[1]:
                cities.setdefault(key, point)
    states: dict[str, tuple[float, float]] = {}
    with _STATE_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                states[(row["state"] or "").strip().upper()] = (float(row["lat"]), float(row["lon"]))
            except (TypeError, ValueError):
                continue
    _zip_index, _city_index, _state_index = zips, cities, states


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def locate(
    city: Optional[str], state: Optional[str], zip_code: Optional[str]
) -> Optional[tuple[float, float, Precision]]:
    """Best available point for a place of performance, coarsening only when the finer lookup misses.

    Order: 5-digit ZIP prefix, then case-insensitive city+state from the ZIP table, then the
    state centroid. Returns None when none of the three resolves.
    """
    _load()
    assert _zip_index is not None and _city_index is not None and _state_index is not None

    code = "".join(ch for ch in (zip_code or "") if ch.isdigit())[:5]
    if len(code) == 5:
        hit = _zip_index.get(code)
        if hit:
            return (hit[0], hit[1], "zip")

    st = (state or "").strip().upper()
    town = (city or "").strip().lower()
    if town and len(st) == 2:
        hit = _city_index.get((town, st))
        if hit:
            return (hit[0], hit[1], "city")

    if len(st) == 2:
        hit = _state_index.get(st)
        if hit:
            return (hit[0], hit[1], "state")

    return None


def distance_from(profile_lat: float, profile_lon: float, notice) -> tuple[Optional[float], str]:
    """Miles from the firm to the notice's place of performance, with how precisely it resolved.

    Returns (None, "unknown") for a non-US place of performance or when nothing resolves.
    """
    country = (getattr(notice, "pop_country", None) or "").strip().upper()
    if country not in _US_COUNTRY:
        return (None, "unknown")

    found = locate(
        getattr(notice, "pop_city", None),
        getattr(notice, "pop_state", None),
        getattr(notice, "pop_zip", None),
    )
    if found is None:
        return (None, "unknown")

    lat, lon, precision = found
    if precision == "state" and not country:
        # A bare two-letter code with no country is ambiguous: DE, GA, etc. are also ISO country codes.
        return (None, "unknown")
    return (haversine_miles(profile_lat, profile_lon, lat, lon), precision)
