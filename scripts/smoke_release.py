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
             "oceanroute.catenary_calculator"):
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
