"""Actual projection, geometry topology and atomic display contracts."""
from copy import deepcopy
import json
import math

from fastapi.testclient import TestClient
from pyproj import CRS, Proj, Transformer
import pytest

from oceanroute.api import create_app
from oceanroute.map_projection import project_map
from oceanroute.storage import ProjectStore


def request(target="EPSG:32650", **extra):
    return {"target_crs":target, "points":[{"id":"p", "label":"start", "longitude":118., "latitude":22.}], **extra}


def layer(geometry, **extra):
    return {"id":"reference", "name":"Reference", "kind":"reference", "visible":True,
            "geojson":{"type":"FeatureCollection", "features":[{"type":"Feature", "id":"f", "properties":{"name":"measured", "data":[1,2]}, "geometry":geometry}]}, **extra}


def test_known_utm_values_full_metadata_and_no_input_mutation():
    payload=request(); before=deepcopy(payload)
    result=project_map(payload)
    assert payload==before and result["can_display"] and not result["errors"]
    assert result["points"][0]["coordinates"]==pytest.approx([603224.6404290784,2433164.428653589],abs=1e-7)
    assert result["points"][0]["source"]==[118,22]
    assert result["target_crs"]["axis_units"][0]["conversion_to_m"]==1
    assert result["axis_order"]=="xy" and result["operations"][0]["best_available"]
    assert not result["operations"][0]["ballpark"]
    assert result["budget"]["transformed_vertices"]==1
    json.dumps(result,allow_nan=False)


def test_local_scale_and_north_arrow_match_independent_proj_factors():
    result=project_map(request())
    axes=result["points"][0]["local_axes"]
    factors=Proj("EPSG:32650").get_factors(118,22)
    assert axes["east_scale"]==pytest.approx(factors.parallel_scale,abs=2e-8)
    assert axes["north_scale"]==pytest.approx(factors.meridional_scale,abs=2e-8)
    assert axes["grid_north_clockwise_deg"]==pytest.approx(-factors.meridian_convergence,abs=2e-7)
    assert axes["east_north_angle_deg"]==pytest.approx(90,abs=1e-6)
    assert axes["height_or_combined_ground_scale"] is False


def test_native_feet_and_metres_have_same_physical_scale():
    feet=CRS.from_proj4("+proj=utm +zone=50 +datum=WGS84 +units=ft +type=crs").to_wkt()
    result=project_map(request(feet)); point=result["points"][0]
    assert point["coordinates"][0]*.3048==pytest.approx(603224.6404290784,abs=1e-7)
    assert result["target_crs"]["axis_units"][0]["conversion_to_m"]==.3048
    meters=project_map(request())["points"][0]
    assert point["local_axes"]["north_scale"]==pytest.approx(meters["local_axes"]["north_scale"],abs=3e-9)


def test_polar_projection_is_actual_native_geometry():
    r=project_map(request("EPSG:3413",points=[{"id":"arctic","longitude":10,"latitude":85}]))
    assert r["can_display"]
    xy=Transformer.from_crs(4326,3413,always_xy=True).transform(10,85)
    assert r["points"][0]["coordinates"]==pytest.approx(xy,abs=1e-7)
    assert r["points"][0]["local_axes"]["grid_north_clockwise_deg"]==pytest.approx(-55,abs=1e-7)


def test_exact_pole_has_no_mercator_image_or_invented_geographic_north():
    point=[{"id":"pole","longitude":0,"latitude":90}]
    mercator=project_map(request("EPSG:3857",points=point))
    assert not mercator["can_display"] and mercator["errors"][0]["code"]=="MAP_PROJECTION_DOMAIN"
    polar=project_map(request("EPSG:3413",points=point))
    assert polar["can_display"] and polar["points"][0]["coordinates"]==pytest.approx([0,0],abs=1e-8)
    assert polar["points"][0]["local_axes"] is None
    assert any(w["code"]=="MAP_LOCAL_AXES_UNAVAILABLE" for w in polar["warnings"])


@pytest.mark.parametrize("longitude",[180.,-180.,179.99999999,-179.99999999])
def test_date_line_display_derivative_is_not_a_wrapped_world_width(longitude):
    r=project_map(request("EPSG:3857",points=[{"id":"seam","longitude":longitude,"latitude":0}]))
    assert r["can_display"] and r["points"][0]["local_axes"] is None
    assert any(w["code"]=="MAP_LOCAL_AXES_UNAVAILABLE" for w in r["warnings"])
    expected=Proj("EPSG:3857").get_factors(longitude,0)
    assert expected.parallel_scale==pytest.approx(1,abs=1e-9)
    assert abs(r["points"][0]["coordinates"][0])>20_000_000


def test_date_line_does_not_disable_a_continuous_utm_native_branch():
    r=project_map(request("EPSG:32660",points=[{"id":"seam","longitude":180,"latitude":0}]))
    assert r["can_display"] and r["points"][0]["local_axes"] is not None
    factor=Proj("EPSG:32660").get_factors(180,0)
    assert r["points"][0]["local_axes"]["east_scale"]==pytest.approx(factor.parallel_scale,abs=1e-8)


