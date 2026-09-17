"""Independent affine fields, WGS84 distances and missing-support counterexamples."""
import base64
from copy import deepcopy
import json
import math

import numpy as np
from pyproj import Geod, Transformer
import pytest

from oceanroute.terrain_slope_neighborhoods import (SlopeNeighborhoodSampler,
                                                   sample_slope_neighborhood)
from oceanroute.terrain_sources import TerrainSourceError, clear_cache

G = Geod(ellps="WGS84")


def raster(field=lambda x, y: 1000+.2*x-.07*y, *, center=(0, 0), xbounds=(-200, 200),
           ybounds=(-200, 200), nodes=9, method="linear", **metadata):
    x, y = np.linspace(*xbounds, nodes), np.linspace(*ybounds, nodes)
    values = [[field(xx, yy) for xx in x] for yy in y]
    text = (f"DSAA\n{nodes} {nodes}\n{xbounds[0]} {xbounds[1]}\n{ybounds[0]} {ybounds[1]}\n0 1000000\n"+
            "\n".join(" ".join(str(z) for z in row) for row in values)+"\n")
    return {"id": "plane", "kind": "surfer", "source_crs":
            f"+proj=aeqd +lat_0={center[1]} +lon_0={center[0]} +datum=WGS84 +units=m +type=crs",
            "depth_positive": "down", "depth_units": "m", "vertical_datum": "TEST_DATUM",
            "sampling": {"method": method}, "data_base64": base64.b64encode(text.encode()).decode(), **metadata}


def run(sources=None, *, center=(0, 0), radius=100, **config):
    return sample_slope_neighborhood({"terrain_sources": [raster()] if sources is None else sources},
                                     {"longitude": center[0], "latitude": center[1]}, radius, config)


@pytest.mark.parametrize("gx,gy", [(0, 0), (.2, -.07), (-.15, .3), (.8, .6)])
def test_affine_plane_reproduction_all_real_supports_and_triangle_gradients(gx, gy):
    result = run([raster(lambda x, y: 1000+gx*x+gy*y)])
    expected = math.degrees(math.atan(math.hypot(gx, gy)))
    for sample in result["samples"]:
        assert sample["depth_m"] == pytest.approx(1000+gx*sample["x_m"]+gy*sample["y_m"], abs=1e-9)
        assert sample["attempts"] == [{"source_id": "plane", "status": "valid", "method": "linear"}]
    for triangle in result["triangles"]:
        assert triangle["valid"] and len(triangle["support_indices"]) == 7
        assert triangle["gradient_height"] == pytest.approx([-gx, -gy], abs=2e-11)
        assert triangle["slope_deg"] == pytest.approx(expected, abs=2e-9)
    assert result["quality"]["triangle_complete"] and result["quality"]["sample_complete"]
    assert result["quality"]["max_sampled_slope_deg"] == pytest.approx(expected, abs=2e-9)


def test_mesh_geometry_has_bounded_edges_actual_disk_area_and_real_queried_locations():
    result = run(radius=113, spacing_m=41)
    for triangle in result["triangles"]:
        vertices = [result["samples"][i] for i in triangle["vertex_indices"]]
        for a, b in zip(vertices, vertices[1:]+vertices[:1]):
            assert math.hypot(a["x_m"]-b["x_m"], a["y_m"]-b["y_m"]) <= 41+1e-12
        for i in triangle["support_indices"]:
            sample = result["samples"][i]
            assert sample["query_index"] >= 0 and sample["attempts"]
        centroid = triangle["centroid"]
        assert centroid["x_m"] == pytest.approx(sum(v["x_m"] for v in vertices)/3)
        assert centroid["y_m"] == pytest.approx(sum(v["y_m"] for v in vertices)/3)
    total_area = sum(t["area_m2"] for t in result["triangles"])
    quality = result["quality"]
    assert quality["triangulated_area_m2"] == pytest.approx(total_area)
    assert quality["disk_area_m2"] == pytest.approx(math.pi*113**2)
    assert quality["missing_area_m2"] == pytest.approx(math.pi*113**2-total_area)
    assert 0 < quality["coverage_fraction"] < 1
    assert quality["valid_disk_area_fraction"] == pytest.approx(quality["coverage_fraction"])
    assert quality["continuous_bed_verified"] is False
    for sample in result["samples"]:
        azimuth, _, distance = G.inv(0, 0, sample["longitude"], sample["latitude"])
        assert distance == pytest.approx(math.hypot(sample["x_m"], sample["y_m"]), abs=1e-8)
        if distance > 0:
            assert distance*math.sin(math.radians(azimuth)) == pytest.approx(sample["x_m"], abs=1e-8)


