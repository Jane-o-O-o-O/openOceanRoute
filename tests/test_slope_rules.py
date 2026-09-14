"""Independent geometry/coverage counterexamples for read-only slope rules."""
import base64
from copy import deepcopy
import json
import math

import pytest
from pyproj import Geod

from oceanroute.core import analyze_project, route_signature
from oceanroute.slope_rules import check_slope_rules, normalize_slope_rules, SlopeRuleError
from oceanroute.terrain_sources import normalize_sources, terrain_library_signature

GEOD = Geod(ellps="WGS84")


def project(length=1000, depths=(100,100)):
    lon,lat,_=GEOD.fwd(0,0,90,length)
    return {"schema_version":1,"id":"slope-rule-test","crs":"EPSG:4326",
            "route":{"curve":"geodesic","mode":"flexible","slack_basis":"surface","slack_pct":0,
                     "points":[{"id":"a","longitude":0,"latitude":0,"depth_m":depths[0]},
                               {"id":"b","longitude":lon,"latitude":lat,"depth_m":depths[1]}],
                     "legs":[{"cable_type_id":"a"}]},
            "cable_types":[{"id":"a","cost_per_m":3,"lay_speed_m_s":1}],"bodies":[],"terrain_sources":[]}


def profile(p, values, source="independent-survey"):
    p["profile"]={"source":source,"route_signature":route_signature(p),
                  "samples":[{"kp_m":kp,"depth_m":depth} for kp,depth in values]}
    return p


def rule(kind="inline", ident="r", start=0, end=None, inline=15, side=15, **extra):
    r={"id":ident,"kind":kind,"start_kp_m":start,"end_kp_m":end,**extra}
    if kind in {"inline","both"}:r["max_inline_slope_deg"]=inline
    if kind in {"side","both"}:r["max_side_slope_deg"]=side
    return r


def attach_side(p, *, kps=(0,250,500,750,1000), depths=(100,100,100,100,100), missing=None, boundary=None, spacing=250):
    """Saved synthetic transects with independently specified measured depths."""
    p["terrain_sources"]=normalize_sources([{"id":"survey","name":"合成平面参考来源","kind":"xyz","enabled":True,"priority":1,
        "source_crs":"EPSG:4326","depth_positive":"down","depth_units":"m","vertical_datum":"synthetic-datum",
        "text":"-0.02 -0.02 100\n0.02 -0.02 100\n-0.02 0.02 100\n0.02 0.02 100"}])
    samples=[]
    for index,kp in enumerate(kps):
        ds=list(depths)
        if missing is not None and index==missing:ds[1]=None
        offsets=[-100,-50,0,50,100]
        trans=[{"offset_m":q,"depth_m":d,"source_id":"survey","source_fingerprint":p["terrain_sources"][0]["fingerprint"],
                "source_boundary_to_next":bool(index==boundary and j==1)} for j,(q,d) in enumerate(zip(offsets,ds))]
        adjacent=[abs(math.degrees(math.atan2(a-b,50))) for a,b in zip(ds,ds[1:]) if a is not None and b is not None]
        port=math.degrees(math.atan2(ds[0]-ds[2],100)) if all(v is not None for v in ds[:3]) else None
        starboard=math.degrees(math.atan2(ds[2]-ds[4],100)) if all(v is not None for v in ds[2:]) else None
        full=math.degrees(math.atan2(ds[0]-ds[4],200)) if all(v is not None for v in ds) else None
        samples.append({"kp_m":float(kp),"port_slope_deg":port,"starboard_slope_deg":starboard,"side_slope_deg":full,
                        "max_sampled_abs_slope_deg":max(adjacent) if adjacent else None,"complete":all(v is not None for v in ds),
                        "source_boundary":index==boundary,"transect":trans})
    p["side_slopes"]={"model":"route-side-slopes-v1","schema_version":1,"route_signature":route_signature(p),
                      "metadata":{"model":"route-side-slopes-v1","terrain_library_signature":terrain_library_signature(p["terrain_sources"]),
                                  "vertical_datum":"synthetic-datum","start_kp_m":float(kps[0]),"end_kp_m":float(kps[-1]),
                                  "spacing_m":spacing,"half_width_m":100,"cross_spacing_m":50},"samples":samples}
    return p


