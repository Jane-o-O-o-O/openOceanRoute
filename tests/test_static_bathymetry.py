"""Independent force/geometry checks for bounded static seabed research."""
from copy import deepcopy
import json
import math

import numpy as np
import pytest
from scipy.integrate import quad

from oceanroute.static_bathymetry import slope_catenary, static_equilibrium


def plane(gradient=(0., 0.), depth=30., extent=80.):
    x, y = [-extent, 0., extent], [-extent, 0., extent]
    return {"schema": "oceanroute.bathymetry.v1", "x_m": x, "y_m": y,
            "z_m": [[-depth+gradient[0]*a+gradient[1]*b for a in x] for b in y],
            "source": {"name": "explicit synthetic plane", "horizontal_crs": "LOCAL_CARTESIAN_METRES",
                       "origin_projected_m": [0, 0], "vertical_datum": "already aligned to model sea zero"}}


def config(**kwargs):
    return {"seabed_grid": plane(), "wet_weight_n_m": 4., "bottom_tension_n": 100.,
            "heading_deg": 90., "nodes": 41, "ea_n": None, **kwargs}


def test_inextensible_horizontal_bed_has_analytic_catenary_geometry_and_force_balance():
    r = slope_catenary(config())
    w, d, h = 4., 30., 100.
    length = math.sqrt(d*(d+2*h/w))
    layback = h/w*math.acosh(1+w*d/h)
    assert r["summary"]["natural_length_m"] == pytest.approx(length, abs=1e-9)
    assert r["summary"]["layback_m"] == pytest.approx(layback, abs=1e-9)
    assert r["summary"]["top_tension_n"] == pytest.approx(h+w*d)
    ends = r["end_forces_on_cable_n"]
    assert np.array(ends["vessel"])+np.array(ends["touchdown"]) == pytest.approx([0, 0, w*length])
    assert r["summary"]["total_force_balance_n"] == pytest.approx([0, 0, 0], abs=1e-10)
    assert r["accepted"] and r["solver"]["converged"]


@pytest.mark.parametrize("gradient,heading", [((.2, .1), 90), ((-.2, .1), 90), ((.1, .2), 0), ((.1, -.2), 0)])
@pytest.mark.parametrize("ea", [None, 1e4, 1e7])
def test_positive_negative_and_cross_slope_match_independent_force_ode_quadrature(gradient, heading, ea):
    r = slope_catenary(config(seabed_grid=plane(gradient), heading_deg=heading, ea_n=ea))
    summary = r["summary"]
    s = summary["natural_length_m"]
    direction = np.array([math.sin(math.radians(heading)), math.cos(math.radians(heading))])
    m = np.dot(gradient, direction)
    h, v0 = 100/math.sqrt(1+m*m), 100*m/math.sqrt(1+m*m)
    inv_ea = 0 if ea is None else 1/ea
    def tension(q): return math.hypot(h, v0+4*q)
    x = quad(lambda q: (1+inv_ea*tension(q))*h/tension(q), 0, s, epsabs=1e-10)[0]
    z = quad(lambda q: (1+inv_ea*tension(q))*(v0+4*q)/tension(q), 0, s, epsabs=1e-10)[0]
    stretched = quad(lambda q: 1+inv_ea*tension(q), 0, s, epsabs=1e-10)[0]
    assert z-m*x == pytest.approx(30., abs=1e-9)
    assert summary["layback_m"] == pytest.approx(x, abs=1e-9)
    assert summary["stretched_arc_length_m"] == pytest.approx(stretched, abs=1e-9)
    assert summary["bottom_vertical_tension_n"] == pytest.approx(v0)
    assert summary["horizontal_tension_n"] == pytest.approx(h)
    assert summary["top_vertical_tension_n"]-v0 == pytest.approx(4*s)
    bottom_t = np.array(r["node_tension_vectors_n"][-1])/100
    normal = np.array([-gradient[0], -gradient[1], 1])/math.sqrt(1+sum(g*g for g in gradient))
    assert np.dot(bottom_t, normal) == pytest.approx(0., abs=1e-12)
    assert min(r["node_clearance_m"]) >= -1e-8
    assert r["nodes"][0] == [0., 0., 0.]
    assert r["summary"]["geometric_chord_length_m"] <= stretched+1e-9
    json.dumps(r, allow_nan=False)


