"""Independent continuum/branch checks, not the calculator's root helpers.

The oracle integrates force and geometry in *natural material coordinates*.
All production interaction is through calculate_catenary or its real HTTP API.
See docs/CATENARY_INDEPENDENT_REVIEW.md for the stretched-arc peak proof.
"""
from copy import deepcopy
import json
import math

import numpy as np
import pytest
from scipy.integrate import quad, solve_ivp
from scipy.optimize import brentq, minimize_scalar

from oceanroute.catenary_calculator import calculate_catenary


def plane(depth=30., gradient=(0., 0.), *, vessel_z=0., x=(-80., 0., 80.), y=(-80., 0., 80.)):
    return {"schema": "oceanroute.bathymetry.v1", "x_m": list(x), "y_m": list(y),
            "z_m": [[vessel_z-depth+gradient[0]*a+gradient[1]*b for a in x] for b in y],
            "source": {"name": "independent declared synthetic affine bed",
                       "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0., 0.],
                       "vertical_datum": "explicitly aligned to model sea zero"}}


def settings(boundary, **extra):
    return {"seabed_grid": plane(), "wet_weight_n_m": 4., "ea_n": None,
            "nodes": 31, "heading_deg": 90., "boundary": boundary, **extra}


def _quadrature(function, length, horizontal, grade, weight):
    # Split at the tension minimum instead of trusting adaptive quadrature to
    # resolve an arbitrarily narrow bottom-turning interval on a negative slope.
    minimum = -grade*horizontal/weight
    points = [minimum] if 0 < minimum < length else None
    return quad(function, 0., length, points=points, epsabs=1e-10, epsrel=2e-12, limit=160)[0]


def continuum(horizontal, natural, grade, weight=4., ea=None):
    inverse = 0. if ea is None else 1/ea
    tension = lambda s: math.hypot(horizontal, grade*horizontal+weight*s)
    integrate = lambda f: _quadrature(f, natural, horizontal, grade, weight)
    x = integrate(lambda s: horizontal/tension(s)+inverse*horizontal)
    z = integrate(lambda s: (grade*horizontal+weight*s)/tension(s)
                              + inverse*(grade*horizontal+weight*s))
    arc = integrate(lambda s: 1+inverse*tension(s))
    rise = integrate(lambda s: weight*s/tension(s)+inverse*weight*s)
    return {"rise": rise, "x": x, "z": z, "arc": arc, "natural": natural,
            "top": tension(natural), "angle": math.degrees(math.atan2(grade*horizontal+weight*natural, horizontal))}


def natural_for_rise(horizontal, depth, grade, weight=4., ea=None):
    upper = depth+math.sqrt(depth*depth+2*depth*horizontal*math.hypot(1., grade)/weight)
    return brentq(lambda s: continuum(horizontal, s, grade, weight, ea)["rise"]-depth,
                  0., upper, xtol=1e-12, rtol=1e-14)


def natural_for_arc(horizontal, arc, grade, weight=4., ea=None):
    if ea is None:
        return arc
    return brentq(lambda s: continuum(horizontal, s, grade, weight, ea)["arc"]-arc,
                  0., arc, xtol=1e-12, rtol=1e-14)


def roots_for_length(depth, length, grade, ea, basis, brackets, weight=4.):
    def rise(horizontal):
        natural = length if basis == "natural" else natural_for_arc(horizontal, length, grade, weight, ea)
        return continuum(horizontal, natural, grade, weight, ea)["rise"]-depth
    return [brentq(rise, lo, hi, xtol=1e-11, rtol=1e-14) for lo, hi in brackets]


