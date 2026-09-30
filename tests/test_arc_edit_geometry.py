"""Independent fixed-radius endpoint certificates, using PROJ geodesics.

Expected circles are constructed before invoking the editor; polygonal length
limits and polar parallel lengths do not use its center/root helpers.
"""
from copy import deepcopy
import json
import math

import numpy as np
from pyproj import Geod
import pytest

from oceanroute.arc_edit_geometry import rebuild_arc_endpoints
from oceanroute.route_geometry import RouteSegment

G = Geod(ellps="WGS84")


def descriptor(center=(0., 0.), radius=1000., azimuth=10., sweep=110.):
    return {"type": "circular_arc", "schema_version": 1, "center": list(center),
            "radius_m": radius, "start_azimuth_deg": azimuth, "sweep_deg": sweep}


def direct(center, az, radius):
    return G.fwd(*center, az, radius)[:2]


def angle(a, b):
    return abs((a-b+180.) % 360.-180.)


def certificate(out, a, b, radius):
    assert out["accepted"], out
    g = out["geometry"]
    assert set(g) == {"type", "schema_version", "center", "radius_m", "start_azimuth_deg", "sweep_deg"}
    assert g["radius_m"] == radius
    for az, p in ((g["start_azimuth_deg"], a), (g["start_azimuth_deg"]+g["sweep_deg"], b)):
        assert G.inv(*g["center"], *p)[2] == pytest.approx(radius, abs=1e-6)
        assert G.inv(*direct(g["center"], az, radius), *p)[2] < 1e-6
    for fraction in np.linspace(0, 1, 17):
        p = direct(g["center"], g["start_azimuth_deg"]+fraction*g["sweep_deg"], radius)
        assert G.inv(*g["center"], *p)[2] == pytest.approx(radius, abs=2e-8)
    for p, label in ((a, "start_tangent_deg"), (b, "end_tangent_deg")):
        _, back, _ = G.inv(*g["center"], *p)
        heading = (back+180+math.copysign(90, g["sweep_deg"])) % 360
        assert angle(out["evidence"][label], heading) < 1e-7
    assert out["budget"]["work_units"] <= out["budget"]["max_work_units"]
    assert out["budget"]["work_units"] == (out["budget"]["direct_evaluations"]+
           out["budget"]["inverse_evaluations"]+out["budget"]["route_segment_work_units"])
    json.dumps(out, allow_nan=False)


@pytest.mark.parametrize("center,radius", [((0, 0), .001), ((179.99, 72), 3),
    ((-179.8, -70), 17000), ((36, 89.99), 1000), ((0, 90), 1_000_000), ((30, -90), 10000)])
@pytest.mark.parametrize("sweep", [73., -73., 267., -267.])
def test_one_endpoint_changes_on_known_circle_preserves_center_radius_and_branch(center, radius, sweep):
    g = descriptor(center, radius, 13, sweep)
    a = direct(center, 13, radius)
    b = direct(center, 13+sweep*.92, radius)
    snapshot = deepcopy((a, b, g))
    out = rebuild_arc_endpoints(a, b, g)
    certificate(out, a, b, radius)
    # Center ill-conditioning amplifies native nanometer errors for the 1mm
    # case; this tolerance measures center geometry rather than azimuth noise.
    assert G.inv(*out["geometry"]["center"], *center)[2] < 2e-7
    assert (abs(out["geometry"]["sweep_deg"]) < 180) == (abs(sweep) < 180)
    assert math.copysign(1, out["geometry"]["sweep_deg"]) == math.copysign(1, sweep)
    assert (a, b, g) == snapshot


@pytest.mark.parametrize("sweep", [55., -55., 305., -305.])
def test_both_endpoints_moved_recover_independently_constructed_center(sweep):
    old = descriptor((179.99, 81), 1_000_000, -20, sweep)
    center = (-172., 78.)
    a, b = direct(center, 48, 1_000_000), direct(center, 48+sweep, 1_000_000)
    out = rebuild_arc_endpoints(a, b, old)
    certificate(out, a, b, 1_000_000)
    assert G.inv(*out["geometry"]["center"], *center)[2] < 2e-7


