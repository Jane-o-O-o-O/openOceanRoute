"""True integrator state persistence, restart identity and future-state branches."""
from copy import deepcopy
import json
import shutil
import subprocess

import numpy as np
import pytest

from oceanroute.checkpoints import pack_checkpoint
from oceanroute.simulation import simulate_lay
from oceanroute.shipplan import _metrics, look_ahead, optimize_tension


BASE = {"depth_m":30,"bottom_tension_n":100,"nodes":8,"duration_s":4,
        "dt_s":.5,"internal_dt_s":.05,"ea_n":1e7,"wet_weight_n_m":4,
        "mass_kg_m":1,"diameter_m":.02,"ship_speed_m_s":.5,"payout_m_s":.6,
        "heading_deg":90,"current_y_m_s":.1}


def roundtrip(value):
    return json.loads(json.dumps(value,allow_nan=False))


def repack(checkpoint):
    return pack_checkpoint(checkpoint["config"],checkpoint["state"],checkpoint["time_s"],checkpoint["numerical"])


def equal_state(left,right):
    for field in ("positions","velocities","rest_lengths_m","node_material_m","node_mass_kg",
                  "node_dry_mass_kg","node_wet_weight_n","segment_ea_n","segment_ei_n_m2",
                  "last_segment_tensions_n","ship","anchor"):
        assert np.array(left["state"][field]) == pytest.approx(np.array(right["state"][field]),rel=1e-11,abs=1e-9)
    assert left["state"]["paid_out_m"] == pytest.approx(right["state"]["paid_out_m"],abs=1e-10)
    assert left["state"]["contact_mask"] == right["state"]["contact_mask"]
    assert left["state"]["plan"] == right["state"]["plan"]
    assert left["time_s"] == pytest.approx(right["time_s"],abs=1e-12)


def test_output_checkpoint_roundtrip_restarts_exact_actual_velocities_and_tension():
    original = simulate_lay({}, {**BASE,"save_checkpoints":True})
    saved = original["checkpoints"][3]
    assert saved["time_s"] == 1.5
    assert np.linalg.norm(saved["state"]["velocities"])>0
    resumed = simulate_lay({}, {"resume_state":roundtrip(saved),"duration_s":2.5})
    equal_state(original["checkpoint"],resumed["checkpoint"])
    assert resumed["frames"][0]["node_velocity_m_s"] == saved["state"]["velocities"]
    assert resumed["frames"][0]["node_material_m"] == saved["state"]["node_material_m"]
    assert resumed["summary"]["start_time_s"] == 1.5
    assert resumed["summary"]["interval_paid_out_m"] == pytest.approx(.6*2.5)
    assert resumed["summary"]["paid_out_m"] == pytest.approx(.6*4)
    assert resumed["solver"]["steps"] == original["solver"]["steps"]
    assert resumed["solver"]["steps_this_run"] < resumed["solver"]["steps"]


def test_checkpoint_survives_actual_javascript_parse_and_stringify_roundtrip():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable for cross-runtime JSON roundtrip")
    original=simulate_lay({}, {**BASE,"duration_s":1})["checkpoint"]
    process=subprocess.run([node,"-e","let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s))));"],
        input=json.dumps(original),capture_output=True,text=True,timeout=5,check=True)
    parsed=json.loads(process.stdout)
    assert isinstance(original["config"]["duration_s"],float)
    assert isinstance(parsed["config"]["duration_s"],int)
    resumed=simulate_lay({}, {"resume_state":parsed,"duration_s":1})
    continuous=simulate_lay({}, {**BASE,"duration_s":2})
    equal_state(continuous["checkpoint"],resumed["checkpoint"])


def test_arbitrary_saved_time_retains_turn_stop_heave_mixed_material_and_new_nodes():
    initial = simulate_lay({}, {**BASE,"duration_s":.1})["summary"]["initial_material_length_m"]
    material = {"wet_weight_n_m":4,"mass_kg_m":1,"diameter_m":.02,
                "ea_n":1e7,"ei_n_m2":10,"drag_coefficient":1.2}
    config = {**BASE,"duration_s":6,"payout_m_s":2,"heave_amplitude_m":.2,"heave_period_s":3,
              "ship_plan_horizon_s":6,"ship_plan":[
                  {"time_s":1.5,"heading_deg":70},
                  {"time_s":3,"speed_m_s":0,"payout_m_s":0},
                  {"time_s":4,"speed_m_s":.7,"heading_deg":105,"payout_m_s":2}],
              "material_segments":[{"start_m":0,"end_m":initial+2,**material},
                                   {"start_m":initial+2,"end_m":1000,**material,"wet_weight_n_m":8,"mass_kg_m":2}],
              "inline_bodies":[{"id":"R","material_m":initial+1,"length_m":2,
                                "mass_kg":10,"wet_weight_n":40,"drag_area_m2":.03}]}
    full = simulate_lay({}, {**config,"checkpoint_times_s":[2.375,4.25]})
    first = simulate_lay({}, {**config,"duration_s":2.375})
    equal_state(full["checkpoints"][0],first["checkpoint"])
    second = simulate_lay({}, {"resume_state":roundtrip(first["checkpoint"]),"duration_s":1.875})
    equal_state(full["checkpoints"][1],second["checkpoint"])
    third = simulate_lay({}, {"resume_state":roundtrip(second["checkpoint"]),"duration_s":1.75})
    equal_state(full["checkpoint"],third["checkpoint"])
    assert third["summary"]["final_nodes"]>BASE["nodes"]
    assert third["frames"][-1]["inline_bodies"][0]["deployed_fraction"] == pytest.approx(1)
    assert third["frames"][-1]["segment_wet_weight_n_m"][0]>4
    assert first["checkpoint"]["state"]["plan"][-1]["time_s"] == 4
    assert full["summary"]["paid_out_m"] == pytest.approx(10)


