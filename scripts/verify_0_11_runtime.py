"""Verify recorded 0.11 execution inputs and runtime bytes; never rerun gates.

Default mode creates new evidence only after every check succeeds. --verify-only
compares existing and byte-identical embedded evidence with no filesystem writes. Full ZIP member
enumeration belongs to audit_release.py; this script checks the initial-install
runtime snapshot in the selected ZIP without embedding that ZIP's total hash.
"""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timedelta, timezone
from email.parser import Parser
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "resources/validation"
VERSION = "0.11.0"
LABEL = "0.11"
# Counts are derived from actual version-bound execution/XML/JSON and frozen rows.
# No previous-release testcase, input, module or artifact count is inherited.
EXPECTED_BACKEND = None
EXPECTED_BROWSER = None


def require(condition: bool, message: str) -> None:
    # Evidence checks must remain effective under python -O.
    if not condition:
        raise RuntimeError(message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_path(raw: str) -> Path:
    require(isinstance(raw, str) and bool(raw), "Evidence path must be a nonempty string")
    value = PurePosixPath(raw)
    require(value.as_posix() == raw and not value.is_absolute()
            and ".." not in value.parts and "\\" not in raw and ":" not in raw,
            f"Noncanonical evidence path: {raw!r}")
    path = ROOT.joinpath(*value.parts)
    require(path.resolve().is_relative_to(ROOT), f"Evidence path escapes checkout: {raw}")
    return path


def parse_json(data: bytes):
    def reject(value):
        raise ValueError(f"Nonfinite JSON constant {value}")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"Duplicate JSON field: {key}")
            result[key] = value
        return result
    return json.loads(data.decode("utf-8"), parse_constant=reject, object_pairs_hook=unique_object)


def load(name: str) -> dict:
    result = parse_json((VALIDATION / name).read_bytes())
    require(isinstance(result, dict), f"Report must be an object: {name}")
    return result


def verify_row(row: dict) -> Path:
    require(isinstance(row, dict), "Evidence row must be an object")
    path = relative_path(row.get("path"))
    size, digest = row.get("bytes"), row.get("sha256")
    require(type(size) is int and size >= 0, f"Invalid byte count: {row['path']}")
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
            f"Invalid SHA256: {row['path']}")
    require(path.is_file() and path.stat().st_size == size and file_sha(path) == digest,
            f"Current bytes differ from recorded evidence: {row['path']}")
    return path


def unique_rows(rows: list, name: str) -> dict:
    require(isinstance(rows, list), f"{name} must be an array")
    paths = [row.get("path") if isinstance(row, dict) else None for row in rows]
    require(all(isinstance(path, str) for path in paths) and len(paths) == len(set(paths)),
            f"Duplicate or invalid paths in {name}")
    return dict(zip(paths, rows))


def row_identity(row: dict) -> tuple:
    return row["path"], row["bytes"], row["sha256"]


def leaves(suites: list) -> list:
    require(isinstance(suites, list), "Browser suites must be an array")
    tests = []
    for suite in suites:
        for spec in suite.get("specs", []):
            tests.extend(spec.get("tests", []))
        tests.extend(leaves(suite.get("suites", [])))
    return tests


def passed_summary(output: str, expected: int) -> float:
    require(isinstance(output, str), "Missing recorded pytest output")
    summaries = re.findall(r"(?m)^(\d+) passed(?:, \d+ warnings?)? in ([0-9.]+)s(?: \([0-9:]+\))?\s*$", output)
    require(len(summaries) == 1 and int(summaries[0][0]) == expected,
            f"Recorded pytest output does not uniquely show {expected} passed")
    return float(summaries[0][1])


def require_true(data: dict, fields: tuple[str, ...], label: str) -> None:
    require(isinstance(data, dict), f"Missing {label} evidence")
    for field in fields:
        require(data.get(field) is True, f"Missing successful {label}.{field} evidence")


def verify_inputs(kind: str, execution: dict) -> dict:
    name = f"release_{LABEL}_{kind}_inputs.json"
    snapshot = load(name)
    require(snapshot.get("version") == VERSION, f"Wrong version in {name}")
    rows = unique_rows(snapshot.get("files"), name)
    require(rows and len(rows) == execution.get("input_files"), f"Input count differs: {kind}")
    if kind == "backend":
        require(set(rows) == backend_runtime_paths(), "Full backend input member set differs")
    if kind == "browser":
        require(set(rows) == browser_runtime_paths(), "Full browser input member set differs")
    require(sha((VALIDATION / name).read_bytes()) == execution.get("input_snapshot_sha256"),
            f"Execution snapshot digest differs: {kind}")
    require(execution.get("version") == VERSION and execution.get("status") == "passed"
            and execution.get("returncode") == 0 and execution.get("changed_inputs") == []
            and not execution.get("timed_out", False), f"Unsuccessful or changed {kind} execution")
    for row in rows.values():
        verify_row(row)
    return {"count": len(rows), "current_bytes_match_recorded_execution": True}


def zip_members(package: zipfile.ZipFile, label: str) -> set[str]:
    names = package.namelist()
    require(len(names) == len(set(names)), f"Duplicate {label} members")
    require(package.testzip() is None, f"{label} CRC failure")
    for info in package.infolist():
        path = PurePosixPath(info.filename)
        require(path.as_posix() == info.filename and not path.is_absolute()
                and ".." not in path.parts and "\\" not in info.filename
                and not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16),
                f"Unsafe {label} member: {info.filename}")
    return set(names)


