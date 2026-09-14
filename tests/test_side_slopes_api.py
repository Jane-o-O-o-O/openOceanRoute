"""Actual HTTP candidates, source binding and persistent workspace ownership."""
from copy import deepcopy
import json
import math

from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.core import analyze_project
from oceanroute.storage import ProjectStore
from oceanroute.workspace import materialize_path


def project():
    return {"schema_version": 1, "id": "side-api-case", "name": "Explicit synthetic side-slope API case",
            "crs": "EPSG:4326", "route": {"id": "r", "curve": "geodesic", "mode": "flexible",
                "slack_pct": 1, "slack_basis": "surface", "points": [
                    {"id": "a", "longitude": 118, "latitude": 22, "depth_m": 1000},
                    {"id": "b", "longitude": 118, "latitude": 22.01, "depth_m": 1000}],
                "allowances": [{"id": "reserve", "kp_m": 200, "length_m": 40, "cable_type_id": "C"}]},
            "profile": {"source": "explicit_synthetic_waypoint_depths", "samples": []},
            "cable_types": [{"id": "C", "name": "Explicit synthetic cable", "cost_per_m": 15, "lay_speed_m_s": 1,
                             "wet_weight_n_m": 10, "ea_n": 1000000}],
            "bodies": [], "layers": [], "terrain_sources": [], "costs": {"currency": "CNY"},
            "user_extension": {"preserve": ["independent route", "actual extra inventory"]}}