def test_explicit_minor_major_and_side_are_real_distinct_centers_not_chord_choices():
    g = descriptor((0, 0), 2000, 0, 60)
    a, b = direct((0, 0), 0, 2000), direct((0, 0), 80, 2000)
    small = rebuild_arc_endpoints(a, b, g, config={"branch": "minor"})
    large = rebuild_arc_endpoints(a, b, g, config={"branch": "major"})
    certificate(small, a, b, 2000); certificate(large, a, b, 2000)
    assert small["geometry"]["sweep_deg"] < 180 < large["geometry"]["sweep_deg"]
    assert G.inv(*small["geometry"]["center"], *large["geometry"]["center"])[2] > 1000
    for branch in ("left", "right"):
        out = rebuild_arc_endpoints(a, b, g, config={"branch": branch})
        certificate(out, a, b, 2000)
        chosen = next(p for p in out["evidence"]["centers_candidates"] if p["center_side"] == branch)
        assert out["geometry"]["center"] == chosen["center"]


def test_explicit_branch_is_not_ignored_when_endpoints_have_not_moved():
    g = descriptor((0, 0), 1000, 10, 110)
    a, b = direct((0, 0), 10, 1000), direct((0, 0), 120, 1000)
    unchanged = rebuild_arc_endpoints(a, b, g)
    assert unchanged["evidence"]["selection"] == "unchanged_descriptor"
    major = rebuild_arc_endpoints(a, b, g, config={"branch": "major"})
    certificate(major, a, b, 1000)
    assert major["geometry"]["sweep_deg"] > 180
    assert major["evidence"]["selection"] == "major"
    assert G.inv(*g["center"], *major["geometry"]["center"])[2] > 1000
    for branch in ("left", "right"):
        out = rebuild_arc_endpoints(a, b, g, config={"branch": branch})
        certificate(out, a, b, 1000)
        chosen = next(x for x in out["evidence"]["centers_candidates"] if x["center_side"] == branch)
        assert out["geometry"]["center"] == chosen["center"]


def test_radius_override_keeps_original_reference_evidence_and_builds_new_circle():
    g = descriptor((10, 45), 2000, 20, -230)
    center = (11, 46)
    a, b = direct(center, 30, 5000), direct(center, -200, 5000)
    out = rebuild_arc_endpoints(a, b, g, config={"radius_m": 5000})
    certificate(out, a, b, 5000)
    assert out["evidence"]["original_radius_m"] == 2000
    assert out["evidence"]["radius_overridden"] is True
    assert g["radius_m"] == 2000


def test_semicircle_original_requires_explicit_branch_after_edit_not_nearest_center_guess():
    g = descriptor((0, 0), 1000, 0, 180)
    a, b = direct((0, 0), 0, 1000), direct((0, 0), 150, 1000)
    rejected = rebuild_arc_endpoints(a, b, g)
    assert not rejected["accepted"] and rejected["geometry"] is None
    assert rejected["rejection_codes"] == ["ARC_EDIT_BRANCH_REQUIRED"]
    for branch in ("minor", "major"):
        certificate(rebuild_arc_endpoints(a, b, g, config={"branch": branch}), a, b, 1000)


@pytest.mark.parametrize("sweep", [360., -360.])
def test_full_circle_unchanged_and_explicit_joint_endpoint_move(sweep):
    g = descriptor((179.9, 82), 4000, 15, sweep)
    old_a, old_b = direct(g["center"], 15, 4000), direct(g["center"], 15+sweep, 4000)
    unchanged = rebuild_arc_endpoints(old_a, old_b, g)
    certificate(unchanged, old_a, old_b, 4000)
    assert unchanged["geometry"] == g
    moved = (179.99, 83.)
    no_policy = rebuild_arc_endpoints(moved, moved, g)
    assert no_policy["rejection_codes"] == ["ARC_EDIT_FULL_CIRCLE_POLICY_REQUIRED"]
    old_bearing = G.inv(*old_a, *g["center"])[0]
    expected_center = direct(moved, old_bearing, 4000)
    out = rebuild_arc_endpoints(moved, moved, g, config={"full_circle_policy": "preserve_endpoint_center_bearing"})
    certificate(out, moved, moved, 4000)
    assert out["geometry"]["sweep_deg"] == sweep
    assert G.inv(*expected_center, *out["geometry"]["center"])[2] < 1e-8
    end_other = direct(moved, 90, 100)
    assert rebuild_arc_endpoints(moved, end_other, g, config={"full_circle_policy": "preserve_endpoint_center_bearing"})["rejection_codes"] == ["ARC_EDIT_FULL_CIRCLE_NOT_CLOSED"]


