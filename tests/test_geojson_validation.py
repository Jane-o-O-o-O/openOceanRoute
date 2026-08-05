import json
import pytest
from oceanroute.exchange import import_geojson


@pytest.mark.parametrize("data", [
    [],
    {"type": "FeatureCollection", "features": [1]},
    {"type": "FeatureCollection", "features": [{"type": "Wrong", "geometry": None}]},
    {"type": "Point", "coordinates": [118]},
    {"type": "Point", "coordinates": [118, 22], "crs": {"type": "name", "properties": {"name": "EPSG:3857"}}},
])
def test_malformed_and_non_wgs84_geojson_has_actionable_error(data):
    with pytest.raises(ValueError):
        import_geojson(json.dumps(data))
