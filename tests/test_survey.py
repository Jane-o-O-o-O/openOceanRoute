from copy import deepcopy
import json
import math

import pytest

from oceanroute.core import analyze_project, route_signature
from oceanroute.geodesy import GEOD, WGS84_A, interpolate, inverse
from oceanroute.survey import SurveyError, parse_observations, reconcile_survey


def project(points=None, *, length=1000, curve="rhumb", depths=(20, 20), slack=1):
    if points is None:
        points = [(0, 0), (math.degrees(length / WGS84_A), 0)]
    return {"crs": "EPSG:4326", "route": {"curve": curve, "mode": "flexible", "slack_basis": "surface", "slack_pct": slack,
        "points": [{"id": f"p{i}", "longitude": x, "latitude": y, "depth_m": depths[min(i, len(depths)-1)]} for i, (x, y) in enumerate(points)],
        "legs": [{"cable_type_id": "A"} for _ in points[1:]]},
        "cable_types": [{"id": "A", "cost_per_m": 1, "lay_speed_m_s": 1}], "bodies": [], "costs": {}}


def observation_at(p, fraction, cross=0, *, depth=20, cable=None, leg=0):
    a, b = p["route"]["points"][leg:leg+2]
    curve = p["route"]["curve"]
    length, azimuth = inverse(a["longitude"], a["latitude"], b["longitude"], b["latitude"], curve)
    xy = interpolate(a["longitude"], a["latitude"], b["longitude"], b["latitude"], fraction, curve)
    if curve == "geodesic":
        _, _, azimuth = GEOD.fwd(a["longitude"], a["latitude"], azimuth, length*fraction, return_back_azimuth=False)
    if cross:
        lon, lat, _ = GEOD.fwd(*xy, azimuth+(90 if cross > 0 else -90), abs(cross))
    else:
        lon, lat = xy
    return {"longitude": lon, "latitude": lat, "depth_m": depth, "cable_kp_m": cable}


def codes(result):
    return {w["code"] for w in result["warnings"]}


@pytest.mark.parametrize("curve", ["rhumb", "geodesic"])
def test_actual_curved_route_projection_uses_surface_station_and_local_normal(curve):
    p = project([(150, 65), (-170, 72)], curve=curve)
    total = analyze_project(p)["summary"]["surface_length_m"]
    observations = [observation_at(p, .24, 75), observation_at(p, .74, -30)]
    result = reconcile_survey(p, {"observations": observations, "max_gap_m": 1_000_000, "route_sample_step_m": 10000})
    assert [r["route_kp_m"] for r in result["observations"]] == pytest.approx([.24*total, .74*total], abs=.05)
    assert [r["cross_track_m"] for r in result["observations"]] == pytest.approx([75, -30], abs=.002)
    assert result["summary"]["deviation_exceeded_count"] == 1
    assert all(not r["ambiguity"] for r in result["observations"])
    assert result["summary"]["mean_signed_cross_track_m"] == pytest.approx(22.5, abs=.002)
    assert result["summary"]["rms_cross_track_m"] == pytest.approx(math.sqrt((75**2+30**2)/2), abs=.002)


def test_reverse_lay_direction_changes_signed_cross_track():
    p = project()
    obs = observation_at(p, .5, 50)
    positive = reconcile_survey(p, {"observations": [obs]})["observations"][0]
    p["route"]["points"].reverse()
    negative = reconcile_survey(p, {"observations": [obs]})["observations"][0]
    assert positive["cross_track_m"] == pytest.approx(50, abs=.001)
    assert negative["cross_track_m"] == pytest.approx(-50, abs=.001)
    assert positive["nearest_distance_m"] == pytest.approx(negative["nearest_distance_m"], abs=.001)


