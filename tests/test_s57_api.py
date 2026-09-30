"""Actual native chart uploads and shared-workspace persistence via HTTP."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.storage import ProjectStore

FIXTURE = Path(__file__).parent / "fixtures/s57/noaa/US5A1KMJ.zip"
CELL = "ENC_ROOT/US5A1KMJ/US5A1KMJ.000"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(ProjectStore(tmp_path / "native-s57.sqlite3"))) as client:
        yield client


def upload(client, stage="", config=None, *, raw=None, name="US5A1KMJ.zip"):
    response = client.post("/api/import/s57" + ("/" + stage if stage else ""),
                           data={"config_json": json.dumps(config or {})},
                           files={"file": (name, FIXTURE.read_bytes() if raw is None else raw,
                                           "application/octet-stream")})
    assert response.status_code == 200, response.text
    return response.json()


def test_real_s57_three_stage_upload_and_full_reference_layer_persistence(client):
    directory = upload(client, "inspect")
    assert directory["stage"] == "inspect" and not directory["can_apply"]
    assert [c["path"] for c in directory["cells"]] == [CELL]
    assert [u["number"] for u in directory["cells"][0]["updates"]] == [1, 2]
    catalog = upload(client, "catalog", {"cells": [CELL]})
    assert catalog["stage"] == "catalog" and not catalog["can_apply"]
    cell = next(c for c in catalog["cells"] if c["selected"])
    assert cell["dsid"]["DSID_UPDN"] == "2"
    assert cell["base_dsid"]["DSID_UPDN"] == "0"
    assert cell["dsid"]["DSPM_DUNI"] == 1 and cell["dsid"]["DSPM_SDAT"] == 12
    classes = {c["name"]: c for c in catalog["classes_catalog"]}
    assert classes["SOUNDG"]["feature_count"] == 4
    assert classes["DEPARE"]["feature_count"] == 88
    assert classes["DEPCNT"]["feature_count"] == 123
    bundle = upload(client, config={"cells": [CELL], "classes": ["SOUNDG", "DEPARE", "DEPCNT"],
                                    "name": "真实NOAA参考海图"})
    assert bundle["stage"] == "import" and bundle["can_apply"] and bundle["output_crs"] == "EPSG:4326"
    layers = {l["source"]["object_class"]: l for l in bundle["layers"]}
    assert set(layers) == {"SOUNDG", "DEPARE", "DEPCNT"}
    assert all(l["kind"] == "reference" for l in bundle["layers"])
    assert layers["SOUNDG"]["geojson"]["features"][0]["geometry"]["coordinates"][0] == [177.5191417, 51.9181095, 23.7]
    assert len(layers["SOUNDG"]["geojson"]["features"]) == 4
    source = client.get("/api/sample").json()
    workspace = client.post("/api/workspace/migrate", json={"project": source}).json()["workspace"]
    original = deepcopy(workspace)
    added = client.post("/api/workspace/action", json={"workspace": workspace, "config": {
        "action": "update_shared", "layers": workspace["layers"] + bundle["layers"]}})
    assert added.status_code == 200, added.text
    candidate = added.json()["workspace"]
    for key in ["paths", "assemblies", "associations", "cable_types", "terrain_sources"]:
        assert candidate[key] == original[key]
    assert candidate["layers"][-3:] == bundle["layers"]
    saved = client.post("/api/workspaces", json=candidate)
    assert saved.status_code == 200, saved.text
    reopened = client.get("/api/workspaces/" + candidate["id"])
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["layers"] == candidate["layers"]
    exported = client.post("/api/workspace/export", json=reopened.json())
    assert exported.status_code == 200, exported.text
    imported = client.post("/api/workspace/import", json={"text": exported.text})
    assert imported.status_code == 200, imported.text
    assert imported.json()["workspace"]["layers"] == candidate["layers"]
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() in json.dumps(bundle)


@pytest.mark.parametrize("stage", ["/inspect", "/catalog", ""])
@pytest.mark.parametrize("setting", ["[]", "null", '{"name":"a","name":"b"}',
                                      '{"timeout_s":NaN}', '{"timeout_s":1e999}',
                                      '{"name":"\\ud800"}', "[" * 1200 + "]" * 1200,
                                      '{"name":"' + "a" * 32768 + '"}'],
                         ids=["array", "null", "duplicate-key", "nan", "infinity",
                              "surrogate", "nesting-limit", "name-size-limit"])
def test_bad_multipart_config_is_an_http_input_error_not_partial_native_success(client, stage, setting):
    result = client.post("/api/import/s57" + stage, data={"config_json": setting},
                         files={"file": ("US5A1KMJ.zip", FIXTURE.read_bytes())})
    assert result.status_code == 422, result.text
    assert isinstance(result.json()["detail"], str)
    assert "S-57" in result.json()["detail"]


@pytest.mark.parametrize("config", [{"classes": []}, {"cells": []}, {"classes": ["DOESNOTEXIST"]},
                                    {"max_vertices": 1}, {"max_output_bytes": 1}, {"unknown": 1}])
def test_rejected_native_upload_leaves_persisted_workspace_unchanged(client, config):
    sample = client.get("/api/sample").json()
    workspace = client.post("/api/workspace/migrate", json={"project": sample}).json()["workspace"]
    saved = client.post("/api/workspaces", json=workspace).json()["workspace"]
    result = client.post("/api/import/s57", data={"config_json": json.dumps(config)},
                         files={"file": ("US5A1KMJ.zip", FIXTURE.read_bytes())})
    assert result.status_code == 422, result.text
    assert client.get("/api/workspaces/" + saved["id"]).json() == saved