def post(client, url, payload):
    response = client.post(url, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def input_example(client, original):
    example = post(client, "/api/terrain/side-slopes/example", {"project": original})
    candidate = deepcopy(original)
    candidate["terrain_sources"] = example["sources"]
    return example, candidate


def side_preview(client, original):
    example, candidate = input_example(client, original)
    return post(client, "/api/terrain/side-slopes", {"project": candidate, "config": example["config"]})


def side_rule(limit=5, **kwargs):
    return {"id": "side-limit", "name": "Declared sampled side-slope limit", "enabled": True,
            "kind": "side", "start_kp_m": 0, "end_kp_m": None, "max_side_slope_deg": limit, **kwargs}


def test_real_example_input_and_actual_http_result_have_independent_signed_plane_values(tmp_path):
    original = project()
    before = deepcopy(original)
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        example, source_project = input_example(client, original)
        assert "project" not in example and "side_slopes" not in example
        assert len(example["sources"]) == 1
        assert len(example["sources"][0]["text"].strip().splitlines()) == 10
        result = post(client, "/api/terrain/side-slopes", {"project": source_project, "config": example["config"]})
        first = result["side_slopes"]["samples"][0]
        # First station is the declared AEQD origin. North-going route has
        # starboard east; the source is D=1500-.2*x there, independently known.
        assert [row["depth_m"] for row in first["transect"]] == pytest.approx([1520, 1510, 1500, 1490, 1480], abs=1e-7)
        assert first["side_slope_deg"] == pytest.approx(math.degrees(math.atan(.2)), abs=1e-8)
        assert first["max_sampled_abs_slope_deg"] == pytest.approx(math.degrees(math.atan(.2)), abs=1e-8)
        assert all(row["source_id"] == example["sources"][0]["id"] for row in first["transect"])
        assert result["side_slopes"]["metadata"]["vertical_datum"] == "synthetic-side-slopes-datum"
        for key in ("route", "profile", "bodies", "cable_types", "user_extension"):
            assert result["project"][key] == original[key]
        assert client.get("/api/workspaces").json() == []
        assert client.get("/api/projects").json() == []
        json.dumps(result, allow_nan=False)
    assert original == before


def test_rule_http_candidate_changes_only_rules_and_detects_flat_along_steep_across(tmp_path):
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        candidate = side_preview(client, project())["project"]
        before = deepcopy(candidate)
        result = post(client, "/api/tools/slope-rules", {"project": candidate, "config": {"rules": [side_rule()]}})
        assert result["results"][0]["status"] == "violations"
        assert result["summary"]["violation_count"] > 0
        assert all(row["kind"] == "side" and row["value_deg"] > row["limit_deg"] for row in result["results"][0]["violations"])
        assert result["rules"][0]["end_kp_m"] is None
        unchanged = deepcopy(result["project"])
        unchanged.pop("slope_rules")
        assert unchanged == before and candidate == before
        analysis = post(client, "/api/analyze", result["project"])
        assert analysis["legs"][0]["max_slope_deg"] == 0
        assert analysis["slope_rule_checks"]["results"][0]["status"] == "violations"
        assert analysis["side_slopes_metadata"]["source_binding_current"]
        assert any(row["code"] == "SLOPE_RULE_VIOLATION" for row in analysis["warnings"])


def test_absent_or_stale_side_data_never_pass_and_rule_range_remains_dynamic(tmp_path):
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        missing = post(client, "/api/tools/slope-rules", {"project": project(), "config": {"rules": [side_rule()]}})
        assert missing["results"][0]["status"] == "unknown"
        current = side_preview(client, project())["project"]
        current["slope_rules"] = [side_rule(30)]
        actual = post(client, "/api/tools/slope-rules", {"project": current})
        assert actual["results"][0]["status"] == "sampled_pass"
        for change in ("geometry", "sources"):
            stale = deepcopy(current)
            if change == "geometry":
                stale["route"]["points"][-1]["latitude"] += .002
            else:
                stale["terrain_sources"][0]["priority"] += 1
            result = post(client, "/api/tools/slope-rules", {"project": stale})
            assert result["results"][0]["status"] == "unknown"
            assert result["rules"][0]["end_kp_m"] is None
            analysis = post(client, "/api/analyze", stale)
            assert not analysis["side_slopes_metadata"]["source_binding_current"]
            assert any(row["code"] == "SIDE_SLOPES_STALE" for row in analysis["warnings"])
            if change == "geometry":
                assert result["results"][0]["end_kp_m"] > actual["results"][0]["end_kp_m"]


def test_nullable_sampling_end_and_shortened_dynamic_rule_remain_editable_over_http(tmp_path):
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        example, candidate = input_example(client, project())
        config = {key: value for key, value in example["config"].items() if key != "end_kp_m"}
        omitted = post(client, "/api/terrain/side-slopes", {"project": candidate, "config": config})
        explicit_null = post(client, "/api/terrain/side-slopes", {
            "project": candidate, "config": {**config, "end_kp_m": None}})
        assert explicit_null["side_slopes"] == omitted["side_slopes"]
        assert explicit_null["project"] == omitted["project"]
        current = explicit_null["project"]
        current["slope_rules"] = [side_rule(30, start_kp_m=500)]
        # Keep the independently declared 200 m allowance inside the route;
        # only this rule's 500 m start lies past the new 332 m route end.
        current["route"]["points"][-1]["latitude"] = 22.003
        analysis = post(client, "/api/analyze", current)
        rule = analysis["slope_rule_checks"]["results"][0]
        assert rule["status"] == "unknown"
        assert rule["end_kp_m"] < rule["start_kp_m"]
        assert any(row["code"] == "RULE_RANGE_EMPTY" for row in rule["diagnostics"])
        assert any(row["code"] == "SLOPE_RULE_UNAVAILABLE" for row in analysis["warnings"])
        assert current["slope_rules"][0]["end_kp_m"] is None


def test_full_workspace_save_close_reopen_restore_and_shared_inventory_are_real(tmp_path):
    path = tmp_path / "state.sqlite3"
    with TestClient(create_app(ProjectStore(path))) as client:
        original = project()
        original["workspace_custom"] = {"preserve": True}
        migrated = post(client, "/api/workspace/migrate", {"project": original})
        workspace = migrated["workspace"]
        workspace["user_workspace_extension"] = {"opaque": [1, 2, 3]}
        saved = post(client, "/api/workspaces", workspace)["workspace"]
        inventory = deepcopy(saved["assemblies"])
        associations = deepcopy(saved["associations"])
        summary = migrated["analysis"]["summary"]
        previews = side_preview(client, materialize_path(saved))
        assert client.get("/api/workspaces/" + saved["id"]).json() == saved
        updated = post(client, "/api/workspace/action", {"workspace": saved, "config": {
            "action": "update_path", "path_id": saved["active_path_id"], "project": previews["project"], "update_shared": True}})
        assert updated["workspace"]["assemblies"] == inventory
        assert updated["workspace"]["associations"] == associations
        assert updated["analysis"]["summary"]["manufactured_total_m"] == summary["manufactured_total_m"]
        assert updated["analysis"]["summary"]["procurement_cost"] == summary["procurement_cost"]
        checked = post(client, "/api/tools/slope-rules", {"project": updated["project"], "config": {"rules": [side_rule()]}})
        applied = post(client, "/api/workspace/action", {"workspace": updated["workspace"], "config": {
            "action": "update_path", "path_id": saved["active_path_id"], "project": checked["project"]}})
        copied = post(client, "/api/workspace/action", {"workspace": applied["workspace"], "config": {
            "action": "copy_path", "path_id": saved["active_path_id"], "assembly_policy": "alternative", "name": "Linked alternate"}})
        final = post(client, "/api/workspaces", copied["workspace"])["workspace"]
        assert final["saved_revision"] == 2 and len(final["paths"]) == 2
        assert final["assemblies"] == inventory
        assert all(item["project"]["side_slopes"] == previews["side_slopes"] for item in final["paths"])
        changed_sources = deepcopy(final["terrain_sources"])
        changed_sources[0]["priority"] += 1
        changed = post(client, "/api/workspace/action", {"workspace": final, "config": {
            "action": "update_shared", "terrain_sources": changed_sources}})
        stale_paths = {row["path_id"] for row in changed["warnings"] if row["code"] == "SIDE_SLOPES_STALE"}
        assert stale_paths == {row["id"] for row in final["paths"]}
        assert changed["workspace"]["assemblies"] == inventory
        assert client.get("/api/workspaces/" + final["id"]).json() == final
    # The storage owner and ASGI lifespan really close, then a fresh owner reads.
    with TestClient(create_app(ProjectStore(path))) as reopened:
        loaded = reopened.get("/api/workspaces/" + final["id"]).json()
        assert loaded == final
        assert loaded["user_workspace_extension"] == {"opaque": [1, 2, 3]}
        restored = post(reopened, f"/api/workspaces/{final['id']}/restore/1", {"expected_revision": 2})
        assert restored["saved_revision"] == 3 and len(restored["paths"]) == 1
        assert "side_slopes" not in restored["paths"][0]["project"]
        assert restored["assemblies"] == inventory
        assert reopened.get("/api/workspaces/" + final["id"]).json() == restored


@pytest.mark.parametrize("url,payload", [
    ("/api/terrain/side-slopes", {"config": {}}),
    ("/api/terrain/side-slopes", {"project": project(), "sources": []}),
    ("/api/terrain/side-slopes", {"project": project(), "config": {"half_width_m": True}}),
    ("/api/terrain/side-slopes", {"project": project(), "config": {"max_query_points": 2}}),
    ("/api/terrain/side-slopes", {"project": project(), "config": {"max_output_bytes": 1024}}),
    ("/api/tools/slope-rules", {"project": project(), "config": {"rules": [side_rule(-1)]}}),
    ("/api/tools/slope-rules", {"project": project(), "config": {"rules": [side_rule(), side_rule()]}}),
    ("/api/tools/slope-rules", {"project": project(), "config": {"rules": [side_rule(kind="unsupported")]}}),
    ("/api/tools/slope-rules", {"project": project(), "config": {"rules": [side_rule(max_side_slope_deg=90)]}}),
    ("/api/tools/slope-rules", {"project": project(), "config": {"rules": [side_rule()], "max_work_units": 1}}),
    ("/api/terrain/side-slopes/example", {"project": project(), "config": {}}),
])
def test_invalid_http_inputs_are_atomic_422(tmp_path, url, payload):
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        response = client.post(url, json=payload)
        assert response.status_code == 422, response.text
        assert client.get("/api/workspaces").json() == []
        assert client.get("/api/projects").json() == []


def test_legacy_analysis_contract_is_unchanged_when_new_fields_absent():
    result = analyze_project(project())
    assert "side_slopes_metadata" not in result and "slope_rule_checks" not in result