def result(p, rules):
    return check_slope_rules(p,{"rules":rules})


def test_inline_clips_both_kp_boundaries_against_whole_profile_segment_not_its_start():
    p=profile(project(),[(0,100),(500,350),(1000,350)])
    actual=result(p,[rule(start=100,end=200,inline=20)])
    violation=actual["results"][0]["violations"][0]
    assert violation["start_kp_m"]==100 and violation["end_kp_m"]==200
    assert violation["value_deg"]==pytest.approx(math.degrees(math.atan(.5)))
    assert actual["summary"]["status"]=="violations"
    assert actual["summary"]["all_requested_data_available"]


def test_longitudinal_signed_direction_absolute_limit_and_equality():
    p=profile(project(),[(0,1100),(1000,100)])
    a=result(p,[rule(ident="absolute",inline=44),rule(ident="equal",inline=45)])
    assert a["results"][0]["violations"][0]["value_deg"]==pytest.approx(-45)
    assert a["results"][1]["status"]=="sampled_pass"


def test_profile_ridge_has_two_real_local_violations_even_when_endpoint_slope_zero():
    p=profile(project(),[(0,100),(500,600),(1000,100)])
    a=result(p,[rule(inline=20)])
    assert [v["value_deg"] for v in a["results"][0]["violations"]]==pytest.approx([45,-45])
    assert a["summary"]["violation_count"]==2


def test_v_shape_cross_section_does_not_pass_zero_full_width_and_half_width_secants():
    p=attach_side(project(),depths=(100,0,100,0,100))
    p["slope_rules"]=[rule("both",inline=5,side=60)]
    a=check_slope_rules(p)
    assert all(s["side_slope_deg"]==s["port_slope_deg"]==s["starboard_slope_deg"]==0 for s in p["side_slopes"]["samples"])
    assert a["results"][0]["components"]["inline"]["status"]=="sampled_pass"
    assert a["summary"]["violation_count"]==5
    assert [v["value_deg"] for v in a["results"][0]["violations"]]==pytest.approx([math.degrees(math.atan(2))]*5,abs=1e-12)


def test_valid_side_is_sampled_pass_with_explicit_no_continuous_bed_claim():
    a=result(attach_side(project(),depths=(100,125,150,175,200)),[rule("side",side=30)])
    row=a["results"][0]
    assert row["status"]=="sampled_pass"
    assert row["components"]["side"]["coverage"]["sampled_kps_m"]==[0,250,500,750,1000]
    assert row["components"]["side"]["coverage"]["continuous_bed_verified"] is False
    assert a["summary"]["continuous_bed_verified"] is False


def test_rules_are_independent_and_disabled_rules_need_no_side_data():
    p=profile(project(),[(0,100),(1000,1100)])
    a=result(p,[rule(ident="strict",inline=40),rule(ident="loose",inline=50),rule("side",ident="off",enabled=False)])
    assert [r["status"] for r in a["results"]]==["violations","sampled_pass","disabled"]
    assert not a["results"][2]["components"]
    assert a["summary"]["disabled_rules"]==1


def test_unknown_side_never_borrows_valid_flat_inline_profile():
    a=result(project(),[rule("both")])
    assert a["results"][0]["components"]["inline"]["status"]=="sampled_pass"
    assert a["results"][0]["components"]["side"]["status"]=="unknown"
    assert a["summary"]["status"]=="unknown"
    assert a["summary"]["all_requested_data_available"] is False


def test_inline_gap_is_unknown_and_other_known_part_is_preserved():
    p=profile(project(),[(0,100),(300,None),(600,100),(1000,200)])
    a=result(p,[rule(ident="full"),rule(ident="gap",start=100,end=500)])
    c=a["results"][0]["components"]["inline"]["coverage"]
    assert len(c["covered_ranges_m"])==1 and c["covered_ranges_m"][0]==pytest.approx([600,1000],abs=1e-7)
    assert c["unknown_ranges_m"]==[[0,600]]
    assert a["results"][0]["status"]=="incomplete"
    assert a["results"][1]["status"]=="unknown"


