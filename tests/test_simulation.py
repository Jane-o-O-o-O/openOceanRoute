"""Independent mechanics and numerical invariants, not commercial validation."""
import json
import math

import numpy as np
import pytest

from oceanroute.simulation import catenary, simulate_lay, span_analysis, steady_state


def test_catenary_force_balance_geometry_and_arc_length():
    depth, h, w = 120.0, 600.0, 6.0
    result = catenary({"depth_m": depth, "bottom_tension_n": h, "wet_weight_n_m": w, "nodes": 1000})
    points = np.array(result["nodes"])
    summary = result["summary"]
    assert points[0] == pytest.approx([0, 0, 0])
    assert points[-1, 2] == -depth
    assert np.all(points[:, 2] >= -depth)
    # Net vertical suspended weight is the vertical support reaction.
    assert summary["vertical_top_force_n"] == pytest.approx(w * summary["suspended_length_m"])
    assert summary["top_tension_n"]**2 == pytest.approx(h*h + summary["vertical_top_force_n"]**2)
    assert summary["top_tension_n"] - h == pytest.approx(w*depth)
    # Dense polygon arc is an independent geometric check on the closed form.
    polygon = np.linalg.norm(np.diff(points, axis=0), axis=1).sum()
    assert polygon == pytest.approx(summary["suspended_length_m"], rel=2e-7)
    assert result["validation_status"] == "research"


def test_catenary_inverse_boundary_conditions_agree():
    direct = catenary({"depth_m": 250, "bottom_tension_n": 800, "wet_weight_n_m": 5})
    for key, value in [("cable_length_m", direct["summary"]["suspended_length_m"]),
                       ("layback_m", direct["summary"]["layback_m"])]:
        inverse = catenary({"depth_m": 250, "wet_weight_n_m": 5, key: value})
        assert inverse["summary"]["bottom_tension_n"] == pytest.approx(800, rel=1e-8)
        assert inverse["nodes"] == pytest.approx(np.array(direct["nodes"]), abs=1e-7)


def test_zero_tension_vertical_limiting_solution_and_heading():
    vertical = catenary({"depth_m": 100, "bottom_tension_n": 0})
    assert vertical["summary"]["layback_m"] == 0
    assert vertical["summary"]["top_tension_n"] == 400
    north = catenary({"depth_m": 100, "heading_deg": 0})["nodes"][-1]
    east = catenary({"depth_m": 100, "heading_deg": 90})["nodes"][-1]
    assert north[0] == pytest.approx(0, abs=1e-10)
    assert north[1] < 0 and east[0] < 0
    assert east[1] == pytest.approx(0, abs=1e-10)


def test_no_drag_steady_matches_analytical_catenary():
    config = {"depth_m": 300, "bottom_tension_n": 200, "wet_weight_n_m": 8,
              "nodes": 100, "ship_speed_m_s": 0, "current_x_m_s": 0, "current_y_m_s": 0}
    analytical = catenary({k: config[k] for k in ("depth_m", "bottom_tension_n", "wet_weight_n_m", "nodes")})
    steady = steady_state(config)
    assert steady["nodes"] == pytest.approx(np.array(analytical["nodes"]), abs=2e-5)
    assert steady["summary"]["top_tension_n"] == pytest.approx(analytical["summary"]["top_tension_n"], rel=1e-7)


def test_steady_cross_current_breaks_plane_and_mirrors_correctly():
    config = {"depth_m": 100, "ship_speed_m_s": .5, "bottom_tension_n": 300}
    plus = steady_state({**config, "current_y_m_s": .3})
    minus = steady_state({**config, "current_y_m_s": -.3})
    p, m = np.array(plus["nodes"]), np.array(minus["nodes"])
    assert np.max(np.abs(p[:, 1])) > 1
    assert p[:, 0] == pytest.approx(m[:, 0], abs=1e-6)
    assert p[:, 1] == pytest.approx(-m[:, 1], abs=1e-6)
    assert p[:, 2] == pytest.approx(m[:, 2], abs=1e-6)


def test_dynamic_material_conservation_contact_and_positive_tension():
    result = simulate_lay({}, {"duration_s": 20, "dt_s": 1, "depth_m": 100, "nodes": 12,
                               "ship_speed_m_s": 1, "payout_m_s": 1,
                               "current_y_m_s": .2, "internal_dt_s": .05})
    summary = result["summary"]
    assert summary["paid_out_m"] == pytest.approx(20, abs=1e-9)
    assert summary["final_material_length_m"] - summary["initial_material_length_m"] == pytest.approx(20, abs=1e-8)
    assert summary["material_balance_residual_m"] < 1e-8
    assert result["frames"][-1]["ship"] == pytest.approx([20, 0, 0], abs=1e-7)
    assert result["frames"][-1]["time_s"] == 20
    for frame in result["frames"]:
        p = np.array(frame["nodes"])
        assert np.min(p[:, 2]) >= -100-1e-10
        assert frame["top_tension_n"] >= 0 and frame["bottom_tension_n"] >= 0
        assert frame["touchdown"][2] == pytest.approx(-100)
        assert len(frame["node_tension_n"]) == len(p)
    json.dumps(result, allow_nan=False)


