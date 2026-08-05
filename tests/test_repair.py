"""Analytical recovery balance, actual tow loading and hydrostatic buoy sizing."""
import json

import numpy as np
import pytest

from oceanroute.repair import estimate_grapnel_rope, recovery_shape, size_buoy, steady_tow
from oceanroute.simulation import G, catenary, steady_state


RECOVERY={"depth_m":100,"wet_weight_n_m":4,"suspended_length_m":150,"heading_deg":90}
ROPE={"depth_m":100,"wet_weight_n_m":4,"diameter_m":.02,"ship_speed_m_s":1,
      "heading_deg":90,"grapnel_wet_weight_n":1000,"grapnel_friction_coefficient":.5,
      "grapnel_drag_area_m2":.2,"grapnel_drag_coefficient":1.2}
BUOY={"cable":RECOVERY,"buoy_mass_kg":50,"height_m":2,"freeboard_m":.5,"reserve_pct":20}


def test_recovery_exact_weight_top_bottom_force_balance_and_geometry():
    result=recovery_shape({**RECOVERY,"nodes":500})
    s=result["summary"]
    assert s["bottom_tension_n"]==pytest.approx(250)
    assert s["required_vertical_lift_n"]==pytest.approx(600)
    assert s["required_lift_tension_n"]==pytest.approx(650)
    assert s["top_tension_n"]**2==pytest.approx(250**2+600**2)
    assert result["end_forces"]["balance_residual_norm_n"]<1e-9
    nodes=np.array(result["nodes"])
    assert nodes[0]==pytest.approx([0,0,0])
    assert nodes[-1,2]==-100
    assert np.min(nodes[:,2])>=-100
    chords=np.linalg.norm(np.diff(nodes,axis=0),axis=1).sum()
    assert 0<=150-chords<.001


def test_retrieved_arc_subtraction_matches_remaining_length_and_changes_actual_tension():
    initial=recovery_shape({**RECOVERY,"suspended_length_m":180})
    shortened=recovery_shape({"depth_m":100,"wet_weight_n_m":4,
        "initial_suspended_length_m":180,"retrieved_length_m":30})
    direct=recovery_shape(RECOVERY)
    assert shortened["nodes"]==direct["nodes"]
    assert shortened["summary"]["retrieved_length_m"]==30
    assert shortened["summary"]["remaining_suspended_length_m"]==150
    assert shortened["summary"]["required_lift_tension_n"]<initial["summary"]["required_lift_tension_n"]
    assert shortened["summary"]["layback_m"]<initial["summary"]["layback_m"]


def test_vertical_recovery_limit_has_zero_horizontal_force_and_real_weight():
    result=recovery_shape({**RECOVERY,"suspended_length_m":100})
    assert result["summary"]["required_lift_tension_n"]==400
    assert result["summary"]["bottom_tension_n"]==0
    assert result["summary"]["minimum_bend_radius_m"] is None
    assert all(abs(p[0])+abs(p[1])==0 for p in result["nodes"])
    assert any(w["code"]=="VERTICAL_LIMIT" for w in result["warnings"])


def test_steady_tow_zero_drag_matches_existing_analytical_and_force_solver():
    config={"depth_m":100,"wet_weight_n_m":4,"bottom_tension_n":250,"ship_speed_m_s":0,"nodes":64}
    result=steady_tow(config)
    analytical=catenary({k:v for k,v in config.items() if k!="ship_speed_m_s"})
    existing=steady_state(config)
    assert np.array(result["nodes"])==pytest.approx(np.array(analytical["nodes"]),abs=1e-10)
    assert np.array(result["nodes"])==pytest.approx(np.array(existing["nodes"]),abs=1e-5)
    assert result["end_forces"]["required_top_support_n"]==pytest.approx([250,0,600],abs=1e-9)
    assert result["summary"]["suspended_length_m"]==150