def check_ode_geometry(result, horizontal, natural, grade, weight, ea, heading=90.):
    """Integrate a separate first-order ODE; reconstruct every reported node."""
    inverse = 0. if ea is None else 1/ea
    def derivative(s, state):
        vertical = grade*horizontal+weight*s
        tension = math.hypot(horizontal, vertical)
        stretch = 1+inverse*tension
        return [stretch*horizontal/tension, stretch*vertical/tension, stretch]
    ode = solve_ivp(derivative, (0., natural), [0., 0., 0.], method="DOP853",
                    dense_output=True, rtol=2e-12, atol=2e-12)
    assert ode.success
    material = np.linspace(natural, 0., len(result["nodes"]))
    x, z, arc = ode.sol(material)
    direction = np.array([math.sin(math.radians(heading)), math.cos(math.radians(heading)), 0.])
    vessel = np.array(result["boundary"]["vessel_position_m"])
    touchdown = vessel-direction*x[0]
    touchdown[2] = vessel[2]-z[0]
    positions = touchdown+x[:, None]*direction
    positions[:, 2] += z
    vectors = horizontal*direction+np.zeros_like(positions)
    vectors[:, 2] = grade*horizontal+weight*material
    assert np.array(result["nodes"]) == pytest.approx(positions, abs=2e-8)
    assert result["node_material_m"] == pytest.approx(material, abs=1e-8)
    assert np.array(result["node_tension_vectors_n"]) == pytest.approx(vectors, abs=2e-8)
    assert result["node_tension_n"] == pytest.approx(np.linalg.norm(vectors, axis=1), abs=2e-8)
    assert result["summary"]["stretched_arc_length_m"] == pytest.approx(arc[0], abs=2e-8)
    assert result["summary"]["wet_weight_total_n"] == pytest.approx(weight*natural, abs=1e-8)
    forces = result["end_forces_on_cable_n"]
    assert forces["vessel"] == pytest.approx(vectors[0], abs=2e-8)
    assert forces["touchdown"] == pytest.approx(-vectors[-1], abs=2e-8)
    assert np.array(forces["vessel"])+np.array(forces["touchdown"]) == pytest.approx([0., 0., weight*natural], abs=2e-8)
    assert min(result["node_clearance_m"]) >= -1e-8


@pytest.mark.parametrize("boundary", [
    {"kind": "bottom_tension", "value_n": 34000.},
    {"kind": "top_tension", "value_n": 68000.},
    {"kind": "top_angle", "value_deg": 60., "reference": "horizontal", "direction": "touchdown_to_vessel"},
    {"kind": "cable_in_water", "value_m": 2000*math.sqrt(3), "length_basis": "natural"},
    {"kind": "cable_in_water", "value_m": 2000*math.sqrt(3), "length_basis": "stretched_arc"},
])
def test_public_manual_figure_four_boundaries_match_independent_flatbed_analytic_values(boundary):
    config = settings(boundary, seabed_grid=plane(depth=2000., x=(-10000., 0., 10000.)), wet_weight_n_m=17.)
    original = deepcopy(config)
    answer = calculate_catenary(config)
    assert config == original
    assert answer["accepted"] and answer["solver"]["root_enumeration_complete"]
    assert answer["summary"]["total_bottom_interval_root_count"] == 1
    result = answer["selected"]["result"]
    summary = result["summary"]
    assert summary["bottom_tension_n"] == pytest.approx(34000., abs=1e-7)
    assert summary["top_tension_n"] == pytest.approx(68000., abs=1e-7)
    assert summary["top_angle_from_horizontal_deg"] == pytest.approx(60., abs=1e-10)
    assert summary["natural_length_m"] == pytest.approx(2000*math.sqrt(3), abs=1e-7)
    assert summary["layback_m"] == pytest.approx(2000*math.acosh(2), abs=1e-7)
    check_ode_geometry(result, 34000., 2000*math.sqrt(3), 0., 17., None)
    json.dumps(answer, allow_nan=False)


@pytest.mark.parametrize("gradient,heading,ea", [
    ((.1, .05), 90., 1e4), ((-.25, .05), 90., 1e5), ((.1, .2), 0., None),
    ((.1, -.2), 0., 1e4), ((.2, -.1), 37., 1e6),
])
def test_inverse_angle_matches_independent_material_ode_at_every_node(gradient, heading, ea):
    direction = [math.sin(math.radians(heading)), math.cos(math.radians(heading))]
    grade = float(np.dot(gradient, direction))
    horizontal, depth = 100., 30.
    natural = natural_for_rise(horizontal, depth, grade, ea=ea)
    expected = continuum(horizontal, natural, grade, ea=ea)
    boundary = {"kind": "top_angle", "value_deg": expected["angle"],
                "reference": "horizontal", "direction": "touchdown_to_vessel"}
    answer = calculate_catenary(settings(boundary, seabed_grid=plane(gradient=gradient), heading_deg=heading, ea_n=ea))
    assert answer["accepted"]
    assert answer["selected"]["bottom_tension_n"] == pytest.approx(horizontal*math.hypot(1., grade), rel=2e-10)
    check_ode_geometry(answer["selected"]["result"], horizontal, natural, grade, 4., ea, heading)


