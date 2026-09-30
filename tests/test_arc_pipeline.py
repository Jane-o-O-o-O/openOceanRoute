"""Independent continuous-circle oracles for the integrated route consumers.

Expected circle points/radial tangents come from independent PROJ construction,
not Radius AC, RouteSegment or a production interpolation helper. WGS84 engine
sharing is explicit. Tiny arcs use analytic curvature bounds for length; these
tests do not declare drawing polygons to be exact continuous curves.
"""
from copy import deepcopy
import json
import math

from pyproj import Geod, Transformer
import pytest

from oceanroute.automatic_rules import check_automatic_rules
from oceanroute.core import analyze_project, route_signature
from oceanroute.exchange import export_geojson, export_kml, reverse_project
from oceanroute.plan_voyage import prepare_plan_voyage
from oceanroute.routing import search_route
from oceanroute.shipplan import build_ship_plan
from oceanroute.survey import reconcile_survey
from oceanroute.workspace import migrate_project

G = Geod(ellps="WGS84")
B = 6356752.314245179


def circle_point(center, radius, alpha):
    return tuple(G.fwd(*center, alpha, radius)[:2])


def bearing_delta(a, b):
    return abs((a-b+180)%360-180)


def circle_project(*, center=(0.,0.), radius=500., start=-90., sweep=180., curve="rhumb", layers=()):
    a, b = circle_point(center,radius,start), circle_point(center,radius,start+sweep)
    geom = {"type":"circular_arc","schema_version":1,"center":list(center),"radius_m":radius,
            "start_azimuth_deg":start,"sweep_deg":sweep}
    return {"schema_version":1,"id":"independent-arc-pipeline","name":"Explicit synthetic true-circle workflow",
            "crs":"EPSG:4326","route":{"curve":curve,"slack_pct":2,"slack_basis":"surface","mode":"flexible",
                "points":[{"id":"a","longitude":a[0],"latitude":a[1],"depth_m":10},
                          {"id":"b","longitude":b[0],"latitude":b[1],"depth_m":10}],
                "legs":[{"geometry":geom,"cable_type_id":"A"}]},
            "cable_types":[{"id":"A","name":"Declared synthetic cable","cost_per_m":1,"lay_speed_m_s":.5,
                            "wet_weight_n_m":4,"mass_kg_m":1,"diameter_m":.02,"ea_n":1e6,"ei_n_m2":0}],
            "layers":list(layers),"bodies":[],"costs":{}}


def layer(geometry, ident="gis"):
    return {"id":ident,"name":"Independent synthetic GIS","kind":"restricted","visible":True,
            "geojson":{"type":"FeatureCollection","features":[{"type":"Feature","id":"target",
                        "properties":{},"geometry":geometry}]}}


def rule_result(project, kind="crossing", **extra):
    ws = migrate_project(project)["workspace"]
    rule = {"id":"oracle","path_id":ws["active_path_id"],"kind":kind,"end_kp_m":None,
            "selectors":[{"layer_id":"gis","feature_ids":None}], **extra}
    if kind == "crossing":
        rule.setdefault("conditions",[])
    else:
        rule.setdefault("around","path"); rule.setdefault("targets",["gis"]); rule.setdefault("distance_m",100)
    before = deepcopy(ws)
    result = check_automatic_rules(ws,{"rules":[rule]})
    assert ws == before
    json.dumps(result,allow_nan=False)
    return result["results"][0]


def test_core_integrated_length_and_continuous_tangents_are_not_endpoint_chord():
    p = circle_project()
    result = analyze_project(p)
    lo, hi = B*math.sin(500/B)*math.pi, 500*math.pi
    # Only the floating evaluation of the independent analytic bounds receives
    # four ULPs; this is sub-picometre here, not an engineering geometry tolerance.
    assert lo-4*math.ulp(lo) <= result["summary"]["surface_length_m"] <= hi+4*math.ulp(hi)
    assert result["summary"]["surface_length_m"] > 1.5*G.inv(
        p["route"]["points"][0]["longitude"],0,p["route"]["points"][1]["longitude"],0)[2]
    assert bearing_delta(result["legs"][0]["bearing_deg"],0) < 1e-7
    assert max(pt[1] for pt in result["route_geometry"]["coordinates"]) > .0045


def test_automatic_crossing_and_angle_at_true_north_middle_of_equator_endpoint_arc():
    low, high = circle_point((0,0),200,0), circle_point((0,0),800,0)
    p = circle_project(layers=[layer({"type":"LineString","coordinates":[list(low),list(high)]})])
    row = rule_result(p)
    assert row["status"] == "violations" and not row["diagnostics"]
    crossings = [v for v in row["violations"] if v["event"] in {"crossing","touch"}]
    assert crossings
    v = min(crossings,key=lambda v:abs(v["location"]["kp_m"]-500*math.pi/2))
    assert v["location"]["kp_m"] == pytest.approx(500*math.pi/2,abs=.05)
    assert v["values"]["angle_deg"] == pytest.approx(90,abs=.001)
    assert G.inv(0,0,v["location"]["longitude"],v["location"]["latitude"])[2] == pytest.approx(500,abs=.02)


