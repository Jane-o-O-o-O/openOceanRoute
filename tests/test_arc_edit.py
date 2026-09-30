"""Independent endpoint/radius and original manufacturing-domain acceptance."""
from copy import deepcopy
import json
import math

import pytest
from geographiclib.geodesic import Geodesic
from scipy.integrate import quad

from oceanroute.arc_edit import ArcEditError, edit_arc_project
from oceanroute.constraints import configure_constraints, edit_constrained_project, ConstraintError
from oceanroute.core import analyze_project, route_signature
from oceanroute.geodesy import GEOD


def circle_geometry(center, radius=300., start=0., sweep=90.):
    return {"type":"circular_arc", "schema_version":1, "center":list(center),
            "radius_m":radius, "start_azimuth_deg":start, "sweep_deg":sweep}


def arc_project(*, fixed=True, sweep=90., center=(0.,0.), arcs=1):
    g = circle_geometry(center, sweep=sweep)
    a = GEOD.fwd(*center, 0., 300.)[:2]
    b = GEOD.fwd(*center, sweep, 300.)[:2]
    coords, geometries = [a,b], [g]
    for i in range(1, arcs):
        c = GEOD.fwd(*coords[-1], 180., 400.+i*50.)[:2]
        start = GEOD.inv(*c,*coords[-1])[0] % 360
        end = GEOD.fwd(*c,start+90.,400.+i*50.)[:2]
        geometries.append(circle_geometry(c,400.+i*50.,start,90.))
        coords.append(end)
    p = {"schema_version":1,"id":"arc-edit-test","name":"独立圆弧编辑","crs":"EPSG:4326",
         "route":{"curve":"rhumb","mode":"fixed" if fixed else "flexible","slack_basis":"surface","slack_pct":1.,
                  "points":[{"id":f"p{i}","longitude":x,"latitude":y,"depth_m":50.+i} for i,(x,y) in enumerate(coords)],
                  "legs":[{"geometry":g,"cable_type_id":"A" if i==0 else "B", "mode":"fixed" if fixed else "flexible",
                           "fixed_cable_length_m":2000. if fixed else None,"stop_hours":i+1.,"extra_cost":10.+i} for i,g in enumerate(geometries)]},
         "cable_types":[{"id":"A","cost_per_m":2.},{"id":"B","cost_per_m":5.}],"bodies":[]}
    return p


def target(p, pid="p1", east=5., north=0.):
    point = next(x for x in p["route"]["points"] if x["id"]==pid)
    lon,lat = GEOD.fwd(point["longitude"],point["latitude"],90.,east)[:2]
    lon,lat = GEOD.fwd(lon,lat,0.,north)[:2]
    return {"point_id":pid,"longitude":lon,"latitude":lat}


def independent_arc_length(g):
    a = math.radians(g["start_azimuth_deg"])
    b = a+math.radians(g["sweep_deg"])
    return abs(quad(lambda alpha:Geodesic.WGS84.Direct(g["center"][1],g["center"][0],math.degrees(alpha),g["radius_m"],Geodesic.ALL)["m12"],a,b,epsabs=1e-8)[0])


@pytest.mark.parametrize("sweep",[90.,-90.,270.,-270.])
@pytest.mark.parametrize("center",[(0.,0.),(179.999,55.),(30.,78.)])
def test_directed_minor_and_major_actual_radius_length_and_fixed_inventory(sweep,center):
    p=arc_project(sweep=sweep,center=center);old=deepcopy(p)
    out=edit_arc_project(p,{"moves":[target(p,east=2.,north=1.)]})
    q=out["project"];g=q["route"]["legs"][0]["geometry"]
    assert (abs(g["sweep_deg"])<180)==(abs(sweep)<180)
    assert math.copysign(1,g["sweep_deg"])==math.copysign(1,sweep)
    for point in q["route"]["points"]:
        assert GEOD.inv(*g["center"],point["longitude"],point["latitude"])[2]==pytest.approx(300.,abs=1e-6)
    assert out["report"]["after"]["surface_length_m"]==pytest.approx(independent_arc_length(g),abs=1e-7)
    assert out["report"]["manufacturing"]["physical_delta_m"]==pytest.approx(0.,abs=1e-8)
    assert q["route"]["points"][1]["depth_m"] is None
    assert q["route"]["points"][0]["depth_m"]==50.
    assert [x["id"] for x in q["route"]["points"]]==["p0","p1"]
    assert p==old
    assert json.loads(json.dumps(out,allow_nan=False))["report"]["changed"]


