"""Verify recorded 0.8 execution inputs and installed runtime bytes without rerunning tests."""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "resources/validation"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(name: str) -> dict:
    return json.loads((VALIDATION / name).read_text())


def verify_row(row: dict) -> None:
    data = (ROOT / row["path"]).read_bytes()
    assert len(data) == row["bytes"] and sha(data) == row["sha256"], row["path"]


def leaves(suites: list) -> list:
    tests = []
    for suite in suites:
        for spec in suite.get("specs", []):
            tests.extend(spec.get("tests", []))
        tests.extend(leaves(suite.get("suites", [])))
    return tests


def verify(archive: Path, *, verify_only: bool) -> None:
    report_path = VALIDATION / "release_0.8_verified_runtime.json"
    if not verify_only and report_path.exists():
        raise RuntimeError("Existing runtime evidence must not be overwritten")
    backend = load("release_0.8_backend.json")
    browser = load("release_0.8_browser_execution.json")
    inputs = {}
    for kind, expected_count, execution in (("backend", 132, backend), ("browser", 156, browser)):
        name = f"release_0.8_{kind}_inputs.json"
        snapshot = load(name)
        assert len(snapshot["files"]) == expected_count
        assert len({row["path"] for row in snapshot["files"]}) == expected_count
        assert sha((VALIDATION / name).read_bytes()) == execution["input_snapshot_sha256"]
        assert execution["status"] == "passed" and execution["returncode"] == 0
        assert execution["changed_inputs"] == []
        for row in snapshot["files"]:
            verify_row(row)
        inputs[kind] = {"count": expected_count, "current_bytes_match_recorded_execution": True}
    cases = list(ET.parse(VALIDATION / "release_0.8_backend.xml").getroot().iter("testcase"))
    assert len(cases) == 1566
    assert all(not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")) for case in cases)
    raw = load("release_0.8_browser.json")
    tests = leaves(raw["suites"])
    assert len(tests) == browser["actual_tests"] == 87
    assert not raw.get("errors")
    assert raw["stats"] == browser["stats"]
    assert all(raw["stats"][key] == 0 for key in ("unexpected", "skipped", "flaky"))
    assert all(len(test["results"]) == 1 and test["results"][0]["status"] == "passed"
               and test["results"][0]["retry"] == 0 and not test["results"][0].get("errors") for test in tests)
    copies = (("development_0.8_final_backend", "release_0.8_backend", (".json", ".log", ".xml", "_inputs.json")),
              ("development_0.8_verified_browser", "release_0.8_browser", (".json", ".log", "_execution.json", "_inputs.json")))
    for original, release, suffixes in copies:
        for suffix in suffixes:
            assert (VALIDATION / (original + suffix)).read_bytes() == (VALIDATION / (release + suffix)).read_bytes()

    initial = load("release_0.8_initial_archive.json")
    assert len(initial["runtime_files"]) == initial["runtime_files_count"] == 237
    with zipfile.ZipFile(archive) as bundle:
        for row in initial["runtime_files"]:
            verify_row(row)
            data = bundle.read("OceanRoute/" + row["path"])
            assert len(data) == row["bytes"] and sha(data) == row["sha256"], row["path"]

    wheel_path = ROOT / initial["wheel"]["path"]
    verify_row(initial["wheel"])
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_name = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(wheel.read(metadata_name).decode())
        assert metadata["Name"] == "oceanroute" and metadata["Version"] == "0.8.0"
        package = {name for name in wheel.namelist() if name.startswith("oceanroute/")}
        expected = {p.relative_to(ROOT).as_posix() for p in (ROOT / "oceanroute").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        assert package == expected
        for name in package:
            assert wheel.read(name) == (ROOT / name).read_bytes(), name
        record_name = next(name for name in wheel.namelist() if name.endswith(".dist-info/RECORD"))
        records = list(csv.reader(io.StringIO(wheel.read(record_name).decode())))
        assert len(records) == len(wheel.namelist()) and {row[0] for row in records} == set(wheel.namelist())
        for name, digest, size in records:
            if name == record_name:
                assert digest == size == ""
                continue
            data = wheel.read(name)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert digest == "sha256=" + encoded and int(size) == len(data), name

    wheel_smoke = load("release_0.8_wheel_smoke.json")
    assert wheel_smoke["wheel_sha256"] == initial["wheel"]["sha256"]
    modules = dict(wheel_smoke["isolated_modules"])
    for branch in ("heterogeneous_and_current_equilibrium", "native_s57"):
        assert wheel_smoke[branch]["status"] == "passed"
        modules.update(wheel_smoke[branch]["isolated_modules"])
    assert len(modules) == 32 and set(modules.values()) <= package
    portable = load("release_0.8_portable_smoke.json")
    assert portable["clean_venv"] and portable["launcher_install"] and portable["http_ui"] and portable["real_analysis"]
    assert portable["http_health"]["version"] == "0.8.0"
    assert "1566 passed, 1 warning in 86.47s" in portable["tests_output"]
    assert len(portable["synthetic_physical_http_examples"]) == 6
    assert sum(bool(row.get("split_json_resume_exact_six_arrays")) for row in portable["synthetic_physical_http_examples"].values()) == 4
    for evidence, requests in ((wheel_smoke["native_s57"], 14), (portable["native_s57"], 11)):
        assert evidence["status"] == "passed" and evidence["actual_http_requests"] == requests
        assert evidence["base_update_number"] == 0 and evidence["applied_update_number"] == 2
        assert evidence["soundg_xyz_points"] == 657 and evidence["path_assembly_material_inventory_unchanged"]
        assert evidence["fixture"]["sha256"] == sha((ROOT / "tests/fixtures/s57/noaa/US5A1KMJ.zip").read_bytes())

    served = load("release_0.8_served_assets.json")
    directories = [ROOT / path for path in served["equal_directories"]]
    assets = served["assets"]
    assert len(assets) == 8
    for directory in directories:
        names = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        assert names == {row["path"] for row in assets}
        for row in assets:
            data = (directory / row["path"]).read_bytes()
            assert len(data) == row["bytes"] and sha(data) == row["sha256"], row["path"]
    with urllib.request.urlopen(served["url"] + "/api/health", timeout=10) as response:
        assert json.load(response) == served["health"]
    for row in assets:
        with urllib.request.urlopen(served["url"] + "/" + row["path"], timeout=10) as response:
            assert response.read() == (directories[0] / row["path"]).read_bytes()
    manual = (ROOT / "docs/USER_MANUAL.md").read_bytes()
    assert manual == (ROOT / "oceanroute/manual.md").read_bytes()
    with urllib.request.urlopen(served["url"] + "/api/manual", timeout=10) as response:
        assert json.load(response)["text"].encode() == manual

    qa = load("release_0.8_pdf_qa.json")
    assert qa["status"] == "passed" and qa["all_pages_actually_visually_verified"] and qa["total_pages"] == 33
    for key in ("builder", "cover"):
        verify_row(qa[key])
    for document in qa["documents"]:
        for key in ("pdf", "source", "review"):
            verify_row(document[key])
        assert not document["blocking_defects"]
        assert document["actual_visual_review_pages"] == document["pdf"]["pages"] == len(document["renders"])
        for row in document["renders"]:
            verify_row(row)
    for document in load("release_0.8_pdf_character_bounds.json")["documents"]:
        verify_row(document)
        assert document["character_boundary_outliers"] == []
    correction = load("release_0.8_screenshot_provenance_correction.json")
    assert correction["screenshot_count"] == len(correction["screenshots"]) == 76
    for row in correction["screenshots"]:
        verify_row(row)
        assert (ROOT / row["path"]).read_bytes() == (ROOT / row["preserved_execution_bytes_path"]).read_bytes()
    legacy = correction["legacy_0.4_restored_from_unchanged_frozen_archive"]
    legacy_archive = ROOT / "outputs/releases/OceanRoute-0.4-portable.zip"
    assert sha(legacy_archive.read_bytes()) == legacy["archive_sha256"]
    assert len(legacy["members"]) == 47
    with zipfile.ZipFile(legacy_archive) as bundle:
        for row in legacy["members"]:
            verify_row(row)
            assert bundle.read("OceanRoute/" + row["path"]) == (ROOT / row["path"]).read_bytes()
    assert (ROOT / "web/artifacts/release-0.8/pdf-cover.png").read_bytes() == (ROOT / "web/artifacts/dev-0.8/async-transactions-final-baseline/production-workspace.png").read_bytes()
    frozen = load("development_0.8_frozen_artifacts.json")["artifacts"]
    assert len(frozen) == 48
    for row in frozen:
        verify_row(row)

    report = {
        "version": "0.8.0", "status": "passed",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Read-only evidence/current-byte verification and actual preview HTTP reads. No test, wheel smoke or clean installation rerun. Archive check covers the 237 initial-install runtime inputs only; complete final ZIP member audit is separate.",
        "input_verification": inputs,
        "initial_install_runtime_files": 237, "current_and_selected_archive_runtime_parity": True,
        "wheel": {"package_files": len(package), "record_entries": len(records), "sha256": initial["wheel"]["sha256"], "source_parity": True, "record_verified": True},
        "execution_evidence": {"backend_testcases": 1566, "browser_tests_each_once_passed": 87, "byte_identical_archival_copies": True, "wheel_smoke_module_union": 32, "clean_install_backend_tests": 1566, "native_s57_http_requests": {"wheel": 14, "clean_install": 11}},
        "actual_preview_http_asset_parity": {"url": served["url"], "asset_count": 8, "directories": served["equal_directories"], "manual": True},
        "pdf_pages": {document["kind"]: document["pdf"]["pages"] for document in qa["documents"]},
        "pdf_sources_reviews_renders_builder_cover_match": True,
        "screenshots": {"final_full_browser": 76, "separate_final_baseline_pdf_cover": 1, "legacy_restored": 47},
        "historical_frozen_artifacts_unchanged": 48,
        "limitations": "Existing execution reports are verified rather than reexecuted. Native S-57 retains chart references, not engineering depth conversion; synthetic numerical checks do not establish original-product equivalence or field accuracy."
    }
    if verify_only:
        saved = json.loads(report_path.read_text())
        assert {key: value for key, value in saved.items() if key != "recorded_at_utc"} == {key: value for key, value in report.items() if key != "recorded_at_utc"}
    else:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "outputs/releases/OceanRoute-0.8-portable.zip")
    parser.add_argument("--verify-only", action="store_true", help="Check existing evidence without rewriting it")
    options = parser.parse_args()
    verify(options.archive, verify_only=options.verify_only)
