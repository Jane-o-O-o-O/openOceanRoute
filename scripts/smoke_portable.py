"""Install and verify a portable archive in a clean temporary environment."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import io
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
import urllib.error
import urllib.parse
import zipfile


def native_s57_http(url: str, fixture: Path, project: dict) -> dict:
    """Real installed-server requests; no source import or preconverted chart."""
    started = time.monotonic()
    payload = fixture.read_bytes()
    source_sha = hashlib.sha256(payload).hexdigest()
    assert source_sha == "ee3fb1a96da5e1da84ca8c00c8aca57c22e1168c78c07c0c9c361cf2e43eed34"
    assert len(payload) == 172585
    requests = 0

    def json_request(path, value=None):
        nonlocal requests
        if value is None:
            request = url + path
        else:
            request = urllib.request.Request(url + path,
                json.dumps(value, allow_nan=False).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        requests += 1
        json.dumps(result, allow_nan=False)
        return result

    def upload(stage, config):
        nonlocal requests
        boundary = "oceanroute-native-s57-" + source_sha
        delimiter = boundary.encode()
        assert delimiter not in payload
        body = (b"--" + delimiter + b'\r\nContent-Disposition: form-data; name="config_json"\r\n\r\n'
                + json.dumps(config, allow_nan=False).encode()
                + b"\r\n--" + delimiter
                + b'\r\nContent-Disposition: form-data; name="file"; filename="US5A1KMJ.zip"'
                + b"\r\nContent-Type: application/octet-stream\r\n\r\n" + payload
                + b"\r\n--" + delimiter + b"--\r\n")
        path = "/api/import/s57" + ("/" + stage if stage else "")
        request = urllib.request.Request(url + path, body,
            {"Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        requests += 1
        json.dumps(result, allow_nan=False)
        assert result["source"]["sha256"] == source_sha and result["source"]["input_bytes"] == len(payload)
        return result

    cell_path = "ENC_ROOT/US5A1KMJ/US5A1KMJ.000"
    inspected = upload("inspect", {})
    assert inspected["stage"] == "inspect" and not inspected["can_apply"] and not inspected["reader"]["native"]
    assert [cell["path"] for cell in inspected["cells"]] == [cell_path]
    catalog = upload("catalog", {"cells": [cell_path]})
    assert catalog["stage"] == "catalog" and not catalog["can_apply"] and catalog["layers"] == []
    cell = catalog["cells"][0]
    assert cell["base_dsid"]["DSID_UPDN"] == "0" and cell["dsid"]["DSID_UPDN"] == "2"
    assert [item["dsid"]["DSID_UPDN"] for item in cell["updates"]] == ["1", "2"]
    counts = {row["name"]: row["feature_count"] for row in catalog["classes_catalog"]}
    assert counts["SOUNDG"] == 4 and counts["DEPARE"] == 88
    imported = upload("", {"cells": [cell_path], "classes": ["DEPARE", "SOUNDG"]})
    assert imported["stage"] == "import" and imported["accepted"] and imported["can_apply"]
    reader = imported["reader"]
    assert reader["native"] and reader["driver"] == "S57" and reader["process_isolated"]
    assert tuple(map(int, reader["pyogrio_version"].split(".")[:2])) >= (0, 12)
    layers = {layer["source"]["object_class"]: layer for layer in imported["layers"]}
    assert set(layers) == {"DEPARE", "SOUNDG"}
    assert all(layer["kind"] == "reference" and layer["crs"] == "EPSG:4326" for layer in layers.values())
    soundings = layers["SOUNDG"]["geojson"]["features"]
    assert len(soundings) == 4
    assert all(feature["geometry"]["type"] == "MultiPoint" for feature in soundings)
    points = [point for feature in soundings for point in feature["geometry"]["coordinates"]]
    assert len(points) == 657 and all(len(point) == 3 for point in points)
    assert points[0] == [177.5191417, 51.9181095, 23.7]
    polygons = []
    for feature in layers["DEPARE"]["geojson"]["features"]:
        geometry = feature["geometry"]
        assert geometry is not None and geometry["type"] in {"Polygon", "MultiPolygon"}
        polygons.extend([geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"])
    holes = sum(len(polygon)-1 for polygon in polygons)
    assert holes > 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for layer in layers.values():
            source = layer["source"]; evidence = source["cell_evidence"]
            assert source["source_sha256"] == source_sha
            assert all(evidence[key] == imported["cells"][0][key] for key in evidence)
            assert evidence["applied_update_number"] == 2
            for item in [evidence["base"], *evidence["updates"]]:
                original = archive.read(item["path"])
                assert item["bytes"] == len(original) and item["sha256"] == hashlib.sha256(original).hexdigest()
            native = source["native_reader"]
            assert native["driver"] == "S57" and native["process_isolated"]
            assert all(native[key] == reader[key] for key in ("pyogrio_version", "gdal_version", "options"))
            assert source["datum_units"]["depth_units"] == "m" and source["datum_units"]["sounding_datum_code"] == 12
            assert source["depth_is_engineering_water_depth"] is False
            assert "worker_path" not in json.dumps(source)
    workspace = json_request("/api/workspace/migrate", {"project": project})["workspace"]
    before = json_request("/api/workspaces", workspace)["workspace"]
    original = deepcopy(before)
    inventory = json_request("/api/workspace/analyze", before)["summary"]
    candidate = json_request("/api/workspace/action", {"workspace": before, "config": {
        "action": "update_shared", "layers": before["layers"] + imported["layers"]}})["workspace"]
    assert before == original
    for key in ("paths", "assemblies", "associations", "cable_types", "terrain_sources"):
        assert candidate[key] == original[key], key
    assert candidate["layers"][-2:] == imported["layers"]
    assert json_request("/api/workspaces/" + before["id"]) == original
    saved = json_request("/api/workspaces", candidate)["workspace"]
    assert saved["saved_revision"] == original["saved_revision"] + 1
    reopened = json_request("/api/workspaces/" + saved["id"])
    assert reopened == saved and reopened["layers"][-2:] == imported["layers"]
    assert [layer["source"] for layer in reopened["layers"][-2:]] == [layer["source"] for layer in imported["layers"]]
    after = json_request("/api/workspace/analyze", reopened)["summary"]
    assert after["manufactured_total_m"] == inventory["manufactured_total_m"]
    assert after["deployment_path_count"] == inventory["deployment_path_count"]
    return {"status": "passed", "stages": ["inspect", "catalog", "import"],
            "actual_http_requests": requests,
            "fixture": {"path": "tests/fixtures/s57/noaa/US5A1KMJ.zip", "bytes": len(payload),
                        "sha256": source_sha, "source_url": "https://www.charts.noaa.gov/ENCs/US5A1KMJ.zip"},
            "native_reader": {key: reader[key] for key in ("driver", "pyogrio_version", "gdal_version", "process_isolated", "options")},
            "base_update_number": 0, "applied_update_number": 2, "soundg_xyz_points": len(points),
            "dep_are_polygon_holes": holes, "reference_layers": 2,
            "atomic_shared_update": True, "preview_not_persisted": True,
            "saved_revision": saved["saved_revision"], "saved_source_and_z_geometry_reopened": True,
            "path_assembly_material_inventory_unchanged": True, "elapsed_s": round(time.monotonic()-started, 3),
            "scope": "fresh launcher-installed HTTP server with shipped original NOAA ZIP; reference chart only, no engineering sounding conversion or navigation certification"}


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
                if tuple(map(int, expected_version.split("."))) >= (0, 8, 0):
                    native_s57 = native_s57_http(url, checkout / "tests/fixtures/s57/noaa/US5A1KMJ.zip", project)
                if tuple(map(int, expected_version.split("."))) >= (0, 9, 0):
                    helper = checkout / "scripts/side_slopes_smoke.py"
                    namespace = {"__name__": "portable_side_slopes_smoke"}
                    exec(compile(helper.read_text(), str(helper), "exec"), namespace)
                    def get_side(path):
                        with urllib.request.urlopen(url + path, timeout=30) as response:
                            return json.load(response)
                    side_slopes = namespace["run_side_slopes_smoke"](post, get_side)
                    side_slopes.update(harness_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                        scope="Shipped harness calling the freshly launcher-installed HTTP server and real SQLite APIs; API reread only, server owner remains running during this workflow")
                if tuple(map(int, expected_version.split("."))) >= (0, 10, 0):
                    helper = checkout / "scripts/automatic_rules_smoke.py"
                    namespace = {"__name__": "portable_automatic_rules_smoke"}
                    exec(compile(helper.read_text(encoding="utf-8"), str(helper), "exec"), namespace)
                    automatic_reads = 0
                    automatic_owner_restarted = False
                    automatic_process_ids = [process.pid]

                    def post_automatic(path, value):
                        request = urllib.request.Request(url + path,
                            json.dumps(value, allow_nan=False).encode("utf-8"),
                            {"Content-Type": "application/json"})
                        try:
                            with urllib.request.urlopen(request, timeout=30) as response:
                                return json.load(response)
                        except urllib.error.HTTPError as error:
                            if error.code != 422:
                                raise
                            return {"_http_status": 422, "_error": json.load(error)}

                    def get_automatic(path):
                        nonlocal process
                        nonlocal automatic_reads, automatic_owner_restarted
                        automatic_reads += 1
                        if automatic_reads == 2:
                            # Unlike the inherited helper's API reread, this
                            # actually stops the fresh installed server owner.
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
                                process.wait(timeout=10)
                            process = subprocess.Popen(
                                [sys.executable, "launcher.py", "--port", str(port)],
                                cwd=checkout, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                start_new_session=os.name != "nt")
                            automatic_process_ids.append(process.pid)
                            restart_health = None
                            deadline = time.monotonic() + 60
                            while time.monotonic() < deadline:
                                if process.poll() is not None:
                                    raise RuntimeError("Restarted launcher stopped: " + log.read_text()[-4000:])
                                try:
                                    with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                                        restart_health = json.load(response)
                                    break
                                except (OSError, ValueError):
                                    time.sleep(.1)
                            assert restart_health is not None and restart_health["version"] == expected_version
                            assert len(set(automatic_process_ids)) == 2
                            automatic_owner_restarted = True
                        with urllib.request.urlopen(url + path, timeout=30) as response:
                            return json.load(response)

                    automatic_rules = namespace["run_automatic_rules_smoke"](post_automatic, get_automatic)
                    assert automatic_owner_restarted
                    # The launcher installs -e ROOT[terrain] in a fresh venv.
                    # Query that real editable registration before adding test
                    # dependencies; cwd remains outside the source checkout.
                    installed_python = checkout / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                    installed_code = (
                        "import importlib,importlib.metadata as md,json,pathlib,hashlib,sys,oceanroute; names="
                        "['oceanroute','oceanroute.api','oceanroute.core','oceanroute.workspace',"
                        "'oceanroute.workspace_storage','oceanroute.sqlite_lifecycle','oceanroute.automatic_rules',"
                        "'oceanroute.automatic_rule_geometry','oceanroute.terrain_slope_neighborhoods']; rows={}; "
                        "\nfor name in names:\n p=pathlib.Path(importlib.import_module(name).__file__).resolve(); "
                        "data=p.read_bytes(); rows[name]={'path':str(p),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}"
                        "\nd=md.distribution('oceanroute'); f=next(f for f in d.files if str(f).endswith('.dist-info/METADATA')); "
                        "mp=pathlib.Path(d.locate_file(f)).resolve(); data=mp.read_bytes(); "
                        "du=json.loads(d.read_text('direct_url.json')); "
                        "print(json.dumps({'version':oceanroute.__version__,'modules':rows,'sys_prefix':sys.prefix,"
                        "'resolved_sys_prefix':str(pathlib.Path(sys.prefix).resolve()),'metadata':{'name':d.metadata['Name'],"
                        "'version':d.version,'dist_info_path':str(mp.parent),'metadata_bytes':len(data),"
                        "'metadata_sha256':hashlib.sha256(data).hexdigest(),'direct_url':du}}))")
                    installed = json.loads(subprocess.check_output([str(installed_python), "-c", installed_code],
                        cwd=root, env=environment, text=True))
                    assert installed["version"] == expected_version
                    source_root = checkout.resolve()
                    environment_root = (checkout / ".venv").resolve()
                    assert Path(installed["sys_prefix"]).resolve() == environment_root
                    assert Path(installed["resolved_sys_prefix"]).resolve() == environment_root
                    registration = installed["metadata"]
                    assert registration["name"].lower() == "oceanroute" and registration["version"] == expected_version
                    assert Path(registration["dist_info_path"]).resolve().is_relative_to(environment_root)
                    direct_url = registration["direct_url"]
                    assert direct_url.get("dir_info", {}).get("editable") is True
                    # urlopen imports urllib.parse; decode a real local file URI
                    # then normalize both sides (/var and /private/var on macOS).
                    parsed_source = urllib.parse.urlsplit(direct_url["url"])
                    assert parsed_source.scheme == "file" and parsed_source.netloc in ("", "localhost")
                    assert not parsed_source.query and not parsed_source.fragment
                    declared_root = Path(urllib.request.url2pathname(parsed_source.path)).resolve()
                    assert declared_root == source_root
                    for name, row in installed["modules"].items():
                        location = Path(row["path"]).resolve()
                        assert location.is_relative_to(source_root / "oceanroute")
                        suffix = "oceanroute/__init__.py" if name == "oceanroute" else name.replace(".", "/") + ".py"
                        source = (source_root / suffix).resolve()
                        assert location == source
                        assert row["bytes"] == source.stat().st_size and row["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
                    automatic_rules.update(version=expected_version,
                        harness_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                        actual_http_server_owner_closed_and_restarted=True,
                        launcher_process_ids=automatic_process_ids,
                        installation_kind="fresh_venv_editable_source",
                        launcher_installed_modules_before_test_dependencies=installed["modules"],
                        launcher_installation={"checkout_path": str(checkout), "resolved_checkout_path": str(source_root),
                            "environment_path": str(checkout / ".venv"), "resolved_environment_path": str(environment_root),
                            "sys_prefix": installed["sys_prefix"], "resolved_sys_prefix": installed["resolved_sys_prefix"],
                            "metadata": registration, "resolved_direct_url_source_root": str(declared_root)},
                        scope="Shipped stdlib harness through the launcher's fresh venv editable source[terrain] HTTP server; server actually stopped/restarted before saved reread; actual editable metadata/direct_url and module source bytes verified before adding test dependencies; distinct from extracted-wheel smoke, synthetic screening only")
                if tuple(map(int, expected_version.split("."))) >= (0, 11, 0):
                    helper = checkout / "scripts/altercourse_smoke.py"
                    namespace = {"__name__": "portable_altercourse_smoke"}
                    exec(compile(helper.read_text(encoding="utf-8"), str(helper), "exec"), namespace)
                    altercourse_reads = 0
                    altercourse_owner_restarted = False
                    altercourse_process_ids = [process.pid]

                    def post_altercourse(path, value):
                        request = urllib.request.Request(url + path,
                            json.dumps(value, allow_nan=False).encode("utf-8"),
                            {"Content-Type": "application/json"})
                        try:
                            with urllib.request.urlopen(request, timeout=30) as response:
                                content_type = response.headers.get("Content-Type", "")
                                if path == "/api/export/csv":
                                    return {"_http_status": response.status,
                                        "_content_type": content_type,
                                        "_text": response.read().decode("utf-8")}
                                return json.load(response)
                        except urllib.error.HTTPError as error:
                            if error.code != 422:
                                raise
                            return {"_http_status": 422, "_error": json.load(error)}

                    def get_altercourse(path):
                        nonlocal process
                        nonlocal altercourse_reads, altercourse_owner_restarted
                        altercourse_reads += 1
                        if altercourse_reads == 2:
                            # Independent of the automatic-rule owner's earlier
                            # restart: this is the arc workflow's post-save read.
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
                                process.wait(timeout=10)
                            process = subprocess.Popen(
                                [sys.executable, "launcher.py", "--port", str(port)],
                                cwd=checkout, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                start_new_session=os.name != "nt")
                            altercourse_process_ids.append(process.pid)
                            restart_health = None
                            deadline = time.monotonic() + 60
                            while time.monotonic() < deadline:
                                if process.poll() is not None:
                                    raise RuntimeError("Arc restarted launcher stopped: " + log.read_text()[-4000:])
                                try:
                                    with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                                        restart_health = json.load(response)
                                    break
                                except (OSError, ValueError):
                                    time.sleep(.1)
                            assert restart_health is not None and restart_health["version"] == expected_version
                            assert len(set(altercourse_process_ids)) == 2
                            altercourse_owner_restarted = True
                        with urllib.request.urlopen(url + path, timeout=30) as response:
                            return json.load(response)

                    altercourse = namespace["run_altercourse_smoke"](post_altercourse, get_altercourse)
                    assert altercourse_owner_restarted
                    # A new interpreter outside the checkout verifies actual
                    # arc consumers in the launcher's editable installation.
                    arc_modules = ["oceanroute.altercourse", "oceanroute.altercourse_workspace",
                        "oceanroute.route_geometry", "oceanroute.rpl_templates", "oceanroute.exchange",
                        "oceanroute.geodesy", "oceanroute.gis", "oceanroute.survey", "oceanroute.shipplan",
                        "oceanroute.plan_voyage", "oceanroute.routing", "oceanroute.terrain_bathymetry",
                        "oceanroute.terrain_slice", "oceanroute.side_slopes",
                        "oceanroute.slope_rules"]
                    arc_code = (
                        "import importlib,importlib.metadata as md,json,pathlib,hashlib,sys,oceanroute; names="
                        + repr(arc_modules) + "; rows={}; "
                        "\nfor name in names:\n p=pathlib.Path(importlib.import_module(name).__file__).resolve(); "
                        "data=p.read_bytes(); rows[name]={'path':str(p),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}"
                        "\nprint(json.dumps({'version':oceanroute.__version__,'sys_prefix':str(pathlib.Path(sys.prefix).resolve()),"
                        "'modules':rows,'geographiclib_version':md.version('geographiclib')}))")
                    arc_installed = json.loads(subprocess.check_output([str(installed_python), "-c", arc_code],
                        cwd=root, env=environment, text=True))
                    assert arc_installed["version"] == expected_version
                    assert Path(arc_installed["sys_prefix"]).resolve() == environment_root
                    assert tuple(map(int, arc_installed["geographiclib_version"].split(".")[:1])) == (2,)
                    for name, row in arc_installed["modules"].items():
                        source = (source_root / (name.replace(".", "/") + ".py")).resolve()
                        assert Path(row["path"]).resolve() == source
                        assert row["bytes"] == source.stat().st_size
                        assert row["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
                    altercourse.update(version=expected_version,
                        harness_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                        actual_http_server_owner_closed_and_restarted=True,
                        launcher_process_ids=altercourse_process_ids,
                        installation_kind="fresh_venv_editable_source",
                        launcher_installed_modules_before_test_dependencies=arc_installed["modules"],
                        launcher_installation=automatic_rules["launcher_installation"],
                        geographiclib_version=arc_installed["geographiclib_version"],
                        scope="Shipped stdlib arc harness through fresh launcher-installed editable source[terrain] HTTP server; independently stopped/restarted before this workflow's saved reread; actual source bytes, venv and editable metadata verified before adding test dependencies; own synthetic geometry, not original-product equivalence")
                if tuple(map(int, expected_version.split("."))) >= (0, 12, 0):
                    helper = checkout / "scripts/arc_edit_smoke.py"
                    namespace = {"__name__": "portable_arc_edit_smoke"}
                    exec(compile(helper.read_text(encoding="utf-8"), str(helper), "exec"), namespace)
                    arc_edit_reads = 0
                    arc_edit_owner_restarted = False
                    arc_edit_process_ids = [process.pid]

                    def post_arc_edit(path, value):
                        request = urllib.request.Request(url + path,
                            json.dumps(value, allow_nan=False).encode("utf-8"),
                            {"Content-Type": "application/json"})
                        try:
                            with urllib.request.urlopen(request, timeout=30) as response:
                                content_type = response.headers.get("Content-Type", "")
                                if path == "/api/export/csv":
                                    return {"_http_status": response.status,
                                        "_content_type": content_type,
                                        "_text": response.read().decode("utf-8")}
                                return json.load(response)
                        except urllib.error.HTTPError as error:
                            if error.code != 422:
                                raise
                            return {"_http_status": 422, "_error": json.load(error)}

                    def get_arc_edit(path):
                        nonlocal process
                        nonlocal arc_edit_reads, arc_edit_owner_restarted
                        arc_edit_reads += 1
                        if arc_edit_reads == 2:
                            # This workflow independently restarts the actual
                            # current owner after its own explicit save; prior
                            # automatic/altercourse restarts do not prove this.
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
                                process.wait(timeout=10)
                            process = subprocess.Popen(
                                [sys.executable, "launcher.py", "--port", str(port)],
                                cwd=checkout, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                start_new_session=os.name != "nt")
                            arc_edit_process_ids.append(process.pid)
                            restart_health = None
                            deadline = time.monotonic() + 60
                            while time.monotonic() < deadline:
                                if process.poll() is not None:
                                    raise RuntimeError("Arc-edit restarted launcher stopped: " + log.read_text()[-4000:])
                                try:
                                    with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                                        restart_health = json.load(response)
                                    break
                                except (OSError, ValueError):
                                    time.sleep(.1)
                            assert restart_health is not None and restart_health["version"] == expected_version
                            assert len(set(arc_edit_process_ids)) == 2
                            arc_edit_owner_restarted = True
                        with urllib.request.urlopen(url + path, timeout=30) as response:
                            return json.load(response)

                    arc_editing = namespace["run_arc_edit_smoke"](post_arc_edit, get_arc_edit)
                    assert arc_edit_owner_restarted
                    # A fresh child outside the extracted source verifies the
                    # real launcher-installed editable registration and all
                    # three new modules before installing test dependencies.
                    arc_edit_modules = ["oceanroute.arc_edit_geometry", "oceanroute.arc_edit",
                                        "oceanroute.arc_edit_workspace"]
                    arc_edit_code = (
                        "import importlib,importlib.metadata as md,json,pathlib,hashlib,sys,oceanroute; names="
                        + repr(arc_edit_modules) + "; rows={}; "
                        "\nfor name in names:\n p=pathlib.Path(importlib.import_module(name).__file__).resolve(); "
                        "data=p.read_bytes(); rows[name]={'path':str(p),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}"
                        "\nd=md.distribution('oceanroute'); f=next(f for f in d.files if str(f).endswith('.dist-info/METADATA')); "
                        "mp=pathlib.Path(d.locate_file(f)).resolve(); data=mp.read_bytes(); "
                        "du=json.loads(d.read_text('direct_url.json')); "
                        "print(json.dumps({'version':oceanroute.__version__,'modules':rows,'sys_prefix':sys.prefix,"
                        "'resolved_sys_prefix':str(pathlib.Path(sys.prefix).resolve()),'metadata':{'name':d.metadata['Name'],"
                        "'version':d.version,'dist_info_path':str(mp.parent),'metadata_bytes':len(data),"
                        "'metadata_sha256':hashlib.sha256(data).hexdigest(),'direct_url':du}}))")
                    arc_edit_installed = json.loads(subprocess.check_output(
                        [str(installed_python), "-c", arc_edit_code], cwd=root, env=environment, text=True))
                    assert arc_edit_installed["version"] == expected_version
                    arc_edit_source_root = checkout.resolve()
                    arc_edit_environment_root = (checkout / ".venv").resolve()
                    assert Path(arc_edit_installed["sys_prefix"]).resolve() == arc_edit_environment_root
                    assert Path(arc_edit_installed["resolved_sys_prefix"]).resolve() == arc_edit_environment_root
                    arc_edit_registration = arc_edit_installed["metadata"]
                    assert arc_edit_registration["name"].lower() == "oceanroute"
                    assert arc_edit_registration["version"] == expected_version
                    assert Path(arc_edit_registration["dist_info_path"]).resolve().is_relative_to(arc_edit_environment_root)
                    arc_edit_direct_url = arc_edit_registration["direct_url"]
                    assert arc_edit_direct_url.get("dir_info", {}).get("editable") is True
                    arc_edit_parsed_source = urllib.parse.urlsplit(arc_edit_direct_url["url"])
                    assert arc_edit_parsed_source.scheme == "file" and arc_edit_parsed_source.netloc in ("", "localhost")
                    assert not arc_edit_parsed_source.query and not arc_edit_parsed_source.fragment
                    arc_edit_declared_root = Path(urllib.request.url2pathname(arc_edit_parsed_source.path)).resolve()
                    assert arc_edit_declared_root == arc_edit_source_root
                    assert set(arc_edit_installed["modules"]) == set(arc_edit_modules)
                    for name, row in arc_edit_installed["modules"].items():
                        source = (arc_edit_source_root / (name.replace(".", "/") + ".py")).resolve()
                        assert source.is_relative_to(arc_edit_source_root / "oceanroute")
                        assert Path(row["path"]).resolve() == source
                        assert row["bytes"] == source.stat().st_size
                        assert row["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
                    arc_editing.update(version=expected_version,
                        harness_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                        actual_http_server_owner_closed_and_restarted=True,
                        actual_read_count=arc_edit_reads,
                        launcher_process_ids=arc_edit_process_ids,
                        installation_kind="fresh_venv_editable_source",
                        launcher_installed_modules_before_test_dependencies=arc_edit_installed["modules"],
                        launcher_installation={"checkout_path": str(checkout), "resolved_checkout_path": str(arc_edit_source_root),
                            "environment_path": str(checkout / ".venv"), "resolved_environment_path": str(arc_edit_environment_root),
                            "sys_prefix": arc_edit_installed["sys_prefix"], "resolved_sys_prefix": arc_edit_installed["resolved_sys_prefix"],
                            "metadata": arc_edit_registration, "resolved_direct_url_source_root": str(arc_edit_declared_root)},
                        scope="Shipped stdlib endpoint-edit harness through fresh launcher-installed editable source[terrain] HTTP server; independently stopped/restarted before this workflow's own saved reread; three new actual module source bytes, venv metadata and editable direct_url verified before adding test dependencies; distinct from extracted-wheel smoke, synthetic geometry only")
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
        if tuple(map(int, expected_version.split("."))) >= (0, 8, 0):
            versions = subprocess.check_output(
                [str(python), "-c", "import json,importlib.metadata as m,pyogrio,sqlite3; v={k:m.version(k) for k in ['oceanroute','fastapi','numpy','scipy','pyproj','shapely','rasterio','contourpy','pyogrio']}; c=sqlite3.connect(':memory:'); v.update(gdal=pyogrio.__gdal_version_string__,sqlite3=sqlite3.sqlite_version,sqlite3_source_id=c.execute('select sqlite_source_id()').fetchone()[0],sqlite3_threadsafety=sqlite3.threadsafety); c.close(); print(json.dumps(v))"],
                cwd=root, env=environment, text=True)
        else:
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
        if tuple(map(int, expected_version.split("."))) >= (0, 8, 0):
            result["native_s57"] = native_s57
            assert native_s57["native_reader"]["pyogrio_version"] == result["versions"]["pyogrio"]
            assert native_s57["native_reader"]["gdal_version"] == result["versions"]["gdal"]
        if tuple(map(int, expected_version.split("."))) >= (0, 9, 0):
            result["side_slopes_and_kp_rules"] = side_slopes
        if tuple(map(int, expected_version.split("."))) >= (0, 10, 0):
            result["automatic_geographic_rules"] = automatic_rules
        if tuple(map(int, expected_version.split("."))) >= (0, 11, 0):
            result["altercourse_geometry"] = altercourse
            result["versions"]["geographiclib"] = arc_installed["geographiclib_version"]
        if tuple(map(int, expected_version.split("."))) >= (0, 12, 0):
            result["arc_endpoint_editing"] = arc_editing
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