def test_stale_explicit_profile_does_not_fall_back_to_incidental_waypoint_depth():
    p=profile(project(),[(0,100),(1000,100)])
    p["route"]["points"][-1]["longitude"]*=1.01
    a=result(p,[rule()])
    assert a["summary"]["status"]=="unknown"
    assert any(d["code"]=="INLINE_PROFILE_STALE" for d in a["results"][0]["diagnostics"])
    assert a["results"][0]["components"]["inline"]["waypoint_approximation"] is False


@pytest.mark.parametrize("change",["route","priority","content","enabled","datum"])
def test_side_is_stopped_by_actual_route_or_library_edit(change):
    p=attach_side(project())
    if change=="route":p["route"]["points"][-1]["longitude"]*=1.01
    else:
        s=p["terrain_sources"][0]
        if change=="priority":s["priority"]+=1
        elif change=="enabled":s["enabled"]=False
        else:
            for k in ("content_sha256","fingerprint","byte_count"):s.pop(k,None)
            if change=="content":s["text"]=s["text"].replace("100","101")
            elif change=="datum":s["vertical_datum"]="another-datum"
    a=result(p,[rule("side")])
    assert a["summary"]["status"]=="unknown"
    assert not a["results"][0]["violations"]
    assert any(d["code"].endswith("STALE") for d in a["results"][0]["diagnostics"])


def test_library_bound_inline_profile_stale_and_no_fallback():
    p=attach_side(profile(project(),[(0,100),(1000,100)]))
    p["profile"]["metadata"]={"model":"priority-terrain-library-v1","terrain_library_signature":terrain_library_signature(p["terrain_sources"])}
    p["terrain_sources"][0]["priority"]+=1
    a=result(p,[rule()])
    assert a["summary"]["status"]=="unknown"
    assert any(w["code"]=="TERRAIN_LIBRARY_STALE" for w in a["warnings"])


def test_complete_source_boundary_is_uncertain_even_if_every_angle_is_under_limit():
    a=result(attach_side(project(),boundary=2),[rule("side")])
    assert a["summary"]["status"]=="incomplete"
    assert a["results"][0]["components"]["side"]["coverage"]["uncertain_kps_m"]==[500]
    assert any(d["code"]=="SIDE_SOURCE_BOUNDARY_UNCERTAIN" for d in a["results"][0]["diagnostics"])


def test_true_violation_is_not_hidden_by_missing_or_uncertain_support():
    p=attach_side(project(),depths=(100,0,100,0,100),missing=2,boundary=3)
    a=result(p,[rule("side",side=60)])
    assert a["summary"]["status"]=="violations" and a["summary"]["incomplete_rules"]==1
    assert not a["summary"]["all_requested_data_available"]
    assert any(v["uncertain"] for v in a["results"][0]["violations"])
    assert a["results"][0]["components"]["side"]["coverage"]["missing_kps_m"]==[500]


def test_side_domain_tail_and_station_gaps_are_incomplete_not_extrapolated():
    p=attach_side(project(),kps=(0,250,500,750),spacing=250)
    a=result(p,[rule("side")])
    assert a["results"][0]["components"]["side"]["coverage"]["unknown_ranges_m"][-1]==pytest.approx([750,1000])
    assert a["summary"]["status"]=="incomplete"
    p=attach_side(project(),kps=(0,1000),spacing=250)
    a=result(p,[rule("side")])
    gaps=a["results"][0]["components"]["side"]["coverage"]["sampling_gaps_m"]
    assert len(gaps)==1 and gaps[0]==pytest.approx([0,1000],abs=1e-7)
    assert a["summary"]["status"]=="incomplete"


def test_no_actual_side_station_in_narrow_rule_is_unknown_not_interpolated_pass():
    a=result(attach_side(project()),[rule("side",start=50,end=100)])
    assert a["summary"]["status"]=="unknown"
    assert a["results"][0]["components"]["side"]["coverage"]["sampled_kps_m"]==[]
    assert not a["summary"]["all_requested_data_available"]


