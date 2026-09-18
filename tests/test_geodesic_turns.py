"""Independent local-course oracles; do not call core/geodesy bearing helpers."""
from copy import deepcopy

import pytest
from pyproj import Geod

from oceanroute.core import analyze_project


WGS84 = Geod(ellps="WGS84")


def project(coordinates, *, curve="geodesic"):
    return {
        "schema_version": 1, "id": "independent-local-course", "crs": "EPSG:4326",
        "route": {"curve": curve, "mode": "flexible", "slack_basis": "surface", "slack_pct": 0,
                  "points": [{"id": f"p{i}", "longitude": lon, "latitude": lat, "depth_m": 100}
                             for i, (lon, lat) in enumerate(coordinates)]},
        "cable_types": [{"id": "C", "cost_per_m": 0, "lay_speed_m_s": 1}],
        "costs": {"currency": "CNY"}, "bodies": [], "layers": [],
    }


@pytest.mark.parametrize("start,end,fraction", [
    ((0, 70), (90, 70), .5),
    ((170, 65), (-160, 65), .4),
    ((-40, -72), (45, -68), .35),
])
def test_splitting_one_wgs84_geodesic_does_not_manufacture_a_turn(start, end, fraction):
    azimuth, _, length = WGS84.inv(*start, *end)
    lon, lat, incoming = WGS84.fwd(*start, azimuth, length * fraction, return_back_azimuth=False)
    outgoing, _, _ = WGS84.inv(lon, lat, *end)
    # This is one continuous geodesic, not two paths with equal INITIAL courses.
    assert abs((outgoing-incoming+180) % 360-180) < 2e-10
    original = project([start, (lon, lat), end])
    before = deepcopy(original)
    actual = analyze_project(original)
    assert actual["rpl"][1]["turn_deg"] == pytest.approx(0, abs=2e-10)
    assert actual["rpl"][0]["turn_deg"] is None
    assert actual["rpl"][-1]["turn_deg"] is None
    assert original == before


@pytest.mark.parametrize("turn", [35, -70, 179, -179, 180])
def test_known_vertex_tangents_give_the_real_signed_folded_angle(turn):
    # Construct legs OUTWARD from a common vertex with prescribed tangents.
    # Reverse the first leg. Its arrival tangent at the vertex is exactly45°.
    # No production inverse, interpolation or turn implementation is an oracle.
    vertex = (20, 75)
    incoming = 45
    start = WGS84.fwd(*vertex, incoming+180, 150_000)[:2]
    end = WGS84.fwd(*vertex, incoming+turn, 120_000)[:2]
    actual = analyze_project(project([start, vertex, end]))
    if turn == 180:
        # At an exact U-turn the signed representation has equivalent +/-180°
        # limits; tiny inverse rounding may approach the fold from either side.
        assert abs(actual["rpl"][1]["turn_deg"]) == pytest.approx(180, abs=2e-10)
    else:
        assert actual["rpl"][1]["turn_deg"] == pytest.approx(turn, abs=2e-10)
    assert actual["rpl"][1]["bearing_deg"] == pytest.approx((incoming+turn) % 360, abs=2e-10)


def test_date_line_corner_uses_common_vertex_tangents_not_initial_leg_courses():
    vertex = (-179.9, 72)
    start = WGS84.fwd(*vertex, 260, 100_000)[:2]
    end = WGS84.fwd(*vertex, 130, 100_000)[:2]
    assert start[0] > 0 and vertex[0] < 0
    actual = analyze_project(project([start, vertex, end]))
    # Reverse260° arrives eastward80°; outgoing130° => starboard turn50°.
    assert actual["rpl"][1]["turn_deg"] == pytest.approx(50, abs=2e-10)
    assert actual["summary"]["surface_length_m"] == pytest.approx(200_000, abs=1e-6)


@pytest.mark.parametrize("coordinates,expected", [
    ([(0, 70), (1, 70), (1, 71)], -90),
    ([(179.9, 70), (-179.9, 70), (-179.9, 70.2)], -90),
    ([(0, -70), (1, -70), (2, -70)], 0),
])
def test_rhumb_constant_parallel_and_meridian_courses_remain_unchanged(coordinates, expected):
    actual = analyze_project(project(coordinates, curve="rhumb"))
    assert actual["rpl"][1]["turn_deg"] == pytest.approx(expected, abs=1e-11)


@pytest.mark.parametrize("coordinates,unknown_indices", [
    ([(0, 70), (0, 70), (1, 71)], [1]),
    ([(0, 70), (1, 71), (1, 71)], [1]),
    ([(0, 70), (1, 71), (1, 71), (2, 70)], [1, 2]),
])
def test_adjacent_zero_length_legs_have_no_invented_course_change(coordinates, unknown_indices):
    actual = analyze_project(project(coordinates))
    # A repeated coordinate has no one-sided tangent for its ZERO leg. Do not
    # silently claim0°, guess a turn across duplicates or generate NaN.
    for index in unknown_indices:
        assert actual["rpl"][index]["turn_deg"] is None
    assert actual["rpl"][0]["turn_deg"] is None
    assert actual["rpl"][-1]["turn_deg"] is None
