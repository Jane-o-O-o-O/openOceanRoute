"""Actual atomic source resampling, material relations and durable transactions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.constraints import configure_constraints
from oceanroute.core import analyze_project, route_signature
from oceanroute.storage import ProjectStore
from oceanroute.terrain_sources import example_sources, profile_from_sources
from oceanroute.workspace import analyze_workspace, materialize_path, migrate_project, workspace_action
from oceanroute.workspace_storage import WorkspaceStore, WorkspaceRevisionConflict
from oceanroute.workspace_terrain import preview_workspace_terrain, preview_input_signature, WorkspaceTerrainError


def bottom_workspace(*, alternative=False, independent=False, fixed=False, constrained=False):
    data = example_sources()
    project = data["project"]
    project["route"].update(slack_basis="bottom", slack_pct=2)
    for point in project["route"]["points"]: point["depth_m"] = 55
    project = profile_from_sources(project, {"spacing_m": 200})["project"]
    if fixed:
        total = analyze_project(project)["summary"]["cable_length_m"]
        project["route"].update(mode="fixed", legs=[{"mode": "fixed", "cable_type_id": "example", "fixed_cable_length_m": total}])
    if constrained:
        project = configure_constraints(project, {"mode": "fixed" if fixed else "flexible"})["project"]
    ws = migrate_project(project)["workspace"]
    if alternative or independent:
        ws = workspace_action(ws, {"action": "copy_path", "assembly_policy": "alternative" if alternative else "independent"})["workspace"]
    return ws


def reprioritize(workspace):
    sources = deepcopy(workspace["terrain_sources"])
    next(s for s in sources if s["id"] == "example-background")["priority"] = 1000
    return sources


def assert_failure(result):
    assert result["can_apply"] is False
    assert result["workspace"] is result["project"] is result["analysis"] is None
    assert result["errors"]
    json.dumps(result, allow_nan=False)


def test_actual_bottom_slack_single_atomic_update_changes_manufacture_and_preserves_input():
    ws = bottom_workspace(); before_bytes = json.dumps(ws, sort_keys=True)
    old_analysis = analyze_workspace(ws)
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200})
    assert result["can_apply"] and not result["errors"]
    assert json.dumps(ws, sort_keys=True) == before_bytes
    assert result["paths"][0]["status"] == "success"
    assert result["paths"][0]["quality"]["complete"]
    assert all(s["source_id"] == "example-background" for s in result["paths"][0]["samples"])
    old_total = old_analysis["summary"]["manufactured_total_m"]
    new_total = result["analysis"]["summary"]["manufactured_total_m"]
    assert new_total < old_total-1
    report = result["manufacturing"][0]
    assert report["status"] == "updated" and report["old_total_m"] == pytest.approx(old_total)
    assert report["delta_m"] == pytest.approx(new_total-old_total)
    project = result["project"]; checked = analyze_project(project)
    assert checked["profile_metadata"]["imported_profile_valid"]
    assert checked["summary"]["cable_length_m"] == pytest.approx(checked["summary"]["bottom_length_m"]*1.02)
    assert project["route"]["points"] == materialize_path(ws)["route"]["points"]
    assert result["workspace"]["active_path_id"] == ws["active_path_id"]


def test_shared_alternative_is_resampled_together_and_manufactured_once():
    ws = bottom_workspace(alternative=True)
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200})
    assert result["can_apply"] and len(result["paths"]) == 2
    assert len(result["workspace"]["assemblies"]) == len(result["manufacturing"]) == 1
    assert result["workspace"]["associations"] == ws["associations"]
    assert all(p["status"] == "success" and p["quality"]["complete"] for p in result["paths"])
    assert result["analysis"]["summary"]["manufactured_total_m"] == pytest.approx(result["paths"][0]["after_summary"]["cable_length_m"])
    assert result["workspace"]["assemblies"][0]["id"] == ws["assemblies"][0]["id"]
    assert [i["id"] for i in result["workspace"]["assemblies"][0]["items"]] == [i["id"] for i in ws["assemblies"][0]["items"]]


def test_independent_paths_keep_two_distinct_inventories_with_explicit_delta():
    ws = bottom_workspace(independent=True)
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200})
    assert result["can_apply"] and len(result["manufacturing"]) == 2
    assert all(m["status"] == "updated" for m in result["manufacturing"])
    assert result["analysis"]["summary"]["manufactured_total_m"] == pytest.approx(sum(p["after_summary"]["cable_length_m"] for p in result["paths"]))
    inventories = [{i["id"] for i in a["items"]} for a in result["workspace"]["assemblies"]]
    assert not inventories[0]&inventories[1]


def test_missing_affected_selection_refuses_without_any_sampling():
    ws = bottom_workspace(alternative=True)
    result = preview_workspace_terrain(ws, reprioritize(ws), {"path_ids": [ws["active_path_id"]]})
    assert_failure(result)
    assert result["errors"][0]["code"] == "WORKSPACE_TERRAIN_SELECTION"
    assert result["budget"]["completed_query_points"] == 0
    assert all(not p["samples"] for p in result["paths"])


def test_real_nodata_on_any_path_refuses_entire_candidate_not_waypoint_rescue():
    ws = bottom_workspace(alternative=True); old = deepcopy(ws)
    result = preview_workspace_terrain(ws, [], {"spacing_m": 200})
    assert_failure(result)
    assert ws == old and len(result["paths"]) == 2
    assert all(p["status"] == "error" and p["quality"]["missing_count"] > 0 for p in result["paths"])
    assert all(s["depth_m"] is None for p in result["paths"] for s in p["samples"])
    assert all(e["code"] == "WORKSPACE_TERRAIN_NODATA" for e in result["errors"])


@pytest.mark.parametrize("config,code", [({"max_total_query_points": 1}, "WORKSPACE_TERRAIN_POINT_BUDGET"),
                                        ({"max_total_work_units": 1}, "TERRAIN_WORK_BUDGET")])
def test_actual_global_budgets_stop_sampling_without_partial_application(config, code):
    ws = bottom_workspace(alternative=True)
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200, **config})
    assert_failure(result)
    assert result["errors"][0]["code"] == code
    assert result["budget"]["completed_query_points"] == 0
    assert result["paths"][-1]["status"] == "not_run"


def test_explicit_preserve_manufacturing_rejects_flexible_changes():
    ws = bottom_workspace()
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200, "manufacturing_policy": "preserve"})
    assert_failure(result)
    assert result["paths"][0]["status"] == "success"
    assert result["manufacturing"][0]["error"]["code"] == "WORKSPACE_TERRAIN_MANUFACTURING_PRESERVED"


def test_legal_geometry_stale_bottom_draft_is_reprofiled_before_full_analysis():
    ws = bottom_workspace(); draft = materialize_path(ws)
    draft["route"]["points"][-1]["longitude"] += .003
    assert route_signature(draft) != draft["profile"]["route_signature"]
    with pytest.raises(ValueError, match="底余缆"):
        analyze_project(draft)
    before = deepcopy(ws); draft_before = deepcopy(draft)
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200}, draft={"path_id": draft["id"], "project": draft})
    assert result["can_apply"] and result["draft_report"]["admitted"]
    assert ws == before and draft == draft_before
    assert result["project"]["route"]["points"] == draft["route"]["points"]
    assert result["project"]["profile"]["route_signature"] == route_signature(draft)
    assert result["paths"][0]["after_summary"]["cable_length_m"] > result["paths"][0]["before_summary"]["surface_length_m"]


def test_shared_geometry_draft_with_different_inventory_rejects_no_automatic_fork():
    ws = bottom_workspace(alternative=True); draft = materialize_path(ws)
    draft["route"]["points"][-1]["longitude"] += .003
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200}, draft={"path_id": draft["id"], "project": draft})
    assert_failure(result)
    assert all(p["status"] == "success" for p in result["paths"])
    assert result["manufacturing"][0]["error"]["code"] == "WORKSPACE_TERRAIN_SHARED_DIVERGENCE"
    totals = result["manufacturing"][0]["error"]["path_totals_m"]
    assert len(set(round(t, 3) for t in totals.values())) == 2
    assert len(ws["assemblies"]) == 1


def test_configured_flexible_links_follow_new_actual_manufacturing_with_no_reconfigure():
    ws = bottom_workspace(constrained=True)
    state_before = deepcopy(ws["paths"][0]["project"]["route"]["constraint_state"])
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200})
    assert result["can_apply"], result["errors"]
    route = result["project"]["route"]
    assert route["constraint_state"]["version"] == state_before["version"]
    assert route["path_links"][-1]["cable_kp_m"] == pytest.approx(result["paths"][0]["after_summary"]["cable_length_m"])
    assert result["paths"][0]["constraint_report"]["moved_point_ids"] == []
    assert analyze_project(result["project"])["profile_metadata"]["imported_profile_valid"]


def test_fixed_links_preserve_inventory_and_explicitly_reject_actual_terrain_shortage():
    ws = bottom_workspace(fixed=True, constrained=True)
    total = ws["assemblies"][0]["total_length_m"]
    safe = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200})
    assert safe["can_apply"] and safe["analysis"]["summary"]["manufactured_total_m"] == pytest.approx(total)
    assert safe["project"]["route"]["path_links"] == materialize_path(ws)["route"]["path_links"]
    steep = deepcopy(ws["terrain_sources"])
    background = next(s for s in steep if s["id"] == "example-background")
    background["priority"] = 1000
    background["text"] = "longitude latitude depth_m\n117.97 21.98 100\n118.03 21.98 10000\n117.97 22.02 100\n118.03 22.02 10000"
    for key in ("fingerprint", "content_sha256", "byte_count"): background.pop(key, None)
    rejected = preview_workspace_terrain(ws, steep, {"spacing_m": 200})
    assert_failure(rejected)
    assert rejected["paths"][0]["error"]["code"] == "WORKSPACE_TERRAIN_SHORTAGE"
    assert any(w["code"] == "CABLE_SHORTAGE" for w in rejected["paths"][0]["warnings"])


@pytest.mark.parametrize("mutate,code", [
    (lambda p:p["cable_types"][0].update(cost_per_m=999), "WORKSPACE_TERRAIN_DRAFT_SHARED"),
    (lambda p:p["workspace_context"].update(workspace_revision=9), "WORKSPACE_TERRAIN_DRAFT_REVISION"),
    (lambda p:p["route"].pop("constraint_state"), "WORKSPACE_TERRAIN_DRAFT_CONSTRAINT"),
    (lambda p:p["route"]["points"][-1].update(longitude=118.015), "CONSTRAINT_UNSOLVED_EDIT")])
def test_invalid_shared_revision_or_constraint_draft_is_not_discarded_or_bypassed(mutate, code):
    ws = bottom_workspace(constrained=True); draft = materialize_path(ws)
    mutate(draft)
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200}, draft={"path_id": draft["id"], "project": draft})
    assert_failure(result)
    assert result["errors"][0]["code"] == code


def test_fixed_stock_edit_cannot_be_recaptured_as_new_inventory():
    ws = bottom_workspace(fixed=True); draft = materialize_path(ws)
    draft["route"]["legs"][0]["fixed_cable_length_m"] += 500
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200}, draft={"path_id": draft["id"], "project": draft})
    assert_failure(result)
    assert result["errors"][0]["code"] == "WORKSPACE_TERRAIN_DRAFT_FIXED"


def test_fixed_stock_cannot_be_enlarged_by_allowance_insertion():
    ws = bottom_workspace(fixed=True); draft = materialize_path(ws)
    draft["route"]["allowances"] = [{"kp_m": 50, "length_m": 500, "cable_type_id": "example"}]
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 200}, draft={"path_id": draft["id"], "project": draft})
    assert_failure(result)
    assert result["errors"][0]["code"] == "WORKSPACE_TERRAIN_DRAFT_FIXED"


def test_explicit_datum_conflict_and_outside_draft_are_actual_path_failures():
    ws = bottom_workspace(); sources = reprioritize(ws)
    sources[0]["vertical_datum"] = "different-datum"; sources[0].pop("fingerprint")
    conflict = preview_workspace_terrain(ws, sources, {"spacing_m": 200})
    assert_failure(conflict)
    assert conflict["errors"][0]["code"] == "TERRAIN_DATUM_CONFLICT"
    draft = materialize_path(ws); draft["route"]["points"][-1]["longitude"] = 120
    outside = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 10000}, draft={"path_id": draft["id"], "project": draft})
    assert_failure(outside)
    assert outside["paths"][0]["quality"]["missing_count"] > 0


def test_global_output_preflight_budget_rejects_before_queries():
    ws = bottom_workspace(alternative=True)
    result = preview_workspace_terrain(ws, reprioritize(ws), {"spacing_m": 1, "max_total_output_bytes": 65536})
    assert_failure(result)
    assert result["errors"][0]["code"] == "WORKSPACE_TERRAIN_OUTPUT_BUDGET"
    assert result["budget"]["estimated_query_points"] > 1000 and result["budget"]["completed_query_points"] == 0


def test_no_change_library_can_explicitly_leave_a_path_untouched():
    ws = bottom_workspace(alternative=True)
    result = preview_workspace_terrain(ws, ws["terrain_sources"], {"spacing_m": 200, "path_ids": [ws["active_path_id"]]})
    assert result["can_apply"]
    untouched = next(p for p in result["paths"] if p["path_id"] != ws["active_path_id"])
    assert untouched["status"] == "not_selected" and not untouched["samples"]
    assert result["manufacturing"][0]["status"] == "unchanged"


def test_legacy_null_profile_is_valid_and_not_treated_as_bound_source_metadata():
    ws = bottom_workspace(); p = materialize_path(ws)
    p["profile"] = None
    p["route"]["slack_basis"] = "surface"
    ws = migrate_project(p)["workspace"]
    result = preview_workspace_terrain(ws, reprioritize(ws), {"path_ids": []})
    assert result["can_apply"] and result["paths"][0]["status"] == "not_selected"
    assert result["workspace"]["paths"][0]["project"]["profile"] is None


def test_request_signature_covers_sources_configuration_revision_draft_and_browser_numbers():
    ws = bottom_workspace(); sources = reprioritize(ws)
    a = preview_input_signature(ws, sources, {"spacing_m": 200.0})
    assert a == preview_input_signature(ws, sources, {"spacing_m": 200})
    assert a != preview_input_signature(ws, sources, {"spacing_m": 201})
    assert a != preview_input_signature(ws, ws["terrain_sources"], {"spacing_m": 200})
    changed = deepcopy(ws); changed["saved_revision"] = 1
    assert a != preview_input_signature(changed, sources, {"spacing_m": 200})
    draft = materialize_path(ws)
    assert a != preview_input_signature(ws, sources, {"spacing_m": 200}, {"path_id": draft["id"], "project": draft})


def test_atomic_sqlite_save_competing_candidates_and_complete_restore(tmp_path):
    database = tmp_path/"workspace-terrain.sqlite3"; store = WorkspaceStore(database)
    initial = store.save(bottom_workspace(alternative=True))["workspace"]
    proposed = preview_workspace_terrain(initial, reprioritize(initial), {"spacing_m": 200})
    assert proposed["can_apply"] and proposed["workspace"]["saved_revision"] == 1
    assert store.get(initial["id"]) == initial  # Preview made no database write.
    candidate = proposed["workspace"]
    def save():
        try: return WorkspaceStore(database).save(candidate)["revision"]
        except WorkspaceRevisionConflict: return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _:save(), range(2)))
    assert sorted(str(r) for r in results) == ["2", "conflict"]
    reopened = WorkspaceStore(database).get(initial["id"])
    assert reopened["terrain_sources"] == candidate["terrain_sources"]
    assert reopened["assemblies"] == candidate["assemblies"]
    assert all(p["project"]["profile"] == c["project"]["profile"] for p, c in zip(reopened["paths"], candidate["paths"]))
    restored = WorkspaceStore(database).restore(initial["id"], 1, expected_revision=2)
    assert restored["saved_revision"] == 3 and restored["terrain_sources"] == initial["terrain_sources"]
    assert restored["assemblies"] == initial["assemblies"]
    assert analyze_workspace(restored)["summary"]["manufactured_total_m"] == pytest.approx(analyze_workspace(initial)["summary"]["manufactured_total_m"])


def test_actual_http_preview_then_one_complete_revision_save_and_failure_without_write(tmp_path):
    with TestClient(create_app(ProjectStore(tmp_path/"workspace-terrain-http.sqlite3"))) as client:
        ws = client.post("/api/workspaces", json=bottom_workspace(alternative=True)).json()["workspace"]
        payload = {"workspace": ws, "sources": reprioritize(ws), "config": {"spacing_m": 200}}
        response = client.post("/api/workspace/terrain/preview", json=payload)
        assert response.status_code == 200, response.text
        result = response.json(); assert result["can_apply"]
        assert client.get("/api/workspaces/"+ws["id"]).json() == ws
        saved = client.post("/api/workspaces", json=result["workspace"])
        assert saved.status_code == 200 and saved.json()["revision"] == 2
        broken = client.post("/api/workspace/terrain/preview", json={"workspace": saved.json()["workspace"], "sources": []})
        assert broken.status_code == 200 and not broken.json()["can_apply"]
        assert client.get("/api/workspaces/"+ws["id"]).json() == saved.json()["workspace"]
        bad = client.post("/api/workspace/terrain/preview", json={**payload, "config": {"eval": "anything"}})
        assert bad.status_code == 422


@pytest.mark.parametrize("config", [{"path_ids": ["foreign"]}, {"path_ids": ["x", "x"]},
                                  {"manufacturing_policy": "fork"}, {"max_total_query_points": True},
                                  {"max_total_work_units": 200000001}, {"max_total_output_bytes": 1}, []])
def test_bad_config_is_explicit_not_silently_ignored(config):
    ws = bottom_workspace()
    with pytest.raises((WorkspaceTerrainError, ValueError)):
        preview_workspace_terrain(ws, reprioritize(ws), config)