def test_two_incident_arcs_rebuilt_and_nonincident_descriptor_exact():
    p=arc_project(arcs=3);old=deepcopy(p)
    out=edit_arc_project(p,{"moves":[target(p)]});q=out["project"]
    assert len(out["report"]["arc_changes"])==2
    assert q["route"]["legs"][2]["geometry"]==p["route"]["legs"][2]["geometry"]
    assert q["route"]["points"][2:]==p["route"]["points"][2:]
    assert [x["id"] for x in q["route"]["points"]]==[x["id"] for x in p["route"]["points"]]
    assert sum(l["stop_hours"] for l in q["route"]["legs"])==6.
    assert sum(l["extra_cost"] for l in q["route"]["legs"])==33.
    join=next(x for x in out["report"]["joins"] if x["point_id"]=="p1")
    def actual_tangent(g,point):
        return (GEOD.inv(*g["center"],point["longitude"],point["latitude"])[1]+180.+math.copysign(90.,g["sweep_deg"]))%360.
    incoming=actual_tangent(q["route"]["legs"][0]["geometry"],q["route"]["points"][1])
    outgoing=actual_tangent(q["route"]["legs"][1]["geometry"],q["route"]["points"][1])
    assert join["after"]["incoming_tangent_deg"]==pytest.approx(incoming,abs=1e-8)
    assert join["after"]["outgoing_tangent_deg"]==pytest.approx(outgoing,abs=1e-8)
    assert join["after"]["turn_deg"]==pytest.approx((outgoing-incoming+180.)%360.-180.,abs=1e-8)
    assert out["report"]["manufacturing"]["by_cable_type"]==[
        {"cable_type_id":"A","before_m":2000.,"after_m":2000.,"delta_m":0.},
        {"cable_type_id":"B","before_m":4000.,"after_m":4000.,"delta_m":0.}]
    assert p==old


def test_radius_override_and_explicit_branch_without_moving_coordinates():
    p=arc_project();out=edit_arc_project(p,{"arc_options":[{"start_point_id":"p0","end_point_id":"p1","radius_m":400.,"branch":"major"}]})
    assert out["report"]["moved_point_ids"]==[]
    assert out["project"]["route"]["points"]==p["route"]["points"]
    g=out["project"]["route"]["legs"][0]["geometry"]
    assert g["radius_m"]==400. and g["sweep_deg"]>180.
    assert out["report"]["arc_changes"][0]["evidence"]["original_radius_m"]==300.
    assert out["report"]["result_selection_point_id"]=="p0"


def test_unaffected_partitioned_arc_retains_exact_descriptors_and_unmoved_marker_depth():
    p=arc_project(arcs=3);oldleg=deepcopy(p["route"]["legs"][2]);g=oldleg["geometry"]
    lon,lat=GEOD.fwd(*g["center"],g["start_azimuth_deg"]+45.,g["radius_m"])[:2]
    p["route"]["points"].insert(3,{"id":"unchanged-marker","longitude":lon,"latitude":lat,"depth_m":111.})
    p["route"]["legs"][2]["geometry"]["sweep_deg"]=45.
    p["route"]["legs"][2]["fixed_cable_length_m"]=1000.
    last=deepcopy(oldleg);last["geometry"]["start_azimuth_deg"]+=45.;last["geometry"]["sweep_deg"]=45.;last["fixed_cable_length_m"]=1000.
    p["route"]["legs"].append(last)
    p=configure_constraints(p,{"mode":"fixed","points":[{"point_id":"unchanged-marker","constraint":"clamped"}]})["project"]
    q=edit_arc_project(p,{"moves":[target(p)]})["project"]
    assert [l["geometry"] for l in q["route"]["legs"][2:]]==[l["geometry"] for l in p["route"]["legs"][2:]]
    old_marker=next(x for x in p["route"]["points"] if x["id"]=="unchanged-marker")
    new_marker=next(x for x in q["route"]["points"] if x["id"]=="unchanged-marker")
    for key in ("longitude","latitude","depth_m"):
        assert new_marker[key]==old_marker[key]
    assert analyze_project(q)["summary"]["cable_length_m"]==6000.


