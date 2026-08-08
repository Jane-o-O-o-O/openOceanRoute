"""Actual inverse truth recovery, sensor geometry, noise and bounded failure."""
from copy import deepcopy
import json
import math

import numpy as np
import pytest

from oceanroute.seismic import estimate_current, predict_transponders


def example():
    return {"reference_frame": {"kind": "local_enu", "origin_wgs84": [118, 22, 0]},
            "line": {"depth_m": 100, "wet_weight_n_m": 4, "diameter_m": .02,
                     "bottom_tension_n": 250, "nodes": 128},
            "snapshots": [{"id": "steady-1", "time_s": 0, "vessel_position_m": [0, 0, 0],
                           "ship_speed_m_s": 1, "heading_deg": 90,
                           "observations": [{"id": f"t{i}", "arc_from_vessel_m": arc,
                                             "sigma_m": [.25, .25, .25]} for i, arc in enumerate([20, 50, 80])]}],
            "current_m_s": [.25, .2]}


def truth(config=None):
    c = deepcopy(config or example())
    predicted = predict_transponders(c)
    for row, record in zip(c["snapshots"], predicted["snapshots"]):
        for o, p in zip(row["observations"], record["observations"]):
            o["position_m"] = p["predicted_position_m"]
    c.pop("current_m_s")
    c["initial_current_m_s"] = [0, 0]
    return c


def test_static_observation_operator_matches_independent_analytic_catenary_and_enu_origin():
    c = example()
    c["current_m_s"] = [0, 0]
    c["line"]["nodes"] = 151
    row = c["snapshots"][0]
    row["ship_speed_m_s"] = 0
    row["vessel_position_m"] = [1234, -256, 0]
    row["observations"] = [{"id": "a", "arc_from_vessel_m": 50}, {"id": "b", "arc_from_vessel_m": 100}]
    result = predict_transponders(c)
    a = 250/4
    length = math.sqrt(100*(100+2*a))
    for o in result["snapshots"][0]["observations"]:
        remaining = length-o["arc_from_vessel_m"]
        expected = [1234-a*(math.asinh(length/a)-math.asinh(remaining/a)), -256,
                    math.sqrt(a*a+remaining*remaining)-math.sqrt(a*a+length*length)]
        assert o["predicted_position_m"] == pytest.approx(expected, abs=1e-10)
    assert result["snapshots"][0]["nodes"][0] == [1234, -256, 0]
    assert result["snapshots"][0]["nodes"][-1][2] == -100
    assert result["snapshots"][0]["end_forces"]["balance_residual_norm_n"] < 1e-9


def test_actual_three_transponder_inverse_recovers_uniform_truth_and_reduces_residual():
    result = estimate_current(truth())
    assert result["estimated_current_m_s"] == pytest.approx([.25, .2], abs=1e-7)
    assert result["summary"]["estimate_accepted"]
    assert result["identifiability"]["rank"] == 2
    assert result["identifiability"]["jacobian_stable"]
    assert result["summary"]["residual_rms_m"] < 1e-7
    assert result["summary"]["weighted_residual_sum_squares"] < result["summary"]["initial_weighted_residual_sum_squares"]*1e-8
    assert result["solver"]["forward_snapshot_solves"] > 5
    assert result["solver"]["ode_function_evaluations"] > 0
    assert np.linalg.eigvalsh(result["parameter_covariance_m2_s2"]).min() > 0
    information = np.array(result["information_matrix"])
    assert information@np.array(result["parameter_covariance_m2_s2"]) == pytest.approx(np.eye(2), abs=1e-10)


def test_material_coordinate_observations_match_arc_but_require_current_top_material():
    arc = example()
    material = deepcopy(arc)
    material["snapshots"][0]["top_material_m"] = 1000
    for o in material["snapshots"][0]["observations"]:
        o["material_m"] = 1000-o.pop("arc_from_vessel_m")
    a, b = predict_transponders(arc), predict_transponders(material)
    for first, second in zip(a["snapshots"][0]["observations"], b["snapshots"][0]["observations"]):
        assert first["predicted_position_m"] == second["predicted_position_m"]
        assert second["arc_from_vessel_m"] == 1000-second["material_m"]
    fitted = estimate_current(truth(material))
    assert fitted["estimated_current_m_s"] == pytest.approx([.25, .2], abs=1e-7)


