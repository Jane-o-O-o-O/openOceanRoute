"""HTTP rule declarations, real geometry, complete candidates and durable ownership.

Expected locations/angles are constructed from equatorial geometry, rather than
copied from the checker. These are independent synthetic cases, not field data.
"""
from copy import deepcopy
import json
import math

from fastapi.testclient import TestClient
import pytest

from oceanroute.api import create_app
from oceanroute.core import route_signature
from oceanroute.storage import ProjectStore
from oceanroute.workspace import analyze_workspace, migrate_project, validate_workspace, workspace_action


def rule_workspace(*, alternative=False):
    project = {
        "schema_version": 1, "id": "rules-http-source", "name": "Synthetic equatorial route",
        "crs": "EPSG:4326",
        "route": {"id": "r", "curve": "geodesic", "mode": "flexible", "slack_pct": 1,
                  "slack_basis": "surface", "points": [
                      {"id": "a", "longitude": 0, "latitude": 0, "depth_m": 50},
                      {"id": "b", "longitude": .01, "latitude": 0, "depth_m": 50}],
                  "legs": [{"cable_type_id": "C"}],
                  "allowances": [{"id": "reserve", "kp_m": 200, "length_m": 40}]},
        "cable_types": [{"id": "C", "name": "Declared cable", "cost_per_m": 3,
                         "lay_speed_m_s": 1, "wet_weight_n_m": 10, "ea_n": 1e6}],
        "bodies": [{"id": "joint", "name": "Synthetic joint", "kp_m": 500,
                    "length_m": 0, "cost": 25}],
        "layers": [{"id": "gis", "name": "Synthetic GIS", "kind": "reference",
                    "visible": False, "geojson": {"type": "FeatureCollection", "features": [
                        {"type": "Feature", "id": 1, "properties": {"name": "Numeric ID"},
                         "geometry": {"type": "LineString", "coordinates": [[.0045, -.001], [.0045, .001]]}},
                        {"type": "Feature", "id": "1", "properties": {"name": "String ID"},
                         "geometry": {"type": "LineString", "coordinates": [[.0075, -.001], [.0075, .001]]}},
                        {"type": "Feature", "properties": {"name": "No native ID"},
                         "geometry": {"type": "Point", "coordinates": [.005, .0001]}},
                        {"type": "Feature", "id": "index:2", "properties": {},
                         "geometry": {"type": "Point", "coordinates": [.006, .0002]}}
                    ]}}],
        "profile": {"source": "declared synthetic survey", "measured": True,
                    "samples": [{"kp_m": 0, "depth_m": 50}, {"kp_m": 2000, "depth_m": 50}]},
        "costs": {"currency": "CNY"}, "user_extension": {"preserve": ["route", "inventory"]},
    }
    project["profile"]["route_signature"] = route_signature(project)
    workspace = migrate_project(project)["workspace"]
    if alternative:
        workspace = workspace_action(workspace, {"action": "copy_path", "assembly_policy": "alternative"})["workspace"]
    workspace["user_extension"] = {"preserve": {"scope": "whole workspace", "value": 17}}
    return workspace


def crossing(workspace, **extra):
    return {"id": "cross", "name": "Measured depth trigger", "enabled": True,
            "path_id": workspace["active_path_id"], "kind": "crossing", "start_kp_m": 0,
            "end_kp_m": None, "selectors": [{"layer_id": "gis", "feature_ids": [1]}],
            "conditions": [{"field": "depth_m", "comparison": "gt", "value": 40}],
            "match_mode": "all", "body_distance_mode": "horizontal", **extra}


