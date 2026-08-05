from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.storage import ProjectStore


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(ProjectStore(tmp_path / "api.sqlite3")))


def test_complete_project_lifecycle_and_exchange(client):
    project=client.get("/api/sample").json()
    analysis=client.post("/api/analyze",json=project)
    assert analysis.status_code==200,analysis.text
    assert analysis.json()["summary"]["surface_length_m"]>0
    first=client.post("/api/projects",json=project)
    assert first.status_code==200,first.text
    saved=first.json()["project"]
    saved["name"]="修改后"
    second=client.post("/api/projects",json=saved).json()
    assert second["revision"]==2
    revisions=client.get(f"/api/projects/{saved['id']}/revisions").json()
    assert len(revisions)==2
    restored=client.post(f"/api/projects/{saved['id']}/restore/1").json()
    assert restored["name"]==project["name"]
    for format_name in ["csv","kml","geojson","dxf","sld","report","project"]:
        response=client.post(f"/api/export/{format_name}",json=project)
        assert response.status_code==200,response.text
        assert response.headers["content-disposition"].startswith("attachment")
        assert len(response.content)>20
    csv_text=client.post("/api/export/csv",json=project).text
    imported=client.post("/api/import/rpl",json={"text":csv_text}).json()
    assert len(imported["points"])==len(project["route"]["points"])


def test_reverse_twice_preserves_geometry_profile_and_lengths(client):
    project=client.get("/api/sample").json()
    original=client.post("/api/analyze",json=project).json()
    reverse=client.post("/api/route/reverse",json=project).json()
    double=client.post("/api/route/reverse",json=reverse).json()
    analyzed=client.post("/api/analyze",json=double)
    assert analyzed.status_code==200,analyzed.text
    assert project["route"]["points"]==double["route"]["points"]
    for key in ["surface_length_m","bottom_length_m","cable_length_m","material_cost"]:
        assert analyzed.json()["summary"][key] == pytest.approx(original["summary"][key], rel=1e-10)


def test_profile_import_binds_to_current_geometry_and_edit_invalidates(client):
    project=client.get("/api/sample").json()
    a=client.post("/api/analyze",json=project).json()
    length=a["summary"]["surface_length_m"]
    imported=client.post("/api/import/profile",json={"project":project,"text":f"kp_m,depth_m\n0,100\n{length},100"})
    assert imported.status_code==200,imported.text
    attached=imported.json()["project"]
    result=client.post("/api/analyze",json=attached).json()
    assert result["summary"]["bottom_length_m"]==pytest.approx(length)
    attached["route"]["points"][1]["longitude"]+=.01
    stale=client.post("/api/analyze",json=attached).json()
    assert stale["summary"]["bottom_length_m"] is None
    assert any(w["code"]=="PROFILE_STALE" for w in stale["warnings"])


def test_split_material_accounting_and_profile(client):
    project=client.get("/api/sample").json()
    whole=client.post("/api/analyze",json=project).json()
    split=client.post("/api/route/split",json={"project":project,"point_index":2})
    assert split.status_code==200,split.text
    parts=[]
    for part in split.json()["projects"]:
        response=client.post("/api/analyze",json=part)
        assert response.status_code==200,response.text
        parts.append(response.json())
    for key in ["surface_length_m","bottom_length_m","cable_length_m","material_cost","body_cost"]:
        assert sum(p["summary"][key] for p in parts)==pytest.approx(whole["summary"][key],rel=1e-8)


def test_numeric_simulation_results_and_input_errors(client):
    response=client.post("/api/simulation/catenary",json={"config":{"depth_m":100,"wet_weight_n_m":5,"bottom_tension_n":100,"nodes":20}})
    assert response.status_code==200,response.text
    result=response.json()
    assert result["validation_status"]=="research"
    assert result.get("nodes") or result.get("frames")
    invalid=client.post("/api/analyze",json={"route":{"points":[]}})
    assert invalid.status_code==422
    assert isinstance(invalid.json()["detail"],str)
