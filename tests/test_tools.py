from copy import deepcopy
import math

import pytest

from oceanroute.core import analyze_project, route_signature
from oceanroute.geodesy import WGS84_A, inverse
from oceanroute.tools import (apply_slack_template, define_cables_by_depth, geodetic,
                             merge_projects, reverse_project, split_project, subdivide_project)


def project(length=1000, offset=0, fixed=False):
    p = {"id": "test", "name": "测试工程", "crs": "EPSG:4326", "route": {
        "id": "route", "curve": "rhumb", "mode": "fixed" if fixed else "flexible", "slack_basis": "surface", "slack_pct": 1,
        "points": [{"id": "p1", "longitude": math.degrees(offset / WGS84_A), "latitude": 0, "depth_m": 50},
                   {"id": "p2", "longitude": math.degrees((offset + length) / WGS84_A), "latitude": 0, "depth_m": 50}],
        "legs": [{"cable_type_id": "A", "fixed_cable_length_m": length * 1.01 if fixed else None, "burial": True}]},
        "cable_types": [{"id": "A", "name": "浅海缆", "cost_per_m": 2, "lay_speed_m_s": 1},
                        {"id": "B", "name": "深海缆", "cost_per_m": 3, "lay_speed_m_s": 2}],
        "costs": {"currency": "CNY", "vessel_day_rate": 86400, "burial_per_m": 3, "contingency_pct": 10}, "bodies": []}
    profile(p, [(0, 50), (length, 50)])
    return p


def profile(p, values):
    p["profile"] = {"samples": [{"kp_m": kp, "depth_m": d} for kp, d in values], "route_signature": route_signature(p), "source": "user", "measured": True}


def compare_invariants(before, after, keys=None):
    keys = keys or ("surface_length_m", "bottom_length_m", "cable_length_m", "material_length_m", "material_cost", "body_cost", "time_hours", "cost_total")
    for key in keys:
        assert after["summary"][key] == pytest.approx(before["summary"][key], rel=1e-9, abs=1e-5), key


