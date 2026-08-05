from fastapi.testclient import TestClient
import pytest
from oceanroute.api import create_app
from oceanroute.storage import ProjectStore


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(ProjectStore(tmp_path / "extended.sqlite3")))


def test_true_constraint_domain_and_direct_edit_guard_http(client):
    project = client.get("/api/sample").json()
    configured = client.post("/api/constraints/configure", json={"project": project, "config": {"mode": "fixed"}})
    assert configured.status_code == 200, configured.text
    p = configured.json()["project"]
    before = configured.json()["report"]["after_summary"]
    moved = client.post("/api/constraints/edit", json={"project": p, "config": {"moves": [{"point_id": p["route"]["points"][1]["id"], "longitude": p["route"]["points"][1]["longitude"]+.005, "latitude": p["route"]["points"][1]["latitude"]}]}})
    assert moved.status_code == 200, moved.text
    assert moved.json()["report"]["after_summary"]["cable_length_m"] == pytest.approx(before["cable_length_m"])
    p["route"]["points"][1]["longitude"] += .005
    rejected = client.post("/api/analyze", json=p)
    assert rejected.status_code == 422
    assert rejected.json()["code"].startswith("CONSTRAINT")


def test_manufacturing_http_real_preview_csv_and_apply(client):
    project = client.get("/api/sample").json()
    text = "kind,name,cable_type_id,length_m,cost\ncable,轻型,LW,300000,\nbody,接头,,2,800\ncable,重缆,DA,250000,"
    preview = client.post("/api/assembly/import", json={"project": project, "text": text, "config": {"position_mode": "relative", "mapping_policy": "surface_fraction"}})
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["report"]["manufactured_total_m"] == 550002
    assert result["report"]["material_conservation_checked"]
    assert client.post("/api/projects", json=result["project"]).status_code == 200
    exported = client.post("/api/export/assembly", json=result["project"])
    assert exported.status_code == 200
    rebuilt = client.post("/api/assembly/import", json={"project": project, "text": exported.text, "config": {"position_mode": "absolute", "mapping_policy": "surface_fraction"}})
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["report"]["after_summary"]["material_cost"] == pytest.approx(result["report"]["after_summary"]["material_cost"])


def test_checkpoint_http_resume_is_actual_state(client):
    cfg = {"depth_m": 30, "nodes": 8, "duration_s": 2, "internal_dt_s": .05, "dt_s": .5, "ship_speed_m_s": .5, "payout_m_s": .6}
    original = client.post("/api/simulation/dynamic", json={"config": cfg})
    assert original.status_code == 200, original.text
    saved = original.json()["checkpoint"]
    resumed = client.post("/api/simulation/dynamic", json={"config": {"resume_state": saved, "duration_s": 2}})
    assert resumed.status_code == 200, resumed.text
    continuous = client.post("/api/simulation/dynamic", json={"config": {**cfg, "duration_s": 4}})
    assert continuous.status_code == 200
    a, b = resumed.json()["checkpoint"], continuous.json()["checkpoint"]
    assert a["state"]["positions"] == b["state"]["positions"]
    assert a["state"]["velocities"] == b["state"]["velocities"]
    assert a["state"]["paid_out_m"] == pytest.approx(b["state"]["paid_out_m"])
