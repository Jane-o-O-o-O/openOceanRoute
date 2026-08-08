from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import math
import sqlite3
import threading

import pytest

from oceanroute.core import analyze_project, route_signature
from oceanroute.constraints import configure_constraints, edit_constrained_project
from oceanroute.geodesy import WGS84_A
from oceanroute.storage import ProjectStore
from oceanroute.workspace import (WorkspaceError, analyze_workspace, export_workspace, import_workspace,
                                 materialize_path, migrate_project, validate_workspace, workspace_action)
from oceanroute.workspace_storage import WorkspaceRevisionConflict, WorkspaceStore


def project(fixed=False):
    p = {"id": "old-project", "name": "主海缆", "schema_version": 1, "saved_revision": 3, "crs": "EPSG:4326",
        "route": {"id": "old-route", "curve": "rhumb", "mode": "fixed" if fixed else "flexible", "slack_basis": "surface", "slack_pct": 1,
            "points": [{"id": "start", "longitude": 0, "latitude": 0, "depth_m": 20},
                       {"id": "end", "longitude": math.degrees(1000/WGS84_A), "latitude": 0, "depth_m": 20}],
            "legs": [{"cable_type_id": "A", "fixed_cable_length_m": 1010 if fixed else None, "burial": True}],
            "allowances": [{"kp_m": 500, "length_m": 20}]},
        "cable_types": [{"id": "A", "name": "A缆", "cost_per_m": 2, "lay_speed_m_s": 1},
                        {"id": "B", "name": "B缆", "cost_per_m": 3, "lay_speed_m_s": 1}],
        "bodies": [{"id": "repeater", "name": "有限体", "cable_kp_m": 250, "length_m": 2, "cost": 50},
                   {"id": "joint", "cable_kp_m": 700, "length_m": 0, "cost": 10}],
        "assembly_references": [{"id": "reference", "cable_kp_m": 300, "name": "制造参考"}],
        "layers": [{"id": "coast", "name": "共享海岸资料", "kind": "reference", "visible": True,
                    "geojson": {"type": "FeatureCollection", "features": []}}],
        "costs": {"currency": "CNY", "vessel_day_rate": 86400, "burial_per_m": 1, "contingency_pct": 10}}
    p["profile"] = {"samples": [{"kp_m": 0, "depth_m": 20}, {"kp_m": 1000, "depth_m": 20}], "route_signature": route_signature(p), "source": "survey", "measured": True}
    return p


def workspace(fixed=False):
    return migrate_project(project(fixed))["workspace"]


def test_schema1_migration_preserves_engineering_and_real_shared_resources():
    p = project(); original = deepcopy(p); old = analyze_project(p)
    result = migrate_project(p)
    ws, projected = result["workspace"], result["project"]
    assert ws["schema_version"] == 2 and ws["id"] != p["id"]
    assert ws["active_path_id"] == ws["paths"][0]["id"] == projected["id"]
    assert ws["origin_project_id"] == p["id"]
    assert "saved_revision" not in ws and "saved_revision" not in projected
    assert not {"cable_types", "layers", "saved_revision"} & ws["paths"][0]["project"].keys()
    assert projected["cable_types"] == ws["cable_types"] and projected["layers"] == ws["layers"]
    assert projected["workspace_context"]["role"] == "deployment"
    summary = result["analysis"]["summary"]
    assert summary["manufactured_total_m"] == old["summary"]["cable_length_m"] == 1030
    for key in ("material_length_m", "body_cost", "material_cost", "vessel_cost", "burial_cost", "cost_total"):
        assert summary[key] == pytest.approx(old["summary"][key])
    assert materialize_path(ws)["profile"]["route_signature"] == route_signature(projected)
    assert p == original