def test_explicit_major_branch_alone_changes_real_curve_not_coordinates():
    p=arc_project()
    out=edit_arc_project(p,{"arc_options":[{"start_point_id":"p0","end_point_id":"p1","branch":"major"}]})
    assert out["report"]["changed"]
    assert out["project"]["route"]["points"]==p["route"]["points"]
    assert out["project"]["route"]["legs"][0]["geometry"]["sweep_deg"]>180.
    assert out["report"]["after"]["surface_length_m"]>3*out["report"]["before"]["surface_length_m"]-1e-5


def test_unchanged_coordinate_edit_is_exact_noop_without_signature_change():
    p=arc_project();move={k:p["route"]["points"][1][k] for k in ("longitude","latitude")};move["point_id"]="p1"
    out=edit_arc_project(p,{"moves":[move]})
    assert not out["report"]["changed"] and out["project"]==p
    assert not out["report"]["arc_changes"]


def test_full_circle_single_endpoint_reject_and_explicit_joint_move():
    p=arc_project(sweep=360.)
    with pytest.raises(ArcEditError,match="FULL_CIRCLE_NOT_CLOSED"):
        edit_arc_project(p,{"moves":[target(p)]})
    move=target(p,"p0");same={**move,"point_id":"p1"}
    with pytest.raises(ArcEditError,match="FULL_CIRCLE_POLICY_REQUIRED"):
        edit_arc_project(p,{"moves":[move,same]})
    out=edit_arc_project(p,{"moves":[move,same],"arc_options":[{"start_point_id":"p0","end_point_id":"p1","full_circle_policy":"preserve_endpoint_center_bearing"}]})
    g=out["project"]["route"]["legs"][0]["geometry"]
    assert g["sweep_deg"]==360. and g["center"]!=p["route"]["legs"][0]["geometry"]["center"]
    assert analyze_project(out["project"])["summary"]["cable_length_m"]==2000.


def configured_marker(*, sliding=False):
    p=arc_project()
    g=p["route"]["legs"][0]["geometry"]
    lon,lat=GEOD.fwd(*g["center"],45.,300.)[:2]
    p["route"]["points"].insert(1,{"id":"marker","longitude":lon,"latitude":lat,"depth_m":50.})
    p["route"]["legs"]=[{**p["route"]["legs"][0],"fixed_cable_length_m":1000.,"geometry":circle_geometry(g["center"],start=0.,sweep=45.)},
                           {**p["route"]["legs"][0],"fixed_cable_length_m":1000.,"geometry":circle_geometry(g["center"],start=45.,sweep=45.)}]
    p["route"]["allowances"]=[{"kp_m":100.,"length_m":20.,"cable_type_id":"B"}]
    p["bodies"]=[{"id":"signed-zero","cable_kp_m":700.,"length_m":0.,"mass_kg":5.,"wet_weight_n":-10.,"cost":7.},
                 {"id":"additional","kp_m":250.,"length_m":4.,"length_mode":"additional"}]
    p["assembly_references"]=[{"id":"ref","cable_kp_m":800.,"length_m":0.,"kind":"reference"}]
    p["events"]=[{"id":"event","kp_m":150.,"kind":"survey"}]
    out=configure_constraints(p,{"mode":"fixed","points":[{"point_id":"marker","constraint":"sliding" if sliding else "clamped"}],
         "path_links":[{"point_id":"p0"},{"point_id":"p1"},*([{"point_id":"marker","slack_change":False}] if sliding else [])]})["project"]
    a=analyze_project(out)
    out["profile"]={"route_signature":route_signature(out),"samples":[{"kp_m":x["kp_m"],"depth_m":50.} for x in a["rpl"]],"source":"independent_synthetic"}
    out["side_slopes"]={"route_signature":route_signature(out),"metadata":{"model":"old-test-binding"}}
    return out