def test_binary_equal_endpoints_override_native_inverse_nonzero_roundoff():
    g = descriptor((0, 0), 300, 0, 360)
    point = (4.4915764255995735e-05, .002713108431129943)
    # Some native builds return a positive rounding residue for identical
    # coordinates; others correctly return zero. Both must use binary identity.
    assert 0 <= G.inv(*point, *point)[2] < 1e-12
    missing = rebuild_arc_endpoints(point, point, g)
    assert missing["rejection_codes"] == ["ARC_EDIT_FULL_CIRCLE_POLICY_REQUIRED"]
    out = rebuild_arc_endpoints(point, point, g, config={"full_circle_policy": "preserve_endpoint_center_bearing"})
    certificate(out, point, point, 300)
    assert out["evidence"]["endpoint_separation_m"] == 0
    # An existing circle at this endpoint must not be treated as a new move.
    existing = rebuild_arc_endpoints(point, point, out["geometry"], config={"original_endpoints": [point, point]})
    assert existing["accepted"] and existing["evidence"]["selection"] == "unchanged_descriptor"
    assert existing["evidence"]["original_endpoint_reference_basis"] == "independently_bound_original_endpoints"


def test_actual_old_endpoint_reference_is_checked_not_trusted_as_unchanged():
    g = descriptor((0, 0), 1000, 10, 110)
    a, b = direct((0, 0), 10, 1000), direct((0, 0), 120, 1000)
    false_a = direct(a, 90, .01)
    out = rebuild_arc_endpoints(false_a, b, g, config={"original_endpoints": [false_a, b]})
    assert not out["accepted"] and out["geometry"] is None
    assert out["rejection_codes"] == ["ARC_EDIT_ORIGINAL_ENDPOINT_MISMATCH"]


@pytest.mark.parametrize("branch", ["minor", "major", "left", "right"])
def test_full_circle_open_branch_options_are_explicitly_rejected(branch):
    g = descriptor((0, 0), 1000, 0, 360)
    a, b = direct((0, 0), 0, 1000), direct((0, 0), 360, 1000)
    out = rebuild_arc_endpoints(a, b, g, config={"branch": branch})
    assert not out["accepted"] and out["geometry"] is None
    assert out["rejection_codes"] == ["ARC_EDIT_FULL_CIRCLE_BRANCH_UNSUPPORTED"]


def test_circle_centered_on_pole_true_length_matches_independent_parallel_metric():
    g = descriptor((14, 90), 1_000_000, 20, 250)
    a, b = direct((14, 90), 20, 1_000_000), direct((14, 90), 240, 1_000_000)
    out = rebuild_arc_endpoints(a, b, g)
    certificate(out, a, b, 1_000_000)
    phi = math.radians(a[1]); f = 1/298.257223563; e2 = f*(2-f)
    expected = 6378137*math.cos(phi)/math.sqrt(1-e2*math.sin(phi)**2)*math.radians(220)
    assert out["evidence"]["length_m"] == pytest.approx(expected, abs=3e-8)
    assert abs(out["evidence"]["length_m"]-1_000_000*math.radians(220)) > 1000


def test_polygonal_chords_converge_to_actual_rebuilt_arc_length():
    old = descriptor((10, 63), 600000, -20, -235)
    a, b = direct((12, 67), 35, 600000), direct((12, 67), -180, 600000)
    out = rebuild_arc_endpoints(a, b, old)
    certificate(out, a, b, 600000)
    g = out["geometry"]; errors = []
    for n in (256, 512, 1024):
        az = np.linspace(g["start_azimuth_deg"], g["start_azimuth_deg"]+g["sweep_deg"], n+1)
        x, y, _ = G.fwd(np.full(n+1, g["center"][0]), np.full(n+1, g["center"][1]), az, np.full(n+1, 600000))
        lengths = G.inv(x[:-1], y[:-1], x[1:], y[1:])[2]
        errors.append(out["evidence"]["length_m"]-float(np.sum(lengths)))
    assert all(x > 0 for x in errors)
    assert errors[0]/errors[1] == pytest.approx(4, rel=1e-4)
    assert errors[1]/errors[2] == pytest.approx(4, rel=1e-4)


@pytest.mark.parametrize("distance,code", [(2100, "ARC_EDIT_ENDPOINTS_TOO_FAR"),
    (2000-1e-9, "ARC_EDIT_INTERSECTION_UNRESOLVED"), (0, "ARC_EDIT_COINCIDENT_ENDPOINTS")])
def test_impossible_or_numerically_unresolved_endpoint_pairs_fail_closed(distance, code):
    g = descriptor()
    a = (0, 0); b = direct(a, 90, distance)
    out = rebuild_arc_endpoints(a, b, g, config={"branch": "minor"})
    assert not out["accepted"] and out["geometry"] is None
    assert out["rejection_codes"] == [code]