def post(client, url, payload):
    response = client.post(url, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    json.dumps(result, allow_nan=False)
    return result


def check(client, workspace, rules):
    return post(client, "/api/automatic-rules/check", {"workspace": workspace, "config": {"rules": rules}})


def without_rules(workspace):
    result = deepcopy(workspace)
    result.pop("automatic_rules", None)
    return result


def test_actual_http_crossing_location_depth_and_typed_feature_ids(tmp_path):
    workspace = rule_workspace()
    original = deepcopy(workspace)
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        catalog = post(client, "/api/automatic-rules/catalog", {"workspace": workspace})
        features = next(layer for layer in catalog["layers"] if layer["id"] == "gis")["features"]
        assert features[0]["id"] == 1 and type(features[0]["id"]) is int
        assert features[1]["id"] == "1" and type(features[1]["id"]) is str
        assert features[2]["selector"] == {"layer_id": "gis", "feature_indexes": [2]}
        assert features[3]["selector"] == {"layer_id": "gis", "feature_ids": ["index:2"]}
        numeric = check(client, workspace, [crossing(workspace)])
        assert numeric["checks"]["results"][0]["status"] == "violations"
        rows = numeric["checks"]["results"][0]["violations"]
        assert rows and all(row["location"]["longitude"] == pytest.approx(.0045, abs=1e-7) for row in rows)
        assert all(row["location"]["latitude"] == pytest.approx(0, abs=1e-7) for row in rows)
        assert all(row["location"]["kp_m"] == pytest.approx(500.9377085697311, abs=.05) for row in rows)
        assert all(row["location"]["depth_m"] == pytest.approx(50) for row in rows)
        textual = check(client, workspace, [crossing(workspace, selectors=[{"layer_id": "gis", "feature_ids": ["1"]}])])
        assert all(row["location"]["longitude"] == pytest.approx(.0075, abs=1e-7)
                   for row in textual["checks"]["results"][0]["violations"])
        assert without_rules(numeric["workspace"]) == workspace
        assert workspace == original
        assert client.get("/api/workspaces").json() == [] and client.get("/api/projects").json() == []


def test_explicit_error_comparators_and_all_any_are_not_inverted(tmp_path):
    workspace = rule_workspace()
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        angle = {"field": "angle_deg", "comparison": "lt", "value": 35}
        depth = {"field": "depth_m", "comparison": "gt", "value": 40}
        all_result = check(client, workspace, [crossing(workspace, conditions=[angle, depth])])
        assert all_result["checks"]["results"][0]["status"] == "clear"
        assert not all_result["checks"]["results"][0]["violations"]
        any_result = check(client, workspace, [crossing(workspace, conditions=[angle, depth], match_mode="any")])
        assert any_result["checks"]["results"][0]["status"] == "violations"
        shallow = check(client, workspace, [crossing(workspace, conditions=[{**depth, "comparison": "lt"}])])
        assert shallow["checks"]["results"][0]["status"] == "clear"


def test_complete_atomic_rule_action_preserves_two_paths_one_inventory_and_extensions(tmp_path):
    workspace = rule_workspace(alternative=True)
    old_summary = analyze_workspace(workspace)["summary"]
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        preview = check(client, workspace, [crossing(workspace)])
        assert without_rules(preview["workspace"]) == workspace
        updated = post(client, "/api/workspace/action", {"workspace": workspace,
            "config": {"action": "update_automatic_rules", "rules": preview["rules"]}})
        assert without_rules(updated["workspace"]) == workspace
        assert updated["analysis"]["summary"] == old_summary
        assert len(updated["workspace"]["assemblies"]) == 1
        assert len(updated["workspace"]["paths"]) == 2
        assert updated["workspace"]["automatic_rules"] == preview["rules"]
        aggregate = post(client, "/api/workspace/analyze", updated["workspace"])
        assert aggregate["automatic_rule_checks"]["results"][0]["status"] == "violations"
        assert client.get("/api/workspaces").json() == []


def test_body_slope_check_uses_actual_two_dimensional_source_not_flat_route_profile(tmp_path):
    workspace = rule_workspace()
    # Independent projected plane centered on the declared body at route KP500.
    # D=1000+.1*x+.2*y has height gradient (-.1,-.2), hence atan(sqrt(.05)).
    center_lon = math.degrees(500 / 6378137.0)
    workspace["terrain_sources"] = [{
        "id": "plane", "name": "Explicit synthetic 2D plane", "kind": "xyz",
        "enabled": True, "priority": 1,
        "source_crs": f"+proj=aeqd +lat_0=0 +lon_0={center_lon:.15f} +datum=WGS84 +units=m +type=crs",
        "depth_positive": "down", "depth_units": "m", "vertical_datum": "synthetic-plane-datum",
        "sampling": {"method": "linear", "max_gap_m": 1000},
        "text": "\n".join(f"{x} {y} {1000+.1*x+.2*y}" for y in (-200, 0, 200) for x in (-200, 0, 200)),
    }]
    workspace = validate_workspace(workspace)
    original = deepcopy(workspace)
    rule = {"id": "body-bed", "name": "Actual surrounding bed", "enabled": True,
            "path_id": workspace["active_path_id"], "kind": "proximity", "start_kp_m": 0,
            "end_kp_m": None, "around": "bodies", "targets": ["slopes"], "distance_m": 50,
            "water_depth_m": {"min_m": 0, "max_m": 100}, "slope_threshold_deg": 5,
            "slope_probe_spacing_m": 25, "slope_vertical_datum": "synthetic-plane-datum"}
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        result = check(client, workspace, [rule])
        assert result["checks"]["results"][0]["status"] == "violations"
        assert result["checks"]["results"][0]["violations"]
        rows = [row for row in result["checks"]["errors"]
                if row["status"] == "violation" and row["location"] is not None]
        assert rows and len(rows) == len(result["checks"]["results"][0]["violations"])
        expected_slope = math.degrees(math.atan(math.sqrt(.05)))
        for row in rows:
            location, evidence = row["location"], row["violation"]
            witness = evidence["sampled_witness"]
            assert evidence["slope_deg"] == pytest.approx(expected_slope, abs=1e-6)
            assert evidence["gradient_height"] == pytest.approx([-.1, -.2], abs=1e-7)
            assert witness["depth_m"] == pytest.approx(1000+.1*witness["x_m"]+.2*witness["y_m"], abs=1e-6)
            assert witness["source_id"] == "plane" and witness["source_fingerprint"]
            assert location["kp_m"] == pytest.approx(500, abs=.01)
            assert location["depth_m"] is None  # This child centroid was not queried.
            assert location["radius_center"]["longitude"] == pytest.approx(center_lon, abs=1e-9)
            assert location["radius_center"]["latitude"] == pytest.approx(0, abs=1e-9)
        assert analyze_workspace(workspace)["active_path_analysis"]["legs"][0]["max_slope_deg"] == 0
        assert without_rules(result["workspace"]) == workspace and workspace == original
        assert client.get("/api/workspaces").json() == []


def test_saved_rules_and_references_survive_actual_new_app_and_revision_conflict(tmp_path):
    database = tmp_path / "state.sqlite3"
    workspace = rule_workspace(alternative=True)
    with TestClient(create_app(ProjectStore(database))) as client:
        initial = post(client, "/api/workspaces", workspace)["workspace"]
        preview = check(client, initial, [crossing(initial)])
        assert preview["workspace"]["saved_revision"] == initial["saved_revision"]
        accepted = post(client, "/api/workspaces", preview["workspace"])["workspace"]
        assert accepted["saved_revision"] == 2
        assert without_rules({**accepted, "saved_revision": initial["saved_revision"]}) == initial
        stale = client.post("/api/workspaces", json=preview["workspace"])
        assert stale.status_code == 422
        assert len(client.get(f'/api/workspaces/{accepted["id"]}/revisions').json()) == 2
    with TestClient(create_app(ProjectStore(database))) as reopened:
        reread = reopened.get(f'/api/workspaces/{accepted["id"]}').json()
        assert reread == accepted
        assert check(reopened, reread, reread["automatic_rules"])["checks"]["results"][0]["status"] == "violations"
        restored = post(reopened, f'/api/workspaces/{accepted["id"]}/restore/1', {"expected_revision": 2})
        assert "automatic_rules" not in restored
        assert restored["saved_revision"] == 3
        assert restored["assemblies"] == workspace["assemblies"]


def test_missing_reference_is_saved_editable_and_exported_not_an_empty_clear(tmp_path):
    workspace = rule_workspace()
    missing = crossing(workspace, path_id="missing-path", selectors=[{"layer_id": "missing-layer", "feature_ids": [1]}])
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        preview = check(client, workspace, [missing])
        assert preview["checks"]["results"][0]["status"] == "reference_error"
        assert preview["checks"]["errors"]
        assert all(row["location"] is None for row in preview["checks"]["errors"])
        accepted = post(client, "/api/workspaces", preview["workspace"])["workspace"]
        exported = post(client, "/api/automatic-rules/export", {"workspace": accepted})
        assert exported["package"]["rules"][0]["path_id"] == "missing-path"
        assert exported["package"]["rules"][0]["end_kp_m"] is None
        assert json.loads(exported["text"]) == exported["package"]


def test_import_binding_retain_replace_and_duplicate_rejection_are_atomic(tmp_path):
    workspace = rule_workspace()
    foreign = crossing(workspace, path_id="foreign-path")
    package = {"schema": "oceanroute.automatic-rules/v1", "schema_version": 1, "rules": [foreign]}
    original = deepcopy(workspace)
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        imported = post(client, "/api/automatic-rules/import", {"workspace": workspace, "package": json.dumps(package)})
        assert imported["rules"][0]["path_id"] == workspace["active_path_id"]
        assert imported["report"]["binding"] == "active_path"
        assert imported["checks"]["results"][0]["status"] == "violations"
        retained = post(client, "/api/automatic-rules/import", {
            "workspace": workspace, "package": package, "config": {"binding": "retain"}})
        assert retained["rules"][0]["path_id"] == "foreign-path"
        assert retained["checks"]["results"][0]["status"] == "reference_error"
        duplicate = client.post("/api/automatic-rules/import", json={"workspace": imported["workspace"], "package": package})
        assert duplicate.status_code == 422
        renamed = post(client, "/api/automatic-rules/import", {"workspace": imported["workspace"],
            "package": package, "config": {"duplicate_ids": "rename"}})
        assert len(renamed["rules"]) == 2 and len({row["id"] for row in renamed["rules"]}) == 2
        assert renamed["report"]["renamed_ids"]
        replaced = post(client, "/api/automatic-rules/import", {"workspace": renamed["workspace"],
            "package": {**package, "rules": []}, "config": {"mode": "replace"}})
        assert replaced["workspace"]["automatic_rules"] == []
        assert without_rules(replaced["workspace"]) == workspace
        assert workspace == original
        assert client.get("/api/workspaces").json() == []


def test_empty_workspace_import_retains_missing_reference_and_can_be_saved(tmp_path):
    workspace = rule_workspace()
    workspace["paths"] = []
    workspace["associations"] = []
    workspace["active_path_id"] = None
    package = {"schema": "oceanroute.automatic-rules/v1", "schema_version": 1,
               "rules": [crossing({**workspace, "active_path_id": "old-path"})]}
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        imported = post(client, "/api/automatic-rules/import", {"workspace": workspace, "package": package})
        assert imported["rules"][0]["path_id"] == "old-path"
        assert imported["checks"]["results"][0]["status"] == "reference_error"
        assert imported["workspace"]["assemblies"] == workspace["assemblies"]
        accepted = post(client, "/api/workspaces", imported["workspace"])["workspace"]
        assert accepted["paths"] == [] and accepted["automatic_rules"] == imported["rules"]


@pytest.mark.parametrize("endpoint,payload", [
    ("catalog", {}), ("check", {}), ("export", {}), ("import", {}),
    ("catalog", {"config": {"unknown": 1}}),
    ("check", {"unknown": 1}), ("export", {"unknown": 1}),
    ("import", {"package": {"schema": "other", "schema_version": 1, "rules": []}}),
    ("import", {"package": {"schema": "oceanroute.automatic-rules/v1", "schema_version": 1,
                               "rules": [], "unknown": 1}}),
])
def test_strict_http_payloads_reject_without_any_saved_state(tmp_path, endpoint, payload):
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        submitted = deepcopy(payload)
        if payload:
            submitted["workspace"] = rule_workspace()
        response = client.post("/api/automatic-rules/" + endpoint, json=submitted)
        assert response.status_code == 422, response.text
        assert client.get("/api/workspaces").json() == []


@pytest.mark.parametrize("change", [
    {"start_kp_m": True}, {"start_kp_m": -1}, {"end_kp_m": -1},
    {"conditions": [{"field": "angle_deg", "comparison": "lt", "value": 91}]},
    {"conditions": [{"field": "depth_m", "comparison": "unknown", "value": 10}]},
    {"selectors": [{"layer_id": "gis", "feature_ids": [1], "feature_indexes": [0]}]},
])
def test_invalid_declarations_reject_whole_http_candidate(tmp_path, change):
    workspace = rule_workspace()
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        response = client.post("/api/automatic-rules/check", json={
            "workspace": workspace, "config": {"rules": [crossing(workspace, **change)]}})
        assert response.status_code == 422, response.text
        assert client.get("/api/workspaces").json() == []


def test_rule_budget_and_nonfinite_json_cannot_return_truncated_clear(tmp_path):
    workspace = rule_workspace()
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        response = client.post("/api/automatic-rules/check", json={"workspace": workspace,
            "config": {"rules": [crossing(workspace), crossing(workspace, id="second")], "max_rules": 1}})
        assert response.status_code == 422
        malformed = {"workspace": workspace, "config": {"rules": [crossing(workspace, start_kp_m=math.nan)]}}
        response = client.post("/api/automatic-rules/check", content=json.dumps(malformed),
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 422
        assert client.get("/api/workspaces").json() == []


def test_new_http_geometry_and_direct_checker_do_not_run_legacy_screening(tmp_path, monkeypatch):
    """The new checker must calculate its own contacts, not filter old results.

    This also prevents unbudgeted per-leg local projections during its admission.
    Normal workspace analysis and persistence still keep their existing checks.
    """
    import oceanroute.core as core
    from oceanroute.automatic_rules import check_automatic_rules
    from oceanroute.workspace import _validate

    workspace = rule_workspace()
    calls = []
    old_crossings = core._crossings

    def observed_crossings(*args, **kwargs):
        calls.append(True)
        return old_crossings(*args, **kwargs)

    monkeypatch.setattr(core, "_crossings", observed_crossings)
    with TestClient(create_app(ProjectStore(tmp_path / "state.sqlite3"))) as client:
        post(client, "/api/automatic-rules/catalog", {"workspace": workspace})
        preview = check(client, workspace, [crossing(workspace)])
        post(client, "/api/automatic-rules/export", {"workspace": preview["workspace"]})
        package = {"schema": "oceanroute.automatic-rules/v1", "schema_version": 1,
                   "rules": [crossing(workspace)]}
        post(client, "/api/automatic-rules/import", {"workspace": workspace, "package": package})
        direct = check_automatic_rules(workspace, {"rules": [crossing(workspace)]})
        assert direct["results"][0]["status"] == "violations" and not calls
        private_workspace, analyses, _ = _validate(workspace, _check_legacy_crossings=False)
        assert private_workspace == workspace
        assert analyses[workspace["active_path_id"]]["crossings"] is None
        assert analyses[workspace["active_path_id"]]["legacy_crossings_evaluated"] is False
        assert not calls
        normal = post(client, "/api/workspace/analyze", workspace)
        assert len(calls) == 1
        assert isinstance(normal["active_path_analysis"]["crossings"], list)
        assert "legacy_crossings_evaluated" not in normal["active_path_analysis"]
        post(client, "/api/workspaces", preview["workspace"])
        assert len(calls) == 2