def test_snapshot_can_resume_after_model_project_defaults_change():
    original=simulate_lay({},BASE)
    altered_project={"cable_types":[{"id":"default","wet_weight_n_m":100,"diameter_m":.1}]}
    resumed=simulate_lay(altered_project,{"resume_state":original["checkpoint"],"duration_s":1})
    assert resumed["checkpoint"]["config"]["wet_weight_n_m"] == 4
    assert resumed["frames"][0]["nodes"] == original["frames"][-1]["nodes"]


def test_resume_scalar_control_and_relative_instruction_update_future_only():
    first=simulate_lay({}, {**BASE,"duration_s":1})
    changed=simulate_lay({}, {"resume_state":first["checkpoint"],"duration_s":2,
        "ship_plan":[{"time_s":0,"heading_deg":0,"speed_m_s":1,"payout_m_s":1},
                     {"time_s":1,"speed_m_s":0,"payout_m_s":0}]})
    assert changed["frames"][0]["nodes"]==first["frames"][-1]["nodes"]
    ship=np.array(changed["frames"][-1]["ship"])
    old=np.array(first["frames"][-1]["ship"])
    assert ship[:2]-old[:2] == pytest.approx([0,1],abs=1e-9)
    assert changed["summary"]["interval_paid_out_m"]==pytest.approx(1)
    scalar=simulate_lay({}, {"resume_state":first["checkpoint"],"duration_s":1,"payout_m_s":.2})
    assert scalar["summary"]["interval_paid_out_m"]==pytest.approx(.2)


def test_explicit_original_scalar_still_changes_active_event_command_on_resume():
    first=simulate_lay({}, {**BASE,"duration_s":1,
        "ship_plan":[{"time_s":.5,"payout_m_s":0}]})
    preserved=simulate_lay({}, {"resume_state":first["checkpoint"],"duration_s":1})
    reset=simulate_lay({}, {"resume_state":first["checkpoint"],"duration_s":1,"payout_m_s":.6})
    assert preserved["summary"]["interval_paid_out_m"]==0
    assert reset["summary"]["interval_paid_out_m"]==pytest.approx(.6)
    branches=look_ahead({}, {"resume_state":first["checkpoint"],"duration_s":1},[
        {"name":"Keep"},{"name":"Reset","overrides":{"payout_m_s":.6}}])
    assert branches["scenarios"][0]["metrics"]["paid_out_m"]==0
    assert branches["scenarios"][1]["metrics"]["paid_out_m"]==pytest.approx(.6)


def test_resumed_heave_change_has_continuous_position_and_preserves_saved_velocity():
    first=simulate_lay({}, {**BASE,"duration_s":1,"heave_amplitude_m":.5,"heave_period_s":4})
    changed=simulate_lay({}, {"resume_state":first["checkpoint"],"duration_s":1,
        "heave_amplitude_m":.2,"heave_period_s":3})
    assert changed["frames"][0]["nodes"]==first["frames"][-1]["nodes"]
    assert changed["frames"][0]["node_velocity_m_s"]==first["frames"][-1]["node_velocity_m_s"]
    assert changed["frames"][-1]["ship"][2] == pytest.approx(.5+.2*np.sin(2*np.pi/3))
    assert any(w["code"]=="RESUMED_HEAVE_CONTROL" for w in changed["warnings"])


@pytest.mark.parametrize("field,value",[("depth_m",31),("nodes",12),("dt_s",1),
    ("internal_dt_s",.1),("ea_n",2e7),("material_segments",[]),("inline_bodies",[]),
    ("seabed_profile",[[0,-30],[100,-30]]),("initial_suspended_material_m",2),
    ("seabed_friction",.7)])
def test_resume_rejects_changed_physics_boundary_or_numerical_grid(field,value):
    saved=simulate_lay({},BASE)["checkpoint"]
    with pytest.raises(ValueError,match="cannot change"):
        simulate_lay({}, {"resume_state":saved,"duration_s":1,field:value})


@pytest.mark.parametrize("case",["version","missing_velocity","missing_source","checksum","nan",
    "badshape","negative_length","anchor_velocity","false_material","false_contact","cursor","numerical"])
