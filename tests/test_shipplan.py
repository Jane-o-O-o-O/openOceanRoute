"""Plan conservation and actual numerical scenario/search verification."""
from copy import deepcopy
import json
import math

import numpy as np
import pytest

from oceanroute.core import analyze_project
from oceanroute.geodesy import WGS84_A
from oceanroute.shipplan import _metrics, build_ship_plan, look_ahead, optimize_tension
from oceanroute.simulation import simulate_lay


def simple_project(length=1000, depth=100, slack=2):
    return {"crs": "EPSG:4326", "route": {"curve": "rhumb", "slack_pct": slack,
             "points": [{"id": "p1", "longitude": 0, "latitude": 0, "depth_m": depth},
                        {"id": "p2", "longitude": math.degrees(length/WGS84_A), "latitude": 0, "depth_m": depth}],
             "legs": [{"cable_type_id": "A"}]},
            "cable_types": [{"id": "A", "lay_speed_m_s": 1, "wet_weight_n_m": 4, "diameter_m": .02,
                             "cost_per_m": 1, "ea_n": 1e8, "ei_n_m2": 0}], "bodies": []}


def test_constant_depth_straight_plan_conserves_distance_time_and_cable():
    project = simple_project()
    result = build_ship_plan(project, {"sample_spacing_m": 200})
    summary = result["summary"]
    assert summary["duration_s"] == pytest.approx(1000, abs=1e-5)
    assert summary["paid_out_m"] == pytest.approx(1020, abs=1e-6)
    assert summary["vessel_track_length_m"] == pytest.approx(1000, abs=1e-5)
    assert summary["material_balance_residual_m"] < 1e-6
    rows = result["instructions"]
    assert sum(r["payout_m_s"]*r["duration_s"] for r in rows) == pytest.approx(1020, abs=1e-6)
    assert all(r["payout_m_s"] == pytest.approx(1.02, abs=1e-7) for r in rows)
    assert all(r["heading_deg"] == pytest.approx(90, abs=1e-6) for r in rows)
    assert result["vessel_waypoints"][0]["ship_offset_m"][0] > 0
    assert result["ship_plan"][-1]["speed_m_s"] == 0
    assert result["ship_plan"][-1]["payout_m_s"] == 0
    json.dumps(result, allow_nan=False)


def test_mixed_cables_turn_offset_transitions_and_body_events_are_explicit():
    p = simple_project(500)
    p["route"]["points"].append({"id": "p3", "longitude": p["route"]["points"][-1]["longitude"],
                                   "latitude": .005, "depth_m": 150})
    p["route"]["legs"].append({"cable_type_id": "B", "stop_hours": .01})
    p["cable_types"].append({"id": "B", "lay_speed_m_s": .7, "wet_weight_n_m": 40,
                              "diameter_m": .1, "cost_per_m": 4})
    p["bodies"] = [{"id": "body", "name": "Repeater", "kp_m": 200, "length_m": 2,
                      "length_mode": "replace", "deployment_pause_s": 15}]
    result = build_ship_plan(p, {"sample_spacing_m": 200})
    instructions = result["instructions"]
    assert {r["cable_type_id"] for r in instructions if r["kind"] == "lay"} == {"A", "B"}
    transition = [r for r in instructions if r["kind"] == "offset_transition"]
    assert len(transition) == 1 and transition[0]["duration_s"] > 0
    assert transition[0]["kp_start_m"] == pytest.approx(transition[0]["kp_end_m"])
    assert transition[0]["payout_m_s"] == 0
    pauses = [r for r in instructions if r["speed_m_s"] == 0]
    assert sorted(r["duration_s"] for r in pauses) == pytest.approx([15, 36])
    for a, b in zip(instructions, instructions[1:]):
        assert a["end_time_s"] == pytest.approx(b["time_s"])
        assert a["vessel_end"] == pytest.approx(b["vessel_start"])
    assert result["summary"]["paid_out_m"] == pytest.approx(analyze_project(p)["summary"]["cable_length_m"], abs=1e-6)
    codes = {w["code"] for w in result["warnings"]}
    assert {"MIXED_PLAN_NOT_MIXED_DYNAMICS", "BODY_EVENT_APPROXIMATION"} <= codes


