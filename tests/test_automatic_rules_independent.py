"""Independent equatorial/meridional, topology and closed-depth-domain oracles.

Expected geometry is constructed directly, never by the production geometry
helpers. PROJ is shared by the independent WGS84 construction/distance oracle.
"""
from copy import deepcopy
import base64
import json
import math

from fastapi.testclient import TestClient
from pyproj import Geod
import pytest

from oceanroute.api import create_app
from oceanroute.automatic_rules import (AutomaticRuleEvaluationError,
                                       automatic_rule_catalog,
                                       check_automatic_rules,
                                       export_automatic_rules,
                                       import_automatic_rules)
from oceanroute.core import route_signature
from oceanroute.storage import ProjectStore
from oceanroute.workspace import analyze_workspace, migrate_project, workspace_action

G = Geod(ellps="WGS84")


def feature(geometry, identifier="reference"):
    result = {"type": "Feature", "properties": {"name": "Independent synthetic geometry"}, "geometry": geometry}
    if identifier is not None:
        result["id"] = identifier
    return result


def point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def line(coords):
    return {"type": "LineString", "coordinates": [list(p) for p in coords]}


def polygon(outer, holes=()):
    return {"type": "Polygon", "coordinates": [[list(p) for p in ring] for ring in [outer, *holes]]}