def test_side_stations_outside_rule_do_not_create_in_range_violations():
    p=attach_side(project())
    # Only the station at zero has a steep measured transect.
    steep=attach_side(project(),depths=(100,0,100,0,100))["side_slopes"]["samples"][0]
    p["side_slopes"]["samples"][0]=steep
    a=result(p,[rule("side",start=250,end=750,side=5)])
    assert a["summary"]["status"]=="sampled_pass" and not a["results"][0]["violations"]


def test_outside_route_and_profile_kp_is_unknown_not_a_slope_extension():
    p=profile(project(),[(0,100),(2000,2100)])
    a=result(p,[rule(start=900,end=1500,inline=30)])
    c=a["results"][0]["components"]["inline"]["coverage"]
    assert len(c["unknown_ranges_m"])==1 and c["unknown_ranges_m"][0]==pytest.approx([1000,1500],abs=1e-7)
    assert a["results"][0]["violations"][0]["end_kp_m"]==pytest.approx(1000)
    assert a["summary"]["all_requested_data_available"] is False


def test_read_only_checker_works_without_manufacturing_solver_and_preserves_bytes():
    p=attach_side(profile(project(),[(0,100),(1000,100)]))
    p["route"]["mode"]="fixed"  # Deliberately no manufacturing fixed length.
    p["bodies"]=[{"id":"unchanged","cable_kp_m":123,"length_m":3}]
    p["route"]["path_links"]=[{"point_id":"a","cable_kp_m":0}]
    p["saved_revision"]=7
    p["slope_rules"]=[rule("both")]
    before=json.dumps(p,sort_keys=True,ensure_ascii=False)
    a=check_slope_rules(p)
    assert a["summary"]["status"]=="sampled_pass"
    assert json.dumps(p,sort_keys=True,ensure_ascii=False)==before
    assert "project" not in a
    with pytest.raises(ValueError):analyze_project(p)


def test_current_analysis_is_reused_without_recursive_manufacturing_analysis():
    p=profile(project(),[(0,100),(1000,300)])
    a=analyze_project(p)
    direct=result(p,[rule()])
    reused=check_slope_rules(p,{"rules":[rule()]},analysis=a)
    assert reused["results"]==direct["results"]
    p["route"]["points"][-1]["longitude"]*=1.01
    with pytest.raises(ValueError,match="ANALYSIS_STALE"):check_slope_rules(p,{"rules":[rule()]},analysis=a)


def test_empty_and_all_disabled_status_and_candidate_override_are_explicit():
    p=project();assert check_slope_rules(p)["summary"]["status"]=="not_configured"
    p["slope_rules"]=[rule("side",enabled=False)]
    before=deepcopy(p)
    assert check_slope_rules(p)["summary"]["status"]=="disabled"
    a=result(p,[rule()]);assert a["summary"]["status"]=="sampled_pass"
    assert p==before


@pytest.mark.parametrize("bad",[
    {"kind":"in_line"},{"kind":[]},{"kind":{}},{"kind":None},
    {"max_inline_slope_deg":90},{"max_inline_slope_deg":-1},
    {"max_inline_slope_deg":True},{"max_inline_slope_deg":float('inf')},
    {"max_inline_slope_deg":10**1000},{"enabled":1},{"start_kp_m":-1},
    {"start_kp_m":500,"end_kp_m":500},{"name":"x"*257},{"id":" "},{"unknown":123},
    {"max_side_slope_deg":1},
])
def test_rule_numeric_schema_is_strict(bad):
    with pytest.raises(ValueError):result(project(),[{**rule(),**bad}])


def test_duplicate_rule_ids_unknown_config_fields_and_list_budget_reject():
    with pytest.raises(ValueError,match="DUPLICATE"):result(project(),[rule(),rule()])
    with pytest.raises(ValueError,match="CONFIG"):check_slope_rules(project(),{"spacing_m":100})
    with pytest.raises(ValueError,match="LIMIT"):check_slope_rules(project(),{"rules":[rule(ident=str(i)) for i in range(129)]})
    with pytest.raises(ValueError):normalize_slope_rules("rules")


