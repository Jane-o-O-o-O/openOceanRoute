import json
from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.storage import ProjectStore


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(ProjectStore(tmp_path / "workflows.sqlite3")))


def test_engineering_preview_and_apply_roundtrip(client):
    original = client.get("/api/sample").json()
    response = client.post("/api/tools/subdivide", json={"project": original, "config": {"spacing_m": 20000}})
    assert response.status_code == 200, response.text
    preview = response.json()
    before = client.post("/api/analyze", json=original).json()
    after = client.post("/api/analyze", json=preview["project"]).json()
    assert len(preview["project"]["route"]["points"]) > len(original["route"]["points"])
    for key in ("surface_length_m", "cable_length_m", "material_cost"):
        assert after["summary"][key] == pytest.approx(before["summary"][key], rel=1e-10)
    assert client.post("/api/projects", json=preview["project"]).status_code == 200


def test_terrain_http_valid_raster_and_contour(client):
    response = client.post("/api/dtm/grid", json={"text": "longitude,latitude,depth_m\n118,22,100\n118.01,22,120\n118,22.01,140\n118.01,22.01,160", "config": {"spacing_m": 100, "max_gap_m": 2000, "contour_interval_m": 10}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["geotiff_base64"]) > 100
    assert result["metadata"]["crs"]
    assert result["preview"]["depth_m"]
    json.dumps(result, allow_nan=False)


def test_actual_ship_plan_and_branch_http(client):
    project = client.get("/api/sample").json()
    plan = client.post("/api/shipplan/create", json={"project": project, "config": {"sample_spacing_m": 100000}})
    assert plan.status_code == 200, plan.text
    assert len(plan.json()["instructions"]) > 1
    assert plan.json()["ship_plan"]
    scenarios = client.post("/api/shipplan/lookahead", json={"project": project, "config": {"depth_m": 100, "duration_s": 2, "nodes": 12, "internal_dt_s": 0.1}, "scenarios": [{"name": "base", "overrides": {}}, {"name": "flow", "overrides": {"current_y_m_s": 0.2}}]})
    assert scenarios.status_code == 200, scenarios.text
    assert len(scenarios.json()["scenarios"]) == 2
    json.dumps(scenarios.json(), allow_nan=False)


def test_gis_http_does_not_relabel_altitude_as_depth(client):
    response = client.post("/api/import/kml", json={"text": '<kml><Placemark><LineString><coordinates>118,22,-20 119,23,-30</coordinates></LineString></Placemark></kml>'})
    assert response.status_code == 200, response.text
    assert response.json()["route_candidates"][0]["points"][0]["depth_m"] is None
