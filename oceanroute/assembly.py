"""Manufacturing CSV preview and explicit independent route/assembly mapping."""
from __future__ import annotations

from copy import deepcopy
import bisect
import csv
import hashlib
import io
import math
from uuid import uuid4

from .core import analyze_project
from .exchange import _table, _cell
from .geodesy import finite_number
from .tools import _insert_kps
from .units import length_factor


def _numeric(row, field, default=None, factor=1):
    value = row.get(field)
    if value is None or str(value).strip() == "":
        if default is None:
            raise ValueError(f"缺少{field}")
        return default
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}不是数值") from exc
    return finite_number(value * factor, field, minimum=0)


def _items(project, text, config, before):
    reader, headers = _table(text, config.get("delimiter"))
    reader.fieldnames = [str(v).strip().lower() for v in reader.fieldnames]
    mode = config.get("position_mode", "relative")
    append = config.get("operation", "replace") == "append_bodies"
    if mode not in ("relative", "absolute") or append and mode != "absolute":
        raise ValueError("装配位置模式须relative/absolute，追加附件必须absolute")
    factor, cursor = length_factor(config.get("length_units", "m")), 0.0
    types = {c["id"]: c for c in project["cable_types"]}
    type_mapping = config.get("type_mapping", {})
    if not isinstance(type_mapping, dict):
        raise ValueError("type_mapping须为对象")
    items, ids = [], set()
    for number, row in enumerate(reader, 2):
        try:
            if None in row:
                raise ValueError("列数超过表头")
            kind = (row.get("kind") or "cable").strip().lower()
            if kind not in {"cable", "allowance", "body", "reference"} or append and kind not in {"body", "reference"}:
                raise ValueError("kind须为cable/allowance/body/reference；追加模式仅body/reference")
            key = row.get("id") or str(uuid4())
            if key in ids:
                raise ValueError("装配id重复")
            ids.add(key)
            if kind == "reference":
                start = _numeric(row, "start_m", cursor, factor)
                length = 0.0
                if row.get("length_m") not in (None, "") and _numeric(row, "length_m", factor=factor) != 0:
                    raise ValueError("参考点必须为零长度")
                if row.get("end_m") not in (None, "") and abs(_numeric(row, "end_m", factor=factor)-start) > 1e-6:
                    raise ValueError("参考点end_m必须等于start_m")
            elif append:
                start = _numeric(row, "start_m", factor=factor)
                if row.get("end_m") not in (None, ""):
                    length = _numeric(row, "end_m", factor=factor)-start
                    if length < 0:
                        raise ValueError("end_m须不小于start_m")
                    if row.get("length_m") not in (None, "") and abs(_numeric(row, "length_m", factor=factor)-length) > 1e-6:
                        raise ValueError("length_m与绝对起止不一致")
                else:
                    length = _numeric(row, "length_m", 0, factor)
            else:
                start = _numeric(row, "start_m", cursor, factor)
                if abs(start-cursor) > 1e-6:
                    raise ValueError("实物装配必须连续，不能留空或重叠")
                if mode == "absolute" and row.get("end_m") not in (None, ""):
                    end = _numeric(row, "end_m", factor=factor)
                    length = end-start
                    if length < 0:
                        raise ValueError("end_m须不小于start_m")
                    if row.get("length_m") not in (None, "") and abs(_numeric(row, "length_m", factor=factor)-length) > 1e-6:
                        raise ValueError("length_m与绝对起止不一致")
                else:
                    length = _numeric(row, "length_m", 0 if kind == "body" else None, factor)
                if kind in {"cable", "allowance"} and length <= 0:
                    raise ValueError("缆材段必须有正长度")
                cursor = finite_number(start+length, "装配累计长度", minimum=0)
            item = {"id": key, "kind": kind, "name": row.get("name") or row.get("cable_type_id") or kind,
                    "start_m": start, "end_m": start+length, "length_m": length, "note": row.get("note") or ""}
            if kind in {"cable", "allowance"}:
                tid = type_mapping.get(row.get("cable_type_id"), row.get("cable_type_id"))
                if tid not in types:
                    raise ValueError(f"未知缆型{tid}，请在type_mapping中映射到型号库")
                item["cable_type_id"] = tid
            if kind == "body":
                item.update(body_kind=row.get("body_kind") or "body", cost=_numeric(row, "cost", 0))
                for field in ("wet_weight_n", "mass_kg", "diameter_m", "drag_area_m2", "drag_coefficient"):
                    if row.get(field) not in (None, ""):
                        item[field] = _numeric(row, field)
            items.append(item)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"装配CSV第{number}行：{exc}") from exc
        if len(items) > 10_000:
            raise ValueError("装配条目超过10,000")
    total = before["summary"]["cable_length_m"] if append else cursor
    if not items or total <= 0:
        raise ValueError("装配为空或没有正实物长度")
    if any(item["end_m"] > total+1e-6 for item in items):
        raise ValueError("参考点/附件位置超出实物总长")
    return items, total


