from copy import deepcopy
import base64
import json
import math
import warnings

import numpy as np
from pyproj import CRS, Transformer
from pyproj.transformer import TransformerGroup
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from oceanroute.core import analyze_project, route_signature
from oceanroute.terrain_sources import (TerrainSourceError, normalize_sources, terrain_library_signature,
    query_terrain, profile_from_sources, example_sources, clear_cache, cache_info)


def descriptor(kind, **options):
    return {"kind": kind, "source_crs": "EPSG:4326", "depth_positive": "down", "depth_units": "m",
            "vertical_datum": "chart-A", **options}


def tiff_source(values, crs, transform, **options):
    values = np.asarray(values, dtype="float32")
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", height=values.shape[0], width=values.shape[1], count=1,
                         dtype="float32", crs=crs, transform=transform, nodata=-9999) as dataset:
            dataset.write(values, 1)
        data = memory.read()
    return descriptor("geotiff", source_crs=str(crs), data_base64=base64.b64encode(data).decode(), **options)


def mixed_sources():
    x, y = Transformer.from_crs(4326, 3857, always_xy=True).transform(118, 22)
    values = np.full((3, 3), 300); values[1, 1] = -9999
    high = tiff_source(values, "EPSG:3857", from_origin(x-150, y+150, 100, 100), id="detail", priority=100)
    low = tiff_source(np.full((5, 5), -100), "EPSG:4326", from_origin(117.99, 22.01, .005, .005),
                      id="background", priority=10, depth_positive="up", depth_units="ft")
    reverse = Transformer.from_crs(3857, 4326, always_xy=True)
    points = [list(reverse.transform(x-100, y)), [118, 22], list(reverse.transform(x+500, y)), [119, 22]]
    return [low, high], points


def test_actual_different_crs_raster_hole_priority_nodata_fallback_units():
    sources, points = mixed_sources(); result = query_terrain(sources, points)
    rows = result["samples"]
    assert [r["source_id"] for r in rows] == ["detail", "background", "background", None]
    assert [r["depth_m"] for r in rows] == [300, 30.48, 30.48, None]
    assert rows[1]["fallback"] and rows[1]["fallback_count"] == 1
    assert [a["status"] for a in rows[1]["attempts"]] == ["nodata", "valid"]
    assert [a["status"] for a in rows[2]["attempts"]] == ["outside_coverage", "valid"]
    assert result["quality"]["missing_count"] == 1 and result["quality"]["fallback_count"] == 2
    assert result["quality"]["source_counts"] == {"detail": 1, "background": 2}
    assert not result["quality"]["complete"]
    json.dumps(result, allow_nan=False)


def test_real_missing_best_horizontal_grid_is_error_not_lower_source_fallback():
    # The normal CI/runtime installation does not contain OSTN15. If a user
    # installs it, this negative environment fixture is no longer applicable.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        group = TransformerGroup(4326, 27700, always_xy=True, allow_ballpark=False)
    if group.best_available:
        pytest.skip("OSTN15 best grid is installed; missing-grid fixture unavailable")
    high = tiff_source(np.full((3, 3), 300), "EPSG:27700",
                       from_origin(399850, 300150, 100, 100), id="missing-grid", priority=100)
    low = descriptor("xyz", id="background", priority=0,
        text="-3 50 100\n1 50 100\n-3 56 100\n1 56 100", sampling={"max_gap_m":1_000_000})
    with pytest.raises(TerrainSourceError, match="TERRAIN_CRS_OPERATION.*PROJ"):
        query_terrain([low, high], [[-2, 53]])


def test_actual_horizontal_operations_are_reported_without_claiming_measurement_accuracy():
    sources, points = mixed_sources()
    result = query_terrain(sources, points)
    for source in result["sources"]:
        assert source["horizontal_operations"]
        for operation in source["horizontal_operations"]:
            assert operation["ballpark"] is False and operation["best_available"] is True
            assert operation["description"] and operation["selection"].startswith("whole_source")
            assert operation["accuracy_m"] is None or operation["accuracy_m"] >= 0


