from copy import deepcopy
import json
import math

import pytest

from oceanroute.core import analyze_project
from oceanroute.exchange import coordinate, export_csv, import_rpl
from oceanroute.geodesy import inverse
from oceanroute.rpl_templates import SCHEMA, dump_template, example_templates, load_template, parse_rpl, validate_template


def delimited(fields=None, **extra):
    return {"schema":SCHEMA,"schema_version":1,"format":"delimited","index_base":0,
        "fields":fields or {"longitude":{"column":0},"latitude":{"column":1}},**extra}


def analysis(result):
    return analyze_project({"route":{**result["route_options"],"points":result["points"],"legs":result["legs"]},
        "cable_types":[{"id":"LW","cost_per_m":10,"lay_speed_m_s":2},{"id":"SA","cost_per_m":20,"lay_speed_m_s":1}],
        "costs":{"currency":"CNY","burial_per_m":2}})


def test_actual_examples_unicode_fixed_two_line_and_declared_csv_dms():
    fixed,csv=example_templates()
    result=parse_rpl(fixed["text"],fixed["template"])
    assert result["can_apply"] and (result["accepted_rows"],result["rejected_rows"])==(3,0)
    assert [p["label"] for p in result["points"]]==["起点","转点","终点"]
    assert result["points"][1]["longitude"]==pytest.approx(118+1/60)
    assert [(r["line_start"],r["line_end"]) for r in result["records"]]==[(2,3),(4,5),(6,7)]
    assert result["metadata"]["cable_distance_origin_m"]==5000
    assert result["metadata"]["slack_change_records"]==[1,3]
    assert [l["fixed_cable_length_m"] for l in result["legs"]]==[1750,1750]
    actual=analysis(result)
    assert actual["summary"]["cable_length_m"]==3500
    assert actual["summary"]["material_cost"]==35000
    assert actual["legs"][0]["surface_slack_pct"]!=1.5  # cumulative length, not a guessed percentage
    assert actual["profile_metadata"]["source"]=="waypoint_linear_approximation"
    other=parse_rpl(csv["text"],csv["template"])
    assert other["points"][0]["label"]=="起点,甲" and other["points"][0]["note"]=="中文,备注"
    actual=analysis(other)
    assert actual["summary"]["cable_length_m"]==pytest.approx(actual["summary"]["surface_length_m"]*1.015)


def test_index_bases_cover_character_and_line_offsets_without_mutating_template():
    fixture=example_templates()[0]; one=fixture["template"]; before=deepcopy(one)
    zero=deepcopy(one); zero["index_base"]=0
    for spec in zero["fields"].values(): spec["line"]-=1; spec["start"]-=1
    a,b=parse_rpl(fixture["text"],one),parse_rpl(fixture["text"],zero)
    assert one==before
    assert [{k:v for k,v in p.items() if k!="id"} for p in a["points"]]==[{k:v for k,v in p.items() if k!="id"} for p in b["points"]]
    assert a["legs"]==b["legs"]


def test_template_save_load_is_json_not_executable_and_schema_is_strict(tmp_path):
    template=example_templates()[0]["template"]
    saved=dump_template(template)
    assert load_template(saved)==validate_template(template)
    bad=deepcopy(template); bad["fields"]["longitude"]["expression"]="__import__('os').system('touch never')"
    with pytest.raises(ValueError,match="位置|可执行"): validate_template(bad)
    with pytest.raises(ValueError): load_template('[]')
    with pytest.raises(ValueError): load_template('{"schema":NaN}')
    assert not (tmp_path/"never").exists()


@pytest.mark.parametrize("key,value",[("index_base",2),("index_base",True),("lines_per_record",0),("header_lines",-1),
    ("error_policy","guess"),("leg_assignment","both"),("schema_version",1.0),("max_records",10001)])
def test_invalid_template_parameters_rejected(key,value):
    t=delimited(); t[key]=value
    with pytest.raises(ValueError): validate_template(t)


def test_fixed_short_bad_record_collect_skip_reject_has_exact_original_source():
    fixture=example_templates()[0]; lines=fixture["text"].splitlines(); lines[3]="坏"
    text="\n".join(lines)
    collect=parse_rpl(text,fixture["template"])
    assert not collect["can_apply"] and collect["accepted_rows"]==2 and collect["rejected_rows"]==1
    problem=collect["errors"][0]
    assert (problem["record"],problem["row"],problem["field"],problem["line_start"],problem["line_end"])==(2,4,"label",4,5)
    assert "短行" in problem["message"]
    skip=parse_rpl(text,{**fixture["template"],"error_policy":"skip"})
    assert skip["can_apply"] and skip["legs"][0]["fixed_cable_length_m"]==3500
    assert any(w["code"]=="RPL_REJECTED_RECORD_BRIDGE" for w in skip["warnings"])
    with pytest.raises(ValueError,match="记录2.*第4行"): parse_rpl(text,{**fixture["template"],"error_policy":"reject"})