def test_steady_tow_with_current_matches_existing_real_force_integration_and_invariant():
    config={"depth_m":100,"wet_weight_n_m":4,"diameter_m":.02,"bottom_tension_n":250,
            "ship_speed_m_s":1,"current_y_m_s":.3,"nodes":96}
    result=steady_tow(config)
    existing=steady_state(config)
    assert np.array(result["nodes"])==pytest.approx(np.array(existing["nodes"]),abs=1e-4)
    assert result["summary"]["suspended_length_m"]==pytest.approx(existing["summary"]["suspended_length_m"],abs=1e-4)
    # Pure normal drag does no tangential work: d|T|/ds=w dz/ds.
    assert result["summary"]["top_tension_n"]==pytest.approx(250+4*100,rel=1e-7)
    assert result["end_forces"]["balance_residual_norm_n"]<1e-6
    assert np.linalg.norm(result["end_forces"]["integrated_drag_n"])>10
    static=steady_tow({**config,"drag_coefficient":0})
    assert np.linalg.norm(np.array(result["nodes"])-np.array(static["nodes"]))>1


def test_steady_tow_relative_flow_is_galilean_invariant_and_cross_current_mirrors():
    common={"depth_m":100,"wet_weight_n_m":4,"bottom_tension_n":250,"heading_deg":90}
    moving=steady_tow({**common,"ship_speed_m_s":1,"current_x_m_s":.2,"current_y_m_s":.3})
    stationary=steady_tow({**common,"ship_speed_m_s":0,"current_x_m_s":-.8,"current_y_m_s":.3})
    assert np.array(moving["nodes"])==pytest.approx(np.array(stationary["nodes"]),abs=1e-9)
    mirror=steady_tow({**common,"ship_speed_m_s":0,"current_x_m_s":-.8,"current_y_m_s":-.3})
    expected=np.array(stationary["nodes"])*[1,-1,1]
    assert np.array(mirror["nodes"])==pytest.approx(expected,abs=1e-7)


def test_grapnel_force_and_contact_geometry_are_conditionally_balanced():
    result=estimate_grapnel_rope(ROPE)
    f=result["grapnel_forces"]
    assert f["rope_n"]==pytest.approx([500+123,0,0],abs=1e-9)
    assert f["drag_n"]==pytest.approx([-123,0,0],abs=1e-9)
    assert f["seabed_friction_n"]==pytest.approx([-500,0,0],abs=1e-9)
    assert f["seabed_normal_n"]==1000
    assert f["horizontal_balance_residual_norm_n"]<1e-9
    assert result["summary"]["minimum_suspended_rope_m"]>100
    assert result["rope_shape"]["summary"]["touchdown_tangent"][2]==0
    assert result["nodes"][-1][2]==pytest.approx(-100,abs=1e-7)
    assert result["summary"]["top_tension_n"]==pytest.approx(1023,rel=1e-7)


def test_grapnel_current_and_body_drag_change_real_rope_length_and_force_bearing():
    quiet=estimate_grapnel_rope({**ROPE,"grapnel_drag_area_m2":0,"drag_coefficient":0})
    loaded=estimate_grapnel_rope({**ROPE,"current_y_m_s":.3})
    assert loaded["summary"]["minimum_suspended_rope_m"]>quiet["summary"]["minimum_suspended_rope_m"]
    assert loaded["grapnel_forces"]["rope_n"][1]<0
    assert loaded["summary"]["bottom_rope_bearing_deg"]>90
    assert abs(loaded["nodes"][-1][1])>1


def test_insufficient_available_rope_is_not_reported_as_actual_bottom_contact():
    base=estimate_grapnel_rope(ROPE)["summary"]["minimum_suspended_rope_m"]
    short=estimate_grapnel_rope({**ROPE,"available_rope_length_m":base-1,"extra_rope_m":10})
    assert not short["summary"]["available_length_reaches_seabed"]
    assert short["summary"]["rope_shortfall_m"]==pytest.approx(11)
    assert any(w["code"]=="INSUFFICIENT_GRAPNEL_ROPE" for w in short["warnings"])
    allowance=estimate_grapnel_rope({**ROPE,"available_rope_length_m":base+5,"extra_rope_m":10})
    assert allowance["summary"]["available_length_reaches_seabed"]
    assert not allowance["summary"]["declared_allowance_met"]


