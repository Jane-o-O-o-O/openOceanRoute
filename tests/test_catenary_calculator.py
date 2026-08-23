"""Four actual inverse boundaries, independent integral/force checks and guards."""
from copy import deepcopy
import json
import math

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.optimize import brentq

from oceanroute.catenary_calculator import calculate_catenary


def plane(grade=0., depth=30., extent=80., cross=0.):
    axes = [-extent, 0., extent]
    return {"schema": "oceanroute.bathymetry.v1", "x_m": axes, "y_m": axes,
            "z_m": [[-depth+grade*x+cross*y for x in axes] for y in axes],
            "source": {"name": "explicit synthetic calculator plane",
                       "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0, 0],
                       "vertical_datum": "already aligned to model sea z=0"}}


def config(boundary=None, **kwargs):
    return {"seabed_grid": plane(), "wet_weight_n_m": 4., "ea_n": None, "nodes": 41,
            "boundary": boundary or {"kind": "bottom_tension", "value_n": 100.}, **kwargs}


def downhill(boundary, **kwargs):
    return config(boundary, seabed_grid=kwargs.pop("seabed_grid", plane(-1, extent=29)), **kwargs)


def finite(result):
    json.dumps(result, allow_nan=False)
    assert result["solver"]["charged_work_units"] <= result["solver"]["max_work_units"]


@pytest.mark.parametrize("boundary", [
    {"kind": "bottom_tension", "value_n": 34000.},
    {"kind": "top_tension", "value_n": 68000.},
    {"kind": "top_angle", "value_deg": 60., "reference": "horizontal", "direction": "touchdown_to_vessel"},
    {"kind": "cable_in_water", "value_m": math.sqrt(12e6), "length_basis": "natural"},
    {"kind": "cable_in_water", "value_m": math.sqrt(12e6), "length_basis": "stretched_arc"},
])
def test_public_manual_flat_example_all_four_boundaries_are_consistent(boundary):
    r = calculate_catenary(config(boundary, seabed_grid=plane(depth=2000., extent=5000.), wet_weight_n_m=17.))
    assert r["accepted"] and r["solver"]["root_enumeration_complete"]
    assert r["summary"]["mathematical_root_count"] == 1
    s = r["selected"]["result"]["summary"]
    assert s["bottom_tension_n"] == pytest.approx(34000., abs=1e-7)
    assert s["top_tension_n"] == pytest.approx(68000., abs=1e-7)
    assert s["top_angle_from_horizontal_deg"] == pytest.approx(60., abs=1e-10)
    assert s["natural_length_m"] == pytest.approx(math.sqrt(12e6), abs=1e-8)
    assert s["stretched_arc_length_m"] == pytest.approx(s["natural_length_m"])
    assert s["layback_m"] == pytest.approx(2000*math.acosh(2), abs=1e-8)
    finite(r)


