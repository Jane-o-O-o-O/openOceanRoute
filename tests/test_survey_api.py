from copy import deepcopy
import json

from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.core import sample_project
from oceanroute.storage import ProjectStore


def test_csv_observations_are_serialized_without_replacing_project_or_depth_datum(tmp_path):
    project = sample_project()
    project["route"]["points"] = [
        {"id": "p1", "longitude": 0, "latitude": 0, "depth_m": 30},
        {"id": "p2", "longitude": .01, "latitude": 0, "depth_m": 30},
    ]
    project["route"]["legs"] = [{"cable_type_id": "LW", "slack_pct": 1}]
    project["profile"] = {"samples": [], "source": "none"}
    project["bodies"], project["events"], project["layers"] = [], [], []
    original = deepcopy(project)
    client = TestClient(create_app(ProjectStore(tmp_path / "db.sqlite3")))
    response = client.post("/api/survey/reconcile", json={"project": project, "config": {
        "text": "id,longitude,latitude,depth_m,cable_kp_m\nfirst,0,0,35,1000\nlast,0.01,0,35,2125",
        "source": "synthetic-api-test",
    }})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["summary"]["matched_count"] == 2
    assert result["observations"][0]["observed_depth_m"] == 35
    assert result["observations"][0]["planned_depth_m"] == 30
    assert result["observations"][0]["depth_difference_m"] is None
    assert result["segments"][0]["measured_cable_length_m"] == 1125
    assert result["geojson"]["type"] == "FeatureCollection"
    json.dumps(result, allow_nan=False)
    assert project == original


def test_survey_station_order_returns_actionable_422_code(tmp_path):
    client = TestClient(create_app(ProjectStore(tmp_path / "db.sqlite3")))
    project = sample_project()
    first, last = project["route"]["points"][0], project["route"]["points"][-1]
    rows = [{"longitude": p["longitude"], "latitude": p["latitude"], "cable_kp_m": 10} for p in (first, last)]
    response = client.post("/api/survey/reconcile", json={"project": project, "config": {"observations": rows}})
    assert response.status_code == 422
    assert response.json()["code"] == "SURVEY_CABLE_STATION_ORDER"
