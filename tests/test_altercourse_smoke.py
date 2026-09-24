"""Actual adapters, lifecycle and fail-closed attachment checks for the harness."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.storage import ProjectStore
from scripts.altercourse_smoke import run_altercourse_smoke


def execute(tmp_path, *, corrupt_csv=False):
    database = tmp_path / "actual-altercourse-smoke.sqlite3"
    client = TestClient(create_app(ProjectStore(database)))
    client.__enter__()
    reads = 0
    actual_calls = []
    owners_created = 1
    owner_reopened = False

    def post(path, payload):
        response = client.post(path, json=payload)
        actual_calls.append(("POST", path, response.status_code))
        if response.status_code == 422:
            return {"_http_status": 422, "_error": response.json()}
        assert response.status_code == 200, response.text
        if path == "/api/export/csv":
            text = response.text
            if corrupt_csv:
                text = text.replace("leg_geometry_json", "discarded_intrinsic_geometry")
            return {"_http_status": response.status_code,
                    "_content_type": response.headers["content-type"], "_text": text}
        return response.json()

    def get(path):
        nonlocal client, reads, owners_created, owner_reopened
        reads += 1
        if reads == 2:
            client.__exit__(None, None, None)
            client = TestClient(create_app(ProjectStore(database)))
            client.__enter__()
            owners_created += 1
            owner_reopened = True
        response = client.get(path)
        actual_calls.append(("GET", path, response.status_code))
        assert response.status_code == 200, response.text
        return response.json()

    try:
        result = run_altercourse_smoke(post, get)
    finally:
        client.__exit__(None, None, None)
    return result, actual_calls, owner_reopened, owners_created


def test_actual_http_workflow_and_actual_owner_reopen_are_separately_evidenced(tmp_path):
    result, calls, restarted, owners = execute(tmp_path)
    assert result["status"] == "passed" and result["synthetic"] is True
    assert result["actual_api_requests"] == len(calls) == 18
    assert restarted is True and owners == 2
    assert result["storage_owner_close_verified_by_harness"] is False
    assert result["second_get_is_post_save_reread"] is True
    assert result["saved_revision"] == 2
    assert result["intrinsic_arc"]["actual_rendered_circle_points"] >= 5
    assert result["intrinsic_arc"]["surface_length_m"] > result["intrinsic_arc"]["chord_length_m"]
    assert result["fixed_physical_stock_m"] == 5300.
    assert result["shared_fixed_inventory_and_associations_unchanged"] is True
    assert result["csv"]["arc_descriptor_equal"] is True
    assert result["shared_flexible_rejection"]["http_status"] == 422
    assert result["shared_flexible_rejection"]["error"]["code"] == "WORKSPACE_SHARED_ASSEMBLY_CHANGED"
    assert result["source_project_unchanged"] is True
    json.dumps(result, allow_nan=False)


def test_actual_csv_attachment_losing_intrinsic_geometry_cannot_be_claimed_as_a_pass(tmp_path):
    with pytest.raises(AssertionError, match="authoritative arc"):
        execute(tmp_path, corrupt_csv=True)


def test_shipped_harness_imports_only_standard_library_not_checkout_production_oracles():
    path = Path(__file__).resolve().parents[1] / "scripts" / "altercourse_smoke.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {node.module.split(".")[0] for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module}
    modules |= {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    assert modules <= {"__future__", "copy", "csv", "hashlib", "io", "json", "math", "time", "urllib", "uuid"}
