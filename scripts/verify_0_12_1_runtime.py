"""Read-only current release checks; actual test execution remains in its reports."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.12.1"
FRONTEND = "web/dist-0.12.1-release"


def require(value, message):
    if not value:
        raise ValueError(message)


def descriptor(path):
    path = Path(path)
    data = path.read_bytes()
    return {"path": path.relative_to(ROOT).as_posix(), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def check_row(row):
    path = ROOT / row["path"]
    require(path.resolve().is_relative_to(ROOT.resolve()) and path.is_file() and not path.is_symlink(), "Unsafe or missing evidence")
    current = descriptor(path)
    require(all(current[key] == row[key] for key in ("bytes", "sha256")), "Evidence bytes changed: " + row["path"])
    return path


def load(name):
    return json.loads((ROOT / "resources/validation" / name).read_text(encoding="utf-8"))


def required_runtime_paths():
    names = {"launcher.py", "pyproject.toml", "web/index.html", "web/package.json", "web/package-lock.json",
             "web/tsconfig.json", "web/vite.config.ts", "web/playwright.config.ts",
             "resources/research/s57_sources.json", "resources/research/arc_sources.json"}
    names.update(f"resources/run_0_12_1_{part}.py" for part in (
        "backend_gate", "browser_gate", "ui_build", "arc_geometry_probe", "arc_edit_consumer_probe"))
    names.add("resources/validate_0_12_1_pdf_structure.py")
    for folder in ("oceanroute", "examples", "tests/fixtures", "web/src", "web/tests", "web/public", FRONTEND):
        names.update(p.relative_to(ROOT).as_posix() for p in (ROOT / folder).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    names.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("*.py"))
    names.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "scripts").rglob("*")
                 if p.is_file() and p.suffix in {".py", ".sh", ".bat"})
    return names


def run(archive, url):
    require(re.search(r'^version = "0.12.1"$', (ROOT / "pyproject.toml").read_text(), re.M), "Wrong source version")
    require((ROOT / "docs/USER_MANUAL.md").read_bytes() == (ROOT / "oceanroute/manual.md").read_bytes(), "Manual mirror mismatch")
    frozen = []
    for name in ("development_0.12_frozen_artifacts.json", "development_0.12_pre_windows_compatibility_freeze.json"):
        value = load(name)
        for row in value["artifacts"]:
            check_row(row)
        frozen.append({"manifest": descriptor(ROOT / "resources/validation" / name), "artifact_count": len(value["artifacts"]), "all_unchanged": True})
    backend = load("release_0.12.1_backend_final_3.json")
    require(backend["status"] == "passed" and backend["returncode"] == 0 and not backend["changed_inputs"], "Backend gate failed")
    stats = backend["stats"]
    require(stats == {"tests": 2232, "failures": 0, "errors": 0, "skipped": 0}, "Incomplete backend regression")
    cases = list(ET.parse(ROOT / "resources/validation/release_0.12.1_backend_final_3.xml").getroot().iter("testcase"))
    require(len(cases) == stats["tests"] and all(not any(c.find(t) is not None for t in ("failure", "error", "skipped")) for c in cases), "JUnit mismatch")
    for row in load("release_0.12.1_backend_final_3_inputs.json")["files"]:
        check_row(row)
    browser = load("release_0.12.1_browser_execution.json")
    require(browser["status"] == "passed" and browser["returncode"] == 0 and not browser["changed_inputs"], "Browser gate failed")
    require(browser["actual_tests"] == 116 and all(len(t["results"]) == 1 and t["results"][0]["status"] == "passed" and t["results"][0]["retry"] == 0 for t in browser["tests"]), "Browser execution incomplete")
    require(all(browser["stats"][k] == 0 for k in ("unexpected", "skipped", "flaky")), "Browser retry/skip/failure")
    for row in load("release_0.12.1_browser_inputs.json")["files"]:
        check_row(row)
    windows = load("release_0.12.1_windows_backend_final_2_execution.json")
    require(windows["status"] == "passed" and windows["returncode"] == 0 and not any(windows["changed_inputs"].values()), "Windows PE full gate failed")
    require(windows["actual_counts"]["tests"] == 2232 and windows["actual_counts"]["failures"] == 0 and windows["actual_counts"]["errors"] == 0, "Windows PE regression incomplete")
    require(windows["actual_counts"]["passed"] == 2231 and windows["actual_counts"]["skipped"] == 1 and windows["only_platform_inapplicable_skips"], "Unexpected Windows skipped tests")
    require(windows["actual_imported_application_modules"] == 57 and windows["child_execution"]["application_module_origins_unchanged"], "Windows tested source mismatch")
    installer = load("release_0.12.1_windows_installer_execution.json")
    require(installer["status"] == "passed" and installer["version"] == VERSION, "Actual installed EXE acceptance failed")
    check_row(installer["installer"])
    require(installer["production_distribution_count"] == 32 and installer["oceanroute_metadata_only_version"] == VERSION and installer["no_legacy_owned_residue_after_upgrade_and_reinstall"], "Upgrade retained old owned metadata")
    uninstaller = installer["actual_uninstall_process"]
    require(uninstaller["actual_windows_pid"] > 0 and uninstaller["actual_exit_code"] == 0 and uninstaller["actual_process_exited"] and uninstaller["byte_identical_copy"], "Real uninstall process did not exit")
    require(installer["reinstallation_file_bytes_identical"] and installer["uninstall_preserved_unrelated_file"] and installer["uninstall_preserved_user_databases"], "Uninstall/reinstall retention failed")
    wheel_smoke = load("release_0.12.1_wheel_smoke.json")
    require(wheel_smoke["version"] == VERSION and wheel_smoke["wheel_metadata_version"] == VERSION, "Wheel runtime version mismatch")
    portable = load("release_0.12.1_portable_smoke.json")
    require(portable["clean_venv"] and portable["versions"]["oceanroute"] == VERSION and re.search(r'2232 passed(?:, \d+ warning)? in ', portable["tests_output"]), "Fresh source install regression incomplete")
    pdf = load("release_0.12.1_pdf_qa.json")
    require(pdf["status"] == "passed" and pdf["all_pages_actually_visually_verified"], "PDF pages not verified")
    for document in pdf["documents"]:
        for key in ("pdf", "source", "review"):
            check_row(document[key])
        for row in document["renders"]:
            check_row(row)
    visual = load("release_0.12.1_visual_review.json")
    require(visual["status"] == "passed" and visual["actual_individually_viewed_count"] == 117, "Screenshot review incomplete")
    for row in visual["images"]:
        check_row(row)
    with urllib.request.urlopen(url + "/api/health", timeout=10) as response:
        health = json.load(response)
    require(health["version"] == VERSION, "Served backend version mismatch")
    assets = []
    for p in sorted((ROOT / FRONTEND).rglob("*")):
        if not p.is_file():
            continue
        name = p.relative_to(ROOT / FRONTEND).as_posix()
        for directory in ("web/dist", "oceanroute/static"):
            require((ROOT / directory / name).read_bytes() == p.read_bytes(), "Static mirror differs")
        with urllib.request.urlopen(url + "/" + name, timeout=10) as response:
            require(response.read() == p.read_bytes(), "Actual HTTP asset mismatch")
        assets.append(descriptor(p))
    require(len(assets) == 8, "Incomplete served frontend")
    with urllib.request.urlopen(url + "/api/manual", timeout=10) as response:
        # The API serves text or a content envelope depending on the maintained contract.
        data = response.read()
    manual = (ROOT / "docs/USER_MANUAL.md").read_text()
    try:
        content = json.loads(data).get("text")
    except (ValueError, AttributeError):
        content = data.decode("utf-8")
    require(content == manual, "Served manual differs")
    runtime = sorted(required_runtime_paths())
    with zipfile.ZipFile(archive) as z:
        require(z.testzip() is None and len(z.namelist()) == len(set(z.namelist())), "Source archive CRC or duplicate member")
        for name in runtime:
            require(z.read("OceanRoute/" + name) == (ROOT / name).read_bytes(), "Source archive runtime mismatch: " + name)
    wheel = ROOT / "outputs/releases/oceanroute-0.12.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as z:
        actual = {name: z.read(name) for name in z.namelist() if name.startswith("oceanroute/")}
        expected = {"oceanroute/" + p.relative_to(ROOT / "oceanroute").as_posix(): p.read_bytes() for p in (ROOT / "oceanroute").rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        require(actual == expected and len(actual) == 66 and z.testzip() is None, "Wheel/current source mismatch")
    names = ["release_0.12.1_backend_final_3.json", "release_0.12.1_backend_final_3.xml", "release_0.12.1_browser.json", "release_0.12.1_browser_execution.json", "release_0.12.1_windows_backend_final_2_execution.json", "release_0.12.1_windows_installer_execution.json", "release_0.12.1_windows_build_final.json", "release_0.12.1_wheel_smoke.json", "release_0.12.1_portable_smoke.json", "release_0.12.1_pdf_qa.json", "release_0.12.1_visual_review.json"]
    return {"version": VERSION, "status": "passed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "Read-only byte/current-source/actual report/loopback verification; not a new test execution or native Windows machine acceptance.",
            "backend_report": names[0], "backend_tests": stats["tests"], "browser_tests": browser["actual_tests"],
            "windows_counts": windows["actual_counts"], "frozen_baselines": frozen, "runtime_files_count": len(runtime),
            "runtime_files": [descriptor(ROOT / name) for name in runtime], "bound_reports": [descriptor(ROOT / "resources/validation" / name) for name in names],
            "assets": assets, "health": health, "wheel": descriptor(wheel),
            "limitations": ["Windows PE and installer checks used macOS Wine/Rosetta; native Windows physical/VM validation remains unperformed.", "Installer is unsigned."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8782")
    parser.add_argument("--report", type=Path, default=ROOT / "resources/validation/release_0.12.1_verified_runtime.json")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    record = run(args.archive, args.url)
    if args.verify_only:
        old = json.loads(args.report.read_text())
        require(old["version"] == VERSION and old["status"] == "passed", "Wrong stored runtime record")
        for key in ("runtime_files", "bound_reports", "assets", "wheel", "frozen_baselines"):
            require(record[key] == old[key], "Stored verified runtime bytes differ: " + key)
    else:
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({key: record[key] for key in ("version", "status", "backend_tests", "browser_tests", "windows_counts", "runtime_files_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