def test_static_dynamic_refinement_approaches_exact_top_support():
    # Segment forces are measured at segment centres. The top segment force
    # approaches the analytical 500 N support as the material mesh is refined.
    errors = []
    for nodes in (12, 24, 48):
        result = simulate_lay({}, {"duration_s": 10, "depth_m": 100, "nodes": nodes,
                                  "wet_weight_n_m": 4, "bottom_tension_n": 100,
                                  "ship_speed_m_s": 0, "payout_m_s": 0, "drag_coefficient": 0})
        errors.append(abs(result["frames"][-1]["top_tension_n"]-500))
        p0 = np.array(result["frames"][0]["nodes"])
        p1 = np.array(result["frames"][-1]["nodes"])
        assert np.max(np.linalg.norm(p1-p0, axis=1)) < .05
    assert errors[1] < .6*errors[0]
    assert errors[2] < .6*errors[1]


def test_dynamic_temporal_refinement_reduces_position_change():
    positions = []
    for step in (.1, .05, .025):
        result = simulate_lay({}, {"duration_s": 10, "dt_s": 2, "depth_m": 100,
                                  "bottom_tension_n": 100, "nodes": 16,
                                  "ship_speed_m_s": .5, "payout_m_s": .5,
                                  "current_y_m_s": .2, "internal_dt_s": step})
        positions.append(np.array(result["frames"][-1]["nodes"]))
    coarse_change = np.linalg.norm(positions[1]-positions[0])
    fine_change = np.linalg.norm(positions[2]-positions[1])
    assert fine_change < .8*coarse_change
    assert fine_change < .3


def test_ship_turn_stop_and_payout_events_have_exact_integral():
    result = simulate_lay({}, {"duration_s": 12, "dt_s": 2.5, "depth_m": 100,
                              "ship_speed_m_s": 1, "payout_m_s": 1, "nodes": 12,
                              "ship_plan": [{"time_s": 3, "speed_m_s": 0, "payout_m_s": 0},
                                            {"time_s": 7, "speed_m_s": 2, "heading_deg": 0, "payout_m_s": 1.5}]})
    assert result["frames"][-1]["ship"] == pytest.approx([3, 10, 0], abs=1e-8)
    assert result["summary"]["paid_out_m"] == pytest.approx(10.5, abs=1e-8)
    assert result["frames"][-1]["time_s"] == 12


def test_current_changes_cable_positions_but_cannot_move_pinned_ends():
    config = {"duration_s": 10, "depth_m": 100, "nodes": 12, "ship_speed_m_s": 0,
              "payout_m_s": 0, "bottom_tension_n": 200}
    still = simulate_lay({}, config)
    flow = simulate_lay({}, {**config, "current_profile": [{"depth_m": 0, "x_m_s": 0, "y_m_s": .4},
                                                           {"depth_m": 100, "x_m_s": 0, "y_m_s": .2}]})
    a, b = np.array(still["frames"][-1]["nodes"]), np.array(flow["frames"][-1]["nodes"])
    assert a[[0, -1]] == pytest.approx(b[[0, -1]], abs=1e-10)
    assert np.max(b[:, 1]-a[:, 1]) > .2


def test_varying_bathymetry_initial_anchor_is_on_the_actual_seabed():
    bed = [{"x_m": -2000, "depth_m": 2000}, {"x_m": 0, "depth_m": 1000}]
    result = simulate_lay({}, {"duration_s": 2, "nodes": 12, "depth_m": 500,
                              "ship_speed_m_s": 0, "payout_m_s": 0, "seabed_profile": bed})
    for frame in result["frames"]:
        points = np.array(frame["nodes"])
        depths = np.interp(points[:, 0], [-2000, 0], [2000, 1000])
        assert np.all(points[:, 2] >= -depths-1e-7)
        td = frame["touchdown"]
        assert td[2] == pytest.approx(-np.interp(td[0], [-2000, 0], [2000, 1000]), abs=1e-7)


