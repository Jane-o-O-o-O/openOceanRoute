import io
import json
import zipfile

from pyproj import CRS, Transformer
import pytest
import shapefile

from oceanroute.geoformats import import_kml, import_shapefile_zip


def shapefile_zip(prj=True, duplicate=False):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYLINE, encoding="gbk") as writer:
        writer.field("NAME", "C", size=80)
        transformer = Transformer.from_crs(4326, 3857, always_xy=True)
        writer.line([[transformer.transform(118, 22), transformer.transform(118.1, 22.1)]])
        writer.record("已敷海缆")
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for ext, data in [("shp", shp.getvalue()), ("shx", shx.getvalue()), ("dbf", dbf.getvalue()), ("cpg", b"936")]:
            archive.writestr("survey/route." + ext, data)
        if prj:
            archive.writestr("survey/route.prj", CRS.from_epsg(3857).to_wkt())
        if duplicate:
            for ext, data in [("shp", shp.getvalue()), ("dbf", dbf.getvalue())]:
                archive.writestr("second/route." + ext, data)
    return result.getvalue()


def test_shapefile_projection_and_chinese_encoding():
    layer = import_shapefile_zip(shapefile_zip(), kind="cable")
    feature = layer["geojson"]["features"][0]
    assert feature["properties"]["NAME"] == "已敷海缆"
    assert feature["geometry"]["coordinates"][0] == pytest.approx([118, 22], abs=1e-9)
    assert layer["source"]["source_crs"] == "EPSG:3857"
    json.dumps(layer, allow_nan=False)


def test_missing_projection_never_guessed():
    with pytest.raises(ValueError, match="源坐标系"):
        import_shapefile_zip(shapefile_zip(prj=False))
    result = import_shapefile_zip(shapefile_zip(prj=False), crs_override="EPSG:3857")
    assert result["geojson"]["features"][0]["geometry"]["coordinates"][0][0] == pytest.approx(118)


def test_multiple_shapefiles_require_explicit_choice():
    with pytest.raises(ValueError, match="source_name"):
        import_shapefile_zip(shapefile_zip(duplicate=True))
    result = import_shapefile_zip(shapefile_zip(duplicate=True), source_name="survey/route.shp")
    assert len(result["geojson"]["features"]) == 1


def test_zip_invalid_path_and_format_rejected():
    with pytest.raises(ValueError, match="有效ZIP"):
        import_shapefile_zip(b"not zip")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("../route.shp", b"bad")
    with pytest.raises(ValueError, match="路径"):
        import_shapefile_zip(data.getvalue())


def test_kml_holes_multigeometry_and_altitude_not_depth():
    kml = '''<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
    <Placemark><name>路线</name><ExtendedData><Data name="owner"><value>test</value></Data></ExtendedData>
    <LineString><altitudeMode>absolute</altitudeMode><coordinates>118,22,-30 118.1,22.1,-180</coordinates></LineString></Placemark>
    <Placemark><name>禁区</name><Polygon><outerBoundaryIs><LinearRing><coordinates>118,22 119,22 119,23 118,23 118,22</coordinates></LinearRing></outerBoundaryIs>
    <innerBoundaryIs><LinearRing><coordinates>118.2,22.2 118.2,22.4 118.4,22.4 118.4,22.2 118.2,22.2</coordinates></LinearRing></innerBoundaryIs></Polygon></Placemark>
    <Placemark><MultiGeometry><Point><coordinates>118,22,0</coordinates></Point><LineString><coordinates>118,22 119,23</coordinates></LineString></MultiGeometry></Placemark>
    <NetworkLink><Link><href>https://example.invalid/remote.kml</href></Link></NetworkLink>
    </Document></kml>'''
    layer = import_kml(kml)
    features = layer["geojson"]["features"]
    assert len(features) == 3
    assert features[0]["properties"]["owner"] == "test"
    assert len(features[1]["geometry"]["coordinates"]) == 2
    assert features[2]["geometry"]["type"] == "GeometryCollection"
    assert layer["route_candidates"][0]["points"][0]["depth_m"] is None
    assert features[0]["geometry"]["coordinates"][0][2] == -30
    assert any("NetworkLink" in warning for warning in layer["warnings"])


@pytest.mark.parametrize("text", [
    '<!DOCTYPE kml [<!ENTITY x "hi">]><kml/>',
    '<kml><Placemark><Point><coordinates>999,22</coordinates></Point></Placemark></kml>',
    '<kml><Placemark><LineString><coordinates>118,22</coordinates></LineString></Placemark></kml>',
    '<kml><Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>118,22 119,22 119,23 118,23</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>',
])
def test_invalid_kml_geometry_and_entity_rejected(text):
    with pytest.raises(ValueError):
        import_kml(text)