def test_rotation_covariance_including_tension_and_surface_normal_vectors():
    a = slope_catenary(config(seabed_grid=plane((.2, .1)), heading_deg=73., ea_n=1e5))
    b = slope_catenary(config(seabed_grid=plane((-.1, .2)), heading_deg=-17., ea_n=1e5))
    rotate = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    for key in ("nodes", "node_tension_vectors_n", "node_seabed_normal"):
        assert np.array(a[key])@rotate.T == pytest.approx(np.array(b[key]), abs=2e-10)
    assert b["node_tension_n"] == pytest.approx(a["node_tension_n"], abs=2e-10)
    assert b["summary"]["natural_length_m"] == pytest.approx(a["summary"]["natural_length_m"], abs=1e-10)


def test_translated_vessel_uses_depth_at_its_actual_xy_and_absolute_model_sea_height():
    g = plane((.1, .05))
    r = slope_catenary(config(seabed_grid=g, vessel_position_m=[10., -6., -2.], ea_n=1e5))
    assert r["summary"]["vessel_clearance_above_bed_m"] == pytest.approx(27.3)
    assert r["nodes"][0] == [10., -6., -2.]
    td = r["nodes"][-1]
    assert td[2] == pytest.approx(-30+.1*td[0]+.05*td[1], abs=1e-9)
    assert r["seabed"]["grid"]["source"]["vertical_datum"] == g["source"]["vertical_datum"]


def test_full_grid_affine_check_detects_internal_deviation_with_unchanged_corners():
    g = plane(); g["z_m"][1][1] += .01
    with pytest.raises(ValueError, match="across all grid nodes"):
        slope_catenary(config(seabed_grid=g))


def test_no_missing_coverage_or_outside_touchdown_is_projected_or_filled():
    g = plane(); g["z_m"][0][0] = None
    with pytest.raises(ValueError, match="NoData"):
        slope_catenary(config(seabed_grid=g))
    with pytest.raises(ValueError, match="outside"):
        slope_catenary(config(seabed_grid=plane(extent=10.)))
    with pytest.raises(ValueError, match="above"):
        slope_catenary(config(vessel_position_m=[0, 0, -30]))


@pytest.mark.parametrize("key,value", [("bottom_tension_n", 0.), ("bottom_tension_n", -1.), ("bottom_tension_n", True),
                                       ("ea_n", float("inf")), pytest.param("ea_n", 10**10000, id="huge-integer-ea"), ("plane_tolerance_m", float("nan")),
                                       ("plane_tolerance_m", .001), ("max_root_iterations", 0), ("nodes", 1001)])
def test_invalid_physical_and_numerical_inputs_reject(key, value):
    with pytest.raises(ValueError):
        slope_catenary(config(**{key: value}))


@pytest.mark.parametrize("extra", [{"current_x_m_s": 1.}, {"ei_n_m2": 1.}, {"material_segments": []}, {"depth_m": 50.}])
def test_unsupported_inputs_are_not_silently_ignored(extra):
    with pytest.raises(ValueError, match="unsupported"):
        slope_catenary(config(**extra))


def test_length_root_and_computation_limits_fail_without_success_shape():
    with pytest.raises(ValueError, match="max_natural_length"):
        slope_catenary(config(max_natural_length_m=1.))
    with pytest.raises(ValueError, match="root did not converge"):
        slope_catenary(config(max_root_iterations=1))
    with pytest.raises(ValueError, match="max_work_units"):
        slope_catenary(config(max_work_units=10))


def test_reachable_length_bound_endpoint_has_explicit_bounded_iteration_count():
    analytic = math.sqrt(30*(30+2*100/4))
    a = slope_catenary(config(max_natural_length_m=analytic))
    b = slope_catenary(config(max_natural_length_m=slope_catenary(config())["summary"]["natural_length_m"]))
    for result in (a, b):
        assert result["accepted"]
        assert result["solver"]["iterations"] == 0
        assert result["solver"]["root_at_length_limit"]
        assert result["solver"]["function_evaluations"] == 1
        json.dumps(result, allow_nan=False)
    with pytest.raises(ValueError, match="max_natural_length"):
        slope_catenary(config(max_natural_length_m=analytic-.0001))