def test_invalid_actual_state_is_rejected_even_when_structural_cases_are_rechecksummed(case):
    saved=deepcopy(simulate_lay({},BASE)["checkpoint"])
    if case=="version": saved["schema_version"]=999
    elif case=="missing_velocity": del saved["state"]["velocities"]
    elif case=="missing_source": del saved["source"]
    elif case=="checksum": saved["state"]["positions"][1][2]+=.01
    elif case=="nan": saved["state"]["velocities"][1][0]=float("nan")
    elif case=="badshape": saved["state"]["velocities"].pop()
    elif case=="negative_length": saved["state"]["rest_lengths_m"][0]=-1
    elif case=="anchor_velocity": saved["state"]["velocities"][-1][0]=1
    elif case=="false_material": saved["state"]["node_mass_kg"][2]+=1
    elif case=="false_contact": saved["state"]["contact_mask"][-1]=False
    elif case=="cursor": saved["state"]["plan_index"]=999
    elif case=="numerical": saved["numerical"]["output_dt_s"]=.1
    if case not in {"version","missing_source","checksum","nan"}:
        saved=repack(saved)
    with pytest.raises(ValueError,match="checkpoint"):
        simulate_lay({}, {"resume_state":saved,"duration_s":1})


@pytest.mark.parametrize("config",[{"resume_state":None},{"save_checkpoints":1},
    {"checkpoint_times_s":[1,1]},{"checkpoint_times_s":[-1]},
    {"checkpoint_times_s":[5]},{"checkpoint_times_s":[True]},
    {"checkpoint_times_s":list(np.linspace(0,4,257))},
    {"duration_s":20,"dt_s":.05,"save_checkpoints":True}])
def test_invalid_save_input_or_excessive_volume_is_bounded(config):
    with pytest.raises(ValueError): simulate_lay({}, {**BASE,**config})


def test_checkpoint_lookahead_has_same_true_initial_state_and_distinct_future_controls():
    saved=simulate_lay({}, {**BASE,"duration_s":1.375})["checkpoint"]
    result=look_ahead({}, {"resume_state":roundtrip(saved),"duration_s":2,"include_frames":True},[
        {"name":"Keep"},{"name":"Turn","overrides":{"heading_deg":50,"payout_m_s":.3}},
        {"name":"Heave","overrides":{"heave_amplitude_m":.3,"heave_period_s":3}}])
    assert result["summary"]["start_time_s"]==1.375
    assert result["summary"]["end_time_s"]==3.375
    reference=result["scenarios"][0]["simulation"]
    for branch in result["scenarios"]:
        first=branch["simulation"]["frames"][0]
        assert first["nodes"]==saved["state"]["positions"]
        assert first["node_velocity_m_s"]==saved["state"]["velocities"]
        assert first["node_material_m"]==saved["state"]["node_material_m"]
        assert [f["time_s"] for f in branch["simulation"]["frames"]] == [f["time_s"] for f in reference["frames"]]
        assert branch["metrics"]["evaluation_start_s"]==2.875
        assert branch["checkpoint"]["time_s"]==3.375
        assert branch["metrics"]["peak_top_tension_n"]==branch["simulation"]["summary"]["interval_max_internal_top_tension_n"]
    assert result["scenarios"][1]["metrics"]["paid_out_m"]==pytest.approx(.6)
    assert result["scenarios"][1]["metrics"]["touchdown_shift_m"]>=0
    assert reference["frames"][-1]["ship"] != result["scenarios"][1]["simulation"]["frames"][-1]["ship"]
    json.dumps(result,allow_nan=False)


def test_lookahead_without_frames_still_returns_continueable_final_checkpoints():
    saved=simulate_lay({},BASE)["checkpoint"]
    result=look_ahead({}, {"resume_state":saved,"duration_s":1},[{"name":"Keep"}])
    branch=result["scenarios"][0]
    assert "simulation" not in branch
    continued=simulate_lay({}, {"resume_state":branch["checkpoint"],"duration_s":1})
    assert continued["summary"]["start_time_s"]==5


def test_checkpoint_payout_search_recomputes_achieved_tension_and_keeps_future_pause():
    first=simulate_lay({}, {**BASE,"duration_s":1,"ship_plan_horizon_s":5,
        "ship_plan":[{"time_s":2,"payout_m_s":0},{"time_s":3,"payout_m_s":.6}]})
    result=optimize_tension({}, {"resume_state":first["checkpoint"],"duration_s":3,
        "target_bottom_tension_n":80,"max_evaluations":5,"payout_min_m_s":.2,
        "payout_max_m_s":.8,"tension_tolerance_n":100})
    repeated=simulate_lay({},result["optimized_config"])
    achieved=_metrics(repeated,.75)["mean_bottom_tension_n"]
    assert achieved==pytest.approx(result["achieved_bottom_tension_n"],abs=1e-9)
    assert repeated["frames"][0]["node_velocity_m_s"]==first["frames"][-1]["node_velocity_m_s"]
    assert repeated["summary"]["interval_paid_out_m"]==pytest.approx(2*result["payout_m_s"])
