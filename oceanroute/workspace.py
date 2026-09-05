"""Schema 2: one engineering workspace, shared resources and manufacturing relations.

Cable Paths remain schema-1 child entities. An assembly is a physical ordered
inventory, not another project. Explicit alternatives may refer to one physical
inventory, while only its deployment path contributes installation expenditure.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from uuid import uuid4

from .core import analyze_project
from .geodesy import finite_number, inverse

TOL = 1e-5
BODY_PROPERTIES = ("wet_weight_n", "mass_kg", "diameter_m", "drag_area_m2", "drag_coefficient")
MAX_GEOMETRY_VERTICES = 250000


class WorkspaceError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _error(code, message):
    raise WorkspaceError(code, message)


def _identifier(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        _error("WORKSPACE_ID", f"{field}须为1～128字符非空标识")
    return value


def _object(value, field):
    if not isinstance(value, dict):
        _error("WORKSPACE_STRUCTURE", f"{field}须为对象")
    return value


def _array(value, field, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        _error("WORKSPACE_LIMIT", f"{field}须为数组，最多{maximum}项")
    return value


def _unique(values, field):
    ids = [_identifier(_object(v, field).get("id"), field+".id") for v in values]
    if len(ids) != len(set(ids)):
        _error("WORKSPACE_DUPLICATE_ID", f"{field}中的ID不能重复")
    return {v["id"]: v for v in values}


def _canonical(value):
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 8)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def _signature(items):
    value = _physical(items)
    return hashlib.sha256(json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _equivalent(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a-b) <= TOL
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equivalent(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equivalent(x, y) for x, y in zip(a, b))
    return a == b


def _physical(items):
    # Cable partition IDs and editorial names do not define the manufactured
    # cable; body/reference identities and positions do.
    values = [{k: v for k, v in i.items() if k not in {"name", "note", "original_kind"} and not (k == "id" and i["kind"] == "cable")} for i in items]
    cables = sorted([i for i in values if i["kind"] == "cable"], key=lambda i: i["start_m"])
    merged = []
    for item in cables:
        if merged and merged[-1]["cable_type_id"] == item["cable_type_id"] and abs(merged[-1]["end_m"]-item["start_m"]) <= TOL:
            merged[-1]["end_m"] = item["end_m"]
            merged[-1]["length_m"] = merged[-1]["end_m"]-merged[-1]["start_m"]
        else:
            merged.append(item)
    # Equality uses canonical physical runs, but never deletes the actual
    # manufacturing partition IDs stored in the assembly inventory.
    result = merged + [i for i in values if i["kind"] != "cable"]
    result.sort(key=lambda i: (i["start_m"], i["kind"] == "cable", i["end_m"], i.get("id", "")))
    return result


def _normalize_items(items, types):
    output = []
    for original in _array(items, "assembly.items", 10000):
        item = deepcopy(_object(original, "assembly.item"))
        item["id"] = _identifier(item.get("id"), "assembly.item.id")
        kind = item.get("kind")
        if kind not in {"cable", "body", "reference"}:
            _error("WORKSPACE_ASSEMBLY_KIND", "制造item仅支持cable/body/reference")
        start = finite_number(item.get("start_m"), "start_m", minimum=0)
        end = finite_number(item.get("end_m"), "end_m", minimum=start)
        if "length_m" in item and abs(finite_number(item["length_m"], "length_m", minimum=0)-(end-start)) > TOL:
            _error("WORKSPACE_ASSEMBLY_LENGTH", "制造条目的长度与起止端不一致")
        normalized = {"id": item["id"], "kind": kind, "name": str(item.get("name", kind)),
                      "start_m": start, "end_m": end, "length_m": end-start, "note": str(item.get("note", ""))}
        if kind == "reference":
            if end-start > TOL:
                _error("WORKSPACE_REFERENCE_LENGTH", "制造参考不得占用实物长度")
            normalized["end_m"] = start
            normalized["length_m"] = 0.0
        elif kind == "cable":
            tid = str(item.get("cable_type_id", ""))
            if tid not in types or end-start <= 0:
                _error("WORKSPACE_CABLE_TYPE", "制造缆材须引用共享库中的型号且长度为正")
            normalized["cable_type_id"] = tid
        else:
            normalized.update(body_kind=str(item.get("body_kind", "body")), cost=finite_number(item.get("cost", 0), "body.cost", minimum=0))
            properties = _object(item.get("properties", {}), "body.properties")
            # Net submerged weight is signed: buoyancy may exceed gravity.
            # Inertia and geometry remain nonnegative physical quantities.
            normalized["properties"] = {
                k: finite_number(v, k, minimum=None if k == "wet_weight_n" else 0)
                for k, v in properties.items() if k in BODY_PROPERTIES
            }
        output.append(normalized)
    _unique(output, "assembly.items")
    output.sort(key=lambda i: (i["start_m"], i["kind"] == "cable", i["end_m"], i["id"]))
    occupied = sorted([i for i in output if i["kind"] != "reference" and i["length_m"] > 0], key=lambda i: (i["start_m"], i["end_m"]))
    cursor = 0.0
    for item in occupied:
        if abs(item["start_m"]-cursor) > TOL:
            _error("WORKSPACE_ASSEMBLY_GAP", "制造装配必须从0连续覆盖，无重叠或空隙")
        cursor = item["end_m"]
    if cursor <= 0:
        _error("WORKSPACE_ASSEMBLY_EMPTY", "制造装配须有正实物总长")
    if any(i["end_m"] > cursor+TOL for i in output):
        _error("WORKSPACE_ASSEMBLY_RANGE", "附件/参考超出制造总长")
    return output, cursor


def _manufacturing(project, analysis):
    items = []
    for part in analysis["sld"]:
        if part["kind"] in {"cable", "allowance"}:
            items.append({"id": str(uuid4()), "kind": "cable", "start_m": part["start_m"], "end_m": part["end_m"],
                          "cable_type_id": part["cable_type_id"], "name": part.get("name", "cable")})
    for body in analysis["bodies"]:
        items.append({"id": body["id"], "kind": "body", "name": body.get("name", body["id"]),
                      "start_m": body["start_m"], "end_m": body["end_m"], "body_kind": body.get("kind", "body"),
                      "cost": body["cost"], "properties": {k: body[k] for k in BODY_PROPERTIES if k in body}, "note": body.get("note", "")})
    for ref in analysis.get("assembly_references", []):
        items.append({"id": ref["id"], "kind": "reference", "name": ref.get("name", ref["id"]),
                      "start_m": ref["cable_kp_m"], "end_m": ref["cable_kp_m"], "note": ref.get("note", "")})
    return _normalize_items(items, {str(t["id"]): t for t in project["cable_types"]})[0]


def _assembly_from(project, analysis, name, identifier=None):
    return {"id": identifier or str(uuid4()), "name": name, "currency": project.get("costs", {}).get("currency", "CNY"),
            "items": _manufacturing(project, analysis), "total_length_m": analysis["summary"]["cable_length_m"],
            "source": "captured_path_manufacturing", "source_path_id": project["id"]}


def _strip(project, identifier, name):
    result = deepcopy(project)
    for key in ("cable_types", "layers", "terrain_sources", "saved_revision", "workspace_context"):
        result.pop(key, None)
    result.update(id=identifier, name=name, schema_version=1)
    return result


def _materialize(workspace, path):
    project = deepcopy(path["project"])
    project.update(schema_version=1, id=path["id"], name=path["name"], cable_types=deepcopy(workspace["cable_types"]), layers=deepcopy(workspace["layers"]))
    project["terrain_sources"] = deepcopy(workspace.get("terrain_sources", []))
    project.pop("saved_revision", None)
    relation = next((l for l in workspace["associations"] if l["path_id"] == path["id"]), None)
    project["workspace_context"] = {"workspace_id": workspace["id"], "path_id": path["id"], "assembly_id": relation["assembly_id"] if relation else None,
                                    "role": relation["role"] if relation else None, "workspace_revision": workspace.get("saved_revision")}
    return project


def _geometry_work(project):
    """Budget the core's exact default densification before allocating vertices."""
    route = _object(project.get("route"), "route")
    raw = _array(route.get("points"), "route.points", 10000)
    points = [_object(p, "route.point") for p in raw]
    vertices = 1 if points else 0
    for a, b in zip(points, points[1:]):
        distance, _ = inverse(a.get("longitude"), a.get("latitude"), b.get("longitude"), b.get("latitude"), route.get("curve", "rhumb"))
        vertices += max(1, min(1000, math.ceil(distance/5000)))
        if vertices > MAX_GEOMETRY_VERTICES:
            _error("WORKSPACE_LIMIT", "工作区全部路径地图加密预算为250,000个顶点；请减少远距折返或拆分工程")
    return vertices