def test_large_tension_small_weight_does_not_lose_height_in_subtractive_formulas():
    r = slope_catenary(config(seabed_grid=plane(depth=1., extent=1e5), bottom_tension_n=1e8,
                              wet_weight_n_m=1., ea_n=1e12))
    assert r["solver"]["root_residual_m"] < 1e-9
    assert r["nodes"][0][2]-r["nodes"][-1][2] == pytest.approx(1., abs=1e-9)
    assert r["summary"]["natural_length_m"] > 10000
    json.dumps(r, allow_nan=False)


def test_input_document_is_not_mutated_and_source_datum_is_not_automatically_shifted():
    c = config(); original = deepcopy(c)
    r = slope_catenary(c)
    assert c == original
    assert r["summary"]["touchdown_depth_below_model_sea_m"] == pytest.approx(30.)


def curved_grid(gx=.1, gy=.05, mixed=.0003):
    g = plane(depth=40., extent=100.)
    g["z_m"] = [[-40+gx*x+gy*y+mixed*x*y for x in g["x_m"]] for y in g["y_m"]]
    return g


def static_config(**kwargs):
    return {"seabed_grid": curved_grid(), "vessel_position_m": [0., 0., 0.],
            "anchor_position_m": [-60., -10., -46.32], "natural_length_m": 80.,
            "nodes": 18, "ea_n": 1e4, "wet_weight_n_m": 4., **kwargs}


def independently_reconstructed_force(result, ea, weight):
    """Reconstruct balance from published geometry, not the production helper."""
    p, rest = np.array(result["nodes"]), np.array(result["rest_lengths_m"])
    chords = np.diff(p, axis=0)
    lengths = np.sqrt(np.sum(chords**2, axis=1))
    tensions = ea*np.maximum(lengths/rest-1., 0.)
    tractions = chords/lengths[:, None]*tensions[:, None]
    forces = np.vstack((tractions, [0., 0., 0.]))-np.vstack(([0., 0., 0.], tractions))
    wnode = weight*np.r_[rest[0]/2, (rest[:-1]+rest[1:])/2, rest[-1]/2]
    forces[:, 2] -= wnode
    residual = forces+np.array(result["node_contact_force_n"])+np.array(result["node_boundary_force_n"])
    return tensions, residual, wnode


def test_curved_2d_contact_solves_actual_independent_nodal_balance_and_preserves_material():
    c = static_config(); original = deepcopy(c)
    r = static_equilibrium(c)
    assert c == original
    assert r["accepted"] and r["solver"]["converged"]
    assert 2 < r["summary"]["contact_nodes"] < c["nodes"]
    tensions, residual, weight = independently_reconstructed_force(r, c["ea_n"], 4.)
    assert tensions == pytest.approx(r["segment_tension_n"], abs=1e-8)
    assert np.linalg.norm(residual, axis=1).max() < r["solver"]["force_tolerance_n"]
    assert residual == pytest.approx(np.array(r["node_force_residual_n"]), abs=1e-8)
    assert weight.sum() == pytest.approx(4*80)
    assert np.sum(r["rest_lengths_m"]) == pytest.approx(80)
    assert r["node_material_m"][0] == pytest.approx(80)
    assert r["node_material_m"][-1] == 0
    assert np.diff(r["node_material_m"]) == pytest.approx(-np.array(r["rest_lengths_m"]))
    normals, reactions = np.array(r["node_seabed_normal"]), np.array(r["node_contact_force_n"])
    assert np.linalg.norm(np.cross(normals, reactions), axis=1).max() < 1e-9
    assert min(r["node_contact_normal_force_n"]) >= 0
    p = np.array(r["nodes"])
    exact_normal = np.column_stack((-.1-.0003*p[:, 1], -.05-.0003*p[:, 0], np.ones(len(p))))
    exact_normal /= np.linalg.norm(exact_normal, axis=1)[:, None]
    assert normals == pytest.approx(exact_normal, abs=1e-12)
    assert np.ptp(normals[:, 0]) > .001 and np.ptp(normals[:, 1]) > .005
    assert r["segment_clearance"]["minimum_clearance_m"] >= -1e-6
    assert min(r["node_clearance_m"]) >= -1e-6
    json.dumps(r, allow_nan=False)


