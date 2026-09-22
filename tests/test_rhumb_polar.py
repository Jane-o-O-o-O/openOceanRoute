"""Independent high-precision golden values for finite-float polar rhumb legs.

The committed oracle was actually generated with mpmath at 80 decimal digits;
mpmath is not a test/runtime dependency. Expected coordinates do not call the
production geodesy helpers. PROJ measures the distance between the rounded
expected and actual output coordinates; that metric engine is shared.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from pyproj import Geod
import pytest

from oceanroute.geodesy import interpolate, inverse


ORACLE_PATH = Path(__file__).parent / "fixtures" / "rhumb_polar_oracles.json"
ORACLE = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
CASES = ORACLE["cases"]
METRIC = Geod(ellps="WGS84")


def angle_error(actual, expected):
    return abs((actual - expected + 180.) % 360. - 180.)


def test_oracle_declares_exact_float_inputs_and_independent_high_precision_method():
    assert ORACLE["schema_version"] == 1
    assert ORACLE["oracle"]["precision_decimal_digits"] == 80
    assert ORACLE["oracle"]["WGS84"] == {
        "a_m": "6378137", "inverse_flattening": "298.257223563",
    }
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert [float.fromhex(v) for v in case["input_float_hex"]] == case["coordinates"]
        assert all(math.isfinite(v) for v in case["coordinates"])
        assert math.isfinite(case["distance_m"]) and case["distance_m"] > 0.
    json.dumps(ORACLE, allow_nan=False)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_polar_inverse_matches_independent_meridian_and_isometric_golden(case):
    length, heading = inverse(*case["coordinates"], "rhumb")
    assert math.isfinite(length) and math.isfinite(heading)
    if case["id"] == "polar_same_latitude":
        # A micrometre absolute tolerance would miss the 14% error of the old
        # rounded-radian cos at the representable latitude closest to a pole.
        assert length == pytest.approx(case["distance_m"], rel=2e-14, abs=0.)
    else:
        assert length == pytest.approx(case["distance_m"], rel=0., abs=1e-6)
    assert angle_error(heading, case["heading_deg"]) <= 1e-9


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_polar_physical_fraction_matches_independent_meridian_root(case):
    for point in case["points"]:
        actual = interpolate(*case["coordinates"], point["fraction"], "rhumb")
        assert all(math.isfinite(v) for v in actual)
        expected = (point["longitude"], point["latitude"])
        assert METRIC.inv(*actual, *expected)[2] <= 3e-6
    assert interpolate(*case["coordinates"], 0., "rhumb") == tuple(case["coordinates"][:2])
    assert interpolate(*case["coordinates"], 1., "rhumb") == tuple(case["coordinates"][2:])


def test_polar_reverse_preserves_length_and_reverses_constant_heading():
    original = next(case for case in CASES if case["id"] == "northern_wide_ratio_almost_one")
    reversed_case = next(case for case in CASES if case["id"] == "northern_wide_reversed")
    a = inverse(*original["coordinates"], "rhumb")
    b = inverse(*reversed_case["coordinates"], "rhumb")
    assert a[0] == pytest.approx(b[0], abs=1e-8)
    assert angle_error(b[1], a[1] + 180.) < 1e-9


@pytest.mark.parametrize("latitude", [90., -90.])
@pytest.mark.parametrize("fraction", [None, .37])
def test_exact_pole_nonmeridian_remains_an_explicitly_rejected_rhumb(latitude, fraction):
    args = (0., 80. if latitude > 0 else -80., 180., latitude)
    with pytest.raises(ValueError, match="极点"):
        if fraction is None:
            inverse(*args, "rhumb")
        else:
            interpolate(*args, fraction, "rhumb")