def test_finite_ea_natural_and_stretched_arc_are_distinct_inverse_constraints():
    horizontal, depth, grade, ea = 100., 30., .1, 1000.
    natural = natural_for_rise(horizontal, depth, grade, ea=ea)
    expected = continuum(horizontal, natural, grade, ea=ea)
    assert expected["arc"] > natural*1.1
    answers = []
    for basis, length in [("natural", natural), ("stretched_arc", expected["arc"])]:
        answer = calculate_catenary(settings({"kind": "cable_in_water", "value_m": length, "length_basis": basis},
                                             seabed_grid=plane(gradient=(grade, 0.)), ea_n=ea))
        assert answer["accepted"]
        assert answer["selected"]["bottom_tension_n"] == pytest.approx(horizontal*math.hypot(1., grade), abs=1e-7)
        check_ode_geometry(answer["selected"]["result"], horizontal, natural, grade, 4., ea)
        answers.append(answer)
    # Same numeric length under the other explicit basis is a different cable;
    # a real inverse must not alias both choices to the same rest length.
    different = calculate_catenary(settings({"kind": "cable_in_water", "value_m": expected["arc"], "length_basis": "natural"},
                                           seabed_grid=plane(gradient=(grade, 0.)), ea_n=ea))
    assert different["accepted"]
    assert abs(different["selected"]["bottom_tension_n"]-answers[0]["selected"]["bottom_tension_n"]) > 20.


@pytest.mark.parametrize("grade,depth,length,ea,vessel_z,x,brackets", [
    (-.25, 100.07100226501906, 100., 1e5, 0., (-350., 0., 350.), ((.05, 2.), (3., 20.))),
    # Exact binary bed heights keep the declared maximum gradient 5 within the
    # unchanged bathymetry guard; this still exercises Q-before-W and two roots.
    (-5., 46.75, 28., 1e5, -100., (-25., 0., 25.), ((.5, 10.), (15., 40.))),
    # The proof does not assume small strain. This remains a research model
    # with an explicit linear-material warning, not a physical certification.
    (-1., 30., 28., 100., 0., (-29.99, 0., 100.), ((.1, 10.), (15., 50.))),
])
def test_stretched_arc_two_roots_for_both_derivative_zero_orders_match_quadrature(
        grade, depth, length, ea, vessel_z, x, brackets):
    expected = roots_for_length(depth, length, grade, ea, "stretched_arc", brackets)
    config = settings({"kind": "cable_in_water", "value_m": length, "length_basis": "stretched_arc"},
                      seabed_grid=plane(depth, (grade, 0.), vessel_z=vessel_z, x=x),
                      vessel_position_m=[0., 0., vessel_z], ea_n=ea)
    answer = calculate_catenary(config)
    assert answer["solver"]["root_enumeration_complete"]
    assert not answer["accepted"] and answer["selected"] is None
    assert answer["solver"]["stop_reason"] == "ambiguous_boundary"
    assert len(answer["candidates"]) == 2
    assert all(candidate["accepted"] for candidate in answer["candidates"])
    assert [c["bottom_tension_n"] for c in answer["candidates"]] == pytest.approx(
        sorted(h*math.hypot(1., grade) for h in expected), abs=1e-7)
    for candidate, horizontal in zip(answer["candidates"], sorted(expected)):
        natural = natural_for_arc(horizontal, length, grade, ea=ea)
        check_ode_geometry(candidate["result"], horizontal, natural, grade, 4., ea)
        assert candidate["result"]["summary"]["stretched_arc_length_m"] == pytest.approx(length, abs=1e-8)
        if ea == 100.:
            assert any(w["code"] == "LARGE_LINEAR_AXIAL_STRAIN" for w in candidate["result"]["warnings"])
    for policy, index in [("lowest_bottom_tension", 0), ("highest_bottom_tension", 1)]:
        selected = calculate_catenary({**config, "root_policy": policy})
        assert selected["accepted"]
        assert selected["selected"]["candidate_index"] == index
        assert selected["selected"]["bottom_tension_n"] == pytest.approx(answer["candidates"][index]["bottom_tension_n"])


