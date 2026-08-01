"""Material-coordinate loads, mixed transitions and true inline-body response."""
from copy import deepcopy
import json

import numpy as np
import pytest

from oceanroute.simulation import G, simulate_lay


BASE = {"duration_s": 5, "dt_s": 1, "depth_m": 50, "nodes": 12,
        "ship_speed_m_s": 0, "payout_m_s": 0, "wet_weight_n_m": 4,
        "mass_kg_m": 1, "diameter_m": .02, "bottom_tension_n": 100,
        "ea_n": 1e8, "ei_n_m2": 0, "drag_coefficient": 0}
PROPS = {k: BASE[k] for k in ("wet_weight_n_m", "mass_kg_m", "diameter_m", "ea_n", "ei_n_m2", "drag_coefficient")}


def interval(start, end, **overrides):
    return {"start_m": start, "end_m": end, **PROPS, **overrides}


def test_explicit_uniform_material_retains_homogeneous_solution():
    common = {**BASE, "ship_speed_m_s": .2, "payout_m_s": .2, "drag_coefficient": 1.2, "current_y_m_s": .1}
    before = simulate_lay({}, common)
    after = simulate_lay({}, {**common, "material_segments": [interval(0,1000,drag_coefficient=1.2)]})
    for a, b in zip(before["frames"], after["frames"]):
        assert np.array(a["nodes"]) == pytest.approx(np.array(b["nodes"]), abs=1e-9)
        assert a["top_tension_n"] == pytest.approx(b["top_tension_n"], abs=1e-7)
        assert a["node_mass_kg"] == pytest.approx(b["node_mass_kg"], abs=1e-10)
    assert before["summary"]["paid_out_m"] == after["summary"]["paid_out_m"]


def test_adjacent_identical_properties_are_physically_equivalent_to_one_interval():
    one = simulate_lay({}, {**BASE, "material_segments": [interval(0,1000)]})
    several = simulate_lay({}, {**BASE, "material_segments": [interval(0,20),interval(20,40),interval(40,1000)]})
    assert np.array(several["frames"][-1]["nodes"]) == pytest.approx(np.array(one["frames"][-1]["nodes"]), abs=1e-8)
    assert several["summary"]["max_tension_n"] == pytest.approx(one["summary"]["max_tension_n"], abs=1e-5)


def test_payout_crosses_material_boundary_and_changes_loads_not_only_labels():
    common = {**BASE, "duration_s": 12, "ship_speed_m_s": .5, "payout_m_s": 1,
              "drag_coefficient": 1.2, "current_y_m_s": .1}
    homogeneous = simulate_lay({}, common)
    initial = homogeneous["summary"]["initial_material_length_m"]
    boundary = initial+2
    mixed = simulate_lay({}, {**common, "material_segments": [interval(0,boundary,drag_coefficient=1.2),
        interval(boundary,1000,wet_weight_n_m=12,mass_kg_m=2,diameter_m=.04,ea_n=3e8,ei_n_m2=100,drag_coefficient=2)]})
    first, last = mixed["frames"][0], mixed["frames"][-1]
    assert first["segment_wet_weight_n_m"][0] == pytest.approx(4)
    assert last["segment_wet_weight_n_m"][0] > 4
    assert last["segment_ea_n"][0] > 1e8
    assert last["segment_diameter_m"][0] > .02
    assert last["node_material_m"][0] - first["node_material_m"][0] == pytest.approx(12, abs=1e-8)
    assert last["node_material_m"][-1] == 0
    end = mixed["summary"]["final_vessel_material_m"]
    expected_weight = 4*boundary + 12*(end-boundary)
    assert mixed["summary"]["final_total_wet_weight_n"] == pytest.approx(expected_weight, abs=1e-7)
    difference = np.linalg.norm(np.array(last["nodes"])-np.array(homogeneous["frames"][-1]["nodes"]))
    assert difference > .01
    assert mixed["solver"]["converged"]
    json.dumps(mixed, allow_nan=False)


def test_material_origin_translation_preserves_physics():
    at_zero = simulate_lay({}, {**BASE, "material_segments": [interval(0,1000)],
                                "inline_bodies": [{"material_m": 35, "wet_weight_n": 50, "mass_kg": 20}]})
    shifted = simulate_lay({}, {**BASE, "initial_suspended_material_m": 100,
                                "material_segments": [interval(100,1100)],
                                "inline_bodies": [{"material_m": 135, "wet_weight_n": 50, "mass_kg": 20}]})
    assert np.array(at_zero["frames"][-1]["nodes"]) == pytest.approx(np.array(shifted["frames"][-1]["nodes"]), abs=1e-8)
    assert np.array(shifted["frames"][-1]["node_material_m"])-100 == pytest.approx(at_zero["frames"][-1]["node_material_m"], abs=1e-8)


