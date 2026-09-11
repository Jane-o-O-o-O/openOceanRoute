"""Verify wheel contents outside the checkout using this interpreter's dependencies.

The positional wheel argument is unchanged. This is an ASGI/runtime smoke check,
not a clean pip installation, console-entry-point check, or numerical validation.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

# Execute in a fresh process, with only the extracted wheel on PYTHONPATH. Keep
# the checks here so the test cannot accidentally import the checkout's package.
SMOKE_CODE = r'''
from copy import deepcopy
import base64
import importlib
import json
import math
import pathlib
import re
import sys
import time

import numpy as np
import oceanroute
from fastapi.testclient import TestClient
from pyproj import CRS, Transformer
from rasterio.io import MemoryFile
from oceanroute.api import create_app
from oceanroute.storage import ProjectStore
from oceanroute.voyage import read_voyage_checkpoint
from oceanroute.checkpoints import read_checkpoint
from oceanroute.plan_voyage import read_plan_mapping

root = pathlib.Path.cwd().resolve()
expected_version = sys.argv[1]
started = time.monotonic()
modules = {}
for name in ("oceanroute", "oceanroute.api", "oceanroute.storage",
             "oceanroute.workspace", "oceanroute.workspace_storage",
             "oceanroute.seismic", "oceanroute.voyage", "oceanroute.voyage_jobs",
             "oceanroute.checkpoints", "oceanroute.plan_voyage", "oceanroute.shipplan",
             "oceanroute.simulation", "oceanroute.rpl_templates", "oceanroute.exchange",
             "oceanroute.dtm", "oceanroute.terrain_boundaries", "oceanroute.terrain_slice",
             "oceanroute.coordinate_transforms", "oceanroute.terrain_sources",
             "oceanroute.bathymetry", "oceanroute.terrain_bathymetry",
             "oceanroute.map_projection", "oceanroute.workspace_terrain",
             "oceanroute.static_bathymetry",
             "oceanroute.catenary_calculator", "oceanroute.initial_equilibrium",
             "oceanroute.plan_equilibrium_frame"):
    module = importlib.import_module(name)
    location = pathlib.Path(module.__file__).resolve()
    assert location.is_relative_to(root), (name, str(location), str(root))
    modules[name] = str(location.relative_to(root))
assert oceanroute.__version__ == expected_version, (oceanroute.__version__, expected_version)


def finite(value):
    json.dumps(value, allow_nan=False)
    return value


def get_json(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:1000])
    return finite(response.json())


def post_json(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 200, (path, response.status_code, response.text[:1000])
    return finite(response.json())


def reject_json(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 422, (path, response.status_code, response.text[:1000])
    return finite(response.json())


def wait_completed(client, identifier, timeout_s=20):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = get_json(client, "/api/voyage/jobs/" + identifier)
        if job["status"] not in {"queued", "running", "cancelling"}:
            assert job["status"] == "completed", job
            assert job["checkpoint_available"] and job["result_available"], job
            return job
        time.sleep(.02)
    raise AssertionError("Actual background job did not complete within the smoke deadline: " + identifier)


def json_roundtrip(value):
    # Persist/reload actual finite JSON rather than sharing in-memory arrays.
    return json.loads(json.dumps(value, allow_nan=False))


def assert_equilibrium_checkpoint(checkpoint, grid, origin_m, initial_length_m):
    read_checkpoint(checkpoint)
    assert checkpoint["schema_version"] == 3
    assert checkpoint["model"] == "material-lumped-mass-xpbd-cable-lay-v4"
    assert checkpoint["numerical"]["scheme"] == "implicit-compliant-material-nodes-equilibrium-prestress-v4"
    assert checkpoint["config"]["seabed_grid"] == grid
    state = checkpoint["state"]
    proof = state["initialization_provenance"]
    assert proof["schema"] == "oceanroute.dynamic.initial-equilibrium.provenance.v1"
    assert proof["verification"]["accepted"] and proof["solver"]["accepted"] and proof["solver"]["converged"]
    assert proof["loading_history_reconstructed"] is False
    assert proof["initial_time_s"] == proof["initial_paid_out_m"] == 0
    assert math.isclose(proof["initial_material_length_m"], initial_length_m, abs_tol=1e-10)
    assert math.isclose(state["initial_material_length_m"], initial_length_m, abs_tol=1e-10)
    assert math.isclose(sum(state["rest_lengths_m"]), initial_length_m+state["paid_out_m"], abs_tol=1e-9)
    assert math.isclose(state["node_material_m"][-1], origin_m, abs_tol=1e-10)
    assert math.isclose(state["node_material_m"][0], origin_m+initial_length_m+state["paid_out_m"], abs_tol=1e-9)
    return proof


def assert_same_physical_state(actual, expected):
    # Retain the strict existing 2D continuation tolerances for the new model.
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg",
                "node_seabed_normal", "node_contact_normal_impulse_n_s", "node_contact_friction_impulse_n_s"):
        assert np.allclose(np.asarray(actual["state"][key]), np.asarray(expected["state"][key]),
                           rtol=1e-9, atol=1e-8), key


store_path = root / "data" / "smoke.sqlite3"
app = create_app(ProjectStore(store_path))
# The context manager actually enters lifespan, starts the directory owner and
# background worker, then waits for shutdown and releases the ownership lock.
with TestClient(app) as client:
    health = get_json(client, "/api/health")
    assert health["status"] == "ok" and health["version"] == expected_version, health
    page = client.get("/")
    assert page.status_code == 200 and 'id="root"' in page.text, page.text[:1000]
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text)
    assert assets, page.text
    for path in assets:
        assert client.get(path).status_code == 200, path
    manual = get_json(client, "/api/manual")
    assert "用户手册" in manual["text"], manual

    # Keep legacy schema1 analysis and optimistic-lock checks from the 0.1 smoke.
    project = get_json(client, "/api/sample")
    assert project["schema_version"] == 1, project.get("schema_version")
    analysis = post_json(client, "/api/analyze", project)
    assert analysis["summary"]["surface_length_m"] > 0
    post_json(client, "/api/projects", project)
    conflict = client.post("/api/projects", json=project)
    assert conflict.status_code in (409, 422), conflict.text

    # Schema2 is the source of truth: migration, shared alternative manufacture,
    # save/open, stale-write guard and an explicitly versioned full restore.
    migrated = post_json(client, "/api/workspace/migrate", {"project": project})
    workspace = migrated["workspace"]
    assert workspace["schema_version"] == 2
    assert len(workspace["paths"]) == len(workspace["assemblies"]) == 1
    assert workspace["active_path_id"] == migrated["project"]["id"]
    assert "cable_types" not in workspace["paths"][0]["project"]
    aggregate = post_json(client, "/api/workspace/analyze", workspace)
    manufactured = aggregate["summary"]["manufactured_total_m"]
    assert manufactured > 0 and aggregate["summary"]["deployment_path_count"] == 1
    first = post_json(client, "/api/workspaces", workspace)
    saved = first["workspace"]
    assert first["revision"] == saved["saved_revision"] == 1
    identifier = saved["id"]
    assert get_json(client, "/api/workspaces/" + identifier) == saved
    copied = post_json(client, "/api/workspace/action", {
        "workspace": saved, "config": {"action": "copy_path",
        "name": "Wheel smoke synthetic alternative", "assembly_policy": "alternative"}})
    assert len(copied["workspace"]["paths"]) == 2
    assert len(copied["workspace"]["assemblies"]) == 1
    assert copied["analysis"]["summary"]["alternative_path_count"] == 1
    assert math.isclose(copied["analysis"]["summary"]["manufactured_total_m"], manufactured,
                        rel_tol=1e-10, abs_tol=1e-7)
    second = post_json(client, "/api/workspaces", copied["workspace"])
    assert second["revision"] == second["workspace"]["saved_revision"] == 2
    stale = client.post("/api/workspaces", json=saved)
    assert stale.status_code in (409, 422), stale.text
    revisions = get_json(client, "/api/workspaces/" + identifier + "/revisions")
    assert {r["revision"] for r in revisions} == {1, 2}, revisions
    stale_restore = client.post("/api/workspaces/" + identifier + "/restore/1",
                                json={"expected_revision": 1})
    assert stale_restore.status_code in (409, 422), stale_restore.text
    restored = post_json(client, "/api/workspaces/" + identifier + "/restore/1",
                         {"expected_revision": 2})
    assert restored["saved_revision"] == 3 and len(restored["paths"]) == 1

    # Save/reload declared templates through actual HTTP routes, then parse
    # Unicode character columns and two physical lines per real record.
    examples = get_json(client, "/api/import/rpl/templates")["examples"]
    fixed_example = next(row for row in examples if row["template"]["format"] == "fixed_width")
    normalized = post_json(client, "/api/import/rpl/template",
                           {"template": fixed_example["template"]})["template"]
    assert post_json(client, "/api/import/rpl/template", {
        "text": json.dumps(normalized, ensure_ascii=False)})["template"] == normalized
    parsed_fixed = post_json(client, "/api/import/rpl", {
        "text": fixed_example["text"], "template": normalized})
    assert parsed_fixed["can_apply"] and parsed_fixed["rejected_rows"] == 0
    assert [row["label"] for row in parsed_fixed["points"]] == ["起点", "转点", "终点"]
    assert [(r["line_start"], r["line_end"]) for r in parsed_fixed["records"]] == [(2, 3), (4, 5), (6, 7)]
    assert parsed_fixed["metadata"]["cable_distance_origin_m"] == 5000
    assert [row["fixed_cable_length_m"] for row in parsed_fixed["legs"]] == [1750, 1750]
    fixed_analysis = post_json(client, "/api/analyze", {
        "route": {**parsed_fixed["route_options"], "points": parsed_fixed["points"], "legs": parsed_fixed["legs"]},
        "cable_types": [{"id": "LW", "cost_per_m": 10, "lay_speed_m_s": 2}]})
    assert fixed_analysis["summary"]["cable_length_m"] == 3500
    assert fixed_analysis["summary"]["material_cost"] == 35000
    broken_lines = fixed_example["text"].splitlines()
    broken_lines[3] = "坏"
    broken_text = "\n".join(broken_lines)
    collected = post_json(client, "/api/import/rpl", {"text": broken_text, "template": normalized})
    assert collected["rejected_rows"] == 1 and not collected["can_apply"]
    assert (collected["errors"][0]["record"], collected["errors"][0]["row"]) == (2, 4)
    skipped = post_json(client, "/api/import/rpl", {"text": broken_text,
        "template": {**normalized, "error_policy": "skip"}, "error_policy": "skip"})
    assert skipped["can_apply"] and skipped["legs"][0]["fixed_cable_length_m"] == 3500
    multiline_template = {"schema": "oceanroute.rpl-template", "schema_version": 1,
        "format": "delimited", "index_base": 0, "lines_per_record": 2, "header_lines": 1,
        "fields": {"label": {"line": 0, "column": 0}, "longitude": {"line": 0, "column": 1},
                   "latitude": {"line": 0, "column": 2}, "note": {"line": 0, "column": 3},
                   "cable_type_id": {"line": 1, "column": 0}, "slack_pct": {"line": 1, "column": 1},
                   "depth_m": {"line": 1, "column": 2}}}
    multiline_text = 'header\n"起点,甲",118,22,"第一行\n#保留的备注"\nLW,1.5,30\n#注释\n终点,118.01,22.01,末尾\nLW,2,40\n'
    parsed_multiline = post_json(client, "/api/import/rpl", {
        "text": multiline_text, "template": multiline_template})
    assert parsed_multiline["can_apply"] and parsed_multiline["accepted_rows"] == 2
    assert parsed_multiline["points"][0]["note"] == "第一行\n#保留的备注"
    assert [(r["line_start"], r["line_end"]) for r in parsed_multiline["records"]] == [(2, 4), (6, 7)]
    assert parsed_multiline["legs"][0]["slack_pct"] == 1.5
    csv_example = next(row for row in examples if row["template"]["format"] == "delimited")
    parsed_dms = post_json(client, "/api/import/rpl", csv_example)
    assert parsed_dms["can_apply"] and parsed_dms["points"][0]["label"] == "起点,甲"
    assert math.isclose(parsed_dms["points"][1]["longitude"], 118 + 1/60, abs_tol=1e-12)

    # A synthetic affine surface exercises the real minimum-curvature sparse
    # solve, declared BLN include/exclude mask, GeoTIFF and full-raster slice.
    local = CRS.from_proj4("+proj=aeqd +lat_0=22 +lon_0=118 +datum=WGS84 +units=m +no_defs")
    inverse = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
    source_rows = ["longitude latitude depth_m"]
    for x in range(-200, 201, 100):
        for y in range(-200, 201, 100):
            longitude, latitude = inverse.transform(x, y)
            source_rows.append(f"{longitude:.15g} {latitude:.15g} {100+.01*x+.02*y:.15g}")
    boundary_text = ('5,0,"包含,示例"\n-220,-220\n220,-220\n220,220\n-220,220\n-220,-220\n'
                     '5,1,hole\n-40,-40\n40,-40\n40,40\n-40,40\n-40,-40\n')
    boundary = post_json(client, "/api/dtm/bln/read", {"text": boundary_text, "crs": local.to_string()})
    written_boundary = client.post("/api/dtm/bln/write", json={"document": boundary})
    assert written_boundary.status_code == 200
    assert "attachment" in written_boundary.headers["content-disposition"]
    assert post_json(client, "/api/dtm/bln/read", {
        "text": written_boundary.text, "crs": boundary["crs"]}) == boundary
    grid = post_json(client, "/api/dtm/grid", {"text": "\n".join(source_rows), "config": {
        "method": "minimum_curvature", "grid_spacing_m": 50, "max_gap_m": 160,
        "contour_interval_m": 5, "boundary_bln": boundary_text, "boundary_crs": local.to_string(),
        "minimum_curvature": {"tension": .2}}})
    solver = grid["metadata"]["solver"]
    assert grid["validation_status"] == "research"
    assert solver["algorithm"] == "masked-variational-thin-plate-lsmr-v1"
    assert solver["converged"] and solver["unique_solution_verified"]
    assert solver["work_units"] > 0 and solver["data_max_abs_error_m"] < 1e-4
    assert grid["metadata"]["boundary_excluded_source_points"] == 1
    with MemoryFile(base64.b64decode(grid["geotiff_base64"], validate=True)) as memory:
        with memory.open() as raster:
            assert raster.count == 4 and raster.width > 2 and raster.height > 2
            assert raster.tags()["depth_positive"] == "down"
            assert raster.descriptions[0] == "depth_m_positive_down"
    # A deliberately wrong preview must never become the slice source.
    raster_only = {"geotiff_base64": grid["geotiff_base64"], "preview": {"depth_m": [[999999]]}}
    crossed_hole = post_json(client, "/api/dtm/slice", {"grid": raster_only, "config": {
        "line": {"coordinates": [[-150, 0], [150, 0]], "crs": local.to_string()}, "spacing_m": 50}})
    assert crossed_hole["summary"]["missing_count"] > 0
    assert crossed_hole["summary"]["valid_count"] > 0
    assert crossed_hole["summary"]["bottom_length_m"] is None
    assert crossed_hole["summary"]["complete"] is False
    assert len(crossed_hole["valid_segments"]) >= 2
    complete_slice = post_json(client, "/api/dtm/slice", {"grid": raster_only, "config": {
        "line": {"coordinates": [[-100, 100], [100, 100]], "crs": local.to_string()}, "spacing_m": 50}})
    assert complete_slice["summary"]["complete"]
    assert math.isclose(complete_slice["summary"]["horizontal_length_m"], 200, abs_tol=1e-6)
    assert math.isclose(complete_slice["summary"]["bottom_length_m"], 200*math.sqrt(1+.01**2), abs_tol=1e-3)
    transform_grid = Transformer.from_crs(grid["metadata"]["crs"], local, always_xy=True)
    for sample in complete_slice["samples"]:
        x, y = transform_grid.transform(sample["x_m"], sample["y_m"])
        assert math.isclose(sample["depth_m"], 100+.01*x+.02*y, abs_tol=1e-4)
    slice_document = post_json(client, "/api/dtm/bln/read", {
        "text": complete_slice["slice_bln_text"], "crs": grid["metadata"]["crs"]})
    assert slice_document["objects"] and all(len(p) == 3 for obj in slice_document["objects"] for p in obj["coordinates"])
    reject_json(client, "/api/dtm/slice", {"grid": {"preview": grid["preview"]}, "config": {}})

    # Real horizontal coordinate previews retain every bad point and prevent
    # partial application. UTM is checked against an independent fixed value.
    coordinates = post_json(client, "/api/coordinates/transform", {
        "source_crs": "EPSG:4326", "target_crs": "EPSG:32650",
        "points": [{"id": "origin", "x": 118, "y": 22}]})
    assert coordinates["can_apply"] and not coordinates["errors"]
    utm = coordinates["points"][0]["output"]
    assert math.isclose(utm["x"], 603224.6404290784, rel_tol=0, abs_tol=1e-7)
    assert math.isclose(utm["y"], 2433164.428653589, rel_tol=0, abs_tol=1e-7)
    assert coordinates["operation"]["ballpark"] is False
    assert coordinates["operation"]["best_available"] is True
    reversed_coordinates = post_json(client, "/api/coordinates/transform", {
        "source_crs": "EPSG:32650", "target_crs": "EPSG:4326", "points": [utm]})
    assert math.isclose(reversed_coordinates["points"][0]["output"]["x"], 118, rel_tol=0, abs_tol=1e-10)
    assert math.isclose(reversed_coordinates["points"][0]["output"]["y"], 22, rel_tol=0, abs_tol=1e-10)
    invalid_coordinates = post_json(client, "/api/coordinates/transform", {
        "source_crs": "EPSG:4326", "target_crs": "EPSG:32650",
        "points": [{"id": "good", "x": 118, "y": 22}, {"id": "bad", "x": 118, "y": 95}]})
    assert not invalid_coordinates["can_apply"] and len(invalid_coordinates["errors"]) == 1
    assert invalid_coordinates["points"][0]["accepted"]
    assert not invalid_coordinates["points"][1]["accepted"]
    assert invalid_coordinates["points"][1]["output"] is None

    # Actual source holes and coverage gaps exercise priority selection and
    # same-datum fallback. The workspace persists one shared source library;
    # raising a priority invalidates both alternative paths without substituting
    # their deliberately supplied waypoint depths.
    terrain_example = get_json(client, "/api/terrain/sources/example")
    normalized_sources = post_json(client, "/api/terrain/sources/normalize", {
        "sources": terrain_example["sources"]})
    terrain_query = post_json(client, "/api/terrain/query", {
        "sources": normalized_sources["sources"], "points": terrain_example["points"]})
    terrain_samples = terrain_query["samples"]
    assert all(math.isclose(row["depth_m"], expected, rel_tol=0, abs_tol=1e-8)
               for row, expected in zip(terrain_samples[:3], [300, 100, 100]))
    assert terrain_samples[3]["depth_m"] is None and terrain_samples[3]["source_id"] is None
    assert [a["status"] for a in terrain_samples[1]["attempts"]] == ["nodata", "valid"]
    assert terrain_samples[1]["fallback"] and terrain_samples[2]["fallback"]
    assert terrain_query["quality"]["library_signature"] == normalized_sources["library_signature"]
    assert terrain_query["quality"]["missing_count"] == 1
    for source in terrain_query["sources"]:
        assert source["horizontal_operations"]
        assert all(op["ballpark"] is False and op["best_available"] is True
                   for op in source["horizontal_operations"])
    terrain_project = deepcopy(terrain_example["project"])
    for point in terrain_project["route"]["points"]:
        point["depth_m"] = 55
    terrain_profile = post_json(client, "/api/terrain/profile", {
        "project": terrain_project, "config": terrain_example["config"]})
    terrain_project = terrain_profile["project"]
    terrain_analysis = post_json(client, "/api/analyze", terrain_project)
    assert terrain_analysis["profile_metadata"]["imported_profile_valid"]
    assert terrain_analysis["summary"]["bottom_length_m"] > terrain_analysis["summary"]["surface_length_m"]
    terrain_workspace = post_json(client, "/api/workspace/migrate", {"project": terrain_project})["workspace"]
    assert terrain_workspace["terrain_sources"] == normalized_sources["sources"]
    assert "terrain_sources" not in terrain_workspace["paths"][0]["project"]
    terrain_saved = post_json(client, "/api/workspaces", terrain_workspace)["workspace"]
    terrain_workspace_id = terrain_saved["id"]
    assert get_json(client, "/api/workspaces/"+terrain_workspace_id) == terrain_saved
    terrain_copy = post_json(client, "/api/workspace/action", {
        "workspace": terrain_saved, "config": {"action": "copy_path", "assembly_policy": "alternative"}})
    assert len(terrain_copy["workspace"]["paths"]) == 2 and len(terrain_copy["workspace"]["assemblies"]) == 1
    terrain_second = post_json(client, "/api/workspaces", terrain_copy["workspace"])["workspace"]
    reprioritized = deepcopy(terrain_second["terrain_sources"])
    next(s for s in reprioritized if s["id"] == "example-background")["priority"] = 1000
    terrain_stale = post_json(client, "/api/workspace/action", {
        "workspace": terrain_second, "config": {"action": "update_shared", "terrain_sources": reprioritized}})
    assert len(terrain_stale["analysis"]["paths"]) == 2
    assert all(row["summary"]["bottom_length_m"] is None for row in terrain_stale["analysis"]["paths"])
    assert {w["path_id"] for w in terrain_stale["warnings"] if w["code"] == "TERRAIN_LIBRARY_STALE"} == {
        p["id"] for p in terrain_stale["workspace"]["paths"]}
    terrain_third = post_json(client, "/api/workspaces", terrain_stale["workspace"])["workspace"]
    assert terrain_third["saved_revision"] == 3

    # Independently served projected geometry and one atomic whole-workspace
    # source refresh: no source checkout or partly written path states.
    map_result = post_json(client, "/api/maps/project", {
        "target_crs": "EPSG:32650",
        "points": [{"id": "smoke-point", "longitude": 118., "latitude": 22.}]})
    assert map_result["can_display"] and not map_result["errors"]
    assert np.allclose(map_result["points"][0]["coordinates"],
                       [603224.6404290784, 2433164.428653589], rtol=0, atol=1e-7)
    atomic_terrain = post_json(client, "/api/workspace/terrain/preview", {
        "workspace": terrain_second, "sources": reprioritized,
        "config": {"spacing_m": 200, "vertical_datum": "synthetic-demo-datum"}})
    assert atomic_terrain["can_apply"] and atomic_terrain["workspace"] is not None
    assert len(atomic_terrain["paths"]) == 2
    assert all(row["status"] == "success" for row in atomic_terrain["paths"])
    assert len(atomic_terrain["workspace"]["assemblies"]) == 1
    new_source_library = post_json(client, "/api/terrain/sources/normalize", {"sources": reprioritized})
    assert atomic_terrain["workspace"]["terrain_sources"] == new_source_library["sources"]
    for row in atomic_terrain["paths"]:
        assert row["samples"]
        assert all(sample["source_id"] == "example-background" and math.isclose(sample["depth_m"], 100., abs_tol=1e-8)
                   for sample in row["samples"])
    for path in atomic_terrain["workspace"]["paths"]:
        assert path["project"]["profile"]["metadata"]["terrain_library_signature"] == new_source_library["library_signature"]
    # Preview does not save a revision or replace already persisted old data.
    assert get_json(client, "/api/workspaces/"+terrain_workspace_id) == terrain_third

    # All four Calculator boundary routes and the two explicit elastic length
    # bases operate from the extracted wheel, with no fabricated selected root.
    plane_grid = {"schema": "oceanroute.bathymetry.v1", "x_m": [-80., 0., 80.],
        "y_m": [-80., 0., 80.],
        "z_m": [[-30.+.1*x+.05*y for x in [-80., 0., 80.]] for y in [-80., 0., 80.]],
        "source": {"name": "explicit synthetic wheel smoke plane",
        "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0., 0.],
        "vertical_datum": "already aligned model sea zero"}}
    calculator_config = {"seabed_grid": plane_grid, "wet_weight_n_m": 4.,
        "ea_n": 100000., "heading_deg": 90., "nodes": 41,
        "boundary": {"kind": "bottom_tension", "value_n": 100.}}
    calculator_first = post_json(client, "/api/simulation/catenary-calculator", {"config": calculator_config})
    assert calculator_first["accepted"] and calculator_first["selected"]
    calculator_summary = calculator_first["selected"]["result"]["summary"]
    assert math.isclose(calculator_summary["natural_length_m"], 50.3803848342, abs_tol=1e-8)
    assert calculator_summary["stretched_arc_length_m"] > calculator_summary["natural_length_m"]
    calculator_boundaries = [
        {"kind": "top_tension", "value_n": calculator_summary["top_tension_n"]},
        {"kind": "top_angle", "value_deg": calculator_summary["top_angle_from_horizontal_deg"],
         "reference": "horizontal", "direction": "touchdown_to_vessel"},
        {"kind": "cable_in_water", "value_m": calculator_summary["natural_length_m"], "length_basis": "natural"},
        {"kind": "cable_in_water", "value_m": calculator_summary["stretched_arc_length_m"], "length_basis": "stretched_arc"}]
    for boundary in calculator_boundaries:
        actual_calculator = post_json(client, "/api/simulation/catenary-calculator", {
            "config": {**calculator_config, "boundary": boundary}})
        assert actual_calculator["accepted"] and actual_calculator["solver"]["root_enumeration_complete"]
        assert math.isclose(actual_calculator["selected"]["bottom_tension_n"], 100., abs_tol=1e-7)
        assert np.allclose(actual_calculator["selected"]["result"]["nodes"],
                           calculator_first["selected"]["result"]["nodes"], rtol=0, atol=1e-7)
    slope_result = post_json(client, "/api/simulation/slope-catenary", {"config": {
        "seabed_grid": plane_grid, "wet_weight_n_m": 4., "ea_n": 100000.,
        "heading_deg": 90., "nodes": 41, "bottom_tension_n": 100.}})
    assert slope_result["accepted"]
    assert np.allclose(slope_result["nodes"], calculator_first["selected"]["result"]["nodes"], rtol=0, atol=1e-7)
    curved_grid = deepcopy(plane_grid)
    curved_grid["z_m"] = [[-40.+.1*x+.05*y+.0003*x*y for x in curved_grid["x_m"]] for y in curved_grid["y_m"]]
    curved_result = post_json(client, "/api/simulation/static-bathymetry", {"config": {
        "seabed_grid": curved_grid, "wet_weight_n_m": 4., "ea_n": 10000., "nodes": 18,
        "vessel_position_m": [0., 0., 0.], "anchor_position_m": [-60., -10., -46.32],
        "natural_length_m": 80.}})
    assert curved_result["accepted"] and curved_result["solver"]["converged"]
    assert 2 < curved_result["summary"]["contact_nodes"] < 18
    assert curved_result["segment_clearance"]["minimum_clearance_m"] >= -1e-6

    # A wholly embedded fixed-end research case uses the actual raw initializer,
    # not examples from the checkout or a trusted accepted/display-node object.
    # The explicit fixed anchor is 10m ABOVE the bed and therefore is not TD.
    equilibrium_grid = {"schema": "oceanroute.bathymetry.v1", "x_m": [-50., 0., 50.],
        "y_m": [-50., 0., 50.], "z_m": [[-20., -20., -20.] for _ in range(3)],
        "source": {"name": "explicit synthetic wheel fixed-end case; not a survey",
        "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0., 0.],
        "vertical_datum": "synthetic heights already aligned to model sea z=0"}}
    equilibrium_simulation = {"seabed_grid": equilibrium_grid, "depth_m": 20.,
        "wet_weight_n_m": 4., "diameter_m": .02, "ea_n": 100000., "ei_n_m2": 0.,
        "mass_kg_m": .7298997321841252, "nodes": 11, "ship_speed_m_s": 0., "payout_m_s": 0.,
        "heading_deg": 90., "current_x_m_s": 0., "current_y_m_s": 0.,
        "duration_s": .2, "dt_s": .05, "internal_dt_s": .01, "solver_iterations": 24,
        "seabed_friction": .4, "damping_ratio": .03, "initial_suspended_material_m": 7.,
        "checkpoint_times_s": [0., .1],
        "initial_equilibrium": {"schema": "oceanroute.dynamic.initial-equilibrium.v1",
            "vessel_position_m": [0., 0., 0.], "anchor_position_m": [-15., 0., -10.],
            "natural_length_m": 20.}}
    equilibrium_project = {"schema_version": 1, "crs": "EPSG:4326",
        "name": "synthetic API envelope; geometry is not inferred as a dynamic initial state",
        "route": {"points": [{"longitude": 0., "latitude": 0., "depth_m": 20.},
                             {"longitude": .001, "latitude": 0., "depth_m": 20.}]},
        "cable_types": [], "bodies": []}
    fixed_preparation = post_json(client, "/api/simulation/prepare-equilibrium-initial", {
        "project": equilibrium_project, "config": equilibrium_simulation})
    assert fixed_preparation["provenance"]["verification"]["accepted"]
    assert fixed_preparation["solver"]["accepted"] and fixed_preparation["solver"]["converged"]
    assert math.isclose(sum(fixed_preparation["rest_lengths_m"]), 20., abs_tol=1e-12)
    assert math.isclose(fixed_preparation["initial_material_length_m"], 20., abs_tol=1e-12)
    assert fixed_preparation["positions"][0] == [0., 0., 0.]
    assert fixed_preparation["positions"][-1] == [-15., 0., -10.]
    fixed_full = post_json(client, "/api/simulation/dynamic", {"project": {}, "config": equilibrium_simulation})
    assert fixed_full["model"] == "material-lumped-mass-xpbd-cable-lay-v4"
    assert fixed_full["solver"]["converged"]
    fixed_proof = assert_equilibrium_checkpoint(fixed_full["checkpoint"], equilibrium_grid, 7., 20.)
    assert fixed_full["initialization"] == fixed_proof
    fixed_first = fixed_full["frames"][0]
    assert fixed_first["time_s"] == fixed_first["paid_out_m"] == 0
    assert fixed_first["node_material_m"][0] == 27. and fixed_first["node_material_m"][-1] == 7.
    assert np.allclose(fixed_first["nodes"], fixed_preparation["positions"], rtol=0, atol=1e-9)
    assert np.allclose(fixed_first["node_velocity_m_s"], 0, rtol=0, atol=0)
    assert np.allclose(fixed_first["node_contact_normal_impulse_n_s"], 0, rtol=0, atol=0)
    assert all(frame["touchdown_detected"] is False and frame["touchdown"] is None
               and frame["touchdown_node_index"] is None and frame["bottom_tension_n"] is None
               and frame["anchor_position_m"] == [-15., 0., -10.]
               and frame["anchor_segment_tension_n"] > 0 for frame in fixed_full["frames"])
    # Reconstruct wet loads and Hooke forces directly from public geometry and
    # natural lengths. No static/dynamic helper supplies the expected forces.
    fixed_rest = np.asarray(fixed_proof["initial_snapshot"]["rest_lengths_m"])
    fixed_halves = np.r_[fixed_rest[0]/2, (fixed_rest[:-1]+fixed_rest[1:])/2, fixed_rest[-1]/2]
    assert np.allclose(fixed_first["node_wet_weight_n"], 4*fixed_halves, rtol=0, atol=1e-10)
    assert np.allclose(fixed_first["node_dry_mass_kg"], equilibrium_simulation["mass_kg_m"]*fixed_halves,
                       rtol=0, atol=1e-10)
    fixed_delta = np.diff(np.asarray(fixed_first["nodes"]), axis=0)
    fixed_chords = np.linalg.norm(fixed_delta, axis=1)
    fixed_tension = 100000.*np.maximum(fixed_chords/fixed_rest-1, 0)
    assert np.allclose(fixed_first["segment_tension_n"], fixed_tension, rtol=1e-9, atol=1e-7)
    fixed_internal = np.zeros_like(np.asarray(fixed_first["nodes"]))
    fixed_internal[:-1] += fixed_tension[:, None]*fixed_delta/fixed_chords[:, None]
    fixed_internal[1:] -= fixed_tension[:, None]*fixed_delta/fixed_chords[:, None]
    fixed_internal[:, 2] -= 4*fixed_halves
    assert np.max(np.linalg.norm(fixed_internal[1:-1], axis=1)) <= fixed_proof["verification"]["force_tolerance_n"]+1e-7
    assert fixed_proof["verification"]["minimum_node_clearance_m"] > 0
    assert fixed_full["summary"]["paid_out_m"] == 0
    assert fixed_full["summary"]["material_balance_residual_m"] < 1e-9
    fixed_saved = json_roundtrip(next(cp for cp in fixed_full["checkpoints"] if cp["time_s"] == .1))
    assert_equilibrium_checkpoint(fixed_saved, equilibrium_grid, 7., 20.)
    fixed_resumed = post_json(client, "/api/simulation/dynamic", {
        "project": {}, "config": {"resume_state": fixed_saved, "duration_s": .1}})
    assert_same_physical_state(fixed_resumed["checkpoint"], fixed_full["checkpoint"])
    assert fixed_resumed["checkpoint"]["state"]["initialization_provenance"] == fixed_proof
    fixed_resume_work = fixed_resumed["solver"]["initialization_work"]
    assert fixed_resume_work["static_optimizer_run_this_call"] is False
    assert fixed_resume_work["estimated_work_units_this_run"] == 0
    assert fixed_resume_work["checkpoint_proof_verification_work_this_run"] > 0
    invalid_initializer = deepcopy(equilibrium_simulation)
    invalid_initializer["initial_equilibrium"]["accepted"] = True
    reject_json(client, "/api/simulation/dynamic", {"config": invalid_initializer})
    invalid_initializer = deepcopy(equilibrium_simulation)
    invalid_initializer["initial_equilibrium"]["solver"] = {"max_work_units": 1}
    insufficient_initial_work = reject_json(client, "/api/simulation/prepare-equilibrium-initial", {
        "project": equilibrium_project, "config": invalid_initializer})
    assert "declared max_work_units" in json.dumps(insufficient_initial_work), insufficient_initial_work

    # Make a real source-derived mechanical grid. The explicit sea-surface
    # height h=.75 in the source datum must translate 12m depth to z=-12.75m.
    # This is a small aligned synthetic case, not a tidal model or shipplan
    # touchdown controller. Every sampled grid node and its source are retained.
    grid_project = deepcopy(terrain_example["project"])
    grid_project["terrain_sources"] = [grid_project["terrain_sources"][0]]
    flat_source = grid_project["terrain_sources"][0]
    flat_source["text"] = flat_source["text"].replace("100", "12")
    for key in ("fingerprint", "content_sha256", "byte_count"):
        flat_source.pop(key, None)
    generated_bathymetry = post_json(client, "/api/terrain/bathymetry", {
        "project": grid_project, "config": {"origin": [118, 22], "bounds_m": [-40, -40, 40, 40],
        "nx": 5, "ny": 5, "vertical_datum": "synthetic-demo-datum", "sea_surface_height_m": .75}})
    assert generated_bathymetry["can_apply"] and generated_bathymetry["validation_status"] == "research"
    physical_grid = generated_bathymetry["seabed_grid"]
    assert np.allclose(np.asarray(physical_grid["z_m"]), -12.75, rtol=0, atol=1e-9)
    assert generated_bathymetry["derivation"]["sea_surface_height_m"] == .75
    assert generated_bathymetry["derivation"]["missing_nodes"] == 0
    assert physical_grid["source"]["sha256"] == generated_bathymetry["derivation"]["library_signature"]
    assert all(row["source_id"] == "example-background" for row in generated_bathymetry["samples"])
    missing_height = deepcopy({"origin": [118, 22], "bounds_m": [-40, -40, 40, 40],
        "nx": 5, "ny": 5, "vertical_datum": "synthetic-demo-datum"})
    reject_json(client, "/api/terrain/bathymetry", {"project": grid_project, "config": missing_height})
    grid_simulation = {"seabed_grid": physical_grid, "depth_m": 12.75, "wet_weight_n_m": 4,
        "bottom_tension_n": 10, "nodes": 12, "ship_speed_m_s": .2, "payout_m_s": .4,
        "heading_deg": 90, "dt_s": .25, "internal_dt_s": .025, "solver_iterations": 24,
        "ea_n": 1e4, "seabed_friction": .5}
    grid_full = post_json(client, "/api/simulation/dynamic", {"config": {
        **grid_simulation, "duration_s": 1.5, "checkpoint_times_s": [.75]}})
    assert grid_full["model"].endswith("v3") and grid_full["solver"]["converged"]
    assert grid_full["checkpoint"]["schema_version"] == 2
    assert grid_full["checkpoint"]["config"]["seabed_grid"] == physical_grid
    assert grid_full["summary"]["material_balance_residual_m"] < 1e-8
    read_checkpoint(grid_full["checkpoint"])
    grid_resumed = post_json(client, "/api/simulation/dynamic", {"config": {
        "resume_state": grid_full["checkpoints"][0], "duration_s": .75}})
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_seabed_normal",
                "node_contact_normal_impulse_n_s", "node_contact_friction_impulse_n_s"):
        assert np.allclose(np.asarray(grid_resumed["checkpoint"]["state"][key]),
                           np.asarray(grid_full["checkpoint"]["state"][key]), rtol=1e-9, atol=1e-8), key
    grid_job = post_json(client, "/api/voyage/jobs", {"project": {}, "config": {
        "simulation": grid_simulation, "duration_s": .75, "chunk_duration_s": .75,
        "adaptive_mesh": {"enabled": False}, "max_total_work_units": 2_000_000,
        "max_chunks": 8, "max_output_frames": 16, "max_mesh_records": 8}})
    grid_parent_id = grid_job["id"]
    wait_completed(client, grid_parent_id)
    grid_parent_checkpoint = get_json(client, "/api/voyage/jobs/"+grid_parent_id+"/checkpoint")
    assert grid_parent_checkpoint["physical_checkpoint"]["schema_version"] == 2
    assert grid_parent_checkpoint["physical_checkpoint"]["config"]["seabed_grid"] == physical_grid
    read_voyage_checkpoint(grid_parent_checkpoint)

    # Synthetic observations are generated by the real forward operator. They
    # are explicitly synthetic, not field data, and truth is removed before fit.
    truth_config = {
        "reference_frame": {"kind": "local_enu", "origin_wgs84": [118, 22, 0]},
        "line": {"depth_m": 100, "wet_weight_n_m": 4, "diameter_m": .02,
                 "bottom_tension_n": 250, "nodes": 64},
        "snapshots": [{"id": "synthetic-smoke", "time_s": 0,
                       "vessel_position_m": [0, 0, 0], "ship_speed_m_s": 1,
                       "heading_deg": 90,
                       "observations": [{"id": "synthetic-" + str(i),
                                         "arc_from_vessel_m": arc,
                                         "sigma_m": [.25, .25, .25]}
                                        for i, arc in enumerate([20, 50, 80])]}],
        "current_m_s": [.25, .2]}
    predicted = post_json(client, "/api/seismic/predict", {"config": truth_config})
    assert len(predicted["snapshots"][0]["nodes"]) == 64
    inverse_config = deepcopy(truth_config)
    inverse_config.pop("current_m_s")
    inverse_config["initial_current_m_s"] = [0, 0]
    for row, forward in zip(inverse_config["snapshots"], predicted["snapshots"]):
        for observation, calculated in zip(row["observations"], forward["observations"]):
            observation["position_m"] = calculated["predicted_position_m"]
    fitted = post_json(client, "/api/seismic/estimate", {"config": inverse_config})
    assert fitted["summary"]["estimate_accepted"] is True, fitted["summary"]
    assert fitted["identifiability"]["rank"] == 2
    assert fitted["summary"]["covariance_valid"] is True
    assert fitted["parameter_covariance_m2_s2"] is not None
    assert fitted["solver"]["forward_snapshot_solves"] > 0
    assert all(abs(value - expected) < 1e-6
               for value, expected in zip(fitted["estimated_current_m_s"], [.25, .2]))

    # Small, real background physics; no route-to-dynamics inference or long-time
    # accuracy/coarsening claim. Supply all material defaults independently.
    simulation = {"depth_m": 10, "wet_weight_n_m": 4, "bottom_tension_n": 10,
                  "nodes": 16, "ship_speed_m_s": .5, "payout_m_s": .6,
                  "heading_deg": 90, "current_x_m_s": 0, "current_y_m_s": 0,
                  "diameter_m": .02, "mass_kg_m": .7298997321841252,
                  "drag_coefficient": 1.2, "water_density_kg_m3": 1025,
                  "added_mass_coefficient": 1, "damping_ratio": .03,
                  "seabed_friction": .5, "ea_n": 1e8, "ei_n_m2": 0,
                  "max_tension_n": 1e12, "min_bend_radius_m": 0,
                  "internal_dt_s": .05, "dt_s": .5, "solver_iterations": 24}
    job = post_json(client, "/api/voyage/jobs", {"project": {}, "config": {
        "simulation": simulation, "duration_s": 2, "chunk_duration_s": 1,
        "adaptive_mesh": {"enabled": False}, "max_total_work_units": 2_000_000,
        "max_chunks": 8, "max_output_frames": 16, "max_mesh_records": 8}})
    parent_id = job["id"]
    parent = wait_completed(client, parent_id)
    checkpoint = get_json(client, "/api/voyage/jobs/" + parent_id + "/checkpoint")
    result = get_json(client, "/api/voyage/jobs/" + parent_id + "/result")
    read_voyage_checkpoint(checkpoint)
    assert checkpoint["schema"] == "oceanroute.voyage.checkpoint"
    assert checkpoint["physical_checkpoint"]["time_s"] == 2
    assert result["status"] == "completed" and result["summary"]["end_time_s"] == 2
    assert result["frames"][0]["time_s"] == 0 and result["frames"][-1]["time_s"] == 2
    assert all(len(frame["nodes"]) >= 16 for frame in result["frames"])
    assert abs(result["summary"]["material_balance_residual_m"]) < 1e-8
    assert math.isclose(result["summary"]["paid_out_m"], 1.2, abs_tol=1e-9)
    continuation = post_json(client, "/api/voyage/jobs/" + parent_id + "/resume", {
        "duration_s": 1, "chunk_duration_s": 1, "max_total_work_units": 2_000_000,
        "max_chunks": 8, "max_output_frames": 16, "max_mesh_records": 8})
    child_id = continuation["id"]
    child = wait_completed(client, child_id)
    assert child["parent_job_id"] == parent_id
    resumed = get_json(client, "/api/voyage/jobs/" + child_id + "/result")
    assert resumed["summary"]["start_time_s"] == 2
    assert resumed["summary"]["end_time_s"] == 3
    assert resumed["frames"][0]["time_s"] == 2 and resumed["frames"][-1]["time_s"] == 3
    assert math.isclose(resumed["summary"]["paid_out_m"], 1.8, abs_tol=1e-9)
    assert abs(resumed["summary"]["material_balance_residual_m"]) < 1e-8
    read_voyage_checkpoint(resumed["checkpoint"])

    # Flat-bed route preparation is a separate, explicit research workflow.
    # Its initial suspended stock is inventory, not additional paid-out cable.
    planned_project = {"crs": "EPSG:4326", "route": {"curve": "rhumb", "slack_pct": 2,
        "points": [{"id": "start", "longitude": 0, "latitude": 0, "depth_m": 10},
                   {"id": "end", "longitude": math.degrees(300/6378137), "latitude": 0, "depth_m": 10}],
        "legs": [{"cable_type_id": "A"}]}, "cable_types": [{"id": "A", "lay_speed_m_s": .5,
        "wet_weight_n_m": 4, "diameter_m": .02, "cost_per_m": 1, "ea_n": 1e6, "ei_n_m2": 0}], "bodies": []}
    prepared = post_json(client, "/api/shipplan/prepare-voyage", {"project": planned_project,
        "config": {"plan": {"bottom_tension_n": 10, "sample_spacing_m": 30}, "duration_s": 2,
                   "simulation": {"dt_s": .25, "internal_dt_s": .05},
                   "voyage": {"adaptive_mesh": {"enabled": False}, "chunk_duration_s": 1}}})
    mapping = prepared["mapping"]
    assert mapping["source_plan_start_s"] > 0 and mapping["manufacturing_origin_m"] == 0
    assert math.isclose(mapping["initial_manufacturing_top_m"], mapping["initial_natural_length_m"], abs_tol=1e-9)
    prepared_full = post_json(client, "/api/voyage/run", {"project": planned_project, "config": prepared["config"]})
    assert prepared_full["status"] == "completed" and prepared_full["plan_mapping"] == mapping
    assert math.isclose(prepared_full["summary"]["paid_out_m"], 1.02, abs_tol=1e-9)
    assert math.isclose(prepared_full["frames"][0]["node_material_m"][0], mapping["initial_manufacturing_top_m"], abs_tol=1e-9)
    assert math.isclose(prepared_full["frames"][-1]["node_material_m"][0], mapping["final_manufacturing_top_m"], abs_tol=1e-9)
    assert abs(prepared_full["summary"]["material_balance_residual_m"]) < 1e-8
    prepared_job = post_json(client, "/api/voyage/jobs", {"project": planned_project,
        "config": {**prepared["config"], "duration_s": 1}})
    prepared_parent_id = prepared_job["id"]
    wait_completed(client, prepared_parent_id)
    prepared_checkpoint = get_json(client, "/api/voyage/jobs/" + prepared_parent_id + "/checkpoint")
    assert prepared_checkpoint["plan_mapping"] == mapping
    read_voyage_checkpoint(prepared_checkpoint)
    for change in ("geometry", "properties"):
        stale_project = deepcopy(planned_project)
        if change == "geometry":
            stale_project["route"]["points"][-1]["latitude"] += .001
        else:
            stale_project["cable_types"][0]["wet_weight_n_m"] = 8
        rejected = reject_json(client, "/api/voyage/run", {"project": stale_project,
            "config": {"resume_state": prepared_checkpoint, "duration_s": 1}})
        assert "saved planning mapping" in json.dumps(rejected), rejected
    reject_json(client, "/api/voyage/run", {"project": stale_project, "config": prepared["config"]})

    # Separate geographic, variable-bed preparation. These payloads are embedded
    # here because examples are intentionally not shipped in the wheel. Natural
    # stock20 is already active; only the new plan payout may advance its top KP.
    geographic_project = deepcopy(planned_project)
    geographic_project["name"] = "Explicit synthetic wheel geographic equilibrium; not a survey"
    geographic_project["route"]["points"][-1]["depth_m"] = 11.
    geographic_project["cable_types"][0]["ea_n"] = 100000.
    geographic_grid = {"schema": "oceanroute.bathymetry.v1", "x_m": [-200., 0., 200.],
        "y_m": [-100., 0., 100.], "z_m": [[-30., -29., -30.], [-30., -30., -30.], [-30., -31., -30.]],
        "source": {"name": "explicit synthetic wheel projected variable bed; not a survey",
        "horizontal_crs": "EPSG:3857", "origin_projected_m": [11., -7.],
        "vertical_datum": "synthetic heights already aligned to model sea z=0"}}
    geographic_config = {"plan": {"bottom_tension_n": 10., "sample_spacing_m": 30.},
        "duration_s": .12, "simulation": {"nodes": 10, "internal_dt_s": .01,
        "dt_s": .02, "solver_iterations": 24},
        "voyage": {"adaptive_mesh": {"enabled": False}, "chunk_duration_s": .02,
        "max_total_work_units": 120000000, "max_chunks": 16,
        "max_output_frames": 32, "max_mesh_records": 16},
        "seabed_grid": geographic_grid,
        "equilibrium_start": {"anchor": {"longitude": math.degrees(14./6378137),
        "latitude": 0., "z_model_m": -10.}, "vessel_z_m": -2., "natural_length_m": 20.}}
    geographic_prepared = post_json(client, "/api/shipplan/prepare-voyage", {
        "project": geographic_project, "config": geographic_config})
    geographic_mapping = geographic_prepared["mapping"]
    assert geographic_mapping["schema_version"] == 2
    assert geographic_mapping["initial_state"] == "verified-discrete-equilibrium-zero-velocity"
    read_plan_mapping(json_roundtrip(geographic_mapping))
    assert geographic_mapping["source_plan_start_s"] > 0
    assert geographic_mapping["manufacturing_origin_m"] == 0
    assert math.isclose(geographic_mapping["initial_natural_length_m"], 20., abs_tol=1e-10)
    assert math.isclose(geographic_mapping["initial_manufacturing_top_m"], 20., abs_tol=1e-10)
    # Derive the actual starting geographic vessel from the public equatorial
    # instruction and declared stock, then use the analytic Mercator formula.
    # Neither tested frame/CRS helper generates this expected origin or shift.
    initial_instruction = next(row for row in geographic_prepared["plan"]["instructions"]
        if row["cable_start_m"] <= 20. <= row["cable_end_m"] and row["cable_end_m"] > row["cable_start_m"])
    initial_fraction = (20.-initial_instruction["cable_start_m"])/(initial_instruction["cable_end_m"]-initial_instruction["cable_start_m"])
    geographic_instruction_distance = 6378137.*abs(math.radians(
        initial_instruction["vessel_end"][0]-initial_instruction["vessel_start"][0]))
    assert initial_instruction["speed_m_s"] == .5 and geographic_instruction_distance > 0
    # Depth-dependent layback changes the vessel-track length: 2% route slack
    # is not by itself a .51m/s vessel payout. Use the independently measured
    # equatorial track and this interval's actual manufacturing stock instead.
    geographic_expected_rate = (initial_instruction["cable_end_m"]-initial_instruction["cable_start_m"])*.5/geographic_instruction_distance
    geographic_start_lon = initial_instruction["vessel_start"][0]+initial_fraction*(
        initial_instruction["vessel_end"][0]-initial_instruction["vessel_start"][0])
    assert initial_instruction["vessel_start"][1] == initial_instruction["vessel_end"][1] == 0.
    geographic_absolute = np.array([6378137.*math.radians(geographic_start_lon), 0.])
    geographic_shift = geographic_absolute-np.array([11., -7.])
    terrain_frame = geographic_mapping["terrain_frame"]
    assert terrain_frame["horizontal_crs"] == "EPSG:3857" and terrain_frame["vertical_translation_m"] == 0
    assert terrain_frame["original_grid"] == geographic_grid
    assert np.allclose(terrain_frame["origin_projected_m"], geographic_absolute, rtol=0, atol=1e-8)
    assert np.allclose(terrain_frame["translation_from_original_local_m"], geographic_shift, rtol=0, atol=1e-8)
    rebased_grid = geographic_prepared["config"]["simulation"]["seabed_grid"]
    assert np.allclose(rebased_grid["x_m"], np.asarray(geographic_grid["x_m"])-geographic_shift[0], rtol=0, atol=1e-8)
    assert np.allclose(rebased_grid["y_m"], np.asarray(geographic_grid["y_m"])-geographic_shift[1], rtol=0, atol=1e-8)
    assert rebased_grid["z_m"] == geographic_grid["z_m"]
    assert np.ptp(np.asarray(rebased_grid["z_m"])) == 2., "the variable bed must not be flattened"
    assert np.allclose(rebased_grid["source"]["origin_projected_m"], geographic_absolute, rtol=0, atol=1e-8)
    geographic_initial = geographic_prepared["config"]["simulation"]["initial_equilibrium"]
    assert geographic_initial["vessel_position_m"] == [0., 0., -2.]
    assert np.allclose(geographic_initial["anchor_position_m"],
                       [14.-geographic_absolute[0], 0., -10.], rtol=0, atol=1e-8)
    assert math.isclose(geographic_mapping["initial_target_touchdown_residual_m"],
        math.dist(geographic_mapping["initial_anchor_xy_m"], geographic_mapping["planned_start_touchdown_xy_m"]), abs_tol=1e-9)
    assert geographic_mapping["initial_equilibrium_preparation"]["provenance"]["verification"]["accepted"]
    assert "PLAN_OFFSETS_REMAIN_FLAT_LOCAL_FIRST_CUT" in {row["code"] for row in geographic_prepared["warnings"]}
    geographic_full = post_json(client, "/api/voyage/run", {
        "project": geographic_project, "config": geographic_prepared["config"]})
    assert geographic_full["status"] == "completed" and geographic_full["plan_mapping"] == geographic_mapping
    assert geographic_full["chunks"][0]["solver"]["initialization_work"]["static_optimizer_run_this_call"] is True
    assert all(row["solver"]["initialization_work"]["static_optimizer_run_this_call"] is False
               and row["solver"]["initialization_work"]["estimated_work_units_this_run"] == 0
               for row in geographic_full["chunks"][1:])
    geographic_physical = geographic_full["checkpoint"]["physical_checkpoint"]
    geographic_proof = assert_equilibrium_checkpoint(geographic_physical, rebased_grid, 0., 20.)
    assert geographic_full["frames"][0]["paid_out_m"] == 0
    assert math.isclose(geographic_full["frames"][0]["material_length_m"], 20., abs_tol=1e-10)
    assert math.isclose(geographic_full["frames"][0]["node_material_m"][0], 20., abs_tol=1e-10)
    assert all(frame["touchdown"] is None and frame["bottom_tension_n"] is None
               and frame["touchdown_detected"] is False and frame["ship"][2] == -2.
               for frame in geographic_full["frames"])
    geographic_payout = geographic_mapping["final_manufacturing_top_m"]-20.
    assert geographic_payout > 0 and math.isclose(geographic_payout, geographic_expected_rate*.12, abs_tol=1e-9)
    assert math.isclose(geographic_full["summary"]["paid_out_m"], geographic_payout, abs_tol=1e-9)
    assert abs(geographic_full["summary"]["material_balance_residual_m"]) < 1e-8
    geographic_job = post_json(client, "/api/voyage/jobs", {"project": geographic_project,
        "config": {**geographic_prepared["config"], "duration_s": .06}})
    geographic_parent_id = geographic_job["id"]
    wait_completed(client, geographic_parent_id)
    geographic_parent_checkpoint = json_roundtrip(get_json(client,
        "/api/voyage/jobs/"+geographic_parent_id+"/checkpoint"))
    read_voyage_checkpoint(geographic_parent_checkpoint)
    assert geographic_parent_checkpoint["plan_mapping"] == geographic_mapping
    assert_equilibrium_checkpoint(geographic_parent_checkpoint["physical_checkpoint"], rebased_grid, 0., 20.)
    assert math.isclose(geographic_parent_checkpoint["physical_checkpoint"]["state"]["paid_out_m"], geographic_expected_rate*.06, abs_tol=1e-9)
    for change in ("geometry", "properties"):
        stale_geographic_project = deepcopy(geographic_project)
        if change == "geometry":
            stale_geographic_project["route"]["points"][-1]["latitude"] += .001
        else:
            stale_geographic_project["cable_types"][0]["ea_n"] *= 2
        rejected = reject_json(client, "/api/voyage/run", {"project": stale_geographic_project,
            "config": {"resume_state": geographic_parent_checkpoint, "duration_s": .02}})
        assert "saved planning mapping" in json.dumps(rejected), rejected
    unbound_geographic = deepcopy(geographic_config)
    unbound_geographic["seabed_grid"]["source"]["horizontal_crs"] = "LOCAL_CARTESIAN_METRES"
    unbound_geographic["seabed_grid"]["source"]["origin_projected_m"] = [0., 0.]
    reject_json(client, "/api/shipplan/prepare-voyage", {"project": geographic_project, "config": unbound_geographic})

# A second app in the same directory proves lifespan released the writer lock
# and completed jobs, including the planning map, reload from durable files.
with TestClient(create_app(ProjectStore(store_path))) as reopened:
    recovered_parent = get_json(reopened, "/api/voyage/jobs/" + parent_id)
    recovered_child = get_json(reopened, "/api/voyage/jobs/" + child_id)
    assert recovered_parent["status"] == recovered_child["status"] == "completed"
    assert get_json(reopened, "/api/voyage/jobs/" + child_id + "/result")["summary"]["end_time_s"] == 3
    read_voyage_checkpoint(get_json(reopened, "/api/voyage/jobs/" + child_id + "/checkpoint"))
    assert get_json(reopened, "/api/workspaces/" + identifier)["saved_revision"] == 3
    reopened_terrain = get_json(reopened, "/api/workspaces/"+terrain_workspace_id)
    assert reopened_terrain == terrain_third
    assert reopened_terrain["terrain_sources"] == reprioritized
    reopened_terrain_analysis = post_json(reopened, "/api/workspace/analyze", reopened_terrain)
    assert all(row["summary"]["bottom_length_m"] is None for row in reopened_terrain_analysis["paths"])
    assert all("terrain_sources" not in p["project"] for p in reopened_terrain["paths"])
    reopened_query = post_json(reopened, "/api/terrain/query", {
        "sources": reopened_terrain["terrain_sources"], "points": terrain_example["points"]})
    assert all(math.isclose(row["depth_m"], 100, rel_tol=0, abs_tol=1e-8) for row in reopened_query["samples"][:3])
    assert all(row["source_id"] == "example-background" for row in reopened_query["samples"][:3])
    assert reopened_query["samples"][3]["depth_m"] is None

    # The durable job record contains the complete source-derived 2D grid,
    # source signature, actual contact state and model version, across owner
    # shutdown/reopen. Resume performs real physics and matches one run.
    assert get_json(reopened, "/api/voyage/jobs/"+grid_parent_id)["status"] == "completed"
    recovered_grid = get_json(reopened, "/api/voyage/jobs/"+grid_parent_id+"/checkpoint")
    assert recovered_grid == grid_parent_checkpoint
    assert recovered_grid["physical_checkpoint"]["config"]["seabed_grid"] == physical_grid
    grid_continuation = post_json(reopened, "/api/voyage/jobs/"+grid_parent_id+"/resume", {
        "duration_s": .75, "chunk_duration_s": .75})
    grid_child_id = grid_continuation["id"]
    grid_complete = wait_completed(reopened, grid_child_id)
    assert grid_complete["parent_job_id"] == grid_parent_id
    grid_result = get_json(reopened, "/api/voyage/jobs/"+grid_child_id+"/result")
    assert grid_result["summary"]["start_time_s"] == .75 and grid_result["summary"]["end_time_s"] == 1.5
    assert math.isclose(grid_result["summary"]["paid_out_m"], .6, rel_tol=0, abs_tol=1e-9)
    assert abs(grid_result["summary"]["material_balance_residual_m"]) < 1e-8
    grid_final_checkpoint = grid_result["checkpoint"]["physical_checkpoint"]
    assert grid_final_checkpoint["schema_version"] == 2
    assert grid_final_checkpoint["config"]["seabed_grid"] == physical_grid
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_seabed_normal",
                "node_contact_normal_impulse_n_s", "node_contact_friction_impulse_n_s"):
        assert np.allclose(np.asarray(grid_final_checkpoint["state"][key]),
                           np.asarray(grid_full["checkpoint"]["state"][key]), rtol=1e-9, atol=1e-8), key
    read_voyage_checkpoint(grid_result["checkpoint"])
    recovered_planned = get_json(reopened, "/api/voyage/jobs/" + prepared_parent_id + "/checkpoint")
    assert recovered_planned["plan_mapping"] == mapping
    continued_plan = post_json(reopened, "/api/voyage/jobs/" + prepared_parent_id + "/resume", {"duration_s": 1})
    prepared_child_id = continued_plan["id"]
    completed_plan = wait_completed(reopened, prepared_child_id)
    assert completed_plan["parent_job_id"] == prepared_parent_id
    prepared_resumed = get_json(reopened, "/api/voyage/jobs/" + prepared_child_id + "/result")
    assert prepared_resumed["plan_mapping"] == mapping
    assert prepared_resumed["summary"]["start_time_s"] == 1 and prepared_resumed["summary"]["end_time_s"] == 2
    assert math.isclose(prepared_resumed["summary"]["paid_out_m"], 1.02, abs_tol=1e-9)
    assert abs(prepared_resumed["summary"]["material_balance_residual_m"]) < 1e-8
    resumed_positions = prepared_resumed["checkpoint"]["physical_checkpoint"]["state"]["positions"]
    full_positions = prepared_full["checkpoint"]["physical_checkpoint"]["state"]["positions"]
    assert len(resumed_positions) == len(full_positions)
    for actual_position, expected_position in zip(resumed_positions, full_positions):
        assert len(actual_position) == len(expected_position) == 3
        assert all(math.isclose(a, b, rel_tol=0, abs_tol=1e-8) for a, b in zip(actual_position, expected_position))
    read_voyage_checkpoint(prepared_resumed["checkpoint"])
    # A saved two-second mapping cannot silently invent later controls.
    reject_json(reopened, "/api/voyage/run", {"project": planned_project,
        "config": {"resume_state": prepared_resumed["checkpoint"], "duration_s": 1}})

    # The new mapping and complete original/rebased terrain survive actual
    # writer shutdown. The child resumes real state, never static display nodes.
    assert get_json(reopened, "/api/voyage/jobs/"+geographic_parent_id)["status"] == "completed"
    recovered_geographic = get_json(reopened, "/api/voyage/jobs/"+geographic_parent_id+"/checkpoint")
    assert recovered_geographic == geographic_parent_checkpoint
    assert recovered_geographic["plan_mapping"]["terrain_frame"]["original_grid"] == geographic_grid
    assert recovered_geographic["physical_checkpoint"]["config"]["seabed_grid"] == rebased_grid
    geographic_continuation = post_json(reopened, "/api/voyage/jobs/"+geographic_parent_id+"/resume", {
        "duration_s": .06, "chunk_duration_s": .02})
    geographic_child_id = geographic_continuation["id"]
    assert wait_completed(reopened, geographic_child_id)["parent_job_id"] == geographic_parent_id
    geographic_resumed = get_json(reopened, "/api/voyage/jobs/"+geographic_child_id+"/result")
    assert geographic_resumed["summary"]["start_time_s"] == .06 and geographic_resumed["summary"]["end_time_s"] == .12
    assert geographic_resumed["plan_mapping"] == geographic_mapping
    assert all(row["solver"]["initialization_work"]["static_optimizer_run_this_call"] is False
               and row["solver"]["initialization_work"]["estimated_work_units_this_run"] == 0
               and row["solver"]["initialization_work"]["checkpoint_proof_verification_work_this_run"] > 0
               for row in geographic_resumed["chunks"])
    assert math.isclose(geographic_resumed["summary"]["paid_out_m"], geographic_payout, abs_tol=1e-9)
    assert abs(geographic_resumed["summary"]["material_balance_residual_m"]) < 1e-8
    geographic_final_checkpoint = geographic_resumed["checkpoint"]["physical_checkpoint"]
    assert_equilibrium_checkpoint(geographic_final_checkpoint, rebased_grid, 0., 20.)
    assert_same_physical_state(geographic_final_checkpoint, geographic_physical)
    assert geographic_final_checkpoint["state"]["initialization_provenance"] == recovered_geographic["physical_checkpoint"]["state"]["initialization_provenance"]
    assert all(frame["touchdown"] is None and frame["bottom_tension_n"] is None
               and frame["touchdown_detected"] is False for frame in geographic_resumed["frames"])
    read_voyage_checkpoint(json_roundtrip(geographic_resumed["checkpoint"]))
    reject_json(reopened, "/api/voyage/run", {"project": geographic_project,
        "config": {"resume_state": geographic_resumed["checkpoint"], "duration_s": .02}})

print(json.dumps(finite({
    "version": oceanroute.__version__, "wheel_module": str(oceanroute.__file__),
    "isolated_modules": modules, "assets": len(assets), "manual": True,
    "analysis": True, "revision_guard": True,
    "workspace": {"schema_version": 2, "migration": True, "shared_alternative": True,
                  "manufactured_total_m": manufactured, "saved_revision": 3,
                  "stale_save_rejected": True, "stale_restore_rejected": True,
                  "restored_path_count": 1},
    "seismic": {"observation_source": "synthetic_forward_model_no_noise",
                "transponders": 3, "nodes": 64, "estimate_accepted": True,
                "estimated_current_m_s": fitted["estimated_current_m_s"],
                "rank": 2, "covariance_valid": True},
    "voyage": {"completed_duration_s": 2, "resume_start_s": 2, "resume_end_s": 3,
               "paid_out_m": resumed["summary"]["paid_out_m"],
               "parent_frames": len(result["frames"]), "child_frames": len(resumed["frames"]),
               "checkpoint_validated": True, "durable_reopen": True,
               "writer_lifespan_reopened": True},
    "rpl_templates": {"fixed_unicode_records": parsed_fixed["accepted_rows"],
                      "fixed_multiline_records": True, "manufactured_length_m": 3500,
                      "quoted_multiline_csv_records": parsed_multiline["accepted_rows"],
                      "dms_csv_records": parsed_dms["accepted_rows"],
                      "template_json_roundtrip": True, "collect_blocks_apply": True,
                      "explicit_skip_bridge": True},
    "dtm": {"source": "synthetic_affine_surface", "source_points": 25,
            "method": "minimum_curvature", "converged": True,
            "unique_solution_verified": True, "solver_work_units": solver["work_units"],
            "data_max_abs_error_m": solver["data_max_abs_error_m"], "bln_roundtrip": True,
            "excluded_source_points": grid["metadata"]["boundary_excluded_source_points"],
            "hole_slice_missing_count": crossed_hole["summary"]["missing_count"],
            "hole_slice_bottom_length_m": None, "preview_ignored": True,
            "complete_slice_bottom_length_m": complete_slice["summary"]["bottom_length_m"]},
    "plan_voyage": {"assumptions": "known_flat_bed_homogeneous_analytical_zero_velocity_initial_state",
                    "source_plan_start_s": mapping["source_plan_start_s"],
                    "initial_natural_length_m": mapping["initial_natural_length_m"],
                    "manufacturing_origin_m": mapping["manufacturing_origin_m"],
                    "paid_out_m": prepared_resumed["summary"]["paid_out_m"],
                    "duration_s": 2, "resume_start_s": 1, "resume_end_s": 2,
                    "initial_inventory_not_repaid": True, "durable_mapping_reopen": True,
                    "resume_matches_uninterrupted_positions": True,
                    "stale_geometry_rejected": True, "stale_physics_rejected": True,
                    "prepared_window_overrun_rejected": True},
    "coordinates": {"source_crs": "EPSG:4326", "target_crs": "EPSG:32650",
                    "known_utm_xy_m": [utm["x"], utm["y"]], "reverse_roundtrip": True,
                    "ballpark": False, "best_available": True,
                    "bad_point_preserved": True, "partial_apply_blocked": True},
    "map_projection": {"target_crs": "EPSG:32650", "actual_xy": map_result["points"][0]["coordinates"],
                       "can_display": True},
    "workspace_terrain": {"selected_paths": len(atomic_terrain["paths"]), "can_apply": True,
                          "shared_assemblies": 1, "preview_does_not_save": True},
    "catenary_calculator": {"boundaries_exercised": 4, "length_bases_exercised": 2,
                            "natural_length_m": calculator_summary["natural_length_m"],
                            "stretched_arc_length_m": calculator_summary["stretched_arc_length_m"],
                            "actual_node_roundtrips": True, "scope": "synthetic unique-root wheel smoke"},
    "static_bathymetry": {"slope_accepted": slope_result["accepted"],
                          "curved_accepted": curved_result["accepted"],
                          "contact_nodes": curved_result["summary"]["contact_nodes"],
                          "minimum_segment_clearance_m": curved_result["segment_clearance"]["minimum_clearance_m"]},
    "initial_equilibrium": {"source": "explicit embedded synthetic fixed-end case, not a survey",
                            "raw_preparation_verified": True, "dynamic_model": fixed_full["model"],
                            "physical_checkpoint_schema_version": 3, "initial_natural_length_m": 20.,
                            "initial_manufacturing_origin_m": 7., "initial_manufacturing_top_m": 27.,
                            "initial_paid_out_m": 0., "off_bed_anchor_not_touchdown": True,
                            "independent_hooke_force_and_half_weight_reconstruction": True,
                            "max_initial_free_node_force_residual_n": float(np.max(np.linalg.norm(fixed_internal[1:-1], axis=1))),
                            "raw_accepted_object_rejected": True, "insufficient_initial_budget_rejected": True,
                            "json_checkpoint_resume_matches_uninterrupted_state": True,
                            "resume_static_optimizer_run": fixed_resume_work["static_optimizer_run_this_call"],
                            "resume_proof_verification_work": fixed_resume_work["checkpoint_proof_verification_work_this_run"]},
    "geographic_equilibrium_plan": {"source": "embedded synthetic projected variable bed, not a survey",
                                   "mapping_schema_version": 2, "physical_checkpoint_schema_version": 3,
                                   "horizontal_crs": "EPSG:3857", "analytic_mercator_origin_checked": True,
                                   "complete_terrain_translation_checked": True, "model_heights_preserved": True,
                                   "translation_from_original_local_m": terrain_frame["translation_from_original_local_m"],
                                   "initial_natural_length_m": 20., "initial_manufacturing_top_m": 20.,
                                   "initial_manufacturing_origin_m": 0., "initial_paid_out_m": 0.,
                                   "paid_out_m": geographic_resumed["summary"]["paid_out_m"],
                                   "initial_inventory_not_repaid": True, "off_bed_anchor_not_touchdown": True,
                                   "durable_mapping_and_full_terrain_reopened": True,
                                   "resume_start_s": .06, "resume_end_s": .12,
                                   "resume_matches_uninterrupted_state": True, "resume_does_not_reoptimize_static_state": True,
                                   "stale_geometry_rejected": True, "stale_physics_rejected": True,
                                   "unbound_local_geographic_frame_rejected": True, "prepared_window_overrun_rejected": True,
                                   "planned_moving_offsets_remain_flat_local_first_cut": True},
    "terrain_sources": {"source": "explicit_synthetic_XYZ_and_Surfer_hole",
                        "sources": len(normalized_sources["sources"]),
                        "query_depths_m": [row["depth_m"] for row in terrain_samples],
                        "query_source_ids": [row["source_id"] for row in terrain_samples],
                        "library_signature": normalized_sources["library_signature"],
                        "hole_fallback": True, "coverage_fallback": True, "all_missing_kept_null": True,
                        "profile_import_valid": True, "shared_paths": 2, "saved_revision": 3,
                        "priority_change_invalidates_both_profiles": True, "waypoint_depth_not_substituted": True,
                        "full_source_payload_reopened": True, "reopened_priority_query_verified": True},
    "bathymetry": {"model": generated_bathymetry["model"], "grid_nodes": 25,
                   "source_datum": generated_bathymetry["derivation"]["source_vertical_datum"],
                   "sea_surface_height_m": .75, "generated_z_m": -12.75,
                   "grid_sha256": generated_bathymetry["derivation"]["grid_sha256"],
                   "source_library_signature": physical_grid["source"]["sha256"],
                   "explicit_height_required": True, "missing_nodes": 0,
                   "dynamic_model": grid_full["model"], "physical_checkpoint_schema_version": 2,
                   "solver_converged": grid_full["solver"]["converged"],
                   "material_balance_residual_m": grid_result["summary"]["material_balance_residual_m"],
                   "duration_s": 1.5, "resume_start_s": .75, "resume_end_s": 1.5,
                   "direct_resume_matches_uninterrupted_state": True,
                   "durable_full_grid_and_contact_reopened": True,
                   "background_resume_matches_uninterrupted_state": True},
    "elapsed_s": time.monotonic() - started,
    "scope": "extracted wheel + existing interpreter dependencies including terrain + ASGI lifecycle; synthetic small cases, not clean install/browser/field accuracy/long-voyage certification"
})))
'''


CURRENT_SMOKE_CODE = r'''
from copy import deepcopy
import importlib
import json
from pathlib import Path
import time
import numpy as np
from fastapi.testclient import TestClient
from oceanroute.api import create_app
from oceanroute.storage import ProjectStore
from oceanroute.checkpoints import read_checkpoint

root=Path.cwd().resolve()
modules={}
for name in ('oceanroute.hydrodynamics','oceanroute.current_equilibrium','oceanroute.current_dynamics'):
    module=importlib.import_module(name)
    path=Path(module.__file__).resolve()
    assert path.is_relative_to(root)
    modules[name]=str(path.relative_to(root))
def post(client,path,value):
    response=client.post(path,json=value)
    assert response.status_code==200,(path,response.status_code,response.text[:1200])
    value=response.json();json.dumps(value,allow_nan=False);return value
def get(client,path):
    response=client.get(path);assert response.status_code==200,response.text;return response.json()
def wait(client,identifier):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        status=get(client,'/api/voyage/jobs/'+identifier)
        if status['status'] not in ('queued','running','cancelling'):
            assert status['status']=='completed',status
            return get(client,'/api/voyage/jobs/'+identifier+'/checkpoint')
        time.sleep(.02)
    raise AssertionError('current durable task timed out')
keys=('positions','velocities','rest_lengths_m','node_material_m','node_mass_kg','node_wet_weight_n')
results={}
database=root/'data/current-smoke.sqlite3'
started=time.monotonic()
with TestClient(create_app(ProjectStore(database))) as client:
    for filename in ('heterogeneous-initial-dynamic.json','heterogeneous-initial-plan-voyage.json',
                     'current-initial-dynamic.json','current-initial-plan-voyage.json'):
        source=json.loads((root/'examples'/filename).read_text())
        geographic='plan-voyage' in filename
        prepared=post(client,'/api/shipplan/prepare-voyage' if geographic else
                      '/api/simulation/prepare-equilibrium-initial',source)
        config=prepared['config'] if geographic else source['config']
        endpoint='/api/voyage/run' if geographic else '/api/simulation/dynamic'
        whole=post(client,endpoint,{'project':source['project'],'config':config})
        first=post(client,endpoint,{'project':source['project'],'config':{**config,'duration_s':.04}})
        continued=post(client,endpoint,{'project':{},'config':{'resume_state':first['checkpoint'],'duration_s':.04}})
        a=whole['checkpoint']['physical_checkpoint'] if geographic else whole['checkpoint']
        b=continued['checkpoint']['physical_checkpoint'] if geographic else continued['checkpoint']
        current=filename.startswith('current-')
        assert a['schema_version']==b['schema_version']==(4 if current else 3)
        proof=a['state']['initialization_provenance']
        assert proof['schema'].endswith('.v3' if current else '.v2')
        assert proof['verification']['accepted']
        for key in keys:assert a['state'][key]==b['state'][key],(filename,key)
        assert proof==b['state']['initialization_provenance']
        read_checkpoint(a);read_checkpoint(b)
        results[filename]={'schema_version':a['schema_version'],'model':a['model'],
                           'proof_schema':proof['schema'],'actual_requests':4,'six_arrays_exact':True}
        if filename=='current-initial-dynamic.json':
            historical=deepcopy(first['checkpoint']['state']['initialization_provenance'])
            changed=post(client,endpoint,{'project':{},'config':{'resume_state':first['checkpoint'],
                'duration_s':.04,'current_profile':[{'depth_m':0.,'x_m_s':-.3,'y_m_s':.2},
                                                {'depth_m':40.,'x_m_s':.1,'y_m_s':-.2}]}})
            assert changed['checkpoint']['state']['initialization_provenance']==historical
            assert changed['frames'][-1]['node_fluid_velocity_m_s']!=whole['frames'][-1]['node_fluid_velocity_m_s']
            assert not np.allclose(changed['checkpoint']['state']['positions'],a['state']['positions'],rtol=0,atol=1e-8)
            results[filename]['future_profile_changes_physics_and_preserves_history']=True
        if filename=='current-initial-plan-voyage.json':
            geo_expected=a;geo_project=source['project']
            job=post(client,'/api/voyage/jobs',{'project':geo_project,'config':{**config,'duration_s':.04}})
            parent=wait(client,job['id'])
            (root/'data/current-parent.json').write_text(json.dumps(parent,allow_nan=False))
with TestClient(create_app(ProjectStore(database))) as client:
    parent=get(client,'/api/voyage/jobs/'+job['id']+'/checkpoint')
    assert parent==json.loads((root/'data/current-parent.json').read_text())
    child=post(client,'/api/voyage/jobs/'+job['id']+'/resume',{'duration_s':.04})
    final=wait(client,child['id'])['physical_checkpoint']
    for key in keys:assert final['state'][key]==geo_expected['state'][key],key
    assert final['state']['initialization_provenance']==geo_expected['state']['initialization_provenance']
    assert final['schema_version']==4
print(json.dumps({'status':'passed','isolated_modules':modules,'examples':results,
    'durable_geographic_owner_reopen_json_child_resume':True,'elapsed_s':time.monotonic()-started,
    'scope':'extracted wheel with existing interpreter dependencies and explicitly copied synthetic source JSON; true ASGI lifecycle, not clean installation or field equivalence'},allow_nan=False))
'''


S57_SMOKE_CODE = r'''
from copy import deepcopy
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import time
import zipfile

import oceanroute
import pyogrio
from fastapi.testclient import TestClient
from oceanroute.api import create_app
from oceanroute.storage import ProjectStore

root = Path.cwd().resolve()
expected_version, expected_sha = sys.argv[1:3]
started = time.monotonic()
modules = {}
for name in ('oceanroute', 'oceanroute.s57', 'oceanroute.api', 'oceanroute.workspace',
             'oceanroute.workspace_storage', 'oceanroute.storage', 'oceanroute.sqlite_lifecycle'):
    module = importlib.import_module(name)
    location = Path(module.__file__).resolve()
    assert location.is_relative_to(root), (name, str(location), str(root))
    modules[name] = str(location.relative_to(root))
assert oceanroute.__version__ == expected_version
assert modules['oceanroute.s57'] == 'oceanroute/s57.py'
assert modules['oceanroute.sqlite_lifecycle'] == 'oceanroute/sqlite_lifecycle.py'
assert 'r' in pyogrio.list_drivers().get('S57', ''), 'Native S57 reader is unavailable'
assert tuple(map(int, pyogrio.__version__.split('.')[:2])) >= (0, 12)
fixture = root / 'fixtures/s57/US5A1KMJ.zip'
payload = fixture.read_bytes()
assert hashlib.sha256(payload).hexdigest() == expected_sha
cell_path = 'ENC_ROOT/US5A1KMJ/US5A1KMJ.000'
requests = []

def finite(value):
    json.dumps(value, allow_nan=False)
    return value

def get(client, path):
    response = client.get(path)
    requests.append(('GET', path))
    assert response.status_code == 200, (path, response.status_code, response.text[:1200])
    return finite(response.json())

def post(client, path, value):
    response = client.post(path, json=value)
    requests.append(('POST', path))
    assert response.status_code == 200, (path, response.status_code, response.text[:1200])
    return finite(response.json())

def upload(client, stage, config):
    path = '/api/import/s57' + ('/' + stage if stage else '')
    response = client.post(path, data={'config_json': json.dumps(config)},
                           files={'file': ('US5A1KMJ.zip', payload, 'application/octet-stream')})
    requests.append(('POST', path))
    assert response.status_code == 200, (path, response.status_code, response.text[:1200])
    result = finite(response.json())
    assert result['source']['sha256'] == expected_sha
    assert result['source']['input_bytes'] == len(payload)
    return result

database = root / 'data/s57-smoke.sqlite3'
with TestClient(create_app(ProjectStore(database))) as client:
    health = get(client, '/api/health')
    assert health['status'] == 'ok' and health['version'] == expected_version
    inspected = upload(client, 'inspect', {})
    assert inspected['stage'] == 'inspect' and not inspected['can_apply']
    assert inspected['reader']['native'] is False
    assert [cell['path'] for cell in inspected['cells']] == [cell_path]
    assert [update['number'] for update in inspected['cells'][0]['updates']] == [1, 2]
    catalog = upload(client, 'catalog', {'cells': [cell_path]})
    assert catalog['stage'] == 'catalog' and not catalog['can_apply']
    assert catalog['layers'] == [] and catalog['reader']['native']
    cell = catalog['cells'][0]
    assert cell['base_dsid']['DSID_UPDN'] == '0'
    assert cell['dsid']['DSID_UPDN'] == '2' and cell['applied_update_number'] == 2
    assert [update['dsid']['DSID_UPDN'] for update in cell['updates']] == ['1', '2']
    counts = {row['name']: row['feature_count'] for row in catalog['classes_catalog']}
    assert {key: counts[key] for key in ('SOUNDG', 'DEPARE', 'DEPCNT')} == {
        'SOUNDG': 4, 'DEPARE': 88, 'DEPCNT': 123}
    imported = upload(client, '', {'cells': [cell_path], 'classes': ['SOUNDG', 'DEPARE', 'DEPCNT']})
    assert imported['stage'] == 'import' and imported['accepted'] and imported['can_apply']
    assert imported['reader']['driver'] == 'S57' and imported['reader']['process_isolated']
    assert imported['reader']['pyogrio_version'] == pyogrio.__version__
    assert imported['reader']['gdal_version'] == pyogrio.__gdal_version_string__
    layers = {layer['source']['object_class']: layer for layer in imported['layers']}
    assert set(layers) == {'SOUNDG', 'DEPARE', 'DEPCNT'}
    assert all(layer['kind'] == 'reference' and layer['crs'] == 'EPSG:4326' for layer in layers.values())
    soundings = layers['SOUNDG']['geojson']['features']
    assert all(feature['geometry']['type'] == 'MultiPoint' for feature in soundings)
    sounding_points = [point for feature in soundings for point in feature['geometry']['coordinates']]
    assert len(soundings) == 4 and len(sounding_points) == 657
    assert all(len(point) == 3 for point in sounding_points)
    assert sounding_points[0] == [177.5191417, 51.9181095, 23.7]
    polygons = []
    for feature in layers['DEPARE']['geojson']['features']:
        geometry = feature['geometry']
        assert geometry is not None and geometry['type'] in ('Polygon', 'MultiPolygon')
        polygons.extend([geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates'])
    holes = sum(len(polygon)-1 for polygon in polygons)
    assert holes > 0, 'Native DEPARE polygon holes disappeared'
    source_evidence = imported['cells'][0]
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for layer in layers.values():
            source = layer['source']
            evidence = source['cell_evidence']
            assert all(evidence[key] == source_evidence[key] for key in evidence)
            assert source['source_sha256'] == expected_sha
            assert source['datum_units']['depth_unit_code'] == 1
            assert source['datum_units']['depth_units'] == 'm'
            assert source['datum_units']['sounding_datum_code'] == 12
            assert source['depth_is_engineering_water_depth'] is False
            for item in [evidence['base'], *evidence['updates']]:
                original = archive.read(item['path'])
                assert item['bytes'] == len(original)
                assert item['sha256'] == hashlib.sha256(original).hexdigest()
            native_reader = source['native_reader']
            assert native_reader['driver'] == 'S57' and native_reader['process_isolated']
            assert native_reader['pyogrio_version'] == pyogrio.__version__
            assert native_reader['gdal_version'] == pyogrio.__gdal_version_string__
            assert native_reader['options'] == imported['reader']['options']
            assert 'worker_path' not in json.dumps(source) and 'oceanroute-s57' not in json.dumps(source)
    sample = get(client, '/api/sample')
    workspace = post(client, '/api/workspace/migrate', {'project': sample})['workspace']
    saved_before = post(client, '/api/workspaces', workspace)['workspace']
    before = deepcopy(saved_before)
    initial_summary = post(client, '/api/workspace/analyze', before)['summary']
    candidate = post(client, '/api/workspace/action', {'workspace': before, 'config': {
        'action': 'update_shared', 'layers': before['layers'] + imported['layers']}})['workspace']
    assert before == saved_before, 'The submitted workspace was mutated'
    for key in ('paths', 'assemblies', 'associations', 'cable_types', 'terrain_sources'):
        assert candidate[key] == before[key], key
    assert candidate['layers'][-3:] == imported['layers']
    after_summary = post(client, '/api/workspace/analyze', candidate)['summary']
    assert after_summary['manufactured_total_m'] == initial_summary['manufactured_total_m']
    assert after_summary['deployment_path_count'] == initial_summary['deployment_path_count']
    # Preview/action is pure; persist the whole candidate in one revision.
    assert get(client, '/api/workspaces/' + before['id']) == saved_before
    saved = post(client, '/api/workspaces', candidate)['workspace']
    assert saved['saved_revision'] == saved_before['saved_revision'] + 1
    assert get(client, '/api/workspaces/' + saved['id']) == saved
with TestClient(create_app(ProjectStore(database))) as client:
    reopened = get(client, '/api/workspaces/' + saved['id'])
    assert reopened == saved
    assert reopened['layers'][-3:] == imported['layers']
    assert [layer['source'] for layer in reopened['layers'][-3:]] == [layer['source'] for layer in imported['layers']]
    for key in ('paths', 'assemblies', 'associations', 'cable_types', 'terrain_sources'):
        assert reopened[key] == saved_before[key], key
print(json.dumps(finite({'status': 'passed', 'version': oceanroute.__version__,
    'isolated_modules': modules,
    'fixture': {'path': str(fixture.relative_to(root)), 'bytes': len(payload), 'sha256': expected_sha,
                'source_url': 'https://www.charts.noaa.gov/ENCs/US5A1KMJ.zip', 'original_bytes_explicitly_copied': True},
    'native_reader': {'pyogrio_version': pyogrio.__version__, 'gdal_version': pyogrio.__gdal_version_string__,
                      'S57_driver': pyogrio.list_drivers()['S57'], 'process_isolated': True},
    'stages': ['inspect', 'catalog', 'import'], 'actual_http_requests': len(requests),
    'base_update_number': 0, 'applied_update_number': 2, 'soundg_features': 4, 'soundg_xyz_points': 657,
    'dep_are_polygon_holes': holes, 'reference_layers': len(imported['layers']),
    'source_file_chain_and_native_metadata_preserved': True,
    'atomic_shared_update': True, 'preview_not_persisted': True,
    'saved_revision': saved['saved_revision'], 'database_owner_reopened': True,
    'path_assembly_material_inventory_unchanged': True, 'elapsed_s': time.monotonic()-started,
    'scope': 'extracted wheel only for OceanRoute; existing interpreter pyogrio/GDAL dependencies and explicitly copied original NOAA ZIP; actual ASGI lifecycle, not clean installation, official navigation product or engineering sounding conversion'}), allow_nan=False))
'''


def smoke(wheel: Path, report_path: Path | None = None) -> dict:
    wheel = wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="oceanroute-wheel-") as directory:
        destination = Path(directory)
        with zipfile.ZipFile(wheel) as package:
            metadata_paths = [name for name in package.namelist()
                              if name.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise ValueError("wheel must contain exactly one distribution METADATA")
            metadata = Parser().parsestr(package.read(metadata_paths[0]).decode("utf-8"))
            if (metadata["Name"] or "").lower() != "oceanroute" or not metadata["Version"]:
                raise ValueError(f"expected OceanRoute METADATA with a version, got {metadata['Name']} {metadata['Version']}")
            package.extractall(destination)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(destination)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["OCEANROUTE_DATA_DIR"] = str(destination / "data")
        try:
            completed = subprocess.run([sys.executable, "-c", SMOKE_CODE, metadata["Version"]], cwd=destination,
                                       env=environment, check=True, capture_output=True,
                                       text=True, timeout=120)
        except subprocess.CalledProcessError as error:
            if error.stdout:
                print(error.stdout, file=sys.stderr, end="")
            if error.stderr:
                print(error.stderr, file=sys.stderr, end="")
            raise RuntimeError(f"wheel smoke child failed with exit code {error.returncode}") from None
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        report = json.loads(completed.stdout)
        if tuple(map(int, metadata["Version"].split("."))) >= (0, 7, 0):
            examples = destination / "examples"
            examples.mkdir()
            for name in ("heterogeneous-initial-dynamic.json", "heterogeneous-initial-plan-voyage.json",
                         "current-initial-dynamic.json", "current-initial-plan-voyage.json"):
                payload = (Path(__file__).resolve().parents[1]/"examples"/name).read_bytes()
                (examples/name).write_bytes(payload)
            current = subprocess.run([sys.executable, "-c", CURRENT_SMOKE_CODE], cwd=destination,
                                     env=environment, check=True, capture_output=True, text=True, timeout=120)
            if current.stderr:
                print(current.stderr, file=sys.stderr, end="")
            report["heterogeneous_and_current_equilibrium"] = json.loads(current.stdout)
        if tuple(map(int, metadata["Version"].split("."))) >= (0, 8, 0):
            fixture_source = Path(__file__).resolve().parents[1] / "tests/fixtures/s57/noaa/US5A1KMJ.zip"
            original_fixture = fixture_source.read_bytes()
            fixture_digest = hashlib.sha256(original_fixture).hexdigest()
            if fixture_digest != "ee3fb1a96da5e1da84ca8c00c8aca57c22e1168c78c07c0c9c361cf2e43eed34":
                raise RuntimeError("Original NOAA S57 fixture differs from the frozen source bytes")
            fixture_destination = destination / "fixtures/s57/US5A1KMJ.zip"
            fixture_destination.parent.mkdir(parents=True)
            fixture_destination.write_bytes(original_fixture)
            if fixture_destination.read_bytes() != original_fixture:
                raise RuntimeError("Original NOAA S57 fixture bytes changed during explicit copy")
            native = subprocess.run([sys.executable, "-c", S57_SMOKE_CODE, metadata["Version"], fixture_digest],
                                    cwd=destination, env=environment, check=True, capture_output=True,
                                    text=True, timeout=120)
            if native.stderr:
                print(native.stderr, file=sys.stderr, end="")
            report["native_s57"] = json.loads(native.stdout)
        report["wheel_metadata_version"] = metadata["Version"]
        report["wheel"] = str(wheel)
        report["wheel_bytes"] = wheel.stat().st_size
        report["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        report["python_executable"] = sys.executable
        report["platform"] = sys.platform
        if report_path is not None:
            report_path = report_path.resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                              allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--report", type=Path, help="write the successful smoke result as JSON")
    options = parser.parse_args()
    smoke(options.wheel, options.report)
