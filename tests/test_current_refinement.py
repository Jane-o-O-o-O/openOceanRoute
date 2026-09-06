"""Independent continuous/traction refinement: actual nonzero-flow startup."""
from copy import deepcopy
import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1]/"scripts/benchmark_current_refinement.py"
spec = importlib.util.spec_from_file_location("independent_current_refinement", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


@pytest.mark.parametrize("boundary,point,weight", [(8., 14., 30.), (8., 14., -30.),
                                                (8.3, 14.7, 30.), (8.3, 14.7, -30.)])
def test_piecewise_zero_drag_ode_matches_independent_closed_elastic_integrals(boundary, point, weight):
    declared = benchmark.declarations(boundary, point, weight)
    declared["current"] = np.zeros(3)
    actual = benchmark.continuous_reference(declared)
    # This expectation is an analytic integration of a vertical force affine
    # in natural coordinate, independent of the numerical ODE under test.
    fx, fy, vertical = declared["bottom"]
    horizontal = math.hypot(fx, fy)
    displacement = np.zeros(3)
    material_cut = declared["rows"][0]["end_m"]-declared["origin"]
    body_cut = declared["body"]["material_m"]-declared["origin"]
    cuts = sorted({0., material_cut, body_cut, declared["length"]})
    for start, end in zip(cuts[:-1], cuts[1:]):
        if start == body_cut:
            vertical += weight
        w, ea = (4., 10000.) if (start+end)/2 < material_cut else (7., 24000.)
        later = vertical+w*(end-start)
        factor = (math.asinh(later/horizontal)-math.asinh(vertical/horizontal))/w+(end-start)/ea
        displacement[:2] += [fx*factor, fy*factor]
        displacement[2] += ((math.hypot(horizontal, later)-math.hypot(horizontal, vertical))/w+
                            (vertical+later)*(end-start)/(2*ea))
        vertical = later
    assert actual["displacement"] == pytest.approx(displacement, abs=1e-9)
    assert actual["top_traction"] == pytest.approx([fx, fy, vertical], abs=1e-9)


@pytest.mark.parametrize("weight", [30., -30.])
def test_continuous_signed_point_has_real_drag_traction_jump_without_position_jump(weight):
    declared = benchmark.declarations(8.3, 14.7, weight)
    actual = benchmark.continuous_reference(declared)
    assert len(actual["point_jumps"]) == 1
    jump = actual["point_jumps"][0]
    before, after = np.array(jump["before"]), np.array(jump["after"])
    fluid = declared["current"]
    drag = declared["rho"]/2*1.2*.02*np.linalg.norm(fluid)*fluid
    assert after[:3] == pytest.approx(before[:3], abs=0)
    assert after[3:]-before[3:] == pytest.approx(np.array([0., 0., weight])-drag, abs=1e-12)
    assert np.linalg.norm(drag[:2]) > 2., "point reference must contain real nonzero-flow body drag"
    assert np.linalg.norm(before[3:]/np.linalg.norm(before[3:])-after[3:]/np.linalg.norm(after[3:])) > .05


def test_continuous_ode_tolerance_refinement_is_smaller_than_mesh_endpoint_error():
    declared = benchmark.declarations(8.3, 14.7, -30.)
    nominal = benchmark.continuous_reference(declared)
    tighter = benchmark.continuous_reference(declared, relative_tolerance=1e-12, absolute_tolerance=1e-13)
    assert nominal["displacement"] == pytest.approx(tighter["displacement"], abs=1e-9)
    assert nominal["top_traction"] == pytest.approx(tighter["top_traction"], abs=1e-8)


@pytest.mark.parametrize("elements", [12, 24, 48])
def test_discrete_common_anchor_traction_includes_anchor_half_gravity_and_drag(elements):
    declared = benchmark.declarations(8.3, 14.7, -30.)
    discrete = benchmark.discrete_reference(elements, declared)
    p, loads, traction = discrete["positions"], discrete["loads"], discrete["traction"]
    drag = benchmark.direct_drag(p, declared, loads)
    external = drag.copy()
    external[:, 2] -= loads["weight"]
    assert -traction[-1]-external[-1] == pytest.approx(-declared["bottom"], abs=1e-8)
    assert np.linalg.norm(traction[-1]-declared["bottom"]) > .5
    assert np.linalg.norm(drag[-1]) > .1
    free = traction[:-1]-traction[1:]+external[1:-1]
    assert np.max(np.linalg.norm(free, axis=1)) < 1e-8


@pytest.mark.parametrize("boundary,point,weight", [(8., 14., 30.), (8., 14., -30.),
                                                (8.3, 14.7, 30.), (8.3, 14.7, -30.)])
@pytest.mark.parametrize("elements", [12, 24, 48])
def test_actual_raw2_initializer_and_first_step_match_independent_traction_mesh(elements, boundary, point, weight):
    actual = benchmark.actual_case(elements, boundary, point, weight)
    assert actual["elements"] == elements
    assert actual["body_wet_weight_n"] == weight
    assert actual["independent_force_residual_n"] < 1e-6
    assert actual["actual_first_step_drift_m"] < 1e-7
    assert actual["actual_initial_function_evaluations"] > 0
    assert actual["actual_static_core_work_units"] > 0
    assert actual["declared_static_and_mapping_work_units"] >= actual["actual_static_core_work_units"]
    # Error is measured, not required to be monotonic or tuned to an asserted
    # convergence rate. Each mesh has a different independently fixed vessel.
    expected = np.linalg.norm(np.array(actual["discrete_displacement_m"])-actual["continuous_displacement_m"])
    assert actual["endpoint_displacement_error_m"] == pytest.approx(expected, abs=1e-14)


def test_actual_driven_time_probe_retains_inventory_and_measurable_free_response():
    probe = benchmark.driven_time_probe()
    assert [row["internal_dt_s"] for row in probe["cases"]] == [.016, .008, .004, .002]
    for row in probe["cases"]:
        assert row["paid_out_m"] == .02
        assert row["material_length_m"] == pytest.approx(24.02, abs=1e-11)
        assert row["max_free_node_displacement_m"] > .001
    assert probe["cases"][-1]["max_position_difference_from_2ms_m"] == 0
    # The observed trend is stored in evidence, without asserting a universal
    # convergence rate or considering the finest permitted step an exact ODE.
    differences = [row["max_position_difference_from_2ms_m"] for row in probe["cases"][:-1]]
    assert all(math.isfinite(value) and value > 0 for value in differences)