def test_polygon_around_chord_center_does_not_contain_actual_semicircle():
    p = circle_project(layers=[layer({"type":"Polygon","coordinates":[[[-.001,-.001],[.001,-.001],
            [.001,.001],[-.001,.001],[-.001,-.001]]]})])
    row = rule_result(p)
    assert row["status"] == "clear", row
    assert not row["violations"] and not row["diagnostics"]
    assert analyze_project(p)["crossings"] == []


def test_arc_rule_work_exhaustion_is_whole_rejection_not_an_empty_clear_report():
    target = circle_point((0,0),540,0)
    p = circle_project(layers=[layer({"type":"Point","coordinates":list(target)})])
    ws = migrate_project(p)["workspace"]
    before = deepcopy(ws)
    rule = {"id":"small-budget","path_id":ws["active_path_id"],"kind":"proximity","end_kp_m":None,
            "selectors":[{"layer_id":"gis","feature_ids":None}],"around":"path","targets":["gis"],"distance_m":100}
    with pytest.raises(ValueError,match="AUTOMATIC_RULE_WORK_LIMIT|max_work_units"):
        check_automatic_rules(ws,{"rules":[rule],"max_work_units":60})
    assert ws == before


@pytest.mark.parametrize("center",[(0.,0.),(179.99,70.)])
def test_automatic_point_proximity_retains_actual_circle_minimum_and_metric_bounds(center):
    radius, extra = 500., 40.
    target = circle_point(center,radius+extra,0)
    p = circle_project(center=center,layers=[layer({"type":"Point","coordinates":list(target)})])
    row = rule_result(p,"proximity",distance_m=100)
    assert row["status"] == "violations" and not row["diagnostics"]
    event = min(row["violations"],key=lambda v:v["distance_m"])
    assert event["distance_m"] == pytest.approx(extra,abs=.25)
    assert event["distance_bounds_m"][0] <= extra+1e-7
    assert event["distance_bounds_m"][1] >= extra-1e-7
    assert G.inv(*center,event["location"]["longitude"],event["location"]["latitude"])[2] == pytest.approx(radius,abs=.02)


def test_legacy_crossings_use_true_arc_north_point_and_explicit_screening_model():
    low, high = circle_point((0,0),200,0), circle_point((0,0),800,0)
    p = circle_project(layers=[layer({"type":"LineString","coordinates":[list(low),list(high)]})])
    result = analyze_project(p)
    assert len(result["crossings"]) == 1
    crossing = result["crossings"][0]
    assert crossing["model"] == "sampled_intrinsic_arc_local_aeqd_screening"
    assert crossing["kp_m"] == pytest.approx(500*math.pi/2,abs=.02)
    assert crossing["angle_deg"] == pytest.approx(90,abs=.01)
    assert G.inv(0,0,crossing["longitude"],crossing["latitude"])[2] == pytest.approx(500,abs=.01)


@pytest.mark.parametrize("offset",[-25.,25.])
def test_survey_projection_uses_arc_station_and_true_radial_normal(offset):
    p = circle_project()
    # At the north middle the route heads east. Its starboard normal is south.
    obs = circle_point((0,0),500-offset,0)
    result = reconcile_survey(p,{"observations":[{"longitude":obs[0],"latitude":obs[1],"depth_m":10}],
                                  "route_sample_step_m":500,"station_tolerance_m":.001})
    row = result["observations"][0]
    assert row["route_kp_m"] == pytest.approx(500*math.pi/2,abs=.01)
    assert row["route_bearing_deg"] == pytest.approx(90,abs=.001)
    assert row["cross_track_m"] == pytest.approx(offset,abs=.002)
    assert row["nearest_distance_m"] == pytest.approx(abs(offset),abs=.002)
    assert not row["ambiguity"]


def test_survey_full_circle_boundary_has_two_distinct_inventory_station_matches():
    p = circle_project(radius=100,start=0,sweep=360)
    observed = circle_point((0,0),100,0)
    result = reconcile_survey(p,{"observations":[{"longitude":observed[0],"latitude":observed[1],"depth_m":10}],
                                  "route_sample_step_m":1000,"station_tolerance_m":.001,
                                  "ambiguity_kp_separation_m":100})
    row = result["observations"][0]
    assert row["route_kp_m"] == pytest.approx(0,abs=.002)
    assert row["ambiguity"], "KP0 and full-circumference KP are distinct manufacture stations at the same actual position"


