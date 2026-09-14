"""Analytical depth fields and independent geodesic checks, not slope self-oracles."""
import base64
from copy import deepcopy
import json
import math

import numpy as np
from pyproj import Geod, Transformer
import pytest

from oceanroute.core import analyze_project, route_signature
from oceanroute.side_slopes import side_slopes_from_sources
from oceanroute.terrain_sources import profile_from_sources, TerrainSourceError

G = Geod(ellps="WGS84")
LOCAL = "+proj=aeqd +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +type=crs"


def source(field=lambda x, y: 1000+.2*x, *, nodes=9, extent=1000, method="linear", **kwargs):
    xy = np.linspace(-extent, extent, nodes)
    values = [[field(x, y) for x in xy] for y in xy]
    text = (f"DSAA\n{nodes} {nodes}\n{-extent} {extent}\n{-extent} {extent}\n0 100000\n"+
            "\n".join(" ".join(str(v) for v in row) for row in values)+"\n")
    return {"id": "plane", "name": "explicit analytical field", "kind": "surfer", "source_crs": LOCAL,
            "depth_positive": "down", "depth_units": "m", "vertical_datum": "TEST_DATUM",
            "data_base64": base64.b64encode(text.encode()).decode(), "sampling": {"method": method}, **kwargs}


def project(heading=0, length=600, *, sources=None):
    a = G.fwd(0, 0, heading+180, length/2)[:2]
    b = G.fwd(0, 0, heading, length/2)[:2]
    return {"route": {"curve": "geodesic", "points": [
        {"id": "a", "label": "start", "longitude": a[0], "latitude": a[1]},
        {"id": "b", "label": "end", "longitude": b[0], "latitude": b[1]}]},
        "terrain_sources": [source()] if sources is None else sources}


def middle(p, **config):
    a, b = p["route"]["points"]
    _, _, length = G.inv(a["longitude"], a["latitude"], b["longitude"], b["latitude"])
    return side_slopes_from_sources(p, {"start_kp_m": length/2, "end_kp_m": length/2, **config})


@pytest.mark.parametrize("heading", [0, 37, 90, 173, 270])
def test_actual_affine_field_depths_and_independent_signed_secants(heading):
    gx, gy = .2, -.07
    p = project(heading, sources=[source(lambda x, y: 1000+gx*x+gy*y)])
    result = middle(p)
    row = result["side_slopes"]["samples"][0]
    # At the centre of this AEQD, geodesic radial coordinates are exact metres.
    expected_grade = -gx*math.cos(math.radians(heading))+gy*math.sin(math.radians(heading))
    expected = math.degrees(math.atan(expected_grade))
    for name in ["side_slope_deg", "port_slope_deg", "starboard_slope_deg"]:
        assert row[name] == pytest.approx(expected, abs=1e-10)
    assert row["max_sampled_abs_slope_deg"] == pytest.approx(abs(expected), abs=1e-10)
    for probe in row["transect"]:
        distance = abs(probe["offset_m"])
        azimuth = heading+(90 if probe["offset_m"] >= 0 else -90)
        ex = distance*math.sin(math.radians(azimuth)); ey = distance*math.cos(math.radians(azimuth))
        assert probe["depth_m"] == pytest.approx(1000+gx*ex+gy*ey, abs=1e-9)
    assert row["complete"] and not row["source_boundary"]


def test_real_transverse_slope_exists_when_current_longitudinal_rule_reports_flat():
    p = project()
    longitudinal = profile_from_sources(p, {"spacing_m": 100})
    analysis = analyze_project(longitudinal["project"])
    assert analysis["legs"][0]["slope_deg"] == 0
    assert analysis["legs"][0]["max_slope_deg"] == 0
    transverse = middle(p)["side_slopes"]["samples"][0]
    assert transverse["side_slope_deg"] == pytest.approx(-math.degrees(math.atan(.2)))
    assert [v["depth_m"] for v in transverse["transect"]] == pytest.approx([980, 990, 1000, 1010, 1020])