def test_incomplete_tail_and_ignored_physical_header_comments_blank_lines():
    fixture=example_templates()[0]; original=fixture["text"].splitlines()
    text="\n".join(["header","ignored second header","; comment",original[1],"",original[2],"# between",original[3],original[4],original[5]])
    result=parse_rpl(text,{**fixture["template"],"header_lines":2})
    assert result["accepted_rows"]==2 and result["rejected_rows"]==1 and not result["can_apply"]
    assert [(r["line_start"],r["line_end"]) for r in result["records"]]==[(4,6),(8,9),(10,10)]
    assert "末尾记录不完整" in result["errors"][0]["message"]


def test_multiline_csv_records_preserve_quoted_newlines_and_comment_text():
    template=delimited({"label":{"line":0,"column":0},"longitude":{"line":0,"column":1},"latitude":{"line":0,"column":2},
        "note":{"line":0,"column":3},"cable_type_id":{"line":1,"column":0},"slack_pct":{"line":1,"column":1},"depth_m":{"line":1,"column":2}},
        lines_per_record=2,header_lines=1,comment_prefixes=["#",";"])
    text='header\n"起点,甲",118,22,"第一行\n#保留的备注"\nLW,1.5,30\n#注释\n终点,118.01,22.01,末尾\nSA,2,40\n'
    result=parse_rpl(text,template)
    assert result["can_apply"] and result["points"][0]["note"]=="第一行\n#保留的备注"
    assert [(r["line_start"],r["line_end"]) for r in result["records"]]==[(2,4),(6,7)]
    assert result["legs"][0]["cable_type_id"]=="LW"
    assert analysis(result)["legs"][0]["surface_slack_pct"]==pytest.approx(1.5)


def test_units_incoming_leg_mapping_and_depth_sign_are_actually_used():
    fields={name:{"column":i} for i,name in enumerate(["longitude","latitude","depth_m","cable_type_id","slack_pct","burial","stop_hours","extra_cost"])}
    t=delimited(fields,leg_assignment="incoming",depth_positive="up",cable_type_map={"轻缆":"LW","铠装":"SA"},
        units={"depth_m":"ft","slack_pct":"fraction","stop_hours":"min"})
    result=parse_rpl("118,22,-100,轻缆,.01,false,0,0\n118.01,22,-200,铠装,.02,true,60,123",t)
    assert result["points"][0]["depth_m"]==pytest.approx(30.48)
    assert result["points"][1]["depth_m"]==pytest.approx(60.96)
    assert result["legs"][0]=={"mode":"flexible","slack_basis":"surface","slack_pct":2,"cable_type_id":"SA","burial":True,"stop_hours":1,"extra_cost":123}
    actual=analysis(result); leg=actual["legs"][0]
    assert actual["summary"]["material_cost"]==pytest.approx(leg["surface_length_m"]*1.02*20)
    assert actual["summary"]["burial_cost"]==pytest.approx(leg["surface_length_m"]*2)
    assert actual["summary"]["time_hours"]==pytest.approx(leg["surface_length_m"]/3600+1)
    assert actual["summary"]["cost_total"]==pytest.approx(actual["summary"]["material_cost"]+actual["summary"]["burial_cost"]+123)


def test_dms_negative_zero_combined_and_split_fields_are_not_lost():
    assert coordinate("-0 30",latitude=True)==-.5
    assert coordinate("-0°30′0″",latitude=False)==-.5
    assert coordinate("0 30 S",latitude=True)==-.5
    assert math.copysign(1,coordinate("-0",latitude=True))==-1
    with pytest.raises(ValueError,match="矛盾"): coordinate("-0 N",latitude=True)
    with pytest.raises(ValueError,match="最后一项"): coordinate("22.0 30",latitude=True)
    assert coordinate("1E2",latitude=False)==100
    with pytest.raises(ValueError): coordinate("1E2 30 E",latitude=False)
    fixture=example_templates()[1]; text=fixture["text"].replace("118,0,0,E,22,0,0,N","-0,30,0,W,-0,30,0,S")
    result=parse_rpl(text,fixture["template"])
    assert result["points"][0]["longitude"]==-.5 and result["points"][0]["latitude"]==-.5


def test_cumulative_material_difference_and_repeated_coordinate_jump():
    t=delimited({"longitude":{"column":0},"latitude":{"column":1},"cable_kp_m":{"column":2}},units={"cable_kp_m":"nm"})
    result=parse_rpl("118,22,6\n118,22,6.1\n118.001,22,6.2",t)
    actual=analysis(result)
    assert actual["summary"]["cable_length_m"]==pytest.approx(.2*1852)
    assert actual["legs"][0]["surface_length_m"]==0
    assert actual["legs"][0]["surface_slack_pct"] is None
    assert actual["rpl"][1]["cable_kp_m"]==pytest.approx(.1*1852)
    assert any(w["code"]=="RPL_ZERO_KP_CABLE_JUMP" for w in result["warnings"])