def test_real_off_route_crosswise_high_slope_cannot_be_replaced_by_flat_longitudinal_profile():
    # The route is east-west at y=0; every longitudinal depth there is 1000.
    field = lambda x, y: 1000+2*max(0, y-25)
    result = run([raster(field, nodes=17)])
    witnesses = [t for t in result["triangles"] if t["valid"] and t["slope_deg"] > 60]
    assert witnesses and all(t["centroid"]["y_m"] > 20 for t in witnesses)
    affine_children = [t for t in result["triangles"] if
                       all(result["samples"][k]["y_m"] > 25 for k in t["vertex_indices"])]
    assert affine_children
    for t in affine_children:
        assert t["gradient_height"] == pytest.approx([0, -2], abs=1e-10)
        assert t["slope_deg"] == pytest.approx(math.degrees(math.atan(2)), abs=1e-8)
    # Secants across a kink can exceed the smooth one-sided derivative. The
    # metric is the actual sampled triangular slope, not an exact derivative.
    assert result["quality"]["max_sampled_slope_deg"] >= math.degrees(math.atan(2))-1e-8


def test_smooth_curved_field_refinement_reduces_error_against_analytic_centroid_gradient():
    field = lambda x, y: 1000+.1*x+.15*y+.002*x*y
    errors = []
    for spacing in (50, 25, 12.5):
        result = run([raster(field)], radius=50, spacing_m=spacing)
        squared = []
        for t in result["triangles"]:
            x, y = t["centroid"]["x_m"], t["centroid"]["y_m"]
            expected = (-.1-.002*y, -.15-.002*x)
            squared.append(sum((a-b)**2 for a, b in zip(t["gradient_height"], expected)))
        errors.append(math.sqrt(sum(squared)/len(squared)))
    assert errors[1] < errors[0]*.8 and errors[2] < errors[1]*.8


@pytest.mark.parametrize("field,expected", [
    (lambda x, y: 1000+.3*abs(x)+.4*abs(y), math.degrees(math.atan(.5))),
    (lambda x, y: 1000-.3*abs(x)-.4*abs(y), math.degrees(math.atan(.5)))])
def test_genuinely_two_dimensional_v_and_ridge_have_independent_quadrant_gradients(field, expected):
    result = run([raster(field, nodes=17)], spacing_m=25)
    found = 0
    for triangle in result["triangles"]:
        vertices = [result["samples"][k] for k in triangle["vertex_indices"]]
        # Avoid triangles crossing the piecewise-affine kink, where the local
        # secant need not equal the one-sided derivative.
        if all(v["x_m"] > 0 and v["y_m"] > 0 for v in vertices):
            assert triangle["slope_deg"] == pytest.approx(expected, abs=1e-8)
            found += 1
    assert found > 10
    assert result["quality"]["max_sampled_slope_deg"] >= expected-1e-8


def test_nodata_hole_keeps_fixed_connectivity_and_never_bridges_to_a_known_maximum():
    blank = 1.70141e38
    result = run([raster(lambda x, y: blank if abs(x) < 10 and abs(y) < 10 else 1000+.2*x,
                         nodes=41, method="nearest")])
    quality = result["quality"]
    assert not quality["sample_complete"] and not quality["triangle_complete"]
    assert quality["missing_triangle_count"] > 0 and quality["valid_triangle_count"] > 0
    assert quality["max_sampled_slope_deg"] is not None
    for triangle in result["triangles"]:
        missing = any(result["samples"][i]["depth_m"] is None for i in triangle["support_indices"])
        if missing:
            assert not triangle["valid"] and triangle["slope_deg"] is None and triangle["reason"] == "nodata"
    assert quality["valid_disk_area_fraction"] < quality["coverage_fraction"]