def test_multiple_known_ship_headings_resolve_same_earth_current_without_time_filter():
    c = example()
    second = deepcopy(c["snapshots"][0])
    second.update({"id": "steady-2", "time_s": 20, "vessel_position_m": [10, 4, 0],
                   "heading_deg": 35, "ship_speed_m_s": .5})
    c["snapshots"].append(second)
    result = estimate_current(truth(c))
    assert result["estimated_current_m_s"] == pytest.approx([.25, .2], abs=1e-7)
    assert result["summary"]["snapshot_count"] == 2
    assert result["summary"]["measurement_component_count"] == 18
    assert result["snapshots"][1]["nodes"][0] == [10, 4, 0]
    assert result["snapshots"][1]["time_s"] == 20


def test_absolute_measurement_sigma_scaling_changes_covariance_by_square_without_rescaling_to_zero_fit():
    a = truth()
    b = deepcopy(a)
    for o in b["snapshots"][0]["observations"]:
        o["sigma_m"] = [.5, .5, .5]
    first, second = estimate_current(a), estimate_current(b)
    assert first["estimated_current_m_s"] == pytest.approx(second["estimated_current_m_s"], abs=1e-8)
    assert np.array(second["parameter_covariance_m2_s2"]) == pytest.approx(4*np.array(first["parameter_covariance_m2_s2"]), rel=1e-6)
    assert min(first["parameter_std_m_s"]) > 0


def test_seeded_noisy_position_measurements_are_actually_fitted_with_finite_uncertainty():
    c = truth()
    rng = np.random.default_rng(123)
    for o in c["snapshots"][0]["observations"]:
        o["position_m"] = (np.array(o["position_m"])+rng.normal(0, .25, 3)).tolist()
    result = estimate_current(c)
    assert result["summary"]["estimate_accepted"]
    error = np.abs(np.array(result["estimated_current_m_s"])-[.25, .2])
    assert np.all(error < 3*np.array(result["parameter_std_m_s"]))
    assert result["summary"]["weighted_residual_sum_squares"] > 1e-5
    assert result["summary"]["weighted_residual_sum_squares"] < result["summary"]["initial_weighted_residual_sum_squares"]


def test_correlated_position_covariance_whitening_matches_mahalanobis_quadratic_form():
    c = example()
    covariance = np.array([[1, .3, 0], [.3, 1, .2], [0, .2, .5]])
    p = predict_transponders(c)
    for o, pred in zip(c["snapshots"][0]["observations"], p["snapshots"][0]["observations"]):
        o.pop("sigma_m")
        o["covariance_m2"] = covariance.tolist()
        o["position_m"] = (np.array(pred["predicted_position_m"])+[.1, -.2, .05]).tolist()
    result = predict_transponders(c)
    error = np.array([-.1, .2, -.05])
    expected = float(error@np.linalg.solve(covariance, error))
    for observation in result["snapshots"][0]["observations"]:
        assert observation["mahalanobis_norm"]**2 == pytest.approx(expected, abs=1e-12)


def test_missing_vertical_components_are_omitted_not_silently_set_to_zero():
    c = truth()
    for o in c["snapshots"][0]["observations"]:
        o["position_m"][2] = None
    result = estimate_current(c)
    assert result["summary"]["measurement_component_count"] == 6
    assert result["estimated_current_m_s"] == pytest.approx([.25, .2], abs=1e-7)
    assert result["summary"]["estimate_accepted"]
    assert all(o["residual_m"][2] is None for o in result["snapshots"][0]["observations"])


@pytest.mark.parametrize("geometry", ["vessel_only", "no_drag", "one_axis"])
def test_unobservable_sensor_geometry_never_has_accepted_estimate_or_false_finite_covariance(geometry):
    c = example()
    if geometry == "vessel_only":
        for o in c["snapshots"][0]["observations"]:
            o["arc_from_vessel_m"] = 0
    if geometry == "no_drag":
        c["line"]["drag_coefficient"] = 0
    c = truth(c)
    if geometry == "one_axis":
        c["snapshots"][0]["observations"] = c["snapshots"][0]["observations"][:1]
        c["snapshots"][0]["observations"][0]["position_m"][:2] = [None, None]
    result = estimate_current(c)
    assert result["identifiability"]["rank"] < 2
    assert not result["summary"]["estimate_accepted"]
    assert result["parameter_covariance_m2_s2"] is None
    assert result["identifiability"]["condition_number"] is None
    assert any(w["code"] == "CURRENT_NOT_IDENTIFIABLE" for w in result["warnings"])
    json.dumps(result, allow_nan=False)


def test_zero_relative_flow_is_not_assigned_spurious_linear_covariance():
    c = example()
    c["current_m_s"] = [1, 0]
    c = truth(c)
    c["initial_current_m_s"] = [1, 0]
    result = estimate_current(c)
    assert result["estimated_current_m_s"] == pytest.approx([1, 0], abs=1e-5)
    assert not result["summary"]["estimate_accepted"]
    assert not result["identifiability"]["jacobian_stable"]
    assert result["parameter_covariance_m2_s2"] is None