def test_cumulative_decrease_is_not_coerced_to_zero_or_silently_accepted():
    t=delimited({"longitude":{"column":0},"latitude":{"column":1},"cable_kp_m":{"column":2}},error_policy="skip")
    result=parse_rpl("118,22,100\n118.001,22,90\n118.002,22,400",t)
    assert result["accepted_rows"]==2 and result["rejected_rows"]==1 and result["can_apply"]
    assert result["errors"][0]["field"]=="cable_kp_m" and result["errors"][0]["row"]==2
    assert analysis(result)["summary"]["cable_length_m"]==300


def test_surface_kp_is_source_only_and_dateline_geometry_is_short_arc():
    t=delimited({"longitude":{"column":0},"latitude":{"column":1},"kp_m":{"column":2}},defaults={"curve":"geodesic"})
    result=parse_rpl("179.99,22,1000\n-179.99,22,9999",t)
    distance,_=inverse(179.99,22,-179.99,22,"geodesic")
    assert distance<2500
    assert analysis(result)["summary"]["surface_length_m"]==pytest.approx(distance)
    assert result["records"][1]["computed_route_kp_m"]==pytest.approx(distance)
    assert result["records"][1]["source_kp_m"]==9999
    assert any(w["code"]=="RPL_SOURCE_KP_DIFFERS" for w in result["warnings"])


def test_fixed_fields_and_bottom_slack_must_have_usable_engineering_inputs():
    t=delimited({"longitude":{"column":0},"latitude":{"column":1},"fixed_cable_length_m":{"column":2}})
    result=parse_rpl("118,22,1200\n118.01,22,",t)
    assert result["can_apply"] and analysis(result)["summary"]["cable_length_m"]==1200
    missing=delimited(defaults={"mode":"fixed"})
    result=parse_rpl("118,22\n118.01,22",missing)
    assert not result["can_apply"] and result["errors"][0]["code"]=="RPL_LEG_INVALID"
    bottom=delimited(defaults={"slack_basis":"bottom","slack_pct":1})
    result=parse_rpl("118,22\n118.01,22",bottom)
    assert not result["can_apply"] and "底余缆" in result["errors"][0]["message"]
    bottom["fields"]["depth_m"]={"column":2}
    result=parse_rpl("118,22,30\n118.01,22,100",bottom)
    actual=analysis(result)
    assert result["can_apply"] and actual["legs"][0]["bottom_slack_pct"]==pytest.approx(1)


@pytest.mark.parametrize("value",["NaN","Infinity","-1"])
def test_invalid_depth_record_has_field_and_original_line(value):
    t=delimited({"longitude":{"column":0},"latitude":{"column":1},"depth_m":{"column":2}},header_lines=2)
    result=parse_rpl(f"header\nheader\n118,22,{value}\n118.01,22,10",t)
    assert result["errors"][0]["row"]==3 and result["errors"][0]["field"]=="depth_m"
    assert not result["can_apply"]


def test_budgets_and_unsafe_or_ambiguous_template_fields_are_rejected():
    with pytest.raises(ValueError,match="记录超过"): parse_rpl("118,22\n118.01,22\n118.02,22",delimited(max_records=2))
    with pytest.raises(ValueError,match="32MiB"): parse_rpl("x"*(32*1024*1024+1),delimited())
    t=delimited(); t["fields"]["longitude"]["column"]=512
    with pytest.raises(ValueError): validate_template(t)
    t=example_templates()[0]["template"]; t["fields"]["longitude"]["width"]=0
    with pytest.raises(ValueError): validate_template(t)
    t=delimited(); t["fields"]["longitude_degrees"]={"column":2}
    with pytest.raises(ValueError,match="仅使用"): validate_template(t)
    t=delimited(); t["fields"]["lay_speed_m_s"]={"column":2}
    with pytest.raises(ValueError): validate_template(t)  # no ignored leg-speed field


def test_legacy_quoted_csv_comments_extra_columns_missing_optional_and_mapping():
    text='# preface\n名称,X,Y,备注\n"点,一",118,22,"第一行\n#原备注"\n坏,181,22,bad\n多列,118,22,ok,extra\n终点,118.01,22\n'
    result=import_rpl(text,mapping={"longitude":"X","latitude":"Y"})
    assert result["can_apply"] and result["accepted_rows"]==2 and result["rejected_rows"]==2
    assert result["points"][0]["note"]=="第一行\n#原备注" and result["points"][1]["note"]==""
    assert [w["row"] for w in result["errors"]]==[5,6]
    assert not import_rpl(text,mapping={"longitude":"X","latitude":"Y"},error_policy="collect")["can_apply"]
    with pytest.raises(ValueError): import_rpl(text,mapping={"longitude":"X","latitude":"Y"},error_policy="reject")