def test_ship_plan_targets_and_offsets_follow_actual_arc_tangent():
    p = circle_project()
    result = build_ship_plan(p,{"sample_spacing_m":400,"bottom_tension_n":10})
    rows = result["target_route"]
    middle = min(rows,key=lambda r:abs(r["kp_m"]-500*math.pi/2))
    for row in result["vessel_waypoints"]:
        _, back, radius = G.inv(0,0,row["longitude"],row["latitude"])
        assert radius == pytest.approx(500,abs=1e-6)
        assert bearing_delta(row["route_heading_deg"],back+180+90) < 1e-7
    assert abs(middle["latitude"]) > .004
    assert result["summary"]["paid_out_m"] == pytest.approx(analyze_project(p)["summary"]["cable_length_m"],abs=1e-7)
    assert abs(result["summary"]["material_balance_residual_m"]) < 1e-7


def test_plan_voyage_preparation_projects_actual_target_on_arc_without_integrating_dynamics():
    p = circle_project()
    result = prepare_plan_voyage(p,{"plan":{"sample_spacing_m":100,"bottom_tension_n":10},
                                   "start_time_s":1500,"duration_s":2,
                                   "voyage":{"adaptive_mesh":{"enabled":False}}})
    mapping = result["mapping"]
    inverse = Transformer.from_crs(mapping["local_crs"],4326,always_xy=True)
    target = inverse.transform(*mapping["planned_start_touchdown_xy_m"])
    assert G.inv(0,0,*target)[2] == pytest.approx(500,abs=1e-5)
    assert target[1] > .004
    assert mapping["initial_natural_length_m"] > 0
    assert mapping["initial_manufacturing_top_m"] < mapping["final_manufacturing_top_m"]
    assert result["config"]["simulation"]["depth_m"] == 10
    json.dumps(result,allow_nan=False)


def test_routing_keeps_outside_arc_and_explicitly_replaces_selected_arc_with_new_straight_legs():
    p = circle_project()
    end = p["route"]["points"][-1]
    q = circle_point((end["longitude"],end["latitude"]),400,90)
    p["route"]["points"].append({"id":"c","longitude":q[0],"latitude":q[1],"depth_m":10})
    p["route"]["legs"].append({"cable_type_id":"A"})
    before = deepcopy(p)
    cfg = {"grid_spacing_m":100,"padding_m":500,"length_policy":"preserve"}
    outside = search_route(p,{**cfg,"start_point_index":1,"end_point_index":2})
    assert outside["project"]["route"]["legs"][0]["geometry"] == p["route"]["legs"][0]["geometry"]
    assert outside["project"]["route"]["points"][0] == p["route"]["points"][0]
    for key in ("id","longitude","latitude","depth_m"):
        assert outside["project"]["route"]["points"][1][key] == p["route"]["points"][1][key]
    selected = search_route(p,{**cfg,"start_point_index":0,"end_point_index":1})
    assert all(o.get("geometry") is None for o in selected["project"]["route"]["legs"])
    assert selected["report"]["transformations"]["geometry_changed"]
    assert selected["report"]["transformations"]["new_selected_length_m"] < 1100
    assert selected["report"]["transformations"]["old_selected_length_m"] > 1500
    assert analyze_project(selected["project"])["summary"]["cable_length_m"] == pytest.approx(analyze_project(p)["summary"]["cable_length_m"],abs=1e-6)
    assert p == before


@pytest.mark.parametrize("center",[(0,0),(179.99,70)])
def test_geojson_and_kml_render_actual_arc_and_preserve_authoritative_descriptor(center):
    p = circle_project(center=center)
    original = deepcopy(p)
    a = analyze_project(p)
    exported = json.loads(export_geojson(p,a))
    route = exported["features"][0]
    assert route["properties"]["leg_geometry"] == [p["route"]["legs"][0]["geometry"]]
    coords = route["geometry"]["coordinates"]
    pieces = coords if route["geometry"]["type"] == "MultiLineString" else [coords]
    assert sum(map(len,pieces)) > 3
    for piece in pieces:
        for point in piece:
            assert G.inv(*center,*point)[2] == pytest.approx(500,abs=1e-5)
    assert "sampled_visualization" in route["properties"]["geometry_representation"]
    kml = export_kml(p,a)
    assert len(kml.split("<coordinates>")) > 1
    assert p == original


def test_exchange_reverse_preserves_actual_circle_and_changes_route_bound_signature():
    p = circle_project()
    a = analyze_project(p)
    reversed_p = reverse_project(p,a)
    geom = reversed_p["route"]["legs"][0]["geometry"]
    assert geom["center"] == p["route"]["legs"][0]["geometry"]["center"]
    assert geom["radius_m"] == 500
    assert geom["sweep_deg"] == -180
    assert route_signature(p) != route_signature(reversed_p)
    assert analyze_project(reversed_p)["summary"]["surface_length_m"] == pytest.approx(a["summary"]["surface_length_m"],abs=1e-7)