def _validate(workspace):
    ws = deepcopy(_object(workspace, "workspace"))
    raw = json.dumps(ws, ensure_ascii=False, allow_nan=False)
    if len(raw.encode()) > 32*1024*1024:
        _error("WORKSPACE_LIMIT", "完整工作区JSON最多32MB")
    if ws.get("schema_version") != 2:
        _error("WORKSPACE_SCHEMA", "工作区须使用schema_version=2，schema1请显式迁移")
    ws["id"] = _identifier(ws.get("id"), "workspace.id")
    ws["name"] = str(ws.get("name", "多路径海缆工程"))
    if len(ws["name"]) > 512:
        _error("WORKSPACE_LIMIT", "工作区名称最多512字符")
    currency = _identifier(ws.get("currency", "CNY"), "currency")
    revision = ws.get("saved_revision")
    if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
        _error("WORKSPACE_REVISION", "saved_revision须为已保存的正整数修订")
    ws["currency"] = currency
    types = _unique(_array(ws.get("cable_types"), "cable_types", 10000), "cable_types")
    if not types:
        _error("WORKSPACE_CABLE_TYPE", "工作区须有共享缆材库")
    for value in types.values():
        if value.get("currency", currency) != currency:
            _error("WORKSPACE_CURRENCY", "工作区库/路径/装配须同币种，不能混币或自动换汇")
        finite_number(value.get("cost_per_m", 0), "cost_per_m", minimum=0)
        speed = finite_number(value.get("lay_speed_m_s", 1), "lay_speed_m_s", minimum=0)
        if speed == 0: _error("WORKSPACE_CABLE_TYPE", "缆型推荐速度须为正，停时属于路径事件")
        for key in ("diameter_m", "wet_weight_n_m", "ea_n", "ei_n_m2", "max_tension_n", "min_bend_radius_m"):
            if key in value: finite_number(value[key], key, minimum=0)
    _unique(_array(ws.get("layers", []), "layers", 1000), "layers")
    ws.setdefault("layers", [])
    from .terrain_sources import normalize_sources, _library_signature
    ws["terrain_sources"] = normalize_sources(ws.get("terrain_sources", []))
    terrain_signature = _library_signature(ws["terrain_sources"])
    paths = _unique(_array(ws.get("paths", []), "paths", 100), "paths")
    assemblies = _unique(_array(ws.get("assemblies", []), "assemblies", 100), "assemblies")
    links = _array(ws.get("associations", []), "associations", 100)
    _unique(links, "associations")
    ws.setdefault("paths", []); ws.setdefault("assemblies", []); ws.setdefault("associations", [])
    active = ws.get("active_path_id")
    if active is not None and active not in paths or paths and active is None:
        _error("WORKSPACE_ACTIVE_PATH", "有路径时active_path_id须指向本工程路径")
    if not paths and active is not None:
        _error("WORKSPACE_ACTIVE_PATH", "空工作区活跃路径须为null")
    item_ids = set(); reports = {}
    for assembly in assemblies.values():
        if assembly.get("currency", currency) != currency:
            _error("WORKSPACE_CURRENCY", "装配币种与工作区不同")
        items, total = _normalize_items(assembly.get("items"), types)
        if assembly.get("total_length_m") is not None and abs(finite_number(assembly["total_length_m"], "total_length_m", minimum=0)-total) > TOL:
            _error("WORKSPACE_ASSEMBLY_LENGTH", "装配总长与实际制造条目不符")
        for item in items:
            if item["id"] in item_ids:
                _error("WORKSPACE_MANUFACTURING_ID", "不同装配不得暗中复用同一制造实体ID；替代路径请关联同一个装配")
            item_ids.add(item["id"])
        assembly.update(items=items, currency=currency, total_length_m=total, manufacturing_signature=_signature(items))
        lengths = {}
        for item in items:
            if item["kind"] == "cable":
                tid = item["cable_type_id"]; lengths[tid] = lengths.get(tid, 0)+item["length_m"]
        material_cost = sum(length*finite_number(types[tid].get("cost_per_m", 0), "cost_per_m", minimum=0) for tid, length in lengths.items())
        body_cost = sum(i.get("cost", 0) for i in items if i["kind"] == "body")
        reports[assembly["id"]] = {"assembly_id": assembly["id"], "name": assembly.get("name", assembly["id"]), "total_length_m": total,
            "material_length_m": sum(lengths.values()), "body_length_m": sum(i["length_m"] for i in items if i["kind"] == "body"),
            "material_by_type_m": lengths, "material_cost": material_cost, "body_cost": body_cost, "procurement_cost": material_cost+body_cost,
            "body_count": sum(i["kind"] == "body" for i in items), "reference_count": sum(i["kind"] == "reference" for i in items),
            "manufacturing_signature": assembly["manufacturing_signature"], "currency": currency}
    if sum(len(a["items"]) for a in assemblies.values()) > 100000:
        _error("WORKSPACE_LIMIT", "所有装配累计最多100,000条目")
    seen_paths = set(); deployments = set()
    for link in links:
        if link.get("path_id") not in paths or link.get("assembly_id") not in assemblies:
            _error("WORKSPACE_RELATION", "路径—装配关联引用不存在的实体")
        if link["path_id"] in seen_paths:
            _error("WORKSPACE_RELATION", "同路径只能关联一套完整制造装配")
        if link.get("role") not in {"deployment", "alternative"}:
            _error("WORKSPACE_RELATION", "关联role须deployment/alternative")
        if link["role"] == "deployment":
            if link["assembly_id"] in deployments:
                _error("WORKSPACE_DOUBLE_DEPLOYMENT", "同一实物装配不能同时分配给两条敷设路径")
            deployments.add(link["assembly_id"])
        seen_paths.add(link["path_id"])
    if any(link["role"] == "alternative" and link["assembly_id"] not in deployments for link in links):
        _error("WORKSPACE_DEPLOYMENT_REQUIRED", "替代路径必须有明确的同装配deployment主路径")
    analyses = {}; points = 0; geometry_vertices = 0
    for path in paths.values():
        if path.get("kind", "cable") != "cable":
            _error("WORKSPACE_PATH_KIND", "当前关系库管理Cable Path；调查成果仍是独立GIS，不伪造可编辑As-Laid路径")
        path["kind"] = "cable"; path["name"] = str(path.get("name", path["id"]))
        child = _object(path.get("project"), "path.project")
        if any(k in child for k in ("cable_types", "layers", "terrain_sources", "saved_revision")):
            _error("WORKSPACE_SHARED_RESOURCE", "子路径不得保存共享库/图层/独立修订，须通过工作区同步")
        if child.get("id") != path["id"] or child.get("schema_version") != 1:
            _error("WORKSPACE_PATH_ID", "schema1子实体id必须等于path.id")
        if child.get("crs", "EPSG:4326") != "EPSG:4326":
            _error("WORKSPACE_CRS", "子路径须为WGS84")
        costs = _object(child.setdefault("costs", {}), "path.costs")
        if costs.get("currency", currency) != currency:
            _error("WORKSPACE_CURRENCY", "路径币种与工作区不同")
        costs["currency"] = currency
        points += len(child.get("route", {}).get("points", []))
        if points > 50000:
            _error("WORKSPACE_LIMIT", "工作区所有路径累计最多50,000路线点")
        geometry_vertices += _geometry_work(child)
        if geometry_vertices > MAX_GEOMETRY_VERTICES:
            _error("WORKSPACE_LIMIT", "工作区全部路径地图加密预算为250,000个顶点；请拆分工程")
        materialized = _materialize(ws, path)
        analysis = analyze_project(materialized, _terrain_signature=terrain_signature)
        analyses[path["id"]] = analysis
        link = next((l for l in links if l["path_id"] == path["id"]), None)
        if link:
            manufactured = _manufacturing(materialized, analysis)
            if not _equivalent(_physical(manufactured), _physical(assemblies[link["assembly_id"]]["items"])):
                _error("WORKSPACE_ASSEMBLY_MISMATCH", f"路径{path['name']}的制造量/顺序/缆型/体或参考位置与所关联装配不同")
    return ws, analyses, reports