def test_real_edge_midpoint_missing_rejects_triangle_even_when_all_corners_known():
    ex, ey = (10+10/math.sqrt(2))/2, 10/math.sqrt(2)/2
    blank = 1.70141e38
    result = run([raster(lambda x, y: blank if abs(x-ex) < .2 and abs(y-ey) < .2 else 1000+.1*x,
                         nodes=101, xbounds=(-12, 12), ybounds=(-12, 12), method="nearest")], radius=10)
    first = result["triangles"][0]
    assert all(result["samples"][i]["depth_m"] is not None for i in first["family_vertex_indices"])
    assert any(result["samples"][i]["depth_m"] is None for i in first["support_indices"][3:6])
    assert first["reason"] == "nodata" and first["slope_deg"] is None


def test_actual_midpoint_ridge_is_used_for_slopes_not_just_nodata_or_source_checks():
    # First family corners (0,0), (10,0), (10/sqrt2,10/sqrt2) are flat.
    # A known raised edge midpoint is nevertheless a sampled bed hazard.
    ex, ey = (10+10/math.sqrt(2))/2, 10/math.sqrt(2)/2
    result = run([raster(lambda x, y: 1040 if abs(x-ex) < .2 and abs(y-ey) < .2 else 1000,
                         nodes=101, xbounds=(-12, 12), ybounds=(-12, 12), method="nearest")], radius=10)
    children = result["triangles"][:6]
    corners = [result["samples"][k] for k in children[0]["family_vertex_indices"]]
    assert [s["depth_m"] for s in corners] == [1000, 1000, 1000]
    assert result["quality"]["max_sampled_slope_deg"] > 80
    steep = [t for t in children if t["slope_deg"] > 80]
    assert steep
    for t in steep:
        assert t["centroid"]["depth_m"] is None
        assert t["centroid"]["location_kind"] == "geometric_child_centroid_not_queried"
        assert t["sampled_witness"]["depth_m"] == result["samples"][t["family_centroid_sample_index"]]["depth_m"]
        assert any(result["samples"][k]["depth_m"] == 1040 for k in t["vertex_indices"])


def test_priority_fallback_different_support_identity_never_forms_a_depth_jump_gradient():
    left = raster(lambda x, y: 1000+.2*x, xbounds=(-200, 0), id="left", priority=200)
    full = raster(lambda x, y: 900+.2*x, id="full", priority=100)
    result = run([left, full])
    quality = result["quality"]
    assert quality["sample_complete"] and not quality["triangle_complete"] and quality["source_boundary"]
    assert any(s["fallback"] and len(s["attempts"]) == 2 for s in result["samples"])
    for triangle in result["triangles"]:
        ids = {(result["samples"][k]["source_id"], result["samples"][k]["source_fingerprint"]) for k in triangle["support_indices"]}
        if len(ids) > 1:
            assert triangle["reason"] == "source_boundary" and triangle["slope_deg"] is None
        else:
            assert triangle["slope_deg"] == pytest.approx(math.degrees(math.atan(.2)), abs=1e-8)
    assert quality["max_sampled_slope_deg"] == pytest.approx(math.degrees(math.atan(.2)), abs=1e-8)


def test_datum_conflict_rejects_and_explicit_selection_does_not_convert_or_mix():
    sources = [raster(id="a", vertical_datum="A"), raster(lambda x, y: 100, id="b", vertical_datum="B", priority=200)]
    with pytest.raises(TerrainSourceError, match="DATUM_CONFLICT"):
        run(sources)
    result = run(sources, vertical_datum="A")
    assert result["metadata"]["vertical_datum"] == "A"
    assert all(s["source_id"] == "a" for s in result["samples"])
    assert result["quality"]["terrain_query"]["excluded_datum_source_ids"] == ["b"]


def test_explicit_positive_up_feet_source_has_same_metric_height_gradient_after_real_conversion():
    result = run([raster(lambda x, y: -(1000+.2*x-.07*y)/.3048, depth_positive="up", depth_units="ft")])
    assert result["quality"]["max_sampled_slope_deg"] == pytest.approx(math.degrees(math.atan(math.hypot(.2, -.07))), abs=1e-8)
    assert result["sources"][0]["source_depth_units"] == "ft"
    assert result["sources"][0]["source_depth_positive"] == "up"