def box(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def workspace(geometry=None, *, positions=((0, 0), (.01, 0)), depths=None, curve="geodesic", features=None,
              profile=None, bodies=None, alternative=False, allowances=None):
    depths = [50]*len(positions) if depths is None else depths
    project = {"schema_version": 1, "id": "independent-rule-oracle", "name": "Explicit synthetic rule oracle", "crs": "EPSG:4326",
               "route": {"curve": curve, "mode": "flexible", "slack_pct": 1, "slack_basis": "surface",
                         "points": [{"id": f"p{i}", "longitude": x, "latitude": y, "depth_m": d}
                                    for i, ((x, y), d) in enumerate(zip(positions, depths))],
                         "legs": [{"cable_type_id": "C"} for _ in positions[1:]],
                         "allowances": [] if allowances is None else allowances},
               "cable_types": [{"id": "C", "name": "Declared cable", "cost_per_m": 3, "lay_speed_m_s": 1,
                                "wet_weight_n_m": 4, "ea_n": 100000}],
               "layers": [{"id": "gis", "name": "Independent synthetic GIS", "kind": "restricted", "visible": False,
                           "geojson": {"type": "FeatureCollection", "features": features if features is not None else
                                       [feature(geometry or point(.005, .0002))]}}],
               "bodies": [] if bodies is None else bodies, "review_extension": {"keep": ["route", "inventory", "datum"]}}
    if profile is not None:
        project["profile"] = {"source": "explicit independent synthetic profile", "measured": True,
                              "route_signature": route_signature(project), "samples": profile}
    result = migrate_project(project)["workspace"]
    if alternative:
        result = workspace_action(result, {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    return result


def crossing(ws, **extra):
    return {"id": "independent-cross", "path_id": ws["active_path_id"], "kind": "crossing", "end_kp_m": None,
            "selectors": [{"layer_id": "gis", "feature_ids": None}], "conditions": [], **extra}


def proximity(ws, **extra):
    return {"id": "independent-near", "path_id": ws["active_path_id"], "kind": "proximity", "end_kp_m": None,
            "around": "path", "targets": ["gis"], "selectors": [{"layer_id": "gis", "feature_ids": None}],
            "distance_m": 100, **extra}


def checked(ws, rule, **config):
    original = deepcopy(ws)
    result = check_automatic_rules(ws, {"rules": [rule], **config})
    assert ws == original
    json.dumps(result, allow_nan=False)
    return result["results"][0], result


def test_recursive_geometrycollection_containment_of_entire_route_is_an_actual_violation():
    geom = {"type": "GeometryCollection", "geometries": [point(20, 20),
            {"type": "GeometryCollection", "geometries": [polygon(box(-.02, -.02, .02, .02))]}]}
    ws = workspace(geom)
    row, _ = checked(ws, crossing(ws))
    assert row["status"] == "violations" and not row["diagnostics"]
    contained = [v for v in row["violations"] if v["event"] == "containment"]
    assert contained and contained[0]["source"]["primitive_path"] == [1, 0]
    assert contained[0]["location"]["longitude"] == pytest.approx(.005, abs=1e-9)
    assert contained[0]["values"]["angle_deg"] is None


def test_v_vertex_touch_preserves_undefined_angle_and_never_averages_two_incident_edges_to_zero():
    ws = workspace(line([(.004, .001), (.005, 0), (.006, .001)]))
    row, _ = checked(ws, crossing(ws, conditions=[{"field": "angle_deg", "comparison": "lt", "value": 35}]))
    assert row["status"] == "unknown" and not row["violations"]
    unknown = [d for d in row["diagnostics"] if d["code"] == "CROSSING_PREDICATE_UNKNOWN"]
    assert unknown and unknown[0]["event"] == "touch"
    assert unknown[0]["predicates"][0]["actual"] is None
    unconditional, _ = checked(ws, crossing(ws))
    assert unconditional["status"] == "violations"
    assert all(v["event"] == "touch" and v["values"]["angle_deg"] is None for v in unconditional["violations"])


def test_restricted_point_near_route_interior_is_ellipsoidal_proximity_not_legacy_area_only():
    ws = workspace(point(.005, .0002))
    row, _ = checked(ws, proximity(ws, distance_m=100))
    expected_distance = G.inv(.005, 0, .005, .0002)[2]
    expected_kp = G.inv(0, 0, .005, 0)[2]
    assert row["status"] == "violations" and not row["diagnostics"]
    event = row["violations"][0]
    assert event["distance_m"] == pytest.approx(expected_distance, abs=.25)
    assert event["distance_bounds_m"][0] <= expected_distance+1e-8
    assert event["distance_bounds_m"][1] >= expected_distance-1e-8
    assert event["kp_m"] == pytest.approx(expected_kp, abs=1)
    far, _ = checked(ws, proximity(ws, distance_m=10))
    assert far["status"] == "clear" and not far["violations"]


def test_polygon_hole_preserves_exclusion_and_crossings_on_both_outer_and_inner_boundaries():
    geom = polygon(box(-.01, -.01, .01, .01), [box(-.003, -.003, .003, .003)])
    inside_hole = workspace(geom, positions=((- .001, 0), (.001, 0)))
    row, _ = checked(inside_hole, crossing(inside_hole))
    assert row["status"] == "clear" and not row["violations"]
    traverse = workspace(geom, positions=((- .02, 0), (.02, 0)))
    row, _ = checked(traverse, crossing(traverse))
    assert row["status"] == "violations"
    events = sorted(row["violations"], key=lambda v: v["location"]["longitude"])
    assert [v["location"]["longitude"] for v in events] == pytest.approx([-.01, -.003, .003, .01], abs=1e-8)
    assert [v["event"] for v in events] == ["area_entry", "area_exit", "area_entry", "area_exit"]
    for v in events:
        assert v["kp_m"] == pytest.approx(G.inv(-.02, 0, v["location"]["longitude"], 0)[2], abs=.05)


def test_overlap_endpoints_and_point_contacts_are_not_discarded_as_zero_area():
    ws = workspace(line([(.002, 0), (.008, 0)]))
    row, _ = checked(ws, crossing(ws))
    assert row["status"] == "violations"
    overlaps = [v for v in row["violations"] if v["event"] == "overlap"]
    assert overlaps and min(v["location"]["longitude"] for v in overlaps) == pytest.approx(.002)
    assert max(v["location"]["longitude"] for v in overlaps) == pytest.approx(.008)
    ps = workspace(point(.005, 0))
    row, _ = checked(ps, crossing(ps))
    assert row["status"] == "violations" and row["violations"][0]["event"] == "touch"


def test_full_curve_high_latitude_geodesic_crosses_meridian_feature_while_rhumb_does_not():
    geom = line([(45, 75), (45, 76)])
    positions = ((0, 70), (90, 70))
    az, _, length = G.inv(*positions[0], *positions[1])
    lon, lat, _ = G.fwd(*positions[0], az, length/2)
    ws = workspace(geom, positions=positions, curve="geodesic")
    row, _ = checked(ws, crossing(ws, start_kp_m=length/2-20000, end_kp_m=length/2+20000))
    assert row["status"] == "violations" and not row["diagnostics"]
    event = row["violations"][0]
    assert event["location"]["longitude"] == pytest.approx(lon, abs=1e-8)
    assert event["location"]["latitude"] == pytest.approx(lat, abs=1e-8)
    assert event["kp_m"] == pytest.approx(length/2, abs=.05)
    assert event["values"]["angle_deg"] == pytest.approx(90, abs=1e-6)
    rhumb = workspace(geom, positions=positions, curve="rhumb")
    # The ellipsoidal parallel has constant latitude 70; it is outside this
    # meridian feature's explicit 75..76-degree native segment.
    parallel_length = 6378137/math.sqrt(1-(1/298.257223563)*(2-1/298.257223563)*math.sin(math.radians(70))**2)*math.cos(math.radians(70))*math.pi/2
    row, _ = checked(rhumb, crossing(rhumb, start_kp_m=parallel_length/2-20000, end_kp_m=parallel_length/2+20000))
    assert row["status"] == "clear" and not row["violations"]


def test_native_179_to_minus179_source_edge_remains_geographic_linear_long_way():
    ws = workspace(line([(179, 0), (-179, 0)]), positions=((0, -.001), (0, .001)))
    row, _ = checked(ws, crossing(ws))
    assert row["status"] == "violations" and not row["diagnostics"]
    event = row["violations"][0]
    assert event["location"]["longitude"] == pytest.approx(0, abs=1e-9)
    assert event["location"]["latitude"] == pytest.approx(0, abs=1e-9)
    assert event["values"]["angle_deg"] == pytest.approx(90, abs=1e-6)


def test_altercourse_subject_uses_true_arrival_initial_tangents_not_short_chord_azimuths():
    start, end = (0, 70), (90, 70)
    az, _, length = G.inv(*start, *end)
    midpoint = G.fwd(*start, az, length/2)[:2]
    ws = workspace(point(*midpoint), positions=(start, midpoint, end))
    row, _ = checked(ws, proximity(ws, around="altercourses", distance_m=1))
    assert row["status"] == "clear" and row["coverage"]["subject_count"] == 0
    assert not row["violations"]


def test_repeated_waypoint_zero_leg_does_not_invent_duplicate_altercourse_subjects():
    ws = workspace(point(.005, 0), positions=((0, 0), (.005, 0), (.005, 0), (.005, .005)))
    row, _ = checked(ws, proximity(ws, around="altercourses", distance_m=1))
    # Both vertices have an adjacent zero leg and core turn=None. They are not
    # two independently well-defined alteration events at the same location.
    assert row["coverage"]["subject_count"] == 0
    assert row["status"] == "clear" and not row["violations"]


@pytest.mark.parametrize("identifier,expected_x", [(1, .002), ("1", .006), ("index:2", .008)])
def test_typed_native_identity_never_collides_with_string_or_source_index(identifier, expected_x):
    fs = [feature(line([(.002, -.001), (.002, .001)]), 1),
          feature(line([(.006, -.001), (.006, .001)]), "1"),
          feature(line([(.004, -.001), (.004, .001)]), None),
          feature(line([(.008, -.001), (.008, .001)]), "index:2")]
    ws = workspace(features=fs)
    row, _ = checked(ws, crossing(ws, selectors=[{"layer_id": "gis", "feature_ids": [identifier]}]))
    assert row["status"] == "violations"
    assert all(v["location"]["longitude"] == pytest.approx(expected_x, abs=1e-8) for v in row["violations"])
    assert all(type(v["source"]["feature_id"]) is type(identifier) for v in row["violations"])
    indexed, _ = checked(ws, crossing(ws, selectors=[{"layer_id": "gis", "feature_indexes": [2]}]))
    assert all(v["location"]["longitude"] == pytest.approx(.004, abs=1e-8) and v["source"]["feature_id"] is None for v in indexed["violations"])


@pytest.mark.parametrize("selector,code", [({"layer_id": "absent", "feature_ids": None}, "LAYER_REFERENCE_MISSING"),
                                         ({"layer_id": "gis", "feature_ids": ["absent"]}, "FEATURE_REFERENCE_MISSING"),
                                         ({"layer_id": "gis", "feature_indexes": [10]}, "FEATURE_REFERENCE_MISSING")])
def test_missing_references_cannot_be_a_clear_empty_selection(selector, code):
    ws = workspace(line([(.005, -.001), (.005, .001)]))
    row, result = checked(ws, crossing(ws, selectors=[selector]))
    assert row["status"] == "reference_error" and any(d["code"] == code for d in row["diagnostics"])
    assert result["errors"] and result["errors"][0]["location"] is None


def test_ambiguous_duplicate_native_id_is_reference_error_instead_of_arbitrary_first_match():
    ws = workspace(features=[feature(point(.003, 0), 1), feature(point(.007, 0), 1)])
    row, _ = checked(ws, crossing(ws, selectors=[{"layer_id": "gis", "feature_ids": [1]}]))
    assert row["status"] == "reference_error"
    assert any(d["code"] == "FEATURE_REFERENCE_AMBIGUOUS" for d in row["diagnostics"])


def test_whole_path_depth_filter_checks_complete_eligible_interval_not_only_unfiltered_nearest_point():
    ws = workspace(point(.002, .0001), depths=[0, 100])
    row, _ = checked(ws, proximity(ws, distance_m=400, water_depth_m={"min_m": 50, "max_m": 100}))
    assert row["status"] == "violations" and not row["diagnostics"]
    event = row["violations"][0]
    expected = G.inv(.005, 0, .002, .0001)[2]
    assert event["distance_m"] == pytest.approx(expected, abs=.25)
    assert event["location"]["longitude"] == pytest.approx(.005, abs=1e-6)
    assert event["location"]["depth_m"] == pytest.approx(50, abs=1e-5)
    assert row["coverage"]["depth_eligible_ranges_m"][0][0] == pytest.approx(G.inv(0, 0, .005, 0)[2])


@pytest.mark.parametrize("eligible_depth,target_x", [(0, 0), (50, .005), (100, .01)])
def test_closed_equal_depth_filter_preserves_isolated_interior_and_endpoint_locations(eligible_depth, target_x):
    ws = workspace(point(target_x, .0001), depths=[0, 100])
    row, _ = checked(ws, proximity(ws, distance_m=100, water_depth_m={"min_m": eligible_depth, "max_m": eligible_depth}))
    assert row["status"] == "violations" and not row["diagnostics"]
    event = row["violations"][0]
    assert event["distance_m"] == pytest.approx(G.inv(target_x, 0, target_x, .0001)[2], abs=.25)
    assert event["location"]["depth_m"] == pytest.approx(eligible_depth)
    assert event["kp_m"] == pytest.approx(G.inv(0, 0, target_x, 0)[2], abs=.05)


def test_known_eligible_depth_isolated_next_to_missing_profile_intervals_keeps_true_evidence_and_unknowns():
    length = G.inv(0, 0, .01, 0)[2]
    profile = [{"kp_m": 0, "depth_m": None}, {"kp_m": length/2, "depth_m": 50}, {"kp_m": length, "depth_m": None}]
    ws = workspace(point(.005, .0001), profile=profile)
    row, _ = checked(ws, proximity(ws, distance_m=100, water_depth_m={"min_m": 50, "max_m": 50}))
    assert row["status"] == "violations" and row["violations"]
    assert not row["coverage"]["depth_filter_complete"]
    assert any(d["code"] == "PROXIMITY_DEPTH_UNAVAILABLE" for d in row["diagnostics"])


def test_explicit_stale_depth_profile_is_unknown_not_silently_replaced_by_incidental_waypoint_depth():
    length = G.inv(0, 0, .01, 0)[2]
    ws = workspace(point(.005, .0001), profile=[{"kp_m": 0, "depth_m": 50}, {"kp_m": length, "depth_m": 50}])
    ws["paths"][0]["project"]["profile"]["route_signature"] = "0"*64
    row, _ = checked(ws, proximity(ws))
    assert row["status"] == "incomplete" and not row["violations"]
    assert any(d["code"] == "PROXIMITY_DEPTH_UNAVAILABLE" for d in row["diagnostics"])


def test_selected_range_endpoint_contact_is_retained_with_actual_curve_kp():
    x, length = .005, G.inv(0, 0, .005, 0)[2]
    ws = workspace(line([(x, -.001), (x, .001)]))
    row, _ = checked(ws, crossing(ws, end_kp_m=length))
    assert row["status"] == "violations"
    assert any(v["kp_m"] == pytest.approx(length, abs=.05) for v in row["violations"])


@pytest.mark.parametrize("config", [{"max_work_units": 1}, {"max_features": 1}, {"max_vertices": 1}, {"max_events": 1}, {"max_output_bytes": 1024}])
def test_declared_budget_exhaustion_rejects_whole_check_never_returns_partial_clear(config):
    ws = workspace(features=[feature(line([(.002, -.001), (.002, .001)]), "a"),
                             feature(line([(.008, -.001), (.008, .001)]), "b")])
    with pytest.raises(AutomaticRuleEvaluationError):
        check_automatic_rules(ws, {"rules": [crossing(ws)], **config})


def test_polar_overlay_unsupported_is_explicit_unknown_instead_of_clear_or_skipped():
    ws = workspace(line([(0, 86), (1, 86)]), positions=((.5, 85.9), (.5, 86.1)))
    row, _ = checked(ws, crossing(ws))
    assert row["status"] == "unknown" and not row["violations"]
    assert any(d["code"] == "GEOMETRY_POLAR_SCOPE" for d in row["diagnostics"])


def test_true_radius_boundary_uses_uncertainty_not_arbitrary_clear_or_invented_distance():
    expected = G.inv(.005, 0, .005, .0002)[2]
    ws = workspace(point(.005, .0002))
    row, _ = checked(ws, proximity(ws, distance_m=expected))
    assert row["status"] in {"violations", "unknown"}
    if row["status"] == "violations":
        assert row["violations"][0]["distance_bounds_m"][1] <= expected+1e-9
    else:
        assert any(d["code"] == "PROXIMITY_THRESHOLD_UNCERTAIN" for d in row["diagnostics"])


def test_export_import_preserves_null_end_typed_selectors_and_missing_refs_without_mutating_stock():
    ws = workspace(alternative=True)
    ws["automatic_rules"] = [crossing(ws, path_id="absent-path", selectors=[{"layer_id": "gis", "feature_ids": [1, "1"]}])]
    original = deepcopy(ws)
    exported = export_automatic_rules(ws)
    imported = import_automatic_rules(ws, exported["text"], {"mode": "replace", "binding": "retain"})
    assert ws == original
    assert imported["rules"][0]["end_kp_m"] is None
    assert imported["rules"][0]["path_id"] == "absent-path"
    assert imported["checks"]["results"][0]["status"] == "reference_error"
    assert imported["workspace"]["assemblies"] == ws["assemblies"]
    assert imported["workspace"]["associations"] == ws["associations"]


def test_actual_http_preview_atomic_apply_save_reopen_and_422_preserve_shared_stock(tmp_path):
    ws = workspace(line([(.004, -.001), (.004, .001)]), alternative=True)
    original = deepcopy(ws)
    store = ProjectStore(tmp_path/"independent.sqlite3")
    app = create_app(store)
    with TestClient(app) as client:
        saved_response = client.post("/api/workspaces", json=ws)
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()["workspace"]
        response = client.post("/api/automatic-rules/check", json={"workspace": saved, "config": {"rules": [crossing(saved)]}})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["checks"]["results"][0]["status"] == "violations"
        assert result["workspace"]["saved_revision"] == saved["saved_revision"] == 1
        assert client.get(f'/api/workspaces/{saved["id"]}').json() == saved
        assert result["workspace"]["assemblies"] == saved["assemblies"]
        assert result["workspace"]["associations"] == saved["associations"]
        assert result["workspace"]["paths"] == saved["paths"]
        before = analyze_workspace(saved)["summary"]
        accepted = client.post("/api/workspaces", json=result["workspace"])
        assert accepted.status_code == 200, accepted.text
        accepted = accepted.json()["workspace"]
        assert accepted["saved_revision"] == 2 and analyze_workspace(accepted)["summary"] == before
        rejected = client.post("/api/automatic-rules/check", json={"workspace": accepted, "config": {"rules": [crossing(accepted)], "max_work_units": 1}})
        assert rejected.status_code == 422 and "code" in rejected.json()
        assert client.get(f'/api/workspaces/{saved["id"]}').json() == accepted
        assert len(client.get(f'/api/workspaces/{saved["id"]}/revisions').json()) == 2
    # A genuinely new app/job owner opens the same durable file. The stores use
    # bounded per-operation connections; this does not claim a field device.
    with TestClient(create_app(ProjectStore(tmp_path/"independent.sqlite3"))) as client:
        reread = client.get(f'/api/workspaces/{ws["id"]}').json()
        assert reread == accepted
        response = client.post("/api/automatic-rules/check", json={"workspace": reread})
        assert response.status_code == 200 and response.json()["checks"]["results"][0]["status"] == "violations"
    assert ws == original


@pytest.mark.parametrize("invalid", [None, [], "string", 1])
def test_actual_http_invalid_complete_workspace_is_422_never_partial_report(tmp_path, invalid):
    with TestClient(create_app(ProjectStore(tmp_path/"invalid.sqlite3"))) as client:
        response = client.post("/api/automatic-rules/check", json={"workspace": invalid})
        assert response.status_code == 422 and "checks" not in response.json()


def test_catalog_genuine_native_ids_and_no_native_id_indexes_remain_distinct():
    ws = workspace(features=[feature(point(.001, 0), 1), feature(point(.002, 0), "1"),
                             feature(point(.003, 0), None), feature(point(.004, 0), "index:2")])
    fs = automatic_rule_catalog(ws)["layers"][0]["features"]
    assert fs[0]["selector"] == {"layer_id": "gis", "feature_ids": [1]}
    assert fs[1]["selector"] == {"layer_id": "gis", "feature_ids": ["1"]}
    assert fs[2]["selector"] == {"layer_id": "gis", "feature_indexes": [2]}
    assert fs[3]["selector"] == {"layer_id": "gis", "feature_ids": ["index:2"]}


@pytest.mark.parametrize("comparison,expected", [("lt", "clear"), ("le", "violations"),
                                                ("gt", "clear"), ("ge", "violations")])
def test_explicit_angle_threshold_equality_uses_requested_comparator(comparison, expected):
    ws = workspace(line([(.005, -.001), (.005, .001)]))
    row, _ = checked(ws, crossing(ws, conditions=[{"field": "angle_deg", "comparison": comparison, "value": 90}]))
    assert row["status"] == expected
    if row["violations"]:
        assert row["violations"][0]["values"]["angle_deg"] == pytest.approx(90, abs=1e-9)


@pytest.mark.parametrize("mode,angle_comparison,expected", [("any", "gt", "violations"),
                                                           ("all", "gt", "unknown"),
                                                           ("all", "lt", "clear"),
                                                           ("any", "lt", "unknown")])
def test_three_valued_predicates_do_not_turn_missing_body_distance_into_zero(mode, angle_comparison, expected):
    ws = workspace(line([(.005, -.001), (.005, .001)]))
    row, _ = checked(ws, crossing(ws, match_mode=mode, conditions=[
        {"field": "angle_deg", "comparison": angle_comparison, "value": 35},
        {"field": "body_distance_m", "comparison": "lt", "value": 1}]))
    assert row["status"] == expected
    if row["violations"]:
        assert row["violations"][0]["values"]["body_distance_m"] is None
        assert row["violations"][0]["predicates"][1]["triggered"] is None


def slope_source(*, missing=False):
    xy = [-200+50*i for i in range(9)]
    rows = [[1.70141e38 if missing and x == y == 0 else 1000+.2*x for x in xy] for y in xy]
    text = "DSAA\n9 9\n-200 200\n-200 200\n0 1000000\n"+"\n".join(" ".join(map(str,r)) for r in rows)+"\n"
    return {"id": "independent-body-plane", "kind": "surfer",
            "source_crs": "+proj=aeqd +lat_0=0 +lon_0=0 +datum=WGS84 +units=m",
            "depth_positive": "down", "depth_units": "m", "vertical_datum": "INDEPENDENT_TEST_DATUM",
            "sampling": {"method": "linear"}, "data_base64": base64.b64encode(text.encode()).decode()}


def test_body_slope_consumption_uses_real_two_dimensional_gradient_and_query_witness_not_profile_or_geometric_depth():
    ws = workspace(bodies=[{"id": "body-0", "kp_m": 0, "length_m": 0, "cost": 2}])
    ws["terrain_sources"] = [slope_source()]
    rule = proximity(ws, around="bodies", targets=["slopes"], slope_threshold_deg=5)
    rule.pop("selectors")
    row, output = checked(ws, rule)
    expected = math.degrees(math.atan(.2))
    assert row["status"] == "violations" and row["violations"]
    assert output["budget"]["terrain_query_count"] == 321
    for v in row["violations"]:
        assert v["slope_deg"] == pytest.approx(expected, abs=1e-8)
        assert v["gradient_height"] == pytest.approx([-.2, 0], abs=1e-10)
        assert v["location"]["depth_m"] is None
        assert v["location"]["location_kind"] == "geometric_child_centroid_not_queried"
        witness = v["sampled_witness"]
        assert witness["depth_m"] == pytest.approx(1000+.2*witness["x_m"], abs=1e-8)
        assert v["source_id"] == witness["source_id"] == "independent-body-plane"
        assert v["source_fingerprint"] == witness["source_fingerprint"] and len(v["source_fingerprint"]) == 64
        assert v["continuous_bed_verified"] is False
    coverage = row["coverage"]["slope_neighborhoods"][0]
    assert coverage["quality"]["sample_complete"] and coverage["quality"]["triangle_complete"]
    assert coverage["quality"]["coverage_fraction"] < 1 and coverage["quality"]["continuous_bed_verified"] is False


def test_body_slope_unknown_window_cannot_pass_even_when_known_triangle_maximum_is_under_threshold():
    ws = workspace(bodies=[{"id": "body-0", "kp_m": 0, "length_m": 0, "cost": 2}])
    ws["terrain_sources"] = [slope_source(missing=True)]
    rule = proximity(ws, around="bodies", targets=["slopes"], slope_threshold_deg=89)
    rule.pop("selectors")
    row, _ = checked(ws, rule)
    assert row["status"] == "incomplete" and not row["violations"]
    assert any(d["code"] == "TERRAIN_SLOPE_WINDOW_INCOMPLETE" for d in row["diagnostics"])
    quality = row["coverage"]["slope_neighborhoods"][0]["quality"]
    assert quality["max_sampled_slope_deg"] < 89 and not quality["triangle_complete"]


def test_actual_curve_chunk_admission_name_rejects_old_projection_radius_noop():
    ws = workspace(line([(.005, -.001), (.005, .001)]))
    row, _ = checked(ws, crossing(ws), max_curve_chunk_m=1000)
    assert row["status"] == "violations"
    with pytest.raises(ValueError):
        check_automatic_rules(ws, {"rules": [crossing(ws)], "max_projection_radius_m": 100000})


def test_actual_body_location_uses_surface_kp_after_physical_slack_and_allowance_mapping():
    # Uniform 1% surface slack + 40m allowance at surface KP200. Past that
    # reserve, physical cable station615 maps to (615-40)/1.01 metres.
    expected_kp = (615-40)/1.01
    lon, lat, _ = G.fwd(0, 0, 90, expected_kp)
    ws = workspace(point(lon, lat), bodies=[{"id": "physical-joint", "kp_m": 0,
                   "cable_kp_m": 615, "length_m": 0, "cost": 7}],
                   allowances=[{"id": "reserve", "kp_m": 200, "length_m": 40}])
    row, _ = checked(ws, proximity(ws, around="bodies", distance_m=1))
    assert row["status"] == "violations" and len(row["violations"]) == 1
    v = row["violations"][0]
    assert v["subject"]["kp_m"] == pytest.approx(expected_kp, abs=1e-8)
    assert v["location"]["longitude"] == pytest.approx(lon, abs=1e-10)
    assert v["distance_m"] < 1e-6


def test_distinct_colocated_bodies_are_real_zero_distance_pairs_but_self_identity_is_excluded():
    ws = workspace(bodies=[{"id": "a", "kp_m": 0, "length_m": 0},
                           {"id": "b", "kp_m": 0, "length_m": 0}])
    rule = proximity(ws, around="bodies", targets=["bodies"], distance_m=0)
    rule.pop("selectors")
    row, _ = checked(ws, rule)
    assert row["status"] == "violations" and len(row["violations"]) == 2
    assert {(v["subject"]["id"], v["target"]["id"]) for v in row["violations"]} == {("a", "b"), ("b", "a")}
    assert all(v["distance_m"] == 0 for v in row["violations"])


def test_missing_second_selection_preserves_real_first_selection_violation_and_reference_unknown():
    ws = workspace(line([(.005, -.001), (.005, .001)]))
    row, result = checked(ws, crossing(ws, selectors=[{"layer_id": "gis", "feature_ids": None},
                                                    {"layer_id": "absent", "feature_ids": None}]))
    assert row["status"] == "reference_error" and row["violations"]
    assert any(d["code"] == "LAYER_REFERENCE_MISSING" for d in row["diagnostics"])
    assert {e["status"] for e in result["errors"]} == {"violation", "reference_error"}
