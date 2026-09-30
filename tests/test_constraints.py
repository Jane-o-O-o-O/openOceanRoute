from copy import deepcopy
import json
import shutil
import subprocess

import pytest

from oceanroute.constraints import ConstraintError, configure_constraints, edit_constrained_project, solve_constraints
from oceanroute.core import analyze_project, route_signature
from oceanroute.geodesy import interpolate, inverse
from oceanroute.tools import reverse_project


def project(xs=(0,1,2,3,4),ys=None,types=None):
    ys=ys or [0]*len(xs)
    points=[{"id":f"p{i}","longitude":118+x*.01,"latitude":23+y*.01,"depth_m":50} for i,(x,y) in enumerate(zip(xs,ys))]
    result={"id":"test","name":"真实域约束","route":{"curve":"rhumb","mode":"flexible","slack_basis":"surface","slack_pct":1,
            "points":points,"legs":[{"cable_type_id":t,"burial":i==0} for i,t in enumerate(types or ["A"]*(len(xs)-1))]},
            "cable_types":[{"id":"A","cost_per_m":2,"lay_speed_m_s":1},{"id":"B","cost_per_m":5,"lay_speed_m_s":2}],
            "bodies":[],"events":[]}
    analysis=analyze_project(result)
    result["profile"]={"samples":[{"kp_m":p["kp_m"],"depth_m":50} for p in analysis["rpl"]],"route_signature":route_signature(result),"source":"survey","measured":True}
    return result


def ends(p):
    return [{"point_id":p["route"]["points"][0]["id"]},{"point_id":p["route"]["points"][-1]["id"]}]


def fixed(p,**config):
    return configure_constraints(p,{"mode":"fixed",**config})["project"]


def lengths(a):
    return {m["cable_type_id"]:m["length_m"] for m in a["materials"]}