def test_body_mass_weight_and_first_moment_are_distributed_conservatively():
    cable = simulate_lay({}, BASE)
    loaded = simulate_lay({}, {**BASE,"inline_bodies": [{"id":"body","material_m":35,
        "length_m":1.2,"wet_weight_n":98,"mass_kg":20,"drag_area_m2":0}]})
    first = loaded["frames"][0]
    effective_body = 20 + (20-98/G)  # default isotropic added-mass coefficient=1
    increments = np.array(first["node_mass_kg"])-np.array(cable["frames"][0]["node_mass_kg"])
    assert increments.sum() == pytest.approx(effective_body, abs=1e-9)
    material_moment = np.sum(increments*np.array(first["node_material_m"]))/increments.sum()
    assert material_moment == pytest.approx(35.6, abs=1e-9)
    assert loaded["summary"]["final_total_dry_mass_kg"] == pytest.approx(cable["summary"]["final_total_dry_mass_kg"]+20, abs=1e-8)
    assert loaded["summary"]["final_total_wet_weight_n"] == pytest.approx(cable["summary"]["final_total_wet_weight_n"]+98, abs=1e-8)
    assert np.linalg.norm(np.array(loaded["frames"][-1]["nodes"])-np.array(cable["frames"][-1]["nodes"])) > .5


def test_body_mass_and_body_drag_independently_affect_dynamics():
    current = {**BASE, "current_y_m_s": .4, "drag_coefficient": 1.2}
    results = []
    for mass, area in ((10,0),(50,0),(10,.2)):
        results.append(simulate_lay({}, {**current, "inline_bodies": [{"material_m":35,"mass_kg":mass,
            "wet_weight_n":50,"drag_area_m2":area,"drag_coefficient":1.2}]}))
    nodes = [np.array(r["frames"][-1]["nodes"]) for r in results]
    assert np.linalg.norm(nodes[1]-nodes[0]) > .01
    assert np.linalg.norm(nodes[2]-nodes[0]) > .01


def test_finite_body_deploys_progressively_as_material_enters_at_vessel():
    common = {**BASE, "duration_s":6, "ship_speed_m_s":1, "payout_m_s":1}
    reference = simulate_lay({}, common)
    top = reference["summary"]["initial_vessel_material_m"]
    body = {"id":"new-body","material_m":top+1,"length_m":4,"mass_kg":10,"wet_weight_n":40}
    deployed = simulate_lay({}, {**common,"inline_bodies":[body]})
    for frame in deployed["frames"]:
        b = frame["inline_bodies"][0]
        expected = np.clip((frame["node_material_m"][0]-body["material_m"])/4,0,1)
        assert b["deployed_fraction"] == pytest.approx(expected, abs=1e-9)
        assert b["deployed_mass_kg"] == pytest.approx(10*expected, abs=1e-9)
        assert sum(frame["node_wet_weight_n"]) == pytest.approx(4*frame["material_length_m"]+40*expected, abs=1e-8)
    assert deployed["frames"][0]["inline_bodies"][0]["position"] is None
    assert deployed["frames"][-1]["inline_bodies"][0]["deployed_fraction"] == pytest.approx(1)
    assert deployed["summary"]["material_balance_residual_m"] < 1e-8


def test_pinned_bottom_body_contact_and_buoyant_body_remain_finite():
    bottom = simulate_lay({}, {**BASE,"inline_bodies":[{"id":"bottom-body","material_m":0,
        "wet_weight_n":50,"mass_kg":20}]})
    for frame in bottom["frames"]:
        body = frame["inline_bodies"][0]
        assert body["contact_fraction"] == pytest.approx(1)
        assert body["position"][2] == pytest.approx(-50)
        assert np.min(np.array(frame["nodes"])[:,2]) >= -50-1e-8
    floating = simulate_lay({}, {**BASE,"inline_bodies":[{"material_m":35,"wet_weight_n":-50,"mass_kg":10}]})
    reference = simulate_lay({}, BASE)
    assert floating["summary"]["final_total_wet_weight_n"] == pytest.approx(reference["summary"]["final_total_wet_weight_n"]-50)
    assert np.linalg.norm(np.array(floating["frames"][-1]["nodes"])-np.array(reference["frames"][-1]["nodes"])) > .2
    json.dumps(bottom,allow_nan=False)
    json.dumps(floating,allow_nan=False)


