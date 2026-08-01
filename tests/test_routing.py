from copy import deepcopy
import json
import math

import numpy as np
from pyproj import CRS, Transformer
import pytest
from shapely.geometry import LineString, box
from shapely.ops import transform

from oceanroute.core import analyze_project, route_signature
from oceanroute.geodesy import densify, inverse
from oceanroute.routing import RoutingError, TerrainGrid, search_route


LOCAL = CRS.from_proj4("+proj=aeqd +lat_0=23 +lon_0=118 +datum=WGS84 +units=m")
TO_GEO = Transformer.from_crs(LOCAL, "EPSG:4326", always_xy=True)
TO_LOCAL = Transformer.from_crs("EPSG:4326", LOCAL, always_xy=True)


def geo_polygon(bounds):
    x0,y0,x1,y1 = bounds
    return {"type": "Polygon", "coordinates": [[list(TO_GEO.transform(x,y)) for x,y in ((x0,y0),(x1,y0),(x1,y1),(x0,y1),(x0,y0))]]}


def layer(identifier, kind, geometry):
    return {"id": identifier, "name": identifier, "kind": kind, "visible": True,
            "geojson": {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "feature", "properties": {}, "geometry": geometry}]}}


def project(xs=(-2000,2000), ys=None):
    ys = ys or [0]*len(xs)
    points = [{"id": f"p{i}", "longitude": lon, "latitude": lat, "depth_m": 50, "label": f"点 {i}"}
              for i,(lon,lat) in enumerate(TO_GEO.transform(x,y) for x,y in zip(xs,ys))]
    result = {"id": "original", "name": "避障测试", "crs": "EPSG:4326", "route": {
        "curve": "rhumb", "mode": "flexible", "slack_basis": "surface", "slack_pct": 1,
        "points": points, "legs": [{"cable_type_id": "A", "burial": i == 0} for i in range(len(points)-1)]},
        "cable_types": [{"id": "A", "cost_per_m": 2, "lay_speed_m_s": 1}, {"id": "B", "cost_per_m": 5, "lay_speed_m_s": 1}],
        "costs": {"currency": "CNY", "vessel_day_rate": 86400}, "bodies": [], "layers": []}
    keys = [0.0]
    for a,b in zip(points,points[1:]):
        keys.append(keys[-1]+inverse(a["longitude"],a["latitude"],b["longitude"],b["latitude"])[0])
    result["profile"] = {"samples": [{"kp_m": key, "depth_m": 50} for key in keys], "route_signature": route_signature(result), "source": "user", "measured": True}
    return result


def config(**extra):
    return {"grid_spacing_m": 200, "padding_m": 1500, **extra}


def projected_candidate(result):
    p = result["project"]
    coords=[]
    for a,b in zip(p["route"]["points"],p["route"]["points"][1:]):
        dense=densify(a["longitude"],a["latitude"],b["longitude"],b["latitude"],p["route"].get("curve","rhumb"),20)
        coords.extend(dense if not coords else dense[1:])
    return LineString([TO_LOCAL.transform(*point) for point in coords])


def terrain(z=None):
    x = [-3000,-2000,-1000,0,1000,2000,3000]
    y = [-2000,-1500,-1000,-500,0,500,1000,1500,2000]
    z = z if z is not None else [[50 for _ in x] for _ in y]
    return {"crs": LOCAL.to_string(), "x_m": x, "y_m": y, "depth_m": z, "measured": True, "source": "test-survey"}


def test_unconstrained_path_is_actual_short_geographic_path_and_input_unchanged():
    p = project()
    saved = deepcopy(p)
    result = search_route(p, config())
    assert p == saved
    assert result["project"]["id"] != p["id"]
    assert result["report"]["constraint_validation"]["selected_section_passed"]
    assert result["report"]["transformations"]["new_selected_length_m"] == pytest.approx(analyze_project(p)["summary"]["surface_length_m"], abs=.001)
    assert result["report"]["objective_m_equivalent"]["total"] == pytest.approx(4000, abs=.1)
    assert not result["report"]["terrain"]
    assert result["report"]["optimality"] == "no_continuous_global_optimality_claim"
    assert result["project"]["profile"] == p["profile"]
    assert not result["report"]["transformations"]["geometry_changed"]
    json.dumps(result,allow_nan=False)


