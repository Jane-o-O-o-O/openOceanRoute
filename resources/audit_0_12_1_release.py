"""Independent 0.12.1 source-ZIP/wheel/EXE/evidence audit; external tool, not an installer test.

Default mode writes a new audit report exactly once. Refresh the source archive
to include that report, then use --verify-only for the final read-only check.
The report enumerates every other member and never records its own hash or the
final ZIP hash. Final archive identity is printed for an external manifest.
"""
from __future__ import annotations

import argparse
import ast
import base64
import csv
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.12.1"
LABEL = "0.12.1"
FRONTEND = "web/dist-0.12.1-release"
FROZEN = "resources/validation/development_0.12_frozen_artifacts.json"
INITIAL = "resources/validation/release_0.12.1_verified_initial_archive.json"

# Independently reproduce build_release.py's 0.12 roots/files contract. Do not
# import or run the builder: neither wheel nor source/static/manual is mutated.
RELEASE_ROOTS = (
    "oceanroute", "docs", "examples", "tests", "scripts", "web/src",
    "web/public", "web/tests", "web/artifacts/release-0.12.1",
    "resources/validation", FRONTEND,
)
RELEASE_FILES = (
    "README.md", "pyproject.toml", "launcher.py", "web/package.json",
    "web/package-lock.json", "web/index.html", "web/tsconfig.json",
    "web/vite.config.ts", "web/playwright.config.ts",
    "output/pdf/OceanRoute_用户手册_0.12.1.pdf",
    "output/pdf/OceanRoute_设计文档_0.12.1.pdf",
    "resources/research/manual_findings.md",
    "resources/research/website_findings.md", "resources/research/web_sources.json",
    "resources/research/s57_sources.json",
    "resources/run_0_9_backend_gate.py", "resources/run_0_9_browser_gate.py",
    "resources/run_0_9_ui_build.py", "resources/validate_0_9_pdf_structure.py",
    "resources/run_0_10_backend_gate.py", "resources/run_0_10_browser_gate.py",
    "resources/validate_0_10_pdf_structure.py",
    "resources/documentation_0_10_automatic_rules_contract.md",
    "resources/run_0_11_backend_gate.py", "resources/run_0_11_browser_gate.py",
    "resources/validate_0_11_pdf_structure.py", "resources/research/arc_sources.json",
    "resources/run_0_12_backend_gate.py", "resources/run_0_12_browser_gate.py",
    "resources/run_0_12_ui_build.py", "resources/validate_0_12_pdf_structure.py",
    "resources/run_0_12_arc_geometry_probe.py",
    "resources/run_0_12_arc_edit_consumer_probe.py",
    "resources/run_0_12_1_backend_gate.py", "resources/run_0_12_1_browser_gate.py",
    "resources/run_0_12_1_ui_build.py", "resources/validate_0_12_1_pdf_structure.py",
    "resources/run_0_12_1_arc_geometry_probe.py",
    "resources/run_0_12_1_arc_edit_consumer_probe.py",
    "resources/audit_0_12_1_release.py",
    "packaging/windows/windows_app_0_12_1.py",
    "packaging/windows/launcher_0_12_1.nsi", "packaging/windows/installer_0_12_1.nsi",
    "packaging/windows/build_windows_0_12_1.py",
    "packaging/windows/uninstall-files-0.12.1.nsh", "packaging/windows/upgrade-owned-0.12.nsh",
    "packaging/windows/validate_installer_0_12_1.py",
    "packaging/windows/windows_uninstall_wait_0_12_1.py",
    "packaging/windows/windows_backend_probe_0_12_1.py",
    "packaging/windows/windows_backend_probe_0_12_1_final.py",
    "packaging/windows/windows_backend_probe_0_12_1_final_2.py",
    "packaging/windows/requirements-frozen.txt",
    "resources/build_product_documents.py", "resources/validation/voyage_1800s.json",
)
RUNNERS_0_12_1 = tuple(name for name in RELEASE_FILES if name.startswith("resources/")
                     and ("run_0_12_1_" in name or "validate_0_12_1_" in name))


def require(condition: bool, message: str) -> None:
    """Do not let python -O disable audit guards."""
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def descriptor(path: Path) -> dict:
    data = path.read_bytes()
    return {"path": path.resolve().relative_to(ROOT).as_posix(),
            "bytes": len(data), "sha256": digest(data)}


def source_path(name: str) -> Path:
    path = PurePosixPath(name)
    require(path.as_posix() == name and not path.is_absolute() and ".." not in path.parts
            and "\\" not in name and ":" not in name, f"Unsafe source path: {name}")
    result = ROOT.joinpath(*path.parts)
    require(result.resolve().is_relative_to(ROOT), f"Source escapes workspace: {name}")
    require(not any((ROOT.joinpath(*path.parts[:i])).is_symlink()
                    for i in range(1, len(path.parts) + 1)), f"Source symlink: {name}")
    require(result.is_file(), f"Required source file missing: {name}")
    return result