def test_migration_preserves_core_default_library_explicitly_with_warning():
    p = {"schema_version": 1, "route": {"points": [{"longitude": 0, "latitude": 0},
          {"longitude": .01, "latitude": 0}]}, "costs": {}}
    baseline = analyze_project(p)["summary"]
    result = migrate_project(p)
    assert result["workspace"]["cable_types"][0]["id"] == "GENERIC"
    assert result["analysis"]["summary"]["material_cost"] == 0
    assert result["analysis"]["summary"]["manufactured_total_m"] == baseline["cable_length_m"]
    assert "WORKSPACE_DEFAULT_CABLE_LIBRARY" in {w["code"] for w in result["warnings"]}
    assert "cable_types" not in p


def test_optional_core_body_and_reference_ids_are_captured_before_independent_copy():
    p = project()
    for item in p["bodies"]+p["assembly_references"]+p["route"]["points"]:
        item.pop("id")
    ws = migrate_project(p)["workspace"]
    projected = materialize_path(ws)
    assert all(isinstance(item["id"], str) for item in projected["bodies"]+projected["assembly_references"]+projected["route"]["points"])
    result = workspace_action(ws, {"action": "copy_path"})
    assert result["analysis"]["summary"]["manufactured_total_m"] == 2060
    assert {r["id"] for r in projected["assembly_references"]}.isdisjoint({r["id"] for r in result["project"]["assembly_references"]})


def test_multi_path_map_geometry_uses_actual_curves_and_date_line_segments():
    p = project(True)
    p["route"].update(curve="geodesic", points=[{"id": "start", "longitude": 170, "latitude": 70},
        {"id": "end", "longitude": -170, "latitude": 70}], allowances=[])
    p["route"]["legs"][0]["fixed_cable_length_m"] = 800000
    p["bodies"] = []; p["assembly_references"] = []; p.pop("profile")
    ws = migrate_project(p)["workspace"]
    result = workspace_action(ws, {"action": "copy_path", "assembly_policy": "alternative"})
    for row in result["analysis"]["paths"]:
        geometry = row["route_geometry"]["coordinates"]
        assert len(geometry) > 50 and max(point[1] for point in geometry) > 70.2
        assert len(row["route_geometry_segments"]) == 2
        assert all(abs(b[0]-a[0]) < 180 for segment in row["route_geometry_segments"] for a,b in zip(segment, segment[1:]))
        assert row["route_signature"] == result["analysis"]["active_path_analysis"]["route_signature"]


def test_geometry_budget_is_checked_before_full_dense_curve_allocation():
    p = project()
    p["route"]["points"] = [{"id": f"p{i}", "longitude": 0 if i%2 else 120, "latitude": 0} for i in range(253)]
    p["route"]["legs"] = []
    p["bodies"] = []; p["assembly_references"] = []
    with pytest.raises(WorkspaceError, match="WORKSPACE_LIMIT"):
        migrate_project(p)


def test_default_copy_allocates_new_manufacturing_ids_and_doubles_real_quantity():
    ws = workspace(); old = deepcopy(ws)
    result = workspace_action(ws, {"action": "copy_path"})
    copied = result["workspace"]
    assert result["analysis"]["summary"]["assembly_count"] == 2
    assert result["analysis"]["summary"]["manufactured_total_m"] == 2060
    assert result["analysis"]["summary"]["cost_total"] == pytest.approx(2*analyze_workspace(ws)["summary"]["cost_total"])
    first_ids = {i["id"] for i in copied["assemblies"][0]["items"]}
    assert not first_ids & {i["id"] for i in copied["assemblies"][1]["items"]}
    assert {b["id"] for b in result["project"]["bodies"]}.isdisjoint({"repeater", "joint"})
    assert copied["active_path_id"] != ws["active_path_id"]
    assert ws == old