def test_large_outlier_retains_actual_best_fit_but_rejects_estimate_and_covariance():
    c = truth()
    c["snapshots"][0]["observations"][0]["position_m"][0] += 20
    result = estimate_current(c)
    assert not result["summary"]["estimate_accepted"]
    assert not result["summary"]["consistent_with_declared_uncertainty"]
    assert result["summary"]["outlier_count"] > 0
    assert result["parameter_covariance_m2_s2"] is None
    assert len(result["snapshots"][0]["nodes"]) == 128
    assert any(w["code"] == "TRANSPONDER_RESIDUAL_MISMATCH" for w in result["warnings"])


def test_bound_active_estimate_and_iteration_exhaustion_never_claim_success():
    c = truth()
    bound = estimate_current({**c, "current_bounds_m_s": {"lower": [-.2, -.2], "upper": [.2, .2]}})
    assert not bound["summary"]["estimate_accepted"]
    assert any(bound["identifiability"]["active_bounds"])
    assert bound["parameter_covariance_m2_s2"] is None
    stopped = estimate_current({**c, "max_evaluations": 1})
    assert not stopped["solver"]["converged"]
    assert not stopped["summary"]["estimate_accepted"]


@pytest.mark.parametrize("budget", [{"max_forward_solves": 1}, {"max_ode_evaluations": 100}])
def test_forward_computation_budget_is_enforced(budget):
    with pytest.raises(ValueError, match="budget"):
        estimate_current({**truth(), **budget})


def test_shifted_enu_observations_preserve_current_and_uncertainty():
    first = truth()
    shifted = deepcopy(first)
    delta = np.array([1234., -256., 0.])
    shifted["snapshots"][0]["vessel_position_m"] = delta.tolist()
    for o in shifted["snapshots"][0]["observations"]:
        o["position_m"] = (np.array(o["position_m"])+delta).tolist()
    a, b = estimate_current(first), estimate_current(shifted)
    assert a["estimated_current_m_s"] == pytest.approx(b["estimated_current_m_s"], abs=1e-7)
    assert np.array(a["parameter_covariance_m2_s2"]) == pytest.approx(np.array(b["parameter_covariance_m2_s2"]), rel=1e-6)


def test_finer_forward_observation_mesh_reduces_inverse_bias_against_separate_fine_truth():
    c = example()
    c["line"]["nodes"] = 500
    measured = truth(c)
    fits = []
    for nodes in (32, 128, 256):
        fit = estimate_current({**measured, "line": {**measured["line"], "nodes": nodes}})
        assert fit["summary"]["estimate_accepted"]
        fits.append(float(np.linalg.norm(np.array(fit["estimated_current_m_s"])-[.25, .2])))
    assert fits[-1] < fits[0]/10
    assert fits[-1] < 1e-5


def test_independent_duplicate_epochs_double_information_not_claim_temporal_filtering():
    c = truth()
    first = estimate_current(c)
    second = deepcopy(c["snapshots"][0])
    second.update({"id": "independent-repeat", "time_s": 10})
    c["snapshots"].append(second)
    repeated = estimate_current(c)
    assert np.array(repeated["parameter_covariance_m2_s2"]) == pytest.approx(np.array(first["parameter_covariance_m2_s2"])/2, rel=1e-5)


def test_declared_absolute_uncertainty_and_input_are_preserved_in_results():
    c = truth()
    result = estimate_current(c)
    assert result["input_config"]["snapshots"] == c["snapshots"]
    assert result["input_config"]["line"]["water_density_kg_m3"] == 1025
    assert result["snapshots"][0]["observations"][0]["position_covariance_m2"] == np.diag([.25**2]*3).tolist()
    assert result["parameter_order"] == ["current_x_m_s", "current_y_m_s"]