def test_reorder_tie_id_enabling_and_priority_are_deterministic():
    sources, points = mixed_sources()
    a, b = query_terrain(sources, points), query_terrain(sources[::-1], points)
    assert a["samples"] == b["samples"] and a["quality"]["library_signature"] == b["quality"]["library_signature"]
    source = deepcopy(sources[1]); source["id"] = "aaa"; source["priority"] = 10
    assert query_terrain([sources[0], source], points[:1])["samples"][0]["source_id"] == "aaa"
    source["enabled"] = False
    assert query_terrain([sources[0], source], points[:1])["samples"][0]["source_id"] == "background"
    source["enabled"] = True; source["priority"] = 0
    assert query_terrain([sources[0], source], points[:1])["samples"][0]["source_id"] == "background"


def test_vertical_datum_conflicts_are_not_hidden_by_nonoverlap_or_priority():
    sources, points = mixed_sources(); sources[1]["vertical_datum"] = "chart-B"
    with pytest.raises(TerrainSourceError, match="DATUM_CONFLICT"):
        query_terrain(sources, points[:1])
    result = query_terrain(sources, points, {"vertical_datum": "chart-A"})
    assert result["quality"]["excluded_datum_source_ids"] == ["detail"]
    assert all(r["source_id"] in ("background", None) for r in result["samples"])
    assert result["quality"]["vertical_datum"] == "chart-A"
    sources[1]["enabled"] = False
    assert query_terrain(sources, points[:1])["quality"]["vertical_datum"] == "chart-A"
    empty = query_terrain(sources, points, {"vertical_datum": "unavailable-datum"})
    assert all(r["depth_m"] is None and r["source_id"] is None for r in empty["samples"])


def test_normalized_saved_payload_roundtrip_stable_ids_signatures_and_integrity():
    raw = descriptor("xyz", text="0 0 10\n.01 0 20\n0 .01 30", sampling={"method": "linear"})
    normalized = normalize_sources([raw]); again = normalize_sources(json.loads(json.dumps(normalized)))
    assert normalized == again and normalized[0]["id"].startswith("terrain-")
    assert terrain_library_signature(normalized) == terrain_library_signature(again)
    renamed = deepcopy(normalized); renamed[0]["name"] = "different display name"
    assert terrain_library_signature(renamed) == terrain_library_signature(normalized)
    changed = deepcopy(normalized); changed[0]["enabled"] = False
    assert terrain_library_signature(changed) != terrain_library_signature(normalized)
    changed = deepcopy(normalized); changed[0]["priority"] += 1
    assert changed[0]["fingerprint"] == normalized[0]["fingerprint"]
    assert terrain_library_signature(changed) != terrain_library_signature(normalized)
    corrupt = deepcopy(normalized); corrupt[0]["text"] += "\n.01 .01 40"
    with pytest.raises(TerrainSourceError, match="INTEGRITY"): normalize_sources(corrupt)
    changed = deepcopy(normalized); changed[0]["depth_units"] = "ft"
    with pytest.raises(TerrainSourceError, match="fingerprint"): normalize_sources(changed)
    changed[0].pop("fingerprint")
    assert normalize_sources(changed)[0]["fingerprint"] != normalized[0]["fingerprint"]


def test_browser_equivalent_numbers_and_negative_zero_keep_fingerprint():
    raw = descriptor("xyz", text="0 0 10\n.01 0 20\n0 .01 30", nodata_value=-0.0,
                     sampling={"method": "linear", "max_gap_m": 5000.0})
    a = normalize_sources([raw]); b = deepcopy(a)
    b[0]["nodata_value"] = 0; b[0]["sampling"]["max_gap_m"] = 5000
    assert normalize_sources(b)[0]["fingerprint"] == a[0]["fingerprint"]


