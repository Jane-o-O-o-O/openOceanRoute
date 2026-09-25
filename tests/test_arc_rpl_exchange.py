"""Own CSV arc extension is endpoint-bound, not a native Makai format."""
from copy import deepcopy
import csv
import io
import json

import pytest

from oceanroute.core import analyze_project
from oceanroute.exchange import export_csv, import_rpl
from oceanroute.geodesy import GEOD
from oceanroute.rpl_templates import parse_rpl


def project(sweep=180.):
    center=(0., 50.); radius=500.
    start=GEOD.fwd(*center,-90.,radius)[:2]
    end=GEOD.fwd(*center,-90.+sweep,radius)[:2]
    return {"id":"csv-arc", "crs":"EPSG:4326", "route":{"curve":"geodesic", "mode":"fixed",
                "slack_basis":"surface", "slack_pct":0,
                "points":[{"id":"start","longitude":start[0],"latitude":start[1],"depth_m":50},
                          {"id":"end","longitude":end[0],"latitude":end[1],"depth_m":50}],
                "legs":[{"cable_type_id":"A","fixed_cable_length_m":4000,
                         "geometry":{"type":"circular_arc","schema_version":1,"center":list(center),
                                     "radius_m":radius,"start_azimuth_deg":-90.,"sweep_deg":sweep}}]},
            "cable_types":[{"id":"A","name":"A","cost_per_m":1,"lay_speed_m_s":1}], "bodies":[]}


def data_rows(text):
    return list(csv.DictReader(io.StringIO('\n'.join(l for l in text.lstrip('\ufeff').splitlines() if not l.startswith('#')))))


def csv_rows(rows):
    out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return out.getvalue()


@pytest.mark.parametrize("sweep", [180., -90., 360.])
def test_actual_export_import_keeps_intrinsic_length_geometry_and_manufacturing(sweep):
    p=project(sweep); a=analyze_project(p); text=export_csv(p,a)
    result=import_rpl(text,error_policy="reject")
    assert result["can_apply"]
    q=deepcopy(p);q["route"].update(result["route_options"],points=result["points"],legs=result["legs"])
    restored=analyze_project(q)
    assert restored["summary"]["surface_length_m"] == pytest.approx(a["summary"]["surface_length_m"],abs=1e-7)
    assert restored["summary"]["cable_length_m"] == 4000
    assert restored["legs"][0]["geometry"] == a["legs"][0]["geometry"]
    assert result["route_options"]["curve"] == "geodesic"
    assert not any(w["code"] in {"RPL_SOURCE_KP_DIFFERS","RPL_ZERO_KP_CABLE_JUMP"} for w in result["warnings"])
    assert len(result["points"]) == 2
    assert data_rows(text)[-1]["leg_geometry_json"] == ""


@pytest.mark.parametrize("mutation", ["wrong_endpoint", "bad_schema", "terminal", "nan", "invalid_json"])
def test_geometry_loss_or_invalid_binding_never_applies_as_a_chord(mutation):
    p=project(); rows=data_rows(export_csv(p,analyze_project(p)))
    if mutation=="wrong_endpoint": rows[-1]["latitude"] = str(float(rows[-1]["latitude"])+.001)
    elif mutation=="bad_schema":
        g=json.loads(rows[0]["leg_geometry_json"]);g["schema_version"]=True;rows[0]["leg_geometry_json"]=json.dumps(g)
    elif mutation=="terminal": rows[-1]["leg_geometry_json"]=rows[0]["leg_geometry_json"]
    elif mutation=="nan": rows[0]["leg_geometry_json"] = rows[0]["leg_geometry_json"].replace('500.0', 'NaN')
    else: rows[0]["leg_geometry_json"]='{"broken":'
    result=import_rpl(csv_rows(rows),error_policy="collect")
    assert not result["can_apply"]
    assert result["errors"]
    with pytest.raises(ValueError):
        import_rpl(csv_rows(rows),error_policy="reject")


def test_incoming_template_geometry_attaches_to_the_actual_preceding_leg():
    p=project(); rows=data_rows(export_csv(p,analyze_project(p)))
    rows[-1]["leg_geometry_json"],rows[0]["leg_geometry_json"]=rows[0]["leg_geometry_json"],""
    baseline=import_rpl(export_csv(p,analyze_project(p)),error_policy="reject")
    template=deepcopy(baseline["template"]);template.update(header_lines=1,leg_assignment="incoming")
    result=parse_rpl(csv_rows(rows),template)
    assert result["can_apply"] and result["legs"][0]["geometry"]["radius_m"] == 500


def test_inconsistent_global_curves_are_rejected_and_plain_csv_contract_is_unchanged():
    p=project();rows=data_rows(export_csv(p,analyze_project(p)))
    rows[-1]["route_curve"]="rhumb"
    with pytest.raises(ValueError,match="route_curve"):
        import_rpl(csv_rows(rows))
    p["route"]["legs"][0].pop("geometry")
    text=export_csv(p,analyze_project(p))
    assert "leg_geometry_json" not in text and "route_curve" not in text
