"""Independent real-state counterexamples for geographic/material preparation."""
from copy import deepcopy
import math

import numpy as np
import pytest
from pyproj import Transformer

from oceanroute.core import analyze_project
from oceanroute.geodesy import GEOD, WGS84_A
from oceanroute.plan_voyage import prepare_plan_voyage
from oceanroute.voyage import read_voyage_checkpoint, run_voyage


def project():
    return {"crs":"EPSG:4326","route":{"curve":"rhumb","slack_pct":2,"points":[
        {"id":"a","longitude":0.,"latitude":0.,"depth_m":10},
        {"id":"b","longitude":math.degrees(300/WGS84_A),"latitude":0.,"depth_m":10}],"legs":[{"cable_type_id":"A"}]},
        "cable_types":[{"id":"A","lay_speed_m_s":.5,"wet_weight_n_m":4,"diameter_m":.02,"cost_per_m":1,"ea_n":1e6,"ei_n_m2":0}],"bodies":[]}


def options(duration=2):
    return {"plan":{"bottom_tension_n":10,"sample_spacing_m":30},"duration_s":duration,
        "simulation":{"dt_s":.25,"internal_dt_s":.05},"voyage":{"adaptive_mesh":{"enabled":False},"chunk_duration_s":1}}


@pytest.mark.parametrize("change",["geometry","properties"])
def test_resume_with_a_changed_nonempty_project_rejects_old_mapping(change):
    p=project(); prepared=prepare_plan_voyage(p,options())
    first=run_voyage(p,{**prepared["config"],"duration_s":1})
    changed=deepcopy(p)
    if change=="geometry": changed["route"]["points"][-1]["latitude"]+=.001
    else: changed["cable_types"][0]["wet_weight_n_m"]=8
    with pytest.raises(ValueError,match="project.*planning|project.*mapping|project.*match"):
        run_voyage(changed,{"resume_state":first["checkpoint"],"duration_s":1})


@pytest.mark.parametrize("saved_project",["original","empty"])
def test_resume_with_original_project_or_explicit_saved_state_only_is_real(saved_project):
    p=project(); prepared=prepare_plan_voyage(p,options())
    first=run_voyage(p,{**prepared["config"],"duration_s":1})
    resumed=run_voyage(p if saved_project=="original" else {},{"resume_state":first["checkpoint"],"duration_s":1})
    full=run_voyage(p,prepared["config"])
    assert resumed["status"]=="completed" and resumed["plan_mapping"]==prepared["mapping"]
    np.testing.assert_allclose(resumed["checkpoint"]["physical_checkpoint"]["state"]["positions"],
        full["checkpoint"]["physical_checkpoint"]["state"]["positions"],atol=1e-9)
    assert resumed["summary"]["paid_out_m"]==pytest.approx(1.02)
    assert resumed["frames"][0]["node_material_m"][0]==pytest.approx(first["frames"][-1]["node_material_m"][0])
    read_voyage_checkpoint(resumed["checkpoint"])


def test_later_window_has_disjoint_prefix_initial_inventory_and_new_payout_with_point_loads():
    p=project()
    p["bodies"]=[{"id":name,"cable_kp_m":station,"length_m":0,"mass_kg":10,"wet_weight_n":30,"drag_area_m2":.1}
        for name,station in [("older",25),("initial",45),("future",55)]]
    prepared=prepare_plan_voyage(p,{**options(10),"start_time_s":100})
    mapping=prepared["mapping"]; actual=run_voyage(p,prepared["config"])
    assert mapping["manufacturing_origin_m"]>0
    assert mapping["manufacturing_origin_m"]+mapping["initial_natural_length_m"]==pytest.approx(mapping["initial_manufacturing_top_m"])
    assert actual["frames"][0]["node_material_m"][0]==pytest.approx(51)
    assert actual["frames"][0]["node_material_m"][-1]==pytest.approx(mapping["manufacturing_origin_m"])
    assert {b["id"] for b in prepared["config"]["simulation"]["inline_bodies"]}=={"initial","future"}
    initial={b["id"]:b for b in actual["frames"][0]["inline_bodies"]}
    final={b["id"]:b for b in actual["frames"][-1]["inline_bodies"]}
    assert initial["initial"]["deployed_mass_kg"]==10 and initial["future"]["deployed_mass_kg"]==0
    assert final["future"]["deployed_mass_kg"]==10
    assert actual["summary"]["paid_out_m"]==pytest.approx(5.1)
    assert actual["summary"]["natural_length_m"]==pytest.approx(mapping["initial_natural_length_m"]+5.1)
    assert actual["frames"][-1]["node_material_m"][0]==pytest.approx(56.1)