def test_2d_static_geometry_force_and_contact_rotate_covariantly():
    a = static_equilibrium(static_config())
    b = static_equilibrium(static_config(seabed_grid=curved_grid(-.05, .1, -.0003), anchor_position_m=[10, -60, -46.32]))
    assert a["accepted"] and b["accepted"]
    rotate = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    for key in ("nodes", "node_seabed_normal", "node_contact_force_n", "node_internal_force_n", "node_boundary_force_n"):
        assert np.array(a[key])@rotate.T == pytest.approx(np.array(b[key]), abs=2e-7)
    assert a["segment_tension_n"] == pytest.approx(b["segment_tension_n"], abs=2e-7)
    assert a["summary"]["total_potential_energy_j"] == pytest.approx(b["summary"]["total_potential_energy_j"], abs=2e-7)


def test_frictionless_contact_on_plane_matches_closed_discrete_slope_force_solution():
    m, n, ea, w = .1, 12, 1e4, 4.
    distance = 40*math.sqrt(1+m*m)
    natural = distance/1.02
    c = static_config(seabed_grid=plane((m, 0.)), vessel_position_m=[-20, 0, -32],
                      anchor_position_m=[20, 0, -28], natural_length_m=natural, nodes=n, ea_n=ea,
                      force_tolerance_n=.001)
    r = static_equilibrium(c)
    assert r["accepted"] and r["summary"]["contact_nodes"] == n
    rest = natural/(n-1)
    sine, cosine = m/math.sqrt(1+m*m), 1/math.sqrt(1+m*m)
    first_t = ea*(distance/natural-1)-(n-2)/2*w*rest*sine
    expected_t = first_t+np.arange(n-1)*w*rest*sine
    assert r["segment_tension_n"] == pytest.approx(expected_t, abs=1e-3)
    expected_force = np.array(r["node_wet_weight_n"])[1:-1]*cosine
    assert r["node_contact_normal_force_n"][1:-1] == pytest.approx(expected_force, abs=1e-3)
    expected_positions = np.array(c["vessel_position_m"])+np.r_[0., np.cumsum(rest*(1+expected_t/ea))][:, None]*np.array([cosine, 0., sine])
    assert np.array(r["nodes"]) == pytest.approx(expected_positions, abs=1e-4)


def test_suspended_discrete_statics_refines_to_independent_continuous_elastic_plane_solution():
    position_errors, end_force_errors = [], []
    for n in (8, 16, 32):
        analytic = slope_catenary(config(seabed_grid=plane((.1, .05)), ea_n=1e5, nodes=n))
        r = static_equilibrium({"seabed_grid": plane((.1, .05)), "vessel_position_m": analytic["nodes"][0],
                                "anchor_position_m": analytic["nodes"][-1], "nodes": n, "ea_n": 1e5,
                                "natural_length_m": analytic["summary"]["natural_length_m"],
                                "initial_positions_m": analytic["nodes"], "max_work_units": 400000000})
        assert r["accepted"]
        position_errors.append(float(np.max(np.linalg.norm(np.array(r["nodes"])-analytic["nodes"], axis=1))))
        end_force_errors.append(float(np.linalg.norm(np.array(r["end_forces_on_cable_n"]["vessel"])-analytic["end_forces_on_cable_n"]["vessel"])))
    assert position_errors[1] < .35*position_errors[0] and position_errors[2] < .35*position_errors[1]
    assert end_force_errors[1] < .35*end_force_errors[0] and end_force_errors[2] < .35*end_force_errors[1]
    assert position_errors[-1] < .006 and end_force_errors[-1] < .12


def prescribed_contact_case(mu=.2, bed=None, points=None):
    bed = plane((.1, 0.)) if bed is None else bed
    if points is None:
        points = [[float(x), 0., float(-30+.1*x)] for x in np.linspace(-20, 20, 6)]
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    return {"seabed_grid": bed, "vessel_position_m": points[0], "anchor_position_m": points[-1],
            "rest_lengths_m": lengths.tolist(), "ea_n": 1e5, "contact_policy": "prescribed_stick",
            "seabed_friction": mu, "sticking_nodes": [{"node_index":i, "position_m":points[i]} for i in range(1, len(points)-1)],
            "initial_positions_m": points}


