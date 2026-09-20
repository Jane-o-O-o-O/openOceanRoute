"""Small real installed-API automatic-rule workflow, using only stdlib.

Adapters return actual successful JSON objects. For an expected HTTP 422 they
must return {"_http_status": 422, "_error": <actual response JSON>}; a fabricated
failure or swallowed exception cannot establish this evidence. The second GET
is deliberately the post-save read: the caller can close and recreate its
actual ASGI/storage owner or restart its installed HTTP server before that read.
This helper itself never claims to close a server or import a production module.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time
from urllib.parse import quote
from uuid import uuid4


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _near(actual, expected, tolerance, name):
    _require(isinstance(actual, (int, float)) and not isinstance(actual, bool)
             and math.isfinite(actual) and abs(actual - expected) <= tolerance,
             f"{name}: {actual!r} differs from {expected!r}")


def run_automatic_rules_smoke(post_json, get_json):
    """Run a finite synthetic workflow; no production geometry helper oracle."""
    started = time.monotonic()
    calls = []

    def post(path, payload, *, status=200):
        _json(payload)
        result = post_json(path, deepcopy(payload))
        _require(isinstance(result, dict), f"{path}: expected actual JSON object")
        _json(result)
        _require(result.get("_http_status", 200) == status,
                 f"{path}: expected HTTP {status}, got {result}")
        calls.append({"method": "POST", "path": path, "http_status": status})
        return result

    def get(path):
        result = get_json(path)
        _require(isinstance(result, dict), f"{path}: expected actual JSON object")
        _json(result)
        calls.append({"method": "GET", "path": path, "http_status": 200})
        return result

    center_lon = math.degrees(500 / 6378137.0)  # Exact equatorial WGS84 KP.
    features = [
        {"id": 1, "type": "Feature", "properties": {"name": "Numeric ID"}, "geometry": {
            "type": "LineString", "coordinates": [[.0045, -.001], [.0045, .001]]}},
        {"id": "1", "type": "Feature", "properties": {"name": "Text ID"}, "geometry": {
            "type": "LineString", "coordinates": [[.0075, -.001], [.0075, .001]]}},
        {"type": "Feature", "properties": {"name": "Unnamed source index 2"}, "geometry": {
            "type": "Point", "coordinates": [.005, .0002]}},
        {"id": "index:2", "type": "Feature", "properties": {}, "geometry": {
            "type": "Point", "coordinates": [.006, .0004]}},
    ]
    project = {
        "id": "synthetic-automatic-installed-" + str(uuid4()), "schema_version": 1,
        "name": "Explicit synthetic installed automatic-rule workflow; not field data",
        "crs": "EPSG:4326", "route": {
            "curve": "geodesic", "mode": "flexible", "slack_pct": 1, "slack_basis": "surface",
            "points": [{"id": "a", "longitude": 0, "latitude": 0, "depth_m": 50},
                       {"id": "b", "longitude": .01, "latitude": 0, "depth_m": 150}],
            "legs": [{"cable_type_id": "SYNTHETIC-C"}],
            "allowances": [{"id": "reserve", "kp_m": 200, "length_m": 40}]},
        "cable_types": [{"id": "SYNTHETIC-C", "name": "Declared synthetic cable",
                         "cost_per_m": 3, "lay_speed_m_s": 1, "wet_weight_n_m": 4, "ea_n": 1e6}],
        "bodies": [{"id": "point-joint", "kp_m": 500, "length_m": 0, "cost": 25}],
        "layers": [{"id": "gis", "visible": False, "kind": "reference", "name": "Synthetic GIS",
                    "geojson": {"type": "FeatureCollection", "features": features}}],
        "terrain_sources": [{"id": "plane", "name": "Explicit synthetic full 2D plane",
            "kind": "xyz", "enabled": True, "priority": 1,
            "source_crs": f"+proj=aeqd +lat_0=0 +lon_0={center_lon:.15f} +datum=WGS84 +units=m +type=crs",
            "depth_positive": "down", "depth_units": "m", "vertical_datum": "synthetic-plane-datum",
            "sampling": {"method": "linear", "max_gap_m": 1000},
            "text": "\n".join(f"{x} {y} {1000+.1*x+.2*y}" for y in (-200, 0, 200) for x in (-200, 0, 200))}],
        "synthetic_extension": {"field_data": False, "preserve": ["stock", "route", "source"]},
    }
    migrated = post("/api/workspace/migrate", {"project": project})
    copied = post("/api/workspace/action", {"workspace": migrated["workspace"], "config": {
        "action": "copy_path", "assembly_policy": "alternative", "name": "Shared synthetic alternative"}})
    initial = post("/api/workspaces", copied["workspace"])["workspace"]
    original = deepcopy(initial)
    path_id = initial["active_path_id"]
    _require(len(initial["paths"]) == 2 and len(initial["assemblies"]) == 1,
             "Expected genuine two paths sharing one physical inventory")
    catalog = post("/api/automatic-rules/catalog", {"workspace": initial})
    rows = next(layer for layer in catalog["layers"] if layer["id"] == "gis")["features"]
    _require(type(rows[0]["id"]) is int and rows[0]["id"] == 1
             and type(rows[1]["id"]) is str and rows[1]["id"] == "1"
             and rows[2]["selector"] == {"layer_id": "gis", "feature_indexes": [2]}
             and rows[3]["selector"] == {"layer_id": "gis", "feature_ids": ["index:2"]},
             "Typed native IDs must differ from source indexes and index-like text IDs")
    common = {"enabled": True, "path_id": path_id, "start_kp_m": 0, "end_kp_m": None}
    cross = {**common, "id": "numeric-cross", "kind": "crossing",
             "selectors": [{"layer_id": "gis", "feature_ids": [1]}],
             "conditions": [{"field": "angle_deg", "comparison": "gt", "value": 85}], "match_mode": "all"}
    rules = [cross,
        {**cross, "id": "text-cross", "selectors": [{"layer_id": "gis", "feature_ids": ["1"]}]},
        {**common, "id": "index-near", "kind": "proximity", "around": "path", "targets": ["gis"],
         "distance_m": 100, "selectors": [{"layer_id": "gis", "feature_indexes": [2]}]},
        {**common, "id": "inline", "kind": "slope", "slope_basis": "inline", "max_inline_slope_deg": 5},
        {**common, "id": "body-plane", "kind": "proximity", "around": "bodies", "targets": ["slopes"],
         "distance_m": 50, "water_depth_m": {"min_m": 0, "max_m": 200}, "slope_threshold_deg": 5,
         "slope_probe_spacing_m": 25, "slope_vertical_datum": "synthetic-plane-datum"},
        {**cross, "id": "missing-reference", "selectors": [{"layer_id": "gis", "feature_ids": ["absent"]}]},
    ]
    preview = post("/api/automatic-rules/check", {"workspace": initial, "config": {"rules": rules}})
    checks = preview["checks"]
    _require(checks["model"] == "automatic-geographic-rules-v1", "Wrong actual checker model")
    by_id = {row["id"]: row for row in checks["results"]}
    _require(len(by_id) == 6 and all(by_id[name]["status"] == "violations" for name in (
        "numeric-cross", "text-cross", "index-near", "inline", "body-plane"))
        and by_id["missing-reference"]["status"] == "reference_error",
        "All three rule families must run, while missing typed references remain errors")
    for name, longitude in (("numeric-cross", .0045), ("text-cross", .0075)):
        actual = by_id[name]["violations"]
        _require(actual, "Missing actual crossing evidence")
        for row in actual:
            _near(row["location"]["longitude"], longitude, 1e-7, name + " longitude")
            _near(row["location"]["kp_m"], math.radians(longitude)*6378137, .05, name + " KP")
            _near(row["values"]["angle_deg"], 90, 1e-7, name + " independent equatorial/meridional angle")
    near = by_id["index-near"]["violations"]
    _require(near, "Missing actual unnamed Point proximity")
    _near(near[0]["distance_m"], 22.11485516431952, .001,
          "Independent WGS84 meridional Point distance")
    inline = by_id["inline"]["violations"]
    _require(inline, "Missing actual inline sampled slope")
    _near(inline[0]["value_deg"], math.degrees(math.atan(100/(math.radians(.01)*6378137))),
          1e-8, "Independent longitudinal secant angle")
    slope_rows = by_id["body-plane"]["violations"]
    _require(slope_rows and checks["budget"]["terrain_query_count"] > 0,
             "Body slope must query real two-dimensional terrain")
    expected_slope = math.degrees(math.atan(math.sqrt(.05)))
    for row in slope_rows:
        witness = row["sampled_witness"]
        vertices = row["queried_vertices"]
        _require(len(vertices) == 3 and all(v["source_id"] == "plane"
                 and v["source_fingerprint"] == witness["source_fingerprint"] for v in vertices),
                 "All three actual queried triangle vertices must bind the same terrain source")
        a, b, c = vertices
        dx1, dy1 = b["x_m"]-a["x_m"], b["y_m"]-a["y_m"]
        dx2, dy2 = c["x_m"]-a["x_m"], c["y_m"]-a["y_m"]
        det = dx1*dy2-dy1*dx2
        _require(abs(det) > 1e-12, "Actual sampled triangle must have positive finite area")
        dh1, dh2 = a["depth_m"]-b["depth_m"], a["depth_m"]-c["depth_m"]
        gradient = [(dh1*dy2-dh2*dy1)/det, (dx1*dh2-dx2*dh1)/det]
        for actual, expected in zip(gradient, (-.1, -.2)):
            _near(actual, expected, 1e-7, "Independent three-queried-vertex height gradient")
        for actual, expected in zip(row["gradient_height"], gradient):
            _near(actual, expected, 1e-9, "Published triangle gradient reconstruction")
        _near(row["slope_deg"], expected_slope, 1e-6, "Actual 2D plane triangle")
        _near(witness["depth_m"], 1000+.1*witness["x_m"]+.2*witness["y_m"], 1e-6,
              "Actual queried witness depth")
        _require(witness["source_id"] == "plane" and witness["source_fingerprint"]
                 and row["location"]["depth_m"] is None,
                 "Query evidence must remain distinct from unqueried geometric centroid")
        _near(row["location"]["radius_center"]["longitude"], center_lon, 1e-9, "Actual body radius center longitude")
        _near(row["location"]["radius_center"]["latitude"], 0, 1e-9, "Actual body radius center latitude")
    candidate = deepcopy(preview["workspace"])
    candidate.pop("automatic_rules")
    _require(candidate == initial and initial == original, "Preview must only add declarations to a copy")
    saved_path = "/api/workspaces/" + quote(initial["id"], safe="")
    _require(get(saved_path) == initial, "Actual rule check must not write the database")
    exported = post("/api/automatic-rules/export", {"workspace": preview["workspace"]})
    imported = post("/api/automatic-rules/import", {"workspace": initial, "package": exported["package"]})
    _require(imported["rules"] == preview["rules"]
             and imported["checks"]["results"] == preview["checks"]["results"],
             "Open package roundtrip must preserve exact typed references and actual rechecks")
    applied = post("/api/workspace/action", {"workspace": initial, "config": {
        "action": "update_automatic_rules", "rules": imported["rules"]}})
    plain = deepcopy(applied["workspace"])
    plain.pop("automatic_rules")
    _require(plain == initial and applied["workspace"]["saved_revision"] == initial["saved_revision"],
             "Atomic action must preserve the full graph and revision until an explicit save")
    saved = post("/api/workspaces", applied["workspace"])["workspace"]
    _require(saved["saved_revision"] == initial["saved_revision"] + 1, "Save must advance exactly once")
    _require(get(saved_path) == saved, "Post-save real API reread must preserve the full candidate")
    rechecked = post("/api/automatic-rules/check", {"workspace": saved})
    _require(rechecked["checks"]["results"] == checks["results"], "Reopened saved declarations must reproduce evidence")
    rejected = post("/api/automatic-rules/check", {"workspace": saved,
        "config": {"max_work_units": 1}}, status=422)
    _require(isinstance(rejected.get("_error"), dict)
             and "AUTOMATIC_RULE_" in _json(rejected["_error"]).decode(),
             "Whole-budget rejection must retain actual structured error, not partial clear rows")
    _require(get(saved_path) == saved, "Failed whole check must not persist or mutate saved declarations")
    turn_project = deepcopy(project)
    turn_project.update(layers=[], terrain_sources=[], bodies=[])
    turn_project["route"].update(points=[
        {"id": "h0", "longitude": 0, "latitude": 70, "depth_m": 50},
        {"id": "hm", "longitude": 45.00000000000001, "latitude": 75.57008147661035, "depth_m": 50},
        {"id": "h1", "longitude": 90, "latitude": 70, "depth_m": 50}],
        legs=[{"cable_type_id": "SYNTHETIC-C"}]*2, allowances=[])
    geodesic = post("/api/analyze", turn_project)
    _near(geodesic["rpl"][1]["turn_deg"], 0, 1e-8,
          "High-latitude single geodesic split at its actual midpoint")
    _require(all(rule["end_kp_m"] is None for rule in rechecked["rules"]),
             "Nullable dynamic KP end declarations must remain null after save/reopen")
    result = {"status": "passed", "synthetic": True, "actual_api_requests": len(calls), "calls": calls,
        "rule_kinds_exercised": sorted({rule["kind"] for rule in rechecked["rules"]}),
        "rule_count": len(rechecked["rules"]), "checker_model": checks["model"],
        "typed_native_ids_and_source_indexes_distinct": True,
        "missing_reference_status": by_id["missing-reference"]["status"],
        "nullable_rule_ends_preserved": True, "preview_not_persisted": True,
        "open_package_exact_rules_and_recheck": True, "atomic_complete_candidate_applied": True,
        "path_assembly_inventory_associations_unchanged": True,
        "saved_revision": saved["saved_revision"], "path_count": len(saved["paths"]),
        "assembly_count": len(saved["assemblies"]), "api_saved_workspace_reread_equal": True,
        "saved_workspace_id": saved["id"], "saved_workspace_sha256": hashlib.sha256(_json(saved)).hexdigest(),
        "storage_owner_close_verified_by_harness": False,
        "body_slope_query_count": checks["budget"]["terrain_query_count"],
        "body_slope_triangle_violations": len(slope_rows), "body_slope_deg": slope_rows[0]["slope_deg"],
        "actual_queried_witness_and_unqueried_centroid_distinct": True,
        "queried_triangle_gradient_independently_reconstructed": True,
        "radius_center_is_actual_body_location": True,
        "continuous_bed_verified": checks["summary"]["continuous_bed_verified"],
        "whole_budget_http_status": 422, "whole_budget_rejection_does_not_save": True,
        "geodesic_common_vertex_turn_deg": geodesic["rpl"][1]["turn_deg"],
        "elapsed_s": time.monotonic()-started,
        "scope": "Explicit synthetic actual APIs and saved declarations; 2D slopes remain sampled, not continuous seabed/original-product/field accuracy certification; ordinary source/workspace admission precedes the independent checker work budget"}
    _json(result)
    return result