def test_natural_length_downhill_two_roots_preserve_material_and_wet_weight():
    grade, ea, length, depth = -.25, 1e5, 100., 100.30
    expected = roots_for_length(depth, length, grade, ea, "natural", ((.1, 2.), (3., 10.)))
    answer = calculate_catenary(settings({"kind": "cable_in_water", "value_m": length, "length_basis": "natural"},
                                        seabed_grid=plane(depth, (grade, 0.), x=(-350., 0., 350.)), ea_n=ea))
    assert answer["solver"]["root_enumeration_complete"] and len(answer["candidates"]) == 2
    assert not answer["accepted"]
    for candidate, horizontal in zip(answer["candidates"], expected):
        assert candidate["bottom_tension_n"] == pytest.approx(horizontal*math.hypot(1., grade), abs=1e-8)
        check_ode_geometry(candidate["result"], horizontal, length, grade, 4., ea)
        assert candidate["natural_length_m"] == pytest.approx(100., abs=1e-8)


def test_signed_negative_top_angle_and_total_top_tension_do_not_erase_negative_vertical_branch():
    grade, depth, angle, vessel_z = -1., 30., -10., -500.
    ratio = math.tan(math.radians(angle))
    horizontal = brentq(lambda h: continuum(h, h*(ratio-grade)/4., grade)["rise"]-depth, 1., 2000.)
    natural = horizontal*(ratio-grade)/4.
    expected = continuum(horizontal, natural, grade)
    config = settings({"kind": "top_angle", "value_deg": angle, "reference": "horizontal", "direction": "touchdown_to_vessel"},
                      seabed_grid=plane(depth, (grade, 0.), vessel_z=vessel_z, x=(-529., 0., 100.)),
                      vessel_position_m=[0., 0., vessel_z])
    angle_answer = calculate_catenary(config)
    total_answer = calculate_catenary({**config, "boundary": {"kind": "top_tension", "value_n": expected["top"]}})
    for answer in (angle_answer, total_answer):
        assert answer["accepted"] and answer["summary"]["mathematical_root_count"] == 1
        result = answer["selected"]["result"]
        assert result["summary"]["top_vertical_tension_n"] < 0
        assert result["summary"]["top_angle_from_horizontal_deg"] == pytest.approx(angle, abs=1e-10)
        check_ode_geometry(result, horizontal, natural, grade, 4., None)


def test_top_total_tension_two_roots_are_not_confused_with_horizontal_component():
    grade, target = -1., 90.
    def height(r):
        h = target/math.hypot(1., r)
        return continuum(h, h*(r-grade)/4., grade)["rise"]-30.
    ratios = [brentq(height, 1., 2.), brentq(height, 4., 8.)]
    expected = sorted(target/math.hypot(1., r)*math.hypot(1., grade) for r in ratios)
    answer = calculate_catenary(settings({"kind": "top_tension", "value_n": target},
                                        seabed_grid=plane(gradient=(grade, 0.), x=(-29.99, 0., 100.))))
    assert answer["solver"]["root_enumeration_complete"] and len(answer["candidates"]) == 2
    assert [c["bottom_tension_n"] for c in answer["candidates"]] == pytest.approx(expected, abs=1e-8)
    assert all(c["accepted"] for c in answer["candidates"])
    assert not answer["accepted"] and answer["selected"] is None
    for candidate in answer["candidates"]:
        assert candidate["result"]["summary"]["top_tension_n"] == pytest.approx(target, abs=1e-8)
        assert candidate["result"]["summary"]["horizontal_tension_n"] != pytest.approx(target, rel=.01)


def _stretched_peak(length=28., grade=-1., ea=1e4):
    def height(log_h):
        h = math.exp(log_h)
        natural = natural_for_arc(h, length, grade, ea=ea)
        return continuum(h, natural, grade, ea=ea)["rise"]
    maximum = minimize_scalar(lambda log_h: -height(log_h), bounds=(math.log(1.), math.log(80.)),
                              method="bounded", options={"xatol": 1e-13})
    return math.exp(maximum.x), -maximum.fun