def test_mode_switch_captures_lengths_material_body_and_profile_without_rearranging():
    p=project(types=["A","A","B","B"])
    p["bodies"]=[{"id":"repeat","kp_m":1200,"length_m":12,"cost":300}]
    old=deepcopy(p);before=analyze_project(p)
    configured=configure_constraints(p,{"mode":"fixed"})
    after=analyze_project(configured["project"])
    assert p==old
    for key in ("surface_length_m","bottom_length_m","cable_length_m","material_cost","material_length_m","body_length_m"):
        assert after["summary"][key]==pytest.approx(before["summary"][key],abs=1e-6)
    assert configured["report"]["domain_count"]==2
    assert configured["project"]["profile"]==p["profile"]
    flex=configure_constraints(configured["project"],{"mode":"flexible"})["project"]
    assert analyze_project(flex)["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-6)
    json.dumps(configured,allow_nan=False)


def test_fixed_domain_across_bends_redistributes_uniformly_and_outside_domain_stays_same():
    p=project()
    p=fixed(p,path_links=[{"point_id":"p0"},{"point_id":"p2"},{"point_id":"p4"}])
    before=analyze_project(p)
    result=edit_constrained_project(p,{"moves":[{"point_id":"p1","longitude":118.01,"latitude":23.01}]})
    after=analyze_project(result["project"])
    assert after["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-6)
    assert after["legs"][0]["surface_slack_pct"]==pytest.approx(after["legs"][1]["surface_slack_pct"],abs=1e-8)
    assert [l["cable_length_m"] for l in after["legs"][2:]]==pytest.approx([l["cable_length_m"] for l in before["legs"][2:]],abs=1e-6)
    assert result["report"]["domains"][0]["changed"]
    assert not result["report"]["domains"][1]["changed"]
    assert result["report"]["domains"][0]["shortage_m"]>900
    assert any(w["code"]=="CONSTRAINT_DOMAIN_SHORTAGE" for w in result["warnings"])
    # Rigid points outside the user's explicit move never move automatically.
    for pid in ("p0","p2","p3","p4"):
        old=next(x for x in p["route"]["points"] if x["id"]==pid)
        new=next(x for x in result["project"]["route"]["points"] if x["id"]==pid)
        assert (new["longitude"],new["latitude"])==(old["longitude"],old["latitude"])


def test_clamped_rubberbands_exact_selected_curve_and_link_changes_adjacent_domains_only():
    p=project(xs=(0,.25,1,2,3))
    p=fixed(p,points=[{"point_id":"p1","constraint":"clamped"}],
            path_links=[{"point_id":"p0"},{"point_id":"p1"},{"point_id":"p3"},{"point_id":"p4"}])
    old=analyze_project(p)
    result=edit_constrained_project(p,{"moves":[{"point_id":"p2","longitude":118.01,"latitude":23.01}]})
    pts={x["id"]:x for x in result["project"]["route"]["points"]}
    expected=interpolate(118,23,118.01,23.01,.25,"rhumb")
    assert inverse(pts["p1"]["longitude"],pts["p1"]["latitude"],*expected)[0]<.0001
    assert pts["p1"]["fraction"]==pytest.approx(.25)
    assert [d["changed"] for d in result["report"]["domains"]]==[True,True,False]
    assert result["report"]["physical_length_delta_m"]==pytest.approx(0,abs=1e-6)
    assert analyze_project(result["project"])["legs"][-1]["cable_length_m"]==pytest.approx(old["legs"][-1]["cable_length_m"],abs=1e-6)


def test_fixed_sliding_type_transition_crosses_unlinked_bend_and_keeps_type_lengths():
    p=project(xs=(0,.8,1,2),types=["A","B","B"])
    p=fixed(p,points=[{"point_id":"p1","constraint":"sliding"}],path_links=[*ends(p),{"point_id":"p1","slack_change":False}])
    before=analyze_project(p)
    station=next(l for l in p["route"]["path_links"] if l["point_id"]=="p1")["cable_kp_m"]
    result=edit_constrained_project(p,{"moves":[{"point_id":"p2","longitude":118.004,"latitude":23}]})
    pts=result["project"]["route"]["points"]
    assert [x["id"] for x in pts]==["p0","p2","p1","p3"]
    after=analyze_project(result["project"])
    assert next(x for x in after["rpl"] if x["id"]=="p1")["cable_kp_m"]==pytest.approx(station,abs=1e-5)
    assert lengths(after)==pytest.approx(lengths(before),abs=1e-5)
    assert max(l["surface_slack_pct"] for l in after["legs"])-min(l["surface_slack_pct"] for l in after["legs"])<1e-8
    assert next(x for x in pts if x["id"]=="p1")["anchor_start_id"]=="p2"
    again=edit_constrained_project(result["project"],{"moves":[{"point_id":"p2","longitude":118.005,"latitude":23.003}]})
    assert lengths(analyze_project(again["project"]))==pytest.approx(lengths(before),abs=1e-5)


def test_unlinked_rigid_transition_generates_manufacturing_sliding_cut_after_asymmetric_edit():
    p=project(xs=(0,1,2),types=["A","B"])
    p=fixed(p,path_links=ends(p))
    before=analyze_project(p)
    result=edit_constrained_project(p,{"moves":[{"point_id":"p1","longitude":118.004,"latitude":23.01}]})
    assert any(x.get("generated_constraint_transition") for x in result["project"]["route"]["points"])
    assert lengths(analyze_project(result["project"]))==pytest.approx(lengths(before),abs=1e-5)
    # Repeated solver use conserves the same baseline manufacture.
    next_result=edit_constrained_project(result["project"],{"moves":[{"point_id":"p1","longitude":118.006,"latitude":23.01}]})
    assert lengths(analyze_project(next_result["project"]))==pytest.approx(lengths(before),abs=1e-5)


def test_finite_components_and_allowance_jumps_preserve_exact_manufacturing_accounting():
    p=project(xs=(0,1,2),types=["A","B"])
    p["route"]["allowances"]=[{"kp_m":700,"length_m":30,"cable_type_id":"B"}]
    p["route"]["legs"][1]["allowance_m"]=9
    p["bodies"]=[{"id":"rep","kp_m":300,"length_m":12,"cost":40},
                 {"id":"added","kp_m":1500,"length_m":5,"cost":20,"length_mode":"additional"}]
    p=fixed(p,path_links=ends(p))
    before=analyze_project(p)
    result=edit_constrained_project(p,{"moves":[{"point_id":"p1","longitude":118.004,"latitude":23.008}]})
    after=analyze_project(result["project"])
    for key in ("cable_length_m","material_length_m","body_length_m","allowance_length_m","material_cost","body_cost"):
        assert after["summary"][key]==pytest.approx(before["summary"][key],abs=1e-5),key
    assert lengths(after)==pytest.approx(lengths(before),abs=1e-5)
    assert {b["id"]:b["cable_kp_m"] for b in after["bodies"]}==pytest.approx({b["id"]:b["cable_kp_m"] for b in before["bodies"]},abs=1e-5)
    assert not any(opt.get("allowance_m",0) for opt in result["project"]["route"]["legs"])


def test_partial_profile_preserves_geographically_unchanged_suffix_and_invalidates_changed_curve():
    p=fixed(project())
    old=analyze_project(p)
    result=edit_constrained_project(p,{"moves":[{"point_id":"p1","longitude":118.01,"latitude":23.01}]})
    after=analyze_project(result["project"])
    assert after["profile_metadata"]["imported_profile_valid"]
    assert after["legs"][0]["bottom_length_m"] is None
    assert after["legs"][1]["bottom_length_m"] is None
    for i in (2,3):
        assert after["legs"][i]["bottom_length_m"]==pytest.approx(old["legs"][i]["bottom_length_m"],abs=1e-5)
    assert not after["profile_metadata"]["measured"]


def test_flexible_sliding_behaves_as_clamped_and_unlinked_body_keeps_physical_station():
    p=project(xs=(0,.4,1))
    p["bodies"]=[{"id":"rep","cable_kp_m":200,"length_m":10,"cost":3}]
    p=configure_constraints(p,{"mode":"flexible","points":[{"point_id":"p1","constraint":"sliding"}]})["project"]
    before=analyze_project(p)
    result=edit_constrained_project(p,{"moves":[{"point_id":"p2","longitude":118.02,"latitude":23.01}]})
    after=analyze_project(result["project"])
    point=result["project"]["route"]["points"][1]
    expected=interpolate(118,23,118.02,23.01,.4,"rhumb")
    assert inverse(point["longitude"],point["latitude"],*expected)[0]<.001
    assert all(l["surface_slack_pct"]==pytest.approx(1,abs=1e-8) for l in after["legs"])
    assert after["summary"]["cable_length_m"]>before["summary"]["cable_length_m"]
    assert after["bodies"][0]["cable_kp_m"]==200


def test_clamped_manual_drag_projects_to_actual_curve_and_fraction_move_changes_link_domains():
    p=project(xs=(0,.4,1))
    p=fixed(p,points=[{"point_id":"p1","constraint":"clamped"}],path_links=[{"point_id":"p0"},{"point_id":"p1"},{"point_id":"p2"}])
    result=edit_constrained_project(p,{"moves":[{"point_id":"p1","longitude":118.006,"latitude":23.005}]})
    point=result["project"]["route"]["points"][1]
    assert point["latitude"]==pytest.approx(23,abs=1e-9)
    assert point["fraction"]==pytest.approx(.6,abs=1e-4)
    assert all(d["changed"] for d in result["report"]["domains"])
    assert result["report"]["physical_length_delta_m"]==pytest.approx(0,abs=1e-5)


def test_solve_without_edits_preserves_assembly_and_survey_source():
    p=fixed(project())
    before=analyze_project(p)
    result=solve_constraints(p)
    after=analyze_project(result["project"])
    assert result["report"]["invalid_profile_ranges_m"]==[]
    assert not any(d["changed"] for d in result["report"]["domains"])
    assert after["profile_metadata"]["measured"]
    assert after["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-6)


def test_core_rejects_plain_coordinate_and_material_patches_instead_of_bypassing_constraints():
    p=fixed(project())
    bad=deepcopy(p);bad["route"]["points"][1]["latitude"]+=.001
    with pytest.raises(ConstraintError,match="CONSTRAINT_UNSOLVED_EDIT"):
        analyze_project(bad)
    bad=deepcopy(p);bad["route"]["legs"][0]["fixed_cable_length_m"]+=100
    with pytest.raises(ConstraintError,match="CONSTRAINT_UNSOLVED_EDIT"):
        analyze_project(bad)
    with pytest.raises(ValueError,match="CONSTRAINT_TRANSFORM_UNSUPPORTED"):
        reverse_project(p)


def test_conflicting_domains_or_offcurve_markers_are_rejected():
    p=project(xs=(0,.4,1),ys=(0,.1,0))
    with pytest.raises(ConstraintError,match="CONSTRAINT_ALTERCOURSE"):
        fixed(p,points=[{"point_id":"p1","constraint":"clamped"}])
    p=project(xs=(0,.4,1))
    with pytest.raises(ConstraintError,match="CONSTRAINT_SLIDING_BOUNDARY"):
        fixed(p,points=[{"point_id":"p1","constraint":"sliding"}],path_links=[*ends(p),{"point_id":"p1","slack_change":True}])
    with pytest.raises(ConstraintError,match="CONSTRAINT_ENDPOINT_LINK"):
        fixed(p,path_links=[{"point_id":"p0"}])
    p=fixed(p,points=[{"point_id":"p1","constraint":"sliding"}])
    with pytest.raises(ConstraintError,match="CONSTRAINT_SLIDING_STATION"):
        edit_constrained_project(p,{"moves":[{"point_id":"p1","longitude":118.005,"latitude":23}]})
    with pytest.raises(ConstraintError,match="CONSTRAINT_RIGID_AUTOMOVE"):
        edit_constrained_project(p,{"automatic":True,"moves":[{"point_id":"p0","longitude":117,"latitude":23}]})


def test_date_line_clamped_fraction_and_wgs84_model_are_preserved():
    p=project(xs=(0,.4,1))
    for point,lon in zip(p["route"]["points"],(179.8,179.96,-179.8)):
        point["longitude"]=lon;point["latitude"]=80
    p.pop("profile")
    p=fixed(p,points=[{"point_id":"p1","constraint":"clamped"}])
    result=edit_constrained_project(p,{"moves":[{"point_id":"p2","longitude":-179.6,"latitude":80.01}]})
    pts=result["project"]["route"]["points"]
    expected=interpolate(179.8,80,-179.6,80.01,.4,"rhumb")
    assert inverse(pts[1]["longitude"],pts[1]["latitude"],*expected)[0]<.001
    assert result["report"]["physical_length_delta_m"]==pytest.approx(0,abs=1e-4)


def test_linked_manufacturing_reference_slides_by_physical_station_without_changing_material():
    p=project(xs=(0,.4,1))
    station=analyze_project(p)["rpl"][1]["cable_kp_m"]
    p["assembly_references"]=[{"id":"ref","cable_kp_m":station,"name":"制造参考"}]
    p=fixed(p,points=[{"point_id":"p1","constraint":"sliding"}],
            path_links=[*ends(p),{"point_id":"p1","slack_change":False,"assembly_item_id":"ref"}])
    result=edit_constrained_project(p,{"moves":[{"point_id":"p1","cable_kp_m":station+100}]})
    a=analyze_project(result["project"])
    assert a["assembly_references"][0]["cable_kp_m"]==pytest.approx(station+100)
    assert a["assembly_references"][0]["kp_m"]==pytest.approx(next(row for row in a["rpl"] if row["id"]=="p1")["kp_m"],abs=1e-5)
    assert result["report"]["physical_length_delta_m"]==pytest.approx(0,abs=1e-5)


def test_price_and_operational_metadata_can_change_without_invalidating_manufacturing_domain():
    p=project()
    p["bodies"]=[{"id":"body","cable_kp_m":200,"length_m":10,"cost":5}]
    p=fixed(p)
    p["bodies"][0]["cost"]=30
    p["route"]["legs"][0]["extra_cost"]=50
    p["route"]["legs"][0]["stop_hours"]=2
    a=analyze_project(p)
    assert a["summary"]["body_cost"]==30
    assert a["summary"]["extra_cost"]==50


def test_edit_that_collapses_rigid_anchor_line_is_rejected():
    p=fixed(project(xs=(0,1,2)))
    with pytest.raises(ConstraintError,match="CONSTRAINT_ZERO_DOMAIN"):
        edit_constrained_project(p,{"moves":[{"point_id":"p1","longitude":118,"latitude":23}]})


def test_explicit_clear_removes_domain_state_keeps_current_manufacturing_and_revision():
    p=fixed(project())
    p["saved_revision"]=3
    before=analyze_project(p)
    result=configure_constraints(p,{"clear":True})
    assert result["project"]["saved_revision"]==3
    assert "constraint_state" not in result["project"]["route"]
    assert "path_links" not in result["project"]["route"]
    assert not any("constraint" in x for x in result["project"]["route"]["points"])
    assert analyze_project(result["project"])["summary"]==before["summary"]
    assert reverse_project(result["project"])["project"]


@pytest.mark.parametrize("signed_zero",[False,True])
def test_actual_node_json_roundtrip_preserves_constraint_signature_analysis_and_edit(signed_zero):
    node=shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the real browser-number JSON roundtrip")
    p=project(xs=(0,5,10))
    for i,point in enumerate(p["route"]["points"]):
        point.update(longitude=118.0+i*.05,latitude=-0.0 if signed_zero else 22.0)
    p.pop("profile",None)
    p["route"]["slack_pct"]=1.0
    p["bodies"]=[{"id":"component","kp_m":200.0,"length_m":10.0,"cost":30.0}]
    configured=fixed(p)
    before=analyze_project(configured)
    script="let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s))));"
    output=subprocess.run([node,"-e",script],input=json.dumps(configured),capture_output=True,text=True,encoding="utf-8",check=True)
    roundtrip=json.loads(output.stdout)
    assert isinstance(roundtrip["route"]["points"][0]["longitude"],int)
    assert roundtrip["route"]["points"][0]["latitude"]==(0 if signed_zero else 22)
    after=analyze_project(roundtrip)
    assert after["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"])
    edited=edit_constrained_project(roundtrip,{"moves":[{"point_id":"p1","longitude":118.05,"latitude":.01 if signed_zero else 22.01}]})
    assert edited["report"]["physical_length_delta_m"]==pytest.approx(0,abs=1e-5)
    second=subprocess.run([node,"-e",script],input=json.dumps(edited["project"]),capture_output=True,text=True,encoding="utf-8",check=True)
    analyze_project(json.loads(second.stdout))
