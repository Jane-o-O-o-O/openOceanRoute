"""Actual NSIS-installed Windows runtime through private macOS Wine, not native Windows."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import time
import traceback
import argparse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.parser import Parser

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "packaging/windows"
WINE = BASE / "tooling/Wine Stable.app/Contents/Resources/wine/bin/wine"
PREFIX = BASE / "wine-prefix-0.12.1"
INSTALL = PREFIX / "drive_c/users" / os.environ["USER"] / "AppData/Local/Programs/OceanRoute"
DATA = PREFIX / "drive_c/users" / os.environ["USER"] / "AppData/Local/OceanRoute"
ENV = {**os.environ, "WINEPREFIX": str(PREFIX), "WINEDEBUG": "-all"}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--stem", default="release_0.12.1_windows_installer_execution")
parser.add_argument("--prefix", type=Path, default=PREFIX)
options = parser.parse_args()
PREFIX = options.prefix.resolve()
INSTALL = PREFIX / "drive_c/users" / os.environ["USER"] / "AppData/Local/Programs/OceanRoute"
DATA = PREFIX / "drive_c/users" / os.environ["USER"] / "AppData/Local/OceanRoute"
ENV = {**os.environ, "WINEPREFIX": str(PREFIX), "WINEDEBUG": "-all"}
if not options.stem.replace("_", "").replace(".", "").isalnum():
    raise ValueError("Invalid report stem")
OUT = ROOT / "resources/validation" / (options.stem + ".json")
LOG = ROOT / "resources/validation" / (options.stem + ".log")
events = []


def descriptor(p):
    data = p.read_bytes()
    return {"path": p.relative_to(ROOT).as_posix(), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def invoke(path, *args):
    with LOG.open("a", encoding="utf-8") as log:
        result = subprocess.run([str(WINE), str(path), *args], env=ENV,
                                stdout=log, stderr=subprocess.STDOUT, timeout=180)
    events.append({"command": [str(path.relative_to(ROOT)), *args], "exit_code": result.returncode})
    if result.returncode:
        raise RuntimeError(f"Windows command failed: {path.name}, exit {result.returncode}")


def running():
    return json.loads((DATA / "running.json").read_text(encoding="utf-8"))


def ready():
    for _ in range(600):
        try:
            row = running()
            url = f"http://127.0.0.1:{row['port']}"
            with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                assert json.load(response) == {"status": "ok", "version": "0.12.1", "product": "OceanRoute"}
            return url, row
        except (OSError, ValueError, AssertionError):
            time.sleep(0.1)
    raise RuntimeError("Installed Windows application did not become ready")


def start():
    invoke(INSTALL / "OceanRoute.exe")
    return ready()


def stop():
    invoke(INSTALL / "OceanRoute.exe", "--stop")
    assert not (DATA / "running.json").exists(), "Stop returned before server cleanup"


def production_files(directory):
    return sorted(p for p in directory.rglob("*") if p.is_file()
                  and "__pycache__" not in p.parts and p.suffix != ".pyc")


def installed_files_equal(package):
    rows = []
    for source in production_files(package):
        installed = INSTALL / source.relative_to(package)
        assert installed.read_bytes() == source.read_bytes(), str(installed)
        rows.append(descriptor(installed))
    return rows


def distribution_inventory(site):
    rows = []
    for directory in sorted(site.glob("*.dist-info")):
        metadata = Parser().parsestr((directory / "METADATA").read_text(encoding="utf-8"))
        rows.append({"directory": directory.name,
                     "name": metadata["Name"].lower().replace("_", "-"),
                     "version": metadata["Version"], "metadata": descriptor(directory / "METADATA")})
    return rows


def verify_distribution_inventory(package, version):
    expected = distribution_inventory(package / "runtime/Lib/site-packages")
    actual = distribution_inventory(INSTALL / "runtime/Lib/site-packages")
    names = [(r["name"], r["version"]) for r in actual]
    assert len(actual) == 32 and len(set(n for n, _ in names)) == 32, "Expected exactly 32 unique production distributions"
    assert names == [(r["name"], r["version"]) for r in expected], "Installed production distribution versions differ"
    assert [r for r in names if r[0] == "oceanroute"] == [("oceanroute", version)], "Unexpected OceanRoute metadata version"
    return actual


def legacy_owned_paths():
    old = BASE / "payload"
    paths = [Path("windows_app.py"), Path("documents/OceanRoute_用户手册_0.12.pdf"),
             Path("documents/OceanRoute_设计文档_0.12.pdf")]
    paths += [p.relative_to(old) for p in production_files(old / "runtime/Lib/site-packages/oceanroute-0.12.0.dist-info")]
    assert len(paths) == 11
    audit = json.loads((ROOT / "resources/validation/development_0.12.1_windows_upgrade_inventory_first.json").read_text())
    assert {p.as_posix() for p in paths} == {r["relative_owned_path"] for r in audit["legacy_owned_files"]}
    for row in audit["legacy_owned_files"]:
        assert descriptor(ROOT / row["original_frozen_payload"]["path"]) == row["original_frozen_payload"]
    return sorted(paths)


def verify_no_legacy_owned_files():
    paths = legacy_owned_paths()
    assert all(not (INSTALL / p).exists() for p in paths), "Old owned wrapper, PDF or metadata files survive upgrade"
    assert not (INSTALL / "runtime/Lib/site-packages/oceanroute-0.12.0.dist-info").exists(), "Old metadata directory survives"
    return [p.as_posix() for p in paths]


def windows_path(path):
    path = path.resolve()
    drive_c = PREFIX / "drive_c"
    if path.is_relative_to(drive_c):
        return "C:" + chr(92) + path.relative_to(drive_c).as_posix().replace("/", chr(92))
    return "Z:" + str(path).replace("/", chr(92))


def wait_actual_uninstall(package, phase):
    child_report = BASE / "qa-evidence" / (options.stem + "_" + phase + "_uninstall_process.json")
    assert not child_report.exists(), "Use fresh child-process evidence"
    external_python = package / "runtime/python.exe"
    assert not external_python.resolve().is_relative_to(INSTALL.resolve())
    helper = BASE / "windows_uninstall_wait_0_12_1.py"
    invoke(external_python, "-B", windows_path(helper), "--install-dir", windows_path(INSTALL),
           "--report", windows_path(child_report))
    actual_uninstall = json.loads(child_report.read_text(encoding="utf-8"))
    assert actual_uninstall["status"] == "passed" and actual_uninstall["actual_process_exited"] is True
    assert type(actual_uninstall["actual_windows_pid"]) is int and actual_uninstall["actual_windows_pid"] > 0
    assert actual_uninstall["actual_exit_code"] == 0
    assert not (INSTALL / "Uninstall.exe").exists()
    return actual_uninstall, child_report, helper, external_python


def main():
    started = time.monotonic()
    package = BASE / "payload-0.12.1"
    assert not OUT.exists() and not LOG.exists(), "Use fresh evidence stem"
    frozen_runtime_before = [descriptor(p) for p in production_files(package / "runtime")]
    # Establish an actual original-version installation for this upgrade test.
    # No reinstall starts until the prior uninstaller's actual process exits.
    reset_process = None
    if (INSTALL / "OceanRoute.exe").exists():
        stop()
    if (INSTALL / "Uninstall.exe").exists():
        reset_process, reset_report, _, _ = wait_actual_uninstall(package, "reset")
    old_installer = ROOT / "outputs/releases/OceanRoute-0.12.0-Windows-x64-Setup.exe"
    old_package = BASE / "payload"
    invoke(old_installer, "/S")
    old_files = installed_files_equal(old_package)
    old_distributions = verify_distribution_inventory(old_package, "0.12.0")
    upgrade_baseline = {"installer": descriptor(old_installer), "version": "0.12.0",
                        "installed_file_count": len(old_files), "installed_files": old_files,
                        "distributions": old_distributions, "all_original_payload_files_byte_identical": True,
                        "reset_actual_uninstall_process": reset_process,
                        "reset_actual_uninstall_process_report": descriptor(reset_report) if reset_process else None}
    invoke(ROOT / "outputs/releases/OceanRoute-0.12.1-Windows-x64-Setup.exe", "/S")
    actual_files = installed_files_equal(package)
    upgraded_distributions = verify_distribution_inventory(package, "0.12.1")
    removed_legacy_paths = verify_no_legacy_owned_files()
    url, first = start()
    _, second = start()
    assert first == second, "Double launch created a second server or changed its port"
    assets = []
    for p in sorted((ROOT / "oceanroute/static").rglob("*")):
        if not p.is_file():
            continue
        name = p.relative_to(ROOT / "oceanroute/static").as_posix()
        with urllib.request.urlopen(url + ("/" if name == "index.html" else "/" + name), timeout=10) as r:
            b = r.read()
        assert b == p.read_bytes(), name
        assets.append({"url_path": name, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()})
    workflows = {}
    owner_changes = []

    def post(path, value):
        request = urllib.request.Request(url + path, json.dumps(value, allow_nan=False).encode("utf-8"),
                                         {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                if path == "/api/export/csv":
                    return {"_http_status": response.status,
                            "_content_type": response.headers.get("Content-Type", ""),
                            "_text": response.read().decode("utf-8")}
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 422:
                raise
            return {"_http_status": 422, "_error": json.load(error)}

    for label, filename, function in [
        ("side_slopes", "side_slopes_smoke.py", "run_side_slopes_smoke"),
        ("automatic_rules", "automatic_rules_smoke.py", "run_automatic_rules_smoke"),
        ("altercourse", "altercourse_smoke.py", "run_altercourse_smoke"),
        ("arc_endpoint_editing", "arc_edit_smoke.py", "run_arc_edit_smoke"),
    ]:
        count = 0

        def get(path):
            nonlocal count, url
            count += 1
            if count == 2:
                before = running()
                stop()
                url, after = start()
                assert before["pid"] != after["pid"], "Saved reread used same process"
                owner_changes.append({"workflow": label, "before": before, "after": after})
            with urllib.request.urlopen(url + path, timeout=180) as response:
                return json.load(response)

        helper = ROOT / "scripts" / filename
        result = runpy.run_path(str(helper))[function](post, get)
        assert count >= 2
        workflows[label] = {**result, "harness": descriptor(helper),
                            "actual_process_closed_and_restarted": True, "actual_reads": count}
    physical = []
    for name, endpoint in [
        ("initial-equilibrium-dynamic.json", "/api/simulation/dynamic"),
        ("geographic-equilibrium-voyage.json", "/api/shipplan/prepare-voyage"),
        ("heterogeneous-initial-dynamic.json", "/api/simulation/dynamic"),
        ("heterogeneous-initial-plan-voyage.json", "/api/voyage/run"),
        ("current-initial-dynamic.json", "/api/simulation/dynamic"),
        ("current-initial-plan-voyage.json", "/api/voyage/run"),
    ]:
        example = ROOT / "examples" / name
        result = post(endpoint, json.loads(example.read_text()))
        assert result.get("_http_status", 200) == 200 and result.get("model"), name
        physical.append({"source": descriptor(example), "endpoint": endpoint,
                         "actual_http_status": 200, "returned_model": result["model"]})
    stop()
    databases = [p for p in DATA.rglob("*") if p.is_file() and p.suffix in {".db", ".sqlite3"}]
    before_data = [descriptor(p) for p in sorted(databases)]
    assert before_data, "No saved installed project database"
    sentinel = INSTALL / "unrelated-user-file.txt"
    sentinel.write_text("Uninstaller must preserve unrelated files.", encoding="utf-8")
    actual_uninstall, child_report, helper, external_python = wait_actual_uninstall(package, "final")
    assert all(not (ROOT / row["path"]).exists() for row in actual_files), "Some production files survive completed uninstall"
    assert sentinel.read_text() == "Uninstaller must preserve unrelated files."
    assert before_data == [descriptor(p) for p in sorted(databases)]
    sentinel.unlink()
    invoke(ROOT / "outputs/releases/OceanRoute-0.12.1-Windows-x64-Setup.exe", "/S")
    assert before_data == [descriptor(p) for p in sorted(databases)]
    for item in actual_files:
        assert descriptor(ROOT / item["path"]) == item
    reinstalled_distributions = verify_distribution_inventory(package, "0.12.1")
    assert verify_no_legacy_owned_files() == removed_legacy_paths
    url, final = start()
    assert frozen_runtime_before == [descriptor(p) for p in production_files(package / "runtime")], "Frozen external runtime changed"
    return {"version": "0.12.1", "status": "passed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "wall_time_s": time.monotonic() - started, "environment": "Windows x64 binaries through private macOS Wine 11.0 under Rosetta; not a native Windows machine",
            "installer": descriptor(ROOT / "outputs/releases/OceanRoute-0.12.1-Windows-x64-Setup.exe"),
            "probe": descriptor(Path(__file__).resolve()), "installed_file_count": len(actual_files),
            "actual_old_version_upgrade_baseline": upgrade_baseline,
            "upgraded_distributions": upgraded_distributions,
            "reinstalled_distributions": reinstalled_distributions,
            "production_distribution_count": 32, "oceanroute_metadata_only_version": "0.12.1",
            "removed_legacy_owned_paths": removed_legacy_paths,
            "no_legacy_owned_residue_after_upgrade_and_reinstall": True,
            "external_frozen_runtime_unchanged": True,
            "installed_files": actual_files, "actual_http_assets": assets, "double_launch_same_pid_and_port": True,
            "workflows": workflows, "actual_owner_changes": owner_changes, "synthetic_physical_examples": physical,
            "actual_uninstall_process": actual_uninstall, "actual_uninstall_process_report": descriptor(child_report),
            "uninstall_helper": descriptor(helper), "external_windows_python": descriptor(external_python),
            "uninstall_preserved_user_databases": before_data, "uninstall_preserved_unrelated_file": True,
            "reinstallation_file_bytes_identical": True, "final_running": final, "url": url,
            "events": events, "native_windows_tested": False,
            "scope": "Actual byte-verified old 0.12.0 installation upgraded to 0.12.1; exactly 32 production distributions, no old owned residue; installed EXE, double launch, HTTP workflows, process stop/reopen, exact-PID uninstall/data retention and byte-verified reinstall. Finite synthetic examples; not field validation or Windows kernel/driver certification."}


if __name__ == "__main__":
    try:
        result = main()
    except Exception as error:
        result = {"version": "0.12.1", "status": "failed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                  "error": str(error), "traceback": traceback.format_exc(), "events": events,
                  "environment": "macOS Wine 11.0 / Windows x64 binaries", "native_windows_tested": False}
        with OUT.open("x", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2); f.write("\n")
        raise
    with OUT.open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2); f.write("\n")
    print(json.dumps({k: result[k] for k in ["status", "wall_time_s", "installed_file_count", "url"]}))