def import_assembly(project, text, config=None):
    """Generate a preview; explicit application is performed by the caller."""
    config = config or {}
    if not isinstance(config, dict):
        raise ValueError("装配config须为对象")
    operation = config.get("operation", "replace")
    if operation not in {"replace", "append_bodies"}:
        raise ValueError("装配operation须replace或append_bodies")
    before = analyze_project(project)
    items, total = _items(project, text, config, before)
    warnings = []
    source_project = project
    if project["route"].get("constraint_state"):
        if not config.get("clear_path_links", False):
            raise ValueError("实制回写会改变约束域；须显式clear_path_links并重新配置，不能绕过域约束")
        source_project = deepcopy(project)
        source_project["route"].pop("constraint_state", None)
        source_project["route"].pop("path_links", None)
        for point in source_project["route"]["points"]:
            point.pop("constraint", None)
        warnings.append({"code": "MANUFACTURING_CLEAR_CONSTRAINTS", "severity": "warning", "message": "已显式移除旧材料约束状态与Path Link；请以新装配重新配置域"})
    if operation == "replace":
        if config.get("mapping_policy") != "surface_fraction":
            raise ValueError("全替换须显式选择mapping_policy=surface_fraction；不能猜测制造缆与路线链接")
        if project["route"].get("path_links") and not config.get("clear_path_links", False):
            raise ValueError("工程有Path Link域；须显式clear_path_links再重建链接，不能静默覆盖约束")
        distance = before["summary"]["surface_length_m"]
        if distance <= 0:
            raise ValueError("制造装配映射需要非零路线长度")
        boundaries = sorted({item[key] * distance/total for item in items if item["kind"] != "reference" for key in ("start_m", "end_m")})
        result, inserted = _insert_kps(source_project, boundaries, before)
        result["route"].pop("path_links", None)
        result["route"].pop("allowances", None)
        result.pop("allowances", None)
        result["bodies"] = []
        updated = analyze_project({**result, "bodies": []})
        keys = [r["kp_m"] for r in updated["rpl"]]
        material = [item for item in items if item["kind"] in {"cable", "allowance"}]
        starts = [item["start_m"] for item in material]
        fallback = material[0]["cable_type_id"] if material else project["cable_types"][0]["id"]
        assigned = 0.0
        result["route"].update(mode="fixed", slack_basis="surface")
        for index, leg in enumerate(result["route"]["legs"]):
            midpoint = (keys[index]+keys[index+1])*.5*total/distance
            item_index = bisect.bisect_right(starts, midpoint)-1
            tid = material[item_index]["cable_type_id"] if item_index >= 0 else fallback
            length = total-assigned if index == len(result["route"]["legs"])-1 else (keys[index+1]-keys[index])*total/distance
            assigned += length
            leg.update(cable_type_id=tid, mode="fixed", fixed_cable_length_m=length, slack_basis="surface", allowance_m=0)
        result["assembly_references"] = []
        warnings.append({"code": "MANUFACTURING_EXPLICIT_MAPPING", "severity": "warning", "message": "保持原空间曲线，将实制里程按平面距离比例映射为固定段；原Path Link及津贴表示被替换，须复核余缆和附件地理位置"})
    else:
        result = deepcopy(source_project)
        inserted = []
        result.setdefault("assembly_references", [])
    existing_ids = {b["id"] for b in result.get("bodies", [])} | {r["id"] for r in result.get("assembly_references", [])}
    for item in items:
        if item["kind"] not in {"body", "reference"}:
            continue
        item = deepcopy(item)
        if item["id"] in existing_ids:
            raise ValueError("追加附件或参考id与现有实体冲突")
        existing_ids.add(item["id"])
        if item["kind"] == "reference":
            result["assembly_references"].append({**item, "cable_kp_m": item["start_m"]})
        else:
            body = {key: value for key, value in item.items() if key not in {"start_m", "end_m", "body_kind"}}
            body.update(kind=item["body_kind"], cable_kp_m=item["start_m"], length_mode="replace")
            result.setdefault("bodies", []).append(body)
    result["manufacturing"] = {"source": "用户装配CSV", "sha256": hashlib.sha256(text.encode()).hexdigest(), "operation": operation,
                               "position_mode": config.get("position_mode", "relative"), "mapping_policy": config.get("mapping_policy"), "length_units": config.get("length_units", "m")}
    after = analyze_project(result)
    expected = {}
    for item in items:
        if item["kind"] in {"cable", "allowance"}:
            tid = item["cable_type_id"]
            expected[tid] = expected.get(tid, 0)+item["length_m"]
    if operation == "replace":
        actual = {}
        for item in after["sld"]:
            if item["kind"] in {"cable", "allowance"}:
                tid = item["cable_type_id"]
                actual[tid] = actual.get(tid, 0)+item["end_m"]-item["start_m"]
        if abs(after["summary"]["cable_length_m"]-total) > 1e-5 or any(abs(actual.get(tid, 0)-length)>1e-5 for tid, length in expected.items()) or any(tid not in expected and length>1e-5 for tid, length in actual.items()):
            raise ValueError("制造装配映射未通过材料守恒验证")
    return {"project": result, "items": items, "warnings": warnings, "report": {"operation": operation, "inserted_points": inserted,
            "manufactured_total_m": total, "input_material_by_type_m": expected, "before_summary": before["summary"], "after_summary": after["summary"],
            "body_count": len(result.get("bodies", [])), "reference_count": len(result.get("assembly_references", [])), "material_conservation_checked": operation == "replace"}}


