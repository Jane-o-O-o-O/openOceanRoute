"""Independent API admission checks; no automatic geometry oracle is reused."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.storage import ProjectStore
from oceanroute.workspace import _validate, migrate_project
import oceanroute.core as core


def admitted_workspace(*, empty=False):
    project = {
        "id": "admission-review", "schema_version": 1, "crs": "EPSG:4326",
        "route": {"curve": "geodesic", "mode": "flexible", "slack_basis": "surface", "slack_pct": 0,
                  "points": [{"id": "a", "longitude": 0, "latitude": 0, "depth_m": 50},
                             {"id": "b", "longitude": .01, "latitude": 0, "depth_m": 50}],
                  "legs": [{"cable_type_id": "C"}]},
        "cable_types": [{"id": "C", "cost_per_m": 2, "lay_speed_m_s": 1}],
        "layers": [],
    }
    ws = migrate_project(project)["workspace"]
    if empty:
        ws.update(paths=[], assemblies=[], associations=[], active_path_id=None)
    return ws


def submitted(endpoint, ws):
    payload = {"workspace": ws}
    if endpoint == "check":
        payload["config"] = {"rules": []}
    if endpoint == "import":
        payload["package"] = {"schema": "oceanroute.automatic-rules/v1", "schema_version": 1, "rules": []}
    return payload


@pytest.mark.parametrize("empty", [False, True], ids=["route", "empty-workspace"])
@pytest.mark.parametrize("endpoint", ["catalog", "check", "export", "import"])
@pytest.mark.parametrize("geometry", [
    {"type": "Point", "coordinates": [181, 0]},
    {"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [0, 91]}]},
    {"type": "UnsupportedObject", "coordinates": [0, 0]},
    {"type": "LineString", "coordinates": [[0, 0]]},
], ids=["longitude", "nested-latitude", "unknown-type", "one-point-line"])
def test_unselected_hidden_invalid_gis_rejected_even_without_paths(tmp_path, empty, endpoint, geometry):
    ws = admitted_workspace(empty=empty)
    ws["layers"] = [{"id": "not-selected", "visible": False, "geojson": {
        "type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geometry}]}}]
    original = deepcopy(ws)
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite"))) as client:
        result = client.post("/api/automatic-rules/" + endpoint, json=submitted(endpoint, ws))
        assert result.status_code == 422, result.text
        assert "GeoJSON" in result.json()["detail"]
        assert client.get("/api/workspaces").json() == []
    assert ws == original


def test_old_defaults_run_legacy_calculations_but_new_admission_never_does(tmp_path, monkeypatch):
    ws = admitted_workspace()
    # Use a real ordinary contact as evidence that the default is preserved.
    ws["layers"] = [{"id": "cross", "geojson": {"type": "FeatureCollection", "features": [{
        "id": "line", "type": "Feature", "properties": {}, "geometry": {
            "type": "LineString", "coordinates": [[.005, -.001], [.005, .001]]}}]}}]
    calls = []
    original = core._crossings

    def observed(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "_crossings", observed)
    default = _validate(ws)
    assert len(calls) == 1
    assert default[1][ws["active_path_id"]]["crossings"]
    admitted = _validate(ws, _check_legacy_crossings=False)
    assert len(calls) == 1
    assert admitted[0] == default[0]
    for key in ("rpl", "legs", "summary", "sld", "materials", "route_signature"):
        assert admitted[1][ws["active_path_id"]][key] == default[1][ws["active_path_id"]][key]
    assert admitted[1][ws["active_path_id"]]["crossings"] is None
    assert admitted[1][ws["active_path_id"]]["legacy_crossings_evaluated"] is False
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite"))) as client:
        for endpoint in ("catalog", "check", "export", "import"):
            result = client.post("/api/automatic-rules/" + endpoint, json=submitted(endpoint, ws))
            assert result.status_code == 200, result.text
        assert len(calls) == 1
        result = client.post("/api/workspace/analyze", json=ws)
        assert result.status_code == 200 and len(calls) == 2
        assert result.json()["active_path_analysis"]["crossings"]


def test_private_admission_flag_is_not_a_public_bypass(tmp_path):
    ws = admitted_workspace()
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite"))) as client:
        result = client.post("/api/automatic-rules/check", json={"workspace": ws,
            "config": {"rules": [], "_check_legacy_crossings": False}})
        assert result.status_code == 422
        result = client.post("/api/automatic-rules/check", json={"workspace": ws,
            "_check_legacy_crossings": False})
        assert result.status_code == 422


def test_direct_empty_admission_rejects_bad_layer_but_old_empty_default_is_unchanged():
    ws = admitted_workspace(empty=True)
    ws["layers"] = [{"id": "outside", "geojson": {"type": "Point", "coordinates": [181, 0]}}]
    # Established empty-workspace default did not evaluate route GIS; preserve
    # that old interface rather than silently changing unrelated admission.
    assert _validate(ws)[0] == ws
    with pytest.raises(ValueError, match="GeoJSON"):
        _validate(ws, _check_legacy_crossings=False)