def validate_workspace(workspace):
    return _validate(workspace)[0]


def materialize_path(workspace, path_id=None):
    ws = validate_workspace(workspace)
    selected = path_id or ws.get("active_path_id")
    path = next((p for p in ws["paths"] if p["id"] == selected), None)
    if path is None:
        if selected is None: return None
        _error("WORKSPACE_PATH_NOT_FOUND", "路径不存在")
    return _materialize(ws, path)


def _analyzed(ws, analyses, assemblies):
    links = ws["associations"]
    deployed = [analyses[l["path_id"]]["summary"] for l in links if l["role"] == "deployment"]
    warnings = [{**deepcopy(w), "path_id": pid} for pid, a in analyses.items() for w in a["warnings"]]
    if any(t.get("source") == "schema1_core_default" for t in ws["cable_types"]):
        warnings.append({"code": "WORKSPACE_DEFAULT_CABLE_LIBRARY", "severity": "warning",
                         "message": "原单路径工程未定义缆库，迁移保留core的GENERIC零单价及1 m/s默认值；请填写真实缆材参数"})
    rows = []
    for path in ws["paths"]:
        link = next((l for l in links if l["path_id"] == path["id"]), None)
        rows.append({"path_id": path["id"], "name": path["name"], "assembly_id": link["assembly_id"] if link else None,
                     "role": link["role"] if link else None, "summary": analyses[path["id"]]["summary"],
                     "route_geometry": analyses[path["id"]]["route_geometry"],
                     "route_geometry_segments": analyses[path["id"]]["route_geometry_segments"],
                     "route_signature": analyses[path["id"]]["route_signature"],
                     "included_in_installation": bool(link and link["role"] == "deployment")})
    for report in assemblies.values():
        report["deployment_path_id"] = next((l["path_id"] for l in links if l["assembly_id"] == report["assembly_id"] and l["role"] == "deployment"), None)
        report["alternative_path_ids"] = [l["path_id"] for l in links if l["assembly_id"] == report["assembly_id"] and l["role"] == "alternative"]
    unallocated = sum(r["deployment_path_id"] is None for r in assemblies.values())
    unassigned = sum(r["assembly_id"] is None for r in rows)
    if unallocated:
        warnings.append({"code": "WORKSPACE_UNALLOCATED_INVENTORY", "severity": "info", "message": "未分配制造装配仍按库存计一次采购费，没有安装费", "count": unallocated})
    if unassigned:
        warnings.append({"code": "WORKSPACE_UNASSIGNED_PATH", "severity": "warning", "message": "未关联路径仅提供方案分析，不计入采购或安装汇总", "count": unassigned})
    summary = {"currency": ws["currency"], "path_count": len(rows), "assembly_count": len(assemblies),
        "deployment_path_count": len(deployed), "alternative_path_count": sum(l["role"] == "alternative" for l in links),
        "unassigned_path_count": unassigned, "unallocated_assembly_count": unallocated,
        "manufactured_total_m": sum(a["total_length_m"] for a in assemblies.values()),
        "material_length_m": sum(a["material_length_m"] for a in assemblies.values()),
        "body_length_m": sum(a["body_length_m"] for a in assemblies.values()),
        "material_cost": sum(a["material_cost"] for a in assemblies.values()), "body_cost": sum(a["body_cost"] for a in assemblies.values()),
        "procurement_cost": sum(a["procurement_cost"] for a in assemblies.values()),
        "deployment_surface_length_m": sum(p["surface_length_m"] for p in deployed),
        "deployment_bottom_length_m": sum(p["bottom_length_m"] for p in deployed) if all(p["bottom_length_m"] is not None for p in deployed) else None,
        "time_hours": sum(p["time_hours"] for p in deployed), "vessel_cost": sum(p["vessel_cost"] for p in deployed),
        "burial_cost": sum(p["burial_cost"] for p in deployed), "extra_cost": sum(p["extra_cost"] for p in deployed),
        "contingency_cost": sum(p["contingency_cost"] for p in deployed)}
    summary["installation_subtotal"] = summary["vessel_cost"]+summary["burial_cost"]+summary["extra_cost"]
    summary["cost_total"] = summary["procurement_cost"]+summary["installation_subtotal"]+summary["contingency_cost"]
    result = {"summary": summary, "paths": rows, "assemblies": list(assemblies.values()), "associations": deepcopy(links),
        "active_path_id": ws["active_path_id"], "active_path_analysis": analyses.get(ws["active_path_id"]), "warnings": warnings,
        "model": {"identity": "oceanroute-workspace-relations-v2", "validation_status": "planning_prototype",
            "manufacturing_tolerance_m": TOL, "currency_policy": "one_currency_no_implicit_exchange",
            "cost_policy": "unique_assembly_procurement_plus_deployment_installation", "unallocated_inventory_contingency": "excluded",
            "limitations": ["一条路径关联一套完整装配；不自动拆分实物或同时敷设同一装配。", "alternative是同一库存的互斥路线方案，不是额外安装。", "库存采购费计一次；安装和预备费仅计deployment路径。", "OceanRoute自有schema，不兼容原厂关系库或native文件。"]}}
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, OverflowError) as error:
        raise WorkspaceError("WORKSPACE_NUMBER", "工作区汇总超出有限数值范围") from error
    return result