def test_real_astar_detours_polygon_with_clearance_and_conserves_assembly():
    p = project()
    p["layers"] = [layer("blocked","restricted",geo_polygon((-500,-600,500,600)))]
    p["bodies"] = [{"id":"repeater","kp_m":1000,"length_m":10,"cost":100},
                   {"id":"additional","kp_m":3000,"length_m":5,"cost":100,"length_mode":"additional"}]
    p["route"]["allowances"] = [{"kp_m":2000,"length_m":30}]
    before = analyze_project(p)
    result = search_route(p,config(clearance_m=200))
    line = projected_candidate(result)
    assert not line.intersects(box(-500,-600,500,600))
    assert line.distance(box(-500,-600,500,600)) >= 199.9
    assert result["report"]["transformations"]["new_selected_length_m"] > before["summary"]["surface_length_m"]
    after = analyze_project(result["project"])
    for key in ("cable_length_m","material_length_m","material_cost","body_cost"):
        assert after["summary"][key] == pytest.approx(before["summary"][key],abs=1e-5),key
    assert {b["id"]:b["cable_kp_m"] for b in after["bodies"]} == pytest.approx({b["id"]:b["cable_kp_m"] for b in before["bodies"]},abs=1e-5)
    assert any(w["code"]=="CABLE_SHORTAGE" for w in after["warnings"])
    assert after["summary"]["bottom_length_m"] is None


def test_connected_edges_cannot_jump_through_wall_between_clear_grid_nodes():
    p=project()
    p["layers"]=[layer("wall","restricted",geo_polygon((-20,-5000,20,5000)))]
    with pytest.raises(RoutingError,match="ROUTING_NO_PATH"):
        search_route(p,config(grid_spacing_m=300))


def test_blocked_endpoint_and_search_budget_have_specific_errors():
    p=project()
    p["layers"]=[layer("start","restricted",geo_polygon((-2200,-500,-1800,500)))]
    with pytest.raises(RoutingError,match="ROUTING_ENDPOINT_BLOCKED"):
        search_route(p,config())
    p["layers"]=[layer("middle","restricted",geo_polygon((-500,-600,500,600)))]
    with pytest.raises(RoutingError,match="ROUTING_SEARCH_LIMIT"):
        search_route(p,config(max_expansions=1))
    with pytest.raises(RoutingError,match="ROUTING_GRID_LIMIT"):
        search_route(p,config(grid_spacing_m=1))


def test_allowed_region_constrains_whole_edges_and_disconnected_region_has_no_path():
    p=project()
    allowed=geo_polygon((-3000,-1000,3000,1000))
    p["layers"]=[layer("allowed","allowed",allowed),layer("block","restricted",geo_polygon((-300,-300,300,300)))]
    result=search_route(p,config(allowed_layer_ids=["allowed"],clearance_m=100))
    assert box(-3000+100,-1000+100,3000-100,1000-100).buffer(.1).covers(projected_candidate(result))
    cfg=config(allowed_areas=[geo_polygon((-3000,-500,-1000,500)),geo_polygon((1000,-500,3000,500))])
    with pytest.raises(RoutingError,match="ROUTING_NO_PATH"):
        search_route(project(),cfg)


def test_hidden_restricted_layer_still_enforces_geometry_and_selection_is_explicit():
    p=project()
    obstacle=layer("block","restricted",geo_polygon((-300,-300,300,300)))
    obstacle["visible"]=False
    p["layers"]=[obstacle]
    result=search_route(p,config())
    assert not projected_candidate(result).intersects(box(-300,-300,300,300))
    ignored=search_route(p,config(obstacle_layer_ids=[]))
    assert projected_candidate(ignored).intersects(box(-300,-300,300,300))


def test_existing_cable_avoidance_and_crossing_penalty_change_the_candidate():
    p=project()
    cable={"type":"LineString","coordinates":[list(TO_GEO.transform(0,-1000)),list(TO_GEO.transform(0,1000))]}
    p["layers"]=[layer("existing","cable",cable)]
    direct=search_route(p,config())
    assert direct["report"]["objective_m_equivalent"]["crossing_units"] == pytest.approx(1)
    costly=search_route(p,config(crossing_penalty_m=10000))
    assert costly["report"]["objective_m_equivalent"]["crossing_units"] == 0
    avoided=search_route(p,config(avoid_existing_cables=True,cable_clearance_m=100))
    assert projected_candidate(avoided).distance(LineString([(0,-1000),(0,1000)])) >= 99.9