def test_buoy_uses_vertical_end_force_not_total_cable_tension_for_archimedes():
    result=size_buoy({**BUOY,"horizontal_restraint_n":[250,0]})
    s=result["summary"]
    load=600+50*G
    assert s["vertical_cable_load_n"]==600
    assert s["total_vertical_load_n"]==pytest.approx(load)
    assert s["minimum_displacement_volume_m3"]==pytest.approx(load*1.2/(1025*G*.75))
    assert s["actual_displaced_water_volume_m3"]*1025*G==pytest.approx(load)
    assert s["vertical_force_residual_n"]==pytest.approx(0,abs=1e-8)
    assert s["horizontal_equilibrium_met"]
    assert s["complete_static_force_balance"]
    assert s["actual_freeboard_m"]>=.5
    assert s["waterplane_area_m2"]*s["height_m"]==pytest.approx(s["displacement_volume_m3"])


def test_buoy_payload_and_wet_rigging_are_separately_counted_and_density_changes_size():
    a=size_buoy({**BUOY,"above_water_payload_mass_kg":10,"rigging_wet_weight_n":20})
    assert a["summary"]["total_vertical_load_n"]==pytest.approx(600+60*G+20)
    b=size_buoy({**BUOY,"above_water_payload_mass_kg":10,"rigging_wet_weight_n":20,"water_density_kg_m3":1000})
    assert b["summary"]["minimum_displacement_volume_m3"]/a["summary"]["minimum_displacement_volume_m3"]==pytest.approx(1025/1000)


def test_under_capacity_buoy_has_no_fabricated_floating_draft_and_horizontal_restraint_remains_explicit():
    minimum=size_buoy(BUOY)["summary"]["minimum_displacement_volume_m3"]
    small=size_buoy({**BUOY,"displacement_volume_m3":minimum*.5})
    assert not small["summary"]["vertical_equilibrium_possible"]
    assert small["summary"]["draft_m"] is None
    assert small["summary"]["actual_freeboard_m"] is None
    assert small["summary"]["vertical_force_residual_n"]<0
    assert not small["summary"]["horizontal_equilibrium_met"]
    assert any(w["code"]=="BUOY_CANNOT_SUPPORT_LOAD" for w in small["warnings"])
    floats=size_buoy({**BUOY,"displacement_volume_m3":minimum*.9})
    assert floats["summary"]["vertical_equilibrium_possible"]
    assert not floats["summary"]["design_margin_and_freeboard_met"]


def test_buoy_static_tow_shape_changes_supported_load_under_actual_flow():
    cable={"depth_m":100,"wet_weight_n_m":4,"bottom_tension_n":250,"ship_speed_m_s":0}
    base={"cable_model":"tow","cable":cable,"buoy_mass_kg":50,"height_m":2,"freeboard_m":.5}
    calm=size_buoy(base)
    flowing=size_buoy({**base,"cable":{**cable,"current_x_m_s":-.5,"current_y_m_s":.2}})
    assert flowing["summary"]["vertical_cable_load_n"]!=pytest.approx(calm["summary"]["vertical_cable_load_n"],abs=.01)
    assert np.linalg.norm(flowing["cable_shape"]["end_forces"]["integrated_drag_n"])>1