def test_route_body_location_after_allowance_matches_core_manufacturing_coordinate():
    p=project(); p["route"]["allowances"]=[{"kp_m":5,"length_m":2,"cable_type_id":"A"}]
    p["bodies"]=[{"id":"route-joint","kp_m":20,"length_m":0,"mass_kg":10,"wet_weight_n":30,"drag_area_m2":.1}]
    planned=analyze_project(p)
    body=planned["bodies"][0]
    assert body["start_m"]==pytest.approx(22.4)  # 20m * 1.02, plus the 2m allowance
    prepared=prepare_plan_voyage(p,options(25)); actual=run_voyage(p,prepared["config"])
    declared=prepared["config"]["simulation"]["inline_bodies"][0]
    assert declared["material_m"]==body["start_m"]
    assert actual["frames"][-1]["inline_bodies"][0]["deployed_mass_kg"]==10
    for frame in actual["frames"]:
        expected=prepared["mapping"]["initial_manufacturing_top_m"]+sum(
            (r["manufacturing_end_m"]-r["manufacturing_start_m"])*np.clip(
                (frame["time_s"]-r["local_start_s"])/(r["local_end_s"]-r["local_start_s"]),0,1)
            for r in prepared["mapping"]["instructions"])
        assert frame["node_material_m"][0]==pytest.approx(expected,abs=1e-7)


def test_actual_local_ship_positions_convert_back_across_date_line():
    p=project(); start=(179.9999,0)
    lon,lat,_=GEOD.fwd(*start,90,300)
    p["route"]["points"][0].update(longitude=start[0],latitude=start[1])
    p["route"]["points"][1].update(longitude=lon,latitude=lat)
    prepared=prepare_plan_voyage(p,options(4)); actual=run_voyage(p,prepared["config"])
    inverse=Transformer.from_crs(prepared["mapping"]["local_crs"],"EPSG:4326",always_xy=True)
    interval=prepared["mapping"]["instructions"][-1]
    end_lon,end_lat=inverse.transform(*interval["vessel_end_xy_m"])
    actual_lon,actual_lat=inverse.transform(*actual["frames"][-1]["ship"][:2])
    assert abs(GEOD.inv(actual_lon,actual_lat,end_lon,end_lat)[2])<1e-7
    assert actual_lon<0  # a short eastern move, not a 360-degree map-line jump
    # The two-metre displacement crosses the date line through a WGS84 AEQD
    # round trip; allow 0.1 micrometre for projection floating-point rounding.
    assert np.linalg.norm(actual["frames"][-1]["ship"][:2])==pytest.approx(2,abs=1e-7)


def test_initial_different_cable_names_with_identical_properties_share_real_stock_without_reset():
    p=project()
    p["route"]["points"].insert(1,{"id":"boundary","longitude":math.degrees(6/WGS84_A),"latitude":0.,"depth_m":10})
    p["route"]["legs"].append({"cable_type_id":"B"})
    other=deepcopy(p["cable_types"][0]); other["id"]="B"; p["cable_types"].append(other)
    prepared=prepare_plan_voyage(p,options(2))
    assert len(prepared["config"]["simulation"]["material_segments"])==2
    actual=run_voyage(p,prepared["config"])
    assert actual["frames"][0]["node_material_m"][0]==pytest.approx(prepared["mapping"]["initial_natural_length_m"])
    assert all(w==pytest.approx(4) for w in actual["frames"][0]["segment_wet_weight_n_m"])
    assert actual["summary"]["paid_out_m"]==pytest.approx(1.02)
