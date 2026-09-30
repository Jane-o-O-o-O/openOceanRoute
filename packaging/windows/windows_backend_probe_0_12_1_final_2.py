"""Independent Windows PE runtime QA under private Wine; never edit product files.

The host side validates/extracts the preserved source candidate and records
before/after bytes. The Windows side imports every installed application
module and runs the entire supplied tests directory. Test dependencies remain
outside the application payload. Wine evidence is not Windows-machine proof.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile

VERSION = "0.12.1"
NODE_VERSION = "v24.19.0"
SOURCE_ROOTS = ("oceanroute", "tests", "scripts", "examples")
SOURCE_FILES = ("pyproject.toml", "resources/research/s57_sources.json",
                "resources/validation/development_0.7_legacy_checkpoint_inputs.json")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def files(directory, *, exclude_cache=True):
    result = {}
    for p in sorted(directory.rglob("*")):
        if not p.is_file():
            continue
        if exclude_cache and ("__pycache__" in p.parts or ".pytest_cache" in p.parts or p.suffix == ".pyc"):
            continue
        if p.is_symlink():
            raise ValueError(f"symlink in QA input: {p}")
        b = p.read_bytes()
        result[p.relative_to(directory).as_posix()] = {"bytes": len(b), "sha256": sha(b)}
    return result


def descriptor(path, root):
    b = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "bytes": len(b), "sha256": sha(b)}


def windows_main():
    source, dependencies, result_path, xml_path, node, baseline_path = map(Path, sys.argv[2:8])
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    os.chdir(source)
    site = Path(sys.executable).parent / "Lib" / "site-packages"
    # Embeddable Python's ._pth already admits the production site. Admit
    # test-only packages separately; never put the source application first.
    sys.path.insert(0, str(site))
    sys.path.append(str(dependencies))
    result = {"version": VERSION, "status": "failed", "scope": "Windows CPython PE execution under Wine, not a Windows physical/VM machine",
              "python": sys.version, "python_executable": sys.executable,
              "os_name": os.name, "sys_platform": sys.platform,
              "test_dependencies_path": str(dependencies), "installed_application_path": str(site / "oceanroute"),
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "imported_modules": [],
              "command": [sys.executable, "-B", str(Path(__file__).resolve()), "--windows-child"],
              "pytest_arguments": ["tests", "-q", "--junitxml=" + str(xml_path)]}
    started = time.perf_counter()
    try:
        if os.name != "nt" or sys.platform != "win32":
            raise ValueError("QA child is not real Windows CPython")
        import importlib.metadata as md
        result["production_distributions"] = sorted(
            [{"name": d.metadata["Name"], "version": d.version} for d in md.distributions(path=[str(site)])],
            key=lambda d: d["name"].lower())
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["child_execution"]["production_distributions"]
        expected = [{**row, "version": VERSION} if row["name"].lower() == "oceanroute" else row for row in baseline]
        if len(result["production_distributions"]) != 32 or result["production_distributions"] != expected:
            raise ValueError("Production dependency names/versions changed beyond the explicit application update")
        result["production_distribution_count"] = len(result["production_distributions"])
        result["production_dependencies_unchanged_except_application_version"] = True
        for p in sorted((site / "oceanroute").rglob("*.py")):
            parts = p.relative_to(site).with_suffix("").parts
            name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            m = importlib.import_module(name)
            actual = Path(m.__file__).resolve()
            if actual != p.resolve():
                raise ValueError(f"application module escaped installed wheel: {name} {actual}")
            result["imported_modules"].append({"module": name, "path": str(actual), "sha256": sha(actual.read_bytes())})
        if len(result["imported_modules"]) != 57 or md.version("oceanroute") != VERSION:
            raise ValueError("Expected actual 57 application modules at version " + VERSION)
        print("WINDOWS_QA_IMPORTED " + str(len(result["imported_modules"])), flush=True)
        # A real official Windows Node PE tool is admitted only to the QA PATH.
        # The installer/production runtime never acquires this dependency.
        os.environ["PATH"] = str(node.parent) + os.pathsep + os.environ.get("PATH", "")
        resolved_node = shutil.which("node")
        if resolved_node is None or Path(resolved_node).resolve() != node.resolve():
            raise ValueError("Node QA PATH does not resolve the verified Windows executable")
        process = subprocess.run([resolved_node, "-p", "JSON.stringify({version:process.version,platform:process.platform,arch:process.arch,execPath:process.execPath})"],
                                 capture_output=True, text=True, timeout=30, check=True)
        actual_node = json.loads(process.stdout)
        if (actual_node.get("version") != NODE_VERSION or actual_node.get("platform") != "win32"
                or actual_node.get("arch") != "x64" or Path(actual_node["execPath"]).resolve() != node.resolve()):
            raise ValueError("QA Node is not the expected official Windows PE runtime")
        result["actual_windows_node"] = {**actual_node, "path": str(node), "sha256": sha(node.read_bytes()),
                                         "qa_only": True, "stderr": process.stderr}
        # Some supplied tests import the release's scripts namespace. All
        # application modules are already pinned to the verified installed
        # package; admit this helper root last and recheck origins after tests.
        sys.path.append(str(source))
        import pytest
        import httpx
        for m in (pytest, httpx):
            if not Path(m.__file__).resolve().is_relative_to(dependencies.resolve()):
                raise ValueError("test dependency leaked into production payload: " + m.__name__)
        result["actual_pytest_path"] = pytest.__file__
        result["actual_httpx_path"] = httpx.__file__
        exitcode = int(pytest.main(result["pytest_arguments"]))
        result["pytest_returncode"] = exitcode
        for row in result["imported_modules"]:
            m = sys.modules[row["module"]]
            if Path(m.__file__).resolve() != Path(row["path"]).resolve():
                raise ValueError("module source changed during tests: " + row["module"])
        result["application_module_origins_unchanged"] = True
        result["status"] = "passed" if exitcode == 0 else "failed"
    except BaseException as exc:
        traceback.print_exc()
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        result["pytest_returncode"] = result.get("pytest_returncode", 1)
    finally:
        result["elapsed_s"] = time.perf_counter() - started
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        result_path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    return result["pytest_returncode"]


def wine_path(path):
    # Private Wine prefixes map Z: to the host root; no product path rewriting.
    return "Z:" + str(path.resolve()).replace("/", "\\")


def source_inputs(root):
    """Copy only actual runtime/test resources, with explicit bounded roots.

    The native installed app remains the only imported app. The copy admits
    test fixtures/scripts/examples and the two frozen research input files.
    It is a byte snapshot of local source, not an installed source ZIP claim.
    """
    result = {}
    for folder in SOURCE_ROOTS:
        for name, row in files(root / folder).items():
            result[folder + "/" + name] = row
    for name in SOURCE_FILES:
        data = (root / name).read_bytes()
        result[name] = {"bytes": len(data), "sha256": sha(data)}
    return dict(sorted(result.items()))


def prepare_source(root, source):
    expected = source_inputs(root)
    if source.exists():
        if files(source) != expected:
            raise ValueError("Existing 0.12.1 source snapshot differs; preserve it instead of silently refreshing")
        return expected
    staging = source.parent / (source.name + ".staging")
    if staging.exists():
        raise ValueError("Incomplete previous source staging must be retained")
    staging.mkdir(parents=True)
    for name, row in expected.items():
        original = root / name
        if original.is_symlink() or not original.resolve().is_relative_to(root):
            raise ValueError("QA source symlink/escape: " + name)
        data = original.read_bytes()
        if {"bytes": len(data), "sha256": sha(data)} != row:
            raise ValueError("Source changed during snapshot preparation: " + name)
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if files(staging) != expected or source_inputs(root) != expected:
        raise ValueError("Source snapshot byte consistency failed")
    staging.rename(source)
    return expected


def host_main():
    root = Path(__file__).resolve().parents[2]
    packaging = root / "packaging" / "windows"
    wheel = root / "outputs/releases/oceanroute-0.12.1-py3-none-any.whl"
    payload = packaging / "payload-0.12.1" / "runtime"
    package = payload / "Lib" / "site-packages" / "oceanroute"
    dependencies = packaging / "test-dependencies"
    qa = packaging / "qa-source-0.12.1-final-2"
    source = qa / "OceanRoute"
    prefix = packaging / "wine-test-prefix-0.12.1-final-2"
    wine = packaging / "tooling/Wine Stable.app/Contents/Resources/wine/bin/wine"
    node = packaging / "qa-tools/node-v24.19.0-win-x64/node.exe"
    node_report_path = root / "resources/validation/development_0.12.1_windows_node_preparation.json"
    node_report = json.loads(node_report_path.read_text(encoding="utf-8"))
    node_evidence = next(r for r in node_report["extracted"] if r["path"].endswith("/node.exe"))
    if (not node_report.get("archive_sha256_verified") or descriptor(node, root) != node_evidence
            or descriptor(root / node_report["archive"]["path"], root) != node_report["archive"]
            or descriptor(root / node_report["checksums"]["path"], root) != node_report["checksums"]):
        raise ValueError("Official Node QA input/checksum evidence changed")
    baseline_path = root / "resources/validation/development_0.12_windows_backend_verified_execution.json"
    if sha(baseline_path.read_bytes()) != "b8702993c283469de026a1c67fa5671b2eb6dc3ee57fda7ee3cbbfcbd7cf9e8b":
        raise ValueError("Original actual Windows dependency baseline changed")
    stem = sys.argv[2] if len(sys.argv) == 3 and sys.argv[1] == "--output-stem" else "release_0.12.1_windows_backend_final_2_execution"
    if not stem.startswith("release_0.12.1_windows_backend_") or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_." for c in stem):
        raise ValueError("invalid QA evidence output stem")
    report_path = root / ("resources/validation/" + stem + ".json")
    log_path = report_path.with_suffix(".log")
    xml_path = report_path.with_suffix(".xml")
    child_path = packaging / (stem + "_child.json")
    for p in (report_path, log_path, xml_path, child_path):
        if p.exists():
            raise ValueError("Refusing to overwrite real QA evidence: " + str(p))
    installed = files(package)
    with zipfile.ZipFile(wheel) as z:
        if z.testzip() is not None or len(z.namelist()) != len(set(z.namelist())):
            raise ValueError("application wheel CRC/duplicate member check failed")
        for name in z.namelist():
            p = PurePosixPath(name)
            if p.is_absolute() or p.as_posix() != name or ".." in p.parts or "\\" in name or ":" in name:
                raise ValueError("Noncanonical/unsafe application wheel member")
        expected = {n[len("oceanroute/"):]: {"bytes": len(z.read(n)), "sha256": sha(z.read(n))}
                    for n in z.namelist() if n.startswith("oceanroute/") and not n.endswith("/")}
    if installed != expected:
        raise ValueError("Windows installed application does not equal real wheel")
    source_snapshot = prepare_source(root, source)
    if files(source / "oceanroute") != installed:
        raise ValueError("source candidate application does not equal installed wheel")
    before = {"windows_runtime_payload": files(payload), "test_only_dependencies": files(dependencies),
              "supplied_tests": files(source / "tests"), "supplied_production_source": files(source / "oceanroute"),
              "complete_qa_source_snapshot": files(source), "official_qa_node_files": files(node.parent)}
    application_modules = sum(n.endswith(".py") for n in installed)
    if application_modules != 57:
        raise ValueError("Expected exactly 57 actual application Python modules")
    command = [str(wine), str(payload / "python.exe"), "-B", wine_path(Path(__file__)), "--windows-child",
               wine_path(source), wine_path(dependencies), wine_path(child_path), wine_path(xml_path), wine_path(node), wine_path(baseline_path)]
    environment = os.environ.copy()
    environment.update(WINEPREFIX=str(prefix), WINEDEBUG="-all", PYTHONDONTWRITEBYTECODE="1",
                       OCEANROUTE_DATA_DIR=wine_path(packaging / "qa-data-0.12.1-final-2"))
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    print("Starting real Windows PE full-suite run under private Wine", flush=True)
    with log_path.open("xb") as stream:
        completed = subprocess.run(command, cwd=source, env=environment, stdout=stream, stderr=subprocess.STDOUT)
    after = {"windows_runtime_payload": files(payload), "test_only_dependencies": files(dependencies),
             "supplied_tests": files(source / "tests"), "supplied_production_source": files(source / "oceanroute"),
             "complete_qa_source_snapshot": files(source), "official_qa_node_files": files(node.parent)}
    changes = {kind: sorted(n for n in before[kind].keys() | after[kind].keys() if before[kind].get(n) != after[kind].get(n)) for kind in before}
    child = json.loads(child_path.read_text(encoding="utf-8")) if child_path.exists() else {"status": "failed", "error": "Windows child did not produce a complete report"}
    counts = None
    if xml_path.exists():
        tree = ET.parse(xml_path).getroot()
        cases = list(tree.iter("testcase"))
        counts = {"tests": len(cases), "failures": sum(c.find("failure") is not None for c in cases),
                  "errors": sum(c.find("error") is not None for c in cases), "skipped": sum(c.find("skipped") is not None for c in cases),
                  "passed": sum(c.find("failure") is None and c.find("error") is None and c.find("skipped") is None for c in cases)}
    skipped = [{"classname": c.get("classname"), "name": c.get("name"), "reason": c.find("skipped").get("message", "")}
               for c in cases if c.find("skipped") is not None] if xml_path.exists() else []
    allowed_skips_only = bool(counts is not None) and len(skipped) == 1 and all(
        row["classname"] == "tests.test_voyage_jobs_integrity"
        and row["name"] == "test_atomic_publication_flushes_parent_directory_on_posix"
        and row["reason"] == "directory fsync is a POSIX durability invariant" for row in skipped)
    current_source_after = source_inputs(root)
    report = {"version": VERSION, "status": "passed" if completed.returncode == 0 and child.get("status") == "passed" and not any(changes.values()) and allowed_skips_only and current_source_after == source_snapshot else "failed",
              "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "started_at_utc": started_at,
              "scope": "Actual bundled Windows CPython PE imports and entire supplied backend tests under private macOS Wine/Rosetta compatibility layer. Not Windows physical/VM execution and not browser, installer/uninstaller or EXE acceptance.",
              "command": command, "environment": {k: environment[k] for k in ("WINEPREFIX", "WINEDEBUG", "OCEANROUTE_DATA_DIR", "PYTHONDONTWRITEBYTECODE")},
              "returncode": completed.returncode, "wall_time_s": time.perf_counter() - started,
              "wheel": descriptor(wheel, root), "source_snapshot": {"kind": "independent_local_byte_copy_not_source_zip", "path": source.relative_to(root).as_posix(),
                  "files": source_snapshot, "file_count": len(source_snapshot), "current_source_after_unchanged": current_source_after == source_snapshot},
              "original_windows_dependency_baseline": descriptor(baseline_path, root),
              "official_qa_node_preparation": descriptor(node_report_path, root),
              "probe": descriptor(Path(__file__).resolve(), root), "expected_application_py_modules": application_modules,
              "actual_imported_application_modules": len(child.get("imported_modules", [])),
              "application_package_member_count": len(installed), "actual_counts": counts, "child_execution": child,
              "actual_skipped_cases": skipped, "only_platform_inapplicable_skips": allowed_skips_only,
              "before_inputs": before, "after_inputs": after, "changed_inputs": changes,
              "log": descriptor(log_path, root), "xml": descriptor(xml_path, root) if xml_path.exists() else None,
              "limitations": ["The installer and native Windows machine have not been exercised by this probe.", "Test-only pytest/httpx and official Windows Node are separate from the production payload.", "No selection, grep or explicit skip flags were supplied; only an actual POSIX-only fsync skip can accompany passed status.", "Source is an independent current local byte snapshot, not a fresh source-ZIP installation claim."]}
    report_path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "returncode": completed.returncode, "counts": counts, "report": descriptor(report_path, root)}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(windows_main() if len(sys.argv) > 1 and sys.argv[1] == "--windows-child" else host_main())