def export_assembly(project):
    analysis = analyze_project(project)
    rows = []
    bodies = {body["id"]: body for body in analysis["bodies"]}
    seen = set()
    for item in analysis["sld"]:
        if item["kind"] == "reference":
            continue
        if item["kind"] == "body":
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            body = bodies[item["id"]]
            rows.append({**body, "kind": "body", "body_kind": body.get("kind", "body"), "start_m": body["start_m"], "end_m": body["end_m"]})
        else:
            rows.append({**item, "kind": "cable", "length_m": item["end_m"]-item["start_m"]})
    rows.extend({**item, "kind": "reference", "start_m": item["cable_kp_m"], "end_m": item["cable_kp_m"], "length_m": 0} for item in project.get("assembly_references", []))
    rows.sort(key=lambda v: (v["start_m"], v["kind"] == "cable"))
    columns = ["kind", "id", "name", "cable_type_id", "start_m", "end_m", "length_m", "body_kind", "cost", "wet_weight_n", "mass_kg", "diameter_m", "drag_area_m2", "drag_coefficient", "note"]
    output = io.StringIO(newline="")
    output.write("# OceanRoute制造清单；实物里程m；absolute；非原厂格式\n")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(row.get(key)) for key in columns})
    return "\ufeff" + output.getvalue()