def test_near_peak_distinct_roots_are_not_merged_and_peak_rounding_is_not_certified():
    length, grade, ea = 28., -1., 1e4
    horizontal_peak, peak_depth = _stretched_peak(length, grade, ea)
    boundary = {"kind": "cable_in_water", "value_m": length, "length_basis": "stretched_arc"}
    below = peak_depth-1e-9
    expected = roots_for_length(below, length, grade, ea, "stretched_arc",
                                ((horizontal_peak*.99, horizontal_peak), (horizontal_peak, horizontal_peak*1.01)))
    assert 0 < expected[1]-expected[0] < .002
    config = settings(boundary, ea_n=ea, seabed_grid=plane(below, (grade, 0.), x=(-31., 0., 80.)))
    answer = calculate_catenary(config)
    assert answer["solver"]["root_enumeration_complete"] and len(answer["candidates"]) == 2
    assert not answer["accepted"]
    assert [c["bottom_tension_n"] for c in answer["candidates"]] == pytest.approx(
        [h*math.hypot(1., grade) for h in expected], abs=3e-6)
    # A decimal target rounded at the fold cannot certify multiplicity. The
    # conservative supported response is incomplete, not a fabricated root.
    at_peak = calculate_catenary({**config, "seabed_grid": plane(peak_depth, (grade, 0.), x=(-31., 0., 80.))})
    assert not at_peak["accepted"] and at_peak["selected"] is None
    assert not at_peak["solver"]["root_enumeration_complete"]
    assert "peak" in at_peak["solver"]["failure"]
    beyond = calculate_catenary({**config, "seabed_grid": plane(peak_depth+1e-6, (grade, 0.), x=(-31., 0., 80.))})
    assert beyond["solver"]["root_enumeration_complete"] and beyond["candidates"] == []


@pytest.mark.parametrize("basis,ea,length", [("natural", None, 30.), ("stretched_arc", 1e4, 30.)])
def test_flatbed_zero_bottom_tension_asymptote_is_not_a_finite_root(basis, ea, length):
    answer = calculate_catenary(settings({"kind": "cable_in_water", "value_m": length, "length_basis": basis}, ea_n=ea))
    assert answer["solver"]["root_enumeration_complete"]
    assert not answer["accepted"] and answer["selected"] is None and answer["candidates"] == []
    assert answer["solver"]["bottom_tension_domain_n"] == [1e-6, 1e9]


@pytest.mark.parametrize("basis,ea", [("natural", None), ("stretched_arc", 100.)])
def test_downhill_height_equal_to_length_retains_only_finite_branch_not_asymptotic_root(basis, ea):
    length, grade = 28., -1.
    expected = roots_for_length(length, length, grade, ea, basis, ((20., 100.),))[0]
    answer = calculate_catenary(settings({"kind": "cable_in_water", "value_m": length, "length_basis": basis},
                                        ea_n=ea, seabed_grid=plane(length, (grade, 0.), x=(-27.99, 0., 100.))))
    assert answer["accepted"] and answer["solver"]["root_enumeration_complete"]
    assert answer["summary"]["total_bottom_interval_root_count"] == 1
    assert answer["selected"]["bottom_tension_n"] == pytest.approx(expected*math.hypot(1., grade), abs=1e-8)
    assert answer["selected"]["bottom_tension_n"] > 40.


def test_original_grid_rejects_high_root_without_silently_selecting_valid_low_root():
    grade, depth, length = -1., 30., 28.
    expected = roots_for_length(depth, length, grade, None, "natural", ((1., 10.), (20., 60.)))
    config = settings({"kind": "cable_in_water", "value_m": length, "length_basis": "natural"},
                      seabed_grid=plane(depth, (grade, 0.), x=(-10., 0., 20.)), root_policy="highest_bottom_tension")
    original = deepcopy(config)
    answer = calculate_catenary(config)
    assert config == original
    assert answer["solver"]["root_enumeration_complete"]
    assert answer["summary"]["mathematical_root_count"] == 2
    assert answer["summary"]["usable_root_count"] == 1
    assert not answer["accepted"] and answer["selected"] is None
    assert answer["solver"]["stop_reason"] == "requested_root_failed_verification"
    low, high = answer["candidates"]
    assert [low["bottom_tension_n"], high["bottom_tension_n"]] == pytest.approx(
        [h*math.hypot(1., grade) for h in expected], abs=1e-8)
    assert low["accepted"] and not high["accepted"] and high["result"] is None
    assert high["rejection_codes"] == ["ORIGINAL_GRID_COVERAGE"]
    valid = calculate_catenary({**config, "root_policy": "lowest_bottom_tension"})
    assert valid["accepted"]
    assert valid["selected"]["candidate_index"] == 0