@pytest.mark.parametrize("ea", [None, 1e4, 1e5])
@pytest.mark.parametrize("grade", [0., .1, -.2])
def test_inverse_round_trip_uses_independent_continuous_ode_not_sample_chords(ea, grade):
    seed = config(seabed_grid=plane(grade), ea_n=ea)
    first = calculate_catenary(seed)
    assert first["accepted"]
    original = first["selected"]["result"]
    summary = original["summary"]
    length, h, w = summary["natural_length_m"], 100/math.hypot(1, grade), 4.
    inv = 0. if ea is None else 1/ea
    independent_arc = quad(lambda s: 1+math.hypot(h, grade*h+w*s)*inv, 0, length,
                           epsabs=1e-10, epsrel=1e-12)[0]
    assert independent_arc == pytest.approx(summary["stretched_arc_length_m"], abs=1e-8)
    for boundary in [
        {"kind": "top_tension", "value_n": summary["top_tension_n"]},
        {"kind": "top_angle", "value_deg": summary["top_angle_from_horizontal_deg"], "reference": "horizontal", "direction": "touchdown_to_vessel"},
        {"kind": "cable_in_water", "value_m": length, "length_basis": "natural"},
        {"kind": "cable_in_water", "value_m": independent_arc, "length_basis": "stretched_arc"},
    ]:
        solved = calculate_catenary({**seed, "boundary": boundary, "root_policy": "highest_bottom_tension"})
        assert solved["accepted"], solved["solver"]
        result = solved["selected"]["result"]
        assert result["summary"]["bottom_tension_n"] == pytest.approx(100, rel=1e-9)
        for s, node in zip(result["node_material_m"], result["nodes"]):
            x = quad(lambda t: h/math.hypot(h, grade*h+w*t)+h*inv, 0, s)[0]
            z = quad(lambda t: (grade*h+w*t)/math.hypot(h, grade*h+w*t)+(grade*h+w*t)*inv, 0, s)[0]
            touchdown = result["boundary"]["touchdown_position_m"]
            assert node == pytest.approx([touchdown[0]+x, touchdown[1], touchdown[2]+z], abs=2e-8)
        forces = result["end_forces_on_cable_n"]
        assert np.array(forces["vessel"])+forces["touchdown"]+[0, 0, -w*result["summary"]["natural_length_m"]] == pytest.approx([0, 0, 0], abs=1e-8)
        finite(solved)


@pytest.mark.parametrize("basis", ["natural", "stretched_arc"])
def test_elastic_lengths_are_different_boundaries_and_not_a_chord_length_alias(basis):
    c = config(seabed_grid=plane(.1), ea_n=1e4, nodes=3)
    seed = calculate_catenary(c)["selected"]["result"]["summary"]
    assert seed["stretched_arc_length_m"] > seed["natural_length_m"]+.5
    key = "natural_length_m" if basis == "natural" else "stretched_arc_length_m"
    value = seed[key]
    right = calculate_catenary({**c, "boundary": {"kind": "cable_in_water", "value_m": value, "length_basis": basis}})
    wrong = calculate_catenary({**c, "boundary": {"kind": "cable_in_water", "value_m": value, "length_basis": "stretched_arc" if basis == "natural" else "natural"}})
    assert right["accepted"] and wrong["accepted"]
    assert right["selected"]["bottom_tension_n"] == pytest.approx(100, abs=1e-7)
    assert abs(wrong["selected"]["bottom_tension_n"]-100) > 2


@pytest.mark.parametrize("boundary,expected", [
    ({"kind": "cable_in_water", "value_m": 28., "length_basis": "natural"}, [4.00397024439729, 48.57869286704871]),
    ({"kind": "top_tension", "value_n": 90.}, [22.50902534224186, 77.48444300382442]),
])
def test_downhill_two_real_roots_do_not_get_implicitly_selected(boundary, expected):
    c = downhill(boundary)
    original = deepcopy(c)
    r = calculate_catenary(c)
    assert not r["accepted"] and r["selected"] is None
    assert r["solver"]["root_enumeration_complete"]
    assert r["summary"]["mathematical_root_count"] == r["summary"]["usable_root_count"] == 2
    assert [a["bottom_tension_n"] for a in r["candidates"]] == pytest.approx(expected, rel=1e-10)
    for policy, index in [("enumerate", None), ("lowest_bottom_tension", 0), ("highest_bottom_tension", 1)]:
        selected = calculate_catenary({**c, "root_policy": policy})
        assert selected["accepted"] == (index is not None)
        if index is not None:
            assert selected["selected"]["candidate_index"] == index
            assert selected["selected"]["bottom_tension_n"] == pytest.approx(expected[index])
        finite(selected)
    assert c == original


