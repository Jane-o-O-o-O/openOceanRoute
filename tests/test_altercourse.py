"""Independent geometry/stock acceptance for explicit altercourse tools."""
from copy import deepcopy
import json
import math

import pytest
from geographiclib.geodesic import Geodesic
from scipy.integrate import quad

from oceanroute.altercourse import AltercourseError, split_altercourse, radius_altercourse
from oceanroute.constraints import ConstraintError, configure_constraints, edit_constrained_project, solve_constraints
from oceanroute.core import analyze_project, route_signature
from oceanroute.geodesy import GEOD, inverse
from oceanroute.route_geometry import route_segments
from oceanroute.tools import reverse_project, split_project, subdivide_project, merge_projects


def project(curve="rhumb", fixed=False, center=(0.,0.), turn=-90.):
    lon,lat=center
    # Independent endpoints from a declared WGS84 heading/distance.
    a=GEOD.fwd(lon,lat,270,2500)[:2]
    c=GEOD.fwd(lon,lat,90+turn,2500)[:2]
    p={"id":"ac-test","name":"转角研究","crs":"EPSG:4326","route":{"curve":curve,"mode":"fixed" if fixed else "flexible","slack_basis":"surface","slack_pct":1,
        "points":[{"id":"a","longitude":a[0],"latitude":a[1],"depth_m":50},{"id":"b","longitude":lon,"latitude":lat,"depth_m":50},{"id":"c","longitude":c[0],"latitude":c[1],"depth_m":60}],
        "legs":[{"cable_type_id":"A","mode":"fixed" if fixed else "flexible","fixed_cable_length_m":2600 if fixed else None,"stop_hours":2,"extra_cost":50,"burial":True},
                {"cable_type_id":"B","mode":"fixed" if fixed else "flexible","fixed_cable_length_m":2700 if fixed else None,"stop_hours":3,"extra_cost":70}]},
       "cable_types":[{"id":"A","cost_per_m":2,"lay_speed_m_s":1},{"id":"B","cost_per_m":5,"lay_speed_m_s":2}],"bodies":[]}
    analysis=analyze_project(p)
    p["profile"]={"samples":[{"kp_m":r["kp_m"],"depth_m":r["depth_m"]} for r in analysis["rpl"]],"route_signature":route_signature(p),"source":"synthetic_explicit_test_only","measured":False}
    p["side_slopes"]={"route_signature":route_signature(p),"test_marker":"old actual artifact retained"}
    return p


def angle(x): return (x+180)%360-180


def independent_tangent(segment, end):
    g=segment.geometry
    if g:
        p=segment.end if end else segment.start
        _,back,_=GEOD.inv(*g["center"],*p)
        return (back+180+math.copysign(90,g["sweep_deg"]))%360
    if segment.curve=="rhumb": return inverse(*segment.start,*segment.end,"rhumb")[1]
    initial,back,_=GEOD.inv(*segment.start,*segment.end)
    return (back+180)%360 if end else initial%360


@pytest.mark.parametrize("curve",["rhumb","geodesic"])
@pytest.mark.parametrize("center",[(0,0),(179.999,25),(45,73)])
def test_split_equal_true_turns_and_internal_kp_steps_preserves_original_path_sides(curve,center):
    p=project(curve,center=center)
    old=deepcopy(p)
    result=split_altercourse(p,{"point_id":"b","max_turn_angle_deg":31,"min_turn_distance_m":120})
    q=result["project"];segments=route_segments(q)
    turns=[angle(independent_tangent(b,False)-independent_tangent(a,True)) for a,b in zip(segments,segments[1:])]
    assert len(turns)==3
    assert max(map(abs,turns))<=31+1e-7
    assert max(turns)-min(turns)<1e-7
    for s in segments[1:-1]:
        # Native inverse independently reconstructs actual new leg distance.
        assert inverse(*s.start,*s.end,curve)[0]==pytest.approx(120,abs=1e-5)
    original=route_segments(p)
    assert angle(independent_tangent(original[0],False)-independent_tangent(segments[0],False))==pytest.approx(0,abs=1e-7)
    assert angle(independent_tangent(original[-1],True)-independent_tangent(segments[-1],True))==pytest.approx(0,abs=1e-7)
    assert q["route"]["points"][-2]["id"]=="b"
    assert q["profile"]==p["profile"] and q["side_slopes"]==p["side_slopes"]
    assert not analyze_project(q)["profile_metadata"]["imported_profile_valid"]
    assert all(x["depth_m"] is None for x in q["route"]["points"][1:-1])
    assert all(l["mode"]=="flexible" for l in q["route"]["legs"])
    assert sum(l.get("stop_hours",0) for l in q["route"]["legs"])==5
    assert sum(l.get("extra_cost",0) for l in q["route"]["legs"])==120
    assert p==old
    json.loads(json.dumps(result,allow_nan=False))


