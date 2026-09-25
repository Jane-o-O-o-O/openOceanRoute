"""Independent acceptance for smooth near-parallel rhumb positions."""
import math

import pytest
from scipy.integrate import quad

from oceanroute.geodesy import (GEOD, WGS84_A, WGS84_E2,
                               _rhumb_psi_per_meridian, interpolate, inverse)


@pytest.mark.parametrize("latitude", [-85., -50., 0., 50., 73., 85.])
@pytest.mark.parametrize("delta", [1e-9, 5e-6, 1e-3])
def test_normalized_divided_difference_matches_independent_quadrature(latitude, delta):
    a, b = map(math.radians, (latitude, latitude+delta))
    def derivative(t, meridian):
        phi = a+(b-a)*t
        scale = 1-WGS84_E2*math.sin(phi)**2
        if meridian:
            return WGS84_A*(1-WGS84_E2)/scale**1.5
        return (1-WGS84_E2)/(math.cos(phi)*scale)
    psi = quad(lambda t: derivative(t, False), 0., 1., epsabs=1e-11, epsrel=1e-12)[0]
    meridian = quad(lambda t: derivative(t, True), 0., 1., epsabs=1e-7, epsrel=1e-12)[0]
    assert _rhumb_psi_per_meridian(a, b) == pytest.approx(psi/meridian, rel=1e-14)


@pytest.mark.parametrize("longitude,latitude", [(179.999, 25.), (179.999, 50.), (45., 73.), (-179.999, -73.)])
def test_near_east_west_fraction_is_smooth_and_heading_remains_constant(longitude, latitude):
    start = GEOD.fwd(longitude, latitude, 270, 2500)[:2]
    end = (longitude, latitude)
    length, heading = inverse(*start, *end)
    samples = [interpolate(*start, *end, f) for f in (.12, .37, .68, .99)]
    for fraction, point in zip((.12, .37, .68, .99), samples):
        distance, bearing = inverse(*start, *point)
        assert distance == pytest.approx(length*fraction, abs=1e-7)
        assert bearing == pytest.approx(heading, abs=1e-7)
    # The original failure quantized changing KP to an identical longitude
    # for micrometre steps. These two real positions must remain distinct.
    p = interpolate(*start, *end, .87999999999)
    q = interpolate(*start, *end, .88)
    assert GEOD.inv(*p, *q)[2] > 5e-9