def test_natural_material_limit_keeps_explicit_excluded_math_root_and_fixed_length_accounting():
    # Finite-EA top-tension roots have different natural lengths. Limit excludes
    # the larger material requirement rather than altering it to meet inventory.
    grade, target, ea, depth = -1., 90., 1e4, 30.
    config = settings({"kind": "top_tension", "value_n": target}, ea_n=ea,
                      seabed_grid=plane(depth, (grade, 0.), x=(-29.99, 0., 100.)), root_policy="enumerate")
    full = calculate_catenary(config)
    assert len(full["candidates"]) == 2
    lengths = sorted(c["theory"]["natural_length_m"] for c in full["candidates"])
    limit = sum(lengths)/2
    bounded = calculate_catenary({**config, "max_natural_length_m": limit, "root_policy": "require_unique"})
    assert bounded["accepted"] and len(bounded["candidates"]) == 2
    assert bounded["summary"]["excluded_natural_length_count"] == 1
    excluded = [c for c in bounded["candidates"] if not c["within_declared_domain"]][0]
    assert excluded["theory"]["natural_length_m"] > limit
    assert excluded["rejection_codes"] == ["DECLARED_DOMAIN_EXCLUSION"]
    assert excluded["result"] is None
    assert bounded["selected"]["result"]["summary"]["natural_length_m"] < limit


def test_root_budget_exhaustion_is_incomplete_and_does_not_return_a_partial_selection():
    answer = calculate_catenary(settings({"kind": "cable_in_water", "value_m": 28., "length_basis": "stretched_arc"},
                                        seabed_grid=plane(gradient=(-1., 0.), x=(-29.99, 0., 100.)), ea_n=1e4,
                                        max_function_evaluations=1, root_policy="lowest_bottom_tension"))
    assert not answer["accepted"] and answer["selected"] is None
    assert not answer["solver"]["root_enumeration_complete"]
    assert answer["solver"]["inverse_function_evaluations"] == 1
    assert "budget" in answer["solver"]["failure"]
    assert answer["solver"]["charged_work_units"] <= answer["solver"]["max_work_units"]
    json.dumps(answer, allow_nan=False)


@pytest.fixture
def http(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    with TestClient(create_app(ProjectStore(tmp_path/"independent-catenary.sqlite3"))) as client:
        yield client


def test_real_http_stretched_arc_preview_matches_quadrature_and_is_not_a_dynamic_initial_state(http):
    horizontal, grade, depth, ea = 100., .1, 30., 1e4
    natural = natural_for_rise(horizontal, depth, grade, ea=ea)
    arc = continuum(horizontal, natural, grade, ea=ea)["arc"]
    config = settings({"kind": "cable_in_water", "value_m": arc, "length_basis": "stretched_arc"},
                      seabed_grid=plane(gradient=(grade, 0.)), ea_n=ea)
    response = http.post("/api/simulation/catenary-calculator", json={"config": config})
    assert response.status_code == 200
    answer = response.json()
    assert answer["accepted"]
    assert "checkpoint" not in answer and "project" not in answer
    assert answer["selected"]["result"]["summary"]["bottom_tension_n"] == pytest.approx(horizontal*math.hypot(1., grade), abs=1e-7)
    check_ode_geometry(answer["selected"]["result"], horizontal, natural, grade, 4., ea)
    assert all(w["code"] != "CATENARY_ENUMERATION_INCOMPLETE" for w in answer["warnings"])


@pytest.mark.parametrize("change", [
    {"max_work_units": 1}, {"max_output_bytes": 1},
    {"boundary": {"kind": "top_angle", "value_deg": 90., "reference": "horizontal", "direction": "touchdown_to_vessel"}},
    {"boundary": {"kind": "cable_in_water", "value_m": 40.}},
    {"bottom_tension_n": 100.}, {"current_x_m_s": .1},
])
def test_real_http_invalid_or_over_budget_input_rejects_without_old_or_partial_geometry(http, change):
    config = settings({"kind": "bottom_tension", "value_n": 100.})
    config.update(change)
    response = http.post("/api/simulation/catenary-calculator", json={"config": config})
    assert response.status_code == 422
    assert "selected" not in response.json() and "candidates" not in response.json()


def test_real_http_nodata_and_nonaffine_grid_are_not_fitted_or_filled(http):
    for value in (None, -29.9):
        config = settings({"kind": "bottom_tension", "value_n": 100.})
        config["seabed_grid"]["z_m"][1][1] = value
        response = http.post("/api/simulation/catenary-calculator", json={"config": config})
        assert response.status_code == 422
        assert "selected" not in response.json()