@pytest.mark.parametrize("ea", [100., 1000., 1e4, 1e5])
def test_stretched_arc_on_downhill_enumerates_both_true_elastic_roots(ea):
    r = calculate_catenary(downhill({"kind": "cable_in_water", "value_m": 28., "length_basis": "stretched_arc"}, ea_n=ea))
    assert r["solver"]["root_enumeration_complete"]
    assert r["summary"]["mathematical_root_count"] == 2
    assert r["selected"] is None and not r["accepted"]
    for candidate in r["candidates"]:
        assert candidate["accepted"]
        s = candidate["theory"]["natural_length_m"]
        h = candidate["bottom_tension_n"]/math.sqrt(2)
        actual_arc = quad(lambda x: 1+math.hypot(h, -h+4*x)/ea, 0, s)[0]
        rise = quad(lambda x: 4*x/math.hypot(h, -h+4*x)+4*x/ea, 0, s)[0]
        assert actual_arc == pytest.approx(28., abs=1e-8)
        assert rise == pytest.approx(30., abs=1e-8)
        assert s < 28
    finite(r)


def test_original_grid_coverage_rejects_requested_high_root_without_low_root_fallback():
    c = downhill({"kind": "cable_in_water", "value_m": 28., "length_basis": "natural"}, seabed_grid=plane(-1, extent=10))
    default = calculate_catenary(c)
    assert default["summary"]["mathematical_root_count"] == 2
    assert default["summary"]["usable_root_count"] == 1
    assert not default["accepted"] and default["selected"] is None
    rejected = calculate_catenary({**c, "root_policy": "highest_bottom_tension"})
    assert not rejected["accepted"] and rejected["selected"] is None
    assert rejected["candidates"][1]["rejection_codes"] == ["ORIGINAL_GRID_COVERAGE"]
    assert rejected["candidates"][1]["result"] is None
    assert rejected["candidates"][1]["theory"]["top_tension_n"] > 0
    accepted = calculate_catenary({**c, "root_policy": "lowest_bottom_tension"})
    assert accepted["accepted"]
    finite(rejected)


def test_natural_length_exclusions_are_reported_without_hiding_bottom_interval_roots():
    r = calculate_catenary(downhill({"kind": "top_tension", "value_n": 90.}, max_natural_length_m=28.))
    assert r["summary"]["total_bottom_interval_root_count"] == 2
    assert r["summary"]["mathematical_root_count"] == 1
    assert r["summary"]["excluded_natural_length_count"] == 1
    assert r["accepted"]
    assert not r["candidates"][1]["within_declared_domain"]
    r = calculate_catenary(config(max_natural_length_m=1.))
    assert not r["accepted"]
    assert r["summary"]["total_bottom_interval_root_count"] == 1
    assert r["summary"]["mathematical_root_count"] == 0


@pytest.mark.parametrize("boundary", [
    {"kind": "top_tension", "value_n": 119.},
    {"kind": "cable_in_water", "value_m": 29., "length_basis": "natural"},
    {"kind": "cable_in_water", "value_m": 29., "length_basis": "stretched_arc"},
    {"kind": "top_angle", "value_deg": 1e-300, "reference": "horizontal", "direction": "touchdown_to_vessel"},
])
def test_valid_but_unreachable_boundaries_have_no_success_shape(boundary):
    r = calculate_catenary(config(boundary))
    assert not r["accepted"] and r["selected"] is None
    assert r["solver"]["root_enumeration_complete"]
    assert r["summary"]["mathematical_root_count"] == 0
    finite(r)


@pytest.mark.parametrize("parameter,value", [("max_root_iterations", 1), ("max_function_evaluations", 1)])
def test_actual_numeric_budget_failure_never_selects_a_partial_root(parameter, value):
    r = calculate_catenary(downhill({"kind": "cable_in_water", "value_m": 28., "length_basis": "stretched_arc"}, ea_n=100., **{parameter: value}))
    assert not r["accepted"] and r["selected"] is None
    assert not r["solver"]["root_enumeration_complete"]
    assert r["solver"]["failure"]
    finite(r)


@pytest.mark.parametrize("extra", [{"bottom_tension_n": 100}, {"depth_m": 30}, {"current_x_m_s": .1}, {"boundary2": {}}])
def test_unknown_and_mixed_top_level_inputs_reject(extra):
    with pytest.raises(ValueError):
        calculate_catenary(config(**extra))