def test_fixed_constraints_snapshot_allowances_signed_body_reference_events_and_stale_profile():
    p=configured_marker();old=deepcopy(p)
    out=edit_arc_project(p,{"moves":[target(p)]});q=out["project"]
    assert q["route"]["constraint_state"]["manufacturing"]==p["route"]["constraint_state"]["manufacturing"]
    assert q["route"]["path_links"]==p["route"]["path_links"]
    assert {x["id"] for x in q["route"]["points"]}>={x["id"] for x in p["route"]["points"]}
    assert q["bodies"][0]==p["bodies"][0]
    assert q["assembly_references"]==p["assembly_references"]
    assert q["profile"]==p["profile"] and q["side_slopes"]==p["side_slopes"]
    assert not analyze_project(q)["profile_metadata"]["imported_profile_valid"]
    assert out["report"]["manufacturing"]["physical_length_after_m"]==pytest.approx(2024.,abs=1e-7)
    assert next(x for x in q["route"]["points"] if x["id"]=="marker")["fraction"]==pytest.approx(.5,abs=1e-8)
    assert q["events"][0]["kp_m"]!=p["events"][0]["kp_m"]
    assert p==old


def test_old_constraint_editor_uses_identical_endpoint_service_and_clamped_fraction():
    p=configured_marker()
    move=target(p)
    direct=edit_arc_project(p,{"moves":[move]})
    classic=edit_constrained_project(p,{"moves":[move]})
    assert direct["project"]==classic["project"]
    combined=edit_constrained_project(p,{"moves":[move,{"point_id":"marker","fraction":.6}]})
    marker=next(x for x in combined["project"]["route"]["points"] if x["id"]=="marker")
    assert marker["fraction"]==pytest.approx(.6,abs=1e-9)
    assert combined["report"]["physical_length_delta_m"]==pytest.approx(0.,abs=1e-7)
    with pytest.raises(ConstraintError,match="RIGID_AUTOMOVE"):
        edit_constrained_project(p,{"moves":[move],"automatic":True})
    with pytest.raises(ArcEditError,match="POINT_TYPE"):
        edit_arc_project(p,{"moves":[target(p,"marker")]})


def test_old_fixed_sliding_station_uses_same_actual_arc_stock_solver():
    p=configured_marker(sliding=True)
    out=edit_constrained_project(p,{"moves":[target(p),{"point_id":"marker","cable_kp_m":1200.}]})
    assert next(x for x in out["project"]["route"]["path_links"] if x["point_id"]=="marker")["cable_kp_m"]==1200.
    assert analyze_project(out["project"])["summary"]["cable_length_m"]==pytest.approx(2024.,abs=1e-7)


@pytest.mark.parametrize("azimuth",[.45,89.55])
def test_old_clamped_coordinate_projection_finds_real_interior_near_either_endpoint(azimuth):
    p=configured_marker();g=p["route"]["legs"][0]["geometry"]
    target_xy=GEOD.fwd(*g["center"],azimuth,g["radius_m"])[:2]
    out=edit_constrained_project(p,{"moves":[{"point_id":"marker","longitude":target_xy[0],"latitude":target_xy[1]}]})
    marker=next(x for x in out["project"]["route"]["points"] if x["id"]=="marker")
    assert GEOD.inv(marker["longitude"],marker["latitude"],*target_xy)[2]<1e-5
    expected=independent_arc_length(circle_geometry(g["center"],g["radius_m"],0.,azimuth))/independent_arc_length(circle_geometry(g["center"],g["radius_m"],0.,90.))
    assert marker["fraction"]==pytest.approx(expected,abs=1e-8)


