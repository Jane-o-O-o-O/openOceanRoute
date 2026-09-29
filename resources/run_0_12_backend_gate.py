"""Run the real backend suite with immutable input snapshots and JUnit evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    paths = {p for p in (ROOT / "oceanroute").rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    paths.update((ROOT / "tests").rglob("*.py"))
    paths.update(p for p in (ROOT / "tests/fixtures").rglob("*") if p.is_file())
    paths.update(p for p in (ROOT / "examples").rglob("*") if p.is_file())
    paths.update(ROOT / name for name in ("pyproject.toml", "resources/research/s57_sources.json", "resources/research/arc_sources.json", "resources/run_0_12_backend_gate.py"))
    return [{"path": p.relative_to(ROOT).as_posix(), "bytes": p.stat().st_size,
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(paths)]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stem", required=True)
    parser.add_argument("tests", nargs="*")
    args = parser.parse_args()
    if not args.stem.startswith(("development_0.12_", "release_0.12_")) or "/" in args.stem:
        parser.error("Use a fresh development_0.12_ evidence stem")
    base = ROOT / "resources/validation"
    report, log, xml, inputs = [base / (args.stem + suffix) for suffix in
                                (".json", ".log", ".xml", "_inputs.json")]
    if any(p.exists() for p in (report, log, xml, inputs)):
        raise RuntimeError("Existing evidence must not be overwritten")
    rows = snapshot()
    write(inputs, {"version": "0.12.0", "scope": "all backend modules, all Python tests, real fixtures, shipped example inputs and configuration", "files": rows})
    command = [sys.executable, "-B", "-m", "pytest", "-o", "addopts=", "-o",
               "faulthandler_timeout=30", "-q", "--junitxml=" + str(xml), *args.tests]
    env = os.environ.copy()
    keys = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
    env.update({key: "1" for key in keys})
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    timed_out = False
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=600)
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        code = None
        stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout or ""
        stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else error.stderr or ""
    wall = time.monotonic() - started
    log.write_text(stdout + stderr)
    cases = list(ET.parse(xml).getroot().iter("testcase")) if xml.exists() else []
    stats = {"tests": len(cases), **{name + "s" if name != "skipped" else name:
             sum(case.find(name) is not None for case in cases) for name in ("failure", "error", "skipped")}}
    after = snapshot()
    changed = sorted({r["path"] for r in rows if r not in after} |
                     {r["path"] for r in after if r not in rows})
    passed = code == 0 and not timed_out and bool(cases) and not changed and not any(
        stats[key] for key in ("failures", "errors", "skipped"))
    record = {"version": "0.12.0", "status": "passed" if passed else "failed",
              "started_at_utc": started_utc, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "command": command, "environment": {key: env[key] for key in keys},
              "returncode": code, "timed_out": timed_out, "wall_time_s": wall,
              "stats": stats, "python": platform.python_version(),
              "sqlite_version": sqlite3.sqlite_version, "platform": platform.platform(),
              "stdout": stdout, "stderr": stderr, "input_files": len(rows),
              "input_snapshot_sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
              "changed_inputs": changed}
    write(report, record)
    print(json.dumps({k: v for k, v in record.items() if k not in ("stdout", "stderr")}, ensure_ascii=False), flush=True)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