def files_under(folder: str) -> dict[str, Path]:
    base = ROOT / folder
    require(base.is_dir(), f"Required release directory missing: {folder}")
    return {p.relative_to(ROOT).as_posix(): source_path(p.relative_to(ROOT).as_posix())
            for p in base.rglob("*") if p.is_file()
            and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def required_sources() -> dict[str, Path]:
    paths = {name: source_path(name) for name in RELEASE_FILES}
    for folder in RELEASE_ROOTS:
        paths.update(files_under(folder))
    return paths


def verify_builder_contract() -> None:
    """Interpret only the builder's lists/version branches; never execute it."""
    module = ast.parse(source_path("scripts/build_release.py").read_text(encoding="utf-8"))
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "build")
    values = {"documents": [f"output/pdf/OceanRoute_用户手册_{LABEL}.pdf",
                            f"output/pdf/OceanRoute_设计文档_{LABEL}.pdf"], "label": LABEL}
    version_expression = ast.dump(ast.parse('tuple(map(int, version.split(".")))', mode="eval").body)
    dist_expression = ast.dump(ast.parse('dist.relative_to(ROOT).as_posix()', mode="eval").body)

    def value(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [value(item) for item in node.elts]
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.JoinedStr):
            return "".join(str(value(item)) for item in node.values)
        if isinstance(node, ast.FormattedValue):
            require(node.conversion == -1 and node.format_spec is None, "Unexpected builder format operation")
            return value(node.value)
        if ast.dump(node) == dist_expression:
            return FRONTEND
        raise ValueError("Unexpected expression in builder file-list contract")

    def interpret(nodes):
        for node in nodes:
            if isinstance(node, ast.Assign):
                require(len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id in {"roots", "files"}, "Unexpected builder contract assignment")
                values[node.targets[0].id] = value(node.value)
            elif isinstance(node, ast.AugAssign):
                require(isinstance(node.target, ast.Name) and node.target.id == "files"
                        and isinstance(node.op, ast.Add), "Unexpected builder contract list mutation")
                values["files"] += value(node.value)
            elif isinstance(node, ast.If):
                test = node.test
                require(isinstance(test, ast.Compare) and ast.dump(test.left) == version_expression
                        and len(test.ops) == len(test.comparators) == 1 and isinstance(test.ops[0], ast.GtE),
                        "Unexpected builder version condition")
                threshold = tuple(value(test.comparators[0]))
                interpret(node.body if tuple(map(int, VERSION.split("."))) >= threshold else node.orelse)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                require(isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "roots" and call.func.attr == "append"
                        and len(call.args) == 1 and not call.keywords, "Unexpected builder root mutation")
                values["roots"].append(value(call.args[0]))
            else:
                raise ValueError("Unexpected statement in builder list contract")
    start = next(i for i, n in enumerate(function.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "roots" for t in n.targets))
    end = next(i for i, n in enumerate(function.body[start:], start) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "paths" for t in n.targets))
    interpret(function.body[start:end])
    require(tuple(values["roots"]) == RELEASE_ROOTS and tuple(values["files"]) == RELEASE_FILES,
            "Independent auditor roots/files differ from actual builder AST contract")


def check_zip_structure(package: zipfile.ZipFile, *, source_archive: bool) -> list[str]:
    names = package.namelist()
    require(len(names) == len(set(names)), "Duplicate archive member")
    require(package.testzip() is None, "Archive CRC failure")
    banned_parts = {".venv", "node_modules", "__pycache__", ".git"}
    banned_pdfs = {"MakaiPlan_Manual_6.2.0.pdf", "MakaiPlan.pdf", "MakaiPlanPro.pdf"}
    for info in package.infolist():
        require(isinstance(info.orig_filename, str) and info.orig_filename == info.filename
                and not any(ord(c) < 32 or ord(c) == 127 for c in info.orig_filename),
                f"Raw archive filename contains truncation/control ambiguity: {info.orig_filename!r}")
        path = PurePosixPath(info.filename)
        require(path.as_posix() == info.filename and not path.is_absolute()
                and ".." not in path.parts and "\\" not in info.filename
                and ":" not in info.filename, f"Unsafe member name: {info.filename}")
        require(bool(path.parts) and not info.is_dir(), f"Directory member: {info.filename}")
        mode = info.external_attr >> 16
        require(not stat.S_ISLNK(mode) and stat.S_IFMT(mode) in (0, stat.S_IFREG),
                f"Nonregular/symlink archive member: {info.filename}")
        require(not (info.flag_bits & 1), f"Encrypted archive member: {info.filename}")
        require(not any(part in banned_parts or part.startswith(".oceanroute")
                        for part in path.parts), f"Developer/user data path: {info.filename}")
        require(path.suffix.lower() not in {".sqlite3", ".db", ".pyc"},
                f"Developer/user database/cache: {info.filename}")
        require(path.name not in banned_pdfs, f"Original proprietary PDF: {info.filename}")
        if source_archive:
            require(path.parts[0] == "OceanRoute" and len(path.parts) > 1,
                    f"Wrong source archive root: {info.filename}")
            if len(path.parts) >= 3 and path.parts[1:3] == ("output", "pdf"):
                require("/".join(path.parts[1:]) in RELEASE_FILES,
                        f"Unexpected historical/product PDF: {info.filename}")
    return names


def verify_historical(frozen: Path) -> dict:
    manifests = []
    all_paths = set()
    for path, expected_count in ((frozen, 190), (source_path(
            "resources/validation/development_0.12_pre_windows_compatibility_freeze.json"), 95)):
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document["artifacts"]
        require(len(rows) == expected_count and document.get("artifact_count", len(rows)) == len(rows),
                f"Wrong frozen artifact count: {path.name}")
        require(len({r["path"] for r in rows}) == len(rows), "Duplicate historical artifact path")
        for row in rows:
            data = source_path(row["path"]).read_bytes()
            require(len(data) == row["bytes"] and digest(data) == row["sha256"],
                    f"Historical artifact changed: {row['path']}")
            all_paths.add(row["path"])
        manifests.append({"manifest": descriptor(path), "count": len(rows), "unchanged": True})
    return {"manifests": manifests, "count": sum(r["count"] for r in manifests),
            "unique_artifact_paths": len(all_paths), "unchanged": True}