def test_additional_material_is_paid_at_declared_kp_and_counted_once():
    p = simple_project()
    p["bodies"] = [{"id": "extra", "kp_m": 200, "length_m": 4, "length_mode": "additional"}]
    p["route"]["allowances"] = [{"kp_m": 400, "length_m": 6}, {"length_m": 2}]
    p["route"]["legs"][0]["allowance_m"] = 3
    result = build_ship_plan(p, {"event_payout_m_s": .5})
    feed = [r for r in result["instructions"] if "feed" in r["kind"]]
    assert sum(r["payout_m_s"]*r["duration_s"] for r in feed) == pytest.approx(15)
    assert sum(r["duration_s"] for r in feed) == pytest.approx(30)
    assert result["summary"]["paid_out_m"] == pytest.approx(1035, abs=1e-6)
    assert result["summary"]["material_balance_residual_m"] < 1e-6
    assert all(r["kp_start_m"] == r["kp_end_m"] for r in feed)


def test_operator_pause_extra_payout_is_separate_from_assembly():
    result = build_ship_plan(simple_project(), {"events": [{"kp_m": 100, "duration_s": 5, "payout_m_s": .2}]})
    assert result["summary"]["assembly_length_m"] == pytest.approx(1020)
    assert result["summary"]["operator_extra_cable_m"] == pytest.approx(1)
    assert result["summary"]["paid_out_m"] == pytest.approx(1021)
    assert "OPERATOR_EXTRA_CABLE" in {w["code"] for w in result["warnings"]}


def test_missing_depth_preserves_unknown_offsets_instead_of_assuming_zero():
    p = simple_project(depth=None)
    result = build_ship_plan(p, {})
    assert all(point["ship_offset_m"] is None for point in result["vessel_waypoints"])
    assert all(point["depth_m"] is None for point in result["target_route"])
    assert "SHIP_OFFSET_UNAVAILABLE" in {w["code"] for w in result["warnings"]}


def test_sampling_budget_coarsens_but_keeps_events():
    result = build_ship_plan(simple_project(100000), {"sample_spacing_m": 1, "max_samples": 20,
                                                    "events": [{"kp_m": 12345, "duration_s": 1}]})
    assert result["summary"]["sample_count"] <= 20
    assert any(p["kp_m"] == pytest.approx(12345) for p in result["target_route"])
    assert result["summary"]["effective_sample_spacing_m"] > 1


def test_time_window_metrics_integrate_signal_independently_of_sample_density():
    # Signal T=2t+3. On [6,10], mean=19 and variance=4*(4^2)/12.
    def simulation(times):
        return {"frames": [{"time_s": t, "bottom_tension_n": 2*t+3} for t in times],
                "summary": {"max_top_tension_n": 1, "max_tension_n": 2, "minimum_bend_radius_m": 3,
                            "final_touchdown": [0,0,-1], "paid_out_m": 5}, "solver": {"converged": True}}
    coarse = _metrics(simulation([0,2,5,10]), 4)
    fine = _metrics(simulation(np.linspace(0,10,101)), 4)
    assert coarse["mean_bottom_tension_n"] == pytest.approx(19)
    assert coarse["bottom_tension_std_n"] == pytest.approx(math.sqrt(16/3))
    assert coarse["mean_bottom_tension_n"] == pytest.approx(fine["mean_bottom_tension_n"])
    assert coarse["bottom_tension_std_n"] == pytest.approx(fine["bottom_tension_std_n"])


LOCAL = {"duration_s": 10, "dt_s": 1, "depth_m": 100, "nodes": 12, "bottom_tension_n": 100,
         "ship_speed_m_s": .5, "payout_m_s": .5}


def test_lookahead_branches_share_initial_state_sampling_and_report_real_heave():
    result = look_ahead({}, {**LOCAL, "include_frames": True},
                        [{"name": "Base"}, {"name": "Current", "overrides": {"current_y_m_s": .3}},
                         {"name": "Heave", "overrides": {"heave_amplitude_m": .5, "heave_period_s": 4}}])
    branches = result["scenarios"]
    for branch in branches:
        assert branch["simulation"]["frames"][0]["nodes"] == result["initial_nodes"]
        assert [f["time_s"] for f in branch["simulation"]["frames"]] == [f["time_s"] for f in branches[0]["simulation"]["frames"]]
    assert result["summary"]["output_interval_s"] == pytest.approx(.2)
    assert abs(branches[1]["metrics"]["bottom_tension_change_n"]) > 1
    heave = branches[2]
    assert max(abs(frame["ship"][2]) for frame in heave["simulation"]["frames"]) > .4
    assert heave["metrics"]["peak_segment_tension_n"] > branches[0]["metrics"]["peak_segment_tension_n"]
    assert heave["metrics"]["minimum_bend_radius_m"] > 0
    json.dumps(result, allow_nan=False)


