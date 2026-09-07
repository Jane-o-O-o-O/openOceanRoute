"""Local explicit-material initial state API needs no geographic route."""
from pathlib import Path
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.storage import ProjectStore


EXAMPLES = Path(__file__).resolve().parents[1]/"examples"


@pytest.mark.parametrize("filename", ["initial-equilibrium-dynamic.json", "heterogeneous-initial-dynamic.json"])
def test_actual_local_example_request_can_prepare_then_run_without_a_geographic_project(tmp_path, filename):
    payload = json.loads((EXAMPLES/filename).read_text())
    assert not payload.get("project")  # missing or explicitly empty
    with TestClient(create_app(ProjectStore(tmp_path/"local-api.sqlite3"))) as client:
        response = client.post("/api/simulation/prepare-equilibrium-initial", json=payload)
        assert response.status_code == 200, response.text
        prepared = response.json()
        assert prepared["solver"]["accepted"]
        dynamic = client.post("/api/simulation/dynamic", json=payload)
        assert dynamic.status_code == 200, dynamic.text
        actual = dynamic.json()
        assert np.asarray(actual["frames"][0]["nodes"]) == pytest.approx(np.asarray(prepared["positions"]), abs=1e-8)
        assert actual["initialization"]["schema"] == prepared["provenance"]["schema"]
        assert actual["frames"][0]["paid_out_m"] == 0.


@pytest.mark.parametrize("project", [None, [], "", 1, False, {"id": "missing-route"}])
def test_optional_project_does_not_accept_wrong_types_or_incomplete_nonempty_geographic_project(tmp_path, project):
    payload = json.loads((EXAMPLES/"heterogeneous-initial-dynamic.json").read_text())
    payload["project"] = project
    with TestClient(create_app(ProjectStore(tmp_path/"invalid-local-api.sqlite3"))) as client:
        response = client.post("/api/simulation/prepare-equilibrium-initial", json=payload)
        assert response.status_code == 422, response.text
        assert "provenance" not in response.json()