@pytest.mark.parametrize("boundary", [None, [], {}, {"kind": "bottom_tension"},
    {"kind": "bottom_tension", "value_n": 100, "value_m": 50},
    {"kind": "top_tension", "value_n": float("nan")},
    {"kind": "top_tension", "value_n": float("inf")},
    {"kind": "bottom_tension", "value_n": True},
    pytest.param({"kind": "bottom_tension", "value_n": 10**1000}, id="huge-integer"),
    {"kind": "cable_in_water", "value_m": 30},
    {"kind": "cable_in_water", "value_m": 30, "length_basis": "geometric_chord"},
    {"kind": "top_angle", "value_deg": 90, "reference": "horizontal", "direction": "touchdown_to_vessel"},
    {"kind": "top_angle", "value_deg": -1, "reference": "horizontal", "direction": "touchdown_to_vessel"},
    {"kind": "top_angle", "value_deg": 60, "reference": "vertical", "direction": "touchdown_to_vessel"},
])
def test_invalid_or_ambiguous_boundary_contract_rejects(boundary):
    c = config(); c["boundary"] = boundary
    with pytest.raises(ValueError):
        calculate_catenary(c)


@pytest.mark.parametrize("key,value", [("max_work_units", 1), ("max_output_bytes", 1),
                                      ("root_policy", "first_found"), ("ea_n", float("inf")),
                                      ("max_function_evaluations", 0)])
def test_declared_compute_output_and_physical_limits_are_preflight_errors(key, value):
    with pytest.raises(ValueError):
        calculate_catenary(config(**{key: value}))


def test_nonaffine_and_missing_seabed_do_not_get_projected_into_a_solution():
    broken = plane(); broken["z_m"][1][1] += .01
    with pytest.raises(ValueError, match="affine"):
        calculate_catenary(config(seabed_grid=broken))
    broken = plane(); broken["z_m"][0][0] = None
    with pytest.raises(ValueError, match="NoData"):
        calculate_catenary(config(seabed_grid=broken))


def test_global_rotation_preserves_all_roots_and_rotates_selected_shape_and_end_forces():
    c = config(seabed_grid=plane(.1, cross=.05), ea_n=1e5)
    a = calculate_catenary(c)
    rotated = plane(-.05, cross=.1)
    b = calculate_catenary({**c, "seabed_grid": rotated, "heading_deg": 0.})
    assert a["accepted"] and b["accepted"]
    pa, pb = a["selected"]["result"], b["selected"]["result"]
    transform = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    assert np.array(pa["nodes"])@transform.T == pytest.approx(np.array(pb["nodes"]), abs=1e-9)
    assert np.array(pa["end_forces_on_cable_n"]["vessel"])@transform.T == pytest.approx(pb["end_forces_on_cable_n"]["vessel"], abs=1e-9)
    assert a["selected"]["bottom_tension_n"] == pytest.approx(b["selected"]["bottom_tension_n"])


def test_near_downhill_peak_keeps_distinct_roots_or_declares_numeric_ambiguity():
    # Independent derivative/integrals establish the true peak, without using
    # any calculator root/shape helper. A rounded exact peak is not certification.
    length, m = 28., -1.
    def f(r):
        return quad(lambda v: (v-m)/math.hypot(1, v), m, r, epsabs=1e-11)[0]
    peak = brentq(lambda r: (r-m)**2/math.hypot(1, r)-f(r), 2., 30., xtol=1e-13)
    maximum_depth = length*f(peak)/(peak-m)
    boundary = {"kind": "cable_in_water", "value_m": length, "length_basis": "natural"}
    near = calculate_catenary(config(boundary, seabed_grid=plane(-1, depth=maximum_depth-1e-9, extent=29)))
    assert near["solver"]["root_enumeration_complete"]
    assert near["summary"]["mathematical_root_count"] == 2
    assert near["candidates"][1]["bottom_tension_n"] > near["candidates"][0]["bottom_tension_n"]
    at_peak = calculate_catenary(config(boundary, seabed_grid=plane(-1, depth=maximum_depth, extent=29)))
    assert not at_peak["accepted"] and at_peak["selected"] is None
    assert not at_peak["solver"]["root_enumeration_complete"]
    assert "peak" in at_peak["solver"]["failure"]
    assert at_peak["solver"]["failure_diagnostic"]["possible_root_counts"] == [0, 1, 2]
    finite(at_peak)