@pytest.mark.parametrize("curve",["rhumb","geodesic"])
@pytest.mark.parametrize("center",[(0,0),(179.999,50),(45,73)])
def test_radius_real_circle_true_tangencies_and_reduced_length_integral(curve,center):
    p=project(curve,center=center)
    q=radius_altercourse(p,{"point_id":"b","radius_m":300})["project"]
    segments=route_segments(q)
    arc=segments[1];g=arc.geometry
    assert len(q["route"]["points"])==4 and q["route"]["points"][2]["id"]=="b"
    for f in (0,.01,.25,.5,.9,1):
        point=arc.point_at_fraction(f)
        assert GEOD.inv(*g["center"],*point)[2]==pytest.approx(300,abs=1e-7)
    for a,b in zip(segments,segments[1:]):
        assert angle(independent_tangent(b,False)-independent_tangent(a,True))==pytest.approx(0,abs=1e-7)
    # Reference quadrature uses the native GeographicLib Jacobi field directly,
    # independently of RouteSegment's implementation/position inversion.
    reference=quad(lambda alpha:Geodesic.WGS84.Direct(g["center"][1],g["center"][0],math.degrees(alpha),300,Geodesic.ALL)["m12"],
                   math.radians(g["start_azimuth_deg"]),math.radians(g["start_azimuth_deg"]+g["sweep_deg"]),epsabs=1e-8)[0]
    assert arc.length_m==pytest.approx(abs(reference),abs=1e-7)
    assert analyze_project(q)["legs"][1]["surface_length_m"]==pytest.approx(abs(reference),abs=1e-7)


def test_split_noop_is_exact_copy_and_has_no_new_stock_or_signature():
    p=project(turn=-20)
    result=split_altercourse(p,{"point_id":"b","max_turn_angle_deg":30,"min_turn_distance_m":1e6})
    assert not result["report"]["changed"]
    assert result["project"]==p and result["project"] is not p
    assert result["report"]["manufacturing"]["physical_delta_m"]==0


@pytest.mark.parametrize("tool,config",[(split_altercourse,{"max_turn_angle_deg":30,"min_turn_distance_m":2000}),(radius_altercourse,{"radius_m":3000})])
def test_unreachable_short_legs_reject_and_leave_input_byte_semantics(tool,config):
    p=project();old=json.dumps(p,sort_keys=True)
    with pytest.raises(AltercourseError,match="SHORT_LEG"):
        tool(p,{"point_id":"b",**config})
    assert json.dumps(p,sort_keys=True)==old


@pytest.mark.parametrize("angle_value",[0,180,-1,181,True,float("nan"),10**1000])
def test_invalid_split_numeric_bounds_fail(angle_value):
    with pytest.raises(ValueError):
        split_altercourse(project(),{"point_id":"b","max_turn_angle_deg":angle_value,"min_turn_distance_m":100})


@pytest.mark.parametrize("tool,extra",[(split_altercourse,{"max_turn_angle_deg":30,"min_turn_distance_m":100}),(radius_altercourse,{"radius_m":100})])
def test_endpoints_zero_legs_nonrigid_unknown_config_and_reversal_rejected(tool,extra):
    p=project()
    with pytest.raises(ValueError,match="ENDPOINT"): tool(p,{"point_id":"a",**extra})
    with pytest.raises(ValueError): tool(p,{"point_id":"b",**extra,"secret":1})
    z=deepcopy(p);z["route"]["points"][0].update(longitude=0,latitude=0)
    with pytest.raises(ValueError,match="ZERO_LEG"): tool(z,{"point_id":"b",**extra})
    back=project(turn=180)
    with pytest.raises(ValueError,match="REVERSAL"):tool(back,{"point_id":"b",**extra})
    clamped=project(turn=0);clamped["route"]["points"][1]["constraint"]="clamped"
    with pytest.raises(ConstraintError,match="POINT_TYPE"): tool(clamped,{"point_id":"b",**extra})


