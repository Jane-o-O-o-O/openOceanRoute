"""QA only: Windows Python waits on the actual NSIS uninstall process handle.

Run from the external production payload runtime, never the runtime being
uninstalled. The copied uninstaller lives in Windows TEMP. NSIS's final _?=
argument disables another self-copy; Popen.wait waits for this exact Win32 PID.
This helper is not shipped in the product payload.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("This QA helper must execute in the external Windows CPython")
    install = args.install_dir.resolve()
    if not install.is_absolute() or not install.is_dir() or install == Path(install.anchor):
        raise ValueError("Expected a non-root absolute installed directory")
    if args.report.exists() or args.timeout <= 0:
        raise ValueError("Use new evidence and a positive timeout")
    source = install / "Uninstall.exe"
    original = source.read_bytes()
    temporary_directory = Path(tempfile.mkdtemp(prefix="OceanRoute-0.12.1-uninstall-"))
    if temporary_directory.resolve().is_relative_to(install):
        raise RuntimeError("Uninstaller copy must be outside the installation")
    copied = temporary_directory / "Uninstall.exe"
    shutil.copyfile(source, copied)
    if copied.read_bytes() != original:
        raise RuntimeError("Uninstaller copy bytes differ")
    # Raw Windows command line preserves NSIS's required unquoted final _?=
    # tail, including an install path that contains spaces.
    command = f'"{copied}" /S _?={install}'
    started = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    result = {"version": "0.12.1", "status": "failed", "install_dir": str(install),
              "source_uninstaller": str(source), "temporary_uninstaller": str(copied),
              "uninstaller_bytes": len(original), "uninstaller_sha256": hashlib.sha256(original).hexdigest(),
              "byte_identical_copy": True, "command": command, "started_at_utc": started,
              "wait_method": "Windows subprocess.Popen.wait on this exact process handle; no file-disappearance predicate",
              "self_copy_disabled_by_final_inst_dir_argument": True}
    process = None
    try:
        process = subprocess.Popen(command, shell=False)
        result["actual_windows_pid"] = process.pid
        result["actual_exit_code"] = process.wait(timeout=args.timeout)
        result["actual_process_exited"] = True
        if result["actual_exit_code"] != 0:
            raise RuntimeError(f"Uninstaller exited {result['actual_exit_code']}")
        result["status"] = "passed"
    except Exception as error:
        result.update(error=str(error), traceback=traceback.format_exc(),
                      actual_process_exited=process is not None and process.poll() is not None)
    finally:
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        result["wall_time_s"] = time.monotonic() - clock
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2); stream.write("\n")
        # Leave a live/timed-out process and its file for diagnosis, never start
        # a reinstall while its old deletion instructions may still execute.
        if result.get("actual_process_exited"):
            shutil.rmtree(temporary_directory)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