def test_three_four_five_depth_and_manufacturing_slack_with_explicit_datum_and_origin():
    p = project(length=300, depths=(0, 400), slack=0)
    p["route"]["mode"] = "fixed"
    p["route"]["legs"][0]["fixed_cable_length_m"] = 505
    observations = [observation_at(p, 0, depth=0, cable=1000), observation_at(p, 1, depth=400, cable=1505)]
    result = reconcile_survey(p, {"observations": observations, "depth_datums_aligned": True, "cable_kp_offset_m": -1000})
    segment = result["segments"][0]
    assert segment["measured_surface_length_m"] == pytest.approx(300, abs=1e-6)
    assert segment["measured_bottom_length_m"] == pytest.approx(500, abs=1e-6)
    assert segment["measured_cable_length_m"] == 505
    assert segment["measured_surface_slack_pct"] == pytest.approx(68.3333333333333)
    assert segment["measured_bottom_slack_pct"] == pytest.approx(1)
    assert segment["bottom_length_difference_m"] == pytest.approx(0, abs=1e-6)
    assert segment["cable_length_difference_m"] == pytest.approx(0, abs=1e-6)
    assert [r["depth_difference_m"] for r in result["observations"]] == pytest.approx([0, 0])
    assert [r["cable_kp_difference_m"] for r in result["observations"]] == pytest.approx([0, 0])
    assert result["summary"]["complete_observed_depth"]


def test_missing_depth_and_missing_physical_station_do_not_invent_values():
    p = project()
    observations = [observation_at(p, 0, cable=0), observation_at(p, .5, depth=None), observation_at(p, 1, cable=1010)]
    result = reconcile_survey(p, {"observations": observations})
    assert all(s["measured_bottom_length_m"] is None for s in result["segments"])
    assert all(s["measured_cable_length_m"] is None for s in result["segments"])
    assert result["summary"]["measured_surface_length_m"] == pytest.approx(1000)
    assert result["summary"]["measured_bottom_length_m"] is None
    assert result["summary"]["measured_cable_length_m"] is None
    assert result["observations"][-1]["observed_bottom_kp_m"] is None
    assert all(r["depth_difference_m"] is None for r in result["observations"])
    assert all(r["cable_kp_difference_m"] is None for r in result["observations"])
    assert "SURVEY_DEPTH_DATUM_UNALIGNED" in codes(result)


@pytest.mark.parametrize("explicit", [False, True])
def test_gap_breaks_global_cumulative_and_geometries_but_keeps_local_chain(explicit):
    p = project()
    obs = [observation_at(p, f, cable=1010*f) for f in (0, .2, .8, 1)]
    if explicit:
        obs[2]["break_before"] = True
    result = reconcile_survey(p, {"observations": obs, "max_gap_m": 1000 if explicit else 300})
    assert [s["connected"] for s in result["segments"]] == [True, False, True]
    assert result["summary"]["continuous_chain_count"] == 2
    assert result["summary"]["measured_surface_length_m"] is None
    assert result["summary"]["known_measured_surface_length_m"] == pytest.approx(400)
    assert result["summary"]["known_measured_bottom_length_m"] == pytest.approx(400)
    assert result["summary"]["known_measured_cable_length_m"] == pytest.approx(404)
    assert [r["chain_surface_kp_m"] for r in result["observations"]] == pytest.approx([0, 200, 0, 200])
    assert result["observations"][-1]["observed_surface_kp_m"] is None
    assert len(result["geojson_layers"]["measured_line"]["features"]) == 2


def test_high_latitude_dateline_output_is_split_and_station_is_short_arc():
    p = project([(179.8, 80), (-179.8, 80)], curve="geodesic")
    total = analyze_project(p)["summary"]["surface_length_m"]
    result = reconcile_survey(p, {"observations": [observation_at(p, .1, 10), observation_at(p, .9, 10)]})
    assert [r["route_kp_m"] for r in result["observations"]] == pytest.approx([.1*total, .9*total], abs=.02)
    assert result["segments"][0]["measured_surface_length_m"] < 10000
    geometry = result["geojson_layers"]["measured_line"]["features"][0]["geometry"]
    assert geometry["type"] == "MultiLineString"
    for part in geometry["coordinates"]:
        assert all(abs(b[0]-a[0]) <= 180 for a,b in zip(part, part[1:]))