def test_curved_bilinear_field_matches_analytic_values_without_averaging_half_slopes():
    field = lambda x, y: 1000+.03*x+.04*y+.0002*x*y
    p = project(37, sources=[source(field)])
    result = side_slopes_from_sources(p, {"spacing_m": 100, "half_width_m": 100, "cross_spacing_m": 25})
    transform = Transformer.from_crs(4326, LOCAL, always_xy=True)
    for row in result["side_slopes"]["samples"]:
        expected = []
        for probe in row["transect"]:
            x, y = transform.transform(probe["longitude"], probe["latitude"])
            expected.append(field(x, y))
            assert probe["depth_m"] == pytest.approx(expected[-1], abs=2e-9)
        half = len(expected)//2
        assert row["port_slope_deg"] == pytest.approx(math.degrees(math.atan((expected[0]-expected[half])/100)), abs=1e-9)
        assert row["starboard_slope_deg"] == pytest.approx(math.degrees(math.atan((expected[half]-expected[-1])/100)), abs=1e-9)
        assert row["side_slope_deg"] == pytest.approx(math.degrees(math.atan((expected[0]-expected[-1])/200)), abs=1e-9)
    assert any(abs(r["port_slope_deg"]-r["starboard_slope_deg"]) > .1 for r in result["side_slopes"]["samples"])


def test_ridge_full_width_zero_does_not_hide_adjacent_or_half_slope():
    p = project(sources=[source(lambda x, y: 1000-.1*abs(x), nodes=3, extent=100)])
    row = middle(p)["side_slopes"]["samples"][0]
    angle = math.degrees(math.atan(.1))
    assert row["side_slope_deg"] == pytest.approx(0, abs=1e-9)
    assert row["port_slope_deg"] == pytest.approx(-angle)
    assert row["starboard_slope_deg"] == pytest.approx(angle)
    assert row["max_sampled_abs_slope_deg"] == pytest.approx(angle)


def test_mid_half_nodata_cannot_be_bridged_even_when_all_endpoints_are_known():
    p = project(sources=[source(lambda x, y: 1.70141e38 if x == -50 else 1000+.2*x,
                                nodes=5, extent=100, method="nearest")])
    result = middle(p)
    row = result["side_slopes"]["samples"][0]
    assert row["transect"][0]["depth_m"] == 980
    assert row["transect"][1]["depth_m"] is None
    assert row["transect"][2]["depth_m"] == 1000
    assert row["port_slope_deg"] is None and row["side_slope_deg"] is None
    assert row["starboard_slope_deg"] == pytest.approx(-math.degrees(math.atan(.2)))
    assert row["transect"][0]["slope_to_next_deg"] is None
    assert row["transect"][1]["slope_to_next_deg"] is None
    assert not row["complete"] and not result["quality"]["complete"]
    assert row["max_sampled_abs_slope_deg"] == pytest.approx(math.degrees(math.atan(.2)))
    json.dumps(result, allow_nan=False)


def test_source_fallback_boundary_is_preserved_as_uncertainty_not_blended():
    detail = source(lambda x, y: 1.70141e38 if x == 0 else 300, nodes=5, extent=100,
                    method="nearest", id="detail", priority=100)
    background = source(lambda x, y: 100, id="background", priority=0)
    result = middle(project(sources=[background, detail]))
    row = result["side_slopes"]["samples"][0]
    assert [v["depth_m"] for v in row["transect"]] == [300, 300, 100, 300, 300]
    assert row["complete"] and row["source_boundary"]
    center = row["transect"][2]
    assert center["fallback"] and center["fallback_count"] == 1
    assert [v["status"] for v in center["attempts"]] == ["nodata", "valid"]
    assert row["transect"][1]["source_boundary_to_next"]
    assert any(v["code"] == "SIDE_SLOPE_SOURCE_BOUNDARY" for v in result["warnings"])


def test_datum_selection_and_depth_unit_sign_use_existing_real_sampler():
    feet_up = source(lambda x, y: -(1000+.2*x)/.3048, depth_positive="up", depth_units="ft")
    assert middle(project(sources=[feet_up]))["side_slopes"]["samples"][0]["side_slope_deg"] == pytest.approx(-math.degrees(math.atan(.2)))
    other = source(id="other", vertical_datum="OTHER")
    with pytest.raises(TerrainSourceError, match="DATUM_CONFLICT"):
        middle(project(sources=[source(), other]))
    result = middle(project(sources=[source(), other]), vertical_datum="TEST_DATUM")
    assert result["quality"]["excluded_datum_source_ids"] == ["other"]
    assert result["side_slopes"]["metadata"]["vertical_datum"] == "TEST_DATUM"


