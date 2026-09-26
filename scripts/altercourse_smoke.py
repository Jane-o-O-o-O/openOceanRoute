"""Small actual-API altercourse workflow, with no production-module imports.

post_json(path, payload) and get_json(path) return actual successful JSON.
For an actual expected 422, the adapter returns
{"_http_status":422,"_error":actual_response_json}. CSV export is the one
attachment: return {"_http_status":200,"_content_type":actual_content_type,
"_text":actual_utf8_response}. No values may be invented by an adapter.

The second GET is the post-save read. Its caller may close/recreate the real
ASGI owner or restart the installed HTTP server before that read. This helper
does not claim a restart, fresh installation, or production import isolation.
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


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _near(actual, expected, tolerance, name):
    _require(isinstance(actual, (float, int)) and not isinstance(actual, bool)
             and math.isfinite(actual) and abs(actual - expected) <= tolerance,
             f"{name}: {actual!r} differs from {expected!r}")


def _ecef_chord(first, second):
    """Independent WGS84 ellipsoid embedding; not a surface-distance solver.

    Over this explicit 300 m radius fixture, the surface/chord difference is
    below 0.1 micrometre, smaller than the 0.1 mm admission check. This does
    not turn a drawn chord or a planar circle into an intrinsic route arc.
    """
    a, f = 6378137., 1 / 298.257223563
    e2 = f * (2 - f)

    def xyz(point):
        lon, lat = map(math.radians, point)
        sine, cosine = math.sin(lat), math.cos(lat)
        n = a / math.sqrt(1 - e2 * sine * sine)
        return n * cosine * math.cos(lon), n * cosine * math.sin(lon), n * (1 - e2) * sine

    return math.sqrt(math.fsum((p - q) ** 2 for p, q in zip(xyz(first), xyz(second))))


def _arc_evidence(project, analysis):
    arcs = [(i, leg["geometry"]) for i, leg in enumerate(project["route"]["legs"])
            if leg.get("geometry") is not None]
    _require(len(arcs) == 1, "Radius must retain one actual per-leg circular arc")
    index, geometry = arcs[0]
    _require(set(geometry) == {"type", "schema_version", "center", "radius_m",
                               "start_azimuth_deg", "sweep_deg"}
             and geometry["type"] == "circular_arc"
             and type(geometry["schema_version"]) is int and geometry["schema_version"] == 1,
             "Actual arc requires its complete six-field intrinsic descriptor")
    _near(geometry["radius_m"], 300., 1e-8, "Declared physical radius")
    _near(geometry["sweep_deg"], -90., 1e-5, "Independent left quarter turn")
    points = project["route"]["points"]
    ends = [(p["longitude"], p["latitude"]) for p in points[index:index + 2]]
    for endpoint in ends:
        _near(_ecef_chord(geometry["center"], endpoint), 300., 1e-4,
              "Independent endpoint radius via small-circle ECEF chord bound")
    leg = analysis["legs"][index]
    length = leg["surface_length_m"]
    # R*angle is an analytic upper bound here, not our definition of length.
    _require(470. < length < 300. * math.pi / 2 + 1e-5,
             "Actual quarter-circle KP length must differ from its straight chord")
    _require(length > 1.1 * _ecef_chord(*ends), "An arc must not be analysed as its chord")
    render = analysis["route_geometry"]["coordinates"]
    circular_points = [p for p in render
                       if abs(_ecef_chord(geometry["center"], p[:2]) - 300.) <= 1e-4]
    _require(len(circular_points) >= 5, "Actual adaptive rendering must retain the curved shape")
    for row in analysis["rpl"][1:-1]:
        _near(row["turn_deg"], 0., 1e-6, "Radius tangency at actual route station")
    return {"leg_index": index, "geometry": deepcopy(geometry), "surface_length_m": length,
            "chord_length_m": _ecef_chord(*ends), "actual_rendered_circle_points": len(circular_points),
            "radius_check_basis": "independent WGS84 ECEF chord, explicit 300m domain; surface/chord difference below 0.1um"}


def run_altercourse_smoke(post_json, get_json):
    """Run 18 actual finite synthetic requests; all expectations are explicit."""
    started, calls = time.monotonic(), []

    def post(path, payload, *, status=200, attachment=False):
        _json(payload)
        result = post_json(path, deepcopy(payload))
        _require(isinstance(result, dict), f"{path}: expected actual response object")
        _json(result)
        _require(result.get("_http_status", 200) == status,
                 f"{path}: expected HTTP {status}, received {result}")
        calls.append({"method": "POST", "path": path, "http_status": status,
                      "response_kind": "csv_attachment" if attachment else "json"})
        if attachment:
            _require(isinstance(result.get("_text"), str)
                     and str(result.get("_content_type", "")).startswith("text/csv"),
                     "CSV must be the actual HTTP text/csv attachment")
        return result

    def get(path):
        result = get_json(path)
        _require(isinstance(result, dict) and result.get("_http_status", 200) == 200,
                 f"{path}: expected successful actual JSON")
        _json(result)
        calls.append({"method": "GET", "path": path, "http_status": 200, "response_kind": "json"})
        return result

    project = {
        "id": "synthetic-altercourse-installed-" + str(uuid4()), "schema_version": 1,
        "name": "Explicit synthetic WGS84 altercourse smoke; not survey data", "crs": "EPSG:4326",
        "route": {"curve": "rhumb", "mode": "fixed", "slack_basis": "surface", "slack_pct": 0,
            "points": [
                {"id": "west", "longitude": -math.degrees(2500. / 6378137.), "latitude": 0., "depth_m": 50.},
                {"id": "turn", "longitude": 0., "latitude": 0., "depth_m": 50.},
                # Independent WGS84 meridian inversion for 2500 m from the equator.
                {"id": "north", "longitude": 0., "latitude": .022609236913982, "depth_m": 50.}],
            "legs": [{"cable_type_id": "SYNTHETIC-C", "fixed_cable_length_m": 2600., "stop_hours": 2},
                     {"cable_type_id": "SYNTHETIC-C", "fixed_cable_length_m": 2700., "stop_hours": 3}]},
        "cable_types": [{"id": "SYNTHETIC-C", "name": "Declared synthetic fixed stock",
                         "cost_per_m": 2., "lay_speed_m_s": 1.}],
        "bodies": [{"id": "synthetic-point", "cable_kp_m": 1300., "length_m": 0., "cost": 5.}],
        "layers": [], "terrain_sources": [], "costs": {"currency": "CNY"},
        "synthetic_extension": {"field_data": False, "retain": "original source and physical inventory"},
    }
    original, original_hash = deepcopy(project), hashlib.sha256(_json(project)).hexdigest()
    before = post("/api/analyze", project)
    _near(before["summary"]["cable_length_m"], 5300., 1e-7, "Declared fixed physical stock")
    radius_config = {"point_id": "turn", "radius_m": 300.}
    radius = post("/api/tools/radius-altercourse", {"project": project, "config": radius_config})
    split = post("/api/tools/split-altercourse", {"project": project, "config": {
        "point_id": "turn", "max_turn_angle_deg": 31., "min_turn_distance_m": 120.}})
    _require(len(radius["project"]["route"]["points"]) == 4
             and len(split["project"]["route"]["points"]) == 5,
             "Actual Radius and equal-turn Split workflows must both generate their declared structures")
    _require(not any(leg.get("geometry") for leg in split["project"]["route"]["legs"]),
             "Split's straight legs must not be relabelled as a circular arc")
    for result in (radius, split):
        _near(result["report"]["manufacturing"]["physical_delta_m"], 0., 1e-7,
              "Fixed tool manufacturing conservation")
        _require(result["project"]["bodies"] == project["bodies"], "Manufacturing point station must survive")
    _require(project == original, "Direct tool must leave its input unchanged")
    migrated = post("/api/workspace/migrate", {"project": project})
    copied = post("/api/workspace/action", {"workspace": migrated["workspace"], "config": {
        "action": "copy_path", "assembly_policy": "alternative", "name": "Shared fixed synthetic alternative"}})
    initial = post("/api/workspaces", copied["workspace"])["workspace"]
    source_workspace = deepcopy(initial)
    path_id = initial["active_path_id"]
    _require(len(initial["paths"]) == 2 and len(initial["assemblies"]) == 1,
             "Expected two real alternatives sharing one actual inventory")
    preview = post("/api/workspace/altercourse-preview", {
        "workspace": initial, "path_id": path_id, "kind": "radius", "config": radius_config})
    candidate = preview["workspace"]
    _require(candidate["assemblies"] == initial["assemblies"]
             and candidate["associations"] == initial["associations"]
             and candidate["saved_revision"] == initial["saved_revision"],
             "Preview must preserve shared fixed inventory, graph and saved revision")
    for path in initial["paths"]:
        if path["id"] != path_id:
            _require(next(p for p in candidate["paths"] if p["id"] == path["id"]) == path,
                     "Unselected complete path must remain unchanged")
    path_project = next(p["project"] for p in candidate["paths"] if p["id"] == path_id)
    active_analysis = preview["analysis"]["active_path_analysis"]
    arc = _arc_evidence(path_project, active_analysis)
    _near(active_analysis["summary"]["cable_length_m"], 5300., 1e-7, "Candidate physical inventory")
    saved_path = "/api/workspaces/" + quote(initial["id"], safe="")
    _require(get(saved_path) == initial, "Read-only preview must not save or alter revisions")
    applied = post("/api/workspace/action", {"workspace": initial, "config": {
        "action": "update_path", "path_id": path_id, "project": path_project,
        "assembly_policy": "auto_exclusive"}})
    _require(applied["workspace"] == candidate, "Actual atomic path action must equal the complete preview candidate")
    saved = post("/api/workspaces", applied["workspace"])["workspace"]
    _require(saved["saved_revision"] == initial["saved_revision"] + 1,
             "Explicit save must create exactly one subsequent revision")
    _require(get(saved_path) == saved, "Second real GET must equal the explicitly saved whole workspace")
    _require(initial == source_workspace and project == original, "All source inputs must remain immutable")

    # CSV exchange changes only route declarations. Retain the explicit master
    # cable/body context when reanalysing the imported open-format route.
    reopened_project = deepcopy(next(p["project"] for p in saved["paths"] if p["id"] == path_id))
    reopened_project.update(id=path_id, cable_types=deepcopy(saved["cable_types"]),
                            layers=deepcopy(saved["layers"]), terrain_sources=deepcopy(saved["terrain_sources"]))
    csv_response = post("/api/export/csv", reopened_project, attachment=True)
    text = csv_response["_text"]
    rows = list(csv.DictReader(io.StringIO("\n".join(
        line for line in text.lstrip("\ufeff").splitlines() if not line.startswith("#")))))
    _require(rows and "leg_geometry_json" in rows[0] and "route_curve" in rows[0],
             "Actual open CSV must contain the authoritative arc and curve extension")
    csv_arcs = [json.loads(row["leg_geometry_json"]) for row in rows if row["leg_geometry_json"]]
    _require(csv_arcs == [arc["geometry"]] and rows[-1]["leg_geometry_json"] == "",
             "Actual CSV must attach the exact descriptor to its starting leg, never the terminal point")
    imported = post("/api/import/rpl", {"text": text, "error_policy": "reject"})
    _require(imported["can_apply"] and not imported["errors"], "Actual CSV import must be valid")
    _require([leg["geometry"] for leg in imported["legs"] if leg.get("geometry")] == [arc["geometry"]],
             "Actual CSV import must preserve the intrinsic arc, not import only a drawn chord")
    restored_project = deepcopy(reopened_project)
    restored_project["route"].update(imported["route_options"],
                                       points=imported["points"], legs=imported["legs"])
    # Constraint snapshots refer to the original point IDs. Open RPL establishes
    # new route rows; it does not pretend to export native constraint history.
    restored_project["route"].pop("constraint_state", None)
    restored_project["route"].pop("path_links", None)
    restored = post("/api/analyze", restored_project)
    _near(restored["summary"]["surface_length_m"], active_analysis["summary"]["surface_length_m"],
          1e-6, "CSV retained physical surface length")
    _near(restored["summary"]["cable_length_m"], 5300., 1e-6, "CSV retained declared manufacturing stock")

    shadow = deepcopy(project)
    shadow["id"] = "synthetic-shared-flexible-" + str(uuid4())
    shadow["route"].update(mode="flexible", slack_pct=1.)
    for leg in shadow["route"]["legs"]:
        leg.pop("fixed_cable_length_m")
    shadow_initial = post("/api/workspace/migrate", {"project": shadow})["workspace"]
    shadow_shared = post("/api/workspace/action", {"workspace": shadow_initial, "config": {
        "action": "copy_path", "assembly_policy": "alternative"}})["workspace"]
    shadow_copy = deepcopy(shadow_shared)
    rejected = post("/api/workspace/altercourse-preview", {
        "workspace": shadow_shared, "path_id": shadow_shared["active_path_id"],
        "kind": "radius", "config": radius_config}, status=422)
    _require(isinstance(rejected.get("_error"), dict)
             and "WORKSPACE_SHARED_ASSEMBLY_CHANGED" in _json(rejected["_error"]).decode("utf-8"),
             "A quantity-changing shared Flexible edit must be an actual typed 422")
    _require(shadow_shared == shadow_copy and get(saved_path) == saved,
             "Rejected shadow edit must not write, fork, or change saved fixed inventory")
    report = {
        "status": "passed", "synthetic": True, "actual_api_requests": len(calls), "calls": calls,
        "source_project_sha256": original_hash, "source_project_unchanged": project == original,
        "tool_operations": [radius["report"]["operation"], split["report"]["operation"]],
        "intrinsic_arc": arc, "fixed_physical_stock_m": active_analysis["summary"]["cable_length_m"],
        "shared_fixed_inventory_and_associations_unchanged": saved["assemblies"] == initial["assemblies"]
            and saved["associations"] == initial["associations"],
        "path_count": len(saved["paths"]), "assembly_count": len(saved["assemblies"]),
        "preview_not_saved": True, "atomic_candidate_equal": True,
        "saved_workspace_id": saved["id"], "saved_revision": saved["saved_revision"],
        "saved_workspace_sha256": hashlib.sha256(_json(saved)).hexdigest(),
        "second_get_is_post_save_reread": True, "api_saved_workspace_reread_equal": True,
        "storage_owner_close_verified_by_harness": False,
        "csv": {"content_type": csv_response["_content_type"], "utf8_bytes": len(text.encode("utf-8")),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "point_count": len(imported["points"]),
                "arc_descriptor_equal": True, "surface_length_m": restored["summary"]["surface_length_m"],
                "physical_stock_m": restored["summary"]["cable_length_m"],
                "scope": "own open CSV route extension with explicit master context; not native constraint-history interchange"},
        "shared_flexible_rejection": {"http_status": rejected["_http_status"], "error": rejected["_error"]},
        "failed_shadow_does_not_save": True, "elapsed_s": time.monotonic() - started,
        "scope": "actual synthetic APIs and saved whole workspace; caller alone verifies owner restart, module import paths and installation; no original-product or field accuracy claim",
    }
    _json(report)
    return report