def test_supplied_date_line_route_segments_are_not_reconnected():
    route={"id":"r", "name":"Date line", "role":"deployment", "geometry":{"type":"MultiLineString", "coordinates":[[[179,10],[180,10]],[[-180,10],[-179,10]]]}}
    r=project_map(request("EPSG:3857",points=[],routes=[route]))
    assert r["can_display"] and len(r["routes"][0]["geometry"]["coordinates"])==2
    segments=r["routes"][0]["geometry"]["coordinates"]
    assert all(abs(s[-1][0]-s[0][0])<120_000 for s in segments)
    assert segments[0][-1][0]>20_000_000 and segments[1][0][0]<-20_000_000
    assert r["budget"]["generated_vertices"]==4


def test_reference_polygon_hole_properties_altitude_and_densification_preserved():
    outer=[[118,22,7],[118.1,22,7],[118.1,22.1,7],[118,22.1,7],[118,22,7]]
    hole=[[118.02,22.02,7],[118.04,22.02,7],[118.04,22.04,7],[118.02,22.04,7],[118.02,22.02,7]]
    obj=layer({"type":"Polygon", "coordinates":[outer,hole], "bbox":[118,22,118.1,22.1], "crs":{"name":"old"}})
    obj["geojson"]["bbox"]=[118,22,118.1,22.1]
    original=deepcopy(obj)
    r=project_map(request(layers=[obj],config={"densify_max_distance_m":1000}))
    assert r["can_display"] and obj==original
    geojson=r["layers"][0]["geojson"]; feature=geojson["features"][0]
    assert feature["properties"]==original["geojson"]["features"][0]["properties"]
    rings=feature["geometry"]["coordinates"]
    assert len(rings)==2 and len(rings[0])>len(outer) and len(rings[1])>len(hole)
    assert all(p[2]==7 for ring in rings for p in ring)
    assert all(ring[0]==ring[-1] for ring in rings)
    assert "bbox" not in geojson and "bbox" not in feature["geometry"] and "crs" not in feature["geometry"]


@pytest.mark.parametrize("geometry",[
    {"type":"Point","coordinates":[118,22]},
    {"type":"MultiPoint","coordinates":[[118,22],[118.1,22]]},
    {"type":"MultiLineString","coordinates":[[[118,22],[118.1,22]],[[118,22.1],[118.1,22.1]]]},
    {"type":"MultiPolygon","coordinates":[[[[118,22],[118.1,22],[118,22.1],[118,22]]]]},
    {"type":"GeometryCollection","geometries":[{"type":"Point","coordinates":[118,22]}, {"type":"LineString","coordinates":[[118,22],[118.01,22]]}]},
])
def test_all_standard_geojson_topologies_display(geometry):
    r=project_map(request(layers=[layer(geometry)]))
    assert r["can_display"] and r["layers"][0]["geojson"]["features"][0]["geometry"]["type"]==geometry["type"]


def test_hidden_layer_is_not_projected_and_does_not_poison_visible_map():
    obj=layer({"type":"bad","coordinates":[float("inf"),22]},visible=False)
    # Nonfinite JSON remains invalid even in hidden fields.
    with pytest.raises(ValueError,match="finite JSON"):project_map(request(layers=[obj]))
    obj["geojson"]={"type":"unprojectable-malformed"}
    r=project_map(request(layers=[obj]))
    assert r["can_display"] and r["budget"]["generated_vertices"]==1
    assert r["layers"][0]["geojson"]["features"]==[] and r["layers"][0]["skipped_reason"]


@pytest.mark.parametrize("geometry",[
    {"type":"LineString","coordinates":[[118,22]]},
    {"type":"Polygon","coordinates":[[[118,22],[118.1,22],[118,22.1]]]},
    {"type":"Point","coordinates":[True,22]},
    {"type":"Point","coordinates":[181,22]},
    {"type":"Point","coordinates":[118,22,7,4]},
    {"type":"Polygon","coordinates":[[[118,22],[118.1,22.1],[118,22.1],[118.1,22],[118,22]]]},
])
def test_invalid_layer_prevents_partial_success_map(geometry):
    r=project_map(request(layers=[layer(geometry)]))
    assert not r["can_display"] and r["errors"][0]["entity"]["id"]=="reference"
    assert r["routes"]==r["points"]==r["layers"]==[] and r["bounds"] is None


def test_generated_vertex_budget_is_preflighted_without_projection(monkeypatch):
    def unavailable(_):raise AssertionError("should not transform over-budget input")
    monkeypatch.setattr("oceanroute.map_projection.transform_coordinates",unavailable)
    r=project_map(request(layers=[layer({"type":"LineString","coordinates":[[118,22],[119,22]]})],config={"max_vertices":10,"densify_max_distance_m":1}))
    assert not r["can_display"] and r["errors"][0]["code"]=="MAP_VERTEX_BUDGET"
    assert r["budget"]["transformed_vertices"]==0