def analyze_workspace(workspace):
    return _analyzed(*_validate(workspace))


def _result(workspace, report=None):
    ws, analyses, assemblies = _validate(workspace)
    analysis = _analyzed(ws, analyses, assemblies)
    active = next((p for p in ws["paths"] if p["id"] == ws.get("active_path_id")), None)
    return {"workspace": ws, "project": _materialize(ws, active) if active else None, "analysis": analysis,
            "report": report or {}, "warnings": analysis["warnings"]}


def _prepare_project(project, identifier, name, currency):
    p = deepcopy(_object(project, "project"))
    if p.get("schema_version", 1) != 1 or p.get("crs", "EPSG:4326") != "EPSG:4326":
        _error("WORKSPACE_SCHEMA", "Cable Path来源须为WGS84 schema1工程")
    p.update(schema_version=1, id=identifier, name=name)
    _object(p.setdefault("costs", {}), "costs")
    if p["costs"].get("currency", currency) != currency:
        _error("WORKSPACE_CURRENCY", "路径币种不同，不做静默换汇")
    p["costs"]["currency"] = currency
    if p.get("cable_types", []) == []:
        # Preserve the existing core's documented fallback explicitly in the
        # shared library instead of losing a valid schema-1 route on migration.
        p["cable_types"] = [{"id": "GENERIC", "name": "待指定电缆", "cost_per_m": 0,
                             "lay_speed_m_s": 1.0, "source": "schema1_core_default"}]
    _geometry_work(p)
    analysis = analyze_project(p)
    for point, row in zip(p["route"]["points"], analysis["rpl"]):
        point["id"] = row["id"]
    for original, body in zip(p.get("bodies", []), analysis["bodies"]):
        original["id"] = body["id"]
    for original, reference in zip(p.get("assembly_references", []), analysis.get("assembly_references", [])):
        original["id"] = reference["id"]
    p.setdefault("layers", [])
    from .terrain_sources import normalize_sources
    p["terrain_sources"] = normalize_sources(p.get("terrain_sources", []))
    return p, analysis