def test_reverse_route_swaps_left_right_and_inverts_signed_elevation_slope():
    p = project(37, sources=[source(lambda x, y: 1000+.03*x+.04*y+.0002*x*y)])
    q = deepcopy(p); q["route"]["points"].reverse()
    a = middle(p)["side_slopes"]["samples"][0]
    b = middle(q)["side_slopes"]["samples"][0]
    assert b["side_slope_deg"] == pytest.approx(-a["side_slope_deg"], abs=1e-9)
    assert b["port_slope_deg"] == pytest.approx(-a["starboard_slope_deg"], abs=1e-9)
    assert b["starboard_slope_deg"] == pytest.approx(-a["port_slope_deg"], abs=1e-9)
    assert b["max_sampled_abs_slope_deg"] == pytest.approx(a["max_sampled_abs_slope_deg"], abs=1e-9)
    assert [v["depth_m"] for v in b["transect"]] == pytest.approx([v["depth_m"] for v in reversed(a["transect"])], abs=1e-8)


def test_high_latitude_geodesic_forward_tangent_varies_and_probes_are_orthogonal():
    p = {"route": {"curve": "geodesic", "points": [
        {"longitude": -60, "latitude": 70}, {"longitude": 60, "latitude": 70}]}, "terrain_sources": []}
    azi, _, distance = G.inv(-60, 70, 60, 70)
    result = side_slopes_from_sources(p, {"spacing_m": 100_000, "half_width_m": 100, "cross_spacing_m": 100})
    sections = result["side_slopes"]["samples"]
    assert len({round(v["heading_deg"], 4) for v in sections}) > 2
    for section in sections:
        lon, lat, forward = G.fwd(-60, 70, azi, section["kp_m"], return_back_azimuth=False)
        assert section["longitude"] == pytest.approx(lon, abs=1e-10)
        assert section["latitude"] == pytest.approx(lat, abs=1e-10)
        assert section["heading_deg"] == pytest.approx(forward % 360, abs=1e-9)
        for probe in [section["transect"][0], section["transect"][-1]]:
            az, _, length = G.inv(lon, lat, probe["longitude"], probe["latitude"])
            expected = section["heading_deg"]+(90 if probe["offset_m"] > 0 else -90)
            assert ((az-expected+180) % 360)-180 == pytest.approx(0, abs=1e-7)
            assert length == pytest.approx(100, abs=2e-8)


def test_ellipsoidal_rhumb_parallel_distance_and_constant_heading():
    p = {"route": {"curve": "rhumb", "points": [
        {"longitude": -2, "latitude": 70}, {"longitude": 2, "latitude": 70}]}, "terrain_sources": []}
    result = side_slopes_from_sources(p, {"spacing_m": 50_000})
    latitude = math.radians(70); e2 = (1/298.257223563)*(2-1/298.257223563)
    expected = 6378137/math.sqrt(1-e2*math.sin(latitude)**2)*math.cos(latitude)*math.radians(4)
    assert result["side_slopes"]["metadata"]["route_length_m"] == pytest.approx(expected, abs=1e-7)
    for row in result["side_slopes"]["samples"]:
        assert row["heading_deg"] == 90
        assert row["latitude"] == 70


def test_corners_ranges_and_duplicate_vertices_use_documented_one_sided_heading():
    p = {"route": {"curve": "geodesic", "points": [
        {"longitude": 0, "latitude": 0}, {"longitude": 0, "latitude": .001},
        {"longitude": 0, "latitude": .001}, {"longitude": .001, "latitude": .001}]}, "terrain_sources": []}
    _, _, corner = G.inv(0, 0, 0, .001)
    _, _, last = G.inv(0, .001, .001, .001)
    result = side_slopes_from_sources(p, {"spacing_m": 1000, "start_kp_m": 20, "end_kp_m": corner+last-20})
    rows = result["side_slopes"]["samples"]
    assert [r["kp_m"] for r in rows] == pytest.approx([20, corner, corner+last-20])
    assert rows[0]["heading_deg"] == pytest.approx(0)
    assert rows[1]["heading_deg"] == pytest.approx(90, abs=1e-7)
    assert rows[2]["heading_deg"] == pytest.approx(90, abs=1e-7)
    assert result["side_slopes"]["metadata"]["heading_policy"].startswith("outgoing_one_sided")