@pytest.mark.parametrize("cap", [1, 5, 20, 40, 60])
def test_budget_is_hard_actual_native_limit_including_final_primitive(cap):
    g = descriptor((179.99, 78), 900000, 25, -280)
    a, b = direct((179.99, 78), 25, 900000), direct((179.99, 78), -240, 900000)
    out = rebuild_arc_endpoints(a, b, g, config={"max_work_units": cap})
    assert not out["accepted"] and out["geometry"] is None
    assert out["rejection_codes"] == ["ARC_EDIT_WORK_LIMIT"]
    assert out["budget"]["work_units"] <= cap
    json.dumps(out, allow_nan=False)


def test_root_iteration_exhaustion_does_not_return_partial_center():
    g = descriptor()
    a, b = direct((0, 0), 0, 1000), direct((0, 0), 120, 1000)
    out = rebuild_arc_endpoints(a, b, g, config={"max_iterations": 1})
    assert not out["accepted"] and out["geometry"] is None
    assert out["rejection_codes"] == ["ARC_EDIT_ROOT_NOT_CONVERGED"]


@pytest.mark.parametrize("radius,margin", [(.001, 1e-5), (1000., .001), (1_000_000., .001)])
def test_near_diameter_but_resolved_edits_have_two_actual_branches(radius, margin):
    a = (20., 64.); b = direct(a, 37, 2*radius-margin)
    g = descriptor((10, 50), radius, 15, 60)
    low = rebuild_arc_endpoints(a, b, g, config={"branch": "minor"})
    high = rebuild_arc_endpoints(a, b, g, config={"branch": "major"})
    certificate(low, a, b, radius); certificate(high, a, b, radius)
    assert low["geometry"]["sweep_deg"] < 180 < high["geometry"]["sweep_deg"]
    assert G.inv(*low["geometry"]["center"], *high["geometry"]["center"])[2] > 0


def test_small_circle_absolute_tolerance_is_also_bounded_relatively():
    center = (30, 89.99); g = descriptor(center, .001, 20, -70)
    a, b = direct(center, 20, .001), direct(center, -65, .001)
    out = rebuild_arc_endpoints(a, b, g, config={"radius_tolerance_m": .001})
    certificate(out, a, b, .001)
    proof = out["evidence"]
    assert proof["requested_radius_tolerance_m"] == .001
    assert proof["effective_radius_tolerance_m"] == pytest.approx(1e-7)
    assert max(proof["endpoint_radius_errors_m"]+proof["end_binding_errors_m"]) <= proof["effective_radius_tolerance_m"]


def test_declared_native_count_matches_instrumented_actual_calls(monkeypatch):
    import oceanroute.arc_edit_geometry as edit
    import oceanroute.route_geometry as primitive
    calls = {"geod": 0, "reduced_length": 0}
    native_geod, native_gl = edit.GEOD, primitive._GL
    class GeodProxy:
        def fwd(self, *args, **kwargs):
            calls["geod"] += 1
            return native_geod.fwd(*args, **kwargs)
        def inv(self, *args, **kwargs):
            calls["geod"] += 1
            return native_geod.inv(*args, **kwargs)
    class ReducedProxy:
        def Direct(self, *args, **kwargs):
            calls["reduced_length"] += 1
            return native_gl.Direct(*args, **kwargs)
    monkeypatch.setattr(edit, "GEOD", GeodProxy())
    monkeypatch.setattr(primitive, "GEOD", GeodProxy())
    monkeypatch.setattr(primitive, "_GL", ReducedProxy())
    g = descriptor((178, 70), 900000, 13, -275)
    a, b = direct((178, 70), 13, 900000), direct((178, 70), -253, 900000)
    out = rebuild_arc_endpoints(a, b, g)
    assert out["accepted"], out
    assert out["budget"]["work_units"] == calls["geod"]+calls["reduced_length"]


@pytest.mark.parametrize("config", [{"unknown": 1}, {"branch": "nearest"}, {"full_circle_policy": "translate"},
    {"radius_m": 0}, {"radius_m": 1_000_001}, {"max_work_units": True}, {"max_iterations": 0},
    {"radius_tolerance_m": float("nan")}, {"radius_m": 10**1000}])
def test_invalid_config_rejected_before_computation(config):
    with pytest.raises(ValueError):
        rebuild_arc_endpoints((0, 0), (0, 1), descriptor(), config=config)


@pytest.mark.parametrize("field,value", [("schema_version", 1.), ("schema_version", True),
    ("sweep_deg", 0), ("radius_m", .0009), ("center", [0, 91]), ("start_azimuth_deg", float("inf"))])
def test_invalid_descriptor_is_not_silently_repaired(field, value):
    g = descriptor(); g[field] = value
    with pytest.raises(ValueError):
        rebuild_arc_endpoints((0, 0), (0, 1), g)