def _independent_project(project):
    p = deepcopy(project); mapping = {}
    for item in p.get("bodies", [])+p.get("assembly_references", []):
        old = item["id"]; mapping[old] = str(uuid4()); item["id"] = mapping[old]
    for link in p["route"].get("path_links", []):
        if link.get("assembly_item_id") in mapping:
            link["assembly_item_id"] = mapping[link["assembly_item_id"]]
    if p["route"].get("constraint_state"):
        from .constraints import configure_constraints
        p = configure_constraints(p, {"mode": p["route"].get("mode", "fixed"), "path_links": p["route"].get("path_links", [])})["project"]
    return p


def migrate_project(project, config=None):
    config = _object(config or {}, "config")
    project = _object(project, "project")
    currency = _object(project.get("costs", {}), "costs").get("currency", "CNY")
    pid = str(uuid4()); name = str(project.get("name", "主路径"))
    p, analysis = _prepare_project(project, pid, name, currency)
    ws = {"schema_version": 2, "id": str(uuid4()), "name": str(config.get("name", project.get("name", "多路径海缆工程"))),
          "currency": currency, "active_path_id": pid, "cable_types": deepcopy(p["cable_types"]), "layers": deepcopy(p.get("layers", [])),
          "terrain_sources": deepcopy(p["terrain_sources"]),
          "paths": [{"id": pid, "name": name, "kind": "cable", "project": _strip(p, pid, name)}],
          "assemblies": [], "associations": [], "origin_project_id": project.get("id"), "migration": "schema1_to_schema2"}
    assembly = _assembly_from(p, analysis, name+" · 制造装配")
    ws["assemblies"].append(assembly)
    ws["associations"].append({"id": str(uuid4()), "path_id": pid, "assembly_id": assembly["id"], "role": "deployment"})
    return _result(ws, {"operation": "migrate", "source_schema_version": 1, "source_saved_revision": project.get("saved_revision"),
                        "manufactured_total_delta_m": 0, "cost_total_delta": 0})