def test_exported_rpl_reimports_actual_material_fields_without_quoted_csv_regression():
    project={"name":"test","route":{"points":[{"id":"a","longitude":118,"latitude":22,"label":"a"},
        {"id":"b","longitude":118.01,"latitude":22,"label":"b"}],"legs":[{"cable_type_id":"LW","slack_pct":2}]},
        "cable_types":[{"id":"LW","cost_per_m":12}]}
    before=analyze_project(project)
    result=import_rpl(export_csv(project,before))
    assert result["can_apply"] and result["legs"][0]["mode"]=="fixed" and result["legs"][0]["cable_type_id"]=="LW"
    assert analysis(result)["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"])


@pytest.mark.parametrize("positions",[[(1e-8,-1e-8),(2e-8,-2e-8)],[(179.99999999,1e-8),(-179.99999999,-1e-8)]])
def test_real_export_import_scientific_near_zero_and_date_line(positions):
    project={"route":{"curve":"geodesic","points":[{"id":str(i),"longitude":lon,"latitude":lat} for i,(lon,lat) in enumerate(positions)],
        "legs":[{"cable_type_id":"LW","slack_pct":2}]},"cable_types":[{"id":"LW"}]}
    before=analyze_project(project)
    text=export_csv(project,before)
    assert "e-08" in text
    result=import_rpl(text)
    assert result["can_apply"] and result["rejected_rows"]==0
    assert [(p["longitude"],p["latitude"]) for p in result["points"]]==positions
    assert analysis(result)["summary"]["cable_length_m"]==pytest.approx(before["summary"]["cable_length_m"],abs=1e-12)
    with pytest.raises(ValueError): coordinate("1e309",latitude=False)


def test_csv_syntax_error_keeps_precise_source_and_disables_incomplete_preview():
    t=delimited(header_lines=1)
    result=parse_rpl('lon,lat\n118,22\n"118.01,22',t)
    assert result["errors"][0]["code"]=="RPL_CSV_INVALID"
    assert result["errors"][0]["row"]==3 and not result["can_apply"]
    result=parse_rpl('lon,lat\n118,22\n118.01,22\n"118.02,22',{**t,"error_policy":"skip"})
    assert result["accepted_rows"]==2 and not result["can_apply"]  # malformed quoting cannot resynchronize safely


def test_bom_and_unicode_emoji_codepoints_not_utf8_bytes_or_display_cells():
    fixture=example_templates()[0]
    lines=fixture["text"].splitlines()
    lines[1]=f"{'点🌊':<6}{'118 E':<14}{'22 N':<14}"
    result=parse_rpl("\ufeff"+"\n".join(lines),fixture["template"])
    assert result["can_apply"] and result["points"][0]["label"]=="点🌊"
    assert result["points"][0]["longitude"]==118


def test_real_http_template_load_validate_examples_parse_and_analysis(tmp_path):
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    with TestClient(create_app(ProjectStore(tmp_path/"projects.sqlite3"))) as client:
        response=client.get("/api/import/rpl/templates")
        assert response.status_code==200,response.text
        fixed=response.json()["examples"][0]
        checked=client.post("/api/import/rpl/template",json={"template":fixed["template"]})
        assert checked.status_code==200,checked.text
        loaded=client.post("/api/import/rpl/template",json={"text":dump_template(fixed["template"])})
        assert loaded.status_code==200 and loaded.json()["template"]==checked.json()["template"]
        response=client.post("/api/import/rpl",json=fixed)
        assert response.status_code==200,response.text
        result=response.json(); assert result["can_apply"]
        project={"route":{**result["route_options"],"points":result["points"],"legs":result["legs"]},"cable_types":[{"id":"LW","cost_per_m":10}]}
        actual=client.post("/api/analyze",json=project)
        assert actual.status_code==200,actual.text
        assert actual.json()["summary"]["cable_length_m"]==3500 and actual.json()["summary"]["material_cost"]==35000
        for invalid in ([],{"schema":"original.native.template"},{**fixed["template"],"python":"exec('x')"}):
            assert client.post("/api/import/rpl/template",json={"template":invalid}).status_code==422
        assert client.post("/api/import/rpl",json={**fixed,"error_policy":"skip"}).status_code==422
        for policy,expected,apply in (("collect",200,False),("skip",200,True),("reject",422,None)):
            response=client.post("/api/import/rpl",json={"text":"lon,lat\n118,22\n181,22\n118.01,22","error_policy":policy})
            assert response.status_code==expected,response.text
            if apply is not None: assert response.json()["can_apply"] is apply