def test_explicit_alternative_shares_inventory_without_double_charge_or_role_switch():
    ws = workspace()
    result = workspace_action(ws, {"action": "copy_path", "assembly_policy": "alternative"})
    summary = result["analysis"]["summary"]
    assert summary["assembly_count"] == 1 and summary["path_count"] == 2
    assert summary["alternative_path_count"] == 1 and summary["deployment_path_count"] == 1
    assert summary["manufactured_total_m"] == 1030
    assert summary["cost_total"] == pytest.approx(analyze_workspace(ws)["summary"]["cost_total"])
    assert result["project"]["workspace_context"]["role"] == "alternative"
    # Active selection is independent of the assembly's installation owner.
    selected = workspace_action(result["workspace"], {"action": "set_active", "path_id": ws["active_path_id"]})
    assert selected["analysis"]["summary"] == summary


def test_independent_flexible_geometry_edit_updates_exclusive_inventory_naturally():
    ws = workspace(); aid = ws["assemblies"][0]["id"]
    p = materialize_path(ws); p["route"]["points"][-1]["longitude"] *= 1.5
    result = workspace_action(ws, {"action": "update_path", "project": p})
    assert result["workspace"]["assemblies"][0]["id"] == aid
    assert result["analysis"]["summary"]["manufactured_total_m"] == pytest.approx(1535)
    assert result["report"]["manufactured_total_delta_m"] == pytest.approx(505)
    assert result["analysis"]["active_path_analysis"]["summary"]["bottom_length_m"] is None


