import math

import pytest
from pyproj import Geod

from oceanroute.geodesy import WGS84_A, WGS84_E2, densify, interpolate, inverse, split_antimeridian


def test_equatorial_rhumb_golden_and_dateline_short_arc():
    expected = WGS84_A * math.radians(1)
    assert inverse(0, 0, 1, 0) == pytest.approx((expected, 90), abs=1e-8)
    assert inverse(179, 0, -179, 0) == pytest.approx((2 * expected, 90), abs=1e-8)
    assert inverse(-179, 0, 179, 0) == pytest.approx((2 * expected, 270), abs=1e-8)


def test_rhumb_parallel_uses_ellipsoidal_prime_vertical_radius():
    phi = math.radians(80)
    expected = WGS84_A * math.cos(phi) / math.sqrt(1 - WGS84_E2 * math.sin(phi) ** 2) * math.radians(30)
    length, bearing = inverse(-15, 80, 15, 80)
    assert length == pytest.approx(expected, abs=1e-7)
    assert bearing == 90
    # A parallel is longer than the shortest ellipsoidal geodesic.
    assert length > inverse(-15, 80, 15, 80, "geodesic")[0]


def test_meridian_rhumb_agrees_with_proj_and_reverses():
    expected = Geod(ellps="WGS84").inv(12, -40, 12, 60)[2]
    assert inverse(12, -40, 12, 60) == pytest.approx((expected, 0), abs=1e-7)
    assert inverse(12, 60, 12, -40) == pytest.approx((expected, 180), abs=1e-7)


@pytest.mark.parametrize("coords", [(118, 20, 120, 25), (179, 80, -175, 81), (-100, -72, -80, -40), (0, 0, 0.001, 1e-9)])
def test_rhumb_length_fraction_adds_and_bearing_is_constant(coords):
    lon1, lat1, lon2, lat2 = coords
    total, heading = inverse(*coords)
    mid = interpolate(*coords, .37)
    first, b1 = inverse(lon1, lat1, *mid)
    second, b2 = inverse(*mid, lon2, lat2)
    assert first == pytest.approx(.37 * total, rel=2e-8, abs=1e-5)
    assert first + second == pytest.approx(total, rel=2e-8, abs=1e-5)
    assert b1 == pytest.approx(heading, abs=2e-6)
    assert b2 == pytest.approx(heading, abs=2e-6)


def test_interpolation_preserves_dateline_geometry_and_geodesic_length():
    mid = interpolate(179, 20, -179, 20, .5)
    assert abs(abs(mid[0]) - 180) < 1e-8
    assert mid[1] == 20
    total = inverse(-73, 40, -1, 51, "geodesic")[0]
    mid = interpolate(-73, 40, -1, 51, .5, "geodesic")
    assert inverse(-73, 40, *mid, "geodesic")[0] == pytest.approx(total / 2, abs=1e-6)


def test_repeated_points_and_pole_policy():
    assert inverse(118, 23, 118, 23) == (0, None)
    assert inverse(0, 89, 0, 90)[1] == 0
    with pytest.raises(ValueError, match="极点"):
        inverse(0, 89, 10, 90)
    assert inverse(0, 89, 10, 90, "geodesic")[0] > 0


@pytest.mark.parametrize("coords", [(float("nan"), 0, 0, 0), (181, 0, 0, 0), (0, 91, 0, 0), (True, 0, 0, 0)])
def test_invalid_coordinate_rejected(coords):
    with pytest.raises(ValueError):
        inverse(*coords)


def test_densification_surface_segments_sum_to_original():
    points = densify(178, 40, -172, 50, spacing_m=10000)
    length = sum(inverse(*a, *b)[0] for a, b in zip(points, points[1:]))
    assert length == pytest.approx(inverse(178, 40, -172, 50)[0], rel=1e-10)


@pytest.mark.parametrize("curve", ["rhumb", "geodesic"])
@pytest.mark.parametrize("coords", [[(179, 20), (-179, 21)], [(-179, 20), (179, 21)]])
def test_antimeridian_rendering_preserves_physical_length(curve, coords):
    segments = split_antimeridian(coords, curve)
    assert len(segments) == 2
    assert abs(segments[0][-1][0]) == 180
    assert segments[0][-1][0] == -segments[1][0][0]
    assert segments[0][-1][1] == segments[1][0][1]
    length = sum(inverse(*a, *b, curve)[0] for part in segments for a, b in zip(part, part[1:]))
    assert length == pytest.approx(inverse(*coords[0], *coords[1], curve)[0], abs=1e-6)
    assert all(abs(b[0] - a[0]) <= 180 for part in segments for a, b in zip(part, part[1:]))
