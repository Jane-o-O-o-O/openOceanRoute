from copy import deepcopy
import json
import math

import pytest

from oceanroute.core import analyze_project, densify_route, route_signature, sample_project
from oceanroute.geodesy import WGS84_A


def project_for_length(length=1000, depth1=20, depth2=20, slack=1):
    return {"crs": "EPSG:4326", "route": {"curve": "rhumb", "mode": "flexible", "slack_basis": "surface", "slack_pct": slack,
             "points": [{"id": "p1", "longitude": 0, "latitude": 0, "depth_m": depth1},
                        {"id": "p2", "longitude": math.degrees(length / WGS84_A), "latitude": 0, "depth_m": depth2}],
             "legs": [{"cable_type_id": "A", "burial": True}]},
            "cable_types": [{"id": "A", "name": "缆 A", "cost_per_m": 2, "lay_speed_m_s": 1}],
            "costs": {"currency": "CNY", "vessel_day_rate": 86400, "burial_per_m": 3}, "bodies": []}


def bind_profile(p, samples, source="user"):
    p["profile"] = {"samples": [{"kp_m": kp, "depth_m": d} for kp, d in samples], "route_signature": route_signature(p), "source": source}


def codes(a):
    return {w["code"] for w in a["warnings"]}


def test_flat_slack_rpl_and_cost_golden():
    a = analyze_project(project_for_length())
    s = a["summary"]
    assert s["surface_length_m"] == pytest.approx(1000, abs=1e-7)
    assert s["bottom_length_m"] == pytest.approx(1000, abs=1e-7)
    assert s["cable_length_m"] == pytest.approx(1010, abs=1e-7)
    assert s["material_cost"] == pytest.approx(2020)
    assert s["vessel_cost"] == pytest.approx(1000)
    assert s["burial_cost"] == pytest.approx(3000)
    assert s["cost_total"] == pytest.approx(6020)
    assert a["rpl"][-1]["cable_kp_m"] == pytest.approx(1010)
    assert "WAYPOINT_DEPTH_APPROXIMATION" in codes(a)


def test_three_four_five_seabed_bottom_slack_golden():
    p = project_for_length(300, 0, 400)
    p["route"]["slack_basis"] = "bottom"
    a = analyze_project(p)
    assert a["summary"]["bottom_length_m"] == pytest.approx(500, abs=1e-7)
    assert a["summary"]["cable_length_m"] == pytest.approx(505, abs=1e-7)
    assert a["legs"][0]["bottom_slack_pct"] == pytest.approx(1)
    assert a["legs"][0]["surface_slack_pct"] == pytest.approx(68.3333333333333)


def test_sampled_interior_ridge_integration_not_endpoint_shortcut():
    p = project_for_length(1000, 100, 100)
    bind_profile(p, [(0, 100), (500, 600), (1000, 100)])
    a = analyze_project(p)
    assert a["summary"]["bottom_length_m"] == pytest.approx(2 * math.hypot(500, 500), abs=1e-7)
    assert a["legs"][0]["max_slope_deg"] == pytest.approx(45)
    assert {"RULE_SLOPE", "CABLE_SHORTAGE"} <= codes(a)


def test_profile_inserts_leg_boundary_before_integrating():
    p = project_for_length(1000, 100, 100)
    second = p["route"]["points"].pop()
    p["route"]["points"].append({"id": "mid", "longitude": math.degrees(300 / WGS84_A), "latitude": 0, "depth_m": 100})
    p["route"]["points"].append(second)
    p["route"]["legs"].append({"cable_type_id": "A"})
    bind_profile(p, [(0, 100), (1000, 1100)])
    a = analyze_project(p)
    assert [l["bottom_length_m"] for l in a["legs"]] == pytest.approx([300 * math.sqrt(2), 700 * math.sqrt(2)])
    assert a["rpl"][1]["depth_m"] == pytest.approx(400)


def test_fixed_length_negative_slack_and_edit_conservation():
    p = project_for_length(1000)
    p["route"]["mode"] = "fixed"
    p["route"]["legs"][0]["fixed_cable_length_m"] = 990
    a = analyze_project(p)
    assert a["legs"][0]["surface_slack_pct"] == pytest.approx(-1)
    p["route"]["points"][-1]["longitude"] *= 2
    b = analyze_project(p)
    assert b["summary"]["cable_length_m"] == 990
    assert b["legs"][0]["surface_slack_pct"] == pytest.approx(-50.5)
    assert "CABLE_SHORTAGE" in codes(b)


def test_profile_signature_edit_invalidation_not_incidental_waypoint_fallback():
    p = project_for_length()
    bind_profile(p, [(0, 20), (1000, 20)])
    before = route_signature(p)
    p["route"]["points"][0]["label"] = "新名称"
    p["route"]["slack_pct"] = 2
    assert route_signature(p) == before
    assert analyze_project(p)["profile_metadata"]["imported_profile_valid"]
    p["route"]["points"][-1]["longitude"] *= 1.1
    a = analyze_project(p)
    assert a["summary"]["bottom_length_m"] is None
    assert "PROFILE_STALE" in codes(a)
    assert "WAYPOINT_DEPTH_APPROXIMATION" not in codes(a)