def test_xyz_projected_coordinate_order_plane_no_extrapolation_gap_and_known_zero():
    local = CRS.from_proj4("+proj=aeqd +lat_0=22 +lon_0=118 +datum=WGS84 +units=m")
    source = descriptor("xyz", source_crs=local.to_string(), coordinate_order="yx",
        text="y x z\n-100 -100 0\n-100 100 2\n100 -100 4\n100 100 6",
        sampling={"method": "linear", "max_gap_m": 1000})
    reverse = Transformer.from_crs(local, 4326, always_xy=True)
    points = [list(reverse.transform(-100, -100)), [118, 22], list(reverse.transform(300, 0))]
    rows = query_terrain([source], points)["samples"]
    assert rows[0]["depth_m"] == pytest.approx(0, abs=1e-8) and rows[1]["depth_m"] == pytest.approx(3, abs=1e-7)
    assert rows[2]["depth_m"] is None and rows[2]["attempts"][0]["status"] == "outside_convex_hull"
    source["sampling"]["max_gap_m"] = 50
    assert query_terrain([source], [[118, 22]])["samples"][0]["attempts"][0]["status"] == "gap_exceeded"


def test_linear_xyz_exact_measured_zero_survives_one_ulp_native_transform_roundoff():
    from oceanroute import terrain_sources as sampler

    local = CRS.from_proj4("+proj=aeqd +lat_0=22 +lon_0=118 +datum=WGS84 +units=m")
    source = descriptor("xyz", id="native-zero-vertex-regression", source_crs=local.to_string(),
        coordinate_order="yx", text="y x z\n-100 -100 0\n-100 100 2\n100 -100 4\n100 100 6",
        sampling={"method": "linear", "max_gap_m": 1000})
    reverse = Transformer.from_crs(local, 4326, always_xy=True)
    point = list(reverse.transform(-100, -100))
    clear_cache()
    try:
        normalized = normalize_sources([source])[0]
        prepared = sampler._prepare(normalized, sampler._Budget(1_000_000))
        query = np.array([prepared["converter"].transform(*point)])
        distance, node = prepared["tree"].query(query)
        assert distance[0] == 0 and prepared["values"][node[0]] == 0
        simplex = int(prepared["tri"].find_simplex(query)[0])
        affine = prepared["tri"].transform[simplex]
        values = prepared["values"][prepared["tri"].simplices[simplex]]
        original = affine.copy()
        # Reproduce the actual Windows diagnostic without requiring that every
        # LAPACK/Qhull build happens to choose the same cancellation sign.
        # Only one coefficient of the complete 3x2 affine transform moves one
        # floating-point ULP, including its reference-point offset. Qhull may
        # put this zero-depth vertex in that reference row, so perturbing only
        # the linear 2x2 block would multiply an exactly zero displacement.
        # Source coordinates/depth, coverage and the exact nearest hit stay.
        negative_estimate = None
        for row, col, direction in ((r, c, d) for r in range(3) for c in range(2)
                                    for d in (-math.inf, math.inf)):
            affine[:] = original
            affine[row, col] = np.nextafter(affine[row, col], direction)
            bary = affine[:2] @ (query[0] - affine[2])
            estimate = float(np.dot(np.r_[bary, 1 - bary.sum()], values))
            if estimate < 0:
                negative_estimate = estimate
                break
        assert negative_estimate is not None and abs(negative_estimate) < 1e-12
        result = query_terrain([source], [point, list(reverse.transform(300, 0))])
        assert result["samples"][0]["depth_m"] == 0.0
        assert result["samples"][0]["attempts"][0]["status"] == "valid"
        assert result["samples"][1]["depth_m"] is None
        assert result["samples"][1]["attempts"][0]["status"] == "outside_convex_hull"
        json.dumps(result, allow_nan=False)
    finally:
        clear_cache()


@pytest.mark.parametrize("method", ["linear", "idw"])
def test_xyz_explicit_missing_nodes_do_not_become_zero_or_deleted_hole(method):
    source = descriptor("xyz", nodata_value=-9999,
        text="-.01 -.01 100\n.01 -.01 100\n-.01 .01 100\n.01 .01 100\n0 0 -9999",
        sampling={"method": method, "max_gap_m": 3000})
    rows = query_terrain([source], [[0, 0], [-.01, -.01]])["samples"]
    assert rows[0]["depth_m"] is None and rows[0]["attempts"][0]["status"] == "nodata"
    assert rows[1]["depth_m"] == pytest.approx(100)