def test_true_work_budget_fails_before_library_preparation(monkeypatch):
    p=attach_side(project())
    def forbidden(*args,**kwargs):raise AssertionError("should not resolve terrain before admission")
    monkeypatch.setattr("oceanroute.terrain_sources.terrain_library_signature",forbidden)
    with pytest.raises(SlopeRuleError,match="WORK_BUDGET"):
        check_slope_rules(p,{"rules":[rule("side")],"max_work_units":1})


def test_output_budget_rejects_complete_result_without_truncating_violations():
    p=profile(project(),[(i*10,100+(i%2)*10) for i in range(101)])
    with pytest.raises(SlopeRuleError,match="OUTPUT_BUDGET"):
        check_slope_rules(p,{"rules":[rule(inline=0)],"max_output_bytes":4096})


@pytest.mark.parametrize("change",["maximum","complete","boundary","kp","transect_nan"])
def test_persisted_side_arithmetic_or_shape_cannot_hide_a_real_ridge(change):
    p=attach_side(project(),depths=(100,0,100,0,100))
    row=p["side_slopes"]["samples"][1]
    if change=="maximum":row["max_sampled_abs_slope_deg"]=0
    elif change=="complete":row["transect"][1]["depth_m"]=None
    elif change=="boundary":row["transect"][1]["source_id"]="other"
    elif change=="kp":row["kp_m"]=0
    else:row["transect"][1]["depth_m"]=float('nan')
    with pytest.raises(ValueError):result(p,[rule("side")])


def test_json_browser_numeric_roundtrip_and_strict_finite_output():
    p=attach_side(project())
    a=result(p,[rule("both")]);b=result(json.loads(json.dumps(p)),[rule("both")])
    assert a==b
    json.dumps(a,allow_nan=False)


def test_date_line_geometry_range_is_real_short_wgs84_route():
    p=project();p["route"]["points"][0].update(longitude=179.99,latitude=70)
    p["route"]["points"][1].update(longitude=-179.99,latitude=70)
    _,_,distance=GEOD.inv(179.99,70,-179.99,70)
    profile(p,[(0,100),(distance,200)])
    a=result(p,[rule()])
    assert a["results"][0]["end_kp_m"]==pytest.approx(distance,abs=1e-7)
    assert a["summary"]["status"]=="sampled_pass"
    assert distance<1000


def test_repeated_waypoint_conflicting_depth_is_unknown_not_vertical_slope():
    p=project();p["route"]["points"].insert(1,{**p["route"]["points"][0],"id":"repeat","depth_m":900})
    a=result(p,[rule()])
    assert a["summary"]["status"]=="unknown"
    assert not a["results"][0]["violations"]


LOCAL = "+proj=aeqd +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +type=crs"


def native_grid(field, *, extent=1000, nodes=9, **changes):
    """A real Surfer native grid, with independently prescribed depth values."""
    coordinates=[-extent+2*extent*j/(nodes-1) for j in range(nodes)]
    text=(f"DSAA\n{nodes} {nodes}\n{-extent} {extent}\n{-extent} {extent}\n0 10000\n"+
          "\n".join(" ".join(str(field(x,y)) for x in coordinates) for y in coordinates)+"\n")
    return {"id":"analytical","name":"Independent affine-depth grid","kind":"surfer",
            "source_crs":LOCAL,"depth_positive":"down","depth_units":"m","vertical_datum":"TEST_DATUM",
            "data_base64":base64.b64encode(text.encode()).decode(),**changes}


def north_project(length=600, *, sources=None):
    p=project(length)
    a=GEOD.fwd(0,0,180,length/2);b=GEOD.fwd(0,0,0,length/2)
    for point,position in zip(p["route"]["points"],(a,b)):
        point.update(longitude=position[0],latitude=position[1])
    p["terrain_sources"]=sources or [native_grid(lambda x,y:1000+.2*x)]
    return p


