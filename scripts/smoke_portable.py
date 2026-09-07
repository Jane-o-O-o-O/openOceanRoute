"""Install and verify a portable archive in a clean temporary environment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


def smoke(archive: Path, report: Path | None = None) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="oceanroute-portable-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(archive.resolve()) as package:
            package.extractall(root)
        checkout = root / "OceanRoute"
        match = re.search(r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"$',
                          (checkout / "pyproject.toml").read_text(), re.M)
        if match is None:
            raise ValueError("Archive has no declared OceanRoute version")
        expected_version = match.group(1)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["OCEANROUTE_DATA_DIR"] = str(root / "data")
        environment["BROWSER"] = "true"
        log = root / "launch.log"
        process = None
        try:
            with log.open("w") as stream:
                process = subprocess.Popen(
                    [sys.executable, "launcher.py", "--port", str(port)],
                    cwd=checkout, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                )
                url = f"http://127.0.0.1:{port}"
                health = None
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Launcher stopped: " + log.read_text()[-4000:])
                    try:
                        with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                            health = json.load(response)
                        break
                    except (OSError, ValueError):
                        time.sleep(.25)
                if health is None:
                    raise RuntimeError("Launcher startup timeout: " + log.read_text()[-4000:])
                assert health["version"] == expected_version, health
                with urllib.request.urlopen(url, timeout=10) as response:
                    assert 'id="root"' in response.read().decode()
                with urllib.request.urlopen(url + "/api/sample", timeout=10) as response:
                    project = json.load(response)
                request = urllib.request.Request(url + "/api/analyze", json.dumps(project).encode(),
                                                 {"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    analysis = json.load(response)
                assert analysis["summary"]["surface_length_m"] > 0
                physical_examples = {}
                if tuple(map(int, expected_version.split("."))) >= (0, 6, 0):
                    # Execute the shipped inputs through the freshly installed
                    # HTTP server, rather than trusting source-tree imports.
                    for filename, endpoint in (
                        ("initial-equilibrium-dynamic.json", "/api/simulation/dynamic"),
                        ("geographic-equilibrium-voyage.json", "/api/shipplan/prepare-voyage"),
                    ):
                        payload = (checkout / "examples" / filename).read_bytes()
                        request = urllib.request.Request(url + endpoint, payload,
                                                         {"Content-Type": "application/json"})
                        with urllib.request.urlopen(request, timeout=30) as response:
                            result = json.load(response)
                        if filename.startswith("initial-"):
                            assert result["model"] == "material-lumped-mass-xpbd-cable-lay-v4"
                            assert result["checkpoint"]["schema_version"] == 3
                            assert result["initialization"]["source"] == "oceanroute.static_bathymetry.static_equilibrium"
                            assert result["summary"]["paid_out_m"] == 0
                            assert result["summary"]["initial_material_length_m"] == 20
                            assert result["frames"][-1]["touchdown"] is None
                        else:
                            assert result["mapping"]["schema_version"] == 2
                            assert result["mapping"]["initial_natural_length_m"] == 20
                            assert result["config"]["simulation"]["initial_equilibrium"]
                            assert result["mapping"]["initial_equilibrium_preparation"]["provenance"]
                        physical_examples[filename] = {"endpoint": endpoint, "http_status": 200,
                                                       "model": result["model"]}
                if tuple(map(int, expected_version.split("."))) >= (0, 7, 0):
                    for filename, geographic in (("heterogeneous-initial-dynamic.json", False),
                                                  ("heterogeneous-initial-plan-voyage.json", True),
                                                  ("current-initial-dynamic.json", False),
                                                  ("current-initial-plan-voyage.json", True)):
                        source = json.loads((checkout/"examples"/filename).read_text())
                        def post(path, value):
                            request = urllib.request.Request(url+path, json.dumps(value, allow_nan=False).encode(),
                                                             {"Content-Type": "application/json"})
                            with urllib.request.urlopen(request, timeout=30) as response:
                                return json.load(response)
                        prepared = post("/api/shipplan/prepare-voyage" if geographic else
                                        "/api/simulation/prepare-equilibrium-initial", source)
                        config = prepared["config"] if geographic else source["config"]
                        endpoint = "/api/voyage/run" if geographic else "/api/simulation/dynamic"
                        result = post(endpoint, {"project": source["project"], "config": config})
                        checkpoint = result["checkpoint"]["physical_checkpoint"] if geographic else result["checkpoint"]
                        current = filename.startswith("current-")
                        assert checkpoint["schema_version"] == (4 if current else 3)
                        proof = checkpoint["state"]["initialization_provenance"]
                        assert proof["schema"].endswith(".v3" if current else ".v2")
                        assert proof["verification"]["accepted"]
                        first = post(endpoint, {"project": source["project"], "config": {**config, "duration_s": .04}})
                        resumed = post(endpoint, {"project": {}, "config": {"resume_state": first["checkpoint"], "duration_s": .04}})
                        final = resumed["checkpoint"]["physical_checkpoint"] if geographic else resumed["checkpoint"]
                        for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg", "node_wet_weight_n"):
                            assert final["state"][key] == checkpoint["state"][key], (filename, key)
                        physical_examples[filename] = {"endpoint": endpoint, "http_status": 200,
                            "model": checkpoint["model"], "proof_schema": proof["schema"],
                            "split_json_resume_exact_six_arrays": True, "requests": 4}
                assert "创建本地 Python 环境" in log.read_text(), "Launcher reused an environment"
                assert "安装 OceanRoute" in log.read_text(), "Launcher skipped installation"
                print("Clean launcher, isolated environment, HTTP UI and real analysis passed.", flush=True)
        finally:
            if process is not None and process.poll() is None:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        python = checkout / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "-q", "-e", ".[terrain,test]"],
                       cwd=checkout, env=environment, check=True)
        tests = subprocess.run([str(python), "-m", "pytest", "-o", "addopts=", "-q"],
                               cwd=checkout, env=environment, check=True, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(tests.stdout, end="", flush=True)
        versions = subprocess.check_output(
            [str(python), "-c", "import json,importlib.metadata as m; print(json.dumps({k:m.version(k) for k in ['oceanroute','fastapi','numpy','scipy','pyproj','shapely','rasterio','contourpy']}))"],
            cwd=root, env=environment, text=True)
        result = {"archive": archive.name, "platform": sys.platform,
                  "python": sys.version.split()[0], "clean_venv": True,
                  "launcher_install": True, "http_health": health,
                  "http_ui": True, "real_analysis": True,
                  "synthetic_physical_http_examples": physical_examples,
                  "tests_output": tests.stdout.strip(), "versions": json.loads(versions),
                  "wall_time_s": round(time.monotonic() - started, 2)}
        if report is not None:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()
    smoke(options.archive, options.report)