def test_date_line_xyz_and_unwrapped_geographic_raster_are_short_local_sources():
    xyz = descriptor("xyz", text="179.98 -.01 100\n-179.98 -.01 100\n179.98 .01 100\n-179.98 .01 100")
    tiff = tiff_source(np.full((2, 4), 200), "EPSG:4326", from_origin(179.98, .01, .01, .01), priority=200)
    rows = query_terrain([xyz, tiff], [[179.99, 0], [-179.99, 0]])["samples"]
    assert [r["depth_m"] for r in rows] == [200, 200]
    rows = query_terrain([xyz], [[179.99, 0], [-179.99, 0]])["samples"]
    assert all(r["depth_m"] == pytest.approx(100) for r in rows)


def test_bilinear_centres_zero_weight_mask_and_outside_are_preserved():
    source = tiff_source([[10, 20], [-9999, 40]], "EPSG:4326", from_origin(0, 2, 1, 1), sampling={"method": "bilinear"})
    rows = query_terrain([source], [[.5, 1.5], [1, 1.5], [1, 1], [.1, 1.5]])["samples"]
    assert rows[0]["depth_m"] == 10 and rows[1]["depth_m"] == 15
    assert rows[2]["depth_m"] is None and rows[2]["attempts"][0]["status"] == "nodata"
    assert rows[3]["depth_m"] is None and rows[3]["attempts"][0]["status"] == "outside_coverage"


def test_real_example_route_profile_provenance_kp_signature_and_core_bottom_distance():
    example = example_sources(); before = deepcopy(example["project"])
    result = profile_from_sources(before, example["config"])
    assert before == example["project"]  # preview is pure
    assert result["profile"]["route_signature"] == route_signature(before)
    assert result["profile"]["metadata"]["terrain_library_signature"] == terrain_library_signature(before["terrain_sources"])
    assert result["quality"]["source_counts"].keys() == {"example-detail", "example-background"}
    assert result["quality"]["fallback_count"] > 0 and result["quality"]["missing_count"] == 0
    assert all(b["kp_m"] > a["kp_m"] for a, b in zip(result["profile"]["samples"], result["profile"]["samples"][1:]))
    assert all("attempts" not in row for row in result["profile"]["samples"])
    actual = analyze_project(result["project"])
    assert actual["profile_metadata"]["imported_profile_valid"]
    assert actual["summary"]["bottom_length_m"] > actual["summary"]["surface_length_m"]


def test_repeated_route_points_and_stale_route_signature_are_not_duplicate_kp():
    example = example_sources(); p = example["project"]
    p["route"]["points"].insert(1, {**p["route"]["points"][0], "id": "repeat"})
    result = profile_from_sources(p, {"spacing_m": 200})
    assert len({row["kp_m"] for row in result["profile"]["samples"]}) == len(result["profile"]["samples"])
    result["project"]["route"]["points"][-1]["latitude"] += .001
    assert not analyze_project(result["project"])["profile_metadata"]["imported_profile_valid"]


def test_no_enabled_sources_remain_explicit_missing_not_waypoint_depth():
    example = example_sources(); p = example["project"]
    p["terrain_sources"] = []; p["route"]["points"][0]["depth_m"] = 55; p["route"]["points"][1]["depth_m"] = 55
    result = profile_from_sources(p)
    assert result["quality"]["missing_count"] == result["quality"]["sample_count"]
    assert all(s["depth_m"] is None for s in result["samples"])
    assert analyze_project(result["project"])["summary"]["bottom_length_m"] is None