def test_actual_source_sampler_and_side_producer_feed_rules_without_field_translation():
    from oceanroute.side_slopes import side_slopes_from_sources
    from oceanroute.terrain_sources import profile_from_sources
    p=north_project();before=deepcopy(p)
    longitudinal=profile_from_sources(p,{"spacing_m":100})["project"]
    candidate=side_slopes_from_sources(longitudinal,{"spacing_m":100})["project"]
    output=result(candidate,[rule("both",ident="strict",inline=1,side=10),
                             rule("both",ident="loose",inline=1,side=12)])
    assert [r["status"] for r in output["results"]]==["violations","sampled_pass"]
    assert output["results"][0]["components"]["inline"]["status"]=="sampled_pass"
    assert output["results"][0]["components"]["inline"]["waypoint_approximation"] is False
    expected=math.degrees(math.atan(.2))
    assert [v["value_deg"] for v in output["results"][0]["violations"]]==pytest.approx([expected]*7,abs=1e-7)
    assert output["summary"]["all_requested_data_available"]
    assert output["metadata"]["terrain_library_signature"]==candidate["side_slopes"]["metadata"]["terrain_library_signature"]
    assert candidate["route"]==p["route"] and candidate["bodies"]==p["bodies"] and p==before
    json.dumps(output,allow_nan=False)


def test_actual_source_hole_fallback_is_uncertain_under_a_loose_rule():
    from oceanroute.side_slopes import side_slopes_from_sources
    detail=native_grid(lambda x,y:1.70141e38 if x==0 else 300,extent=200,nodes=9,
                       id="detail",priority=100,sampling={"method":"nearest"})
    background=native_grid(lambda x,y:100,id="background",priority=0)
    p=side_slopes_from_sources(north_project(100,sources=[background,detail]),{"spacing_m":25})["project"]
    output=result(p,[rule("side",side=89)])
    assert output["summary"]["status"]=="incomplete"
    assert not output["results"][0]["violations"]
    assert all(s["complete"] and s["source_boundary"] for s in p["side_slopes"]["samples"])
    assert all(s["transect"][2]["fallback"] for s in p["side_slopes"]["samples"])
    assert not output["summary"]["all_requested_data_available"]


def test_actual_source_mid_half_nodata_preserves_known_violation_without_passing_domain():
    from oceanroute.side_slopes import side_slopes_from_sources
    source=native_grid(lambda x,y:1.70141e38 if x==-50 else 1000+.2*x,
                       extent=200,nodes=9,sampling={"method":"nearest"})
    p=side_slopes_from_sources(north_project(100,sources=[source]),{"spacing_m":25})["project"]
    output=result(p,[rule("side",side=10)])
    assert output["summary"]["status"]=="violations"
    assert output["summary"]["incomplete_rules"]==1
    assert all(v["uncertain"] for v in output["results"][0]["violations"])
    assert len(output["results"][0]["components"]["side"]["coverage"]["missing_kps_m"])==5
    assert all(s["port_slope_deg"] is None for s in p["side_slopes"]["samples"])


def test_actual_single_transect_can_reveal_violation_but_cannot_cover_a_positive_rule_span():
    from oceanroute.side_slopes import side_slopes_from_sources
    p=side_slopes_from_sources(north_project(),{"start_kp_m":300,"end_kp_m":300})["project"]
    output=result(p,[rule("side",ident="strict",side=10),rule("side",ident="loose",side=12)])
    assert [r["status"] for r in output["results"]]==["violations","incomplete"]
    assert output["results"][0]["violations"][0]["kp_m"]==300
    assert not output["summary"]["all_requested_data_available"]
    assert output["results"][1]["components"]["side"]["coverage"]["covered_length_m"]==0


def test_sources_separated_by_missing_probes_cannot_hide_a_boundary():
    p=attach_side(project(),depths=(100,100,100,100,100))
    row=p["side_slopes"]["samples"][2]
    for point in row["transect"][1:4]:point["depth_m"]=None
    row.update(complete=False,port_slope_deg=None,starboard_slope_deg=None,
               side_slope_deg=None,max_sampled_abs_slope_deg=None)
    row["transect"][-1]["source_id"]="different-survey"
    with pytest.raises(ValueError,match="来源边界"):result(p,[rule("side")])


