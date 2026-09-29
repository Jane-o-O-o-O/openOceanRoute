"""Actual installed-API arc editing; this harness imports only the stdlib.

Adapters return actual JSON, actual 422 as {_http_status:422,_error:...},
and actual CSV attachment as {_http_status:200,_content_type:...,_text:...}.
The second GET is the explicitly saved reread. Its caller must independently
close/reopen the ASGI owner or stop/restart HTTP before claiming persistence.
This harness itself never claims installation, restart or field equivalence.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import io
import json
import math
import time
from urllib.parse import quote
from uuid import uuid4


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def _near(actual, expected, tolerance, name):
    _require(type(actual) in (int, float) and math.isfinite(actual)
             and abs(actual - expected) <= tolerance,
             f"{name}: {actual!r} != {expected!r}")


def _ecef_chord(first, second):
    # Independent ellipsoid embedding; only for this explicit <=450m radius.
    # Surface distance differs by <1 micrometre, below the 0.1mm check here.
    a, f = 6378137., 1 / 298.257223563
    e2 = f * (2 - f)

    def position(point):
        longitude, latitude = map(math.radians, point)
        s, c = math.sin(latitude), math.cos(latitude)
        n = a / math.sqrt(1 - e2 * s * s)
        return n * c * math.cos(longitude), n * c * math.sin(longitude), n * (1-e2) * s

    return math.sqrt(math.fsum((a-b)**2 for a, b in zip(position(first), position(second))))


def _arc(project, analysis=None, *, radius_m=300.):
    arcs = [(i, leg["geometry"]) for i, leg in enumerate(project["route"]["legs"])
            if leg.get("geometry") is not None]
    _require(len(arcs) == 1, "Expected one real arc, not drawing-only legs")
    index, geometry = arcs[0]
    _require(set(geometry) == {"type", "schema_version", "center", "radius_m",
                               "start_azimuth_deg", "sweep_deg"}, "Strict six-field descriptor")
    _require(geometry["type"] == "circular_arc" and type(geometry["schema_version"]) is int
             and geometry["schema_version"] == 1, "Actual circular-arc version")
    _near(geometry["radius_m"], radius_m, 1e-8, "Declared true radius")
    endpoints = [(p["longitude"], p["latitude"])
                 for p in project["route"]["points"][index:index+2]]
    residuals = [abs(_ecef_chord(geometry["center"], point) - radius_m) for point in endpoints]
    _require(max(residuals) <= 1e-4, "Independent small-radius ECEF endpoint admission")
    row = {"leg_index": index, "geometry": deepcopy(geometry),
           "ecef_endpoint_radius_residuals_m": residuals, "ecef_endpoint_chord_m": _ecef_chord(*endpoints)}
    if analysis is not None:
        length = analysis["legs"][index]["surface_length_m"]
        _require(length > row["ecef_endpoint_chord_m"] * 1.05,
                 "Actual changed arc must not be analysed as a straight chord")
        row["actual_surface_length_m"] = length
    return row


def run_arc_edit_smoke(post_json, get_json):
    started, calls = time.monotonic(), []

    def post(path, payload, *, status=200, attachment=False):
        _bytes(payload)
        actual = post_json(path, deepcopy(payload))
        _require(isinstance(actual, dict), f"{path}: actual response object required")
        _bytes(actual)
        _require(actual.get("_http_status", 200) == status,
                 f"{path}: expected actual HTTP{status}: {actual}")
        calls.append({"method": "POST", "path": path, "http_status": status,
                      "response_kind": "csv_attachment" if attachment else "json"})
        if attachment:
            _require(isinstance(actual.get("_text"), str)
                     and str(actual.get("_content_type", "")).startswith("text/csv"),
                     "Actual CSV body and MIME required")
        return actual

    def get(path):
        actual = get_json(path)
        _require(isinstance(actual, dict) and actual.get("_http_status", 200) == 200,
                 "Actual successful saved-workspace GET required")
        _bytes(actual)
        calls.append({"method": "GET", "path": path, "http_status": 200, "response_kind": "json"})
        return actual

    source = {
        "id": "synthetic-installed-arc-edit-"+str(uuid4()), "schema_version": 1,
        "name": "独立合成圆弧端点编辑／安装验证（非测量）", "crs": "EPSG:4326",
        "route": {"curve": "rhumb", "mode": "fixed", "slack_basis": "surface", "slack_pct": 0,
            "points": [
                {"id": "west", "longitude": -math.degrees(2500./6378137.), "latitude": 0., "depth_m": 50.},
                {"id": "turn", "longitude": 0., "latitude": 0., "depth_m": 50.},
                {"id": "north", "longitude": 0., "latitude": .022609236913982, "depth_m": 50.}],
            "legs": [{"cable_type_id": "SYNTHETIC-C", "fixed_cable_length_m": 2600., "stop_hours": 2.},
                     {"cable_type_id": "SYNTHETIC-C", "fixed_cable_length_m": 2700., "stop_hours": 3.}]},
        "cable_types": [{"id": "SYNTHETIC-C", "name": "Synthetic declared fixed stock",
                         "cost_per_m": 2., "lay_speed_m_s": 1.}],
        "bodies": [{"id": "synthetic-point", "cable_kp_m": 1300., "length_m": 0., "cost": 5.}],
        "layers": [], "terrain_sources": [], "costs": {"currency": "CNY"},
        "synthetic_extension": {"field_data": False, "retain": "actual declared inventory and original identity"}}
    untouched = deepcopy(source)
    radius = post("/api/tools/radius-altercourse", {"project": source,
                  "config": {"point_id": "turn", "radius_m": 300.}})["project"]
    baseline = post("/api/analyze", radius)
    original_arc = _arc(radius, baseline)
    points, legs = radius["route"]["points"], radius["route"]["legs"]
    ai = original_arc["leg_index"]
    option = {"start_point_id": points[ai]["id"], "end_point_id": points[ai+1]["id"]}
    moved = points[ai+1]
    a, f = 6378137., 1/298.257223563
    meridian_at_equator = a * (1 - f*(2-f))
    target = {"point_id": moved["id"], "longitude": moved["longitude"],
              "latitude": moved["latitude"] + math.degrees(10./meridian_at_equator)}
    config = {"moves": [target]}
    direct = post("/api/tools/arc-edit", {"project": radius, "config": config})
    _require(direct["report"]["changed"] and direct["report"]["operation"] == "arc_edit",
             "Direct edit must return actual changed arc report")
    _near(direct["report"]["manufacturing"]["physical_delta_m"], 0., 1e-7,
          "Fixed source inventory conservation")
    migrated = post("/api/workspace/migrate", {"project": radius})["workspace"]
    shared = post("/api/workspace/action", {"workspace": migrated, "config": {
        "action": "copy_path", "assembly_policy": "alternative", "name": "同库存真实备选"}})["workspace"]
    initial = post("/api/workspaces", shared)["workspace"]
    initial_copy = deepcopy(initial)
    path_id = initial["active_path_id"]
    _require(len(initial["paths"]) == 2 and len(initial["assemblies"]) == 1,
             "Two real alternatives must share one fixed inventory")
    preview = post("/api/workspace/arc-edit-preview", {
        "workspace": initial, "path_id": path_id, "config": config})
    candidate = preview["workspace"]
    _require(candidate["assemblies"] == initial["assemblies"]
             and candidate["associations"] == initial["associations"]
             and candidate["saved_revision"] == initial["saved_revision"],
             "Preview preserves real shared fixed graph and saved revision")
    for path in initial["paths"]:
        if path["id"] != path_id:
            _require(next(p for p in candidate["paths"] if p["id"] == path["id"]) == path,
                     "Unselected complete path unchanged")
    project = next(p["project"] for p in candidate["paths"] if p["id"] == path_id)
    analysis = preview["analysis"]["active_path_analysis"]
    changed_arc = _arc(project, analysis)
    _require(changed_arc["geometry"] != original_arc["geometry"], "Changed real arc descriptor")
    _require(changed_arc["geometry"]["sweep_deg"] * original_arc["geometry"]["sweep_deg"] > 0,
             "Retain original directed sweep sign")
    _require(project["route"] == direct["project"]["route"], "Direct and workspace real route agree")
    _require(project["bodies"] == radius["bodies"], "Declared physical point stock unchanged")
    actual_point = next(p for p in project["route"]["points"] if p["id"] == target["point_id"])
    _near(actual_point["longitude"], target["longitude"], 1e-12, "Actual edited longitude")
    _near(actual_point["latitude"], target["latitude"], 1e-12, "Actual edited latitude")
    _require(actual_point["depth_m"] is None, "New position does not certify old depth")
    _require(preview["tool_report"]["joins"], "Real join diagnostics retained")
    _near(analysis["summary"]["cable_length_m"], 5300., 1e-7, "Actual fixed physical stock")
    saved_path = "/api/workspaces/" + quote(initial["id"], safe="")
    _require(get(saved_path) == initial, "Preview does not save")
    applied = post("/api/workspace/action", {"workspace": initial, "config": {
        "action": "update_path", "path_id": path_id, "project": project, "assembly_policy": "auto_exclusive"}})
    _require(applied["workspace"] == candidate, "Actual atomic application equals full candidate")
    saved = post("/api/workspaces", applied["workspace"])["workspace"]
    _require(saved["saved_revision"] == initial["saved_revision"]+1, "One explicit new revision")
    _require(get(saved_path) == saved, "Second actual GET reads the explicitly saved whole workspace")

    major = post("/api/tools/arc-edit", {"project": radius,
                 "config": {"arc_options": [{**option, "branch": "major"}]}})
    major_arc = _arc(major["project"])
    _require(abs(major_arc["geometry"]["sweep_deg"]) > 180.
             and major_arc["geometry"]["sweep_deg"] * original_arc["geometry"]["sweep_deg"] > 0,
             "Unmoved actual endpoints must not bypass explicit major branch")
    resized = post("/api/tools/arc-edit", {"project": radius,
                   "config": {"arc_options": [{**option, "radius_m": 450.}]}})
    resized_arc = _arc(resized["project"], radius_m=450.)
    _require(resized["report"]["changed"], "Explicit radius-only edit changes geometry")
    far_target = {**target, "longitude": target["longitude"]+.03}
    rejected = post("/api/workspace/arc-edit-preview", {"workspace": initial, "path_id": path_id,
                    "config": {"moves": [far_target]}}, status=422)
    _require("ARC_EDIT_" in _bytes(rejected["_error"]).decode(), "Actual unreachable typed rejection")
    post("/api/tools/arc-edit", {"project": radius,
         "config": {"arc_options": [{**option, "branch": []}]}}, status=422)

    materialized = deepcopy(project)
    materialized.update(id=path_id, cable_types=deepcopy(saved["cable_types"]),
                        layers=deepcopy(saved["layers"]), terrain_sources=deepcopy(saved["terrain_sources"]))
    csv_result = post("/api/export/csv", materialized, attachment=True)
    text = csv_result["_text"]
    rows = list(csv.DictReader(io.StringIO("\n".join(
        line for line in text.lstrip("\ufeff").splitlines() if not line.startswith("#")))))
    csv_arcs = [json.loads(row["leg_geometry_json"]) for row in rows if row.get("leg_geometry_json")]
    _require(csv_arcs == [changed_arc["geometry"]], "Actual changed descriptor survives CSV export")
    imported = post("/api/import/rpl", {"text": text, "error_policy": "reject"})
    _require(imported["can_apply"] and not imported["errors"], "Actual changed arc CSV is admissible")
    _require([leg["geometry"] for leg in imported["legs"] if leg.get("geometry")] == csv_arcs,
             "Actual changed arc CSV imports intrinsic geometry")
    restored = deepcopy(materialized)
    restored["route"].update(imported["route_options"], points=imported["points"], legs=imported["legs"])
    restored["route"].pop("constraint_state", None)
    restored["route"].pop("path_links", None)
    restored_analysis = post("/api/analyze", restored)
    _near(restored_analysis["summary"]["surface_length_m"], analysis["summary"]["surface_length_m"],
          1e-6, "Actual CSV true route length")
    _near(restored_analysis["summary"]["cable_length_m"], 5300., 1e-6, "Actual CSV declared fixed stock")
    _require(get(saved_path) == saved and initial == initial_copy and source == untouched,
             "Rejected/direct/exchange operations do not write or mutate source")
    _require(len(legs) == 3, "Baseline is the actual 3-leg Radius construction")
    return {"status": "passed", "synthetic": True, "actual_api_requests": len(calls), "calls": calls,
            "original_source_sha256": hashlib.sha256(_bytes(untouched)).hexdigest(),
            "source_inputs_unchanged": True, "original_arc": original_arc, "edited_arc": changed_arc,
            "major_arc_unmoved_endpoints": major_arc, "radius_only_arc": resized_arc,
            "fixed_physical_stock_m": analysis["summary"]["cable_length_m"],
            "shared_fixed_inventory_and_associations_unchanged": True,
            "preview_not_saved": True, "atomic_candidate_equal": True,
            "path_count": len(saved["paths"]), "assembly_count": len(saved["assemblies"]),
            "saved_workspace_id": saved["id"], "saved_revision": saved["saved_revision"],
            "saved_workspace_sha256": hashlib.sha256(_bytes(saved)).hexdigest(),
            "second_get_is_post_save_reread": True, "api_saved_workspace_reread_equal": True,
            "storage_owner_close_verified_by_harness": False,
            "csv_intrinsic_descriptor_and_length_retained": True,
            "unreachable_and_enum_container_actual_http422": True,
            "actual_join_diagnostics": deepcopy(preview["tool_report"]["joins"]),
            "wall_time_s": time.monotonic()-started,
            "scope": "Explicit synthetic actual APIs, independent <=450m ECEF radius check and true arc/chord distinction; not installation, restart, native format or field certification"}