def test_shared_fixed_path_can_change_geometry_when_physical_allocation_is_preserved():
    ws = workspace_action(workspace(True), {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    p = materialize_path(ws); p["route"]["points"][-1]["longitude"] *= 1.5
    result = workspace_action(ws, {"action": "update_path", "project": p})
    assert result["analysis"]["summary"]["manufactured_total_m"] == 1030
    assert result["analysis"]["summary"]["deployment_surface_length_m"] == pytest.approx(1000)
    assert result["analysis"]["active_path_analysis"]["summary"]["surface_length_m"] == pytest.approx(1500)
    assert result["analysis"]["active_path_analysis"]["summary"]["slack_pct"] < 0


def test_shared_flexible_quantity_change_rejected_then_explicit_fork_allocates_new_material():
    ws = workspace_action(workspace(), {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    p = materialize_path(ws); p["route"]["slack_pct"] = 2
    with pytest.raises(WorkspaceError, match="WORKSPACE_SHARED_ASSEMBLY_CHANGED"):
        workspace_action(ws, {"action": "update_path", "project": p})
    result = workspace_action(ws, {"action": "update_path", "project": p, "assembly_policy": "fork"})
    assert result["analysis"]["summary"]["assembly_count"] == 2
    assert result["analysis"]["summary"]["manufactured_total_m"] == pytest.approx(2070)
    assert result["analysis"]["summary"]["deployment_path_count"] == 2
    assert result["project"]["workspace_context"]["role"] == "deployment"


def test_deployment_handover_requires_explicit_role_and_changes_only_installation_owner():
    ws = workspace_action(workspace(True), {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    alternative = ws["active_path_id"]; primary = ws["associations"][0]["path_id"]
    p = materialize_path(ws); p["route"]["points"][-1]["longitude"] *= 2
    ws = workspace_action(ws, {"action": "update_path", "project": p})["workspace"]
    result = workspace_action(ws, {"action": "set_deployment", "path_id": alternative})
    assert result["analysis"]["summary"]["manufactured_total_m"] == 1030
    assert result["analysis"]["summary"]["deployment_surface_length_m"] == pytest.approx(2000)
    assert next(l for l in result["workspace"]["associations"] if l["path_id"] == primary)["role"] == "alternative"
    with pytest.raises(WorkspaceError, match="WORKSPACE_DEPLOYMENT_REQUIRED"):
        workspace_action(ws, {"action": "remove_path", "path_id": primary})
    removed = workspace_action(ws, {"action": "remove_path", "path_id": primary, "successor_path_id": alternative})
    assert removed["analysis"]["summary"]["path_count"] == 1
    assert removed["project"]["workspace_context"]["role"] == "deployment"


def test_geometry_equal_total_but_changed_type_sequence_or_body_station_does_not_match():
    ws = workspace()
    for mutation in ("type", "body", "reference"):
        bad = deepcopy(ws)
        p = bad["paths"][0]["project"]
        if mutation == "type": p["route"]["legs"][0]["cable_type_id"] = "B"
        elif mutation == "body": p["bodies"][0]["cable_kp_m"] += 10
        else: p["assembly_references"][0]["cable_kp_m"] += 10
        with pytest.raises(WorkspaceError, match="WORKSPACE_ASSEMBLY_MISMATCH"):
            analyze_workspace(bad)


def test_shared_price_layer_edits_reprice_once_and_are_visible_to_every_path():
    ws = workspace_action(workspace(), {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    p = materialize_path(ws); p["cable_types"][0]["cost_per_m"] = 4; p["layers"][0]["name"] = "更新共享图层"
    with pytest.raises(WorkspaceError, match="WORKSPACE_SHARED_EDIT_REQUIRED"):
        workspace_action(ws, {"action": "update_path", "project": p})
    made = workspace_action(ws, {"action": "update_path", "project": p, "update_shared": True})
    assert made["analysis"]["summary"]["material_cost"] == pytest.approx(1028*4)
    for path in made["workspace"]["paths"]:
        derived = materialize_path(made["workspace"], path["id"])
        assert derived["cable_types"][0]["cost_per_m"] == 4
        assert derived["layers"][0]["name"] == "更新共享图层"
        assert "cable_types" not in path["project"]
    with pytest.raises(WorkspaceError, match="WORKSPACE_CABLE_TYPE"):
        workspace_action(made["workspace"], {"action": "update_shared", "cable_types": made["workspace"]["cable_types"][1:]})


def test_unassigned_path_and_unallocated_inventory_are_separate_explicit_accounts():
    ws = workspace(); original_cost = analyze_workspace(ws)["summary"]["procurement_cost"]
    detached = workspace_action(ws, {"action": "detach"})
    summary = detached["analysis"]["summary"]
    assert summary["unassigned_path_count"] == summary["unallocated_assembly_count"] == 1
    assert summary["procurement_cost"] == summary["cost_total"] == original_cost
    assert summary["installation_subtotal"] == 0
    assert {w["code"] for w in detached["warnings"]} >= {"WORKSPACE_UNALLOCATED_INVENTORY", "WORKSPACE_UNASSIGNED_PATH"}
    associated = workspace_action(detached["workspace"], {"action": "associate", "assembly_id": ws["assemblies"][0]["id"]})
    assert associated["analysis"]["summary"]["cost_total"] == pytest.approx(analyze_workspace(ws)["summary"]["cost_total"])
    with pytest.raises(WorkspaceError, match="WORKSPACE_ASSEMBLY_IN_USE"):
        workspace_action(associated["workspace"], {"action": "remove_assembly", "assembly_id": ws["assemblies"][0]["id"]})
    empty = workspace_action(ws, {"action": "remove_path", "delete_orphan_assembly": True})
    assert empty["project"] is None and empty["workspace"]["active_path_id"] is None
    assert empty["analysis"]["summary"]["cost_total"] == 0


def test_new_path_and_inventory_are_entities_with_explicit_relations_not_project_list():
    ws = workspace(); p = materialize_path(ws)
    unassigned = workspace_action(ws, {"action": "add_path", "project": p, "name": "待分配方案", "assembly_policy": "unassigned"})
    assert len(unassigned["workspace"]["paths"]) == 2 and len(unassigned["workspace"]["assemblies"]) == 1
    inventory = workspace_action(unassigned["workspace"], {"action": "add_assembly", "project": p, "name": "第二实物装配"})
    assert len(inventory["workspace"]["assemblies"]) == 2
    # It is newly allocated stock with new body IDs, so the old unassigned
    # path cannot silently pretend it contains the second physical assembly.
    with pytest.raises(WorkspaceError, match="WORKSPACE_ASSEMBLY_MISMATCH"):
        workspace_action(inventory["workspace"], {"action": "associate", "assembly_id": inventory["workspace"]["assemblies"][-1]["id"]})


@pytest.mark.parametrize("mutation", ["path_id", "assembly_id", "item_id", "dangling", "two_deployments", "currency", "active", "body_gap"])
def test_duplicate_dangling_mixed_currency_and_inconsistent_assembly_rejected(mutation):
    ws = workspace()
    if mutation == "path_id": ws["paths"].append(deepcopy(ws["paths"][0]))
    elif mutation == "assembly_id": ws["assemblies"].append(deepcopy(ws["assemblies"][0]))
    elif mutation == "item_id":
        extra = deepcopy(ws["assemblies"][0]); extra["id"] = "other"; ws["assemblies"].append(extra)
    elif mutation == "dangling": ws["associations"][0]["assembly_id"] = "absent"
    elif mutation == "two_deployments":
        ws = workspace_action(ws, {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
        ws["associations"][-1]["role"] = "deployment"
    elif mutation == "currency": ws["paths"][0]["project"]["costs"]["currency"] = "USD"
    elif mutation == "active": ws["active_path_id"] = "absent"
    else: ws["assemblies"][0]["items"][0]["end_m"] -= 1; ws["assemblies"][0]["items"][0]["length_m"] -= 1
    with pytest.raises(WorkspaceError):
        validate_workspace(ws)


def test_json_roundtrip_import_migration_preserves_relations_and_new_workspace_identity():
    ws = workspace_action(workspace(), {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    ws["saved_revision"] = 8
    exported = export_workspace(ws); imported = import_workspace(exported)
    assert imported["workspace"]["id"] != ws["id"]
    assert imported["workspace"]["origin_workspace_id"] == ws["id"]
    assert "saved_revision" not in imported["workspace"]
    assert imported["workspace"]["associations"] == ws["associations"]
    assert imported["analysis"]["summary"] == analyze_workspace(ws)["summary"]
    assert import_workspace(json.dumps(project()))["workspace"]["schema_version"] == 2
    json.dumps(imported, allow_nan=False)
    with pytest.raises(ValueError): import_workspace('[1,2]')
    with pytest.raises(ValueError): import_workspace('{"schema_version":NaN}')


def test_path_renaming_workspace_metadata_and_single_source_revision():
    ws = workspace(); ws["saved_revision"] = 5
    p = materialize_path(ws); p["name"] = "备用登陆方案"
    updated = workspace_action(ws, {"action": "update_path", "project": p})
    assert updated["workspace"]["paths"][0]["name"] == updated["project"]["name"] == "备用登陆方案"
    assert updated["workspace"]["saved_revision"] == 5
    assert "saved_revision" not in updated["project"]
    renamed = workspace_action(updated["workspace"], {"action": "update_metadata", "name": "同工程多路径"})
    assert renamed["workspace"]["name"] == "同工程多路径"
    assert renamed["project"]["name"] == "备用登陆方案"


def test_constraints_cannot_be_bypassed_and_independent_copy_recaptures_new_entities():
    p = project(True); p["route"].pop("allowances")
    p = configure_constraints(p, {"mode": "fixed"})["project"]
    ws = migrate_project(p)["workspace"]
    copied = workspace_action(ws, {"action": "copy_path"})
    assert copied["project"]["route"]["constraint_state"]
    assert analyze_project(copied["project"])["summary"]["cable_length_m"] == 1010
    bad = materialize_path(ws); bad["route"]["points"][-1]["longitude"] *= 1.2
    with pytest.raises(ValueError, match="CONSTRAINT_UNSOLVED_EDIT"):
        workspace_action(ws, {"action": "update_path", "project": bad})
    edited = edit_constrained_project(materialize_path(ws), {"moves": [{"point_id": "end", "longitude": .02, "latitude": 0}]})["project"]
    result = workspace_action(ws, {"action": "update_path", "project": edited})
    assert result["analysis"]["summary"]["manufactured_total_m"] == 1010


def test_storage_normalized_relations_revision_conflicts_restore_and_schema1_coexist(tmp_path):
    database = tmp_path/"shared.sqlite3"
    legacy = ProjectStore(database); p = project(); p.pop("saved_revision"); legacy.save(p)
    store = WorkspaceStore(database); ws = workspace()
    first = store.save(ws); saved = first["workspace"]
    assert saved["saved_revision"] == 1 and store.get(ws["id"]) == saved
    with store.connection() as con:
        assert con.execute("SELECT count(*) FROM workspace_paths").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM workspace_assemblies").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM workspace_associations").fetchone()[0] == 1
        assert "paths" not in json.loads(con.execute("SELECT metadata FROM workspace_projects").fetchone()[0])
    with pytest.raises(WorkspaceRevisionConflict): store.save(ws)
    edited = workspace_action(saved, {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    second = store.save(edited)["workspace"]
    assert second["saved_revision"] == 2 and store.list()[0]["path_count"] == 2
    with pytest.raises(WorkspaceRevisionConflict): store.save(saved)
    with pytest.raises(WorkspaceRevisionConflict): store.restore(ws["id"], 1, expected_revision=1)
    restored = store.restore(ws["id"], 1, expected_revision=2)
    assert restored["saved_revision"] == 3 and len(restored["paths"]) == 1
    assert [r["revision"] for r in store.revisions(ws["id"])] == [3,2,1]
    assert legacy.get(p["id"])["saved_revision"] == 1


def test_concurrent_real_sqlite_saves_only_one_writer_wins_and_no_partial_rows(tmp_path):
    store = WorkspaceStore(tmp_path/"concurrent.sqlite3")
    saved = store.save(workspace())["workspace"]
    barrier = threading.Barrier(2)
    def writer(name):
        draft = workspace_action(saved, {"action": "copy_path", "assembly_policy": "alternative", "name": name})["workspace"]
        barrier.wait()
        try: return store.save(draft)
        except WorkspaceRevisionConflict as error: return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, ["并发方案A", "并发方案B"]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, WorkspaceRevisionConflict) for r in results) == 1
    current = store.get(saved["id"])
    assert current["saved_revision"] == 2 and len(current["paths"]) == 2
    assert len(store.revisions(saved["id"])) == 2
    assert len(current["associations"]) == 2 and len(current["assemblies"]) == 1
    validate_workspace(current)


def test_foreign_keys_double_deployment_index_and_write_failure_rollback(tmp_path):
    store = WorkspaceStore(tmp_path/"atomic.sqlite3")
    saved = store.save(workspace())["workspace"]
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection() as con:
            con.execute("INSERT INTO workspace_associations(workspace_id,id,path_id,assembly_id,role,ordinal) VALUES(?,?,?,?,?,?)", (saved["id"], "bad", "missing", saved["assemblies"][0]["id"], "alternative", 99))
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection() as con:
            other = deepcopy(saved["paths"][0]); other["id"] = "another-path"; other["project"]["id"] = other["id"]
            con.execute("INSERT INTO workspace_paths VALUES(?,?,?,?)", (saved["id"], other["id"], 1, json.dumps(other)))
            con.execute("INSERT INTO workspace_associations(workspace_id,id,path_id,assembly_id,role,ordinal) VALUES(?,?,?,?,?,?)",
                        (saved["id"], "second-deployment", other["id"], saved["assemblies"][0]["id"], "deployment", 1))
    assert len(store.get(saved["id"])["paths"]) == 1
    with store.connection() as con:
        con.execute("CREATE TRIGGER reject_workspace_path BEFORE INSERT ON workspace_paths BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
    draft = workspace_action(saved, {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.save(draft)
    assert store.get(saved["id"]) == saved
    assert [r["revision"] for r in store.revisions(saved["id"])] == [1]


def test_http_schema1_import_action_projection_direct_analysis_lifecycle_download(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    client = TestClient(create_app(ProjectStore(tmp_path/"workspace-api.sqlite3")))
    imported = client.post("/api/workspace/import", json={"text": json.dumps(project())})
    assert imported.status_code == 200, imported.text
    envelope = imported.json()
    assert {"workspace", "project", "analysis", "report", "warnings"} <= envelope.keys()
    ws = envelope["workspace"]
    analysis = client.post("/api/workspace/analyze", json=ws)
    assert analysis.status_code == 200 and "workspace" not in analysis.json()
    assert analysis.json()["summary"]["manufactured_total_m"] == 1030
    migrated = client.post("/api/workspace/migrate", json={"project": project()})
    assert migrated.status_code == 200, migrated.text
    first = client.post("/api/workspaces", json=ws)
    assert first.status_code == 200 and first.json()["revision"] == 1
    saved = first.json()["workspace"]
    identifier = saved["id"]
    assert client.get("/api/workspaces").json()[0]["id"] == identifier
    assert client.get(f"/api/workspaces/{identifier}").json() == saved
    missing_revision = client.post("/api/workspaces", json=ws)
    assert missing_revision.status_code == 422 and missing_revision.json()["code"] == "WORKSPACE_REVISION_CONFLICT"
    copied = client.post("/api/workspace/action", json={"workspace": saved, "config": {"action": "copy_path", "assembly_policy": "alternative"}})
    assert copied.status_code == 200, copied.text
    assert copied.json()["workspace"]["saved_revision"] == 1
    second = client.post("/api/workspaces", json=copied.json()["workspace"])
    assert second.status_code == 200 and second.json()["revision"] == 2
    assert client.post("/api/workspaces", json=saved).status_code == 422
    history = client.get(f"/api/workspaces/{identifier}/revisions").json()
    assert [r["revision"] for r in history] == [2,1]
    stale_restore = client.post(f"/api/workspaces/{identifier}/restore/1", json={"expected_revision": 1})
    assert stale_restore.status_code == 422
    restored = client.post(f"/api/workspaces/{identifier}/restore/1", json={"expected_revision": 2})
    assert restored.status_code == 200 and restored.json()["saved_revision"] == 3
    assert len(restored.json()["paths"]) == 1
    exported = client.post("/api/workspace/export", json=restored.json())
    assert exported.status_code == 200 and 'attachment' in exported.headers['content-disposition']
    assert json.loads(exported.text)["schema_version"] == 2
    again = client.post("/api/workspace/import", json={"text": exported.text})
    assert again.status_code == 200 and again.json()["workspace"]["id"] != identifier
    assert "saved_revision" not in again.json()["workspace"]
    for invalid in ("broken JSON", "[]", "null", "1", '{"schema_version":NaN}'):
        response = client.post("/api/workspace/import", json={"text": invalid})
        assert response.status_code == 422, (invalid, response.text)


def test_same_curve_partition_changes_do_not_replace_manufacturing_entities(tmp_path):
    from oceanroute.tools import subdivide_project
    ws = workspace()
    entity_ids = [i["id"] for i in ws["assemblies"][0]["items"]]
    p = subdivide_project(materialize_path(ws), {"spacing_m": 100})["project"]
    result = workspace_action(ws, {"action": "update_path", "project": p})
    assert [i["id"] for i in result["workspace"]["assemblies"][0]["items"]] == entity_ids
    result["workspace"]["associations"][0]["note"] = "附加关系追溯备注"
    store = WorkspaceStore(tmp_path/"metadata.sqlite3")
    made = store.save(result["workspace"])["workspace"]
    assert store.get(made["id"])["associations"][0]["note"] == "附加关系追溯备注"