@pytest.mark.parametrize("center", [(179.9998, 0), (-179.9998, 82), (37, 87), (-67, -85)])
def test_high_latitude_and_dateline_centres_preserve_real_surface_radius_and_field(center):
    result = run([raster(center=center)], center=center)
    transform = Transformer.from_crs(4326, result["metadata"]["local_crs"], always_xy=True)
    for sample in result["samples"]:
        x, y = transform.transform(sample["longitude"], sample["latitude"])
        assert x == pytest.approx(sample["x_m"], abs=3e-8)
        assert y == pytest.approx(sample["y_m"], abs=3e-8)
        assert sample["depth_m"] == pytest.approx(1000+.2*x-.07*y, abs=2e-9)
        assert -180 <= sample["longitude"] <= 180
        assert G.inv(*center, sample["longitude"], sample["latitude"])[2] <= 100+3e-8
    assert result["quality"]["max_sampled_slope_deg"] == pytest.approx(math.degrees(math.atan(math.hypot(.2, -.07))), abs=3e-8)
    if abs(center[0]) > 179:
        assert any(s["longitude"] > 0 for s in result["samples"]) and any(s["longitude"] < 0 for s in result["samples"])


@pytest.mark.parametrize("curve", ["geodesic", "rhumb"])
def test_kp_centres_use_real_curve_position_with_duplicate_waypoints_and_terminal(curve):
    a, b = (0, 0), G.fwd(0, 0, 90, 100)[:2]
    project = {"route": {"curve": curve, "points": [{"longitude": x, "latitude": y} for x, y in [a, a, b, b]]},
               "terrain_sources": [raster()]}
    length = G.inv(*a, *b)[2]
    for center in [{"longitude": 0, "latitude": 0, "kp_m": 0},
                   {"longitude": b[0], "latitude": b[1], "kp_m": length}]:
        result = sample_slope_neighborhood(project, center, 10)
        assert result["metadata"]["route_signature"]
        assert result["center"]["route_position_error_m"] <= 1e-4
    with pytest.raises(ValueError, match="do not match"):
        sample_slope_neighborhood(project, {"longitude": 0, "latitude": 0, "kp_m": length}, 10)


def test_high_latitude_geodesic_midpoint_is_not_linearly_interpolated_longitude_latitude():
    start, end = (0, 70), (90, 70)
    az, _, length = G.inv(*start, *end)
    lon, lat, _ = G.fwd(*start, az, length/2)
    project = {"route": {"curve": "geodesic", "points": [{"longitude": x, "latitude": y} for x, y in [start, end]]}}
    result = sample_slope_neighborhood(project, {"longitude": lon, "latitude": lat, "kp_m": length/2}, 10)
    assert result["center"]["latitude"] > 75 and not result["quality"]["sample_complete"]
    with pytest.raises(ValueError, match="do not match"):
        sample_slope_neighborhood(project, {"longitude": 45, "latitude": 70, "kp_m": length/2}, 10)


def test_all_coincident_route_valid_kp0_needs_no_transverse_heading():
    project = {"route": {"points": [{"longitude": 0, "latitude": 0}]*2}, "terrain_sources": [raster()]}
    result = sample_slope_neighborhood(project, {"longitude": 0, "latitude": 0, "kp_m": 0}, 10)
    assert result["quality"]["sample_complete"]


