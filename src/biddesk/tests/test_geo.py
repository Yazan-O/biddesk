"""Distance checks for the owner rule "no bids over 200 miles from Oklahoma City"."""
from __future__ import annotations

from biddesk.geo import distance_from, haversine_miles, locate
from biddesk.models import Notice

OKC_LAT, OKC_LON = 35.4676, -97.5164   # Oklahoma City, OK


def _notice(**pop) -> Notice:
    return Notice(
        notice_id="test",
        title="test notice",
        type="Solicitation",
        posted="2026-09-13",
        ui_link="https://sam.gov/opp/test/view",
        fetched_at="2026-09-13T18:00:00-05:00",
        **pop,
    )


def test_okc_to_tulsa_about_100_miles():
    tulsa = locate("Tulsa", "OK", None)
    assert tulsa is not None and tulsa[2] == "city"
    miles = haversine_miles(OKC_LAT, OKC_LON, tulsa[0], tulsa[1])
    assert 90 <= miles <= 115, miles

    miles2, precision = distance_from(
        OKC_LAT, OKC_LON, _notice(pop_city="Tulsa", pop_state="OK", pop_zip="74103", pop_country="USA")
    )
    assert precision == "zip"
    assert 90 <= miles2 <= 115, miles2


def test_okc_to_wilmington_de_over_1200_miles():
    miles, precision = distance_from(
        OKC_LAT, OKC_LON,
        _notice(pop_city="Wilmington", pop_state="DE", pop_zip="19807", pop_country="USA"),
    )
    assert precision == "zip"
    assert miles is not None and miles > 1200, miles


def test_state_only_falls_back_to_state_centroid():
    miles, precision = distance_from(
        OKC_LAT, OKC_LON, _notice(pop_city=None, pop_state="AK", pop_zip=None, pop_country="USA")
    )
    assert precision == "state"
    assert miles is not None and miles > 1500, miles


def test_unresolvable_place_is_unknown():
    miles, precision = distance_from(OKC_LAT, OKC_LON, _notice())
    assert (miles, precision) == (None, "unknown")


def test_foreign_country_is_unknown():
    miles, precision = distance_from(
        OKC_LAT, OKC_LON,
        _notice(pop_city="Libreville", pop_state="GA-1", pop_zip="00000", pop_country="GAB"),
    )
    assert (miles, precision) == (None, "unknown")


def test_bad_zip_falls_through_to_city_then_state():
    # 00000 is not a real ZIP; the city+state pair still resolves.
    miles, precision = distance_from(
        OKC_LAT, OKC_LON,
        _notice(pop_city="Norman", pop_state="OK", pop_zip="00000", pop_country="USA"),
    )
    assert precision == "city"
    assert miles is not None and miles < 40, miles

    # Unknown city inside a known state coarsens to the state centroid.
    _, precision2 = distance_from(
        OKC_LAT, OKC_LON,
        _notice(pop_city="Nowheresville", pop_state="OK", pop_zip=None, pop_country="USA"),
    )
    assert precision2 == "state"


# ---------------------------------------------------------------------------
# Stress-test regressions added by the tiering/geo review (2026-09-13).
# ---------------------------------------------------------------------------
import math

import pytest


