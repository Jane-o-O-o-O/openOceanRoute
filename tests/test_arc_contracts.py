"""Public signatures, intrinsic budgets and actual CSV HTTP admission."""
from copy import deepcopy
from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.automatic_rules import check_automatic_rules
from oceanroute.core import analyze_project, route_signature
from oceanroute.exchange import export_csv
from oceanroute.storage import ProjectStore
from oceanroute.workspace import migrate_project
from test_arc_rpl_exchange import project


def check_project(p):
    ws=migrate_project(p)["workspace"]
    return check_automatic_rules(ws)


def test_same_endpoint_opposite_semicircles_have_distinct_geometry_signatures():
    p=project(180.); q=deepcopy(p)
    q["route"]["legs"][0]["geometry"]["sweep_deg"]=-180.
    assert route_signature(p)!=route_signature(q)
    a,b=check_project(p),check_project(q)
    assert a["metadata"]["geometry_signature"]!=b["metadata"]["geometry_signature"]
    assert a["metadata"]["input_signature"]!=b["metadata"]["input_signature"]


def test_arc_render_tolerance_is_reported_as_a_sampled_criterion():
    a=analyze_project(project())
    evidence=a["route_geometry_render"]
    assert evidence["render_tolerance_m"]==1
    assert evidence["maximum_sampled_chord_error_m"]<=1
    assert "quarter/mid/three-quarter checks" in evidence["tolerance_basis"]
    assert "not a certified continuous Hausdorff bound" in evidence["tolerance_basis"]
    assert len(a["rpl"])==2
    assert len(a["route_geometry"]["coordinates"])>2


def test_real_csv_http_export_import_has_a_positive_arc_and_never_writes_sqlite(tmp_path):
    app=create_app(ProjectStore(tmp_path/"arc-csv.sqlite3"))
    with TestClient(app) as client:
        p=project(360.)
        response=client.post("/api/export/csv",json=p)
        assert response.status_code==200,response.text
        restored=client.post("/api/import/rpl",json={"text":response.text,"error_policy":"reject"})
        assert restored.status_code==200,restored.text
        result=restored.json()
        assert result["can_apply"] and result["legs"][0]["geometry"]["sweep_deg"]==360
        assert result["records"][-1]["computed_route_kp_m"]>3000
        assert client.get("/api/projects").json()==[]
        assert client.get("/api/workspaces").json()==[]