def test_positive_weighted_zone_cost_steers_route_without_hard_exclusion():
    p=project()
    weighted={"geojson":geo_polygon((-500,-500,500,500)),"cost_per_m":20}
    result=search_route(p,config(weighted_areas=[weighted]))
    assert not projected_candidate(result).intersects(box(-499,-499,499,499))
    # A soft weighted zone may be crossed briefly if that lowers total cost.
    # The original direct crossing would incur approximately 20,000 equivalent m.
    assert result["report"]["objective_m_equivalent"]["zone_cost_m"] < 100


def test_actual_terrain_slope_constraint_detours_ridge_and_produces_new_valid_profile():
    data=terrain()
    for y,row in zip(data["y_m"],data["depth_m"]):
        if abs(y)<=500:
            row[3]=800
    p=project()
    result=search_route(p,config(terrain_grid=data,max_slope_deg=5,slope_weight=100))
    candidate=result["project"]
    a=analyze_project(candidate)
    assert candidate["profile"]["route_signature"]==route_signature(candidate)
    assert candidate["profile"]["measured"]
    assert a["summary"]["bottom_length_m"] is not None
    assert max(s["depth_m"] for s in a["profile"] if s["depth_m"] is not None)<100
    assert result["report"]["constraint_validation"]["terrain_constraints_checked"]
    assert max(r["candidate_objective"]["max_sampled_slope_deg"] for r in result["report"]["segments"])<=5+1e-6


def test_nodata_is_not_fabricated_or_crossed_for_hard_terrain_constraints():
    data=terrain()
    for row in data["depth_m"]:
        row[3]=None
    with pytest.raises(RoutingError,match="ROUTING_NO_PATH"):
        search_route(project(),config(terrain_grid=data,max_slope_deg=10))
    with pytest.raises(ValueError,match="二维 terrain_grid"):
        search_route(project(),config(max_slope_deg=10))


def test_terrain_sampler_honours_known_exact_nodes_and_missing_interpolation():
    data={"crs":LOCAL.to_string(),"x_m":[0,100],"y_m":[0,100],"depth_m":[[10,None],[30,40]]}
    grid=TerrainGrid(data,LOCAL)
    values=grid.sample([(0,0),(0,50),(100,0),(50,50),(101,50)])
    assert values[0]==10 and values[1]==20
    assert np.isnan(values[2:]).all()


def test_partial_search_preserves_prefix_suffix_and_uses_actual_missing_depth():
    p=project(xs=(-3000,-1000,1000,3000))
    p["route"]["legs"][1]["cable_type_id"]="B"
    p["bodies"]=[{"id":"before","kp_m":500,"length_m":10,"cost":100},{"id":"after","cable_kp_m":5500,"length_m":10,"cost":100}]
    p["events"]=[{"kp_m":5000,"stop_hours":2,"extra_cost":100}]
    p["layers"]=[layer("block","restricted",geo_polygon((-250,-500,250,500)))]
    before=analyze_project(p)
    result=search_route(p,config(start_point_index=1,end_point_index=2))
    q=result["project"]
    after=analyze_project(q)
    assert q["route"]["points"][0]==p["route"]["points"][0]
    assert q["route"]["points"][-1]==p["route"]["points"][-1]
    assert after["legs"][0]["bottom_length_m"]==pytest.approx(before["legs"][0]["bottom_length_m"],abs=.001)
    assert after["legs"][-1]["bottom_length_m"]==pytest.approx(before["legs"][-1]["bottom_length_m"],abs=.001)
    assert after["summary"]["bottom_length_m"] is None
    assert after["summary"]["material_cost"]==pytest.approx(before["summary"]["material_cost"],abs=1e-5)
    assert q["events"][0]["kp_m"]>p["events"][0]["kp_m"]


def test_explicit_via_and_rigid_waypoint_are_kept_as_geographic_anchors():
    p=project(xs=(-2000,0,2000),ys=(0,1000,0))
    p["route"]["points"][1]["constraint"]="rigid"
    result=search_route(p,config())
    assert result["report"]["via_point_indices"]==[1]
    kept=next(point for point in result["project"]["route"]["points"] if point["id"]=="p1")
    assert kept["longitude"]==pytest.approx(p["route"]["points"][1]["longitude"],abs=1e-10)
    assert kept["latitude"]==pytest.approx(p["route"]["points"][1]["latitude"],abs=1e-10)
    with pytest.raises(ValueError,match="严格递增"):
        search_route(p,config(via_point_indices=[1,1]))


