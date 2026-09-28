"""Complete inventory transactions and actual local HTTP, without mocked geometry."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from oceanroute.arc_edit_workspace import preview_arc_edit_workspace
from oceanroute.api import create_app
from oceanroute.storage import ProjectStore
from oceanroute.workspace import WorkspaceError, migrate_project, materialize_path, workspace_action
from test_arc_edit import arc_project, target, configured_marker


@pytest.mark.parametrize("fixed",[True,False])
def test_exclusive_candidate_inventory_identity_and_shared_libraries(fixed):
    ws=migrate_project(arc_project(fixed=fixed))["workspace"]
    ws["operator_metadata"]={"keep":"same"};old=deepcopy(ws)
    pid=ws["active_path_id"]
    result=preview_arc_edit_workspace(ws,pid,{"moves":[target(materialize_path(ws))]})
    candidate=result["workspace"]
    assert ws==old and candidate["id"]==ws["id"]
    assert candidate["operator_metadata"]==ws["operator_metadata"]
    assert candidate["associations"]==ws["associations"]
    for field in ("cable_types","layers","terrain_sources"):
        assert candidate[field]==ws[field]
    assert result["operation"]["kind"]=="arc_edit"
    assert result["operation"]["config"]==result["tool_report"]["config"]
    assert result["result_selection_point_id"]=="p1"
    if fixed:
        assert candidate["assemblies"]==ws["assemblies"]
    else:
        assert candidate["assemblies"][0]["id"]==ws["assemblies"][0]["id"]
        assert result["tool_report"]["manufacturing"]["physical_delta_m"]!=0.
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize("fixed",[True,False])
def test_shared_fixed_permitted_and_flexible_quantity_change_typed_rejected(fixed):
    ws=migrate_project(arc_project(fixed=fixed))["workspace"]
    ws=workspace_action(ws,{"action":"copy_path","assembly_policy":"alternative"})["workspace"]
    old=deepcopy(ws);pid=ws["active_path_id"]
    config={"moves":[target(materialize_path(ws))]}
    if fixed:
        result=preview_arc_edit_workspace(ws,pid,config)
        assert result["workspace"]["assemblies"]==ws["assemblies"]
        assert result["workspace"]["associations"]==ws["associations"]
        assert len(result["workspace"]["paths"])==2
    else:
        with pytest.raises(WorkspaceError,match="WORKSPACE_SHARED_ASSEMBLY_CHANGED"):
            preview_arc_edit_workspace(ws,pid,config)
    assert ws==old


def test_real_http_preview_one_apply_explicit_save_reopen_and_stale_revision(tmp_path):
    db=tmp_path/"arc-editor.sqlite3"
    with TestClient(create_app(ProjectStore(db))) as client:
        ws=migrate_project(configured_marker())["workspace"]
        first=client.post("/api/workspaces",json=ws)
        assert first.status_code==200,first.text
        saved=first.json()["workspace"];pid=saved["active_path_id"]
        payload={"workspace":saved,"path_id":pid,"config":{"moves":[target(materialize_path(saved))]}}
        preview=client.post("/api/workspace/arc-edit-preview",json=payload)
        assert preview.status_code==200,preview.text
        result=preview.json()
        assert client.get(f"/api/workspaces/{ws['id']}").json()==saved
        assert len(client.get(f"/api/workspaces/{ws['id']}/revisions").json())==1
        path_project=next(x["project"] for x in result["workspace"]["paths"] if x["id"]==pid)
        applied=client.post("/api/workspace/action",json={"workspace":saved,"config":{"action":"update_path","path_id":pid,"project":path_project,"assembly_policy":"auto_exclusive"}})
        assert applied.status_code==200,applied.text
        assert applied.json()["workspace"]==result["workspace"]
        second=client.post("/api/workspaces",json=applied.json()["workspace"])
        assert second.status_code==200,second.text
        assert second.json()["revision"]==2
        stale=client.post("/api/workspaces",json=result["workspace"])
        assert stale.status_code==422
        expected=client.get(f"/api/workspaces/{ws['id']}").json()
        direct=client.post("/api/tools/arc-edit",json={"project":materialize_path(saved),"config":payload["config"]})
        assert direct.status_code==200,direct.text
        assert direct.json()["project"]["route"]==result["project"]["route"]
    with TestClient(create_app(ProjectStore(db))) as reopened:
        actual=reopened.get(f"/api/workspaces/{ws['id']}").json()
        assert actual==expected
        assert actual["paths"][0]["project"]["route"]["constraint_state"]["manufacturing"]==saved["paths"][0]["project"]["route"]["constraint_state"]["manufacturing"]


@pytest.mark.parametrize("payload_kind",["extra","badref","budget","unreachable","duplicate","branch_array","policy_object"])
def test_http_rejected_complete_preview_does_not_write(tmp_path,payload_kind):
    with TestClient(create_app(ProjectStore(tmp_path/"arc.sqlite3"))) as client:
        ws=migrate_project(arc_project())["workspace"]
        saved=client.post("/api/workspaces",json=ws).json()["workspace"]
        config={"moves":[target(materialize_path(saved))]}
        payload={"workspace":saved,"path_id":saved["active_path_id"],"config":config}
        if payload_kind=="extra":payload["apply"]=True
        elif payload_kind=="badref":payload["path_id"]="missing"
        elif payload_kind=="budget":config["max_work_units"]=1
        elif payload_kind=="unreachable":config["moves"]=[target(materialize_path(saved),east=2000.)]
        elif payload_kind=="duplicate":config["moves"]*=2
        elif payload_kind=="branch_array":config["arc_options"]=[{"start_point_id":"p0","end_point_id":"p1","branch":[]}]
        else:config["arc_options"]=[{"start_point_id":"p0","end_point_id":"p1","full_circle_policy":{}}]
        response=client.post("/api/workspace/arc-edit-preview",json=payload)
        assert response.status_code==422,response.text
        if payload_kind in {"badref","budget","unreachable","duplicate"}:
            assert response.json()["code"].startswith("ARC_EDIT_")
        assert client.get(f"/api/workspaces/{ws['id']}").json()==saved


def test_actual_export_headers_version_unicode_scope_and_unchanged_csv_geometry(tmp_path):
    with TestClient(create_app(ProjectStore(tmp_path/"names.sqlite3"))) as client:
        p=arc_project();p["name"]='海缆/\r\n甲';ws=migrate_project(p)["workspace"]
        for kind in ("csv","kml","geojson","dxf","sld","report","project","assembly"):
            response=client.post(f"/api/export/{kind}",json=p)
            assert response.status_code==200,response.text
            header=response.headers["Content-Disposition"]
            assert header.startswith('attachment; filename="') and "filename*=UTF-8''" in header
            assert "0.12" in header and "\r" not in header and "\n" not in header
        response=client.post("/api/workspace/export",json=ws)
        assert response.status_code==200
        assert "workspace" in response.headers["Content-Disposition"]
        assert json.loads(response.text)==ws
        rules=client.post("/api/automatic-rules/export",json={"workspace":ws})
        assert rules.status_code==200,rules.text
        assert rules.json()["filename"].endswith(".json")