@pytest.mark.parametrize("mu", [.1, .2, 1.])
def test_declared_stick_history_supports_known_slope_weight_only_if_coulomb_capacity_suffices(mu):
    r = static_equilibrium(prescribed_contact_case(mu))
    assert r["accepted"]
    for row in r["stick_feasibility"]:
        assert row["required_tangential_n"] == pytest.approx(.1*row["required_normal_n"], abs=1e-8)
        assert row["margin_n"] >= -1e-8
    contact = np.array(r["node_contact_force_n"])
    assert contact[1:-1] == pytest.approx(np.column_stack((np.zeros(4), np.zeros(4), r["node_wet_weight_n"][1:-1])), abs=1e-8)
    json.dumps(r, allow_nan=False)


def test_insufficient_friction_returns_actual_capacity_clipped_force_and_unaccepted_shape():
    r = static_equilibrium(prescribed_contact_case(.01))
    assert not r["accepted"] and r["solver"]["converged"]
    assert "PRESCRIBED_STICK_CAPACITY_EXCEEDED" in r["solver"]["rejection_codes"]
    actual = np.linalg.norm(r["node_contact_friction_force_n"], axis=1)
    assert np.all(actual <= .01*np.array(r["node_contact_normal_force_n"])+1e-8)
    assert max(np.linalg.norm(r["node_force_residual_n"], axis=1)) > 1
    assert min(row["margin_n"] for row in r["stick_feasibility"]) < -1
    json.dumps(r, allow_nan=False)


def test_prescribed_stick_cannot_invent_adhesion_to_hold_an_upward_pulling_line():
    c = prescribed_contact_case(points=[[-20, 0, -20], [-12, 0, -30], [-4, 0, -30],
                                       [4, 0, -30], [12, 0, -30], [20, 0, -20]], bed=plane())
    c["rest_lengths_m"] = (np.array(c["rest_lengths_m"])*.8).tolist()
    r = static_equilibrium(c)
    assert not r["accepted"]
    assert min(row["required_normal_n"] for row in r["stick_feasibility"]) < 0
    assert min(r["node_contact_normal_force_n"]) >= 0
    assert "PRESCRIBED_STICK_CAPACITY_EXCEEDED" in r["solver"]["rejection_codes"]


def test_exact_between_node_bilinear_clearance_rejects_hidden_ridge_even_with_balanced_nodes():
    g = curved_grid(0., 0., .003)
    points = [[x, -x, -40-.003*x*x] for x in [-30., -25., -20., 20., 25., 30.]]
    r = static_equilibrium(prescribed_contact_case(.5, g, points))
    assert r["solver"]["converged"] and not r["accepted"]
    assert r["solver"]["max_node_force_residual_n"] < 1e-6
    assert min(r["node_clearance_m"]) >= -1e-8
    assert r["segment_clearance"]["minimum_clearance_m"] == pytest.approx(-1.2, abs=1e-8)
    assert r["segment_clearance"]["worst_segment_index"] == 2
    assert "STRAIGHT_SEGMENT_BED_INTERSECTION" in r["solver"]["rejection_codes"]


def test_segment_unknown_coverage_hard_rejects_when_all_nodes_have_known_cells():
    g = plane(extent=80.)
    g["x_m"] = g["y_m"] = [-80., -40., 0., 40., 80.]
    g["z_m"] = [[-30.]*5 for _ in range(5)]
    g["z_m"][2][2] = None
    p = [[x, 20., -30.] for x in [-60., -70., -50., 50., 70., 60.]]
    with pytest.raises(ValueError, match="NoData"):
        static_equilibrium(prescribed_contact_case(.5, g, p))