def stocked_configured():
    p=project(fixed=True)
    # A real old marker divides the A interval and is not a geometric turn.
    a,b=p["route"]["points"][:2]
    lon,lat,_=GEOD.fwd(a["longitude"],a["latitude"],90,1000)
    marker={"id":"mark","longitude":lon,"latitude":lat,"depth_m":50}
    p["route"]["points"].insert(1,marker)
    p["route"]["legs"]=[{"cable_type_id":"A","mode":"fixed","fixed_cable_length_m":1040},
                          {"cable_type_id":"A","mode":"fixed","fixed_cable_length_m":1560},
                          {"cable_type_id":"B","mode":"fixed","fixed_cable_length_m":2700}]
    p.pop("profile");p.pop("side_slopes")
    p["route"]["allowances"]=[{"kp_m":900,"length_m":20,"cable_type_id":"B"}]
    p["bodies"]=[{"id":"replace","cable_kp_m":1200,"length_m":10,"cost":100},{"id":"extra","kp_m":3200,"length_m":8,"length_mode":"additional","cost":20}]
    p["assembly_references"]=[{"id":"ref","cable_kp_m":2000,"length_m":0,"kind":"reference"}]
    p["events"]=[{"id":"survey","kp_m":500},{"id":"turn-event","kp_m":2500}]
    return configure_constraints(p,{"mode":"fixed","points":[{"point_id":"mark","constraint":"clamped"}],
                                   "path_links":[{"point_id":"a"},{"point_id":"c"}]})["project"]