def test_return_loop_is_ambiguous_and_excluded_from_automatic_pairing():
    p = project([(0, 0), (.01, 0), (0, 0)])
    result = reconcile_survey(p, {"observations": [observation_at(p, .3, 20), observation_at(p, .6, 20)]})
    assert all(r["ambiguity"] for r in result["observations"])
    assert result["summary"]["ambiguous_count"] == 2
    assert result["summary"]["deviation_stat_count"] == 0
    assert result["summary"]["comparable_segment_count"] == 0
    assert result["segments"][0]["planned_surface_length_m"] is None
    assert result["segments"][0]["measured_surface_length_m"] > 0
    assert "SURVEY_AMBIGUOUS_KP" in codes(result)


def test_observed_backtracking_is_not_repaired_into_planned_interval():
    p = project()
    result = reconcile_survey(p, {"observations": [observation_at(p, .8), observation_at(p, .2)]})
    assert not result["segments"][0]["comparable"]
    assert result["segments"][0]["measured_surface_length_m"] == pytest.approx(600)
    assert "SURVEY_KP_NONMONOTONIC" in codes(result)


def test_unmatched_points_keep_nearest_display_but_not_planned_depth_or_pair():
    p = project()
    result = reconcile_survey(p, {"observations": [observation_at(p, .2, 200), observation_at(p, .8, 200)], "max_match_distance_m": 100})
    assert result["summary"]["matched_count"] == 0
    assert all(r["route_kp_m"] is not None and r["planned_depth_m"] is None for r in result["observations"])
    assert result["summary"]["comparable_segment_count"] == 0
    assert not result["geojson_layers"]["residual_vectors"]["features"]
    assert "SURVEY_OUTSIDE_MATCH_RADIUS" in codes(result)


def test_endpoint_has_along_residual_and_turn_has_explicit_tangent_warning():
    p = project()
    lon, lat, _ = GEOD.fwd(0, 0, 270, 50)
    result = reconcile_survey(p, {"observations": [{"longitude": lon, "latitude": lat}]})
    row = result["observations"][0]
    assert row["endpoint_projection"]
    assert row["route_kp_m"] == 0
    assert row["along_track_residual_m"] == pytest.approx(-50)
    assert row["cross_track_m"] == pytest.approx(0, abs=.001)
    p = project([(0, 0), (.01, 0), (.01, .01)])
    # Outside an east-to-north elbow; nearest point is its vertex.
    lon, lat, _ = GEOD.fwd(.01, 0, 135, 50)
    result = reconcile_survey(p, {"observations": [{"longitude": lon, "latitude": lat}]})
    assert result["observations"][0]["tangent_ambiguous"]
    assert result["summary"]["deviation_stat_count"] == 0
    assert "SURVEY_TURN_TANGENT" in codes(result)


def test_manufacturing_allowance_jump_and_body_replacement_semantics():
    p = project(slack=0)
    p["route"]["allowances"] = [{"kp_m": 500, "length_m": 20}]
    p["route"]["legs"][0]["allowance_m"] = 5
    p["bodies"] = [{"id": "extra", "kp_m": 600, "length_m": 10, "length_mode": "additional"},
                   {"id": "replace", "kp_m": 700, "length_m": 10, "length_mode": "replace"}]
    result = reconcile_survey(p, {"observations": [observation_at(p, f, cable=c) for f,c in [(0,0),(.5,520),(.6,630),(1,1035)]],
                                    "cable_kp_offset_m": 0})
    assert [r["planned_cable_kp_m"] for r in result["observations"]] == pytest.approx([0,520,630,1035], abs=.01)
    assert result["segments"][-1]["planned_cable_length_m"] == pytest.approx(405, abs=.01)
    assert result["summary"]["measured_cable_length_m"] == 1035