def test_recalculate_flexible_mode_is_explicit_and_increases_required_cable():
    p=project()
    p["layers"]=[layer("block","restricted",geo_polygon((-300,-300,300,300)))]
    before=analyze_project(p)
    result=search_route(p,config(length_policy="recalculate"))
    after=analyze_project(result["project"])
    assert after["summary"]["cable_length_m"]>before["summary"]["cable_length_m"]
    assert all(leg["mode"]=="flexible" for leg in after["legs"])


def test_explicit_manufacturing_body_station_is_not_scaled_in_recalculate_mode():
    p=project()
    p["layers"]=[layer("block","restricted",geo_polygon((-300,-300,300,300)))]
    p["bodies"]=[{"id":"anchored","cable_kp_m":1000,"length_m":10}]
    result=search_route(p,config(length_policy="recalculate"))
    assert result["project"]["bodies"][0]["cable_kp_m"]==1000
    assert analyze_project(result["project"])["bodies"][0]["cable_kp_m"]==1000


def test_dtm_preview_input_reports_its_actual_coarser_grid_and_provenance():
    values=terrain()
    dtm={"metadata":{"crs":LOCAL.to_string(),"grid_spacing_m":50,"vertical_datum":"chart-datum"},
         "preview":{key:values[key] for key in ("x_m","y_m","depth_m")}}
    result=search_route(project(),config(terrain_grid=dtm,max_slope_deg=5))
    assert result["report"]["terrain"]["preview_grid"]
    assert result["report"]["terrain"]["width"]==7
    assert result["report"]["terrain"]["vertical_datum"]=="chart-datum"
    assert not result["project"]["profile"]["measured"]
    assert "ROUTE_SEARCH_PREVIEW_TERRAIN" in {w["code"] for w in result["warnings"]}


def test_high_latitude_antimeridian_obstacle_search_uses_short_local_domain():
    local=CRS.from_proj4("+proj=aeqd +lat_0=80 +lon_0=180 +datum=WGS84 +units=m")
    back=Transformer.from_crs(local,"EPSG:4326",always_xy=True)
    fwd=Transformer.from_crs("EPSG:4326",local,always_xy=True)
    p=project()
    for point,(x,y) in zip(p["route"]["points"],[(-10000,0),(10000,0)]):
        point["longitude"],point["latitude"]=back.transform(x,y)
    length=inverse(p["route"]["points"][0]["longitude"],p["route"]["points"][0]["latitude"],p["route"]["points"][1]["longitude"],p["route"]["points"][1]["latitude"])[0]
    p["profile"]={"samples":[{"kp_m":0,"depth_m":50},{"kp_m":length,"depth_m":50}],"route_signature":route_signature(p),"source":"user"}
    polygon={"type":"Polygon","coordinates":[[list(back.transform(x,y)) for x,y in [(-1000,-1500),(1000,-1500),(1000,1500),(-1000,1500),(-1000,-1500)]]]}
    p["layers"]=[layer("dateline-obstacle","restricted",polygon)]
    result=search_route(p,{"grid_spacing_m":500,"padding_m":4000,"clearance_m":100})
    assert result["preview"]["geometry"]["type"]=="MultiLineString"
    assert result["report"]["grid_cells"]<10000
    assert result["report"]["transformations"]["new_selected_length_m"]<30000
    coords=[]
    points=result["project"]["route"]["points"]
    for a,b in zip(points,points[1:]):
        coords.extend(densify(a["longitude"],a["latitude"],b["longitude"],b["latitude"],spacing_m=20))
    line=LineString([fwd.transform(*point) for point in coords])
    assert line.distance(box(-1000,-1500,1000,1500))>=99.9


def test_invalid_terrain_crs_raises_readable_value_error():
    with pytest.raises(ValueError,match="地形网格 CRS"):
        TerrainGrid({**terrain(),"crs":"missing-nonsense-crs"},LOCAL)


@pytest.mark.parametrize("change", [
    {"grid_spacing_m":0},{"clearance_m":-1},{"slope_weight":-1},
    {"obstacle_layer_ids":["missing"]},{"max_expansions":.5},
    {"weighted_areas":[{"geojson":geo_polygon((-50,-50,50,50)),"cost_per_m":-1}]},
])
def test_invalid_search_parameters_are_rejected(change):
    with pytest.raises(ValueError):
        search_route(project(),config(**change))
def test_explicit_manufacturing_domains_are_not_silently_bypassed_by_search():
    from oceanroute.constraints import configure_constraints
    p=configure_constraints(project(),{"mode":"fixed"})["project"]
    with pytest.raises(RoutingError,match="ROUTING_CONSTRAINT_DOMAIN_UNSUPPORTED"):
        search_route(p,config())