def test_heavy_near_bottom_body_settles_onto_seabed_through_actual_dynamics():
    result = simulate_lay({}, {**BASE,"dt_s":.5,
        "inline_bodies":[{"material_m":5,"wet_weight_n":1000,"mass_kg":200}]})
    first = result["frames"][0]["inline_bodies"][0]
    final = result["frames"][-1]["inline_bodies"][0]
    assert first["position"][2] > -50
    assert first["contact_fraction"] < 1
    assert final["position"][2] == pytest.approx(-50,abs=1e-9)
    assert final["contact_fraction"] == pytest.approx(1)
    for frame in result["frames"]:
        assert np.min(np.array(frame["nodes"])[:,2]) >= -50-1e-9
        assert all(tension>=0 for tension in frame["node_tension_n"])
    assert result["solver"]["converged"]


def test_body_loaded_dynamic_shape_is_sensitive_to_timestep_and_refines():
    arrays = []
    for h in (.1,.05,.025):
        result = simulate_lay({}, {**BASE,"internal_dt_s":h,"ship_speed_m_s":.1,"payout_m_s":.1,
            "current_y_m_s":.2,"drag_coefficient":1.2,
            "inline_bodies":[{"material_m":35,"wet_weight_n":50,"mass_kg":30,"drag_area_m2":.1}]})
        arrays.append(np.array(result["frames"][-1]["nodes"]))
        assert result["solver"]["converged"]
    coarse = np.linalg.norm(arrays[1]-arrays[0])
    fine = np.linalg.norm(arrays[2]-arrays[1])
    assert coarse > 1e-5
    assert fine < .8*coarse


@pytest.mark.parametrize("segments", [[],[interval(10,1000)], [interval(0,50)],
    [interval(0,20),interval(21,1000)], [interval(0,20),interval(19,1000)],
    [interval(20,1000),interval(0,20)], [interval(0,0)],
    [interval(0,1000,mass_kg_m=.001)], [interval(0,1000,ea_n=0)],
    [interval(0,1000,wet_weight_n_m=float("nan"))]])
def test_invalid_material_gaps_overlaps_or_properties_rejected(segments):
    with pytest.raises(ValueError):
        simulate_lay({}, {**BASE,"material_segments":segments})


@pytest.mark.parametrize("bodies", [None,[{}],[{"material_m":1,"mass_kg":1,"wet_weight_n":100}],
    [{"material_m":1,"mass_kg":10,"wet_weight_n":50,"length_m":-1}],
    [{"material_m":1,"mass_kg":float("inf"),"wet_weight_n":50}],
    [{"material_m":1,"mass_kg":10,"wet_weight_n":50,"length_m":5},
     {"material_m":3,"mass_kg":10,"wet_weight_n":50}],
    [{"id":"same","material_m":1,"mass_kg":10,"wet_weight_n":50},
     {"id":"same","material_m":3,"mass_kg":10,"wet_weight_n":50}],
    [{"material_m":1,"mass_kg":True,"wet_weight_n":50}]])
def test_invalid_inline_bodies_rejected(bodies):
    with pytest.raises(ValueError):
        simulate_lay({}, {**BASE,"inline_bodies":bodies})


def test_requested_payout_must_have_material_coverage_and_body_before_origin_is_rejected():
    baseline = simulate_lay({}, BASE)
    top = baseline["summary"]["initial_vessel_material_m"]
    with pytest.raises(ValueError,match="all requested payout"):
        simulate_lay({}, {**BASE,"payout_m_s":1,"material_segments":[interval(0,top+1)]})
    with pytest.raises(ValueError):
        simulate_lay({}, {**BASE,"initial_suspended_material_m":100,
            "inline_bodies":[{"material_m":99,"mass_kg":10,"wet_weight_n":50}]})


def test_explicit_material_and_body_inputs_are_not_mutated():
    c = {**BASE,"material_segments":[interval(0,1000)],
        "inline_bodies":[{"material_m":35,"mass_kg":20,"wet_weight_n":50}]}
    saved = deepcopy(c)
    simulate_lay({},c)
    assert c == saved