def test_batch_reuses_coordinates_normalizes_once_and_charges_one_global_budget(monkeypatch):
    import oceanroute.terrain_slope_neighborhoods as module
    actual, calls = module.normalize_sources, []
    monkeypatch.setattr(module, "normalize_sources", lambda raw: (calls.append(True), actual(raw))[1])
    project = {"terrain_sources": [raster()]}
    sampler = SlopeNeighborhoodSampler(project)
    window = {"center": {"longitude": 0, "latitude": 0}, "radius_m": 100}
    estimate = sampler.estimate_many([window, window])
    result = sampler.sample_many([window, window])
    assert len(calls) == 1 and len(result["neighborhoods"]) == 2
    budget = result["budget"]
    assert budget["query_count"] == 642 and budget["unique_query_count"] == 321 and budget["cache_hits"] == 321
    assert budget["work_units"] <= estimate["estimated_work_units"]
    assert budget["output_bytes"] == len(json.dumps(result, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
    assert result["neighborhoods"][0] == result["neighborhoods"][1]
    with pytest.raises(ValueError, match="whole.*max_query_points"):
        SlopeNeighborhoodSampler(project, {"max_query_points": 500}).sample_many([window, window])


def test_cache_warmth_does_not_change_work_admission_or_estimate():
    clear_cache()
    first = run()
    second = run()
    assert first["budget"]["work_units"] == second["budget"]["work_units"]
    assert first["budget"]["source_preparation_cache_hits"] == 0
    assert second["budget"]["source_preparation_cache_hits"] == 1


@pytest.mark.parametrize("config,match", [({"spacing_m": .001}, "max_query_points"),
                                         ({"max_query_points": 1}, "max_query_points"),
                                         ({"max_work_units": 500}, "max_work_units"),
                                         ({"max_output_bytes": 1024}, "max_output_bytes")])
def test_budget_rejects_before_mesh_or_source_interpolation(monkeypatch, config, match):
    import oceanroute.terrain_slope_neighborhoods as module
    monkeypatch.setattr(module, "_mesh", lambda *args: pytest.fail("mesh must not be allocated"))
    monkeypatch.setattr(module, "_query", lambda *args: pytest.fail("terrain must not be prepared"))
    with pytest.raises(ValueError, match=match):
        run(**config)


@pytest.mark.parametrize("radius", [0, None, True, float("nan"), float("inf"), 10**1000])
def test_invalid_or_zero_radius_never_becomes_an_arbitrary_nonzero_window(radius):
    with pytest.raises(ValueError):
        run(radius=radius)


@pytest.mark.parametrize("config", [{"vertical_datum": None}, {"spacing_m": None}, {"spacing_m": float("nan")},
                                    {"max_query_points": 50_001}, {"max_work_units": True}, {"unsupported": 1}])
def test_strict_configuration_rejection(config):
    with pytest.raises(ValueError):
        run(**config)


@pytest.mark.parametrize("center", [{"longitude": 0, "latitude": 90}, {"longitude": 0, "latitude": 0, "kp_m": None},
                                    {"longitude": 0, "latitude": 0, "kp_m": 0}, {"longitude": 0, "latitude": 0, "z": 0}])
def test_invalid_or_unbound_centres_reject(center):
    with pytest.raises(ValueError):
        sample_slope_neighborhood({}, center, 10)


def test_batch_empty_or_over_200_windows_explicitly_rejects_without_truncation():
    sampler = SlopeNeighborhoodSampler({})
    for windows in [[], [{"center": {"longitude": 0, "latitude": 0}, "radius_m": 1}]*201]:
        with pytest.raises(ValueError, match="1..200"):
            sampler.sample_many(windows)


def test_missing_source_remains_unknown_finite_json_and_original_project_never_changes():
    project = {"route": {"points": [{"longitude": 0, "latitude": 0}, {"longitude": 0, "latitude": .001}]},
               "profile": [{"kp_m": 0, "depth_m": 12}], "material": {"inventory": 123}, "terrain_sources": []}
    original = deepcopy(project)
    result = sample_slope_neighborhood(project, {"longitude": 0, "latitude": 0}, 100)
    assert project == original
    assert not result["quality"]["sample_complete"] and not result["quality"]["triangle_complete"]
    assert result["quality"]["max_sampled_slope_deg"] is None
    assert all(t["slope_deg"] is None and t["reason"] == "nodata" for t in result["triangles"])
    json.dumps(result, allow_nan=False)


def test_normalized_signatures_preserve_source_changes_and_no_input_mutation():
    original = {"terrain_sources": [raster()]}
    before = deepcopy(original)
    one = sample_slope_neighborhood(original, {"longitude": 0, "latitude": 0}, 10)
    assert original == before
    changed = deepcopy(original)
    changed["terrain_sources"][0]["priority"] = 99
    two = sample_slope_neighborhood(changed, {"longitude": 0, "latitude": 0}, 10)
    assert one["metadata"]["terrain_library_signature"] != two["metadata"]["terrain_library_signature"]
    assert one["sources"][0]["fingerprint"] == two["sources"][0]["fingerprint"]