def test_legal_allowance_defaults_and_endpoint_tolerance_survive_tools_api(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore

    client = TestClient(create_app(ProjectStore(tmp_path / "tools-defaults.sqlite3")))
    p = project()
    end = analyze_project(p)["summary"]["surface_length_m"]
    p["route"]["allowances"] = [{"length_m": 20}, {"kp_m": 500}, {"kp_m": end+5e-8, "length_m": 3}]
    original = deepcopy(p)
    before = client.post("/api/analyze", json=p)
    assert before.status_code == 200, before.text
    assert before.json()["summary"]["cable_length_m"] == pytest.approx(1033)

    reverse = client.post("/api/route/reverse", json=p)
    assert reverse.status_code == 200, reverse.text
    reversed_project = reverse.json()
    assert all("kp_m" in a and "length_m" in a for a in reversed_project["route"]["allowances"])
    assert reversed_project["route"]["allowances"][0]["kp_m"] == 0
    compare_invariants(before.json(), analyze_project(reversed_project))
    double = client.post("/api/tools/reverse", json={"project": reversed_project, "config": {}})
    assert double.status_code == 200, double.text
    compare_invariants(before.json(), analyze_project(double.json()["project"]))

    split = client.post("/api/tools/split", json={"project": p, "config": {"kp_m": 500}})
    assert split.status_code == 200, split.text
    parts = split.json()["projects"]
    assert parts[0]["route"]["allowances"][0]["kp_m"] == 0
    assert parts[0]["route"]["allowances"][1]["length_m"] == 0
    assert parts[1]["route"]["allowances"][0]["kp_m"] == pytest.approx(500)
    assert sum(analyze_project(part)["summary"]["cable_length_m"] for part in parts) == pytest.approx(1033)

    # Raw second input also contains missing station/length fields; merge must
    # canonicalize every source, not only values normalized by a previous tool.
    second = project(length=500, offset=1000)
    second["route"]["allowances"] = [{"length_m": 7}, {"kp_m": 100}]
    merged = client.post("/api/tools/merge", json={"projects": [p, second], "config": {}})
    assert merged.status_code == 200, merged.text
    made = merged.json()["project"]
    assert analyze_project(made)["summary"]["cable_length_m"] == pytest.approx(1545)
    assert made["route"]["allowances"][3]["kp_m"] == pytest.approx(1000)
    assert made["route"]["allowances"][4]["length_m"] == 0
    assert p == original


def test_allowance_outside_core_endpoint_tolerance_still_rejected():
    p = project()
    end = analyze_project(p)["summary"]["surface_length_m"]
    p["route"]["allowances"] = [{"kp_m": end+2e-7, "length_m": 1}]
    with pytest.raises(ValueError, match="kp_m"):
        reverse_project(p)


@pytest.mark.parametrize("curve", ["rhumb", "geodesic"])
def test_geodetic_inverse_direct_roundtrip_and_surface_samples(curve):
    a = geodetic({"longitude1": 179, "latitude1": 45, "longitude2": -178, "latitude2": 48, "curve": curve, "segments": 10})
    b = geodetic({"from": a["from"], "distance_m": a["distance_m"], "bearing_deg": a["bearing_deg"], "curve": curve})
    assert b["to"]["longitude"] == pytest.approx(-178, abs=1e-8)
    assert b["to"]["latitude"] == pytest.approx(48, abs=1e-8)
    distances = [inverse(x["longitude"], x["latitude"], y["longitude"], y["latitude"], curve)[0] for x, y in zip(a["points"], a["points"][1:])]
    assert sum(distances) == pytest.approx(a["distance_m"], abs=1e-6)
    assert max(distances) - min(distances) < 1e-6


def test_geodetic_parallel_direct_and_pole_or_multiturn_rejection():
    a = geodetic({"from": {"longitude": 0, "latitude": 60}, "distance_m": 10000, "bearing_deg": 90})
    assert a["to"]["latitude"] == 60
    assert inverse(0, 60, a["to"]["longitude"], 60)[0] == pytest.approx(10000, abs=1e-6)
    with pytest.raises(ValueError, match="极点"):
        geodetic({"from": {"longitude": 0, "latitude": 89}, "distance_m": 1_000_000, "bearing_deg": 0})
    with pytest.raises(ValueError, match="最短经度"):
        geodetic({"from": {"longitude": 0, "latitude": 80}, "distance_m": 5_000_000, "bearing_deg": 90})


@pytest.mark.parametrize("fixed", [False, True])
def test_subdivision_preserves_geography_profile_constraints_bodies_and_events(fixed):
    p = project(fixed=fixed)
    profile(p, [(0, 50), (400, 300), (1000, 50)])
    p["bodies"] = [{"id": "b", "kp_m": 250, "length_m": 10, "cost": 100},
                   {"id": "b2", "kp_m": 750, "length_m": 10, "cost": 100, "length_mode": "additional"}]
    p["route"]["allowances"] = [{"kp_m": 500, "length_m": 20}]
    p["route"]["legs"][0].update(allowance_m=5, stop_hours=2, extra_cost=30)
    p["events"] = [{"kp_m": 700, "stop_hours": 3, "extra_cost": 100}]
    old = analyze_project(p)
    original = deepcopy(p)
    tool = subdivide_project(p, {"spacing_m": 250})
    new = analyze_project(tool["project"])
    assert p == original
    assert len(tool["project"]["route"]["points"]) == 5
    compare_invariants(old, new)
    assert tool["project"]["profile"]["route_signature"] == route_signature(tool["project"])
    assert [b["cable_kp_m"] for b in new["bodies"]] == pytest.approx([b["cable_kp_m"] for b in old["bodies"]])


def test_geodesic_subdivision_produces_rhumb_approximation_not_false_measured_profile():
    p = project()
    p["route"]["points"] = [{"id": "a", "longitude": 0, "latitude": 60, "depth_m": 50}, {"id": "b", "longitude": 30, "latitude": 60, "depth_m": 50}]
    length = inverse(0, 60, 30, 60)[0]
    profile(p, [(0, 50), (length, 50)])
    before = analyze_project(p)
    result = subdivide_project(p, {"spacing_m": 10000, "mode": "geodesic_as_rhumb"})
    after = analyze_project(result["project"])
    assert after["summary"]["surface_length_m"] < before["summary"]["surface_length_m"]
    assert after["summary"]["cable_length_m"] == pytest.approx(before["summary"]["cable_length_m"], abs=1e-6)
    assert after["summary"]["bottom_length_m"] is None
    assert all(l["mode"] == "fixed" for l in after["legs"])
    assert max(p["latitude"] for p in result["project"]["route"]["points"]) > 60


def test_depth_allocation_finds_every_repeated_threshold_crossing():
    p = project()
    profile(p, [(0, 50), (250, 150), (500, 50), (750, 150), (1000, 50)])
    rules = [{"min_depth_m": 0, "max_depth_m": 100, "cable_type_id": "A"},
             {"min_depth_m": 100, "max_depth_m": None, "cable_type_id": "B"}]
    before = analyze_project(p)
    result = define_cables_by_depth(p, rules)
    after = analyze_project(result["project"])
    assert [t["kp_m"] for t in result["report"]["transitions"]] == pytest.approx([125, 375, 625, 875])
    assert [l["cable_type_id"] for l in after["legs"]] == ["A", "B", "A", "B", "A"]
    compare_invariants(before, after, ("surface_length_m", "bottom_length_m", "cable_length_m", "material_length_m"))
    assert after["summary"]["material_cost"] == pytest.approx(505 * 2 + 505 * 3)


def test_depth_conversion_at_exact_terrain_vertex_and_range_boundaries():
    p = project()
    profile(p, [(0, 50), (200, 100), (400, 150), (600, 100), (800, 50), (1000, 50)])
    rules = {"start_kp_m": 100, "end_kp_m": 900, "bands": [
        {"min_depth_m": 0, "max_depth_m": 100, "cable_type_id": "A"},
        {"min_depth_m": 100, "max_depth_m": None, "cable_type_id": "B", "slack_pct": 2}]}
    tool = define_cables_by_depth(p, rules)
    a = analyze_project(tool["project"])
    assert [t["kp_m"] for t in tool["report"]["transitions"]] == pytest.approx([200, 600])
    assert len(a["legs"]) == 5
    assert a["summary"]["cable_length_m"] == pytest.approx(1014)


def test_depth_allocation_rejects_missing_or_stale_depth_and_bad_bands():
    bands = [{"min_depth_m": 0, "max_depth_m": None, "cable_type_id": "A"}]
    p = project()
    p["route"]["points"][-1]["longitude"] *= 2
    with pytest.raises(ValueError, match="有效剖面"):
        define_cables_by_depth(p, bands)
    p = project()
    profile(p, [(0, 50), (500, None), (1000, 50)])
    with pytest.raises(ValueError, match="缺测"):
        define_cables_by_depth(p, bands)
    p = project()
    with pytest.raises(ValueError, match="连续"):
        define_cables_by_depth(p, [{"min_depth_m": 0, "max_depth_m": 50, "cable_type_id": "A"}, {"min_depth_m": 60, "max_depth_m": None, "cable_type_id": "B"}])


def test_depth_allocation_fixed_total_preserved_and_explicit_conversion():
    p = project(fixed=True)
    profile(p, [(0, 50), (1000, 150)])
    bands = [{"min_depth_m": 0, "max_depth_m": 100, "cable_type_id": "A", "slack_pct": 5},
             {"min_depth_m": 100, "max_depth_m": None, "cable_type_id": "B", "slack_pct": 5}]
    a = define_cables_by_depth(p, bands)
    assert analyze_project(a["project"])["summary"]["cable_length_m"] == pytest.approx(1010)
    assert a["warnings"]
    b = define_cables_by_depth(p, {"bands": bands, "convert_fixed": True})
    assert analyze_project(b["project"])["summary"]["cable_length_m"] == pytest.approx(1050)


def test_slack_template_range_and_fixed_policies():
    p = project()
    tool = apply_slack_template(p, {"mapping": {"A": 3}, "start_kp_m": 250, "end_kp_m": 750})
    a = analyze_project(tool["project"])
    assert [l["surface_slack_pct"] for l in a["legs"]] == pytest.approx([1, 3, 1])
    assert a["summary"]["cable_length_m"] == pytest.approx(1020)
    p = project(fixed=True)
    keep = apply_slack_template(p, {"mapping": {"A": 3}})
    assert analyze_project(keep["project"])["summary"]["cable_length_m"] == pytest.approx(1010)
    convert = apply_slack_template(p, {"mapping": {"A": 3}, "fixed_policy": "convert"})
    assert analyze_project(convert["project"])["summary"]["cable_length_m"] == pytest.approx(1030)
    with pytest.raises(ValueError, match="固定"):
        apply_slack_template(p, {"mapping": {"A": 3}, "fixed_policy": "reject"})


@pytest.mark.parametrize("fixed", [False, True])
def test_split_merge_preserves_cable_material_terrain_bodies_allowances_events(fixed):
    p = project(fixed=fixed)
    p = subdivide_project(p, {"spacing_m": 500})["project"]
    p["bodies"] = [{"id": "a", "kp_m": 200, "length_m": 10, "cost": 100},
                   {"id": "b", "cable_kp_m": 800, "length_m": 10, "cost": 100},
                   {"id": "extra", "kp_m": 500, "length_m": 15, "cost": 100, "length_mode": "additional"}]
    p["route"]["allowances"] = [{"kp_m": 500, "length_m": 20}, {"kp_m": 600, "length_m": 5}]
    p["route"]["legs"][0]["allowance_m"] = 10
    p["events"] = [{"kp_m": 500, "stop_hours": 2, "extra_cost": 100}, {"kp_m": 700, "stop_hours": 1, "extra_cost": 50}]
    before = analyze_project(p)
    split = split_project(p, {"point_index": 1})
    parts = split["projects"]
    part_analyses = [analyze_project(part) for part in parts]
    for key in ("cable_length_m", "material_length_m", "body_cost", "material_cost", "cost_total", "time_hours"):
        assert sum(a["summary"][key] for a in part_analyses) == pytest.approx(before["summary"][key], rel=1e-9, abs=1e-5)
    assert len(parts[0]["events"]) == 1 and len(parts[1]["events"]) == 1
    assert any(b["id"] == "extra" for b in parts[0]["bodies"])
    merged = merge_projects(parts)
    after = analyze_project(merged["project"])
    compare_invariants(before, after)
    assert {b["id"]: b["cable_kp_m"] for b in after["bodies"]} == pytest.approx({b["id"]: b["cable_kp_m"] for b in before["bodies"]})


def test_arbitrary_kp_split_interpolates_and_rejects_cut_through_rigid_body():
    p = project()
    tool = split_project(p, {"kp_m": 350})
    assert tool["split_kp_m"] == pytest.approx(350)
    assert analyze_project(tool["projects"][0])["summary"]["surface_length_m"] == pytest.approx(350)
    p["bodies"] = [{"id": "rigid", "cable_kp_m": 340, "length_m": 30}]
    with pytest.raises(ValueError, match="附属体"):
        split_project(p, {"kp_m": 350})


def test_merge_new_connector_is_explicit_unmeasured_and_preserves_source_fixed_lengths():
    a, b = project(1000, fixed=True), project(500, offset=2000, fixed=True)
    with pytest.raises(ValueError, match="connect_gaps"):
        merge_projects([a, b])
    result = merge_projects([a, b], {"connect_gaps": True, "bridge_cable_type_id": "B", "bridge_slack_pct": 2})
    analysis = analyze_project(result["project"])
    assert analysis["summary"]["surface_length_m"] == pytest.approx(2500)
    assert analysis["summary"]["cable_length_m"] == pytest.approx(1010 + 1020 + 505)
    assert analysis["summary"]["bottom_length_m"] is None
    assert [l["mode"] for l in analysis["legs"]] == ["fixed", "flexible", "fixed"]
    assert result["report"]["new_connectors"]


def test_merge_conflicting_cable_libraries_are_namespaced_and_cost_policy_explicit():
    a, b = project(), project(offset=1000)
    b["cable_types"][0]["cost_per_m"] = 10
    result = merge_projects([a, b])
    analysis = analyze_project(result["project"])
    assert [l["cable_type_id"] for l in analysis["legs"]] == ["A", "source2-A"]
    assert analysis["summary"]["material_cost"] == pytest.approx(1010 * 2 + 1010 * 10)
    b["costs"]["vessel_day_rate"] = 1
    with pytest.raises(ValueError, match="cost_policy"):
        merge_projects([a, b])
    assert merge_projects([a, b], {"cost_policy": "first"})["warnings"]


@pytest.mark.parametrize("fixed", [False, True])
def test_reverse_rigid_body_leading_edges_profiles_and_double_reverse(fixed):
    p = project(fixed=fixed)
    p = subdivide_project(p, {"spacing_m": 500})["project"]
    p["route"]["legs"][1]["cable_type_id"] = "B"
    p["bodies"] = [{"id": "route-body", "kp_m": 200, "length_m": 10, "cost": 100},
                   {"id": "cable-body", "cable_kp_m": 800, "length_m": 20, "cost": 100}]
    profile(p, [(0, 50), (400, 100), (1000, 70)])
    before = analyze_project(p)
    reverse = reverse_project(p)
    after = analyze_project(reverse["project"])
    compare_invariants(before, after)
    expected = {b["id"]: before["summary"]["cable_length_m"] - b["end_m"] for b in before["bodies"]}
    assert {b["id"]: b["cable_kp_m"] for b in after["bodies"]} == pytest.approx(expected)
    twice = analyze_project(reverse_project(reverse["project"])["project"])
    compare_invariants(before, twice)
    assert {b["id"]: b["cable_kp_m"] for b in twice["bodies"]} == pytest.approx({b["id"]: b["cable_kp_m"] for b in before["bodies"]})


def test_reverse_additional_body_and_allowance_at_same_kp_preserves_physical_span():
    p = project()
    p["bodies"] = [{"id": "b", "kp_m": 500, "length_m": 10, "cost": 100, "length_mode": "additional"}]
    p["route"]["allowances"] = [{"kp_m": 500, "length_m": 20}]
    before = analyze_project(p)
    result = reverse_project(p)
    after = analyze_project(result["project"])
    compare_invariants(before, after)
    assert after["bodies"][0]["cable_kp_m"] == pytest.approx(before["summary"]["cable_length_m"] - before["bodies"][0]["end_m"])
    assert result["warnings"][0]["code"] == "REVERSE_ASSEMBLY_REPRESENTATION"


def test_stale_profile_never_revalidated_by_same_curve_transform_or_split():
    p = project()
    p["route"]["points"][-1]["longitude"] *= 2
    divided = subdivide_project(p, {"spacing_m": 500})["project"]
    assert analyze_project(divided)["summary"]["bottom_length_m"] is None
    split = split_project(divided, {"point_index": 1})
    assert all(analyze_project(part)["summary"]["bottom_length_m"] is None for part in split["projects"])
    merged = merge_projects(split["projects"])
    assert analyze_project(merged["project"])["summary"]["bottom_length_m"] is None


def test_boundary_allowance_cable_types_survive_split_merge_and_reverse():
    p = subdivide_project(project(), {"spacing_m": 500})["project"]
    p["route"]["legs"][1]["cable_type_id"] = "B"
    p["route"]["legs"][0]["allowance_m"] = 10
    p["route"]["allowances"] = [{"kp_m": 500, "length_m": 20}]
    before = analyze_project(p)
    assert {m["cable_type_id"]: m["length_m"] for m in before["materials"]} == pytest.approx({"A": 515, "B": 525})
    reverse = analyze_project(reverse_project(p)["project"])
    compare_invariants(before, reverse)
    assert {m["cable_type_id"]: m["length_m"] for m in reverse["materials"]} == pytest.approx({m["cable_type_id"]: m["length_m"] for m in before["materials"]})
    split = split_project(p, {"point_index": 1})
    joined = analyze_project(merge_projects(split["projects"])["project"])
    compare_invariants(before, joined)
    assert {m["cable_type_id"]: m["length_m"] for m in joined["materials"]} == pytest.approx({m["cable_type_id"]: m["length_m"] for m in before["materials"]})


def test_allowance_override_material_cost_leg_sums_and_reference_validation():
    p = project()
    p["route"]["allowances"] = [{"kp_m": 500, "length_m": 10, "cable_type_id": "B"}]
    a = analyze_project(p)
    assert a["summary"]["material_cost"] == pytest.approx(2020 + 30)
    assert sum(l["material_cost"] for l in a["legs"]) == pytest.approx(a["summary"]["material_cost"])
    p["route"]["allowances"][0]["cable_type_id"] = "UNKNOWN"
    with pytest.raises(ValueError, match="未知缆型"):
        analyze_project(p)


def test_synthetic_source_mark_survives_split_merge():
    p = subdivide_project(project(), {"spacing_m": 500})["project"]
    p["profile"]["source"] = "synthetic_test"
    parts = split_project(p, {"point_index": 1})["projects"]
    a = analyze_project(merge_projects(parts)["project"])
    assert "SYNTHETIC_BATHYMETRY" in {w["code"] for w in a["warnings"]}
    assert not a["profile_metadata"]["measured"]


def test_manufacturing_references_reverse_split_merge_with_boundary_owner_and_no_cost():
    p = subdivide_project(project(), {"spacing_m": 500})["project"]
    a = analyze_project(p)
    cut = a["rpl"][1]["cable_kp_m"]
    total = a["summary"]["cable_length_m"]
    p["assembly_references"] = [{"id":"before","cable_kp_m":100}, {"id":"cut","cable_kp_m":cut}, {"id":"after","cable_kp_m":700}]
    a = analyze_project(p)
    assert len([s for s in a["sld"] if s["kind"]=="reference"])==3
    assert all(s["start_m"]==s["end_m"] for s in a["assembly_references"])
    reverse = reverse_project(p)["project"]
    assert {r["id"]:r["cable_kp_m"] for r in reverse["assembly_references"]}==pytest.approx({"before":total-100,"cut":total-cut,"after":total-700})
    parts = split_project(p,{"point_index":1})["projects"]
    assert [r["id"] for r in parts[0]["assembly_references"]]==["before","cut"]
    assert parts[1]["assembly_references"][0]["cable_kp_m"]==pytest.approx(700-cut)
    joined = merge_projects(parts)["project"]
    assert {r["id"]:r["cable_kp_m"] for r in joined["assembly_references"]}==pytest.approx({"before":100,"cut":cut,"after":700})
    compare_invariants(a,analyze_project(joined))


def test_manufacturing_reference_merge_connector_and_entity_id_collisions():
    a,b=project(),project()
    a["assembly_references"]=[{"id":"shared","cable_kp_m":100}]
    b["bodies"]=[{"id":"shared","cable_kp_m":200,"length_m":5}]
    b["assembly_references"]=[{"id":"other","cable_kp_m":300}]
    for point in b["route"]["points"]:
        point["longitude"]+=.03
    b.pop("profile",None)
    result=merge_projects([a,b],{"connect_gaps":True})
    offset=result["report"]["sources"][1]["cable_offset_m"]
    refs={r["id"]:r for r in result["project"]["assembly_references"]}
    assert refs["other"]["cable_kp_m"]==pytest.approx(offset+300)
    assert result["project"]["bodies"][0]["id"]!="shared"