def test_null_end_remains_a_declaration_when_route_changes_or_becomes_shorter_than_start():
    p=project();p["slope_rules"]=[rule(start=500)]
    first=check_slope_rules(p)
    assert first["rules"][0]["end_kp_m"] is None
    assert first["results"][0]["requested_range_m"]==[500,None]
    assert first["summary"]["status"]=="sampled_pass"
    p["route"]["points"][-1]["longitude"]=GEOD.fwd(0,0,90,300)[0]
    short=check_slope_rules(p)
    assert short["rules"]==first["rules"] and short["results"][0]["end_kp_m"]==pytest.approx(300)
    assert short["summary"]["status"]=="unknown"
    assert any(d["code"]=="RULE_RANGE_EMPTY" for d in short["results"][0]["diagnostics"])
    assert not short["summary"]["all_requested_data_available"]


def test_metadata_model_binding_is_required_even_when_other_signatures_match():
    p=attach_side(project());p["side_slopes"]["metadata"]["model"]="unknown"
    output=result(p,[rule("side")])
    assert output["summary"]["status"]=="unknown"
    assert output["results"][0]["diagnostics"][0]["code"]=="SIDE_SLOPES_MODEL_UNSUPPORTED"


def test_rounded_persisted_maximum_cannot_hide_above_threshold_actual_adjacent_angle():
    p=attach_side(project(),depths=(100,125,150,175,200))
    angle=math.degrees(math.atan(.5))
    for row in p["side_slopes"]["samples"]:row["max_sampled_abs_slope_deg"]-=5e-9
    output=result(p,[rule("side",side=angle-1e-9)])
    assert output["summary"]["violation_count"]==5
    assert [v["value_deg"] for v in output["results"][0]["violations"]]==pytest.approx([angle]*5,abs=1e-12)


def test_current_core_analysis_correctly_reports_a_stale_bound_profile_as_unknown():
    p=attach_side(profile(project(),[(0,100),(1000,100)]))
    p["profile"]["metadata"]={"model":"priority-terrain-library-v1",
                              "terrain_library_signature":terrain_library_signature(p["terrain_sources"])}
    p["slope_rules"]=[rule()]
    current=analyze_project(p)
    assert current["slope_rule_checks"]["summary"]["status"]=="sampled_pass"
    p["terrain_sources"][0]["priority"]+=1
    stale=analyze_project(p)
    assert stale["slope_rule_checks"]["summary"]["status"]=="unknown"
    assert not stale["profile_metadata"]["imported_profile_valid"]
    assert any(w["code"]=="TERRAIN_LIBRARY_STALE" for w in stale["warnings"])
    with pytest.raises(ValueError,match="ANALYSIS_STALE"):
        check_slope_rules(p,analysis=current)


def test_core_automatic_512_rule_budget_preserves_disabled_rules_and_rejects_513():
    p=project();p["slope_rules"]=[rule("side",ident=f"saved-{i}",enabled=False) for i in range(512)]
    before=deepcopy(p)
    report=analyze_project(p)["slope_rule_checks"]
    assert report["summary"]["status"]=="disabled" and report["summary"]["disabled_rules"]==512
    assert len(report["rules"])==len(report["results"])==512
    assert report["rules"][-1]["id"]=="saved-511" and p==before
    assert all(r["end_kp_m"] is None for r in report["rules"])
    with pytest.raises(ValueError,match="LIMIT"):check_slope_rules(p)
    assert check_slope_rules(p,{"max_rules":512})["summary"]["disabled_rules"]==512
    p["slope_rules"].append(rule("side",ident="saved-512",enabled=False))
    with pytest.raises(ValueError,match="LIMIT"):analyze_project(p)


@pytest.mark.parametrize("field,value",[("source_id",[]),("source_fingerprint",{})])
def test_invalid_source_identity_is_a_schema_error_not_an_unhashable_runtime_error(field,value):
    p=attach_side(project());p["side_slopes"]["samples"][0]["transect"][0][field]=value
    with pytest.raises(ValueError,match=field):result(p,[rule("side")])