@pytest.mark.parametrize("function,config",[
    (recovery_shape,{"depth_m":100,"suspended_length_m":99}),
    (recovery_shape,{"depth_m":100,"initial_suspended_length_m":150,"retrieved_length_m":51}),
    (recovery_shape,{"retrieved_length_m":1}),
    (recovery_shape,{**RECOVERY,"ship_speed_m_s":.1}),
    (recovery_shape,{**RECOVERY,"current_x_m_s":.1}),
    (recovery_shape,{**RECOVERY,"ei_n_m2":1}),
    (recovery_shape,{**RECOVERY,"material_segments":[{}]}),
    (recovery_shape,{**RECOVERY,"bottom_tension_n":250}),
    (recovery_shape,{**RECOVERY,"suspended_length_m":float("inf")}),
    (steady_tow,{"bottom_tension_n":0,"ship_speed_m_s":1}),
    (steady_tow,{"bottom_tension_n":1e9,"wet_weight_n_m":1e-6,"ship_speed_m_s":0}),
    (steady_tow,{"current_profile":[{}]}),
    (steady_tow,{"suspended_length_m":150}),
    (steady_tow,{"retrieval_speed_m_s":1}),
    (estimate_grapnel_rope,{}),
    (estimate_grapnel_rope,{**ROPE,"grapnel_wet_weight_n":-1}),
    (estimate_grapnel_rope,{**ROPE,"grapnel_mass_kg":1}),
    (estimate_grapnel_rope,{**ROPE,"ship_speed_m_s":0}),
    (estimate_grapnel_rope,{**ROPE,"bottom_tension_n":623}),
    (size_buoy,{**BUOY,"freeboard_m":2}),
    (size_buoy,{**BUOY,"buoy_mass_kg":True}),
    (size_buoy,{**BUOY,"support_force_n":[0,0,600]}),
    (size_buoy,{"support_force_n":[0,0,-1],"buoy_mass_kg":50,"height_m":2}),
    (size_buoy,{**BUOY,"horizontal_restraint_n":[0,float("nan")]}),
])
def test_incompatible_or_invalid_repair_assumptions_are_rejected(function,config):
    with pytest.raises(ValueError): function(config)


def test_all_repair_results_have_finite_json_and_research_status():
    for result in (recovery_shape(RECOVERY),steady_tow({"depth_m":30,"ship_speed_m_s":0,"bottom_tension_n":0}),
                   estimate_grapnel_rope(ROPE),size_buoy(BUOY)):
        assert result["validation_status"]=="research"
        json.dumps(result,allow_nan=False)


@pytest.fixture
def repair_api(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    return TestClient(create_app(ProjectStore(tmp_path/"repair-api.sqlite3")))


@pytest.mark.parametrize("kind,config",[
    ("recovery",RECOVERY),
    ("tow",{"depth_m":100,"bottom_tension_n":250,"ship_speed_m_s":1,"current_y_m_s":.3}),
    ("rope",{**ROPE,"available_rope_length_m":300,"extra_rope_m":10}),
    ("buoy",{**BUOY,"horizontal_restraint_n":[250,0]}),
])
def test_repair_endpoints_emit_real_force_balanced_finite_json(repair_api,kind,config):
    response=repair_api.post(f"/api/repair/{kind}",json={"config":config})
    assert response.status_code==200,response.text
    result=response.json()
    json.dumps(result,allow_nan=False)
    assert result["validation_status"]=="research"
    if kind=="buoy":
        assert result["summary"]["vertical_force_residual_n"]==pytest.approx(0,abs=1e-7)
        assert result["summary"]["complete_static_force_balance"]
    else:
        shape=result["rope_shape"] if kind=="rope" else result
        assert shape["nodes"][0]==pytest.approx([0,0,0],abs=1e-8)
        assert shape["nodes"][-1][2]==pytest.approx(-100,abs=1e-7)
        assert shape["end_forces"]["balance_residual_norm_n"]<1e-5
        assert "frames" not in result


@pytest.mark.parametrize("kind,config",[
    ("recovery",{**RECOVERY,"suspended_length_m":99}),
    ("tow",{"bottom_tension_n":0,"ship_speed_m_s":1}),
    ("rope",{**ROPE,"bottom_tension_n":123}),
    ("buoy",{**BUOY,"freeboard_m":2}),
])
def test_repair_endpoints_reject_impossible_or_conflicting_assumptions(repair_api,kind,config):
    response=repair_api.post(f"/api/repair/{kind}",json={"config":config})
    assert response.status_code==422,response.text
    assert response.json()["detail"]


def test_unknown_repair_tool_returns_404(repair_api):
    assert repair_api.post("/api/repair/dynamic",json={"config":ROPE}).status_code==404