def test_negative_top_angle_is_valid_for_a_submerged_vessel_and_top_total_inverse_keeps_it():
    grid = plane(-1., depth=530., extent=400.)
    angle = {"kind": "top_angle", "value_deg": -10., "reference": "horizontal", "direction": "touchdown_to_vessel"}
    c = config(angle, seabed_grid=grid, vessel_position_m=[0., 0., -500.])
    r = calculate_catenary(c)
    assert r["accepted"]
    selected = r["selected"]["result"]
    assert selected["summary"]["top_vertical_tension_n"] < 0
    inverted = calculate_catenary({**c, "boundary": {"kind": "top_tension", "value_n": selected["summary"]["top_tension_n"]}})
    assert inverted["accepted"]
    assert inverted["selected"]["bottom_tension_n"] == pytest.approx(r["selected"]["bottom_tension_n"], rel=1e-9)
    assert inverted["selected"]["result"]["summary"]["top_angle_from_horizontal_deg"] == pytest.approx(-10., abs=1e-8)


@pytest.fixture
def http_client(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    with TestClient(create_app(ProjectStore(tmp_path/"calculator.db"))) as client:
        yield client


@pytest.mark.parametrize("boundary", [
    {"kind": "bottom_tension", "value_n": 100.},
    {"kind": "top_tension", "value_n": 220.},
    {"kind": "top_angle", "value_deg": math.degrees(math.atan(math.sqrt(2400)/25)), "reference": "horizontal", "direction": "touchdown_to_vessel"},
    {"kind": "cable_in_water", "value_m": math.sqrt(2400), "length_basis": "natural"},
    {"kind": "cable_in_water", "value_m": math.sqrt(2400), "length_basis": "stretched_arc"},
])
def test_actual_http_all_four_boundary_successes_are_finite(http_client, boundary):
    response = http_client.post("/api/simulation/catenary-calculator", json={"config": config(boundary)})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"]
    assert result["selected"]["bottom_tension_n"] == pytest.approx(100., abs=1e-7)
    finite(result)


@pytest.mark.parametrize("change", [
    {"boundary": {"kind": "top_angle", "value_deg": 1e-300, "reference": "horizontal", "direction": "touchdown_to_vessel"}},
    {"boundary": {"kind": "top_angle", "value_deg": 89.9999999999, "reference": "horizontal", "direction": "touchdown_to_vessel"}},
    {"boundary": {"kind": "top_tension", "value_n": 1e12}},
    pytest.param({"boundary": {"kind": "bottom_tension", "value_n": 10**1000}}, id="huge-int-json"),
    {"boundary": {"kind": "cable_in_water", "value_m": .001, "length_basis": "stretched_arc"}},
    {"max_work_units": 1}, {"max_output_bytes": 1}, {"max_root_iterations": 1},
    {"boundary": {"kind": "top_tension", "value_n": float("nan")}},
])
def test_actual_http_extreme_numeric_and_budget_inputs_never_return_500(http_client, change):
    payload = {"config": config(**change)}
    response = http_client.post("/api/simulation/catenary-calculator", content=json.dumps(payload),
                                headers={"Content-Type": "application/json"})
    assert response.status_code in (200, 422), response.text
    result = response.json()
    json.dumps(result, allow_nan=False)
    if response.status_code == 200:
        assert not result["accepted"] and result["selected"] is None