def test_lookahead_rejects_branch_that_changes_initial_heading():
    with pytest.raises(ValueError, match="initial cable geometry"):
        look_ahead({}, LOCAL, [{"name": "Base"}, {"name": "New initial heading", "overrides": {
            "ship_plan": [{"time_s": 0, "heading_deg": 0}]}}])


def test_numerical_payout_search_returns_measured_target_and_reproducible_config():
    c = {**LOCAL, "target_bottom_tension_n": 80, "payout_min_m_s": .3, "payout_max_m_s": .8,
         "max_evaluations": 9, "tension_tolerance_n": 1, "evaluation_window_s": 2.5, "include_frames": True}
    result = optimize_tension({}, c)
    assert result["summary"]["evaluation_count"] == 9
    assert result["target_met"]
    assert abs(result["error_n"]) < 1
    assert result["payout_m_s"] != pytest.approx(c["payout_m_s"])
    independent = simulate_lay({}, result["optimized_config"])
    measured = _metrics(independent, 2.5)
    assert result["achieved_bottom_tension_n"] == pytest.approx(measured["mean_bottom_tension_n"], abs=1e-9)
    assert result["error_n"] == pytest.approx(result["achieved_bottom_tension_n"]-80)
    baseline = next(x for x in result["evaluations"] if x["payout_m_s"] == c["payout_m_s"])
    assert abs(result["error_n"]) < abs(baseline["error_n"])
    assert all(x["solver_converged"] for x in result["evaluations"])
    json.dumps(result, allow_nan=False)


def test_unreachable_target_reports_failure_without_fabricating_achievement():
    result = optimize_tension({}, {**LOCAL, "target_bottom_tension_n": 1e7,
                                   "payout_min_m_s": .4, "payout_max_m_s": .8, "max_evaluations": 3})
    assert not result["target_met"]
    assert result["error_n"] < -1e6
    assert "TENSION_TARGET_NOT_MET" in {w["code"] for w in result["warnings"]}


def test_search_preserves_zero_payout_pauses_and_does_not_mutate_config():
    c = {**LOCAL, "target_bottom_tension_n": 80, "max_evaluations": 3,
         "ship_plan": [{"time_s": 2, "speed_m_s": 0, "payout_m_s": 0},
                       {"time_s": 4, "speed_m_s": .5, "payout_m_s": .5}]}
    original = deepcopy(c)
    result = optimize_tension({}, c)
    assert c == original
    assert result["optimized_config"]["ship_plan"][0]["payout_m_s"] == 0
    assert result["optimized_config"]["ship_plan"][1]["payout_m_s"] == result["payout_m_s"]


@pytest.mark.parametrize("config", [{"max_samples": 1}, {"sample_spacing_m": 0}, {"ship_speed_m_s": 0},
    {"events": [{"kp_m": 1001, "duration_s": 2}]}, {"events": [{"kp_m": 0, "duration_s": -1}]},
    {"bottom_tension_n": float("nan")}, {"body_pause_s": True}])
def test_plan_invalid_inputs(config):
    with pytest.raises(ValueError):
        build_ship_plan(simple_project(), config)


@pytest.mark.parametrize("config,scenarios", [
    ({}, []), ({}, [{"name": "A"}, {"name": "A"}]),
    ({}, [{"overrides": {"depth_m": 1}}]), ({"duration_s": 1000}, [{"name": "A"}]),
    ({"include_frames": 1}, [{"name": "A"}]),
    ({"dt_s": .001}, [{"name": "A"}]),
])
def test_lookahead_invalid_or_unbounded_inputs(config, scenarios):
    with pytest.raises(ValueError):
        look_ahead({}, config, scenarios)


@pytest.mark.parametrize("config", [{}, {"target_bottom_tension_n": -1},
    {"target_bottom_tension_n": 10, "max_evaluations": 100},
    {"target_bottom_tension_n": 10, "payout_min_m_s": 1, "payout_max_m_s": 1},
    {"target_bottom_tension_n": 10, "duration_s": 150},
    {"target_bottom_tension_n": 10, "ship_plan": [{"time_s": 1, "payout_m_s": True}]},
])
def test_optimization_invalid_or_unbounded_inputs(config):
    with pytest.raises(ValueError):
        optimize_tension({}, config)