def test_stale_planned_depth_is_not_used_for_bottom_comparison():
    p = project()
    p["profile"] = {"route_signature": route_signature(p), "samples": [{"kp_m": 0, "depth_m": 20}, {"kp_m": 1000, "depth_m": 25}]}
    p["route"]["points"][-1]["longitude"] *= 1.1
    result = reconcile_survey(p, {"observations": [observation_at(p, 0), observation_at(p, 1)], "depth_datums_aligned": True})
    assert all(r["planned_depth_m"] is None for r in result["observations"])
    assert result["segments"][0]["measured_bottom_length_m"] > 0
    assert result["segments"][0]["planned_bottom_length_m"] is None
    assert "PROFILE_STALE" in codes(result)
    assert not result["comparison"]["planned_profile_metadata"]["imported_profile_valid"]


def test_accepted_core_allowance_defaults_are_not_key_errors_in_survey():
    p = project(slack=0)
    p["route"]["allowances"] = [{"length_m": 20}, {"kp_m": 500}]
    result = reconcile_survey(p, {"observations": [observation_at(p,0), observation_at(p,1)]})
    assert [r["planned_cable_kp_m"] for r in result["observations"]] == pytest.approx([20,1020])
    assert result["segments"][0]["planned_cable_length_m"] == pytest.approx(1000)


def test_vessel_positions_do_not_become_seabed_observations():
    p = project()
    result = reconcile_survey(p, {"observations": [observation_at(p, 0, cable=0), observation_at(p, 1, cable=1010)], "observation_kind": "vessel_track"})
    assert result["segments"][0]["measured_surface_length_m"] == pytest.approx(1000)
    assert result["segments"][0]["measured_bottom_length_m"] is None
    assert result["segments"][0]["measured_cable_length_m"] is None
    assert "SURVEY_VESSEL_TRACK" in codes(result)


@pytest.mark.parametrize("stations", [[0,0], [10,5]])
def test_repeated_or_reverse_physical_stations_rejected(stations):
    p = project()
    with pytest.raises(SurveyError, match="SURVEY_CABLE_STATION_ORDER"):
        reconcile_survey(p, {"observations": [observation_at(p, f, cable=c) for f,c in zip((0,1),stations)]})


def test_work_route_and_output_limits_fail_explicitly():
    p = project()
    with pytest.raises(SurveyError, match="SURVEY_WORK_LIMIT"):
        reconcile_survey(p, {"observations": [observation_at(p,.5)], "max_work_evaluations": 1})
    with pytest.raises(SurveyError, match="SURVEY_OUTPUT_LIMIT"):
        reconcile_survey(p, {"observations": [observation_at(p,0),observation_at(p,1)], "geometry_step_m": 10, "max_output_vertices": 10})
    p = project([(-10,0),(10,0)])
    with pytest.raises(SurveyError, match="SURVEY_ROUTE_GRID_LIMIT"):
        reconcile_survey(p, {"observations": [observation_at(p,.5)], "route_sample_step_m": 25})


def test_csv_tsv_parse_and_truthful_geojson_contract_and_input_immutability():
    assert parse_observations("\ufefflongitude,latitude,depth_m,cable_kp_m,id,break_before\n0,0,20,0,start,0\n.001,0,,,end,true\n")[1]["depth_m"] is None
    assert parse_observations("longitude\tlatitude\n118\t22\n")[0]["longitude"] == 118
    with pytest.raises(ValueError, match="表头"):
        parse_observations("")
    p = project()
    before = deepcopy(p)
    config = {"observations": [observation_at(p,0), observation_at(p,1)]}
    config_before = deepcopy(config)
    result = reconcile_survey(p, config)
    assert p == before and config == config_before
    assert result["geojson"]["type"] == "FeatureCollection"
    assert {f["properties"]["feature_role"] for f in result["geojson"]["features"]} == {"observed_point", "measured_line", "residual_vector"}
    assert len(result["geojson"]["features"]) == 5
    assert result["observations"][0]["observed_depth_m"] == 20
    assert result["observations"][0]["route_kp_m"] == result["observations"][0]["planned_kp_m"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("changes", [{"longitude": float("nan")}, {"latitude": 91}, {"depth_m": -1}, {"break_before": "true"}])
def test_nonfinite_or_invalid_observations_rejected(changes):
    p = project()
    obs = observation_at(p,.5)
    obs.update(changes)
    with pytest.raises(ValueError):
        reconcile_survey(p, {"observations": [obs]})