def workspace_action(workspace, config):
    config = _object(config, "config")
    ws, analyses, reports = _validate(workspace)
    before = _analyzed(ws, analyses, reports)["summary"]
    action = config.get("action")
    path_id = config.get("path_id", ws.get("active_path_id"))
    path = next((p for p in ws["paths"] if p["id"] == path_id), None)
    link = next((l for l in ws["associations"] if l["path_id"] == path_id), None)
    if action in {"copy_path", "update_path", "remove_path", "set_active", "set_deployment", "detach", "associate"} and path is None:
        _error("WORKSPACE_PATH_NOT_FOUND", "路径不存在")
    if action == "set_active":
        ws["active_path_id"] = path_id
    elif action == "set_deployment":
        if link is None: _error("WORKSPACE_RELATION", "未关联路径不能成为装配deployment")
        for existing in ws["associations"]:
            if existing["assembly_id"] == link["assembly_id"]:
                existing["role"] = "deployment" if existing["path_id"] == path_id else "alternative"
    elif action == "update_metadata":
        if "name" not in config: _error("WORKSPACE_STRUCTURE", "update_metadata须提供name")
        ws["name"] = str(config["name"])
    elif action == "update_shared":
        for key in ("cable_types", "layers", "terrain_sources"):
            if key in config: ws[key] = deepcopy(config[key])
    elif action in {"add_path", "copy_path"}:
        source = _materialize(ws, path) if action == "copy_path" else _object(config.get("project"), "project")
        policy = config.get("assembly_policy", "independent")
        if policy not in {"independent", "alternative", "unassigned"}:
            _error("WORKSPACE_POLICY", "复制/新增assembly_policy须independent/alternative/unassigned")
        pid = str(uuid4()); name = str(config.get("name", str(source.get("name", "路径"))+(" · 副本" if action == "copy_path" else "")))
        if (source.get("cable_types") != ws["cable_types"] or source.get("layers", []) != ws["layers"]
                or source.get("terrain_sources", []) != ws["terrain_sources"]):
            _error("WORKSPACE_SHARED_RESOURCE", "新增路径必须引用当前共享库和GIS；先通过update_shared显式合并资源")
        p, a = _prepare_project(source, pid, name, ws["currency"])
        if policy == "independent":
            p = _independent_project(p); a = analyze_project(p)
        ws["paths"].append({"id": pid, "name": name, "kind": "cable", "project": _strip(p, pid, name)})
        if policy == "independent":
            assembly = _assembly_from(p, a, name+" · 制造装配"); ws["assemblies"].append(assembly)
            ws["associations"].append({"id": str(uuid4()), "path_id": pid, "assembly_id": assembly["id"], "role": "deployment"})
        elif policy == "alternative":
            aid = config.get("assembly_id", link["assembly_id"] if link else None)
            if aid is None: _error("WORKSPACE_RELATION", "alternative须明确已有装配")
            ws["associations"].append({"id": str(uuid4()), "path_id": pid, "assembly_id": aid, "role": "alternative"})
        ws["active_path_id"] = pid
    elif action == "update_path":
        source = deepcopy(_object(config.get("project"), "project"))
        if source.get("id") != path_id:
            _error("WORKSPACE_PATH_ID", "投影id不匹配当前path，不自动覆盖其他路径")
        for key in ("cable_types", "layers", "terrain_sources"):
            if key in source and source[key] != ws[key]:
                if not config.get("update_shared", False):
                    _error("WORKSPACE_SHARED_EDIT_REQUIRED", "共享库/GIS变更须update_shared:true，不能丢失或私存修改")
                ws[key] = deepcopy(source[key])
            source[key] = deepcopy(ws[key])
        p, a = _prepare_project(source, path_id, str(source.get("name", path["name"])), ws["currency"])
        if link:
            old = next(x for x in ws["assemblies"] if x["id"] == link["assembly_id"])
            items = _manufacturing(p, a)
            if not _equivalent(_physical(items), _physical(old["items"])):
                policy = config.get("assembly_policy", "auto_exclusive")
                references = [l for l in ws["associations"] if l["assembly_id"] == old["id"]]
                if policy in {"auto_exclusive", "update_exclusive"}:
                    if len(references) != 1 or references[0]["role"] != "deployment":
                        _error("WORKSPACE_SHARED_ASSEMBLY_CHANGED", "共享制造装配不随单条方案量变；保留制造量或显式fork新装配")
                    made = _assembly_from(p, a, old.get("name", "制造装配"), old["id"])
                    ws["assemblies"][ws["assemblies"].index(old)] = made
                elif policy == "fork":
                    if link["role"] == "deployment" and any(l["role"] == "alternative" for l in references):
                        successor = config.get("successor_path_id")
                        successor_link = next((l for l in references if l["path_id"] == successor and l["role"] == "alternative"), None)
                        if successor_link is None:
                            _error("WORKSPACE_DEPLOYMENT_REQUIRED", "主路径fork后仍有替代方案，须明确successor_path_id接管旧装配")
                        successor_link["role"] = "deployment"
                    p = _independent_project(p); a = analyze_project(p)
                    made = _assembly_from(p, a, path["name"]+" · 新制造装配"); ws["assemblies"].append(made)
                    link.update(assembly_id=made["id"], role="deployment")
                else:
                    _error("WORKSPACE_ASSEMBLY_CHANGED", "制造量改变，须auto_exclusive/update_exclusive/fork明确处理")
        path.update(name=p["name"], project=_strip(p, path_id, p["name"]))
    elif action in {"remove_path", "detach"}:
        if link:
            alternatives = [l for l in ws["associations"] if l["assembly_id"] == link["assembly_id"] and l["role"] == "alternative" and l is not link]
            if link["role"] == "deployment" and alternatives:
                successor = next((l for l in alternatives if l["path_id"] == config.get("successor_path_id")), None)
                if successor is None: _error("WORKSPACE_DEPLOYMENT_REQUIRED", "移除主关联前须明确继任替代路径")
                successor["role"] = "deployment"
            ws["associations"].remove(link)
            if config.get("delete_orphan_assembly", False):
                if any(l["assembly_id"] == link["assembly_id"] for l in ws["associations"]):
                    _error("WORKSPACE_ASSEMBLY_IN_USE", "装配仍被其他路径引用，不可删除")
                ws["assemblies"] = [a for a in ws["assemblies"] if a["id"] != link["assembly_id"]]
        if action == "remove_path":
            ws["paths"].remove(path)
            if ws["active_path_id"] == path_id:
                ws["active_path_id"] = config.get("active_path_id", ws["paths"][0]["id"] if ws["paths"] else None)
    elif action == "associate":
        if link:
            if not config.get("replace_existing", False): _error("WORKSPACE_RELATION", "路径已有装配，替换须replace_existing:true")
            if link["role"] == "deployment" and any(l["assembly_id"] == link["assembly_id"] and l["role"] == "alternative" for l in ws["associations"]):
                _error("WORKSPACE_DEPLOYMENT_REQUIRED", "先通过detach明确接管旧装配，再更换主关联")
            ws["associations"].remove(link)
        ws["associations"].append({"id": str(uuid4()), "path_id": path_id, "assembly_id": config.get("assembly_id"), "role": config.get("role", "deployment")})
    elif action == "add_assembly":
        if "assembly" in config:
            assembly = deepcopy(_object(config["assembly"], "assembly")); assembly.setdefault("id", str(uuid4()))
        else:
            p, a = _prepare_project(config.get("project"), str(uuid4()), str(config.get("name", "库存制造装配")), ws["currency"])
            if p["cable_types"] != ws["cable_types"]: _error("WORKSPACE_SHARED_RESOURCE", "装配来源必须引用共享库")
            p = _independent_project(p); a = analyze_project(p)
            assembly = _assembly_from(p, a, str(config.get("name", "库存制造装配")))
        ws["assemblies"].append(assembly)
    elif action == "remove_assembly":
        aid = config.get("assembly_id")
        if any(l["assembly_id"] == aid for l in ws["associations"]): _error("WORKSPACE_ASSEMBLY_IN_USE", "有关联的制造装配不得删除")
        if not any(a["id"] == aid for a in ws["assemblies"]): _error("WORKSPACE_ASSEMBLY_NOT_FOUND", "装配不存在")
        ws["assemblies"] = [a for a in ws["assemblies"] if a["id"] != aid]
    else:
        _error("WORKSPACE_ACTION", "未知工作区操作")
    result = _result(ws, {"operation": action})
    result["report"].update(before_summary=before, after_summary=result["analysis"]["summary"],
        manufactured_total_delta_m=result["analysis"]["summary"]["manufactured_total_m"]-before["manufactured_total_m"],
        cost_total_delta=result["analysis"]["summary"]["cost_total"]-before["cost_total"])
    return result


def import_workspace(text, config=None):
    config = _object(config or {}, "config")
    if not isinstance(text, str) or len(text.encode()) > 32*1024*1024:
        _error("WORKSPACE_LIMIT", "工作区JSON文本最多32MB")
    document = _object(json.loads(text, parse_constant=lambda value: _error("WORKSPACE_NUMBER", "JSON不得含非有限数值")), "JSON")
    if document.get("schema_version", 1) == 1:
        return migrate_project(document, config)
    ws = validate_workspace(document)
    ws["origin_workspace_id"] = ws["id"]
    ws["id"] = str(uuid4()); ws.pop("saved_revision", None)
    return _result(ws, {"operation": "import", "source_schema_version": 2, "new_workspace_identity": True})


def export_workspace(workspace):
    return json.dumps(validate_workspace(workspace), ensure_ascii=False, allow_nan=False, indent=2)
