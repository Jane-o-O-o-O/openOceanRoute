from fastapi.testclient import TestClient
import pytest
from oceanroute.api import create_app
from oceanroute.storage import ProjectStore


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(ProjectStore(tmp_path / "revisions.sqlite3")))


def test_same_project_tool_keeps_revision_and_rejects_real_second_window(client):
    project = client.get("/api/sample").json()
    saved = client.post("/api/projects", json=project).json()["project"]
    tool = client.post("/api/tools/subdivide", json={"project": saved, "config": {"spacing_m": 20000}})
    assert tool.status_code == 200
    edited = tool.json()["project"]
    assert edited["saved_revision"] == 1
    latest = client.post("/api/projects", json=saved).json()["project"]
    assert latest["saved_revision"] == 2
    rejected = client.post("/api/projects", json=edited)
    assert rejected.status_code == 422
    assert "另一窗口" in rejected.json()["detail"]
    assert client.get(f"/api/projects/{saved['id']}").json()["saved_revision"] == 2


def test_missing_revision_cannot_bypass_lock_but_import_is_new_identity(client):
    project = client.get("/api/sample").json()
    assert client.post("/api/projects", json=project).status_code == 200
    assert client.post("/api/projects", json=project).status_code == 422
    file = client.post("/api/export/project", json=project).text
    imported = client.post("/api/import/project", json={"text": file}).json()
    assert imported["id"] != project["id"]
    assert imported["origin_project_id"] == project["id"]
    assert client.post("/api/projects", json=imported).status_code == 200
    assert len(client.get("/api/projects").json()) == 2


def test_sample_loads_are_new_working_projects(client):
    assert client.get("/api/sample").json()["id"] != client.get("/api/sample").json()["id"]