def verify_initial_inputs() -> tuple[dict, list[dict]]:
    path = source_path(INITIAL)
    initial = json.loads(path.read_text(encoding="utf-8"))
    rows = initial["runtime_files"]
    require(initial["version"] == VERSION and initial["runtime_files_count"] == len(rows)
            and len(rows) > 0, "Initial 0.12.1 installation snapshot needs its actual nonempty runtime-input set")
    require(len({r["path"] for r in rows}) == len(rows), "Duplicate initial runtime path")
    for row in rows:
        data = source_path(row["path"]).read_bytes()
        require(len(data) == row["bytes"] and digest(data) == row["sha256"],
                f"Verified initial installation input changed: {row['path']}")
    preserved = initial["preserved_initial_archive"]
    preserved_path = source_path(preserved["path"])
    require(descriptor(preserved_path) == preserved, "Preserved initial ZIP descriptor changed")
    with zipfile.ZipFile(preserved_path) as old:
        check_zip_structure(old, source_archive=True)
        for row in rows:
            data = old.read("OceanRoute/" + row["path"])
            require(len(data) == row["bytes"] and digest(data) == row["sha256"],
                    f"Initial ZIP runtime input differs: {row['path']}")
    return {"snapshot": descriptor(path), "runtime_files": len(rows),
            "unchanged": True, "preserved_initial_archive": preserved,
            "scope": "Byte identity only; actual installation execution is separate evidence."}, rows


def verify_versions() -> None:
    for name, pattern in (
        ("pyproject.toml", r'^version = "([^"\n]+)"$'),
        ("oceanroute/__init__.py", r'^__version__ = "([^"\n]+)"$'),
    ):
        match = re.search(pattern, source_path(name).read_text(encoding="utf-8"), re.M)
        require(match is not None and match.group(1) == VERSION, f"Wrong release version: {name}")
    require(json.loads(source_path("web/package.json").read_text())["version"] == VERSION,
            "Wrong frontend package version")
    require(source_path("docs/USER_MANUAL.md").read_bytes()
            == source_path("oceanroute/manual.md").read_bytes(), "Packaged manual differs from documentation")


def verify_frontend() -> list[dict]:
    frontend = files_under(FRONTEND)
    static = files_under("oceanroute/static")
    require(len(frontend) == 8, "0.12.1 reviewed frontend must contain exactly 8 files")
    left = {str(Path(name).relative_to(FRONTEND)): p for name, p in frontend.items()}
    right = {str(Path(name).relative_to("oceanroute/static")): p for name, p in static.items()}
    require(set(left) == set(right), "Reviewed frontend/package static file set differs")
    for name in left:
        require(left[name].read_bytes() == right[name].read_bytes(),
                f"Reviewed frontend differs from packaged static: {name}")
    return [descriptor(frontend[name]) for name in sorted(frontend)]


def verify_wheel(wheel: Path, bundle: zipfile.ZipFile) -> dict:
    expected = files_under("oceanroute")
    with zipfile.ZipFile(wheel) as package:
        names = check_zip_structure(package, source_archive=False)
        actual = {n for n in names if n.startswith("oceanroute/")}
        require(actual == set(expected), "Wheel package member set differs from current source/static/manual")
        for name, source in expected.items():
            data = package.read(name)
            require(data == source.read_bytes() == bundle.read("OceanRoute/" + name),
                    f"Wheel/ZIP/source package bytes differ: {name}")
        dist_infos = {PurePosixPath(n).parts[0] for n in names if ".dist-info/" in n}
        require(len(dist_infos) == 1, "Wheel must contain exactly one dist-info directory")
        dist_info = next(iter(dist_infos))
        require(dist_info == "oceanroute-0.12.1.dist-info", "Wrong wheel dist-info directory")
        require(all(n in actual or n.startswith(dist_info + "/") for n in names),
                "Unexpected non-package/non-dist-info wheel member")
        metadata_name = dist_info + "/METADATA"
        metadata = Parser().parsestr(package.read(metadata_name).decode("utf-8"))
        require(metadata["Name"].lower() == "oceanroute" and metadata["Version"] == VERSION,
                "Wrong wheel METADATA name/version")
        record_name = dist_info + "/RECORD"
        rows = list(csv.reader(io.StringIO(package.read(record_name).decode("utf-8"))))
        require(all(len(row) == 3 for row in rows), "Malformed wheel RECORD row")
        require(len(rows) == len(names) and len({r[0] for r in rows}) == len(rows)
                and {r[0] for r in rows} == set(names), "Wheel RECORD/member set mismatch")
        for name, encoded_hash, encoded_size in rows:
            if name == record_name:
                require(encoded_hash == encoded_size == "", "RECORD self row must omit digest/size")
                continue
            data = package.read(name)
            expected_hash = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            require(encoded_hash == "sha256=" + expected_hash and encoded_size == str(len(data)),
                    f"Wheel RECORD digest/size mismatch: {name}")
    return {"artifact": descriptor(wheel), "members": len(names), "package_files": len(actual),
            "record_rows": len(rows), "record_verified": True, "zip_wheel_source_parity": True}


def verify_artifact_row(row: dict) -> Path:
    require(isinstance(row, dict) and {"path", "bytes", "sha256"} <= set(row),
            "Artifact evidence needs an actual path/bytes/SHA256 descriptor")
    path = source_path(row["path"])
    actual = descriptor(path)
    require(all(actual[k] == row[k] for k in ("path", "bytes", "sha256")),
            f"Evidence artifact changed: {row['path']}")
    return path


