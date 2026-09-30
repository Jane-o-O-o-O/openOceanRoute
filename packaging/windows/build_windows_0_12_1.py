"""Build an isolated 0.12.1 installer from explicit, byte-recorded inputs.

--prepare-only copies the frozen production runtime without the old OceanRoute
package. It neither compiles an EXE nor claims an installation test. Final build
requires the new wheel, compiled frontend and both new PDFs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "packaging/windows"
VERSION = "0.12.1"
PAYLOAD = BASE / "payload-0.12.1"
FORBIDDEN = {"pytest", "httpx", "httpcore", "iniconfig", "pluggy", "pygments", "pip", "setuptools", "wheel"}


def legacy_owned_paths():
    old = BASE / "payload"
    paths = [Path("windows_app.py"), Path("documents/OceanRoute_用户手册_0.12.pdf"),
             Path("documents/OceanRoute_设计文档_0.12.pdf")]
    paths += [p.relative_to(old) for p in files(old / "runtime/Lib/site-packages/oceanroute-0.12.0.dist-info")]
    require(len(paths) == 11, "Original owned upgrade cleanup must be exactly the audited 11 files")
    audit = json.loads((ROOT / "resources/validation/development_0.12.1_windows_upgrade_inventory_first.json").read_text())
    require({p.as_posix() for p in paths} == {r["relative_owned_path"] for r in audit["legacy_owned_files"]},
            "Legacy cleanup differs from the actual original-payload audit")
    for row in audit["legacy_owned_files"]:
        require(descriptor(ROOT / row["original_frozen_payload"]["path"]) == row["original_frozen_payload"],
                "Original frozen legacy file changed")
    return sorted(paths)


def write_upgrade_cleanup():
    lines = ["; Exactly 11 old OceanRoute-owned files confirmed against the frozen original payload."]
    lines += ['Delete "$INSTDIR\\' + p.as_posix().replace("/", "\\") + '"' for p in legacy_owned_paths()]
    lines += ['RMDir "$INSTDIR\\runtime\\Lib\\site-packages\\oceanroute-0.12.0.dist-info"']
    (BASE / "upgrade-owned-0.12.nsh").write_text("\n".join(lines) + "\n", encoding="utf-8")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def descriptor(path):
    path = path.resolve()
    data = path.read_bytes()
    return {"path": path.relative_to(ROOT).as_posix(), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def files(directory):
    return sorted(p for p in directory.rglob("*") if p.is_file()
                  and "__pycache__" not in p.parts and p.suffix != ".pyc")


def distributions(site):
    result = {}
    for path in sorted(site.glob("*.dist-info")):
        metadata = Parser().parsestr((path / "METADATA").read_text(encoding="utf-8"))
        name = metadata["Name"].lower().replace("_", "-")
        require(name not in result, f"Duplicate distribution: {name}")
        result[name] = metadata["Version"]
    require(not FORBIDDEN.intersection(result), "Test/build tools present in production runtime")
    return result


def prepare():
    source = BASE / "payload/runtime"
    before = [descriptor(p) for p in files(source)]
    old_site = source / "Lib/site-packages"
    pinned = distributions(old_site)
    require(len(pinned) == 32 and pinned.pop("oceanroute", None) == "0.12.0",
            "Expected original 32 distributions, including OceanRoute 0.12.0")
    if not (PAYLOAD / "runtime").exists():
        shutil.copytree(source, PAYLOAD / "runtime", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        site = PAYLOAD / "runtime/Lib/site-packages"
        shutil.rmtree(site / "oceanroute")
        for path in site.glob("oceanroute-*.dist-info"):
            shutil.rmtree(path)
    site = PAYLOAD / "runtime/Lib/site-packages"
    current = distributions(site)
    current.pop("oceanroute", None)
    require(current == pinned, "Prepared third-party versions differ from frozen runtime")
    retained = []
    for original in files(source):
        relative = original.relative_to(source)
        if relative.parts[:3] == ("Lib", "site-packages", "oceanroute") or any(part.startswith("oceanroute-") and part.endswith(".dist-info") for part in relative.parts):
            continue
        target = PAYLOAD / "runtime" / relative
        require(target.read_bytes() == original.read_bytes(), f"Dependency bytes changed: {relative}")
        retained.append(descriptor(target))
    require(before == [descriptor(p) for p in files(source)], "Frozen source runtime changed during preparation")
    shutil.copyfile(BASE / "windows_app_0_12_1.py", PAYLOAD / "windows_app_0_12_1.py")
    return {"source_runtime": before, "third_party_distributions": pinned,
            "third_party_distribution_count": len(pinned), "final_distribution_count_including_application": 32,
            "retained_runtime_files": retained, "old_runtime_unchanged": True,
            "test_tools_excluded": sorted(FORBIDDEN)}


def inject_wheel(wheel, frontend):
    with zipfile.ZipFile(wheel) as archive:
        require(archive.testzip() is None, "Application wheel CRC failed")
        entries = archive.infolist()
        names = [row.filename for row in entries]
        require(len(names) == len(set(names)), "Duplicate wheel paths")
        for row in entries:
            path = PurePosixPath(row.filename)
            require(path.as_posix() == row.filename and not path.is_absolute()
                    and ".." not in path.parts and "\\" not in row.filename
                    and ":" not in row.filename and not any(ord(c) < 32 for c in row.filename)
                    and (row.external_attr >> 16) & 0o170000 != 0o120000,
                    f"Unsafe wheel path: {row.filename}")
            require(path.parts[0] in {"oceanroute", "oceanroute-0.12.1.dist-info"},
                    f"Unexpected wheel top-level member: {row.filename}")
        metadata = Parser().parsestr(archive.read("oceanroute-0.12.1.dist-info/METADATA").decode())
        require(metadata["Name"].lower() == "oceanroute" and metadata["Version"] == VERSION,
                "Application wheel is not OceanRoute 0.12.1")
        front = {p.relative_to(frontend).as_posix(): p.read_bytes() for p in files(frontend)}
        require(front and "index.html" in front, "Missing compiled frontend")
        wheel_front = {name.removeprefix("oceanroute/static/"): archive.read(name)
                       for name in names if name.startswith("oceanroute/static/") and not name.endswith("/")}
        require(front == wheel_front, "New wheel static bytes differ from supplied frontend")
        site = PAYLOAD / "runtime/Lib/site-packages"
        source_package = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in files(ROOT / "oceanroute")}
        wheel_package = {name: archive.read(name) for name in names if name.startswith("oceanroute/") and not name.endswith("/")}
        require(source_package == wheel_package, "New wheel package differs from current frozen product source")
        if not (site / "oceanroute").exists():
            archive.extractall(site)
        for name in names:
            if not name.endswith("/"):
                require((site / name).read_bytes() == archive.read(name), f"Wheel extraction differs: {name}")
    installed = distributions(site)
    require(len(installed) == 32 and installed["oceanroute"] == VERSION, "Final production distribution count/version differs")


def write_uninstall_list():
    lines = ["; Exact 0.12.1 production files; unrelated files and external user databases are preserved."]
    directories = set()
    for path in files(PAYLOAD):
        relative = path.relative_to(PAYLOAD)
        require(not any(c in relative.as_posix() for c in '$"\r\n'), f"Unsafe NSIS path: {relative}")
        lines.append('Delete "$INSTDIR\\' + str(relative).replace("/", "\\") + '"')
        directories.update(parent for parent in relative.parents if parent != Path("."))
    for relative in legacy_owned_paths():
        lines.append('Delete "$INSTDIR\\' + relative.as_posix().replace("/", "\\") + '"')
        directories.update(parent for parent in relative.parents if parent != Path("."))
    lines.append('Delete "$INSTDIR\\Uninstall.exe"')
    for directory in sorted(directories, key=lambda p: (-len(p.parts), p.as_posix())):
        lines.append('RMDir "$INSTDIR\\' + str(directory).replace("/", "\\") + '"')
    lines.append('RMDir "$INSTDIR"')
    (BASE / "uninstall-files-0.12.1.nsh").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--stage-wheel-only", action="store_true",
                        help="Verify and stage the new wheel/static package without PDFs or NSIS compilation")
    for name in ("wheel", "frontend", "manual", "design", "instructions"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--makensis", type=Path, default=BASE / "tooling/nsis/mac/makensis")
    parser.add_argument("--stem", default="development_0.12.1_windows_build")
    options = parser.parse_args()
    require(options.stem.replace("_", "").replace(".", "").isalnum(), "Unsafe report stem")
    report = ROOT / "resources/validation" / (options.stem + ".json")
    log_path = report.with_suffix(".log")
    require(not report.exists() and not log_path.exists(), "Build evidence already exists; use a fresh stem")
    require(not (options.prepare_only and options.stage_wheel_only), "Select only one preparation mode")
    if options.stage_wheel_only:
        require(options.wheel and options.frontend, "Wheel staging requires --wheel --frontend")
    elif not options.prepare_only:
        require(all(getattr(options, name) for name in ("wheel", "frontend", "manual", "design")),
                "Final build requires --wheel --frontend --manual --design")
        require(not (ROOT / "outputs/releases/OceanRoute-0.12.1-Windows-x64-Setup.exe").exists(),
                "Preserve existing 0.12.1 candidate before rebuilding")
    preparation = prepare()
    result = {"version": VERSION, "status": "prepared", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "preparation": preparation, "installer_built": False,
              "scope": "Isolated production dependency preparation; no Windows execution or test claim."}
    if options.stage_wheel_only:
        before = [descriptor(options.wheel.resolve()), *[descriptor(p) for p in files(options.frontend.resolve())],
                  *[descriptor(p) for p in files(ROOT / "oceanroute")]]
        inject_wheel(options.wheel.resolve(), options.frontend.resolve())
        require(before == [descriptor(options.wheel.resolve()), *[descriptor(p) for p in files(options.frontend.resolve())],
                           *[descriptor(p) for p in files(ROOT / "oceanroute")]], "Stage inputs changed")
        package_files = [descriptor(p) for p in files(PAYLOAD / "runtime/Lib/site-packages/oceanroute")]
        result.update(status="staged", input_files=before, staged_package_files=package_files,
                      package_file_count=len(package_files), python_file_count=sum(p["path"].endswith(".py") for p in package_files),
                      static_file_count=len(files(PAYLOAD / "runtime/Lib/site-packages/oceanroute/static")),
                      scope="Actual new Windows production wheel staged and matched to frozen source/static; no installer or Windows execution claim.")
    elif not options.prepare_only:
        write_upgrade_cleanup()
        input_paths = [options.wheel.resolve(), options.manual.resolve(), options.design.resolve(),
                       *files(options.frontend.resolve()), BASE / "windows_app_0_12_1.py",
                       BASE / "launcher_0_12_1.nsi", BASE / "installer_0_12_1.nsi",
                       BASE / "upgrade-owned-0.12.nsh", Path(__file__).resolve()]
        if options.instructions:
            input_paths.append(options.instructions.resolve())
        before = [descriptor(p) for p in input_paths]
        inject_wheel(options.wheel.resolve(), options.frontend.resolve())
        documents = PAYLOAD / "documents"
        documents.mkdir(exist_ok=True)
        for source, suffix in [(options.manual, "用户手册"), (options.design, "设计文档")]:
            require(source.read_bytes().startswith(b"%PDF-"), f"Not a PDF: {source}")
            shutil.copyfile(source, documents / f"OceanRoute_{suffix}_0.12.1.pdf")
        if options.instructions:
            shutil.copyfile(options.instructions, documents / "Windows_安装说明.txt")
        commands = []
        with log_path.open("x", encoding="utf-8") as log:
            for script in ("launcher_0_12_1.nsi", "installer_0_12_1.nsi"):
                if script.startswith("installer"):
                    write_uninstall_list()
                command = [str(options.makensis.resolve()), script]
                completed = subprocess.run(command, cwd=BASE, stdout=log, stderr=subprocess.STDOUT,
                                           env={**os.environ, "NSISDIR": str(BASE / "tooling/nsis")})
                commands.append({"command": command, "exit_code": completed.returncode})
                require(completed.returncode == 0, f"NSIS failed: {script}; actual log preserved at {log_path}")
        require(before == [descriptor(p) for p in input_paths], "Build inputs changed during compilation")
        require(distributions(PAYLOAD / "runtime/Lib/site-packages") == {**preparation["third_party_distributions"], "oceanroute": VERSION},
                "Final production dependency versions differ")
        result.update(status="built", installer_built=True, input_files=before, commands=commands,
                      payload_files=[descriptor(p) for p in files(PAYLOAD)],
                      uninstall_list=descriptor(BASE / "uninstall-files-0.12.1.nsh"),
                      legacy_upgrade_cleanup=descriptor(BASE / "upgrade-owned-0.12.nsh"),
                      installer=descriptor(ROOT / "outputs/releases/OceanRoute-0.12.1-Windows-x64-Setup.exe"),
                      scope="Actual NSIS compilation from explicit recorded inputs; installation/execution verification is separate.")
    with report.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2); stream.write("\n")
    print(json.dumps({"status": result["status"], "installer_built": result["installer_built"], "report": str(report.relative_to(ROOT))}))


if __name__ == "__main__":
    main()