def test_accepted_fit_and_prediction_are_finite_json_and_do_not_mutate_observations():
    c = example()
    original = deepcopy(c)
    p = predict_transponders(c)
    assert c == original
    measured = truth(c)
    saved = deepcopy(measured)
    e = estimate_current(measured)
    assert measured == saved
    for result in (p, e):
        assert result["validation_status"] == "research"
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("path,value", [
    (("reference_frame", "kind"), "wgs84"),
    (("reference_frame", "origin_wgs84"), [181, 22, 0]),
    (("line", "depth_m"), float("nan")),
    (("line", "diameter_m"), True),
    (("line", "bottom_tension_n"), 0),
    (("line", "nodes"), 2),
    (("line", "inline_bodies"), []),
    (("line", "current_x_m_s"), 0),
    (("line", "ei_n_m2"), 1),
    (("initial_current_m_s",), [float("inf"), 0]),
    (("current_bounds_m_s",), {"lower": [2, 0], "upper": [1, 1]}),
    (("max_evaluations",), True),
    (("current_m_s",), [.25, .2]),
    (("unknown_nonfinite",), float("nan")),
    (("snapshots", 0, "vessel_position_m"), [0, 0, 1]),
    (("snapshots", 0, "observations", 0, "position_m"), [None, None, None]),
    (("snapshots", 0, "observations", 0, "sigma_m"), [0, .25, .25]),
    (("snapshots", 0, "observations", 0, "sigma_m"), .25),
    (("snapshots", 0, "observations", 0, "sigma_m"), [1e-6, 10, .25]),
    (("snapshots", 0, "observations", 0, "position_m"), [0, float("inf"), 0]),
    (("snapshots", 0, "observations", 0, "arc_from_vessel_m"), 1e6),
    (("snapshots", 0, "observations", 1, "id"), "t0"),
    (("snapshots", 0, "wave_kinematics"), {}),
])
def test_invalid_reference_measurement_physics_and_uncertainty_are_rejected(path, value):
    c = truth()
    target = c
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        estimate_current(c)


@pytest.mark.parametrize("covariance", [
    [[1, 2, 0], [2, 1, 0], [0, 0, 1]],
    [[1, .2, 0], [.3, 1, 0], [0, 0, 1]],
    [[1, 0, 0], [0, 0, 0], [0, 0, 1]],
    [[1, 0], [0, 1]],
])
def test_indefinite_asymmetric_singular_or_wrong_size_position_covariance_is_rejected(covariance):
    c = truth()
    o = c["snapshots"][0]["observations"][0]
    o.pop("sigma_m")
    o["covariance_m2"] = covariance
    with pytest.raises(ValueError):
        estimate_current(c)


def test_missing_uncertainty_and_ambiguous_material_mapping_are_rejected():
    c = truth()
    c["snapshots"][0]["observations"][0].pop("sigma_m")
    with pytest.raises(ValueError, match="sigma"):
        estimate_current(c)
    c = example()
    o = c["snapshots"][0]["observations"][0]
    o["material_m"] = o.pop("arc_from_vessel_m")
    with pytest.raises(ValueError, match="top_material"):
        predict_transponders(c)
    c["snapshots"][0]["top_material_m"] = 10
    with pytest.raises(ValueError, match="exceed"):
        predict_transponders(c)


@pytest.fixture
def seismic_api(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    return TestClient(create_app(ProjectStore(tmp_path/"seismic-api.sqlite3")))


@pytest.mark.parametrize("kind", ["predict", "estimate"])
def test_http_seismic_actual_nodes_and_estimation_survive_finite_json(seismic_api, kind):
    c = example() if kind == "predict" else truth()
    response = seismic_api.post(f"/api/seismic/{kind}", json={"config": c})
    assert response.status_code == 200, response.text
    result = response.json()
    json.dumps(result, allow_nan=False)
    assert result["validation_status"] == "research"
    assert len(result["snapshots"][0]["nodes"]) == 128
    assert result["snapshots"][0]["nodes"][0] == [0, 0, 0]
    if kind == "estimate":
        assert result["summary"]["estimate_accepted"]
        assert result["estimated_current_m_s"] == pytest.approx([.25, .2], abs=1e-7)


def test_http_unidentifiable_geometry_is_explicitly_unaccepted_not_fake_success(seismic_api):
    c = example()
    c["line"]["drag_coefficient"] = 0
    response = seismic_api.post("/api/seismic/estimate", json={"config": truth(c)})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["identifiability"]["rank"] == 0
    assert not result["summary"]["estimate_accepted"]
    assert result["parameter_covariance_m2_s2"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("kind,config", [
    ("predict", {**example(), "reference_frame": {"kind": "unknown"}}),
    ("estimate", example()),
    ("estimate", {**truth(), "max_forward_solves": 1}),
    ("estimate", {**truth(), "snapshots": []}),
])
def test_http_invalid_observation_assumptions_and_budget_return_422(seismic_api, kind, config):
    response = seismic_api.post(f"/api/seismic/{kind}", json={"config": config})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_http_unknown_seismic_workflow_is_404(seismic_api):
    assert seismic_api.post("/api/seismic/kalman", json={"config": example()}).status_code == 404