def passed_evidence(path: Path) -> dict:
    path = source_path(path.resolve().relative_to(ROOT).as_posix())
    require(path.is_relative_to(ROOT / "resources/validation"), "Execution evidence must be a saved validation record")
    document = json.loads(path.read_text(encoding="utf-8"))
    require(document.get("version") == VERSION and document.get("status") == "passed",
            f"Missing passing {VERSION} evidence: {path.name}")
    return document


def verify_windows_source_snapshot(execution: dict) -> dict:
    snapshot = execution["source_snapshot"]
    require(snapshot.get("kind") == "independent_local_byte_copy_not_source_zip"
            and snapshot.get("current_source_after_unchanged") is True,
            "Windows source evidence is not the actual unchanged independent byte copy")
    files = snapshot.get("files")
    require(isinstance(files, dict) and files and snapshot.get("file_count") == len(files),
            "Windows source snapshot file count differs")
    require(files == execution["before_inputs"]["complete_qa_source_snapshot"],
            "Windows independent copy differs from the full before-input snapshot")
    base = snapshot["path"]
    changes = []
    allowed_post_execution_changes = {"scripts/build_release.py", "scripts/verify_0_12_1_runtime.py"}
    for name, row in files.items():
        # Resolve both the independent copy and its original source. No implied
        # source ZIP, absent manifest, or unrecorded directory is manufactured.
        copy = descriptor(source_path(base + "/" + name))
        require(copy["bytes"] == row["bytes"] and copy["sha256"] == row["sha256"],
                f"Windows independent supplied source copy changed: {name}")
        current = descriptor(source_path(name))
        if current["bytes"] != row["bytes"] or current["sha256"] != row["sha256"]:
            require(name in allowed_post_execution_changes,
                    f"Windows-tested product/test or unapproved source changed: {name}")
            changes.append({"path": name, "qa_original": {"path": name, **row}, "qa_copy": copy,
                            "current": current,
                            "scope": "Explicit post-Windows-execution packaging/audit infrastructure change; "
                                     "not a product/test change or a claim that this current script ran in that QA execution."})
    if "manifest" in snapshot:
        verify_artifact_row(snapshot["manifest"])
    result = {"kind": snapshot["kind"], "path": base, "file_count": len(files),
            "snapshot_files_sha256": digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()),
            "actual_independent_copy_verified": True, "current_product_and_tests_unchanged": True,
            "all_qa_files_equal_current": not changes,
            "explicit_post_execution_packaging_audit_changes": changes}
    if "manifest" in snapshot:
        result["manifest"] = snapshot["manifest"]
    return result


def verify_installed_distributions(rows: list[dict], count: int) -> None:
    require(isinstance(rows, list) and len(rows) == count == 32,
            "Windows application must retain exactly the actual 32 production distributions")
    names = [row["name"].lower().replace("_", "-") for row in rows]
    require(len(set(names)) == len(names) and names.count("oceanroute") == 1,
            "Duplicate/absent installed production distribution")
    for row in rows:
        path = verify_artifact_row(row["metadata"])
        metadata = Parser().parsestr(path.read_text(encoding="utf-8"))
        require(metadata["Name"].lower().replace("_", "-") == row["name"].lower().replace("_", "-")
                and metadata["Version"] == row["version"], "Installed metadata differs from recorded distribution")
        if row["name"].lower() == "oceanroute":
            require(row["version"] == VERSION, "Installed application metadata has an old version")


def verify_uninstall_process(execution: dict) -> dict:
    raw = execution["actual_uninstall_process"]
    require(raw.get("version") == VERSION and raw.get("status") == "passed"
            and isinstance(raw.get("actual_windows_pid"), int) and raw["actual_windows_pid"] > 0
            and raw.get("actual_exit_code") == 0 and raw.get("actual_process_exited") is True
            and raw.get("byte_identical_copy") is True
            and raw.get("self_copy_disabled_by_final_inst_dir_argument") is True
            and "subprocess.Popen.wait" in raw.get("wait_method", ""),
            "Uninstall evidence must wait for the actual Windows process handle, not file disappearance")
    report_path = verify_artifact_row(execution["actual_uninstall_process_report"])
    require(json.loads(report_path.read_text(encoding="utf-8")) == raw,
            "Uninstall embedded process record differs from its actual saved report")
    verify_artifact_row(execution["uninstall_helper"])
    verify_artifact_row(execution["external_windows_python"])
    return {"process": raw, "report": execution["actual_uninstall_process_report"],
            "helper": execution["uninstall_helper"]}


