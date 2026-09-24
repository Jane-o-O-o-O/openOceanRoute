"""Full candidate isolation, inventory policy and real HTTP persistence."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from oceanroute.altercourse_workspace import preview_altercourse_workspace
from oceanroute.api import create_app
from oceanroute.core import route_signature
from oceanroute.geodesy import GEOD
from oceanroute.storage import ProjectStore
from oceanroute.workspace import (WorkspaceError, materialize_path, migrate_project,
                                 workspace_action)


def workspace(fixed=False):
    a = GEOD.fwd(0, 0, 270, 2500)[:2]
    c = GEOD.fwd(0, 0, 0, 2500)[:2]
    p = {"id":"ac-workspace-source", "name":"转角完整工作区", "crs":"EPSG:4326",
         "route":{"curve":"rhumb", "mode":"fixed" if fixed else "flexible",
                  "slack_basis":"surface", "slack_pct":1,
                  "points":[{"id":"a", "longitude":a[0], "latitude":a[1], "depth_m":50},
                            {"id":"b", "longitude":0, "latitude":0, "depth_m":50},
                            {"id":"c", "longitude":c[0], "latitude":c[1], "depth_m":50}],
                  "legs":[{"cable_type_id":"A", "fixed_cable_length_m":2600 if fixed else None},
                          {"cable_type_id":"A", "fixed_cable_length_m":2700 if fixed else None}]},
         "cable_types":[{"id":"A", "name":"A", "cost_per_m":2, "lay_speed_m_s":1}],
         "bodies":[], "costs":{"currency":"CNY"}}
    p["profile"] = {"route_signature":route_signature(p), "source":"synthetic-test-not-field",
                    "measured":False, "samples":[{"kp_m":0,"depth_m":50}, {"kp_m":5000,"depth_m":50}]}
    ws = migrate_project(p)["workspace"]
    ws["operator_metadata"] = {"must_survive":[1,"same"]}
    return ws


def configuration(kind):
    return {"point_id":"b", **({"max_turn_angle_deg":31,"min_turn_distance_m":120}
                              if kind == "split" else {"radius_m":300})}


@pytest.mark.parametrize("kind", ["split", "radius"])
@pytest.mark.parametrize("fixed", [False, True])
def test_complete_candidate_preserves_identity_and_normal_inventory_policy(kind, fixed):
    ws = workspace(fixed); original = deepcopy(ws)
    pid = ws["active_path_id"]
    result = preview_altercourse_workspace(ws, pid, kind, configuration(kind))
    candidate = result["workspace"]
    assert ws == original
    assert candidate["id"] == ws["id"] and candidate["active_path_id"] == pid
    assert candidate["operator_metadata"] == ws["operator_metadata"]
    assert candidate["cable_types"] == ws["cable_types"]
    assert candidate["layers"] == ws["layers"]
    assert candidate["terrain_sources"] == ws["terrain_sources"]
    assert len(candidate["assemblies"]) == len(ws["assemblies"]) == 1
    assert candidate["associations"] == ws["associations"]
    assert result["result_selection_point_id"] == "b"
    assert result["operation"]["config"] == result["tool_report"]["config"]
    assert result["analysis"]["active_path_analysis"]["route_signature"] != route_signature(materialize_path(ws))
    assert not result["analysis"]["active_path_analysis"]["profile_metadata"]["imported_profile_valid"]
    if fixed:
        assert result["tool_report"]["manufacturing"]["physical_delta_m"] == pytest.approx(0, abs=1e-7)
        assert candidate["assemblies"] == ws["assemblies"]
    else:
        assert result["tool_report"]["manufacturing"]["physical_delta_m"] < 0
        assert candidate["assemblies"][0]["id"] == ws["assemblies"][0]["id"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("fixed", [False, True])
def test_shared_inventory_has_no_silent_fork_or_mode_conversion(fixed):
    ws = workspace(fixed)
    ws = workspace_action(ws, {"action":"copy_path", "assembly_policy":"alternative"})["workspace"]
    pid = ws["active_path_id"]; original = deepcopy(ws)
    if fixed:
        result = preview_altercourse_workspace(ws, pid, "radius", configuration("radius"))
        assert result["workspace"]["assemblies"] == ws["assemblies"]
        assert result["workspace"]["associations"] == ws["associations"]
    else:
        with pytest.raises(WorkspaceError, match="WORKSPACE_SHARED_ASSEMBLY"):
            preview_altercourse_workspace(ws, pid, "radius", configuration("radius"))
    assert ws == original


@pytest.mark.parametrize("kind", ["split", "radius"])
def test_actual_http_preview_apply_save_and_reopen_are_separate(tmp_path, kind):
    store = ProjectStore(tmp_path/"altercourse.sqlite3")
    app = create_app(store)
    with TestClient(app) as client:
        ws = workspace(True)
        saved = client.post("/api/workspaces", json=ws).json()["workspace"]
        pid = saved["active_path_id"]
        preview = client.post("/api/workspace/altercourse-preview", json={"workspace":saved,"path_id":pid,"kind":kind,"config":configuration(kind)})
        assert preview.status_code == 200, preview.text
        assert len(client.get(f"/api/workspaces/{ws['id']}/revisions").json()) == 1
        result = preview.json()
        path_project = next(p["project"] for p in result["workspace"]["paths"] if p["id"] == pid)
        applied = client.post("/api/workspace/action", json={"workspace":saved,"config":{"action":"update_path","path_id":pid,"project":path_project,"assembly_policy":"auto_exclusive"}})
        assert applied.status_code == 200, applied.text
        assert len(client.get(f"/api/workspaces/{ws['id']}/revisions").json()) == 1
        second = client.post("/api/workspaces", json=applied.json()["workspace"])
        assert second.status_code == 200, second.text
        assert second.json()["revision"] == 2
        restored = client.get(f"/api/workspaces/{ws['id']}").json()
        assert restored["paths"][0]["project"]["route"] == result["workspace"]["paths"][0]["project"]["route"]
        direct = client.post(f"/api/tools/{kind}-altercourse", json={"project":materialize_path(saved),"config":configuration(kind)})
        assert direct.status_code == 200, direct.text
        invalid = client.post("/api/workspace/altercourse-preview", json={"workspace":saved,"path_id":pid,"kind":kind,"config":configuration(kind),"update_shared":True})
        assert invalid.status_code == 422