def _law_of_cosines_miles(lat1, lon1, lat2, lon2):
    """Independent great-circle formula on the same sphere (radius 3958.7613 mi)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    return 3958.7613 * math.acos(min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))


def _vincenty_miles(lat1, lon1, lat2, lon2):
    """WGS84 ellipsoidal geodesic (Vincenty inverse), the independent check on the sphere model."""
    a, f = 6378137.0, 1 / 298.257223563
    b = (1 - f) * a
    L = math.radians(lon2 - lon1)
    U1, U2 = math.atan((1 - f) * math.tan(math.radians(lat1))), math.atan((1 - f) * math.tan(math.radians(lat2)))
    sU1, cU1, sU2, cU2 = math.sin(U1), math.cos(U1), math.sin(U2), math.cos(U2)
    lam, c2m, ss, cs, sig, c2 = L, 0.0, 0.0, 0.0, 0.0, 0.0
    for _ in range(200):
        sl, cl = math.sin(lam), math.cos(lam)
        ss = math.sqrt((cU2 * sl) ** 2 + (cU1 * sU2 - sU1 * cU2 * cl) ** 2)
        if ss == 0:
            return 0.0
        cs = sU1 * sU2 + cU1 * cU2 * cl
        sig = math.atan2(ss, cs)
        sa = cU1 * cU2 * sl / ss
        c2 = 1 - sa ** 2
        c2m = cs - 2 * sU1 * sU2 / c2 if c2 != 0 else 0.0
        C = f / 16 * c2 * (4 + f * (4 - 3 * c2))
        prev, lam = lam, L + (1 - C) * f * sa * (sig + C * ss * (c2m + C * cs * (-1 + 2 * c2m ** 2)))
        if abs(lam - prev) < 1e-12:
            break
    u2 = c2 * (a * a - b * b) / (b * b)
    A = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))
    B = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)))
    dsig = B * ss * (c2m + B / 4 * (cs * (-1 + 2 * c2m ** 2) - B / 6 * c2m * (-3 + 4 * ss ** 2) * (-3 + 4 * c2m ** 2)))
    return b * A * (sig - dsig) / 1609.344


FIVE_PAIRS = ["73069", "19801", "33101", "99501", "96813"]   # Norman, Wilmington DE, Miami, Anchorage, Honolulu


def test_haversine_matches_two_independent_formulas():
    """The sphere formula is exact against the law of cosines and within 0.25% of the WGS84 geodesic."""
    for zip_code in FIVE_PAIRS:
        pt = locate(None, None, zip_code)
        assert pt is not None, zip_code
        hav = haversine_miles(OKC_LAT, OKC_LON, pt[0], pt[1])
        assert abs(hav - _law_of_cosines_miles(OKC_LAT, OKC_LON, pt[0], pt[1])) < 1e-6, zip_code
        vin = _vincenty_miles(OKC_LAT, OKC_LON, pt[0], pt[1])
        assert abs(hav - vin) / vin < 0.0025, (zip_code, hav, vin)


def test_zip_plus_four_resolves_at_zip_precision():
    """SAM emits ZIP+4 and padded ZIPs; both must keep ZIP precision, not coarsen to the state."""
    for z in ("73102-1234", " 73102 ", "73102"):
        miles, precision = distance_from(OKC_LAT, OKC_LON, _notice(pop_zip=z, pop_country="USA"))
        assert precision == "zip" and miles is not None and miles < 5, (z, miles, precision)


def test_numeric_junk_city_coarsens_to_the_state():
    """Real corpus: SAM sends placeOfPerformance.city.name = '0' and '46357' with a valid state."""
    for junk in ("0", "46357"):
        miles, precision = distance_from(
            OKC_LAT, OKC_LON, _notice(pop_city=junk, pop_state="UT", pop_zip=None, pop_country="USA"))
        assert precision == "state" and miles is not None and miles > 700, (junk, miles)


def test_territories_resolve_and_apo_does_not():
    """PR/GU/VI have centroids; AA/AE/AP (military post offices) are absent, so they stay unknown."""
    for st, floor in (("PR", 2000), ("GU", 6000), ("VI", 2000)):
        miles, precision = distance_from(OKC_LAT, OKC_LON, _notice(pop_state=st, pop_country="USA"))
        assert precision == "state" and miles is not None and miles > floor, (st, miles)
    for st in ("AA", "AE", "AP"):
        assert distance_from(OKC_LAT, OKC_LON,
                             _notice(pop_city="APO", pop_state=st, pop_zip=None, pop_country="USA")) == (None, "unknown")


def test_def02_iso_country_code_in_pop_state_must_not_resolve():
    # placeOfPerformance.country is missing on 58 of the 191 snapshot notices, and locate()
    # accepts any 2-letter state. DE (Germany), IN (India) and GA (Georgia) collide with US states,
    # so a foreign site is reported as "Berlin, DE is 1233 miles from Oklahoma City".
    assert distance_from(OKC_LAT, OKC_LON,
                         _notice(pop_city="Berlin", pop_state="DE", pop_zip=None, pop_country=None)) == (None, "unknown")