def test_regional_operation_budget_is_preflighted_including_local_axes(monkeypatch):
    def unavailable(_):raise AssertionError("regional operation budget must stop before conversion")
    monkeypatch.setattr("oceanroute.map_projection.transform_coordinates",unavailable)
    route={"id":"regional","geometry":{"type":"LineString","coordinates":[[2+i*1e-7,48] for i in range(65)]}}
    r=project_map(request("EPSG:23031",points=[],routes=[route],config={"max_operation_selections":64}))
    assert not r["can_display"] and r["errors"][0]["code"]=="MAP_OPERATION_BUDGET"
    assert r["budget"]["estimated_operation_selections"]==65
    assert r["budget"]["operation_selections"]==r["budget"]["transformed_vertices"]==0


def test_real_regional_datum_operation_is_used_and_charged():
    r=project_map(request("EPSG:23031",points=[{"id":"paris","longitude":2,"latitude":48}],routes=[{"id":"regional","geometry":{"type":"LineString","coordinates":[[2+i*1e-7,48] for i in range(30)]}}],config={"max_operation_selections":64}))
    assert r["can_display"] and r["budget"]["estimated_operation_selections"]==32
    assert r["budget"]["operation_selections"]==32 and r["points"][0]["local_axes"] is not None
    assert all(op["ballpark"] is False and op["best_available"] is True for op in r["operations"])
    op=r["operations"][0]
    assert "ED50" in op["description"]
    assert r["points"][0]["coordinates"][0]>400_000


def test_mixed_crs_axis_units_never_generate_a_mislabeled_map():
    definition=CRS.from_epsg(32631).to_json_dict(); definition.pop("id",None)
    definition["coordinate_system"]["axis"][0]["unit"]={"type":"LinearUnit","name":"foot","conversion_factor":.3048}
    mixed=CRS.from_json_dict(definition).to_wkt()
    with pytest.raises(ValueError,match="混合轴单位"):project_map(request(mixed))


def test_batched_geometry_larger_than_coordinate_editor_limit():
    line=[[118+i*1e-7,22] for i in range(10_001)]
    r=project_map(request(points=[],routes=[{"id":"large", "geometry":{"type":"LineString","coordinates":line}}]))
    assert r["can_display"] and len(r["routes"][0]["geometry"]["coordinates"])==10_001
    assert r["budget"]["transformed_vertices"]==10_001
    assert len(r["operations"])==1


def test_projection_domain_failure_and_missing_operation_return_no_partial_map(monkeypatch):
    r=project_map(request("+proj=ortho +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +type=crs"))
    assert not r["can_display"] and r["errors"][0]["code"]=="MAP_PROJECTION_DOMAIN"
    def unavailable(_):raise ValueError("required regional grid unavailable")
    monkeypatch.setattr("oceanroute.map_projection.transform_coordinates",unavailable)
    r=project_map(request())
    assert not r["can_display"] and r["errors"][0]["code"]=="MAP_OPERATION_UNAVAILABLE"


def test_out_of_area_warnings_aggregated_without_claiming_local_accuracy():
    r=project_map(request(points=[{"id":"outside","longitude":125,"latitude":22}]))
    assert r["can_display"] and r["warnings"][0]["code"]=="COORDINATE_OUTSIDE_AREA"
    assert r["warnings"][0]["entity"]["id"]=="outside"
    assert r["warnings"][0]["count"]==1


def test_geographic_long_edge_is_not_guessed_as_a_short_date_line_crossing():
    r=project_map(request("EPSG:3857",points=[],layers=[layer({"type":"LineString","coordinates":[[179,10],[-179,10]]})],config={"densify_max_distance_m":100_000}))
    assert r["can_display"] and r["warnings"][0]["code"]=="MAP_LONG_GEOGRAPHIC_EDGE"
    coords=r["layers"][0]["geojson"]["features"][0]["geometry"]["coordinates"]
    assert len(coords)>350 and any(abs(p[0])<60_000 for p in coords)


@pytest.mark.parametrize("target",["EPSG:4326","EPSG:4979","EPSG:4978","EPSG:7415"])
def test_target_must_be_only_horizontal_projected(target):
    with pytest.raises(ValueError):project_map(request(target))


def test_http_contract_has_finite_json_and_rejects_extra_mutation_fields(tmp_path):
    with TestClient(create_app(ProjectStore(tmp_path/"map-api.sqlite3"))) as client:
        response=client.post("/api/maps/project",json=request())
        assert response.status_code==200 and response.json()["can_display"]
        bad=client.post("/api/maps/project",json=request(project={"id":"mutation"}))
        assert bad.status_code==422 and "detail" in bad.json()
        assert client.post("/api/maps/project",json=request(config={"max_vertices":True})).status_code==422
        assert client.post("/api/maps/project",json=request(config={"densify_max_distance_m":10**1000})).status_code==422