@pytest.mark.parametrize("tool,config",[(split_altercourse,{"max_turn_angle_deg":30,"min_turn_distance_m":100}),(radius_altercourse,{"radius_m":200})])
def test_fixed_stock_domain_material_boundary_bodies_allowances_references_events_not_recaptured(tool,config):
    p=stocked_configured();old=deepcopy(p);before=analyze_project(p)
    result=tool(p,{"point_id":"b",**config});q=result["project"];after=analyze_project(q)
    assert p==old
    assert q["route"]["constraint_state"]["manufacturing"]==p["route"]["constraint_state"]["manufacturing"]
    for l in p["route"]["path_links"]:
        assert next(x for x in q["route"]["path_links"] if x["id"]==l["id"])==l
    assert sum(x["slack_change"] for x in q["route"]["path_links"])==2
    assert after["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-7)
    assert {m["cable_type_id"]:m["length_m"] for m in after["materials"]}==pytest.approx({m["cable_type_id"]:m["length_m"] for m in before["materials"]},abs=1e-7)
    assert [b["cable_kp_m"] for b in after["bodies"]]==pytest.approx([b["cable_kp_m"] for b in before["bodies"]],abs=1e-7)
    assert after["assembly_references"][0]["cable_kp_m"]==2000
    rows={r["id"]:r for r in after["rpl"]}
    for transition in q["route"]["points"]:
        if transition.get("generated_constraint_transition"):
            assert rows[transition["id"]]["cable_kp_m"]==pytest.approx(2620,abs=1e-7)
    mark=next(x for x in q["route"]["points"] if x["id"]=="mark")
    assert mark["constraint"]=="clamped"
    # The old clamped fraction is preserved over the replacement old-anchor
    # interval, then explicitly re-bound to the containing new Rigid segment.
    oldfraction=next(x for x in p["route"]["points"] if x["id"]=="mark")["fraction"]
    assert rows["mark"]["kp_m"]==pytest.approx(oldfraction*rows["b"]["kp_m"],abs=1e-6)
    assert q["events"][0]["kp_m"]==pytest.approx(.2*rows["b"]["kp_m"],abs=1e-6)
    assert q["events"][1]["kp_m"]==pytest.approx(rows["b"]["kp_m"],abs=1e-6)
    solved=solve_constraints(q)["project"]
    assert analyze_project(solved)["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-6)
    forged=deepcopy(q)
    arcleg=next((l for l in forged["route"]["legs"] if l.get("geometry")),None)
    if arcleg:
        arcleg["geometry"]["radius_m"]+=1
        with pytest.raises(ValueError):analyze_project(forged)


def test_flexible_configured_targets_remain_flexible_and_quantity_recomputed():
    p=configure_constraints(project(),{"mode":"flexible"})["project"]
    old=deepcopy(p["route"]["constraint_state"]["manufacturing"])
    q=radius_altercourse(p,{"point_id":"b","radius_m":200})["project"]
    assert all(l["mode"]=="flexible" and l["slack_pct"]==pytest.approx(1) for l in q["route"]["legs"])
    assert q["route"]["constraint_state"]["manufacturing"]["physical_length_m"]<old["physical_length_m"]
    analyze_project(q)


def test_intrinsic_arc_subdivide_reverse_split_merge_preserve_lengths_and_shape():
    q=radius_altercourse(project(fixed=True),{"point_id":"b","radius_m":200})["project"]
    before=analyze_project(q)
    sub=subdivide_project(q,{"spacing_m":50})["project"]
    assert analyze_project(sub)["summary"]["surface_length_m"]==pytest.approx(before["summary"]["surface_length_m"],abs=1e-6)
    assert sum(s.length_m for s in route_segments(sub) if s.is_arc)==pytest.approx(route_segments(q)[1].length_m,abs=1e-6)
    reverse=reverse_project(q)["project"]
    assert analyze_project(reverse)["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-6)
    assert reverse_project(reverse)["project"]["route"]["legs"][1]["geometry"]["sweep_deg"]==pytest.approx(q["route"]["legs"][1]["geometry"]["sweep_deg"])
    kp=before["rpl"][1]["kp_m"]+route_segments(q)[1].length_m*.4
    parts=split_project(q,{"kp_m":kp})["projects"]
    merged=merge_projects(parts)["project"]
    assert analyze_project(merged)["summary"]["surface_length_m"]==pytest.approx(before["summary"]["surface_length_m"],abs=1e-6)
    assert analyze_project(merged)["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-6)


def test_solver_and_point_budgets_are_hard_rejection():
    with pytest.raises(ValueError,match="BUDGET"):
        radius_altercourse(project(),{"point_id":"b","radius_m":100,"max_solver_evaluations":1})
    with pytest.raises(ValueError,match="BUDGET"):
        split_altercourse(project(),{"point_id":"b","max_turn_angle_deg":.01,"min_turn_distance_m":.001,"max_generated_turns":1})
    with pytest.raises(ValueError,match="BUDGET"):
        radius_altercourse(project(),{"point_id":"b","radius_m":100,"max_work_units":1})


def test_arc_configure_clamped_fraction_and_sliding_actual_stock_station_use_true_arc():
    from oceanroute.tools import _insert_kps
    p=radius_altercourse(project(fixed=True),{"point_id":"b","radius_m":300})["project"]
    a=analyze_project(p)
    start=a["rpl"][1]["kp_m"];arc=route_segments(p)[1]
    p,inserted=_insert_kps(p,[start+arc.length_m*.3,start+arc.length_m*.7],a)
    cid,sid=[v["point_id"] for v in inserted]
    p=configure_constraints(p,{"mode":"fixed","points":[{"point_id":cid,"constraint":"clamped"},{"point_id":sid,"constraint":"sliding"}],
                              "path_links":[{"point_id":"a"},{"point_id":sid,"slack_change":False},{"point_id":"c"}]})["project"]
    before=analyze_project(p)
    oldstock=deepcopy(p["route"]["constraint_state"]["manufacturing"])
    source_arc=next(s for s in route_segments(p) if s.is_arc)
    # Same radius at all interpolated marker coordinates is checked directly.
    moved=edit_constrained_project(p,{"moves":[{"point_id":cid,"fraction":.2}]})["project"]
    mark=next(v for v in moved["route"]["points"] if v["id"]==cid)
    assert GEOD.inv(*source_arc.geometry["center"],mark["longitude"],mark["latitude"])[2]==pytest.approx(300,abs=1e-7)
    assert mark["fraction"]==pytest.approx(.2)
    assert moved["route"]["constraint_state"]["manufacturing"]==oldstock
    oldrow=next(v for v in before["rpl"] if v["id"]==sid)
    moved2=edit_constrained_project(moved,{"moves":[{"point_id":sid,"cable_kp_m":oldrow["cable_kp_m"]+15}]})["project"]
    row=next(v for v in analyze_project(moved2)["rpl"] if v["id"]==sid)
    assert row["cable_kp_m"]==pytest.approx(oldrow["cable_kp_m"]+15,abs=1e-6)
    assert moved2["route"]["constraint_state"]["manufacturing"]==oldstock
    assert analyze_project(moved2)["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-6)


def test_old_clamped_marker_inside_trimmed_corner_rebinds_onto_new_arc_not_chord():
    p=project(fixed=True)
    a,b=p["route"]["points"][:2]
    marker={"id":"near-b","longitude":a["longitude"]+.98*(b["longitude"]-a["longitude"]),"latitude":0.,"depth_m":50}
    p["route"]["points"].insert(1,marker)
    p["route"]["legs"]=[{"cable_type_id":"A","mode":"fixed","fixed_cable_length_m":2548},
                           {"cable_type_id":"A","mode":"fixed","fixed_cable_length_m":52},
                           {"cable_type_id":"B","mode":"fixed","fixed_cable_length_m":2700}]
    p.pop("profile");p.pop("side_slopes")
    p=configure_constraints(p,{"mode":"fixed","points":[{"point_id":"near-b","constraint":"clamped"}],"path_links":[{"point_id":"a"},{"point_id":"c"}]})["project"]
    q=radius_altercourse(p,{"point_id":"b","radius_m":300})["project"]
    mark=next(p for p in q["route"]["points"] if p["id"]=="near-b")
    arc=next(s for s in route_segments(q) if s.is_arc)
    assert GEOD.inv(*arc.geometry["center"],mark["longitude"],mark["latitude"])[2]==pytest.approx(300,abs=1e-7)
    assert mark["anchor_start_id"].startswith("altercourse-b") and mark["anchor_end_id"]=="b"
    before=analyze_project(p);after=analyze_project(q)
    assert after["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-6)


def test_flexible_bottom_slack_is_not_frozen_or_recertified_to_make_geometry_change_pass():
    p=project();p["route"]["slack_basis"]="bottom"
    before=deepcopy(p)
    with pytest.raises(ConstraintError,match="BOTTOM_REQUIRES_TERRAIN"):
        radius_altercourse(p,{"point_id":"b","radius_m":100})
    assert p==before


def test_large_number_of_turns_preflight_refuses_without_new_geometry_or_stock():
    p=project();old=deepcopy(p)
    with pytest.raises(ValueError,match="POINT_BUDGET"):
        split_altercourse(p,{"point_id":"b","max_turn_angle_deg":.01,"min_turn_distance_m":.001})
    assert p==old
    with pytest.raises(AltercourseError,match="POINT_BUDGET"):
        split_altercourse(p,{"point_id":"b","max_turn_angle_deg":5e-324,"min_turn_distance_m":.001})


@pytest.mark.parametrize("fixed",[True,False])
def test_true_http_shared_inventory_fixed_admitted_flexible_rejected_no_store_then_revision_save(tmp_path,fixed):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    from oceanroute.workspace import migrate_project,workspace_action
    p=stocked_configured() if fixed else project()
    p.pop("side_slopes",None)
    ws=migrate_project(p)["workspace"]
    pid=ws["active_path_id"]
    ws=workspace_action(ws,{"action":"copy_path","path_id":pid,"assembly_policy":"alternative"})["workspace"]
    original=deepcopy(ws)
    with TestClient(create_app(ProjectStore(tmp_path/"altercourse.sqlite3"))) as client:
        save=client.post("/api/workspaces",json=ws)
        assert save.status_code==200,save.text
        stored=save.json()["workspace"]
        preview=client.post("/api/workspace/altercourse-preview",json={"workspace":stored,"path_id":pid,"kind":"radius","config":{"point_id":"b","radius_m":200}})
        if not fixed:
            assert preview.status_code==422 and "WORKSPACE_SHARED_ASSEMBLY_CHANGED" in preview.text
        else:
            assert preview.status_code==200,preview.text
            candidate=preview.json()["workspace"]
            assert candidate["assemblies"]==stored["assemblies"]
            assert candidate["associations"]==stored["associations"]
            assert candidate["saved_revision"]==stored["saved_revision"]
            assert len(candidate["paths"][0]["project"]["route"]["points"])>len(stored["paths"][0]["project"]["route"]["points"])
        assert client.get(f"/api/workspaces/{stored['id']}").json()==stored
        if fixed:
            committed=client.post("/api/workspaces",json=candidate)
            assert committed.status_code==200,committed.text
            reopened=client.get(f"/api/workspaces/{stored['id']}").json()
            assert reopened==committed.json()["workspace"]
            assert reopened["saved_revision"]==stored["saved_revision"]+1
            assert reopened["assemblies"]==stored["assemblies"]
    assert ws==original


def test_fixed_non_slack_rigid_component_link_cannot_leave_its_frozen_body_geography():
    p=project(fixed=True)
    p["bodies"]=[{"id":"at-b","cable_kp_m":2600,"length_m":5}]
    p=configure_constraints(p,{"mode":"fixed","path_links":[{"point_id":"a"},
       {"point_id":"b","slack_change":False,"assembly_item_id":"at-b"},{"point_id":"c"}]})["project"]
    original=deepcopy(p)
    with pytest.raises(ConstraintError,match="ITEM_GEOGRAPHIC_MISMATCH"):
        radius_altercourse(p,{"point_id":"b","radius_m":300})
    assert p==original


def test_fixed_additional_body_link_uses_same_geography_not_postinsert_point_cable_station():
    p=project(fixed=True)
    kp=analyze_project(p)["rpl"][1]["kp_m"]
    p["bodies"]=[{"id":"at-b","kp_m":kp,"length_m":8,"length_mode":"additional"}]
    p=configure_constraints(p,{"mode":"fixed","path_links":[{"point_id":"a"},
        {"point_id":"b","cable_kp_m":2600,"slack_change":True,"assembly_item_id":"at-b"},{"point_id":"c"}]})["project"]
    before=deepcopy(p["route"]["constraint_state"]["manufacturing"])
    result=radius_altercourse(p,{"point_id":"b","radius_m":300})
    q=result["project"];after=analyze_project(q)
    row=next(r for r in after["rpl"] if r["id"]=="b")
    assert after["bodies"][0]["cable_kp_m"]==pytest.approx(2600,abs=1e-6)
    assert row["cable_kp_m"]==pytest.approx(2608,abs=1e-6)
    assert after["bodies"][0]["kp_m"]==pytest.approx(row["kp_m"],abs=1e-6)
    assert q["route"]["constraint_state"]["manufacturing"]==before
    placement=next(r for r in result["report"]["constraint_reconciliation"]["link_placements"] if r["point_id"]=="b")
    assert placement["frozen_cable_kp_m"]==2600
    assert placement["actual_postinsert_cable_kp_m"]==pytest.approx(2608,abs=1e-6)
    assert placement["assembly_route_kp_m"]==pytest.approx(placement["actual_route_kp_m"],abs=1e-6)


def test_non_slack_rigid_without_assembly_item_is_an_explicit_manufacturing_reference_with_offset():
    p=configure_constraints(project(fixed=True),{"mode":"fixed","path_links":[{"point_id":"a"},
        {"point_id":"b","slack_change":False},{"point_id":"c"}]})["project"]
    result=radius_altercourse(p,{"point_id":"b","radius_m":300})
    placement=next(r for r in result["report"]["constraint_reconciliation"]["link_placements"] if r["point_id"]=="b")
    assert placement["frozen_cable_kp_m"]==2600
    assert placement["base_station_offset_m"]>300
    assert placement["assembly_route_kp_m"] is None
    for oldlink in p["route"]["path_links"]:
        assert next(l for l in result["project"]["route"]["path_links"] if l["id"]==oldlink["id"])==oldlink