def test_linked_replace_body_cannot_be_moved_by_clamped_fraction_reconstruction():
    p=configured_marker();station=next(x for x in analyze_project(p)["rpl"] if x["id"]=="marker")["cable_kp_m"]
    p["bodies"].append({"id":"linked-zero","cable_kp_m":station,"length_m":0.,"wet_weight_n":-8.,"mass_kg":3.})
    p=configure_constraints(p,{"mode":"fixed","path_links":[{"point_id":"p0"},{"point_id":"p1"},
        {"point_id":"marker","slack_change":False,"assembly_item_id":"linked-zero"}]})["project"]
    old=deepcopy(p)
    with pytest.raises(ConstraintError,match="ITEM_GEOGRAPHIC_MISMATCH"):
        edit_constrained_project(p,{"moves":[target(p),{"point_id":"marker","fraction":.6}]})
    assert p==old


def test_linked_additional_body_leading_station_differs_from_postinsert_point_but_same_geographic_kp():
    p=configured_marker();a=analyze_project(p);marker=next(x for x in a["rpl"] if x["id"]=="marker")
    p["bodies"][1]["kp_m"]=marker["kp_m"]
    # Body leading CKP is 1,000 base + 20 allowance; its four metres occupy
    # the same route KP, while the RPL marker is after those four metres.
    p=configure_constraints(p,{"mode":"fixed","path_links":[{"point_id":"p0"},{"point_id":"p1"},
        {"point_id":"marker","cable_kp_m":1020.,"slack_change":False,"assembly_item_id":"additional"}]})["project"]
    old=deepcopy(p)
    out=edit_arc_project(p,{"moves":[target(p)]})
    q=out["project"];analysis=analyze_project(q)
    link=next(x for x in q["route"]["path_links"] if x["point_id"]=="marker")
    actual_marker=next(x for x in analysis["rpl"] if x["id"]=="marker")
    body=next(x for x in analysis["bodies"] if x["id"]=="additional")
    assert link["cable_kp_m"]==body["cable_kp_m"]==1020.
    assert actual_marker["cable_kp_m"]==pytest.approx(1024., rel=0, abs=1e-12)
    assert body["kp_m"]==pytest.approx(actual_marker["kp_m"],abs=1e-7)
    assert q["route"]["constraint_state"]["manufacturing"]==old["route"]["constraint_state"]["manufacturing"]
    assert p==old


@pytest.mark.parametrize("config",[
    {"moves":[],"surprise":1}, {"moves":[{"point_id":"p1","longitude":True,"latitude":0}]},
    {"moves":[{"point_id":"p1","longitude":10**1000,"latitude":0}]},
    {"moves":[{"point_id":"missing","longitude":0,"latitude":0}]},
    {"arc_options":[{"start_point_id":"p1","end_point_id":"p0","radius_m":300}]},
    {"arc_options":[{"start_point_id":"p0","end_point_id":"p1","radius_m":.0001}]},
    {"arc_options":[{"start_point_id":"p0","end_point_id":"p1","branch":"guess"}]},
    {"arc_options":[{"start_point_id":"p0","end_point_id":"p1","branch":[]}]},
    {"arc_options":[{"start_point_id":"p0","end_point_id":"p1","branch":{}}]},
    {"arc_options":[{"start_point_id":"p0","end_point_id":"p1","full_circle_policy":[]}]},
    {"arc_options":[{"start_point_id":"p0","end_point_id":"p1","full_circle_policy":{}}]},
])
def test_strict_bad_inputs_leave_source_untouched(config):
    p=arc_project();old=deepcopy(p)
    with pytest.raises(ValueError):edit_arc_project(p,config)
    assert p==old


def test_whole_budget_and_unreachable_edit_fail_closed():
    p=arc_project(arcs=2);old=deepcopy(p)
    with pytest.raises(ArcEditError,match="ARC_BUDGET"):edit_arc_project(p,{"moves":[target(p)],"max_arc_rebuilds":1})
    with pytest.raises(ArcEditError,match="WORK_LIMIT"):edit_arc_project(p,{"moves":[target(p)],"max_work_units":1})
    with pytest.raises(ArcEditError,match="OUTPUT_LIMIT"):edit_arc_project(p,{"moves":[target(p)],"max_output_bytes":1})
    with pytest.raises(ArcEditError,match="ENDPOINTS_TOO_FAR"):edit_arc_project(p,{"moves":[target(p,east=2000.)]})
    assert p==old
