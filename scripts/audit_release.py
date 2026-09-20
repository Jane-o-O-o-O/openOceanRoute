"""Read-only archive/wheel/source audit; write an external JSON evidence report."""
from __future__ import annotations

import argparse
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
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def audit(archive: Path, wheel: Path, report: Path, frozen: Path, *, verify_only: bool = False) -> dict:
    report = report.resolve()
    frozen_rows = json.loads(frozen.read_text())["artifacts"]
    for row in frozen_rows:
        data = (ROOT / row["path"]).read_bytes()
        assert len(data) == row["bytes"] and digest(data) == row["sha256"], row["path"]

    members = []
    self_member = "OceanRoute/" + report.relative_to(ROOT).as_posix()
    expected_version = json.loads((ROOT / "web/package.json").read_text())["version"]
    assert re.search(r'^version = "([^"\n]+)"$', (ROOT / "pyproject.toml").read_text(), re.M).group(1) == expected_version
    assert re.search(r'^__version__ = "([^"\n]+)"$', (ROOT / "oceanroute/__init__.py").read_text(), re.M).group(1) == expected_version
    label = expected_version.removesuffix(".0")
    # Independently enumerate the release contract, including documentation and
    # examples. Checking only present members would miss an omitted deliverable.
    required_files = ["README.md", "pyproject.toml", "launcher.py", "web/package.json",
                      "web/package-lock.json", "web/index.html", "web/tsconfig.json",
                      "web/vite.config.ts", "web/playwright.config.ts",
                      f"output/pdf/OceanRoute_用户手册_{label}.pdf",
                      f"output/pdf/OceanRoute_设计文档_{label}.pdf",
                      "resources/research/manual_findings.md", "resources/research/website_findings.md",
                      "resources/research/web_sources.json", "resources/build_product_documents.py",
                      "resources/validation/voyage_1800s.json"]
    if tuple(map(int, expected_version.split("."))) >= (0, 8, 0):
        required_files.append("resources/research/s57_sources.json")
    if tuple(map(int, expected_version.split("."))) >= (0, 9, 0):
        required_files.extend(["resources/run_0_9_backend_gate.py",
                               "resources/run_0_9_browser_gate.py",
                               "resources/run_0_9_ui_build.py",
                               "resources/validate_0_9_pdf_structure.py"])
    if tuple(map(int, expected_version.split("."))) >= (0, 10, 0):
        required_files.extend(["resources/run_0_10_backend_gate.py",
                               "resources/run_0_10_browser_gate.py",
                               "resources/validate_0_10_pdf_structure.py",
                               "resources/documentation_0_10_automatic_rules_contract.md"])
    source_paths = {ROOT / name for name in required_files}
    for folder in ["oceanroute", "docs", "examples", "tests", "scripts", "web/src", "web/public",
                   "web/tests", f"web/artifacts/release-{label}", "resources/validation"]:
        source_paths.update(p for p in (ROOT / folder).rglob("*") if p.is_file()
                            and "__pycache__" not in p.parts and p.suffix != ".pyc")
    assert all(p.is_file() for p in source_paths), "Required release file is missing"
    expected_names = {"OceanRoute/" + p.relative_to(ROOT).as_posix() for p in source_paths}
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert len(names) == len(set(names)), "Duplicate ZIP members"
        assert set(names) - {self_member} == expected_names - {self_member}, "ZIP member set differs from release contract"
        if verify_only:
            assert self_member in names, "Final ZIP must contain its audit evidence"
        assert bundle.testzip() is None, "ZIP CRC failure"
        for info in bundle.infolist():
            path = PurePosixPath(info.filename)
            assert path.as_posix() == info.filename, "Noncanonical ZIP member name"
            assert not path.is_absolute() and ".." not in path.parts
            assert path.parts[0] == "OceanRoute" and len(path.parts) > 1
            assert "\\" not in info.filename and not info.is_dir()
            assert not stat.S_ISLNK(info.external_attr >> 16), info.filename
            assert not any(part in {".venv", "node_modules", "__pycache__"}
                           or part.startswith(".oceanroute") for part in path.parts)
            assert path.suffix not in {".sqlite3", ".db", ".pyc"}, info.filename
            assert path.name not in {"MakaiPlan_Manual_6.2.0.pdf", "MakaiPlan.pdf", "MakaiPlanPro.pdf"}
            # An embedded audit report describes all other members. Its own
            # final bytes are checked separately, avoiding a recursive digest.
            if info.filename == self_member:
                assert bundle.read(info.filename) == report.read_bytes(), "Embedded audit report differs"
                continue
            source = ROOT.joinpath(*path.parts[1:])
            payload = bundle.read(info.filename)
            assert payload == source.read_bytes(), info.filename
            members.append({"path": info.filename, "bytes": len(payload), "sha256": digest(payload)})

    with zipfile.ZipFile(wheel) as package:
        assert len(package.namelist()) == len(set(package.namelist()))
        assert package.testzip() is None, "Wheel CRC failure"
        for info in package.infolist():
            path = PurePosixPath(info.filename)
            assert path.as_posix() == info.filename, "Noncanonical wheel member name"
            assert not path.is_absolute() and ".." not in path.parts and "\\" not in info.filename
            assert not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16)
        metadata_paths = [n for n in package.namelist() if n.endswith(".dist-info/METADATA")]
        assert len(metadata_paths) == 1
        metadata = Parser().parsestr(package.read(metadata_paths[0]).decode())
        assert metadata["Name"].lower() == "oceanroute" and metadata["Version"] == expected_version
        expected = {p.relative_to(ROOT).as_posix(): p for p in (ROOT / "oceanroute").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        actual = {n for n in package.namelist() if n.startswith("oceanroute/")}
        assert actual == set(expected), "Wheel package member mismatch"
        for name, source in expected.items():
            assert package.read(name) == source.read_bytes(), name
        records = [n for n in package.namelist() if n.endswith(".dist-info/RECORD")]
        assert len(records) == 1
        rows = list(csv.reader(io.StringIO(package.read(records[0]).decode())))
        assert len(rows) == len(package.namelist()) and {r[0] for r in rows} == set(package.namelist())
        for name, hash_value, size in rows:
            if name == records[0]:
                assert hash_value == size == ""
                continue
            data = package.read(name)
            expected_hash = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert hash_value == "sha256=" + expected_hash and int(size) == len(data), name
        with zipfile.ZipFile(archive) as bundle:
            for name in actual:
                assert package.read(name) == bundle.read("OceanRoute/" + name), name

    result = {"version": expected_version, "status": "passed",
              "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "audited_non_self_zip_members": len(members), "members": members,
              "wheel_members": len(rows), "wheel_package_files": len(actual),
              "wheel_record_verified": True, "zip_wheel_source_parity": True,
              "historical_frozen_artifacts": len(frozen_rows),
              "path_crc_and_no_developer_data_checks": True,
              "self_reference": "Final ZIP SHA256 is external; this report excludes its own embedded member."}
    if verify_only:
        saved = json.loads(report.read_text())
        assert saved["status"] == "passed" and saved["version"] == expected_version
        assert saved["members"] == members, "Final ZIP differs from audited member evidence"
        assert saved["wheel_members"] == len(rows) and saved["wheel_package_files"] == len(actual)
    else:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "members"}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, default=ROOT / "resources/validation/development_0.6_frozen_artifacts.json")
    parser.add_argument("--verify-only", action="store_true", help="Check the final embedded report without changing any files")
    options = parser.parse_args()
    audit(options.archive, options.wheel, options.report, options.frozen, verify_only=options.verify_only)