def test_flat_seabed_has_no_free_spans_and_supports_all_weight():
    result = span_analysis({"profile": [{"x_m": 0, "depth_m": 30}, {"x_m": 100, "depth_m": 30}],
                            "wet_weight_n_m": 7, "bottom_tension_n": 1000, "ei_n_m2": 1000})
    assert result["summary"]["span_count"] == 0
    assert result["summary"]["max_clearance_m"] == 0
    assert result["summary"]["total_vertical_reaction_n"] == pytest.approx(700, abs=1e-5)
    assert all(row["reaction_n"] >= -1e-7 for row in result["profile"])
    assert result["solver"]["converged"]


VALLEY = [{"x_m": 0, "depth_m": 10}, {"x_m": 50, "depth_m": 100}, {"x_m": 100, "depth_m": 10}]


def test_tension_span_matches_parabolic_force_equilibrium():
    result = span_analysis({"profile": VALLEY, "wet_weight_n_m": 10, "bottom_tension_n": 1000,
                            "ei_n_m2": 0, "nodes": 101})
    p = np.array(result["nodes"])
    exact = -10 - 10*p[:, 0]*(100-p[:, 0])/(2*1000)
    assert p[:, 2] == pytest.approx(exact, abs=1e-7)
    assert result["summary"]["span_count"] == 1
    assert result["solver"]["free_equilibrium_residual_n"] < 1e-6
    assert result["solver"]["vertical_balance_residual_n"] < 1e-6
    assert result["solver"]["contact_penetration_m"] == 0


def test_beam_span_mesh_refines_toward_simply_supported_solution():
    # EI z'''' = -w, endpoints pinned/free rotation: centre sag 5wL^4/(384EI).
    errors = []
    expected_sag = 5*10*100**4/(384*1e6)
    for nodes in (21, 41, 81):
        result = span_analysis({"profile": VALLEY, "wet_weight_n_m": 10, "bottom_tension_n": 0,
                                "ei_n_m2": 1e6, "nodes": nodes})
        sag = result["profile"][nodes//2]["cable_depth_m"]-10
        errors.append(abs(sag-expected_sag))
        assert result["summary"]["total_vertical_reaction_n"] == pytest.approx(1000, abs=1e-4)
        assert result["solver"]["converged"]
    assert errors[1] < .3*errors[0]
    assert errors[2] < .3*errors[1]


@pytest.mark.parametrize("function, config", [
    (catenary, {"depth_m": 0}),
    (catenary, {"wet_weight_n_m": -1}),
    (catenary, {"depth_m": float("nan")}),
    (catenary, {"depth_m": True}),
    (catenary, {"nodes": 3.5}),
    (catenary, {"cable_length_m": 2, "depth_m": 100}),
    (catenary, {"layback_m": 5, "bottom_tension_n": 20}),
    (steady_state, {"bottom_tension_n": 0}),
    (steady_state, {"current_x_m_s": float("inf")}),
    (span_analysis, {"profile": []}),
    (span_analysis, {"profile": [{"x_m": 0, "depth_m": 0}, {"x_m": 0, "depth_m": 1}]}),
])
def test_invalid_physics_inputs_rejected(function, config):
    with pytest.raises(ValueError):
        function(config)


@pytest.mark.parametrize("config", [
    {"duration_s": -1}, {"dt_s": 0}, {"nodes": 1000}, {"ea_n": 0},
    {"mass_kg_m": .0001}, {"duration_s": 7200, "dt_s": .02},
    {"duration_s": 200, "depth_m": 1, "nodes": 100, "payout_m_s": 20},
    {"ship_plan": [{"time_s": 1}, {"time_s": 0}]},
    {"current_profile": [{"depth_m": 1}, {"depth_m": 0}]},
])
def test_dynamic_invalid_or_unbounded_computation_rejected(config):
    with pytest.raises(ValueError):
        simulate_lay({}, config)


def test_all_models_emit_strict_json_for_boundary_cases():
    results = [catenary({"depth_m": .001, "bottom_tension_n": 1e9, "heading_deg": 36000}),
               catenary({"depth_m": 12000, "bottom_tension_n": 0}),
               steady_state({"depth_m": 1, "ship_speed_m_s": 0, "heading_deg": -36000}),
               simulate_lay({}, {"depth_m": 1, "duration_s": .02, "dt_s": .02,
                                 "ship_speed_m_s": 0, "payout_m_s": 0, "heading_deg": 36000}),
               span_analysis({"profile": [{"x_m": 0, "depth_m": 0}, {"x_m": 10, "depth_m": 0}],
                              "bottom_tension_n": 0, "ei_n_m2": 0})]
    for result in results:
        assert result["validation_status"] == "research"
        json.dumps(result, allow_nan=False)