@pytest.mark.parametrize("config", [{"max_work_units": 1}, {"max_output_bytes": 1024}, {"max_query_points": 1}, [], {"max_query_points": True}, {"spacing_m": 1}])
def test_work_output_point_and_route_budgets_reject_without_partial_profile(config):
    example = example_sources()
    if config == {"spacing_m": 1}:
        config = {"spacing_m": 1, "max_query_points": 10}
    with pytest.raises(ValueError):profile_from_sources(example["project"], config)


@pytest.mark.parametrize("mutate", ["datum", "crs", "data", "duplicate-id", "xyz-line", "schema", "unsafe", "sampling"])
def test_invalid_sources_fail_with_location_or_metadata_not_silent_fallback(mutate):
    source = descriptor("xyz", text="0 0 100\n.01 0 100\n0 .01 100")
    if mutate == "datum":source["vertical_datum"] = "user-unspecified"
    elif mutate == "crs":source["source_crs"] = "unknown crs"
    elif mutate == "data":source = descriptor("surfer", data_base64="corrupt!")
    elif mutate == "xyz-line":source["text"] += "\nbad row"
    elif mutate == "schema":source["schema_version"] = 1.0
    elif mutate == "unsafe":source["file_path"] = "/tmp/data"
    elif mutate == "sampling":source["sampling"] = {"method": "fake"}
    sources = [source, deepcopy(source)] if mutate == "duplicate-id" else [source]
    with pytest.raises(ValueError):normalize_sources(sources)


def test_embedded_crs_conflict_corrupt_grid_and_duplicate_xyz_geometry_rejected():
    source = tiff_source([[100]], "EPSG:3857", from_origin(0, 100, 100, 100)); source["source_crs"] = "EPSG:4326"
    with pytest.raises(TerrainSourceError, match="CRS"):normalize_sources([source])
    source = descriptor("xyz", text="0 0 10\n.01 0 20\n0 .01 30\n0 0 11")
    with pytest.raises(TerrainSourceError, match="DUPLICATE"):query_terrain([source], [[0, 0]])
    source["text"] = "0 0 1\n.01 0 1\n.02 0 1"
    with pytest.raises(TerrainSourceError, match="GEOMETRY"):query_terrain([source], [[0, 0]])


def test_cache_hit_same_material_values_and_budget_accounting_and_identity_updates():
    clear_cache(); example = example_sources()
    a = query_terrain(example["sources"], example["points"])
    b = query_terrain(example["sources"], example["points"])
    assert a["samples"] == b["samples"] and a["budget"]["work_units"] == b["budget"]["work_units"]
    assert b["budget"]["cache_hits"] == 2 and b["budget"]["cache_misses"] == 0
    info = cache_info(); assert info["entries"] == 2 and 0 < info["estimated_array_bytes"] <= info["max_estimated_array_bytes"]
    clear_cache(); assert cache_info()["entries"] == 0


def test_actual_200000_xyz_nodes_are_not_artificially_limited_to_20000():
    # Exactly the existing XYZ node limit, using a real projected point cloud,
    # an actual Delaunay interpolant and a geographic query in its interior.
    text = "x y z\n"+"\n".join(f"{x} {y} 100" for x in range(400) for y in range(500))
    source = descriptor("xyz", source_crs="EPSG:3857", text=text,
                        sampling={"method": "linear", "max_gap_m": 1000})
    longitude, latitude = Transformer.from_crs(3857, 4326, always_xy=True).transform(199.5, 249.5)
    result = query_terrain([source], [[longitude, latitude]])
    assert result["samples"][0]["depth_m"] == pytest.approx(100)
    assert result["budget"]["work_units"] >= 200000
    # One extra node is rejected before triangulation; none are silently lost.
    source["text"] += "\n400 500 100"
    with pytest.raises(TerrainSourceError, match="200,000"):normalize_sources([source])
    clear_cache()