def required_runtime_paths() -> set[str]:
    # Minimum independent coverage of installed execution inputs. Extra recorded
    # files are allowed, but every extra file is equally byte-checked.
    names = {"launcher.py", "pyproject.toml", "web/index.html", "web/package.json",
             "web/package-lock.json", "web/tsconfig.json", "web/vite.config.ts",
             "web/playwright.config.ts", "resources/research/s57_sources.json", "resources/research/arc_sources.json",
             "scripts/smoke_release.py", "scripts/smoke_portable.py", "scripts/side_slopes_smoke.py",
             "scripts/automatic_rules_smoke.py", "scripts/altercourse_smoke.py", "scripts/verify_0_11_runtime.py"}
    for folder in ("oceanroute", "examples", "tests", "web/src", "web/tests", "web/public"):
        names.update(path.relative_to(ROOT).as_posix() for path in (ROOT / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    return names


def verify_wheel(wheel_path: Path, archive: Path) -> tuple[set[str], int]:
    with zipfile.ZipFile(wheel_path) as wheel:
        names = zip_members(wheel, "wheel")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        require(len(metadata_names) == 1, "Wheel must have exactly one METADATA")
        metadata = Parser().parsestr(wheel.read(metadata_names[0]).decode("utf-8"))
        require(metadata.get("Name", "").lower() == "oceanroute" and metadata.get("Version") == VERSION,
                "Wheel metadata version/name differs")
        package = {name for name in names if name.startswith("oceanroute/")}
        expected = {path.relative_to(ROOT).as_posix() for path in (ROOT / "oceanroute").rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
        require(package == expected, "Wheel package set differs from current source")
        with zipfile.ZipFile(archive) as bundle:
            for name in sorted(package):
                data = wheel.read(name)
                require(data == (ROOT / name).read_bytes() == bundle.read("OceanRoute/" + name),
                        f"Wheel/selected ZIP/current source differ: {name}")
        record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
        require(len(record_names) == 1, "Wheel must have exactly one RECORD")
        record_name = record_names[0]
        records = list(csv.reader(io.StringIO(wheel.read(record_name).decode("utf-8"))))
        require(all(len(row) == 3 for row in records), "Invalid wheel RECORD row")
        require(len(records) == len(names) and {row[0] for row in records} == names,
                "Wheel RECORD has missing, duplicate or additional paths")
        for name, digest, size in records:
            if name == record_name:
                require(digest == size == "", "RECORD must not recursively hash itself")
                continue
            data = wheel.read(name)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            require(digest == "sha256=" + encoded and size == str(len(data)), f"Bad RECORD digest/size: {name}")
    return package, len(records)


def module_union(wheel_smoke: dict, package: set[str]) -> dict:
    union = {}
    sources = [wheel_smoke.get("isolated_modules")]
    for branch in ("heterogeneous_and_current_equilibrium", "native_s57", "side_slopes_and_kp_rules", "automatic_geographic_rules", "altercourse_geometry"):
        evidence = wheel_smoke.get(branch)
        require(isinstance(evidence, dict) and evidence.get("status") == "passed", f"Missing wheel smoke: {branch}")
        sources.append(evidence.get("isolated_modules"))
    for modules in sources:
        require(isinstance(modules, dict) and modules, "Missing isolated wheel module origins")
        for name, path in modules.items():
            require(isinstance(name, str) and re.fullmatch(r"oceanroute(?:\.[A-Za-z_]\w*)*", name) is not None,
                    f"Non-OceanRoute module reported: {name!r}")
            expected = "oceanroute/__init__.py" if name == "oceanroute" else name.replace(".", "/") + ".py"
            require(path == expected and path in package, f"Module origin is not its wheel package member: {name}")
            require(name not in union or union[name] == path, f"Conflicting module origin: {name}")
            union[name] = path
    require({"oceanroute.side_slopes", "oceanroute.slope_rules", "oceanroute.sqlite_lifecycle",
             "oceanroute.hydrodynamics", "oceanroute.current_equilibrium", "oceanroute.current_dynamics",
             "oceanroute.s57", "oceanroute.automatic_rules", "oceanroute.automatic_rule_geometry",
             "oceanroute.terrain_slope_neighborhoods", "oceanroute.route_geometry", "oceanroute.altercourse",
             "oceanroute.altercourse_workspace"} <= set(union), "New or inherited isolated modules were omitted")
    return dict(sorted(union.items()))


def verify_side_smoke(evidence: dict, *, wheel: bool) -> int:
    require(isinstance(evidence, dict) and evidence.get("status") == "passed", "Missing actual side-slope smoke")
    require_true(evidence, ("synthetic", "nullable_sampling_end_equivalent", "nullable_rule_end_preserved",
                           "preview_not_persisted", "atomic_side_and_rules_applied",
                           "inventory_and_original_association_unchanged", "api_saved_workspace_reread_equal"),
                 "side_slopes_and_kp_rules")
    require(evidence.get("actual_api_requests") == len(evidence.get("calls", [])) == 12,
            "Side-slope workflow must record twelve actual requests")
    calls = evidence["calls"]
    expected = [("POST", "/api/workspace/migrate"), ("POST", "/api/workspaces"),
                ("POST", "/api/terrain/side-slopes/example"), ("POST", "/api/terrain/side-slopes"),
                ("POST", "/api/terrain/side-slopes"), ("POST", "/api/tools/slope-rules"),
                ("GET", None), ("POST", "/api/workspace/action"), ("POST", "/api/workspace/action"),
                ("POST", "/api/workspaces"), ("GET", None), ("POST", "/api/workspace/action")]
    for row, (method, path) in zip(calls, expected):
        require(isinstance(row, dict) and row.get("method") == method, "Side-slope call order/method differs")
        require(row.get("path") == path if path else isinstance(row.get("path"), str)
                and row["path"].startswith("/api/workspaces/"), "Side-slope call endpoint differs")
    require(calls[6]["path"] == calls[10]["path"], "Saved workspace reread targeted a different workspace")
    require(evidence.get("harness_sha256") == file_sha(ROOT / "scripts/side_slopes_smoke.py"),
            "Side-slope smoke harness differs from recorded execution")
    require(evidence.get("side_model") == "route-side-slopes-v1"
            and evidence.get("source_xyz_points") == 9 and evidence.get("path_count") == 2
            and evidence.get("assembly_count") == 1 and evidence.get("saved_revision") == 2
            and evidence.get("inline_rule_status") == "sampled_pass"
            and evidence.get("side_rule_status") == "violations"
            and evidence.get("stale_priority_rule_status") == "unknown"
            and evidence.get("stale_source_path_count") == 2,
            "Missing actual sampled, shared-inventory or stale-source side-slope evidence")
    require(evidence.get("side_violation_count") == evidence.get("station_count")
            and type(evidence.get("station_count")) is int and 2 <= evidence["station_count"] <= 20
            and evidence.get("cross_probe_count") == 5 * evidence["station_count"], "Unexpected real station/probe counts")
    require(evidence.get("first_probe_depths_m") and len(evidence["first_probe_depths_m"]) == 5,
            "Missing actual cross-section depths")
    for actual, expected_depth in zip(evidence["first_probe_depths_m"], (1520, 1510, 1500, 1490, 1480)):
        require(abs(actual - expected_depth) <= 1e-7, "Recorded synthetic cross-section depth differs")
    require(abs(evidence["first_side_slope_deg"] - 11.309932474020215) <= 1e-8,
            "Recorded signed starboard-up side slope differs")
    if wheel:
        require(evidence.get("version") == VERSION and evidence.get("actual_asgi_owner_closed_and_reopened") is True,
                "Wheel side workflow did not actually close/reopen its ASGI/storage owner")
    else:
        require(evidence.get("storage_owner_close_verified_by_harness") is False,
                "Fresh HTTP reread must not be described as closing its server owner")
    return 12


def verify_inherited_smoke(wheel_smoke: dict, portable: dict) -> None:
    require_true(wheel_smoke, ("manual", "analysis", "revision_guard"), "wheel")
    groups = {
        "workspace": ("migration", "shared_alternative", "stale_save_rejected", "stale_restore_rejected"),
        "seismic": ("estimate_accepted", "covariance_valid"),
        "voyage": ("checkpoint_validated", "durable_reopen", "writer_lifespan_reopened"),
        "rpl_templates": ("fixed_multiline_records", "template_json_roundtrip", "collect_blocks_apply", "explicit_skip_bridge"),
        "dtm": ("converged", "unique_solution_verified", "bln_roundtrip", "preview_ignored"),
        "coordinates": ("reverse_roundtrip", "best_available", "bad_point_preserved", "partial_apply_blocked"),
        "map_projection": ("can_display",), "workspace_terrain": ("can_apply", "preview_does_not_save"),
        "catenary_calculator": ("actual_node_roundtrips",), "static_bathymetry": ("slope_accepted", "curved_accepted"),
        "initial_equilibrium": ("raw_preparation_verified", "off_bed_anchor_not_touchdown",
            "independent_hooke_force_and_half_weight_reconstruction", "raw_accepted_object_rejected",
            "insufficient_initial_budget_rejected", "json_checkpoint_resume_matches_uninterrupted_state"),
        "geographic_equilibrium_plan": ("analytic_mercator_origin_checked", "complete_terrain_translation_checked",
            "model_heights_preserved", "initial_inventory_not_repaid", "off_bed_anchor_not_touchdown",
            "durable_mapping_and_full_terrain_reopened", "resume_matches_uninterrupted_state",
            "resume_does_not_reoptimize_static_state", "stale_geometry_rejected", "stale_physics_rejected",
            "unbound_local_geographic_frame_rejected", "prepared_window_overrun_rejected"),
        "terrain_sources": ("hole_fallback", "coverage_fallback", "all_missing_kept_null", "profile_import_valid",
            "priority_change_invalidates_both_profiles", "waypoint_depth_not_substituted",
            "full_source_payload_reopened", "reopened_priority_query_verified"),
        "bathymetry": ("explicit_height_required", "direct_resume_matches_uninterrupted_state",
            "durable_full_grid_and_contact_reopened", "background_resume_matches_uninterrupted_state"),
    }
    for name, fields in groups.items():
        require_true(wheel_smoke.get(name), fields, f"wheel.{name}")
    calculator = wheel_smoke["catenary_calculator"]
    require(calculator.get("boundaries_exercised") == 4 and calculator.get("length_bases_exercised") == 2,
            "Inherited four-boundary/two-length-basis calculator evidence missing")
    require(wheel_smoke["coordinates"].get("ballpark") is False, "Inherited CRS evidence used ballpark")
    require(wheel_smoke["initial_equilibrium"].get("resume_static_optimizer_run") is False,
            "Inherited checkpoint resume reran static optimizer")
    current = wheel_smoke["heterogeneous_and_current_equilibrium"]
    require(current.get("durable_geographic_owner_reopen_json_child_resume") is True,
            "Inherited durable current-plan owner reopen missing")
    names = {"heterogeneous-initial-dynamic.json", "heterogeneous-initial-plan-voyage.json",
             "current-initial-dynamic.json", "current-initial-plan-voyage.json"}
    require(set(current.get("examples", {})) == names, "Inherited four current/material examples missing")
    fresh = portable.get("synthetic_physical_http_examples", {})
    require(set(fresh) == names | {"initial-equilibrium-dynamic.json", "geographic-equilibrium-voyage.json"},
            "Fresh install did not execute the six shipped physical examples")
    for name in names:
        recorded, installed = current["examples"][name], fresh[name]
        is_current = name.startswith("current-")
        require(recorded.get("schema_version") == (4 if is_current else 3)
                and recorded.get("actual_requests") == 4 and recorded.get("six_arrays_exact") is True
                and installed.get("requests") == 4 and installed.get("split_json_resume_exact_six_arrays") is True,
                f"Actual JSON split/resume evidence missing: {name}")
        require(recorded.get("model") == installed.get("model")
                and recorded.get("proof_schema") == installed.get("proof_schema")
                and installed.get("http_status") == 200, f"Installed/wheel physical contracts differ: {name}")
    fixture = ROOT / "tests/fixtures/s57/noaa/US5A1KMJ.zip"
    for evidence, requests in ((wheel_smoke["native_s57"], 14), (portable.get("native_s57"), 11)):
        require_true(evidence, ("atomic_shared_update", "preview_not_persisted", "path_assembly_material_inventory_unchanged"), "native_s57")
        require(evidence.get("status") == "passed" and evidence.get("actual_http_requests") == requests
                and evidence.get("base_update_number") == 0 and evidence.get("applied_update_number") == 2
                and evidence.get("soundg_xyz_points") == 657
                and evidence.get("fixture", {}).get("sha256") == file_sha(fixture),
                "Inherited real native S57 update/geometry evidence differs")
    require(wheel_smoke["native_s57"].get("database_owner_reopened") is True,
            "Native wheel storage owner was not reopened")
    require(portable["native_s57"].get("saved_source_and_z_geometry_reopened") is True,
            "Installed native source/Z geometry reread missing")


def utc(value: str) -> datetime:
    require(isinstance(value, str), "Missing recorded timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "Timestamp must carry a timezone")
    return parsed.astimezone(timezone.utc)


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as image:
        head = image.read(24)
    require(len(head) == 24 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR",
            f"Not an actual PNG: {path}")
    return struct.unpack(">II", head[16:24])


def verify_screenshots(browser: dict) -> dict:
    provenance = load(f"release_{LABEL}_screenshot_provenance.json")
    require(provenance.get("version") == VERSION and provenance.get("status") == "passed",
            "Missing successful new screenshot provenance")
    rows = unique_rows(provenance.get("screenshots"), "screenshots")
    require(rows and provenance.get("screenshot_count") == len(rows), "Missing screenshots or incorrect full-execution count")
    execution = f"resources/validation/release_{LABEL}_browser_execution.json"
    require(provenance.get("source_execution") == execution
            and provenance.get("source_execution_sha256") == file_sha(ROOT / execution)
            and provenance.get("actual_tests") == EXPECTED_BROWSER,
            "Screenshot provenance is not bound to the complete actual browser execution")
    artifact_dir = browser.get("environment", {}).get("OCEANROUTE_ARTIFACTS_DIR")
    require(isinstance(artifact_dir, str) and bool(artifact_dir),
            "Actual final browser execution omitted its screenshot directory")
    artifact_path = PurePosixPath(artifact_dir)
    require(artifact_path.as_posix() == artifact_dir and not artifact_path.is_absolute()
            and ".." not in artifact_path.parts and "\\" not in artifact_dir and ":" not in artifact_dir
            and artifact_path.parts[:3] == ("artifacts", f"release-{LABEL}", "final"),
            "Canonical screenshots must originate from this release's actual final execution directory")
    directory = relative_path("web/" + artifact_dir)
    actual = {path.relative_to(ROOT).as_posix() for path in directory.rglob("*") if path.is_file()}
    require(actual == set(rows), "Full-browser screenshot member set differs from provenance")
    start = utc(browser["started_at_utc"])
    end = start + timedelta(seconds=browser["wall_time_s"])
    window = provenance.get("generation_window_utc", {})
    # The provenance end is the execution report's recorded timestamp, after
    # parsing stdout and checking inputs. Validate that exact declared window;
    # each PNG must additionally lie inside the shorter actual process window.
    require(utc(window.get("start")) == start
            and utc(window.get("end")) == utc(browser["recorded_at_utc"])
            and utc(window["end"]) >= end,
            "Screenshot provenance window differs from the actual execution report")
    require(start <= utc(browser["stats"]["startTime"]) <= end,
            "Browser-reported start is outside the recorded process window")
    for row in rows.values():
        path = verify_row(row)
        recorded = utc(row["filesystem_mtime_utc"])
        actual_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        require(abs((actual_time - recorded).total_seconds()) <= 1e-6, f"Screenshot filesystem time changed: {row['path']}")
        require(start <= recorded <= end, f"Screenshot outside complete browser execution: {row['path']}")
        require(png_dimensions(path) == (row["width"], row["height"]), f"Screenshot PNG dimensions differ: {row['path']}")
    cover = provenance.get("pdf_cover_provenance", {})
    source = cover.get("actual_source")
    require(isinstance(source, str) and source in rows, "Current cover has no actual full-execution source image")
    target = f"web/artifacts/release-{LABEL}/pdf-cover.png"
    require(cover.get("actual_source") == source and cover.get("release_copy") == target
            and cover.get("source_execution") == execution
            and cover.get("source_execution_sha256") == file_sha(ROOT / execution), "Cover does not originate from this full browser execution")
    require(source in rows and cover.get("bytes") == rows[source]["bytes"]
            and cover.get("sha256") == rows[source]["sha256"], "Cover/source snapshot differs")
    verify_row({"path": target, "bytes": cover["bytes"], "sha256": cover["sha256"]})
    require((ROOT / source).read_bytes() == (ROOT / target).read_bytes(), "PDF cover is not an exact byte copy")
    visual = load(f"release_{LABEL}_screenshot_visual_review.json")
    require(visual.get("version") == VERSION and visual.get("status") == "passed"
            and not visual.get("blocking_defects", []), "Screenshot visual review has unresolved defects")
    inventory = visual.get("all_screenshot_provenance", {})
    recorded = unique_rows(inventory.get("files"), "visual screenshot inventory")
    require(recorded == rows and inventory.get("file_count") == len(rows)
            and inventory.get("source_report") == f"resources/validation/release_{LABEL}_screenshot_provenance.json"
            and inventory.get("source_report_sha256") == file_sha(VALIDATION / f"release_{LABEL}_screenshot_provenance.json"),
            "Visual review inventory differs from actual browser provenance")
    actual_browser = visual.get("actual_browser_report", {})
    require(actual_browser.get("path") == f"resources/validation/release_{LABEL}_browser.json"
            and actual_browser.get("sha256") == file_sha(VALIDATION / f"release_{LABEL}_browser.json")
            and actual_browser.get("execution_path") == execution
            and actual_browser.get("execution_sha256") == file_sha(ROOT / execution)
            and actual_browser.get("stats") == browser["stats"]
            and actual_browser.get("actual_test_count") == EXPECTED_BROWSER
            and actual_browser.get("every_actual_test_passed_once_retry_zero") is True,
            "Screenshot visual review is not bound to this actual browser execution")
    require(visual.get("pdf_cover_provenance") == cover, "Screenshot visual cover provenance differs")
    viewed = unique_rows(visual.get("actual_viewed_images"), "actually viewed screenshots")
    require(visual.get("actual_viewed_image_count") == len(viewed), "Actual visual image count differs")
    require(viewed and set(viewed) <= set(rows) | {target}, "Visual review has no valid individual image witnesses")
    for name, row in viewed.items():
        path = verify_row(row)
        require(row.get("actually_viewed") is True and row.get("status") in {"pass", "passed"}
                and not row.get("blocking_defects", []), f"Image was not actually visually passed: {name}")
        basis = rows[name] if name in rows else {"path": target, "bytes": cover["bytes"], "sha256": cover["sha256"]}
        require(row_identity(row) == row_identity(basis), f"Viewed image differs from current capture: {name}")
        require(png_dimensions(path)[0] > 0, f"Invalid viewed PNG: {name}")
    return {"final_full_browser": len(rows), "pdf_cover_byte_copy_from_full_browser": 1,
            "actually_visually_reviewed_images": len(viewed),
            "all_current_bytes_dimensions_and_execution_window_match": True,
            "scope": "Hash/PNG/filesystem-time checks bind recorded files to the exclusive execution window; they are not source identity authentication. Only the listed individual images have actual visual review evidence."}


def verify_pdf() -> dict:
    qa = load(f"release_{LABEL}_pdf_qa.json")
    require(qa.get("version") == VERSION and qa.get("status") == "passed"
            and qa.get("all_pages_actually_visually_verified") is True, "Incomplete current PDF QA")
    for key in ("builder", "cover"):
        verify_row(qa[key])
    require(qa["cover"]["path"] == f"web/artifacts/release-{LABEL}/pdf-cover.png", "PDF QA used an old cover")
    documents = qa.get("documents", [])
    require(len(documents) == 2 and {item["kind"] for item in documents} == {"manual", "design"}, "Both current PDFs must be reviewed")
    pages = {}
    for document in documents:
        kind = document["kind"]
        title = "用户手册" if kind == "manual" else "设计文档"
        source = "docs/USER_MANUAL.md" if kind == "manual" else "docs/DESIGN.md"
        require(document["pdf"]["path"] == f"output/pdf/OceanRoute_{title}_{LABEL}.pdf"
                and document["source"]["path"] == source, "PDF source or suffix differs")
        for key in ("pdf", "source", "review"):
            verify_row(document[key])
        count = document["pdf"]["pages"]
        require(type(count) is int and count > 0 and not document.get("blocking_defects"), "Invalid PDF page count or blocking defect")
        renders = document.get("renders", [])
        require(document.get("actual_visual_review_pages") == count == len(renders)
                and {row["page"] for row in renders} == set(range(1, count + 1)), "Not every current PDF page has a render/review")
        for row in renders:
            verify_row(row)
        review = parse_json((ROOT / document["review"]["path"]).read_bytes())
        require(review.get("status") in {"pass", "passed"}
                and (review.get("all_pages_verified") is True or review.get("all_pages_actually_viewed") is True)
                and not review.get("blocking_defects", []) and not review.get("remaining_defects", [])
                and not review.get("issues", []), "Independent PDF review is incomplete or has unresolved defects")
        viewed = review.get("actual_viewed_pages", review.get("actually_viewed_pages"))
        require(isinstance(viewed, list) and len(viewed) == count, "Actual PDF visual page list is incomplete")
        object_pages = all(isinstance(row, dict) for row in viewed)
        viewed_numbers = [row["page"] for row in viewed] if object_pages else viewed
        require(all(type(page) is int for page in viewed_numbers)
                and sorted(viewed_numbers) == list(range(1, count + 1)), "Actual PDF visual page list is incomplete/duplicated")
        review_pages = viewed if object_pages else review.get("pages", [])
        require(len(review_pages) == count and {row["page"] for row in review_pages} == set(range(1, count + 1)), "Independent per-page review missing")
        render_by_page = {row["page"]: row for row in renders}
        for row in review_pages:
            # The new object-valued actual_viewed_pages list is itself an
            # explicit per-image witness when the global actual-view flag is
            # true. Optional per-row fields may strengthen, never contradict it.
            witnessed = row.get("actually_viewed", row.get("actual_view_image",
                              object_pages and review.get("all_pages_actually_viewed") is True))
            require(witnessed is True and row.get("status", "passed" if object_pages else None) in {"pass", "passed"}
                    and not row.get("blocking_defects", []) and not row.get("issues", []), "Unreviewed or defective PDF page")
            recorded = {"path": row.get("path", row.get("image", row.get("render_path"))),
                        "bytes": row.get("bytes", row.get("image_bytes", row.get("render_bytes"))),
                        "sha256": row.get("sha256", row.get("image_sha256", row.get("render_sha256")))}
            require(row_identity(recorded) == row_identity(render_by_page[row["page"]]), "PDF render differs from actual per-page visual evidence")
        pdf_hash = review.get("pdf_sha256", review.get("pdf", {}).get("sha256"))
        require(pdf_hash == document["pdf"]["sha256"], "PDF hash differs from independent review")
        if "source_files" in review:
            sources = unique_rows(review["source_files"], "PDF reviewed sources")
            for row in sources.values():
                verify_row(row)
            require(source in sources and row_identity(sources[source]) == row_identity(document["source"]), "PDF source differs from reviewed source")
        elif "source" in review and "builder" in review and isinstance(review["builder"], dict):
            require(review["source"].get("path") == source
                    and review["source"].get("sha256") == document["source"]["sha256"]
                    and review["builder"].get("path") == qa["builder"]["path"]
                    and review["builder"].get("sha256") == qa["builder"]["sha256"],
                    "PDF source/builder differs from actual review")
        else:
            require(review.get("source_document") == source and review.get("source_sha256") == document["source"]["sha256"]
                    and review.get("builder_sha256") == qa["builder"]["sha256"], "Design source/builder differs from review")
        pages[kind] = count
    require(qa.get("total_pages") == sum(pages.values()), "PDF QA total page count differs")
    bounds = load(f"release_{LABEL}_pdf_character_bounds.json")
    require(bounds.get("version") == VERSION and bounds.get("status") == "passed", "Character-bound evidence failed")
    require(len(bounds.get("documents", [])) == 2, "Missing PDF character-bound documents")
    bound_by_kind = {row["kind"]: row for row in bounds["documents"]}
    require(set(bound_by_kind) == set(pages), "Character-bound PDF set differs")
    require(row_identity(bounds.get("builder", {})) == row_identity(qa["builder"]),
            "PDF structural check used a different builder")
    verify_row(bounds["builder"])
    verify_row(bounds["validator"])
    require(bounds.get("actual_pages_visually_reviewed_by_this_script") == [],
            "Automatic PDF structural validation must not claim actual visual review")
    for document in documents:
        row = bound_by_kind[document["kind"]]
        verify_row(row["pdf"])
        verify_row(row["source"])
        require(row_identity(row["pdf"]) == row_identity(document["pdf"])
                and row_identity(row["source"]) == row_identity(document["source"])
                and row.get("status") == "passed" and row.get("pages") == pages[document["kind"]]
                and row.get("character_boundary_outliers") == []
                and row.get("unsupported_extraction_tokens") == []
                and row.get("source_version_0_11_present") is True
                and row.get("cover_version_0_11_present") is True, "PDF characters/snapshot differ")
        checks = row.get("page_checks", [])
        require(len(checks) == row["pages"] and {check["page"] for check in checks} == set(range(1, row["pages"] + 1)),
                "Missing actual per-page PDF structural checks")
        for check in checks:
            require(check.get("status") == "passed" and check.get("character_boundary_outliers") == 0
                    and check.get("unsupported_extraction_tokens") == []
                    and check.get("body_text_characters", 0) > 0
                    and check.get("header_present") is True and check.get("footer_version_date_present") is True
                    and check.get("page_number_present") is True, "PDF page has missing text, glyph, bounds or page furniture")
        coverage = row.get("source_coverage", {})
        require(coverage.get("checked_printable_units", 0) > 0 and coverage.get("missing_units") == []
                and coverage.get("printable_character_shortfalls") == [], "Maintained PDF source content is missing")
        new = row.get("new_0_11_content", {})
        require(new.get("required_section") and new.get("required_fields_and_terms")
                and new.get("missing_from_source") == [] and new.get("missing_from_pdf") == [],
                "New 0.11 PDF content is missing")
    return pages


def verify_installation_candidates(initial: dict) -> dict:
    """Bind the preserved real failure and corrected installation, not a rerun."""
    first = load(f"release_{LABEL}_initial_archive.json")
    failure = load(f"release_{LABEL}_initial_installation_failure.json")
    require(first.get("version") == initial.get("version") == VERSION,
            "Installation candidate snapshot versions differ")
    old_rows = unique_rows(first.get("runtime_files"), "first failed-install runtime")
    rows = unique_rows(initial.get("runtime_files"), "verified initial-install runtime")
    require(rows and set(rows) == set(old_rows) and required_runtime_paths() <= set(rows)
            and first.get("runtime_files_count") == len(old_rows)
            and initial.get("runtime_files_count") == len(rows),
            "First/corrected candidate runtime path sets or declared counts differ")
    changed = {name for name in rows if row_identity(rows[name]) != row_identity(old_rows[name])}
    require(changed == {"scripts/smoke_portable.py", "scripts/verify_0_11_runtime.py"},
            "Correction must change only the two actual installation/evidence harnesses")
    require(row_identity(first["wheel"]) == row_identity(initial["wheel"]),
            "The corrected source installation unexpectedly changed the tested wheel")
    require(failure.get("version") == VERSION and failure.get("status") == "failed"
            and failure.get("exit_code") == 1 and failure.get("failure_type") == "ModuleNotFoundError"
            and failure.get("missing_module") == "oceanroute.terrain_profile"
            and failure.get("successful_portable_report_written") is False
            and failure.get("fresh_full_pytest_executed") is False
            and failure.get("phase") ==
                "fresh launcher installation, after HTTP workflow, before test dependency installation and full pytest",
            "The actual first import-probe failure must not be counted as full installation success")
    require(failure.get("initial_archive_record", {}).get("path") ==
                f"resources/validation/release_{LABEL}_initial_archive.json",
            "First failure did not bind the original candidate snapshot")
    verify_row(failure["initial_archive_record"])
    require(row_identity(failure["preserved_initial_archive"]) == row_identity(first["preserved_initial_archive"]),
            "Failure/preserved candidate evidence differs")
    log_path = verify_row(failure["actual_log"])
    require("ModuleNotFoundError: No module named 'oceanroute.terrain_profile'" in log_path.read_text(encoding="utf-8"),
            "Missing actual native Python import failure traceback")
    first_harness = verify_row(failure["first_harness"])
    corrected_harness = verify_row(failure["corrected_harness"])
    require(failure["first_harness"]["bytes"] == old_rows["scripts/smoke_portable.py"]["bytes"]
            and failure["first_harness"]["sha256"] == old_rows["scripts/smoke_portable.py"]["sha256"]
            and row_identity(failure["corrected_harness"]) == row_identity(rows["scripts/smoke_portable.py"]),
            "Failed/corrected import probes do not bind the two actual candidate harnesses")
    repaired = first_harness.read_bytes()
    for old, new in ((b"oceanroute.terrain_profile", b"oceanroute.terrain_bathymetry"),
                     (b"oceanroute.terrain_cross_section", b"oceanroute.terrain_slice"),
                     (b"oceanroute.terrain_side_slopes", b"oceanroute.side_slopes"),
                     (b"oceanroute.terrain_slope_rules", b"oceanroute.slope_rules")):
        require(repaired.count(old) == 1, "Failed portable probe contained an unexpected module declaration")
        repaired = repaired.replace(old, new)
    require(repaired == corrected_harness.read_bytes(),
            "Portable harness correction changed more than the four actual module-name strings")
    preserved_counts = {}
    for label, snapshot, expected_path, expected_rows in (
        ("first_failed", first, f"outputs/releases/install-candidates/{LABEL}/OceanRoute-{LABEL}-portable.zip", old_rows),
        ("verified", initial, f"outputs/releases/install-candidates/{LABEL}/verified/OceanRoute-{LABEL}-portable.zip", rows),
    ):
        declared, retained = snapshot["initial_archive"], snapshot["preserved_initial_archive"]
        require(declared.get("path") == f"outputs/releases/OceanRoute-{LABEL}-portable.zip"
                and retained.get("path") == expected_path
                and (declared.get("bytes"), declared.get("sha256")) ==
                    (retained.get("bytes"), retained.get("sha256")),
                "Preserved installation candidate differs from its recorded original bytes")
        retained_path = verify_row(retained)
        with zipfile.ZipFile(retained_path) as bundle:
            members = zip_members(bundle, f"preserved {label} candidate")
            require(type(snapshot.get("members")) is int and len(members) == snapshot["members"],
                    "Preserved candidate ZIP count differs from its actual snapshot")
            for name, row in expected_rows.items():
                data = bundle.read("OceanRoute/" + name)
                require(len(data) == row["bytes"] and sha(data) == row["sha256"],
                        f"Preserved {label} candidate runtime differs: {name}")
        preserved_counts[label] = len(members)
    return {"verified_snapshot": f"release_{LABEL}_verified_initial_archive.json",
            "first_failed_snapshot": f"release_{LABEL}_initial_archive.json",
            "first_failed_installation_record": f"release_{LABEL}_initial_installation_failure.json",
            "first_failed_actual_log": failure["actual_log"],
            "first_failed_harness": failure["first_harness"],
            "first_failed_archive_as_originally_recorded": first["initial_archive"],
            "verified_archive_as_originally_recorded": initial["initial_archive"],
            "preserved_candidate_members": preserved_counts,
            "only_corrected_harness_runtime_inputs_changed": sorted(changed),
            "first_failure_fresh_pytest_not_executed": True,
            "portable_probe_exact_four_name_correction": True,
            "scope": "Preserved failed and verified candidate archives retain exact runtime inputs. Original failure log/harness are external checkout evidence; no success or complete HTTP assertion is inferred from the failed attempt."}


def verify(archive: Path, *, verify_only: bool) -> None:
    global EXPECTED_BACKEND, EXPECTED_BROWSER
    report_path = VALIDATION / f"release_{LABEL}_verified_runtime.json"
    require(report_path.exists() if verify_only else not report_path.exists(),
            "Read-only verification requires existing evidence; new evidence must not overwrite a prior report")
    backend = load(f"release_{LABEL}_backend.json")
    browser = load(f"release_{LABEL}_browser_execution.json")
    EXPECTED_BACKEND = backend.get("stats", {}).get("tests")
    EXPECTED_BROWSER = browser.get("actual_tests")
    require(type(EXPECTED_BACKEND) is int and EXPECTED_BACKEND > 0
            and type(EXPECTED_BROWSER) is int and EXPECTED_BROWSER > 0, "Missing actual gate counts")
    require_full_gate_commands(backend, browser)
    inputs = {kind: verify_inputs(kind, execution) for kind, execution in (("backend", backend), ("browser", browser))}
    xml_path = VALIDATION / f"release_{LABEL}_backend.xml"
    cases = list(ET.parse(xml_path).getroot().iter("testcase"))
    require(len(cases) == EXPECTED_BACKEND and all(not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")) for case in cases),
            "JUnit testcase count or status differs from required backend gate")
    require(backend.get("stats") == {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0}, "Backend stats differ from JUnit")
    backend_log = (VALIDATION / f"release_{LABEL}_backend.log").read_text(encoding="utf-8")
    require(backend_log == backend.get("stdout"), "Backend raw log differs from execution output")
    backend_seconds = passed_summary(backend_log, EXPECTED_BACKEND)
    raw = load(f"release_{LABEL}_browser.json")
    tests = leaves(raw.get("suites"))
    require(len(tests) == browser.get("actual_tests") == EXPECTED_BROWSER and not raw.get("errors")
            and raw.get("stats") == browser.get("stats"), "Actual browser count/stats differ")
    require(raw["stats"].get("expected") == EXPECTED_BROWSER and all(raw["stats"].get(key) == 0 for key in ("unexpected", "skipped", "flaky")), "Browser gate has failure, skip, flaky or missing counters")
    projects = raw.get("config", {}).get("projects", [])
    require(raw.get("config", {}).get("workers") == 1 and projects
            and all(p.get("retries") == 0 and p.get("repeatEach") == 1 for p in projects),
            "Browser gate must use one worker without configured retries/repeats")
    for test in tests:
        results = test.get("results", [])
        require(test.get("expectedStatus") == "passed" and test.get("status") == "expected"
                and len(results) == 1 and results[0].get("status") == "passed"
                and results[0].get("retry") == 0 and results[0].get("workerIndex") == 0
                and results[0].get("parallelIndex") == 0 and not results[0].get("errors"), "A browser testcase did not pass exactly once")
    require(len(browser.get("tests", [])) == len(tests), "Browser execution omitted per-test evidence")
    for actual, recorded in zip(tests, browser["tests"]):
        require(actual["status"] == recorded.get("status")
                and [{key: row.get(key) for key in ("status", "retry", "duration", "errors")} for row in actual["results"]]
                == [{key: row.get(key) for key in ("status", "retry", "duration", "errors")} for row in recorded.get("results", [])],
                "Browser raw and execution per-test results differ")
    browser_log = (VALIDATION / f"release_{LABEL}_browser.log").read_text(encoding="utf-8")
    # The gate invokes Playwright's JSON reporter, parses its complete stdout
    # into browser.json, and writes captured stderr to browser.log. An empty
    # stderr is the actual successful execution, not a missing human summary.
    command = browser.get("command", [])
    require(isinstance(command, list) and "--reporter=json" in command
            and len([part for part in command if isinstance(part, str) and part.startswith("--reporter")]) == 1
            and "--workers=1" in command and "--retries=0" in command,
            "Actual browser command did not use the single JSON reporter/worker without retries")
    require(browser_log.strip() == "", "Actual full-browser stderr has unreviewed nonempty diagnostics")
    for original, release, suffixes in (
        (f"development_{LABEL}_verified_backend", f"release_{LABEL}_backend", (".json", ".log", ".xml", "_inputs.json")),
        (f"development_{LABEL}_verified_browser", f"release_{LABEL}_browser", (".json", ".log", "_execution.json", "_inputs.json")),
    ):
        for suffix in suffixes:
            require((VALIDATION / (original + suffix)).read_bytes() == (VALIDATION / (release + suffix)).read_bytes(), f"Archival copy differs: {release + suffix}")

    # The first real installation attempt failed in an import probe. Preserve
    # its actual snapshot/log; only the later verified candidate binds success.
    initial = load(f"release_{LABEL}_verified_initial_archive.json")
    candidate_evidence = verify_installation_candidates(initial)
    rows = unique_rows(initial.get("runtime_files"), "verified initial-install runtime")
    with zipfile.ZipFile(archive) as bundle:
        zip_members(bundle, "selected ZIP")
        for row in rows.values():
            verify_row(row)
            data = bundle.read("OceanRoute/" + row["path"])
            require(len(data) == row["bytes"] and sha(data) == row["sha256"], f"Selected ZIP differs from initial-install runtime: {row['path']}")
    wheel_path = verify_row(initial["wheel"])
    package, records = verify_wheel(wheel_path, archive)
    require(parse_json((ROOT / "web/package.json").read_bytes())["version"] == VERSION
            and re.search(r'^version = "' + re.escape(VERSION) + r'"$', (ROOT / "pyproject.toml").read_text(), re.M)
            and re.search(r'^__version__ = "' + re.escape(VERSION) + r'"$', (ROOT / "oceanroute/__init__.py").read_text(), re.M), "Current source/UI versions differ")
    wheel_smoke = load(f"release_{LABEL}_wheel_smoke.json")
    require(wheel_smoke.get("version") == wheel_smoke.get("wheel_metadata_version") == VERSION
            and wheel_smoke.get("wheel_bytes") == initial["wheel"]["bytes"]
            and wheel_smoke.get("wheel_sha256") == initial["wheel"]["sha256"], "Wheel smoke does not bind actual wheel bytes/version")
    modules = module_union(wheel_smoke, package)
    historical_runtime = load("release_0.10_verified_runtime.json")
    inherited_modules = historical_runtime.get("execution_evidence", {}).get("isolated_modules", {})
    require(isinstance(inherited_modules, dict) and inherited_modules
            and set(inherited_modules) <= set(modules)
            and all(modules[name] == origin for name, origin in inherited_modules.items()),
            "This release omitted inherited frozen wheel module coverage")
    portable = load(f"release_{LABEL}_portable_smoke.json")
    require_true(portable, ("clean_venv", "launcher_install", "http_ui", "real_analysis"), "fresh install")
    require(portable.get("archive") == "OceanRoute-0.11-portable.zip"
            and portable.get("http_health", {}).get("version") == VERSION
            and portable.get("versions", {}).get("oceanroute") == VERSION, "Fresh install archive/health/package version differs")
    fresh_seconds = passed_summary(portable.get("tests_output"), EXPECTED_BACKEND)
    verify_inherited_smoke(wheel_smoke, portable)
    wheel_requests = verify_side_smoke(wheel_smoke.get("side_slopes_and_kp_rules"), wheel=True)
    portable_requests = verify_side_smoke(portable.get("side_slopes_and_kp_rules"), wheel=False)
    automatic_requests = {"wheel_asgi_new_owner": verify_automatic_smoke(wheel_smoke.get("automatic_geographic_rules"), wheel=True, package=package),
                          "fresh_http_actual_server_restart": verify_automatic_smoke(portable.get("automatic_geographic_rules"), wheel=False, package=package)}

    arc_requests = {"wheel_asgi_new_owner": verify_altercourse_smoke(wheel_smoke.get("altercourse_geometry"), wheel=True, package=package),
                    "fresh_http_actual_server_restart": verify_altercourse_smoke(portable.get("altercourse_geometry"), wheel=False, package=package)}
    require(portable.get("versions", {}).get("geographiclib") == portable["altercourse_geometry"]["geographiclib_version"],
            "Installed GeographicLib version differs between actual metadata reports")

    served = load(f"release_{LABEL}_served_assets.json")
    require(served.get("version") == VERSION and served.get("status") == "passed"
            and served.get("health", {}).get("version") == VERSION, "Served frontend evidence version/status differs")
    assets = unique_rows(served.get("assets"), "served assets")
    require(assets and len(assets) == served.get("asset_count"), "Served asset count differs")
    browser_assets = unique_rows(browser.get("served_assets"), "browser-served assets")
    require(set(assets) == set(browser_assets) and all(row_identity(assets[name]) == row_identity(browser_assets[name]) for name in assets), "Current frontend differs from actual browser execution")
    directories = [relative_path(path) for path in served.get("equal_directories", [])]
    require(directories and ROOT / "oceanroute/static" in directories, "Packaged static directory missing from parity evidence")
    for directory in directories:
        actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        require(actual == set(assets), f"Frontend directory file set differs: {directory}")
        for row in assets.values():
            path = directory / row["path"]
            require(path.stat().st_size == row["bytes"] and file_sha(path) == row["sha256"], f"Frontend asset bytes differ: {path}")
    url = served.get("url", "")
    parsed = urllib.parse.urlparse(url)
    require(parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and not parsed.query and not parsed.fragment, "Preview verification must read the recorded loopback server")
    with urllib.request.urlopen(url + "/api/health", timeout=10) as response:
        require(parse_json(response.read()) == served["health"], "Current preview health differs")
    for row in assets.values():
        with urllib.request.urlopen(url + "/" + row["path"], timeout=10) as response:
            require(response.read() == (directories[0] / row["path"]).read_bytes(), f"Actual preview HTTP asset differs: {row['path']}")
    manual = (ROOT / "docs/USER_MANUAL.md").read_bytes()
    require(manual == (ROOT / "oceanroute/manual.md").read_bytes(), "Packaged manual differs from current source")
    with urllib.request.urlopen(url + "/api/manual", timeout=10) as response:
        require(parse_json(response.read())["text"].encode("utf-8") == manual, "Actually served manual differs")
    screenshots = verify_screenshots(browser)
    pages = verify_pdf()
    frozen = load(f"development_{LABEL}_frozen_artifacts.json")
    history = unique_rows(frozen.get("artifacts"), "frozen historical artifacts")
    require(frozen.get("version") == VERSION and frozen.get("artifact_count") == len(history) == 153,
            "0.11 historical baseline must contain exactly the 153 frozen artifacts")
    require("resources/validation/release_0.10_verified_runtime.json" in history,
            "The inherited wheel module coverage must itself be in the frozen historical baseline")
    for row in history.values():
        verify_row(row)

    evidence_names = [f"release_{LABEL}_{suffix}" for suffix in (
        "backend.json", "backend.log", "backend.xml", "backend_inputs.json",
        "browser.json", "browser.log", "browser_execution.json", "browser_inputs.json",
        "initial_archive.json", "verified_initial_archive.json", "initial_installation_failure.json",
        "wheel_smoke.json", "portable_smoke.json", "served_assets.json",
        "pdf_qa.json", "pdf_character_bounds.json", "screenshot_provenance.json", "screenshot_visual_review.json")]
    evidence_names.append(f"development_{LABEL}_frozen_artifacts.json")
    evidence_files = [{"path": (VALIDATION / name).relative_to(ROOT).as_posix(),
                       "bytes": (VALIDATION / name).stat().st_size, "sha256": file_sha(VALIDATION / name)} for name in evidence_names]
    report = {
        "version": VERSION, "status": "passed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Recorded execution/current-byte verification and actual loopback HTTP reads; no backend/browser/smoke/installation rerun. Selected ZIP check binds every recorded initial-install runtime input and all wheel package bytes; complete final ZIP member audit is separate. In verify-only mode the embedded runtime report must equal the unchanged current report byte for byte. No total ZIP SHA is embedded, avoiding recursive report/archive hashing.",
        "evidence_files": evidence_files, "input_verification": inputs,
        "initial_install_runtime_files": len(rows), "current_and_selected_archive_runtime_parity": True,
        "installation_candidates": {**candidate_evidence,
            "verified_runtime_files": len(rows),
            "launcher_installation_kind": "fresh_venv_editable_source"},
        "wheel": {"package_files": len(package), "record_entries": records, "bytes": initial["wheel"]["bytes"],
                  "sha256": initial["wheel"]["sha256"], "source_and_selected_archive_parity": True, "record_verified": True},
        "execution_evidence": {"backend_testcases": len(cases), "backend_reported_pytest_seconds": backend_seconds,
            "browser_tests_each_once_passed": len(tests), "browser_duration_s": raw["stats"]["duration"] / 1000,
            "byte_identical_archival_copies": True, "wheel_smoke_module_union": len(modules), "isolated_modules": modules,
            "clean_install_backend_tests": EXPECTED_BACKEND, "clean_install_reported_pytest_seconds": fresh_seconds,
            "side_slopes_and_kp_rules_actual_requests": {"wheel_asgi_new_owner": wheel_requests, "fresh_http_api_reread": portable_requests},
            "native_s57_actual_requests": {"wheel": 14, "clean_install": 11},
            "automatic_geographic_rules_actual_requests": automatic_requests,
            "altercourse_geometry_actual_requests": arc_requests,
            "inherited_isolated_module_coverage": len(inherited_modules),
            "counts_derived_from_actual_current_version_evidence": True},
        "actual_preview_http_asset_parity": {"url": url, "asset_count": len(assets), "directories": served["equal_directories"], "manual": True},
        "pdf_pages": pages, "pdf_sources_reviews_renders_builder_cover_match": True,
        "screenshots": screenshots, "historical_frozen_artifacts_unchanged": len(history),
        "limitations": "This script checks the recorded reports and current bytes rather than reexecuting their tests or visually inspecting new images. Existing-interpreter extracted-wheel smoke is distinct from the clean launcher editable-source installation. Intrinsic-arc/CSV/stock and owner-reopen checks are small synthetic workflows, not native-Makai interchange or field accuracy. Synthetic automatic-rule/side-slope/source checks and sampled KP rules do not certify continuous seabed, original-product equivalence, field precision or chart navigation. Automatic rules use actual geographic primitives and continuous distance bounds, but ambiguous/polar/tolerance cases remain unresolved and 2D seabed slopes remain sampled. Pure workspace/source admission precedes the checker budget."
    }
    if verify_only:
        saved_bytes = report_path.read_bytes()
        saved = parse_json(saved_bytes)
        require({key: value for key, value in saved.items() if key != "recorded_at_utc"}
                == {key: value for key, value in report.items() if key != "recorded_at_utc"}, "Existing runtime evidence differs from actual current checks")
        with zipfile.ZipFile(archive) as bundle:
            member = "OceanRoute/" + report_path.relative_to(ROOT).as_posix()
            require(member in bundle.namelist() and bundle.read(member) == saved_bytes,
                    "Selected final ZIP must contain the exact unchanged current runtime report")
    else:
        # Exclusive creation preserves older evidence even if another writer won
        # the race after the initial existence check. No report is written on an
        # earlier failed check; no self-referential archive digest is retained.
        with report_path.open("x", encoding="utf-8") as target:
            target.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))



def backend_runtime_paths() -> set[str]:
    paths = {p for p in (ROOT / "oceanroute").rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    paths.update((ROOT / "tests").rglob("*.py"))
    paths.update(p for p in (ROOT / "tests/fixtures").rglob("*") if p.is_file())
    paths.update(p for p in (ROOT / "examples").rglob("*") if p.is_file())
    paths.update(ROOT / name for name in ("pyproject.toml", "resources/research/s57_sources.json", "resources/research/arc_sources.json", "resources/run_0_11_backend_gate.py"))
    return {p.relative_to(ROOT).as_posix() for p in paths}


def browser_runtime_paths() -> set[str]:
    paths = set()
    for folder in ("web/src", "web/public", "web/tests", "web/dist-0.11-release", "tests/fixtures/s57", "examples"):
        paths.update(p for p in (ROOT / folder).rglob("*") if p.is_file())
    paths.update((ROOT / "oceanroute").glob("*.py"))
    paths.update(ROOT / name for name in ("pyproject.toml", "web/package.json", "web/package-lock.json", "web/tsconfig.json", "web/vite.config.ts", "web/playwright.config.ts", "web/index.html", "scripts/run_development_server.py", "docs/USER_MANUAL.md", "resources/research/s57_sources.json", "resources/research/arc_sources.json", "resources/run_0_11_browser_gate.py"))
    return {p.relative_to(ROOT).as_posix() for p in paths}


def require_full_gate_commands(backend: dict, browser: dict) -> None:
    command = backend.get("command", [])
    require(isinstance(command, list) and len(command) >= 8 and command[1:4] == ["-B", "-m", "pytest"], "Backend execution is not the declared full pytest invocation")
    # The full root runner has no positional selectors after its options.
    require(command[4:8] == ["-o", "addopts=", "-o", "faulthandler_timeout=30"]
            and command[8:9] == ["-q"] and len(command) == 10
            and command[9].startswith("--junitxml="), "Backend evidence is a selected subset or a different gate")
    browser_command = browser.get("command", [])
    require(isinstance(browser_command, list) and not any(isinstance(part, str) and (part.endswith(".spec.ts") or part.startswith("tests/")) for part in browser_command), "Browser evidence ran a selected file/subset")


def verify_altercourse_smoke(evidence: dict, *, wheel: bool, package: set[str]) -> int:
    """Validate actual synthetic arc workflow evidence without rerunning it."""
    require(isinstance(evidence, dict) and evidence.get("status") == "passed"
            and evidence.get("version") == VERSION, "Missing actual 0.11 altercourse smoke")
    require_true(evidence, ("synthetic", "source_project_unchanged",
                           "shared_fixed_inventory_and_associations_unchanged",
                           "preview_not_saved", "atomic_candidate_equal",
                           "second_get_is_post_save_reread", "api_saved_workspace_reread_equal",
                           "failed_shadow_does_not_save"), "altercourse_geometry")
    require(evidence.get("harness_sha256") == file_sha(ROOT / "scripts/altercourse_smoke.py"),
            "Actual altercourse smoke used different harness bytes")
    require(evidence.get("storage_owner_close_verified_by_harness") is False,
            "The shared harness must not claim that it closes its caller's owner")
    for key in ("source_project_sha256", "saved_workspace_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", evidence.get(key, "")) is not None,
                f"Missing full altercourse source/saved-workspace hash: {key}")
    require(evidence.get("tool_operations") == ["radius", "split"]
            and evidence.get("path_count") == 2 and evidence.get("assembly_count") == 1
            and evidence.get("saved_revision") == 2,
            "Actual two-tool, shared-inventory or revision evidence differs")
    calls = evidence.get("calls", [])
    expected = [("POST", "/api/analyze", 200),
                ("POST", "/api/tools/radius-altercourse", 200),
                ("POST", "/api/tools/split-altercourse", 200),
                ("POST", "/api/workspace/migrate", 200),
                ("POST", "/api/workspace/action", 200),
                ("POST", "/api/workspaces", 200),
                ("POST", "/api/workspace/altercourse-preview", 200),
                ("GET", None, 200), ("POST", "/api/workspace/action", 200),
                ("POST", "/api/workspaces", 200), ("GET", None, 200),
                ("POST", "/api/export/csv", 200), ("POST", "/api/import/rpl", 200),
                ("POST", "/api/analyze", 200), ("POST", "/api/workspace/migrate", 200),
                ("POST", "/api/workspace/action", 200),
                ("POST", "/api/workspace/altercourse-preview", 422), ("GET", None, 200)]
    require(isinstance(calls, list) and len(calls) == len(expected)
            and type(evidence.get("actual_api_requests")) is int
            and evidence["actual_api_requests"] == len(calls),
            "Altercourse workflow must contain all eighteen actual requests")
    saved_id = evidence.get("saved_workspace_id")
    require(isinstance(saved_id, str) and bool(saved_id), "Missing actual saved workspace identity")
    saved_path = "/api/workspaces/" + urllib.parse.quote(saved_id, safe="")
    for row, (method, path, status) in zip(calls, expected):
        require(isinstance(row, dict) and row.get("method") == method
                and row.get("path") == (path or saved_path) and row.get("http_status") == status
                and row.get("response_kind") == ("csv_attachment" if path == "/api/export/csv" else "json"),
                "Actual arc HTTP call order, status, saved identity or attachment kind differs")
    arc = evidence.get("intrinsic_arc", {})
    geometry = arc.get("geometry", {})
    require(isinstance(geometry, dict) and set(geometry) == {
                "type", "schema_version", "center", "radius_m", "start_azimuth_deg", "sweep_deg"}
            and geometry.get("type") == "circular_arc"
            and type(geometry.get("schema_version")) is int and geometry["schema_version"] == 1,
            "Actual CSV/route arc must retain the six-field geometry, not an outer segment descriptor")
    require(isinstance(geometry.get("center"), list) and len(geometry["center"]) == 2
            and all(type(value) in (int, float) and math.isfinite(value) for value in geometry["center"])
            and abs(geometry["center"][0]) <= 180 and abs(geometry["center"][1]) <= 90,
            "Actual circular center is not finite WGS84")
    for key in ("radius_m", "start_azimuth_deg", "sweep_deg"):
        require(type(geometry.get(key)) in (int, float) and math.isfinite(geometry[key]),
                "Actual intrinsic circle has a nonfinite parameter")
    require(abs(geometry["radius_m"] - 300.) <= 1e-8
            and abs(geometry["sweep_deg"] + 90.) <= 1e-5
            and type(arc.get("leg_index")) is int and arc["leg_index"] >= 0,
            "Declared synthetic 300m left-quarter-circle differs")
    surface, chord = arc.get("surface_length_m"), arc.get("chord_length_m")
    require(type(surface) in (int, float) and type(chord) in (int, float)
            and math.isfinite(surface) and math.isfinite(chord)
            and 470. < surface < 300. * math.pi / 2 + 1e-5 and 0 < chord < surface / 1.1,
            "Actual route surface length was replaced by the endpoint chord")
    require(type(arc.get("actual_rendered_circle_points")) is int
            and arc["actual_rendered_circle_points"] >= 5
            and arc.get("radius_check_basis") ==
                "independent WGS84 ECEF chord, explicit 300m domain; surface/chord difference below 0.1um",
            "Actual rendered circle evidence or independent radius-check scope differs")
    require(type(evidence.get("fixed_physical_stock_m")) in (int, float)
            and abs(evidence["fixed_physical_stock_m"] - 5300.) <= 1e-7,
            "Declared fixed 5300m manufacturing stock changed")
    exchange = evidence.get("csv", {})
    require(isinstance(exchange, dict) and exchange.get("arc_descriptor_equal") is True
            and str(exchange.get("content_type", "")).startswith("text/csv")
            and type(exchange.get("utf8_bytes")) is int and exchange["utf8_bytes"] > 0
            and re.fullmatch(r"[0-9a-f]{64}", exchange.get("sha256", "")) is not None
            and exchange.get("point_count") == 4
            and type(exchange.get("surface_length_m")) in (int, float)
            and math.isfinite(exchange["surface_length_m"]) and exchange["surface_length_m"] > surface
            and type(exchange.get("physical_stock_m")) in (int, float)
            and abs(exchange["physical_stock_m"] - 5300.) <= 1e-6
            and exchange.get("scope") ==
                "own open CSV route extension with explicit master context; not native constraint-history interchange",
            "Actual CSV six-field descriptor, physical-KP or exchange scope evidence differs")
    rejected = evidence.get("shared_flexible_rejection", {})
    require(rejected.get("http_status") == 422 and isinstance(rejected.get("error"), dict)
            and "WORKSPACE_SHARED_ASSEMBLY_CHANGED" in json.dumps(rejected["error"], allow_nan=False),
            "Shared Flexible stock change was not rejected by the actual workspace API")
    if wheel:
        required = {"oceanroute", "oceanroute.api", "oceanroute.core", "oceanroute.geodesy",
                    "oceanroute.route_geometry", "oceanroute.altercourse", "oceanroute.altercourse_workspace",
                    "oceanroute.constraints", "oceanroute.tools", "oceanroute.exchange", "oceanroute.rpl_templates",
                    "oceanroute.workspace", "oceanroute.workspace_storage", "oceanroute.storage",
                    "oceanroute.sqlite_lifecycle"}
        origins, digests = evidence.get("isolated_modules", {}), evidence.get("isolated_module_sha256", {})
        require(set(origins) == set(digests) == required,
                "New extracted-wheel origin/hash evidence omitted actual arc consumers")
        for name, origin in origins.items():
            expected_path = "oceanroute/__init__.py" if name == "oceanroute" else name.replace(".", "/") + ".py"
            require(origin == expected_path and origin in package and digests[name] == file_sha(ROOT / origin),
                    "Actual arc module origin or bytes differ from the validated wheel package")
        require_true(evidence, ("child_outside_checkout_verified", "actual_asgi_owner_closed_and_reopened"),
                     "altercourse_geometry.wheel")
        require(evidence.get("actual_read_count") == 3 and evidence.get("harness_path") == "harnesses/altercourse_smoke.py"
                and evidence.get("harness_copy_sha256") == evidence["harness_sha256"]
                and evidence.get("owner_lifecycle_events") == [
                    {"event": "client_enter", "owner": 1},
                    {"event": "client_exit", "owner": 1, "before_get_number": 2},
                    {"event": "client_enter", "owner": 2, "before_get_number": 2},
                    {"event": "client_exit", "owner": 2}],
                "The new wheel workflow did not actually close and recreate its owner before saved reread")
    else:
        required = {"oceanroute.altercourse", "oceanroute.altercourse_workspace", "oceanroute.route_geometry",
                    "oceanroute.rpl_templates", "oceanroute.exchange", "oceanroute.geodesy", "oceanroute.gis",
                    "oceanroute.survey", "oceanroute.shipplan", "oceanroute.plan_voyage", "oceanroute.routing",
                    "oceanroute.terrain_bathymetry", "oceanroute.terrain_slice", "oceanroute.side_slopes",
                    "oceanroute.slope_rules"}
        verify_editable_arc_owner(evidence, required, package)
    return len(calls)


def verify_editable_arc_owner(evidence: dict, required: set[str], package: set[str]) -> None:
    require(evidence.get("actual_http_server_owner_closed_and_restarted") is True,
            "Fresh arc workflow did not actually stop and restart its HTTP server before saved reread")
    pids = evidence.get("launcher_process_ids", [])
    require(len(pids) == 2 and all(type(pid) is int and pid > 0 for pid in pids) and len(set(pids)) == 2,
            "Fresh arc workflow lacks two distinct actual launcher owners")
    require(evidence.get("installation_kind") == "fresh_venv_editable_source",
            "Fresh launcher source installation must not be described as wheel installation")
    geographiclib_version = evidence.get("geographiclib_version", "")
    require(isinstance(geographiclib_version, str) and re.fullmatch(r"2(?:\.\d+)+", geographiclib_version),
            "Missing actual installed GeographicLib 2.x metadata")
    installation = evidence.get("launcher_installation", {})
    source_root = Path(installation.get("resolved_checkout_path", "")).resolve()
    environment_root = Path(installation.get("resolved_environment_path", "")).resolve()
    require(source_root != ROOT and not source_root.is_relative_to(ROOT)
            and source_root.name == "OceanRoute" and environment_root == source_root / ".venv"
            and all(Path(installation.get(key, "")).resolve() == expected for key, expected in (
                ("checkout_path", source_root), ("environment_path", environment_root),
                ("sys_prefix", environment_root), ("resolved_sys_prefix", environment_root))),
            "Fresh arc editable environment or outside-checkout source root differs")
    metadata = installation.get("metadata", {})
    dist_info = Path(metadata.get("dist_info_path", "")).resolve()
    require(isinstance(metadata.get("name"), str) and metadata["name"].lower() == "oceanroute"
            and metadata.get("version") == VERSION and dist_info.is_relative_to(environment_root)
            and dist_info.name.endswith(".dist-info") and type(metadata.get("metadata_bytes")) is int
            and metadata["metadata_bytes"] > 0
            and re.fullmatch(r"[0-9a-f]{64}", metadata.get("metadata_sha256", "")) is not None,
            "Actual new-venv editable dist-info metadata is missing or differs")
    direct_url = metadata.get("direct_url", {})
    parsed = urllib.parse.urlsplit(direct_url.get("url", ""))
    require(direct_url.get("dir_info", {}).get("editable") is True and parsed.scheme == "file"
            and parsed.netloc in {"", "localhost"} and not parsed.query and not parsed.fragment
            and Path(urllib.request.url2pathname(parsed.path)).resolve() == source_root
            and Path(installation.get("resolved_direct_url_source_root", "")).resolve() == source_root,
            "Actual arc editable registration does not bind the extracted source")
    modules = evidence.get("launcher_installed_modules_before_test_dependencies", {})
    require(isinstance(modules, dict) and set(modules) == required,
            "Fresh arc consumer module evidence is incomplete")
    for name, row in modules.items():
        suffix = name.replace(".", "/") + ".py"
        require(suffix in package and Path(row.get("path", "")).resolve() == (source_root / suffix).resolve()
                and row.get("bytes") == (ROOT / suffix).stat().st_size
                and row.get("sha256") == file_sha(ROOT / suffix),
                "Fresh editable arc consumer differs from extracted source/current/wheel package bytes")


def verify_automatic_smoke(evidence: dict, *, wheel: bool, package: set[str]) -> int:
    require(isinstance(evidence, dict) and evidence.get("status") == "passed"
            and evidence.get("version") == VERSION, "Missing actual current-version automatic-rule smoke")
    require_true(evidence, ("synthetic", "typed_native_ids_and_source_indexes_distinct", "nullable_rule_ends_preserved", "preview_not_persisted", "open_package_exact_rules_and_recheck", "atomic_complete_candidate_applied", "path_assembly_inventory_associations_unchanged", "api_saved_workspace_reread_equal", "actual_queried_witness_and_unqueried_centroid_distinct", "queried_triangle_gradient_independently_reconstructed", "radius_center_is_actual_body_location", "whole_budget_rejection_does_not_save"), "automatic_geographic_rules")
    require(evidence.get("harness_sha256") == file_sha(ROOT / "scripts/automatic_rules_smoke.py"), "Actual automatic smoke used different helper bytes")
    calls = evidence.get("calls", [])
    require(isinstance(calls, list) and type(evidence.get("actual_api_requests")) is int and evidence["actual_api_requests"] == len(calls), "Actual automatic request count/list differs")
    expected = [("POST", "/api/workspace/migrate", 200), ("POST", "/api/workspace/action", 200), ("POST", "/api/workspaces", 200), ("POST", "/api/automatic-rules/catalog", 200), ("POST", "/api/automatic-rules/check", 200), ("GET", None, 200), ("POST", "/api/automatic-rules/export", 200), ("POST", "/api/automatic-rules/import", 200), ("POST", "/api/workspace/action", 200), ("POST", "/api/workspaces", 200), ("GET", None, 200), ("POST", "/api/automatic-rules/check", 200), ("POST", "/api/automatic-rules/check", 422), ("GET", None, 200), ("POST", "/api/analyze", 200)]
    require(len(calls) == len(expected), "Automatic helper workflow is incomplete")
    for actual, (method, path, status) in zip(calls, expected):
        require(actual.get("method") == method and actual.get("http_status") == status
                and (actual.get("path") == path if path else actual.get("path") == "/api/workspaces/" + urllib.parse.quote(evidence.get("saved_workspace_id", ""), safe="")), "Automatic actual request order/status differs")
    require(evidence.get("checker_model") == "automatic-geographic-rules-v1"
            and evidence.get("rule_kinds_exercised") == ["crossing", "proximity", "slope"]
            and evidence.get("rule_count") == 6 and evidence.get("missing_reference_status") == "reference_error"
            and evidence.get("saved_revision") == 2 and evidence.get("path_count") == 2 and evidence.get("assembly_count") == 1
            and evidence.get("body_slope_query_count") == 321 and evidence.get("body_slope_triangle_violations") == 600
            and abs(evidence.get("body_slope_deg", 0)-math.degrees(math.atan(math.sqrt(.05)))) <= 1e-6
            and evidence.get("continuous_bed_verified") is False and evidence.get("whole_budget_http_status") == 422
            and abs(evidence.get("geodesic_common_vertex_turn_deg", 1)) <= 1e-8
            and evidence.get("storage_owner_close_verified_by_harness") is False,
            "Actual automatic geometry, sampled terrain or durable candidate evidence differs")
    require(re.fullmatch(r"[0-9a-f]{64}", evidence.get("saved_workspace_sha256", "")) is not None, "Missing exact saved full-workspace hash")
    required = {"oceanroute", "oceanroute.api", "oceanroute.core", "oceanroute.workspace", "oceanroute.workspace_storage", "oceanroute.sqlite_lifecycle", "oceanroute.automatic_rules", "oceanroute.automatic_rule_geometry", "oceanroute.terrain_slope_neighborhoods"}
    if wheel:
        require(evidence.get("actual_asgi_owner_closed_and_reopened") is True
                and required <= set(evidence.get("isolated_modules", {})), "Wheel automatic smoke did not actually reopen storage or import required wheel modules")
    else:
        require(evidence.get("actual_http_server_owner_closed_and_restarted") is True, "Fresh installed server did not actually restart before saved reread")
        pids = evidence.get("launcher_process_ids", [])
        require(len(pids) == 2 and all(type(pid) is int and pid > 0 for pid in pids) and len(set(pids)) == 2, "Fresh HTTP restart lacks two distinct actual launcher owners")
        require(evidence.get("installation_kind") == "fresh_venv_editable_source",
                "Fresh launcher installation must not be mislabelled as wheel installation")
        installation = evidence.get("launcher_installation", {})
        source_root = Path(installation.get("resolved_checkout_path", "")).resolve()
        environment_root = Path(installation.get("resolved_environment_path", "")).resolve()
        require(source_root.name == "OceanRoute" and environment_root == source_root / ".venv"
                and Path(installation.get("checkout_path", "")).resolve() == source_root
                and Path(installation.get("environment_path", "")).resolve() == environment_root
                and Path(installation.get("sys_prefix", "")).resolve() == environment_root
                and Path(installation.get("resolved_sys_prefix", "")).resolve() == environment_root,
                "Fresh editable metadata/environment/source roots differ")
        metadata = installation.get("metadata", {})
        dist_info = Path(metadata.get("dist_info_path", "")).resolve()
        require(isinstance(metadata.get("name"), str) and metadata["name"].lower() == "oceanroute"
                and metadata.get("version") == VERSION and dist_info.is_relative_to(environment_root)
                and dist_info.name.endswith(".dist-info")
                and type(metadata.get("metadata_bytes")) is int and metadata["metadata_bytes"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", metadata.get("metadata_sha256", "")) is not None,
                "Missing actual fresh-venv OceanRoute dist-info metadata")
        direct_url = metadata.get("direct_url", {})
        parsed = urllib.parse.urlsplit(direct_url.get("url", ""))
        require(direct_url.get("dir_info", {}).get("editable") is True
                and parsed.scheme == "file" and parsed.netloc in {"", "localhost"}
                and not parsed.query and not parsed.fragment
                and Path(urllib.request.url2pathname(parsed.path)).resolve() == source_root
                and Path(installation.get("resolved_direct_url_source_root", "")).resolve() == source_root,
                "Actual editable registration does not refer to the extracted source checkout")
        modules = evidence.get("launcher_installed_modules_before_test_dependencies", {})
        require(set(modules) == required, "Fresh editable source module evidence is incomplete")
        for name, row in modules.items():
            suffix = "oceanroute/__init__.py" if name == "oceanroute" else name.replace(".", "/") + ".py"
            location = Path(row.get("path", "")).resolve()
            require(suffix in package and location == (source_root / suffix).resolve()
                    and location.is_relative_to(source_root / "oceanroute"),
                    "Fresh editable module differs from its declared extracted source path")
            source = ROOT / suffix
            require(row.get("bytes") == source.stat().st_size and row.get("sha256") == file_sha(source),
                    "Fresh launcher-installed source module differs from current/archive/wheel package bytes")
    return len(calls)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "outputs/releases/OceanRoute-0.11-portable.zip")
    parser.add_argument("--verify-only", action="store_true", help="Read and compare existing evidence without writing any file")
    options = parser.parse_args()
    verify(options.archive, verify_only=options.verify_only)
