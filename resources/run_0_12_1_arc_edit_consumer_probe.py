#!/usr/bin/env python3
"""Bounded actual ASGI workflow probe; never modifies production inputs.

Expected geometry uses independent PROJ Direct and GeographicLib m12
quadrature, not the editor's root helpers or route interpolation. Default
report creation is exclusive; failed evidence is retained under its own name.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def finite_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def snapshot():
    rows = []
    for p in sorted((ROOT/"oceanroute").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and (p.suffix == ".py" or "static" in p.parts or p.name == "manual.md"):
            b = p.read_bytes()
            rows.append({"path": p.relative_to(ROOT).as_posix(), "bytes": len(b), "sha256": digest(b)})
    return rows


def circle_point(g, az, radius=None):
    from pyproj import Geod
    return Geod(ellps="WGS84").fwd(*g["center"], az, g["radius_m"] if radius is None else radius)[:2]


def metric_integral(g, lo=0., hi=1.):
    from geographiclib.geodesic import Geodesic
    from scipy.integrate import quad
    scale = abs(math.radians(g["sweep_deg"]))
    def metric(f):
        radial = Geodesic.WGS84.Direct(g["center"][1], g["center"][0],
            g["start_azimuth_deg"]+g["sweep_deg"]*f, g["radius_m"], Geodesic.ALL)
        return radial["m12"]*scale
    return float(quad(metric, lo, hi, epsabs=1e-8, epsrel=4e-14)[0])


def fixture():
    geometry = {"type": "circular_arc", "schema_version": 1, "center": [118., 22.],
                "radius_m": 600., "start_azimuth_deg": -60., "sweep_deg": 120.}
    a = circle_point(geometry, -60.)
    b = circle_point(geometry, 60.)
    source = {"id": "explicit-synthetic-constant-bed", "name": "合成55米平床，非现场", "kind": "xyz",
              "enabled": True, "priority": 1, "source_crs": "EPSG:4326", "depth_positive": "down",
              "depth_units": "m", "vertical_datum": "synthetic-model-sea-level",
              "text": "longitude latitude depth_m\n117.97 21.97 55\n118.03 21.97 55\n117.97 22.03 55\n118.03 22.03 55",
              "sampling": {"method": "linear", "max_gap_m": 10000}}
    return {"schema_version": 1, "id": "independent-0.12-arc-consumers", "name": "Explicit synthetic edit consumer probe",
            "crs": "EPSG:4326", "route": {"curve": "rhumb", "mode": "fixed", "slack_basis": "surface", "slack_pct": 1,
                "points": [{"id": ident, "longitude": p[0], "latitude": p[1], "depth_m": 55.}
                           for ident, p in (("a", a), ("b", b))],
                "legs": [{"geometry": geometry, "cable_type_id": "A", "mode": "fixed", "fixed_cable_length_m": 5300.}],
                "allowances": [{"kp_m": 100., "length_m": 10., "cable_type_id": "A"}]},
            "cable_types": [{"id": "A", "name": "Explicit synthetic cable", "cost_per_m": 1., "lay_speed_m_s": .5,
                "wet_weight_n_m": 4., "mass_kg_m": 1., "diameter_m": .02, "ea_n": 1e6, "ei_n_m2": 0.}],
            "bodies": [{"id": "physical-zero-point", "cable_kp_m": 1200., "length_m": 0., "mass_kg": 5., "wet_weight_n": -10.},
                       {"id": "additional-physical-body", "kp_m": 150., "length_m": 8., "length_mode": "additional"}],
            "assembly_references": [{"id": "manufacturing-reference", "cable_kp_m": 1800., "length_m": 0.}],
            "events": [{"id": "synthetic-survey-event", "kp_m": 200., "kind": "survey"}],
            "terrain_sources": [source], "layers": []}


def run(report):
    from fastapi.testclient import TestClient
    from pyproj import Geod
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    from oceanroute.core import route_signature
    G = Geod(ellps="WGS84")
    calls = []
    input_project = fixture()
    untouched = deepcopy(input_project)
    with tempfile.TemporaryDirectory(prefix="oceanroute-0.12-independent-consumers-") as temporary:
        with TestClient(create_app(ProjectStore(Path(temporary)/"isolated.sqlite3"))) as client:
            def post(path, payload):
                saved = deepcopy(payload)
                response = client.post(path, json=payload)
                calls.append({"method": "POST", "path": path, "status_code": response.status_code,
                              "request_sha256": digest(finite_json(payload)), "response_sha256": digest(response.content),
                              "response_bytes": len(response.content)})
                assert payload == saved, "caller input mutated"
                assert response.status_code == 200, (path, response.status_code, response.text[:3000])
                value = response.json(); finite_json(value)
                return value
            report["actual_http_calls"] = calls
            configured = post("/api/constraints/configure", {"project": input_project,
                "config": {"mode": "fixed", "path_links": [{"point_id": "a"}, {"point_id": "b"}]}})["project"]
            before_profile = post("/api/terrain/profile", {"project": configured,
                "config": {"spacing_m": 500., "vertical_datum": "synthetic-model-sea-level"}})
            before = post("/api/terrain/side-slopes", {"project": before_profile["project"],
                "config": {"spacing_m": 500., "half_width_m": 20., "cross_spacing_m": 20.,
                           "vertical_datum": "synthetic-model-sea-level"}})["project"]
            before_exact = deepcopy(before)
            analysis_before = post("/api/analyze", before)
            b = before["route"]["points"][-1]
            lon, lat, _ = G.fwd(b["longitude"], b["latitude"], 25., 5.)
            edited = post("/api/tools/arc-edit", {"project": before,
                "config": {"moves": [{"point_id": "b", "longitude": lon, "latitude": lat}],
                           "arc_options": [{"start_point_id": "a", "end_point_id": "b", "radius_m": 700.}]}})
            candidate = edited["project"]
            assert before == before_exact and input_project == untouched
            assert candidate["route"]["constraint_state"]["manufacturing"] == before["route"]["constraint_state"]["manufacturing"]
            assert candidate["route"]["path_links"] == before["route"]["path_links"]
            assert candidate["assembly_references"] == before["assembly_references"]
            assert candidate["bodies"][0] == before["bodies"][0]
            assert candidate["profile"] == before["profile"] and candidate["side_slopes"] == before["side_slopes"]
            assert candidate["route"]["points"][-1]["depth_m"] is None
            assert route_signature(candidate) != route_signature(before)
            analysis = post("/api/analyze", candidate)
            assert not analysis["profile_metadata"]["imported_profile_valid"]
            assert not analysis["side_slopes_metadata"]["geometry_current"]
            assert analysis["summary"]["cable_length_m"] == analysis_before["summary"]["cable_length_m"] == 5318.
            g = candidate["route"]["legs"][0]["geometry"]
            length = metric_integral(g)
            assert abs(analysis["summary"]["surface_length_m"]-length) < 1e-7
            chord = G.inv(candidate["route"]["points"][0]["longitude"], candidate["route"]["points"][0]["latitude"], lon, lat)[2]
            assert length-chord > 50.
            assert g["radius_m"] == 700.
            alpha = g["start_azimuth_deg"]+.5*g["sweep_deg"]
            on_circle = circle_point(g, alpha)
            observed = circle_point(g, alpha, 725.)
            true_kp = metric_integral(g, 0., .5)
            _, back, _ = G.inv(*g["center"], *on_circle)
            true_heading = (back+180.+90.) % 360.
            survey = post("/api/survey/reconcile", {"project": candidate,
                "config": {"observations": [{"longitude": observed[0], "latitude": observed[1], "depth_m": 55.}],
                           "route_sample_step_m": 300., "station_tolerance_m": .001}})
            observation = survey["observations"][0]
            assert abs(observation["route_kp_m"]-true_kp) < .02
            assert abs(observation["nearest_distance_m"]-25.) < .002
            assert abs(observation["cross_track_m"]+25.) < .002
            assert abs((observation["route_bearing_deg"]-true_heading+180.) % 360.-180.) < .002
            fresh = post("/api/terrain/profile", {"project": candidate,
                "config": {"spacing_m": 200., "vertical_datum": "synthetic-model-sea-level"}})
            assert fresh["quality"]["missing_count"] == 0
            assert fresh["profile"]["route_signature"] == route_signature(candidate)
            assert fresh["project"]["side_slopes"] == candidate["side_slopes"]
            for row in fresh["samples"]:
                assert abs(G.inv(*g["center"], row["longitude"], row["latitude"])[2]-700.) < 1e-6
                assert abs(row["depth_m"]-55.) < 1e-9
            layer = {"id": "independent-actual-circle-target", "name": "Synthetic radial witness, not a chart",
                "kind": "restricted", "visible": True, "geojson": {"type": "FeatureCollection", "features": [{
                    "type": "Feature", "id": "radial-25m", "properties": {"synthetic": True},
                    "geometry": {"type": "Point", "coordinates": list(observed)}}]}}
            rule_project = deepcopy(candidate); rule_project["layers"] = [layer]
            ws = post("/api/workspace/migrate", {"project": rule_project})["workspace"]
            rule = {"id": "circle-nearest-25m", "path_id": ws["active_path_id"], "kind": "proximity",
                "end_kp_m": None, "selectors": [{"layer_id": layer["id"], "feature_ids": None}],
                "around": "path", "targets": ["gis"], "distance_m": 40.}
            checked = post("/api/automatic-rules/check", {"workspace": ws, "config": {"rules": [rule]}})["checks"]
            report["stale_profile_rule_guard"] = checked["results"][0]
            assert checked["results"][0]["status"] == "incomplete"
            assert any(x["code"] == "PROXIMITY_DEPTH_UNAVAILABLE" for x in checked["results"][0]["diagnostics"])
            # The same edited circle needs a truly fresh depth profile before
            # the rule's default min-depth filter can be physically evaluated.
            rule_project = deepcopy(fresh["project"]); rule_project["layers"] = [layer]
            ws = post("/api/workspace/migrate", {"project": rule_project})["workspace"]
            rule["path_id"] = ws["active_path_id"]
            checked = post("/api/automatic-rules/check", {"workspace": ws, "config": {"rules": [rule]}})["checks"]
            report["actual_automatic_result"] = checked
            row = checked["results"][0]
            assert row["status"] == "violations" and not row["diagnostics"]
            nearest = min(row["violations"], key=lambda event: event["distance_m"])
            assert abs(nearest["distance_m"]-25.) < .25
            # Native numerical allowances are not certified directed-rounded
            # intervals. Do not accidentally cancel the allowance on both
            # sides of a chained comparison; the two Geod constructions can
            # differ from the analytical 25m by a few nanometres.
            assert nearest["distance_bounds_m"][0] <= 25.+2e-8
            assert nearest["distance_bounds_m"][1] >= 25.-2e-8
            straight = deepcopy(rule_project)
            straight["route"]["legs"][0].pop("geometry")
            # Remove stale intrinsic-domain state for this explicit independent
            # chord comparator, rather than silently reinterpret the editor.
            straight["route"].pop("constraint_state", None); straight["route"].pop("path_links", None)
            straight = post("/api/terrain/profile", {"project": straight,
                "config": {"spacing_m": 200., "vertical_datum": "synthetic-model-sea-level"}})["project"]
            chord_ws = post("/api/workspace/migrate", {"project": straight})["workspace"]
            chord_rule = {**rule, "path_id": chord_ws["active_path_id"]}
            chord_check = post("/api/automatic-rules/check", {"workspace": chord_ws, "config": {"rules": [chord_rule]}})["checks"]["results"][0]
            assert chord_check["status"] == "clear" and not chord_check["violations"]
            plan = post("/api/shipplan/create", {"project": fresh["project"],
                "config": {"sample_spacing_m": 300., "max_samples": 30, "bottom_tension_n": 10.}})
            assert abs(plan["summary"]["paid_out_m"]-5318.) < 1e-7
            assert abs(plan["summary"]["material_balance_residual_m"]) < 1e-7
            for point in plan["vessel_waypoints"]:
                assert abs(G.inv(*g["center"], point["longitude"], point["latitude"])[2]-700.) < 1e-6
                _, radial_back, _ = G.inv(*g["center"], point["longitude"], point["latitude"])
                heading = (radial_back+180.+90.) % 360.
                assert abs((point["route_heading_deg"]-heading+180.) % 360.-180.) < 1e-7
            # No project or workspace candidate has ever been saved by this probe.
            response = client.get("/api/projects")
            calls.append({"method": "GET", "path": "/api/projects", "status_code": response.status_code,
                          "response_sha256": digest(response.content), "response_bytes": len(response.content)})
            assert response.status_code == 200 and response.json() == []
            assert before == before_exact and input_project == untouched
            report.update({"status": "passed", "synthetic": True, "actual_http_request_count": len(calls),
                "consumer_groups": ["RPL_analysis", "survey", "multi_source_terrain", "automatic_rules", "shipplan"],
                "candidate": {"geometry": g, "original_geometry": before["route"]["legs"][0]["geometry"],
                    "input_unchanged": True, "saved_project_count": 0,
                    "old_route_signature": route_signature(before), "new_route_signature": route_signature(candidate),
                    "profile_retained_stale": True, "side_slopes_retained_stale": True,
                    "constraint_manufacturing_unchanged": True, "physical_stock_m": 5318.,
                    "edit_report": edited["report"]},
                "oracle": {"length_basis": "independent GeographicLib reduced-length quadrature; shared native ellipsoid engine disclosed",
                    "length_m": length, "straight_chord_m": chord, "arc_minus_chord_m": length-chord,
                    "radial_observation": list(observed), "known_nearest_distance_m": 25., "true_kp_m": true_kp,
                    "true_heading_deg": true_heading},
                "observed": {"rpl_surface_length_m": analysis["summary"]["surface_length_m"], "survey": observation,
                    "terrain_sample_count": len(fresh["samples"]), "terrain_budget": fresh["budget"],
                    "automatic_status": row["status"], "automatic_witness": nearest, "explicit_chord_status": chord_check["status"],
                    "shipplan_sample_count": len(plan["vessel_waypoints"]), "shipplan_summary": plan["summary"]},
                "limitations": ["Bounded synthetic workflow only; no manufacturer-equivalence or field-calibration claim",
                    "Whole source parity covers oceanroute Python/static/manual inputs, not concurrent frontend authoring",
                    "No production persistence or browser action; actual isolated ASGI owner lifecycle was used",
                    "Steady ShipPlan offsets remain sampled first-cut approximations; this probe verifies their route geometry and inventory consumption"]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="resources/validation/development_0.12.1_arc_edit_consumer_probe.json")
    args = parser.parse_args()
    output = ROOT/args.report
    if output.exists():
        raise SystemExit("refusing to overwrite an existing evidence report")
    before = snapshot(); start = time.perf_counter()
    report = {"schema_version": 1, "development_version": "0.12.1", "status": "running",
              "scope": "independent synthetic actual ASGI edit-to-consumer workflow, not release gate",
              "script": {"path": Path(__file__).relative_to(ROOT).as_posix(), "sha256": digest(Path(__file__).read_bytes())},
              "source_before": before, "actual_http_calls": []}
    try:
        run(report)
    except Exception:
        report["status"] = "failed"; report["failure_traceback"] = traceback.format_exc()
    after = snapshot(); report["source_after"] = after
    report["all_declared_source_inputs_unchanged"] = before == after
    if before != after:
        report["status"] = "failed"
        report["source_mutation_failure"] = "production input changed during actual probe; not a passing frozen-source execution"
    report["elapsed_s"] = time.perf_counter()-start
    report["actual_http_request_count"] = len(report["actual_http_calls"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2); stream.write("\n")
    print(json.dumps({"status": report["status"], "actual_http_request_count": report["actual_http_request_count"],
                      "elapsed_s": report["elapsed_s"], "source_count": len(before), "source_unchanged": before == after,
                      "report": str(output), "report_sha256": digest(output.read_bytes())}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