def test_actual_50000_queries_keep_every_point_and_priority_provenance_with_explicit_output_budget():
    example = example_sources()
    points = [[117.99+.02*i/49999, 22] for i in range(50000)]
    with pytest.raises(TerrainSourceError, match="OUTPUT_BUDGET"):
        query_terrain(example["sources"], points)
    result = query_terrain(example["sources"], points, {"max_output_bytes": 64*1024**2})
    assert result["quality"]["sample_count"] == len(result["samples"]) == 50000
    assert result["quality"]["missing_count"] == 0
    assert result["quality"]["source_counts"] == {"example-detail": 25000, "example-background": 25000}
    assert result["samples"][0]["depth_m"] == result["samples"][-1]["depth_m"] == 300
    assert result["samples"][25000]["depth_m"] == 100 and result["samples"][25000]["fallback"]
    with pytest.raises(TerrainSourceError, match="50,000"):
        query_terrain(example["sources"], points+[points[0]], {"max_output_bytes": 64*1024**2})


@pytest.mark.parametrize("mutation", ["priority", "enabled", "delete", "content", "units", "method", "datum"])
def test_core_library_bridge_cannot_repair_stale_observations_from_waypoint_depths(mutation):
    example = example_sources(); p = profile_from_sources(example["project"], example["config"])["project"]
    for point in p["route"]["points"]:point["depth_m"] = 55
    sources = p["terrain_sources"]
    if mutation == "priority":sources[0]["priority"] += 1
    elif mutation == "enabled":sources[0]["enabled"] = False
    elif mutation == "delete":sources.pop()
    else:
        source = sources[0]
        if mutation == "content":source["text"] = source["text"].replace("100", "101")
        elif mutation == "units":source["depth_units"] = "ft"
        elif mutation == "method":source["sampling"]["method"] = "idw"
        else:source["vertical_datum"] = "other-explicit-datum"
        for key in ("fingerprint", "content_sha256", "byte_count"):source.pop(key, None)
    actual = analyze_project(p)
    assert actual["profile_metadata"]["terrain_library_valid"] is False
    assert actual["profile_metadata"]["imported_profile_valid"] is False
    assert actual["summary"]["bottom_length_m"] is None
    assert all(point["depth_m"] is None for point in actual["profile"])
    assert any(w["code"] == "TERRAIN_LIBRARY_STALE" for w in actual["warnings"])


def test_actual_http_normalize_query_profile_and_metadata_errors(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    with TestClient(create_app(ProjectStore(tmp_path/"projects.sqlite3"))) as client:
        response = client.get("/api/terrain/sources/example"); assert response.status_code == 200
        fixture = response.json()
        response = client.post("/api/terrain/sources/normalize", json={"sources": fixture["sources"]})
        assert response.status_code == 200, response.text
        normalized = response.json(); assert set(normalized) == {"sources", "library_signature"}
        assert normalized["library_signature"] == terrain_library_signature(normalized["sources"])
        response = client.post("/api/terrain/query", json={"sources": normalized["sources"], "points": fixture["points"]})
        assert response.status_code == 200, response.text
        depths = [r["depth_m"] for r in response.json()["samples"]]
        assert depths[:3] == pytest.approx([300, 100, 100])
        assert depths[3] is None
        assert "project" not in response.json()
        response = client.post("/api/terrain/profile", json={"project": fixture["project"], "config": fixture["config"]})
        assert response.status_code == 200, response.text
        profile = response.json(); assert profile["profile"] == profile["project"]["profile"]
        assert profile["profile"]["metadata"]["terrain_library_signature"] == normalized["library_signature"]
        response = client.post("/api/analyze", json=profile["project"])
        assert response.status_code == 200 and response.json()["profile_metadata"]["terrain_library_valid"]
        bad = deepcopy(normalized["sources"]); bad[0]["vertical_datum"] = "different"
        bad[0].pop("fingerprint")
        response = client.post("/api/terrain/query", json={"sources": bad, "points": fixture["points"]})
        assert response.status_code == 422 and "DATUM_CONFLICT" in response.text
        response = client.post("/api/terrain/profile", json={"project": fixture["project"], "config": {"max_work_units": 1}})
        assert response.status_code == 422 and "WORK_BUDGET" in response.text