def verify_runtime_record(runtime_report: Path, wheel: Path) -> dict:
    """Reject a generic passed record substituted for the actual runtime gate."""
    runtime = passed_evidence(runtime_report)
    require(runtime.get("backend_report") == "release_0.12.1_backend_final_3.json"
            and runtime.get("backend_tests") == 2232 and runtime.get("browser_tests") == 116
            and runtime.get("health", {}).get("version") == VERSION,
            "Runtime evidence must bind the actual final Mac/browser/served-version gates")
    require(runtime.get("windows_counts") == {"tests": 2232, "failures": 0, "errors": 0,
                                              "skipped": 1, "passed": 2231},
            "Runtime evidence must bind the completed Windows PE count, including its one POSIX skip")
    expected_reports = {
        "release_0.12.1_backend_final_3.json", "release_0.12.1_backend_final_3.xml",
        "release_0.12.1_browser.json", "release_0.12.1_browser_execution.json",
        "release_0.12.1_windows_backend_final_2_execution.json",
        "release_0.12.1_windows_installer_execution.json", "release_0.12.1_windows_build_final.json",
        "release_0.12.1_wheel_smoke.json", "release_0.12.1_portable_smoke.json",
        "release_0.12.1_pdf_qa.json", "release_0.12.1_visual_review.json",
    }
    bound = runtime.get("bound_reports")
    require(isinstance(bound, list) and len(bound) == len(expected_reports)
            and {r["path"] for r in bound} == {"resources/validation/" + n for n in expected_reports},
            "Runtime gate does not bind the actual complete final report set")
    for row in bound:
        verify_artifact_row(row)
    rows = runtime.get("runtime_files")
    require(isinstance(rows, list) and rows and runtime.get("runtime_files_count") == len(rows)
            and len({r["path"] for r in rows}) == len(rows), "Runtime file evidence is missing or duplicated")
    for row in rows:
        verify_artifact_row(row)
    initial = json.loads(source_path(INITIAL).read_text(encoding="utf-8"))
    require(sorted(rows, key=lambda r: r["path"]) == sorted(initial["runtime_files"], key=lambda r: r["path"]),
            "Runtime gate and verified installation candidate do not bind the same current inputs")
    require(runtime.get("wheel") == descriptor(wheel), "Runtime gate did not bind this actual wheel")
    portable = json.loads(source_path("resources/validation/release_0.12.1_portable_smoke.json").read_text(encoding="utf-8"))
    require(portable.get("clean_venv") is True and portable.get("launcher_install") is True
            and portable.get("http_health", {}).get("version") == VERSION
            and portable.get("versions", {}).get("oceanroute") == VERSION
            and re.search(r'2232 passed(?:, \d+ warnings?)? in ', portable.get("tests_output", "")),
            "Source fresh-install evidence has not completed its whole regression")
    require(portable.get("archive") == Path(initial["preserved_initial_archive"]["path"]).name,
            "Source fresh-install record names a different verified initial archive")
    wheel_smoke = json.loads(source_path("resources/validation/release_0.12.1_wheel_smoke.json").read_text(encoding="utf-8"))
    require(wheel_smoke.get("version") == wheel_smoke.get("wheel_metadata_version") == VERSION
            and wheel_smoke.get("wheel_bytes") == wheel.stat().st_size
            and wheel_smoke.get("wheel_sha256") == digest(wheel.read_bytes()),
            "Wheel runtime evidence did not test this exact application wheel")
    return runtime