def test_unbound_profile_disabled_and_gap_remains_unknown():
    p = project_for_length()
    p["profile"] = {"samples": [{"kp_m": 0, "depth_m": 20}, {"kp_m": 1000, "depth_m": 30}]}
    a = analyze_project(p)
    assert a["summary"]["bottom_length_m"] is None
    assert "PROFILE_UNBOUND" in codes(a)
    bind_profile(p, [(0, 20), (500, None), (1000, 30)])
    a = analyze_project(p)
    assert a["summary"]["bottom_length_m"] is None
    assert a["legs"][0]["bottom_slack_pct"] is None
    assert "PROFILE_GAPS" in codes(a)


def test_missing_depth_never_becomes_zero_and_bottom_slack_requires_depth():
    p = project_for_length(depth1=None, depth2=None)
    a = analyze_project(p)
    assert a["summary"]["bottom_length_m"] is None
    assert all(s["depth_m"] is None for s in a["profile"])
    p["route"]["slack_basis"] = "bottom"
    with pytest.raises(ValueError, match="水深缺失"):
        analyze_project(p)


def test_replacement_body_material_and_sld_accounting():
    p = project_for_length()
    p["bodies"] = [{"id": "r", "name": "中继器", "kp_m": 500, "length_m": 10, "cost": 100}]
    a = analyze_project(p)
    assert a["summary"]["cable_length_m"] == pytest.approx(1010)
    assert a["summary"]["material_length_m"] == pytest.approx(1000)
    assert a["summary"]["material_cost"] == pytest.approx(2000)
    assert a["bodies"][0]["cable_kp_m"] == pytest.approx(505)
    assert sum(s["end_m"] - s["start_m"] for s in a["sld"]) == pytest.approx(1010)
    assert sum(l["material_cost"] for l in a["legs"]) == pytest.approx(a["summary"]["material_cost"])


def test_additional_body_and_allowance_create_true_cable_kp_jumps():
    p = project_for_length()
    p["bodies"] = [{"id": "r", "kp_m": 500, "length_m": 10, "cost": 100, "length_mode": "additional"}]
    p["route"]["allowances"] = [{"kp_m": 500, "length_m": 20}, {"kp_m": 500, "length_m": 5}]
    a = analyze_project(p)
    assert a["summary"]["cable_length_m"] == pytest.approx(1045)
    assert a["summary"]["material_length_m"] == pytest.approx(1035)
    assert a["summary"]["body_length_m"] == 10
    assert a["summary"]["allowance_length_m"] == 25
    assert a["rpl"][-1]["cable_kp_m"] == pytest.approx(1045)
    assert any(s["kind"] == "allowance" and s["route_start_kp_m"] == s["route_end_kp_m"] for s in a["sld"])
    assert sum(s["end_m"] - s["start_m"] for s in a["sld"]) == pytest.approx(1045)


def test_explicit_body_cable_kp_and_overlap_validation():
    p = project_for_length()
    p["bodies"] = [{"id": "r", "cable_kp_m": 202, "length_m": 10}]
    a = analyze_project(p)
    assert a["bodies"][0]["kp_m"] == pytest.approx(200)
    p["bodies"].append({"id": "other", "cable_kp_m": 205, "length_m": 10})
    with pytest.raises(ValueError, match="重叠"):
        analyze_project(p)
    p["bodies"] = [{"id": "end", "kp_m": 1000, "length_m": 10}]
    with pytest.raises(ValueError, match="超出"):
        analyze_project(p)


def test_events_contingency_and_zero_length_joint_do_not_manufacture_slack():
    p = project_for_length()
    p["events"] = [{"kp_m": 500, "stop_hours": 2, "extra_cost": 100}]
    p["bodies"] = [{"id": "joint", "kp_m": 500, "length_m": 0, "cost": 50}]
    p["costs"]["contingency_pct"] = 10
    a = analyze_project(p)
    assert a["summary"]["time_hours"] == pytest.approx(1000 / 3600 + 2)
    assert a["summary"]["vessel_cost"] == pytest.approx(8200)
    assert a["summary"]["cost_total"] == pytest.approx((2020 + 8200 + 3000 + 100 + 50) * 1.1)
    assert any(s["kind"] == "body" and s["end_m"] == s["start_m"] for s in a["sld"])


