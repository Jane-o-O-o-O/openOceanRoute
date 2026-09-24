"""Atomic shared-terrain resampling and consistent manufacturing preview.

The old valid workspace and optional admitted path draft are never mutated.
This prepares one reviewable candidate; persistence remains WorkspaceStore's
optimistic transaction, not a sequence of source/path writes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

from .core import analyze_project, route_signature
from .geodesy import finite_number, inverse
from .route_geometry import route_segments
from .terrain_sources import normalize_sources, _library_signature, profile_from_sources
from .workspace import (_validate, _analyzed, _materialize, _strip, _manufacturing,
                        _physical, _equivalent, _assembly_from, validate_workspace,
                        analyze_workspace)

MODEL = "atomic-workspace-terrain-preview-v1"
BOUND_MODELS = {"priority-terrain-library-v1", "terrain-library-derived-profile-v1"}
MAX_INPUT_BYTES = 64*1024**2


class WorkspaceTerrainError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _error(code, message):
    raise WorkspaceTerrainError(code, message)


def _canonical(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _json(value):
    try:
        return json.dumps(_canonical(value), ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        _error("WORKSPACE_TERRAIN_JSON", f"请求和诊断须为有限JSON：{exc}")


def preview_input_signature(workspace, sources, config=None, draft=None):
    """Exact request fingerprint, including revision and independent draft.

    Numerically equal JSON int/float/-0 values have the same signature. No
    decimal rounding, physical-data omission, or security-signature claim.
    """
    encoded = _json({"workspace": workspace, "sources": sources,
                     "config": {} if config is None else config, "draft": draft})
    if len(encoded) > MAX_INPUT_BYTES:
        _error("WORKSPACE_TERRAIN_INPUT_BUDGET", "完整预览输入最多64MiB，不丢弃草稿或源资料")
    return hashlib.sha256(encoded).hexdigest()


def _integer(value, name, lower, upper):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        _error("WORKSPACE_TERRAIN_CONFIG", f"{name}须为{lower}..{upper}整数")
    return value


def _config(config):
    c = deepcopy({} if config is None else config)
    allowed = {"spacing_m", "vertical_datum", "path_ids", "max_total_query_points",
               "max_total_work_units", "max_total_output_bytes", "manufacturing_policy"}
    if not isinstance(c, dict) or set(c)-allowed:
        _error("WORKSPACE_TERRAIN_CONFIG", "config含不支持字段")
    c["spacing_m"] = finite_number(c.get("spacing_m", 1000), "spacing_m", minimum=1, maximum=100000)
    c["max_total_query_points"] = _integer(c.get("max_total_query_points", 50000), "max_total_query_points", 1, 200000)
    c["max_total_work_units"] = _integer(c.get("max_total_work_units", 60000000), "max_total_work_units", 1, 200000000)
    c["max_total_output_bytes"] = _integer(c.get("max_total_output_bytes", 32*1024**2), "max_total_output_bytes", 65536, 64*1024**2)
    c["manufacturing_policy"] = c.get("manufacturing_policy", "update_consistent")
    if c["manufacturing_policy"] not in {"update_consistent", "preserve"}:
        _error("WORKSPACE_TERRAIN_CONFIG", "manufacturing_policy仅支持update_consistent/preserve，不自动fork")
    if "vertical_datum" in c and (not isinstance(c["vertical_datum"], str) or not 1 <= len(c["vertical_datum"].strip()) <= 128):
        _error("WORKSPACE_TERRAIN_CONFIG", "vertical_datum须为1..128字符明确声明")
    if "path_ids" in c:
        ids = c["path_ids"]
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) or not i or len(i) > 128 for i in ids) or len(set(ids)) != len(ids):
            _error("WORKSPACE_TERRAIN_CONFIG", "path_ids须为最多100个不重复路径ID")
    return c


def _failure(exc, stage, path_id=None, assembly_id=None):
    row = {"code": getattr(exc, "code", "WORKSPACE_TERRAIN_FAILED"),
           "message": str(exc)[:4000], "stage": stage}
    if path_id is not None: row["path_id"] = path_id
    if assembly_id is not None: row["assembly_id"] = assembly_id
    return row


def _flexible_bottom(project):
    route = project["route"]
    defaults = route.get("mode", "flexible"), route.get("slack_basis", "surface")
    options = route.get("legs", [])
    return any((options[i] if i < len(options) else {}).get("mode", defaults[0]) == "flexible" and
               (options[i] if i < len(options) else {}).get("slack_basis", defaults[1]) == "bottom"
               for i in range(len(route["points"])-1))


def _bound(project):
    profile = project.get("profile")
    metadata = profile.get("metadata") if isinstance(profile, dict) else None
    return isinstance(metadata, dict) and metadata.get("model") in BOUND_MODELS


def _sample_count(project, spacing, *, max_work_units=None, with_work=False):
    route = project.get("route")
    if not isinstance(route, dict) or not isinstance(route.get("points"), list) or not 2 <= len(route["points"]) <= 10000:
        _error("WORKSPACE_TERRAIN_DRAFT", "草稿route须为2..10,000点路线")
    count = 1
    has_arc = any(isinstance(leg, dict) and leg.get("geometry") is not None for leg in route.get("legs", []))
    try:
        parts = route_segments(project, {"max_work_units": min(10_000_000, max_work_units)} if has_arc and max_work_units is not None else None)
    except ValueError as error:
        if "max_work_units" in str(error):
            _error("TERRAIN_WORK_BUDGET", "真实圆弧路线采样预估超过工作预算")
        raise
    for a, b, segment in zip(route["points"], route["points"][1:], parts):
        if not isinstance(a, dict) or not isinstance(b, dict):
            _error("WORKSPACE_TERRAIN_DRAFT", "草稿路线点须为对象")
        distance = segment.length_m
        count += math.ceil(distance/spacing) if distance > 1e-9 else 0
    arc_work = sum(s.solver["work_units"] for s in parts if s.is_arc)
    return (count, arc_work) if with_work else count


def _admit_draft(old, draft):
    if not isinstance(draft, dict) or set(draft) != {"path_id", "project"} or not isinstance(draft["project"], dict):
        _error("WORKSPACE_TERRAIN_DRAFT", "draft须仅含path_id和完整project对象")
    path = next((p for p in old["paths"] if p["id"] == draft["path_id"]), None)
    p = draft["project"]
    if path is None or p.get("id") != path["id"] or p.get("schema_version") != 1 or p.get("crs", "EPSG:4326") != "EPSG:4326":
        _error("WORKSPACE_TERRAIN_DRAFT", "草稿路径ID/schema/WGS84必须对应旧工作区中的路径")
    for key in ("cable_types", "layers", "terrain_sources"):
        if key in p and p[key] != old[key]:
            _error("WORKSPACE_TERRAIN_DRAFT_SHARED", "草稿共享缆库/GIS/地形须与旧工程一致；新地形只通过独立sources传入")
    if "saved_revision" in p:
        _error("WORKSPACE_TERRAIN_DRAFT_REVISION", "子路径不得带独立saved_revision，修订属于完整工作区")
    expected = _materialize(old, path)["workspace_context"]
    if "workspace_context" in p and p["workspace_context"] != expected:
        _error("WORKSPACE_TERRAIN_DRAFT_REVISION", "草稿工作区/路径/关联/修订上下文不匹配当前旧工程")
    old_route = path["project"]["route"]
    if not isinstance(p.get("route"), dict) or not isinstance(p["route"].get("points"), list) or any(not isinstance(point, dict) for point in p["route"]["points"]):
        _error("WORKSPACE_TERRAIN_DRAFT", "draft.route须包含对象形式路线点数组")
    if not isinstance(p["route"].get("legs", []), list) or any(not isinstance(option, dict) for option in p["route"].get("legs", [])):
        _error("WORKSPACE_TERRAIN_DRAFT", "draft.route.legs须为对象数组")
    for field in ("bodies", "assembly_references"):
        values = p.get(field, [])
        if not isinstance(values, list) or len(values) > 10000 or any(not isinstance(value, dict) for value in values):
            _error("WORKSPACE_TERRAIN_DRAFT", f"draft.{field}须为最多10,000对象数组")
    allowances = p["route"].get("allowances", p.get("allowances", []))
    if not isinstance(allowances, list) or len(allowances) > 10000 or any(not isinstance(value, dict) for value in allowances):
        _error("WORKSPACE_TERRAIN_DRAFT", "draft allowances须为最多10,000对象数组")
    if p["route"].get("constraint_state") != old_route.get("constraint_state") or p["route"].get("path_links", []) != old_route.get("path_links", []):
        _error("WORKSPACE_TERRAIN_DRAFT_CONSTRAINT", "本预览不能删除或重新捕获已有PathLink/制造域状态")
    # Fixed segment stock cannot be enlarged under a terrain-update operation.
    # Require its original point pair and explicit material assignment to remain.
    old_opts = old_route.get("legs", [])
    new_route = p.get("route", {})
    new_opts = new_route.get("legs", [])
    new_points = new_route.get("points", [])
    has_fixed = False
    for i, (a, b) in enumerate(zip(old_route["points"], old_route["points"][1:])):
        option = old_opts[i] if i < len(old_opts) else {}
        if option.get("mode", old_route.get("mode", "flexible")) != "fixed": continue
        has_fixed = True
        match = next((j for j, (na, nb) in enumerate(zip(new_points, new_points[1:]))
                      if na.get("id") == a.get("id") and nb.get("id") == b.get("id")), None)
        new_opt = new_opts[match] if match is not None and match < len(new_opts) else {}
        if match is None or new_opt.get("mode", new_route.get("mode", "flexible")) != "fixed" or any(
                new_opt.get(k) != option.get(k) for k in ("fixed_cable_length_m", "cable_type_id")):
            _error("WORKSPACE_TERRAIN_DRAFT_FIXED", "固定段的点对、缆型与制造量不可在来源更新中被修改")
    if has_fixed:
        def inventory_fields(project):
            route = project["route"]
            return {"allowances": [{k: a.get(k) for k in ("kp_m", "length_m", "cable_type_id", "reserved_for_body_id")}
                                   for a in route.get("allowances", project.get("allowances", []))],
                    "bodies": [{k: b.get(k) for k in ("id", "kp_m", "cable_kp_m", "length_m", "length_mode")}
                               for b in project.get("bodies", [])],
                    "references": [{k: r.get(k) for k in ("id", "cable_kp_m", "length_m")}
                                   for r in project.get("assembly_references", [])]}
        if inventory_fields(p) != inventory_fields(path["project"]):
            _error("WORKSPACE_TERRAIN_DRAFT_FIXED", "含固定段路线的津贴、组件和实物参考须保留；制造编辑请走独立装配流程")
    result = deepcopy(old)
    target = next(row for row in result["paths"] if row["id"] == path["id"])
    target["name"] = str(p.get("name", path["name"]))
    target["project"] = _strip(p, path["id"], target["name"])
    return result


def _preserve_partition_ids(old, made):
    # Unchanged ordered cable partitions retain their entity identity even if
    # their numeric lengths change. Bodies/references already have explicit IDs.
    old_cables = [i for i in old["items"] if i["kind"] == "cable"]
    cables = [i for i in made["items"] if i["kind"] == "cable"]
    if len(old_cables) == len(cables) and all(a["cable_type_id"] == b["cable_type_id"] for a, b in zip(old_cables, cables)):
        for a, b in zip(old_cables, cables): b["id"] = a["id"]
    return made


def preview_workspace_terrain(workspace, sources, config=None, *, draft=None):
    """Return one all-or-none candidate with actual per-path sample diagnostics."""
    signature = preview_input_signature(workspace, sources, config, draft)
    c = _config(config)
    old, old_analyses, old_assemblies = _validate(workspace)
    before = _analyzed(old, old_analyses, old_assemblies)["summary"]
    normalized = normalize_sources(sources)
    source_signature = _library_signature(normalized)
    initial = deepcopy(old)
    diagnostics, errors, warnings, manufacturing = [], [], [], []
    work, completed_query_work, completed_points, estimated_points = 0, 0, 0, 0
    preflight_arc_work = 0
    draft_report = None

    def finish(candidate=None, analysis=None):
        active = next((p for p in candidate["paths"] if p["id"] == candidate["active_path_id"]), None) if candidate else None
        result = {"model": MODEL, "validation_status": "research", "can_apply": candidate is not None,
                  "input_signature": signature, "sources_signature": source_signature,
                  "workspace": candidate, "project": _materialize(candidate, active) if active else None,
                  "analysis": analysis, "paths": diagnostics, "manufacturing": manufacturing,
                  "errors": errors, "warnings": warnings, "draft_report": draft_report,
                  "before_summary": before, "after_summary": analysis["summary"] if analysis else None,
                  "budget": {"estimated_query_points": estimated_points, "completed_query_points": completed_points,
                             "work_units": work, "completed_query_work_units": completed_query_work,
                             "preflight_arc_geometry_work_units": preflight_arc_work,
                             "work_accounting": "actual arc preflight numerical evaluations plus completed query allowances and conservative remaining reservation on work-budget failure; not CPU FLOPs",
                             "max_total_query_points": c["max_total_query_points"],
                             "max_total_work_units": c["max_total_work_units"],
                             "max_total_output_bytes": c["max_total_output_bytes"]},
                  "assumptions": ["No path geometry, target slack, fixed inventory or manufacturing association is silently changed.",
                                  "A shared assembly is updated once only when all linked paths require the same physical inventory; no automatic fork.",
                                  "A complete sampled profile does not certify unsampled seabed continuity or measurement accuracy.",
                                  "The preview never writes storage; apply the complete workspace and save using its retained revision."]}
        if len(_json(result)) > c["max_total_output_bytes"]:
            _error("WORKSPACE_TERRAIN_OUTPUT_BUDGET", "完整预览输出超过预算；不返回截断诊断或部分候选")
        return result

    if draft is not None:
        try:
            initial = _admit_draft(old, draft)
            draft_report = {"path_id": draft["path_id"], "admitted": True,
                            "meaning": "structural_admission_only_pending_fresh_profile_and_full_validation"}
        except ValueError as exc:
            pid = draft.get("path_id") if isinstance(draft, dict) else None
            errors.append(_failure(exc, "draft", pid))
            draft_report = {"path_id": pid, "admitted": False}
            return finish()

    base = initial
    base_analyses = old_analyses
    changed_library = source_signature != _library_signature(base["terrain_sources"])
    ids = {p["id"] for p in base["paths"]}
    selected = ids if "path_ids" not in c else set(c["path_ids"])
    if selected-ids: _error("WORKSPACE_TERRAIN_PATH", "path_ids引用不属于本工作区的路径")
    required = {p["id"] for p in base["paths"] if changed_library and (_bound(p["project"]) or _flexible_bottom(p["project"]))}
    if draft is not None:
        required.add(draft["path_id"])
    missing = required-selected
    for path in base["paths"]:
        diagnostics.append({"path_id": path["id"], "name": path["name"],
                            "status": "pending" if path["id"] in selected else "not_selected",
                            "samples": [], "quality": None, "budget": None,
                            "before_summary": old_analyses[path["id"]]["summary"], "after_summary": None,
                            "constraint_report": None, "warnings": [], "error": None})
    if missing:
        for row in diagnostics:
            if row["path_id"] in missing:
                error = _failure(WorkspaceTerrainError("WORKSPACE_TERRAIN_SELECTION", "来源库变化后，受影响路径必须同笔重新采样"), "selection", row["path_id"])
                row.update(status="error", error=error); errors.append(error)
            elif row["status"] == "pending": row["status"] = "not_run"
        return finish()

    counts = {}
    try:
        for p in base["paths"]:
            if p["id"] not in selected:
                continue
            count, arc_work = _sample_count(p["project"], c["spacing_m"], max_work_units=max(1, c["max_total_work_units"]-work), with_work=True)
            counts[p["id"]] = count
            preflight_arc_work += arc_work
            work += arc_work
            if work >= c["max_total_work_units"]:
                _error("TERRAIN_WORK_BUDGET", "真实圆弧路线采样预估耗尽整体工作预算")
    except ValueError as exc:
        errors.append(_failure(exc, "preflight"))
        for row in diagnostics:
            if row["path_id"] in selected: row["status"] = "not_run"
        return finish()
    estimated_points = sum(counts.values())
    if estimated_points > c["max_total_query_points"] or any(n > 50000 for n in counts.values()):
        error = _failure(WorkspaceTerrainError("WORKSPACE_TERRAIN_POINT_BUDGET", "全部路线采样点预估超过总预算或单路径50,000点上限；未开始采样"), "preflight")
        errors.append(error)
        for row in diagnostics:
            if row["path_id"] in selected: row.update(status="not_run", error=error)
        return finish()
    if estimated_points*1200+len(_json(base))+len(_json(normalized)) > c["max_total_output_bytes"]:
        error = _failure(WorkspaceTerrainError("WORKSPACE_TERRAIN_OUTPUT_BUDGET", "采样诊断和候选工作区的保守总输出预算不足；未开始采样"), "preflight")
        errors.append(error)
        for row in diagnostics:
            if row["path_id"] in selected: row.update(status="not_run", error=error)
        return finish()

    candidate = deepcopy(base); candidate["terrain_sources"] = normalized
    candidate_analyses = dict(base_analyses)
    exhausted = False
    for path, row in zip(candidate["paths"], diagnostics):
        if path["id"] not in selected: continue
        if exhausted or work >= c["max_total_work_units"]:
            error = _failure(WorkspaceTerrainError("WORKSPACE_TERRAIN_WORK_BUDGET", "先前路线已耗尽工作预算，此路径未采样"), "budget", path["id"])
            row.update(status="not_run", error=error); errors.append(error)
            continue
        try:
            qconfig = {"spacing_m": c["spacing_m"], "max_query_points": 50000,
                       "max_work_units": c["max_total_work_units"]-work,
                       "max_output_bytes": min(64*1024**2, c["max_total_output_bytes"])}
            if "vertical_datum" in c: qconfig["vertical_datum"] = c["vertical_datum"]
            sampled = profile_from_sources(_materialize(candidate, path), qconfig)
            work += sampled["budget"]["work_units"]
            completed_query_work += sampled["budget"]["work_units"]
            completed_points += sampled["quality"]["sample_count"]
            row.update(samples=sampled["samples"], quality=sampled["quality"], budget=sampled["budget"], warnings=sampled["warnings"])
            if not sampled["quality"]["complete"]:
                _error("WORKSPACE_TERRAIN_NODATA", "路线存在缺测；不以路线点水深修补或应用不完整候选")
            project = sampled["project"]
            original_curve = route_signature(project)
            if project["route"].get("constraint_state") is not None:
                from .constraints import solve_constraints
                constrained = solve_constraints(project)
                project = constrained["project"]
                row["constraint_report"] = constrained["report"]
                row["warnings"] += constrained["warnings"]
                if route_signature(project) != original_curve:
                    _error("WORKSPACE_TERRAIN_CONSTRAINT_GEOMETRY", "无移点的域复核改变了路线几何；不转移新采样位置")
            analysis = analyze_project(project, _terrain_signature=source_signature)
            row["after_summary"] = analysis["summary"]
            row["warnings"] += analysis["warnings"]
            if any(w["code"] in {"CABLE_SHORTAGE", "CONSTRAINT_DOMAIN_SHORTAGE"} for w in row["warnings"]):
                _error("WORKSPACE_TERRAIN_SHORTAGE", "新地形下固定缆量或约束域不足；保留原制造量并拒绝整笔应用")
            if not analysis["profile_metadata"]["imported_profile_valid"] or analysis["summary"]["bottom_length_m"] is None:
                _error("WORKSPACE_TERRAIN_PROFILE", "重采样后的完整剖面未通过路线/来源签名和底距校核")
            old_path = next(p for p in old["paths"] if p["id"] == path["id"])
            if old_analyses[path["id"]]["legs"] and all(l["mode"] == "fixed" for l in old_analyses[path["id"]]["legs"]):
                old_project = _materialize(old, old_path)
                if not _equivalent(_physical(_manufacturing(old_project, old_analyses[path["id"]])),
                                   _physical(_manufacturing(project, analysis))):
                    _error("WORKSPACE_TERRAIN_FIXED_INVENTORY", "固定路线的制造总量、材料、组件和参考站位必须保持")
            path["project"] = _strip(project, path["id"], path["name"])
            candidate_analyses[path["id"]] = analysis
            row["status"] = "success"
        except ValueError as exc:
            error = _failure(exc, "path", path["id"])
            row.update(status="error", error=error); errors.append(error)
            if getattr(exc, "code", "") in {"TERRAIN_WORK_BUDGET", "WORKSPACE_TERRAIN_WORK_BUDGET"}:
                work = c["max_total_work_units"]; exhausted = True
        warnings.extend({**deepcopy(w), "path_id": path["id"]} for w in row["warnings"])
    if errors: return finish()

    for assembly in candidate["assemblies"]:
        links = [l for l in candidate["associations"] if l["assembly_id"] == assembly["id"]]
        if not links: continue
        projects = {l["path_id"]: _materialize(candidate, next(p for p in candidate["paths"] if p["id"] == l["path_id"])) for l in links}
        inventories = {pid: _manufacturing(p, candidate_analyses[pid]) for pid, p in projects.items()}
        deployment = next(l["path_id"] for l in links if l["role"] == "deployment")
        target = inventories[deployment]
        old_assembly = next(a for a in old["assemblies"] if a["id"] == assembly["id"])
        new_total = candidate_analyses[deployment]["summary"]["cable_length_m"]
        info = {"assembly_id": assembly["id"], "path_ids": [l["path_id"] for l in links],
                "status": "unchanged", "old_total_m": old_assembly["total_length_m"],
                "new_total_m": new_total, "delta_m": new_total-old_assembly["total_length_m"], "error": None}
        manufacturing.append(info)
        divergence = [pid for pid, inventory in inventories.items() if not _equivalent(_physical(target), _physical(inventory))]
        if divergence:
            exc = WorkspaceTerrainError("WORKSPACE_TERRAIN_SHARED_DIVERGENCE", "共享装配的路径要求不同制造序列；须独立显式fork，不能多次计费或复用不一致实物")
            error = _failure(exc, "manufacturing", assembly_id=assembly["id"])
            error["path_totals_m"] = {pid: a["summary"]["cable_length_m"] for pid, a in candidate_analyses.items() if pid in projects}
            info.update(status="error", error=error); errors.append(error)
        elif not _equivalent(_physical(target), _physical(assembly["items"])):
            if c["manufacturing_policy"] == "preserve":
                error = _failure(WorkspaceTerrainError("WORKSPACE_TERRAIN_MANUFACTURING_PRESERVED", "preserve政策下不允许制造量/序列改变"), "manufacturing", assembly_id=assembly["id"])
                info.update(status="error", error=error); errors.append(error)
            else:
                made = _assembly_from(projects[deployment], candidate_analyses[deployment], assembly["name"], assembly["id"])
                made = _preserve_partition_ids(assembly, made)
                assembly.clear(); assembly.update(made)
                info["status"] = "updated"
        elif abs(info["delta_m"]) > 1e-5:
            info["status"] = "updated_by_draft"
    if errors: return finish()
    try:
        # Public all-workspace validation is deliberately last: no intermediate
        # source replacement is ever presented as an applyable workspace.
        candidate = validate_workspace(candidate)
        aggregate = analyze_workspace(candidate)
        if any(w["code"] in {"CABLE_SHORTAGE", "CONSTRAINT_DOMAIN_SHORTAGE"} for w in aggregate["warnings"]):
            _error("WORKSPACE_TERRAIN_SHORTAGE", "完整候选仍含制造或固定域缺缆，不应用")
        return finish(candidate, aggregate)
    except ValueError as exc:
        errors.append(_failure(exc, "final_validation"))
        return finish()