def verify_release_evidence(runtime_report: Path, windows_reports: list[Path],
                            windows_exe: Path, wheel: Path) -> dict:
    """Bind completed records, never substitute old execution or run a gate.

    The runtime verifier owns its exact evidence schema; bind its real file and
    scope instead of manufacturing old field names. Independently check the
    actual served assets, PDFs/page counts and two existing Windows PE records.
    """
    runtime = verify_runtime_record(runtime_report, wheel)
    served_path = source_path(f"resources/validation/release_{LABEL}_served_assets.json")
    served = passed_evidence(served_path)
    require(served.get("health", {}).get("version") == VERSION, "Served health version differs")
    assets = served.get("assets")
    require(isinstance(assets, list) and len(assets) == 8
            and len({r["path"] for r in assets}) == len(assets), "Served asset list must contain its 8 real unique files")
    if "asset_count" in served:
        require(served["asset_count"] == len(assets), "Actual served asset_count differs")
    # The actual 0.12 record uses static_directories. A new explicitly supplied
    # equal_directories field is also usable, but never invent it from defaults.
    directories = served.get("static_directories", served.get("equal_directories"))
    require(isinstance(directories, list) and directories and len(set(directories)) == len(directories),
            "Served evidence must declare its actual static_directories/equal_directories")
    if "static_directories" in served and "equal_directories" in served:
        require(served["static_directories"] == served["equal_directories"], "Conflicting served directory evidence")
    require(FRONTEND in directories and "oceanroute/static" in directories,
            "Reviewed release frontend and packaged static must both be in actual served parity evidence")
    for folder in directories:
        files = files_under(folder)
        actual_names = {Path(n).relative_to(folder).as_posix() for n in files}
        require(actual_names == {r["path"] for r in assets}, f"Served frontend file set differs: {folder}")
        for row in assets:
            actual = descriptor(source_path(folder + "/" + row["path"]))
            require(actual["bytes"] == row["bytes"] and actual["sha256"] == row["sha256"],
                    f"Served frontend asset bytes differ: {folder}/{row['path']}")
    verify_artifact_row(served["manual"])

    qa_path = source_path(f"resources/validation/release_{LABEL}_pdf_qa.json")
    qa = passed_evidence(qa_path)
    require(qa.get("all_pages_actually_visually_verified") is True and not qa.get("blocking_defects"),
            "PDF QA must contain completed actual page-review evidence without blockers")
    documents = qa.get("documents")
    require(isinstance(documents, list) and len(documents) == 2
            and {r["kind"] for r in documents} == {"manual", "design"}, "PDF QA must identify the two actual documents")
    from pypdf import PdfReader
    pdfs = []
    for document in documents:
        title = "用户手册" if document["kind"] == "manual" else "设计文档"
        require(document["pdf"]["path"] == f"output/pdf/OceanRoute_{title}_{LABEL}.pdf",
                "PDF QA points at a historical PDF")
        path = verify_artifact_row(document["pdf"])
        reader = PdfReader(path)
        require(not reader.is_encrypted, "Encrypted product PDF")
        pages = len(reader.pages)
        require(pages == document["pdf"]["pages"] == document.get("actual_visual_review_pages")
                and pages > 0 and not document.get("blocking_defects"), "PDF actual/QA/visual page count differs")
        for field in ("source", "review"):
            verify_artifact_row(document[field])
        pdfs.append({"kind": document["kind"], "pdf": {**descriptor(path), "pages": pages},
                     "source": document["source"], "review": document["review"]})
    require(sum(r["pdf"]["pages"] for r in pdfs) == qa.get("total_pages"), "PDF QA total pages differs")
    verify_artifact_row(qa["builder"])
    verify_artifact_row(qa["cover"])

    windows_exe = source_path(windows_exe.resolve().relative_to(ROOT).as_posix())
    require(windows_exe.name == f"OceanRoute-{VERSION}-Windows-x64-Setup.exe", "Wrong Windows installer version/name")
    executable = windows_exe.read_bytes()
    require(len(executable) >= 64 and executable[:2] == b"MZ", "Windows installer is not a PE executable")
    pe_offset = struct.unpack_from("<I", executable, 0x3c)[0]
    require(pe_offset <= len(executable) - 24 and executable[pe_offset:pe_offset + 4] == b"PE\0\0",
            "Windows installer PE header is invalid")
    machine = struct.unpack_from("<H", executable, pe_offset + 4)[0]
    # NSIS's bootstrap can be x86 while the installed Python/application is x64.
    require(machine in (0x14c, 0x8664), "Unsupported Windows installer PE machine")
    require(len(windows_reports) == 2 and len({p.resolve() for p in windows_reports}) == 2,
            "Supply both actual Windows backend and installed-EXE execution records")
    windows = []
    kinds = set()
    wheel_row = descriptor(wheel)
    for path in windows_reports:
        execution = passed_evidence(path)
        if "actual_counts" in execution and "child_execution" in execution:
            kind = "backend"
            changed = execution.get("changed_inputs")
            require(isinstance(changed, dict) and changed
                    and all(isinstance(v, list) for v in changed.values())
                    and set(changed) == set(execution["before_inputs"])
                    and execution.get("returncode") == 0 and not any(changed.values()),
                    "Windows backend failed or changed inputs")
            require(execution.get("before_inputs") == execution.get("after_inputs"),
                    "Windows backend before/after source/runtime inputs differ")
            child = execution["child_execution"]
            require(child.get("version") == VERSION and child.get("status") == "passed"
                    and child.get("os_name") == "nt" and child.get("sys_platform") == "win32"
                    and child.get("pytest_returncode") == 0, "Windows PE backend child did not actually pass")
            counts = execution["actual_counts"]
            require(counts.get("tests", 0) > 0 and counts.get("failures") == 0 and counts.get("errors") == 0,
                    "Windows PE full backend has failed/errored cases")
            require(counts.get("passed", -1) + counts.get("skipped", -1) == counts["tests"]
                    and counts.get("skipped") == len(execution["actual_skipped_cases"])
                    and execution.get("only_platform_inapplicable_skips") is True,
                    "Windows full test accounting or natural-platform skip evidence differs")
            require({k: execution["wheel"][k] for k in wheel_row} == wheel_row,
                    "Windows backend used a different application wheel")
            for field in ("wheel", "probe", "log", "xml"):
                verify_artifact_row(execution[field])
            snapshot = verify_windows_source_snapshot(execution)
            require(child.get("production_distribution_count") == len(child["production_distributions"]) == 32
                    and child.get("production_dependencies_unchanged_except_application_version") is True,
                    "Windows PE did not use the recorded unchanged 32 production distributions")
            proof = {"actual_counts": counts, "actual_imported_application_modules": execution.get("actual_imported_application_modules"),
                     "environment": execution.get("environment"), "scope": execution.get("scope"),
                     "child_scope": child.get("scope"), "changed_inputs": changed, "source_snapshot": snapshot,
                     "production_distribution_count": child["production_distribution_count"]}
        elif "installer" in execution and "events" in execution:
            kind = "installer"
            require({k: execution["installer"][k] for k in ("path", "bytes", "sha256")} == descriptor(windows_exe),
                    "Installed-EXE execution did not test the current setup bytes")
            require(execution.get("double_launch_same_pid_and_port") is True
                    and execution.get("reinstallation_file_bytes_identical") is True
                    and execution.get("uninstall_preserved_unrelated_file") is True
                    and bool(execution.get("uninstall_preserved_user_databases")),
                    "Installed-EXE lifecycle/data-retention evidence is incomplete")
            require(execution.get("events") and all(r.get("exit_code") == 0 for r in execution["events"]),
                    "Installed-EXE execution contains command failures")
            require(execution.get("actual_owner_changes") and all(r["before"]["pid"] != r["after"]["pid"]
                    for r in execution["actual_owner_changes"]), "Installed workflows did not record actual owner replacement")
            require(execution.get("production_distribution_count") == 32
                    and execution.get("oceanroute_metadata_only_version") == VERSION
                    and execution.get("no_legacy_owned_residue_after_upgrade_and_reinstall") is True
                    and execution.get("external_frozen_runtime_unchanged") is True,
                    "Windows upgrade retained old owned metadata/dependency residue")
            for field in ("upgraded_distributions", "reinstalled_distributions"):
                verify_installed_distributions(execution[field], execution["production_distribution_count"])
            baseline = execution["actual_old_version_upgrade_baseline"]
            require(baseline.get("version") == "0.12.0"
                    and baseline.get("all_original_payload_files_byte_identical") is True,
                    "Upgrade must bind the actual byte-verified old installation")
            verify_artifact_row(baseline["installer"])
            installed = execution["installed_files"]
            require(execution["installed_file_count"] == len(installed) > 0
                    and len({r["path"] for r in installed}) == len(installed), "Installed payload inventory differs")
            for row in installed:
                verify_artifact_row(row)
            install_root = source_path(next(r["path"] for r in installed if r["path"].endswith("/OceanRoute.exe"))).parent
            removed = execution["removed_legacy_owned_paths"]
            require(isinstance(removed, list) and removed and len(set(removed)) == len(removed),
                    "Upgrade must record the removed legacy owned paths")
            for name in removed:
                relative = PurePosixPath(name)
                require(relative.as_posix() == name and not relative.is_absolute() and ".." not in relative.parts
                        and "\\" not in name and ":" not in name
                        and not install_root.joinpath(*relative.parts).exists(), "Legacy owned residue remains installed")
            for workflow in execution["workflows"].values():
                require(workflow.get("status") == "passed" and workflow.get("actual_process_closed_and_restarted") is True,
                        "Installed HTTP workflow lacks actual process lifecycle evidence")
                verify_artifact_row(workflow["harness"])
            uninstall = verify_uninstall_process(execution)
            verify_artifact_row(execution["probe"])
            proof = {"environment": execution.get("environment"), "native_windows_tested": execution.get("native_windows_tested"),
                     "scope": execution.get("scope"), "installed_file_count": execution.get("installed_file_count"),
                     "events": execution["events"], "actual_owner_changes": execution["actual_owner_changes"],
                     "production_distribution_count": execution["production_distribution_count"],
                     "oceanroute_metadata_only_version": execution["oceanroute_metadata_only_version"],
                     "removed_legacy_owned_paths": removed,
                     "no_legacy_owned_residue_after_upgrade_and_reinstall": True,
                     "actual_uninstall_process": uninstall, "actual_old_version_upgrade_baseline": baseline["installer"]}
        else:
            raise ValueError(f"Unknown actual Windows execution schema: {path.name}")
        require(kind not in kinds, "Duplicate Windows execution kind")
        kinds.add(kind)
        windows.append({"kind": kind, "record": descriptor(path.resolve()), "proof": proof})
    require(kinds == {"backend", "installer"}, "Both Windows execution kinds are required")
    build_path = source_path(f"resources/validation/release_{LABEL}_windows_build_final.json")
    build = json.loads(build_path.read_text(encoding="utf-8"))
    require(build.get("version") == VERSION and build.get("status") == "built"
            and build.get("installer_built") is True and build.get("commands")
            and all(row.get("exit_code") == 0 for row in build["commands"]),
            "Final Windows build evidence has not actually succeeded")
    require(build["installer"] == descriptor(windows_exe), "Windows final build did not produce this exact installer")
    require(build["preparation"].get("final_distribution_count_including_application") == 32
            and build["preparation"].get("old_runtime_unchanged") is True,
            "Windows final payload does not preserve the recorded production dependency baseline")
    for row in build["input_files"]:
        verify_artifact_row(row)
    require(any(row == wheel_row for row in build["input_files"]), "Windows final build used a different wheel")
    for field in ("uninstall_list", "legacy_upgrade_cleanup"):
        verify_artifact_row(build[field])
    return {"runtime_record": descriptor(runtime_report.resolve()), "runtime_scope": runtime.get("scope"),
            "served_record": descriptor(served_path), "served_assets": assets,
            "served_directory_field": "static_directories" if "static_directories" in served else "equal_directories",
            "served_directories": directories, "pdf_qa_record": descriptor(qa_path), "pdfs": pdfs,
            "windows_exe": {**descriptor(windows_exe), "pe_machine": hex(machine)}, "windows_execution_records": windows,
            "windows_final_build": {"record": descriptor(build_path), "installer": build["installer"],
                                    "input_files": build["input_files"], "scope": build["scope"]},
            "scope": "Read-only binding of actual completed reports and product bytes. This auditor never executes an installer, "
                     "tests, HTTP requests or a visual review, and does not turn Wine evidence into native Windows-machine proof."}