@pytest.mark.parametrize("curve", ["rhumb", "geodesic"])
def test_antimeridian_sections_stay_wrapped_without_huge_fake_route_distance(curve):
    p = {"route": {"curve": curve, "points": [
        {"longitude": 179.999, "latitude": 10}, {"longitude": -179.999, "latitude": 10}]}, "terrain_sources": []}
    result = side_slopes_from_sources(p, {"spacing_m": 50, "half_width_m": 200, "cross_spacing_m": 100})
    assert result["side_slopes"]["metadata"]["route_length_m"] < 300
    assert all(-180 <= v["longitude"] <= 180 for r in result["side_slopes"]["samples"] for v in r["transect"])
    assert not result["quality"]["complete"]
    assert result["quality"]["max_sampled_abs_slope_deg"] is None
    json.dumps(result, allow_nan=False)


def test_candidate_does_not_change_profile_route_cable_or_inventory_and_roundtrips():
    p = project()
    p.update(profile={"source": "independent-original", "samples": [{"kp_m": 0, "depth_m": 333}]},
             cable_types=[{"id": "c", "wet_weight_n_m": 4}], cable_assembly=[{"extra": 7}],
             arbitrary_extension={"stock": 123, "unknown": [1, 2, 3]})
    original = deepcopy(p)
    result = side_slopes_from_sources(p)
    assert p == original
    for key in ["profile", "route", "cable_types", "cable_assembly", "arbitrary_extension"]:
        assert result["project"][key] == p[key]
    assert result["side_slopes"]["route_signature"] == route_signature(p)
    assert result["side_slopes"]["model"] == result["side_slopes"]["metadata"]["model"] == "route-side-slopes-v1"
    assert json.loads(json.dumps(result, allow_nan=False))["project"]["side_slopes"] == result["side_slopes"]
    assert result["budget"]["work_units"] <= result["budget"]["max_work_units"]
    assert result["budget"]["output_bytes"] == len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())


def test_nullable_end_is_current_route_terminal_and_matches_omitted_end_candidate():
    p = project()
    p["profile"] = {"source": "retained-original", "samples": [{"kp_m": 0, "depth_m": 999}]}
    omitted = side_slopes_from_sources(p, {"spacing_m": 125, "start_kp_m": 37})
    nullable = side_slopes_from_sources(p, {"spacing_m": 125, "start_kp_m": 37, "end_kp_m": None})
    assert nullable["side_slopes"] == omitted["side_slopes"]
    assert nullable["project"] == omitted["project"]
    metadata = nullable["side_slopes"]["metadata"]
    assert metadata["end_kp_m"] == metadata["route_length_m"]
    assert nullable["side_slopes"]["samples"][-1]["kp_m"] == metadata["route_length_m"]
    assert nullable["budget"]["query_point_count"] == omitted["budget"]["query_point_count"]
    assert nullable["quality"] == omitted["quality"]


@pytest.mark.parametrize("config", [
    {"spacing_m": True}, {"spacing_m": 0}, {"spacing_m": float("nan")},
    {"half_width_m": 0}, {"half_width_m": 10**1000}, {"cross_spacing_m": float("inf")},
    {"cross_spacing_m": .0001}, {"start_kp_m": -1}, {"end_kp_m": 1e12},
    {"start_kp_m": 200, "end_kp_m": 100}, {"vertical_datum": ""},
    {"max_query_points": 50_001}, {"max_work_units": 0}, {"max_output_bytes": 65*1024**2},
    {"unrecognized": 1}, [],
])
def test_invalid_inputs_are_strict_rejections(config):
    with pytest.raises(ValueError):
        side_slopes_from_sources(project(), config)


@pytest.mark.parametrize("config", [{"max_query_points": 2}, {"max_work_units": 1}, {"max_output_bytes": 1024},
                                    {"half_width_m": 100000, "cross_spacing_m": .001}])
def test_insufficient_resources_reject_before_terrain_reader(monkeypatch, config):
    import oceanroute.side_slopes as module
    def prohibited(*args, **kwargs):
        pytest.fail("query must not be reached after preflight rejection")
    monkeypatch.setattr(module, "query_terrain", prohibited)
    with pytest.raises(ValueError):
        side_slopes_from_sources(project(), config)


def test_zero_route_and_true_polar_station_reject_undefined_transverse_frame():
    p = project()
    p["route"]["points"][1].update(longitude=p["route"]["points"][0]["longitude"], latitude=p["route"]["points"][0]["latitude"])
    with pytest.raises(ValueError, match="coincide"):
        side_slopes_from_sources(p)
    p = {"route": {"curve": "geodesic", "points": [{"longitude": 0, "latitude": 89}, {"longitude": 0, "latitude": 90}]}, "terrain_sources": []}
    with pytest.raises(ValueError, match="pole"):
        side_slopes_from_sources(p, {"spacing_m": 100000})