def test_crossing_angle_and_restricted_area_screening():
    p = project_for_length()
    x = math.degrees(500 / WGS84_A)
    p["layers"] = [
        {"id": "c", "kind": "cable", "name": "海缆", "geojson": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[x, -.01], [x, .01]]}, "properties": {}}]}},
        {"id": "r", "kind": "restricted", "name": "限制区", "geojson": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[x - .001, -.001], [x + .001, -.001], [x + .001, .001], [x - .001, .001], [x - .001, -.001]]]}, "properties": {}}]}}]
    a = analyze_project(p)
    crossing = next(c for c in a["crossings"] if c["layer_id"] == "c")
    assert crossing["kp_m"] == pytest.approx(500, abs=.01)
    assert crossing["angle_deg"] == pytest.approx(90, abs=.01)
    assert "RULE_RESTRICTED_AREA" in codes(a)


def test_sample_is_synthetic_strict_json_and_material_conservation():
    p = sample_project()
    before = deepcopy(p)
    a = analyze_project(p)
    assert p == before
    assert p["profile"]["route_signature"] == a["route_signature"]
    assert "SYNTHETIC_BATHYMETRY" in codes(a)
    assert not a["profile_metadata"]["measured"]
    assert len(a["rpl"]) == len(p["route"]["points"])
    assert sum(m["length_m"] for m in a["materials"]) + a["summary"]["body_length_m"] == pytest.approx(a["summary"]["cable_length_m"])
    assert sum(m["cost"] for m in a["materials"]) == pytest.approx(a["summary"]["material_cost"])
    assert a["summary"]["surface_length_m"] > 500000
    assert len(densify_route(p)) > len(p["route"]["points"])
    json.dumps(a, allow_nan=False)


@pytest.mark.parametrize("mutation", [
    lambda p: p["route"]["points"][0].update(longitude=181),
    lambda p: p["route"]["points"][0].update(depth_m=float("nan")),
    lambda p: p["cable_types"][0].update(cost_per_m=-1),
    lambda p: p["cable_types"][0].update(lay_speed_m_s=0),
    lambda p: p["route"]["legs"][0].update(cable_type_id="missing"),
    lambda p: p["route"].update(slack_pct=-100),
    lambda p: p["route"].update(mode="fixed"),
    lambda p: p["route"]["points"][1].update(id="p1"),
    lambda p: p["costs"].update(vessel_day_rate=float("inf")),
    lambda p: p["bodies"].append({"id": "b", "kp_m": 500, "cost": -1}),
])
def test_invalid_numeric_and_reference_inputs_rejected(mutation):
    p = project_for_length()
    mutation(p)
    with pytest.raises(ValueError):
        analyze_project(p)


def test_profile_kp_duplicate_or_unsorted_rejected_even_if_stale():
    for samples in ([(0, 10), (0, 10)], [(100, 10), (50, 10)]):
        p = project_for_length()
        bind_profile(p, samples)
        p["route"]["points"][-1]["longitude"] *= 2
        with pytest.raises(ValueError, match="严格递增"):
            analyze_project(p)


def test_repeated_coordinates_have_no_infinite_slack():
    p = project_for_length(0)
    a = analyze_project(p)
    assert a["summary"]["surface_length_m"] == 0
    assert a["summary"]["slack_pct"] is None
    assert a["legs"][0]["bearing_deg"] is None
    assert a["legs"][0]["surface_slack_pct"] is None
    assert "ZERO_LENGTH_LEG" in codes(a)
    json.dumps(a, allow_nan=False)


def test_same_position_conflicting_depths_do_not_create_vertical_seabed():
    p = project_for_length(0, 10, 20)
    a = analyze_project(p)
    assert a["summary"]["bottom_length_m"] is None
    assert "DUPLICATE_DEPTH_CONFLICT" in codes(a)


def test_partial_profile_keeps_known_downstream_leg_local_distance():
    p = project_for_length(1000, 100, 100)
    second = p["route"]["points"].pop()
    p["route"]["points"].append({"id": "mid", "longitude": math.degrees(500 / WGS84_A), "latitude": 0, "depth_m": 100})
    p["route"]["points"].append(second)
    p["route"]["legs"].append({"cable_type_id": "A"})
    bind_profile(p, [(0, None), (500, 100), (1000, 100)])
    a = analyze_project(p)
    assert a["summary"]["bottom_length_m"] is None
    assert a["legs"][0]["bottom_length_m"] is None
    assert a["legs"][1]["bottom_length_m"] == pytest.approx(500)
    assert a["rpl"][-1]["bottom_kp_m"] is None
def test_manufacturing_references_validate_physical_extent_and_do_not_occupy_material():
    p=sample_project()
    before=analyze_project(p)
    p["assembly_references"]=[{"id":"ref","cable_kp_m":100,"length_m":0}]
    after=analyze_project(p)
    assert after["summary"]==before["summary"]
    assert after["assembly_references"][0]["kp_m"]>=0
    p["assembly_references"][0]["cable_kp_m"]=before["summary"]["cable_length_m"]+10
    with pytest.raises(ValueError):
        analyze_project(p)
    p["assembly_references"]=[{"id":"ref","cable_kp_m":100,"length_m":5}]
    with pytest.raises(ValueError,match="不占实物长度"):
        analyze_project(p)