def audit(archive: Path, wheel: Path, report: Path, frozen: Path,
          *, runtime_report: Path, windows_reports: list[Path], windows_exe: Path,
          verify_only: bool = False) -> dict:
    archive, wheel, report, frozen = (p.resolve() for p in (archive, wheel, report, frozen))
    require(report.is_relative_to(ROOT / "resources/validation") and report.suffix == ".json",
            "The embedded report must be a JSON file under resources/validation")
    require(frozen == (ROOT / FROZEN).resolve(), "Use the actual inherited 0.12 190-artifact historical manifest")
    require(archive.is_file() and wheel.is_file(), "Source archive/wheel does not exist")
    require(verify_only or not report.exists(), "Refusing to overwrite existing audit evidence")
    require(not verify_only or report.is_file(), "Final embedded audit report does not exist")
    verify_versions()
    verify_builder_contract()
    historical = verify_historical(frozen)
    initial, runtime_rows = verify_initial_inputs()
    frontend = verify_frontend()
    evidence = verify_release_evidence(runtime_report, windows_reports, windows_exe, wheel)
    expected = required_sources()
    self_member = "OceanRoute/" + report.relative_to(ROOT).as_posix()
    expected_names = {"OceanRoute/" + name for name in expected}
    members = []
    with zipfile.ZipFile(archive) as bundle:
        names = check_zip_structure(bundle, source_archive=True)
        require(set(names) - {self_member} == expected_names - {self_member},
                "Source ZIP member set differs from actual 0.12.1 builder contract; "
                f"missing={sorted(expected_names-set(names)-{self_member})}, "
                f"extra={sorted(set(names)-expected_names-{self_member})}")
        require(not verify_only or self_member in names, "Final ZIP must contain the audit report")
        for info in bundle.infolist():
            data = bundle.read(info.filename)
            if info.filename == self_member:
                require(report.is_file() and data == report.read_bytes(), "Embedded audit report differs")
                continue
            name = info.filename.removeprefix("OceanRoute/")
            require(data == expected[name].read_bytes(), f"Source ZIP byte mismatch: {info.filename}")
            members.append({"path": info.filename, "bytes": len(data), "sha256": digest(data)})
        for row in runtime_rows:
            data = bundle.read("OceanRoute/" + row["path"])
            require(len(data) == row["bytes"] and digest(data) == row["sha256"],
                    f"Final ZIP differs from tested initial runtime input: {row['path']}")
        wheel_evidence = verify_wheel(wheel, bundle)
    result = {
        "schema_version": 1, "version": VERSION, "status": "passed",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Independent byte/CRC/path/RECORD/source contract audit of the 0.12.1 source-portable "
                 "ZIP, external wheel, Windows EXE and bound existing execution/PDF evidence. "
                 "No installer or test is executed by this auditor; Wine is not a native Windows machine.",
        "auditor": descriptor(Path(__file__).resolve()),
        "auditor_packaging": "Versioned resource audit tool explicitly included by the actual builder; "
                             "its audit report must be embedded after the one-time archive refresh.",
        "build_contract": {"builder": descriptor(source_path("scripts/build_release.py")),
                           "roots": list(RELEASE_ROOTS), "explicit_files": list(RELEASE_FILES),
                           "resources_validation_policy": "Dynamically include every current non-pyc/non-cache file; "
                                                          "only this report's own member is excluded from its digest list.",
                           "required_0_12_1_runners_and_probes": [descriptor(source_path(n)) for n in RUNNERS_0_12_1]},
        "audited_non_self_zip_members": len(members), "members": members,
        "non_self_uncompressed_bytes": sum(m["bytes"] for m in members),
        "wheel_members": wheel_evidence["members"], "wheel_package_files": wheel_evidence["package_files"],
        "wheel_record_verified": True, "zip_wheel_source_parity": True, "wheel": wheel_evidence,
        "reviewed_frontend": frontend, "reviewed_frontend_count": len(frontend),
        "historical_frozen_artifacts": historical["count"], "historical": historical,
        "verified_initial_runtime_inputs": initial, "release_evidence": evidence,
        "path_crc_and_no_developer_data_checks": True,
        "self_reference": "Final ZIP SHA256 and size are external manifest facts; this report excludes "
                          "its own embedded member and does not claim a self-referential ZIP digest.",
    }
    if verify_only:
        saved = json.loads(report.read_text(encoding="utf-8"))
        require(saved.get("status") == "passed" and saved.get("version") == VERSION,
                "Saved audit report has wrong status/version")
        for key in ("auditor", "build_contract", "audited_non_self_zip_members", "members",
                    "non_self_uncompressed_bytes", "wheel_members", "wheel_package_files",
                    "wheel_record_verified", "zip_wheel_source_parity", "wheel", "reviewed_frontend",
                    "reviewed_frontend_count", "historical_frozen_artifacts", "historical",
                    "verified_initial_runtime_inputs", "release_evidence", "path_crc_and_no_developer_data_checks"):
            require(saved.get(key) == result[key], f"Final artifact/evidence differs from audit snapshot: {key}")
    else:
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    # This output is deliberately outside the embedded evidence, and can be
    # used for the external final manifest after the report has been packaged.
    print(json.dumps({"version": VERSION, "status": "passed", "verify_only": verify_only,
                      "archive": descriptor(archive), "zip_members": len(names),
                      "zip_uncompressed_bytes": sum(m["bytes"] for m in members)
                                                + (report.stat().st_size if self_member in names else 0),
                      "audited_non_self_zip_members": len(members), "wheel": wheel_evidence,
                      "historical_frozen_artifacts": historical["count"],
                      "verified_initial_runtime_inputs": len(runtime_rows)}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, default=ROOT / FROZEN)
    parser.add_argument("--verify-only", action="store_true",
                        help="Read-only final check, including the exact embedded audit report")
    parser.add_argument("--runtime-report", type=Path, default=ROOT / "resources/validation/release_0.12.1_verified_runtime.json")
    parser.add_argument("--windows-report", type=Path, action="append",
                        help="Actual backend/installer Windows PE execution records; supply both, or use canonical defaults")
    parser.add_argument("--windows-exe", type=Path, default=ROOT / "outputs/releases/OceanRoute-0.12.1-Windows-x64-Setup.exe")
    options = parser.parse_args()
    audit(options.archive, options.wheel, options.report, options.frozen,
          runtime_report=options.runtime_report, windows_exe=options.windows_exe,
          windows_reports=options.windows_report or [
              ROOT / "resources/validation/release_0.12.1_windows_backend_final_2_execution.json",
              ROOT / "resources/validation/release_0.12.1_windows_installer_execution.json"],
          verify_only=options.verify_only)
