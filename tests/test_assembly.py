from copy import deepcopy
import json
import pytest

from oceanroute.assembly import import_assembly, export_assembly
from oceanroute.core import sample_project, analyze_project


def project():
    p = sample_project()
    p["route"]["points"] = [{"id": "p1", "label": "A", "longitude": 0, "latitude": 0, "depth_m": None},
                            {"id": "p2", "label": "B", "longitude": .01, "latitude": 0, "depth_m": None}]
    p["route"]["legs"] = [{"cable_type_id": "LW", "slack_pct": 1}]
    p["profile"] = {"samples": [], "source": "none"}
    p["bodies"], p["events"], p["layers"] = [], [], []
    return p


TEXT = "kind,id,name,cable_type_id,length_m,cost,start_m\ncable,c1,轻型缆,LW,100,,\nbody,b1,中继器,,10,50,\nreference,r1,测试参考,,,,100\ncable,c2,铠装缆,DA,200,,\nbody,b2,尾部接头,,0,20,\n"
CONFIG = {"position_mode": "relative", "mapping_policy": "surface_fraction"}


def test_actual_manufacturing_material_body_and_geometry_accounting():
    original = project()
    previous = deepcopy(original)
    result = import_assembly(original, TEXT, CONFIG)
    a = analyze_project(result["project"])
    assert original == previous
    assert a["summary"]["cable_length_m"] == pytest.approx(310)
    assert a["summary"]["material_length_m"] == pytest.approx(300)
    assert a["summary"]["body_length_m"] == pytest.approx(10)
    assert a["summary"]["body_cost"] == pytest.approx(70)
    assert result["report"]["input_material_by_type_m"] == {"LW": 100, "DA": 200}
    assert a["summary"]["surface_length_m"] == pytest.approx(analyze_project(original)["summary"]["surface_length_m"])
    assert result["project"]["assembly_references"][0]["cable_kp_m"] == 100
    assert len(a["bodies"]) == 2
    json.dumps(result, allow_nan=False)


def test_csv_manufacturing_roundtrip_restores_material_and_costs():
    made = import_assembly(project(), TEXT, CONFIG)["project"]
    csv = export_assembly(made)
    rebuilt = import_assembly(project(), csv, {**CONFIG, "position_mode": "absolute"})["project"]
    before, after = analyze_project(made), analyze_project(rebuilt)
    for key in ("cable_length_m", "material_length_m", "body_length_m", "material_cost", "body_cost"):
        assert after["summary"][key] == pytest.approx(before["summary"][key])
    assert rebuilt["assembly_references"][0]["name"] == "测试参考"


def test_append_bodies_keeps_existing_manufacturing_and_reports_removed_material():
    made = import_assembly(project(), TEXT, CONFIG)["project"]
    before = analyze_project(made)
    result = import_assembly(made, "kind,id,name,start_m,length_m,cost\nbody,b3,测试体,250,5,30", {"operation": "append_bodies", "position_mode": "absolute"})
    after = analyze_project(result["project"])
    assert after["summary"]["cable_length_m"] == before["summary"]["cable_length_m"]
    assert after["summary"]["material_length_m"] == pytest.approx(295)
    assert after["summary"]["body_cost"] == pytest.approx(100)
    assert len(after["bodies"]) == 3


def test_append_absolute_end_is_a_real_finite_body_not_a_zero_length_joint():
    made = import_assembly(project(), TEXT, CONFIG)["project"]
    result = import_assembly(made, "kind,id,start_m,end_m,cost\nbody,finite,250,260,30", {
        "operation": "append_bodies", "position_mode": "absolute"})
    after = analyze_project(result["project"])
    assert after["summary"]["cable_length_m"] == 310
    assert after["summary"]["material_length_m"] == pytest.approx(290)
    assert next(b for b in result["project"]["bodies"] if b["id"] == "finite")["length_m"] == 10
    with pytest.raises(ValueError, match="绝对起止"):
        import_assembly(made, "kind,start_m,end_m,length_m\nbody,250,260,5", {
            "operation": "append_bodies", "position_mode": "absolute"})


@pytest.mark.parametrize("row", ["reference,250,260,0", "reference,250,250,10"])
def test_reference_cannot_silently_discard_declared_physical_span(row):
    with pytest.raises(ValueError, match="参考点"):
        import_assembly(project(), "kind,start_m,end_m,length_m\n"+row, {
            "operation": "append_bodies", "position_mode": "absolute"})


def test_unknown_factory_name_requires_explicit_mapping_and_units_are_converted():
    text = "kind,cable_type_id,length_m\ncable,factory-01,100"
    with pytest.raises(ValueError, match="未知缆型"):
        import_assembly(project(), text, CONFIG)
    result = import_assembly(project(), text, {**CONFIG, "type_mapping": {"factory-01": "LW"}, "length_units": "ft"})
    assert result["report"]["manufactured_total_m"] == pytest.approx(30.48)


@pytest.mark.parametrize("text", [
    "kind,cable_type_id,start_m,end_m\ncable,LW,0,100\ncable,DA,101,200",
    "kind,cable_type_id,start_m,end_m\ncable,LW,0,100\ncable,DA,90,200",
    "kind,cable_type_id,start_m,end_m\ncable,WRONG,0,100",
    "kind,id,cable_type_id,start_m,end_m\ncable,x,LW,0,100\ncable,x,DA,100,200",
])
def test_invalid_factory_assembly_rejected_atomically(text):
    p = project()
    original = deepcopy(p)
    with pytest.raises(ValueError):
        import_assembly(p, text, {**CONFIG, "position_mode": "absolute"})
    assert p == original


def test_mapping_intent_required_and_path_links_not_silently_replaced():
    with pytest.raises(ValueError, match="显式选择"):
        import_assembly(project(), TEXT)
    p = project()
    p["route"]["path_links"] = [{"point_id": "p1", "cable_kp_m": 0}]
    with pytest.raises(ValueError, match="Path Link"):
        import_assembly(p, TEXT, CONFIG)
    assert not import_assembly(p, TEXT, {**CONFIG, "clear_path_links": True})["project"]["route"].get("path_links")