def test_real_optimizer_nonconvergence_and_independent_force_rejection_are_distinct():
    a = static_equilibrium(static_config(max_solver_iterations=1))
    assert not a["accepted"] and not a["solver"]["converged"]
    assert "OPTIMIZER_NOT_CONVERGED" in a["solver"]["rejection_codes"]
    b = static_equilibrium(static_config(force_tolerance_n=1e-6, relative_force_tolerance=1e-9))
    assert not b["accepted"] and b["solver"]["converged"]
    assert "FORCE_BALANCE_NOT_CONVERGED" in b["solver"]["rejection_codes"]
    c = static_equilibrium(static_config(max_function_evaluations=1))
    assert not c["accepted"] and "COMPUTATION_BUDGET_EXHAUSTED" in c["solver"]["rejection_codes"]
    assert c["solver"]["function_evaluations"] <= 1
    for r in (a, b, c): json.dumps(r, allow_nan=False)


def test_exhausted_exact_chord_verification_budget_cannot_claim_accepted_contact():
    r = static_equilibrium(static_config(max_segment_samples=10))
    assert not r["accepted"] and r["segment_clearance"] is None
    assert "COMPUTATION_BUDGET_EXHAUSTED" in r["solver"]["rejection_codes"]


@pytest.mark.parametrize("changes", [{"natural_length_m":0}, {"natural_length_m":None}, {"natural_length_m":True},
    {"rest_lengths_m":[1.]*17}, {"nodes":81}, {"max_work_units":1}, {"ea_n":None}, {"ea_n":float("nan")},
    {"current_x_m_s":1}, {"ei_n_m2":1}, {"contact_policy":"unknown"}, {"seabed_friction":.1},
    {"contact_policy":"prescribed_stick", "seabed_friction":.5}, {"anchor_position_m":[-200,0,-40]},
    {"vessel_position_m":[0,0,1]}, {"anchor_position_m":[-60,-10,-50]},
    {"force_tolerance_n":10}, {"max_solver_iterations":float("inf")}])
def test_static_invalid_boundaries_ambiguous_lengths_and_physics_are_rejected(changes):
    with pytest.raises(ValueError):
        static_equilibrium(static_config(**changes))


def test_sticking_index_position_and_history_validation():
    c = prescribed_contact_case()
    for alteration in ([{"node_index":0, "position_m":c["initial_positions_m"][0]}],
                       [c["sticking_nodes"][0], c["sticking_nodes"][0]],
                       [{"node_index":1, "position_m":[-12,0,-20]}]):
        with pytest.raises(ValueError):
            static_equilibrium({**c, "sticking_nodes":alteration})
    p = deepcopy(c["initial_positions_m"]);p[2][2] -= 1
    with pytest.raises(ValueError):
        static_equilibrium({**c,"initial_positions_m":p})


@pytest.fixture
def static_client(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    with TestClient(create_app(ProjectStore(tmp_path/"static-test-projects.json"))) as client:
        yield client


@pytest.mark.parametrize("route,settings", [("slope-catenary", config(seabed_grid=plane((.1, .05)), ea_n=1e5)),
                                           ("static-bathymetry", static_config())])
def test_actual_http_static_routes_return_accepted_finite_force_results(static_client, route, settings):
    response = static_client.post("/api/simulation/"+route, json={"config":settings})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"] and result["validation_status"] == "research"
    assert result["summary"]["total_force_balance_n"] is not None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("route,payload", [("slope-catenary", {}),
    ("slope-catenary", {"config":config(bottom_tension_n=0)}),
    ("slope-catenary", {"config":config(seabed_grid=curved_grid())}),
    ("slope-catenary", {"config":config(), "project":{}}),
    ("static-bathymetry", {"config":static_config(seabed_friction=.5)}),
    ("static-bathymetry", {"config":static_config(max_work_units=1)}),
    ("static-bathymetry", {"config":static_config(), "project":{}})])
def test_http_bad_boundary_or_ambiguous_physics_rejects_with_finite_json(static_client, route, payload):
    response = static_client.post("/api/simulation/"+route, json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]
    json.dumps(response.json(), allow_nan=False)


def test_http_nonconverged_static_is_finite_diagnostic_result_not_claimed_success(static_client):
    response = static_client.post("/api/simulation/static-bathymetry", json={"config":static_config(max_solver_iterations=1)})
    assert response.status_code == 200
    result = response.json()
    assert not result["accepted"] and not result["solver"]["converged"]
    assert result["solver"]["rejection_codes"]
    json.dumps(result, allow_nan=False)
