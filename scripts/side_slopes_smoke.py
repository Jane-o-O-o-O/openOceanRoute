"""Small installed-API side-slope workflow, with no production-module imports.

Callers provide real JSON HTTP/ASGI adapters. Paths include ``/api``. The
second GET rereads the saved workspace; a caller may close/reopen its owner
before that call. This helper never claims to have closed a storage owner.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time
from typing import Callable
from urllib.parse import quote
from uuid import uuid4


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _finite_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _near(actual, expected, tolerance: float, field: str) -> None:
    _require(isinstance(actual, (int, float)) and not isinstance(actual, bool)
             and math.isfinite(actual) and abs(actual - expected) <= tolerance,
             f"{field}: actual {actual!r}, expected {expected!r}")


def run_side_slopes_smoke(
    post_json: Callable[[str, dict], dict],
    get_json: Callable[[str], dict],
) -> dict:
    """Execute 12 real API calls and return only verified, finite evidence.

    The explicit synthetic route has constant *waypoint* depth and additional
    declared cable inventory. The generated XYZ source is a separate synthetic
    plane. This checks workflow and sampling semantics, not field bathymetry.
    Adapter exceptions propagate; no fallback or fabricated result is used.
    """
    started = time.monotonic()
    calls: list[dict] = []

    def post(path: str, payload: dict) -> dict:
        _finite_json(payload)
        result = post_json(path, deepcopy(payload))
        _require(isinstance(result, dict), f"POST {path}: expected a JSON object")
        _finite_json(result)
        calls.append({"method": "POST", "path": path})
        return result

    def get(path: str) -> dict:
        result = get_json(path)
        _require(isinstance(result, dict), f"GET {path}: expected a JSON object")
        _finite_json(result)
        calls.append({"method": "GET", "path": path})
        return result

    project = {
        "schema_version": 1,
        "id": "synthetic-side-slopes-smoke-" + str(uuid4()),
        "name": "Explicit synthetic installed side-slope workflow — not field data",
        "crs": "EPSG:4326",
        "route": {
            "id": "synthetic-north-route", "curve": "geodesic", "mode": "flexible",
            "slack_pct": 1, "slack_basis": "surface",
            "points": [
                {"id": "synthetic-a", "longitude": 118, "latitude": 22,
                 "depth_m": 1000, "note": "Explicit synthetic waypoint depth"},
                {"id": "synthetic-b", "longitude": 118, "latitude": 22.005,
                 "depth_m": 1000, "note": "Explicit synthetic waypoint depth"},
            ],
            "allowances": [{"id": "synthetic-reserve", "kp_m": 200,
                            "length_m": 40, "cable_type_id": "SYNTHETIC-C"}],
        },
        "profile": {"source": "explicit_synthetic_waypoint_depths", "samples": []},
        "cable_types": [{"id": "SYNTHETIC-C", "name": "Explicit synthetic cable",
                         "cost_per_m": 15, "lay_speed_m_s": 1,
                         "wet_weight_n_m": 10, "ea_n": 1_000_000}],
        "bodies": [], "layers": [], "terrain_sources": [],
        "costs": {"currency": "CNY"},
        "synthetic_smoke_extension": {"field_data": False, "preserve": ["inventory", "profile"]},
    }
    migrated = post("/api/workspace/migrate", {"project": project})
    saved_before = post("/api/workspaces", migrated["workspace"])["workspace"]
    initial = deepcopy(saved_before)
    materialized = deepcopy(migrated["project"])
    inventory = deepcopy(initial["assemblies"])
    associations = deepcopy(initial["associations"])
    inventory_summary = migrated["analysis"]["summary"]
    _require(len(inventory) == 1 and len(associations) == 1,
             "Migration must retain one declared inventory and deployment association")

    example = post("/api/terrain/side-slopes/example", {"project": materialized})
    _require("project" not in example and "side_slopes" not in example,
             "Example must return source inputs, never computed side slopes")
    _require(example["source"] == "explicit_synthetic_source_inputs_not_field_data",
             "Example must explicitly label synthetic source inputs")
    _require(len(example["sources"]) == 1, "Expected one explicitly generated XYZ source")
    source = example["sources"][0]
    _require(source["kind"] == "xyz" and source["depth_positive"] == "down"
             and source["depth_units"] == "m"
             and source["vertical_datum"] == "synthetic-side-slopes-datum",
             "Synthetic source reference/units must be explicit")
    rows = source["text"].strip().splitlines()
    _require(len(rows) == 10, "Expected a header and nine genuine XYZ input points")
    _require(all(len(row.split(",")) == 3 for row in rows[1:]), "XYZ source must have three columns")
    _require(all(math.isfinite(float(value)) for row in rows[1:] for value in row.split(",")),
             "Generated XYZ input must contain only finite coordinates/depths")
    source_project = deepcopy(materialized)
    source_project["terrain_sources"] = deepcopy(example["sources"])
    config = {key: value for key, value in example["config"].items() if key != "end_kp_m"}
    config.update(max_query_points=100, max_work_units=100_000, max_output_bytes=1_048_576)
    omitted = post("/api/terrain/side-slopes", {"project": source_project, "config": config})
    explicit_null = post("/api/terrain/side-slopes", {
        "project": source_project, "config": {**config, "end_kp_m": None}})
    _require(omitted["side_slopes"] == explicit_null["side_slopes"]
             and omitted["project"] == explicit_null["project"],
             "Explicit null sampling end must be identical to an omitted end")
    side = omitted["side_slopes"]
    _require(side["model"] == "route-side-slopes-v1" and side["schema_version"] == 1,
             "Expected the actual declared side-slope model")
    samples = side["samples"]
    _require(2 <= len(samples) <= 20, "Installed smoke must remain within 20 actual stations")
    _require(all(row["complete"] and not row["source_boundary"] for row in samples),
             "The declared small synthetic plane should cover all real probe points")
    first = samples[0]
    depths = [row["depth_m"] for row in first["transect"]]
    expected_depths = [1520, 1510, 1500, 1490, 1480]
    _require(len(depths) == len(expected_depths), "Expected five real cross-track probes")
    for index, (actual, expected) in enumerate(zip(depths, expected_depths)):
        _near(actual, expected, 1e-7, f"First-station depth {index}")
    expected_angle = math.degrees(math.atan(.2))
    _near(first["heading_deg"], 0, 1e-9, "North-going actual geodesic heading")
    _near(first["side_slope_deg"], expected_angle, 1e-8, "Signed starboard-up side slope")
    _near(first["max_sampled_abs_slope_deg"], expected_angle, 1e-8, "Adjacent sampled maximum")
    _require([row["offset_m"] for row in first["transect"]] == [-100, -50, 0, 50, 100],
             "Signed cross-track positions must include both ends and the route")
    for key in ("route", "profile", "bodies", "cable_types", "synthetic_smoke_extension"):
        _require(omitted["project"][key] == materialized[key], f"Sampling must preserve {key}")

    rule = {"id": "synthetic-both-limit", "name": "Explicit synthetic sampled 5 degree limits",
            "enabled": True, "kind": "both", "start_kp_m": 0, "end_kp_m": None,
            "max_inline_slope_deg": 5, "max_side_slope_deg": 5}
    checked = post("/api/tools/slope-rules", {"project": omitted["project"], "config": {
        "rules": [rule], "max_rules": 1, "max_work_units": 100_000,
        "max_output_bytes": 1_048_576}})
    actual_rule = checked["results"][0]
    _require(actual_rule["status"] == "violations"
             and actual_rule["components"]["inline"]["status"] == "sampled_pass"
             and actual_rule["components"]["inline"]["waypoint_approximation"] is True
             and actual_rule["components"]["side"]["status"] == "violations",
             "Flat waypoint longitudinal approximation and true side excess must remain distinct")
    _require(checked["rules"][0]["end_kp_m"] is None
             and actual_rule["end_kp_m"] == side["metadata"]["end_kp_m"],
             "The null rule declaration must resolve to the actual current route end")
    _require(actual_rule["violations"] and all(row["kind"] == "side"
             and row["value_deg"] > row["limit_deg"] for row in actual_rule["violations"]),
             "Only the actual side samples should exceed the declared limits")
    _require(get("/api/workspaces/" + quote(initial["id"], safe="")) == initial,
             "Example, sampling and rule previews must not persist the candidate")

    applied = post("/api/workspace/action", {"workspace": initial, "config": {
        "action": "update_path", "path_id": initial["active_path_id"],
        "project": checked["project"], "assembly_policy": "auto_exclusive", "update_shared": True}})
    workspace = applied["workspace"]
    _require(workspace["saved_revision"] == initial["saved_revision"]
             and workspace["assemblies"] == inventory and workspace["associations"] == associations,
             "Atomic candidate application must preserve the saved revision and exact inventory graph")
    _require(applied["project"]["side_slopes"] == side
             and applied["project"]["slope_rules"] == checked["rules"],
             "One validated application must carry both side samples and rule declarations")
    _require(initial == saved_before, "The caller's original saved workspace must remain immutable")
    for leg in applied["analysis"]["active_path_analysis"]["legs"]:
        _near(leg["max_slope_deg"], 0, 1e-12, "Actual flat longitudinal waypoint slope")
    copied = post("/api/workspace/action", {"workspace": workspace, "config": {
        "action": "copy_path", "path_id": workspace["active_path_id"],
        "assembly_policy": "alternative", "name": "Explicit synthetic shared-inventory alternative"}})
    alternative = copied["workspace"]
    _require(len(alternative["paths"]) == 2 and alternative["assemblies"] == inventory,
             "The alternative must share the existing physical inventory")
    _require(alternative["associations"][:len(associations)] == associations
             and len(alternative["associations"]) == len(associations) + 1,
             "The original association must remain unchanged")
    new_link = alternative["associations"][-1]
    _require(new_link["role"] == "alternative" and new_link["assembly_id"] == inventory[0]["id"]
             and new_link["path_id"] == alternative["active_path_id"],
             "The new active alternative must reference the exact same assembly")
    for key in ("manufactured_total_m", "procurement_cost"):
        _near(copied["analysis"]["summary"][key], inventory_summary[key], 1e-8,
              f"Shared-inventory {key}")
    saved = post("/api/workspaces", alternative)["workspace"]
    _require(saved["saved_revision"] == initial["saved_revision"] + 1,
             "Only the explicit save should create the next revision")
    reread = get("/api/workspaces/" + quote(saved["id"], safe=""))
    _require(reread == saved and reread["assemblies"] == inventory
             and reread["associations"] == alternative["associations"],
             "Saved API reread must preserve all paths, rules, source evidence and inventory links")
    _require(all(path["project"]["side_slopes"] == side
             and path["project"]["slope_rules"] == checked["rules"]
             for path in reread["paths"]), "Both saved alternatives must retain the actual records")
    changed_sources = deepcopy(reread["terrain_sources"])
    changed_sources[0]["priority"] += 1
    stale = post("/api/workspace/action", {"workspace": reread, "config": {
        "action": "update_shared", "terrain_sources": changed_sources}})
    stale_analysis = stale["analysis"]["active_path_analysis"]
    stale_rule = stale_analysis["slope_rule_checks"]["results"][0]
    _require(not stale_analysis["side_slopes_metadata"]["source_binding_current"]
             and stale_rule["status"] == "unknown"
             and stale_rule["components"]["side"]["status"] == "unknown",
             "A priority change must invalidate the source binding and prevent a passing rule")
    _require(stale["workspace"]["assemblies"] == inventory
             and stale["workspace"]["associations"] == alternative["associations"]
             and stale["workspace"]["saved_revision"] == saved["saved_revision"],
             "A stale source candidate must preserve revision, inventory and associations")
    _require({row["path_id"] for row in stale["warnings"] if row["code"] == "SIDE_SLOPES_STALE"}
             == {row["id"] for row in reread["paths"]}, "Both source-bound paths must become stale")
    _require(len(calls) == 12, "Unexpected workflow request count")
    report = {
        "status": "passed", "synthetic": True,
        "actual_api_requests": len(calls), "calls": calls,
        "source_xyz_points": len(rows) - 1,
        "source_text_sha256": hashlib.sha256(source["text"].encode("utf-8")).hexdigest(),
        "side_model": side["model"], "station_count": len(samples),
        "cross_probe_count": sum(len(row["transect"]) for row in samples),
        "first_probe_depths_m": depths, "first_side_slope_deg": first["side_slope_deg"],
        "inline_rule_status": actual_rule["components"]["inline"]["status"],
        "inline_basis": "explicit_synthetic_constant_waypoint_depth_approximation",
        "side_rule_status": actual_rule["components"]["side"]["status"],
        "side_violation_count": len(actual_rule["violations"]),
        "nullable_sampling_end_equivalent": True, "nullable_rule_end_preserved": True,
        "preview_not_persisted": True, "atomic_side_and_rules_applied": True,
        "workspace_id": saved["id"], "saved_revision": saved["saved_revision"],
        "path_count": len(reread["paths"]), "assembly_count": len(inventory),
        "inventory_and_original_association_unchanged": True,
        "api_saved_workspace_reread_equal": True,
        "storage_owner_close_verified_by_harness": False,
        "reread_scope": "actual GET only; caller may separately close/reopen owner before the second GET",
        "saved_side_slopes_sha256": hashlib.sha256(_finite_json(side)).hexdigest(),
        "stale_priority_rule_status": stale_rule["status"],
        "stale_source_path_count": len(reread["paths"]),
        "manufactured_total_m": copied["analysis"]["summary"]["manufactured_total_m"],
        "procurement_cost": copied["analysis"]["summary"]["procurement_cost"],
        "elapsed_s": round(time.monotonic() - started, 6),
        "limitations": ["Explicit synthetic inputs; no field or original-product equivalence.",
                        "Sampled slope rules do not certify unsampled continuous seabed."],
    }
    _finite_json(report)
    return report
