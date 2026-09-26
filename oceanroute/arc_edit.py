"""Explicit endpoint edits of intrinsic arcs, with original stock-domain review."""
from __future__ import annotations

from copy import deepcopy
import json

from .core import analyze_project, route_signature
from .geodesy import coordinate, finite_number, inverse


class ArcEditError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name}须为{low}..{high}整数")
    return value


def _config(raw, *, constraint_editor=False):
    raw = {} if raw is None else raw
    fields = {"moves", "arc_options", "max_arc_rebuilds", "max_work_units", "max_output_bytes"}
    if constraint_editor:
        fields.add("automatic")
    if not isinstance(raw, dict) or set(raw)-fields:
        raise ValueError("arc-edit config含未知字段")
    moves = raw.get("moves", [])
    maximum = 10000 if constraint_editor else 256
    if not isinstance(moves, list) or len(moves) > maximum:
        raise ValueError(f"moves须为最多{maximum}项数组")
    allowed = {"point_id", "longitude", "latitude"}
    if constraint_editor:
        allowed |= {"fraction", "cable_kp_m"}
    normalized, seen = [], set()
    for move in moves:
        if not isinstance(move, dict) or set(move)-allowed or "point_id" not in move:
            raise ValueError("move须含point_id及受支持的坐标／沿线字段")
        pid = move["point_id"]
        if not isinstance(pid, str) or not pid or pid in seen:
            raise ArcEditError("ARC_EDIT_POINT_REFERENCE", "移点ID须唯一非空字符串")
        seen.add(pid)
        if not constraint_editor and set(move) != {"point_id", "longitude", "latitude"}:
            raise ValueError("人工端点move须仅含point_id/longitude/latitude")
        item = deepcopy(move)
        if "longitude" in move or "latitude" in move:
            item["longitude"], item["latitude"] = coordinate(move.get("longitude"), move.get("latitude"))
        normalized.append(item)
    options = raw.get("arc_options", [])
    if not isinstance(options, list) or len(options) > 256:
        raise ValueError("arc_options须为最多256项数组")
    result_options, spans = [], set()
    for option in options:
        allowed_option = {"start_point_id", "end_point_id", "radius_m", "branch", "full_circle_policy"}
        if not isinstance(option, dict) or set(option)-allowed_option or not {"start_point_id", "end_point_id"} <= option.keys():
            raise ValueError("arc_options含未知字段或缺少Rigid跨度ID")
        pair = (option["start_point_id"], option["end_point_id"])
        if any(not isinstance(x, str) or not x for x in pair) or pair[0] == pair[1] or pair in spans:
            raise ArcEditError("ARC_EDIT_ARC_REFERENCE", "圆弧跨度ID无效或重复")
        spans.add(pair)
        item = {"start_point_id": pair[0], "end_point_id": pair[1],
                "branch": option.get("branch", "preserve"),
                "full_circle_policy": option.get("full_circle_policy", "reject_move")}
        if not isinstance(item["branch"], str) or item["branch"] not in {"preserve", "minor", "major", "left", "right"}:
            raise ValueError("branch须为preserve/minor/major/left/right")
        if not isinstance(item["full_circle_policy"], str) or item["full_circle_policy"] not in {"reject_move", "preserve_endpoint_center_bearing"}:
            raise ValueError("full_circle_policy无效")
        if "radius_m" in option:
            item["radius_m"] = finite_number(option["radius_m"], "radius_m", minimum=.001, maximum=1e6)
        result_options.append(item)
    out = {"moves": normalized, "arc_options": result_options,
           "max_arc_rebuilds": _integer(raw.get("max_arc_rebuilds", 32), "max_arc_rebuilds", 1, 256),
           "max_work_units": _integer(raw.get("max_work_units", 200000), "max_work_units", 1, 2000000),
           "max_output_bytes": _integer(raw.get("max_output_bytes", 16*1024**2), "max_output_bytes", 1, 64*1024**2)}
    if constraint_editor:
        if not isinstance(raw.get("automatic", False), bool):
            raise ValueError("automatic须为boolean")
        out["automatic"] = raw.get("automatic", False)
    if not constraint_editor and not moves and not options:
        raise ValueError("arc-edit须提供实际moves或arc_options")
    return out


def _nearest_fraction(segment, target):
    """Review every local minimum, including on directed major arcs."""
    from scipy.optimize import minimize_scalar
    grid = [i/32 for i in range(33)]
    def distance(f):
        return inverse(*segment.point_at_fraction(float(f)), *target, "geodesic")[0]
    values = [distance(f) for f in grid]
    candidates = [(values[0], 0.), (values[-1], 1.)]
    for i in range(33):
        if (i==0 or values[i] <= values[i-1]) and (i==32 or values[i] <= values[i+1]):
            fitted = minimize_scalar(distance, bounds=(grid[max(0,i-1)], grid[min(32,i+1)]), method="bounded", options={"xatol": 1e-13})
            candidates.append((float(fitted.fun), float(fitted.x)))
    candidates.sort()
    if len(candidates) > 1 and abs(candidates[1][0]-candidates[0][0]) <= 1e-6 and abs(candidates[1][1]-candidates[0][1]) > 1e-6:
        raise ArcEditError("ARC_EDIT_MARKER_AMBIGUOUS", "目标存在多个等近沿线位置；请显式指定fraction")
    return candidates[0][1]


def _join_evidence(project, identifiers):
    from .route_geometry import route_segments
    segments = route_segments(project)
    rows = {}
    for i, point in enumerate(project["route"]["points"]):
        if point["id"] not in identifiers:
            continue
        incoming = segments[i-1].tangent_at_fraction(1.) if i else None
        outgoing = segments[i].tangent_at_fraction(0.) if i < len(segments) else None
        rows[point["id"]] = {"incoming_tangent_deg":incoming, "outgoing_tangent_deg":outgoing,
            "turn_deg":None if incoming is None or outgoing is None else (outgoing-incoming+180.)%360.-180.}
    return rows


def edit_arc_project(project, config=None):
    """Return an unsaved project candidate. Public moves address Rigid points."""
    return _edit_intrinsic_project(project, config, constraint_editor=False)


def _edit_intrinsic_project(project, config=None, *, constraint_editor=False):
    from .constraints import (ConstraintError, _points, _rigid_path, _signature,
                              reconcile_route_structure)
    from .arc_edit_geometry import rebuild_arc_endpoints
    from .altercourse import _manufacturing

    c = _config(config, constraint_editor=constraint_editor)
    before = analyze_project(project)
    if not any(leg.get("geometry") is not None for leg in project["route"].get("legs", [])):
        raise ArcEditError("ARC_EDIT_NO_ARC", "工程无内禀圆弧，请使用普通坐标编辑")
    if constraint_editor and not project["route"].get("constraint_state"):
        raise ConstraintError("CONSTRAINT_NOT_CONFIGURED", "请先配置约束")
    rigid, segments, _ = _rigid_path(project)
    oldpoints, by, indexes = _points(project)
    skeleton = deepcopy(project)
    skeleton["route"]["points"] = deepcopy(rigid)
    newby = {p["id"]: p for p in skeleton["route"]["points"]}
    source_links = {link["point_id"]:link for link in project["route"].get("path_links", [])}
    fractions, stations, moved = {}, {}, []
    for move in c["moves"]:
        pid = move["point_id"]
        if pid not in by:
            raise ArcEditError("ARC_EDIT_POINT_REFERENCE", "移点ID不属于当前工程")
        point, typ = by[pid], by[pid].get("constraint", "rigid")
        moved.append(pid)
        if typ == "rigid":
            if c.get("automatic", False):
                raise ConstraintError("CONSTRAINT_RIGID_AUTOMOVE", "自动联动不能移动Rigid")
            if set(move) != {"point_id", "longitude", "latitude"}:
                raise ValueError("Rigid move须仅含point_id/longitude/latitude")
            lon, lat = coordinate(move["longitude"], move["latitude"])
            target = newby[pid]
            if (target["longitude"], target["latitude"]) != (lon, lat):
                target.update(longitude=lon, latitude=lat, depth_m=None)
        elif not constraint_editor:
            raise ArcEditError("ARC_EDIT_POINT_TYPE", "人工端点编辑只移动Rigid；沿线点使用既有fraction或实物站编辑入口")
        elif typ == "sliding" and project["route"].get("mode") == "fixed":
            if set(move) != {"point_id", "cable_kp_m"}:
                raise ConstraintError("CONSTRAINT_SLIDING_STATION", "Fixed Sliding只能修改实物cable_kp_m")
            if pid not in source_links:
                raise ConstraintError("CONSTRAINT_UNLINKED_SLIDING", "Fixed Sliding缺少实际Path Link")
            stations[pid] = finite_number(move["cable_kp_m"], "cable_kp_m", minimum=0,
                maximum=project["route"]["constraint_state"]["manufacturing"]["physical_length_m"])
        else:
            if "fraction" in move:
                if set(move) != {"point_id", "fraction"}:
                    raise ValueError("fraction move不能同时指定坐标或实物站")
                fraction = finite_number(move["fraction"], "fraction", minimum=0, maximum=1)
            else:
                if set(move) != {"point_id", "longitude", "latitude"}:
                    raise ValueError("沿线坐标move须含longitude/latitude")
                anchors = [i for i,p in enumerate(rigid) if p["id"] == point.get("anchor_start_id")]
                if not anchors or anchors[0] >= len(segments) or rigid[anchors[0]+1]["id"] != point.get("anchor_end_id"):
                    raise ConstraintError("CONSTRAINT_ANCHOR_DOMAIN", "沿线点缺少相邻实际Rigid锚点域")
                j = anchors[0]
                fraction = _nearest_fraction(segments[j], coordinate(move["longitude"], move["latitude"]))
            if not 1e-10 < fraction < 1-1e-10:
                raise ConstraintError("CONSTRAINT_POINT_AT_ANCHOR", "沿线点不能与锚点重合")
            fractions[pid] = fraction
    options = {(o["start_point_id"], o["end_point_id"]): o for o in c["arc_options"]}
    available = {(a["id"], b["id"]): i for i, (a,b) in enumerate(zip(rigid, rigid[1:])) if segments[i].geometry}
    if set(options)-set(available):
        raise ArcEditError("ARC_EDIT_ARC_REFERENCE", "arc_options须引用相邻Rigid端点之间的真实圆弧")
    tasks = [i for i, segment in enumerate(segments) if segment.geometry and (
        (rigid[i]["id"], rigid[i+1]["id"]) in options or
        any((newby[p["id"]]["longitude"], newby[p["id"]]["latitude"]) != (p["longitude"],p["latitude"]) for p in rigid[i:i+2]))]
    if len(tasks) > c["max_arc_rebuilds"]:
        raise ArcEditError("ARC_EDIT_ARC_BUDGET", "所有相邻圆弧超过重构数量上限；整笔拒绝")
    arc_changes, used = [], 0
    skeleton["route"]["legs"] = []
    for i, segment in enumerate(segments):
        geometry = segment.geometry
        if i in tasks:
            pair = (rigid[i]["id"], rigid[i+1]["id"])
            option = options.get(pair, {})
            remaining = c["max_work_units"]-used
            if remaining < 1:
                raise ArcEditError("ARC_EDIT_WORK_LIMIT", "整笔圆弧重构工作预算耗尽")
            pure = {k:v for k,v in option.items() if k not in {"start_point_id", "end_point_id"}}
            pure["max_work_units"] = remaining
            # The project has already bound its actual persisted endpoints to
            # this primitive. Do not confuse Direct roundtrip nanometres with
            # a user edit, and never accept caller-supplied "old" evidence.
            pure["original_endpoints"] = [[p["longitude"],p["latitude"]] for p in rigid[i:i+2]]
            rebuilt = rebuild_arc_endpoints(newby[pair[0]], newby[pair[1]], geometry, config=pure)
            used += rebuilt["budget"]["work_units"]
            if not rebuilt["accepted"] or rebuilt["geometry"] is None:
                codes = rebuilt.get("rejection_codes", [])
                code = codes[0] if codes else "ARC_EDIT_GEOMETRY_REJECTED"
                if isinstance(code, dict):
                    code = code.get("code", "ARC_EDIT_GEOMETRY_REJECTED")
                raise ArcEditError(code, f"跨度{pair[0]}→{pair[1]}不可重构：{rebuilt.get('rejection_codes')}")
            arc_changes.append({"start_point_id":pair[0], "end_point_id":pair[1],
                "source_leg_indexes":list(range(indexes[pair[0]], indexes[pair[1]])),
                "old_geometry":geometry, "new_geometry":deepcopy(rebuilt["geometry"]),
                "evidence":deepcopy(rebuilt["evidence"]), "budget":deepcopy(rebuilt["budget"])})
            geometry = rebuilt["geometry"]
        skeleton["route"]["legs"].append({"geometry":geometry} if geometry else {})
    coordinate_changed = any((newby[p["id"]]["longitude"],newby[p["id"]]["latitude"]) != (p["longitude"],p["latitude"]) for p in rigid)
    changed = (coordinate_changed or any(x["new_geometry"] != x["old_geometry"] for x in arc_changes)
               or any(by[p].get("fraction") != f for p,f in fractions.items())
               or any(source_links[p]["cable_kp_m"] != s for p,s in stations.items()))
    if changed:
        solved = reconcile_route_structure(project, skeleton, marker_fractions=fractions, sliding_stations=stations)
        result = solved["project"]
        # A genuinely unchanged span must not lose its business coordinates,
        # depth or descriptors because length-fraction inversion rounds by a
        # few nanometres. Actual Sliding stock relocation and explicit marker
        # moves remain authoritative and are not snapped back.
        stable_markers = set()
        for i, (a,b) in enumerate(zip(rigid,rigid[1:])):
            unchanged = (skeleton["route"]["legs"][i].get("geometry") == segments[i].geometry
                and all((newby[p["id"]]["longitude"],newby[p["id"]]["latitude"]) == (p["longitude"],p["latitude"]) for p in (a,b)))
            if unchanged:
                stable_markers.update(p["id"] for p in oldpoints[indexes[a["id"]]+1:indexes[b["id"]]] if p["id"] not in fractions and p["id"] not in stations)
        for point in result["route"]["points"]:
            pid = point["id"]
            if pid in stable_markers and inverse(point["longitude"],point["latitude"],by[pid]["longitude"],by[pid]["latitude"],"geodesic")[0] <= 1e-7:
                point["longitude"], point["latitude"] = by[pid]["longitude"], by[pid]["latitude"]
                if "depth_m" in by[pid]:
                    point["depth_m"] = by[pid]["depth_m"]
                else:
                    point.pop("depth_m", None)
        # Unaffected actual legs retain the exact persisted descriptor. These
        # are business spans, not dense display samples or reconstructed copies.
        oldlegs = project["route"]["legs"]
        affected = {j for x in arc_changes for j in x["source_leg_indexes"]}
        oldpairs = {(a["id"],b["id"]):i for i,(a,b) in enumerate(zip(oldpoints,oldpoints[1:]))}
        for i,(a,b) in enumerate(zip(result["route"]["points"],result["route"]["points"][1:])):
            oldi = oldpairs.get((a["id"],b["id"]))
            if oldi is not None and oldi not in affected and all((p["longitude"],p["latitude"]) == (by[p["id"]]["longitude"],by[p["id"]]["latitude"]) for p in (a,b)):
                if oldlegs[oldi].get("geometry") is not None:
                    result["route"]["legs"][i]["geometry"] = deepcopy(oldlegs[oldi]["geometry"])
        if result["route"].get("constraint_state"):
            result["route"]["constraint_state"]["materialized_signature"] = _signature(result)
        if not set(by) <= {p["id"] for p in result["route"]["points"]}:
            raise ArcEditError("ARC_EDIT_POINT_ID_LOSS", "候选不能删除已有业务点ID")
        after = analyze_project(result)
        warnings = deepcopy(solved["warnings"])
        warnings.append({"code":"ARC_EDIT_DEPTH_REQUIRES_RESAMPLING", "severity":"warning", "message":"实际移动点水深为空；原剖面和侧坡签名保留，须真实重采样"})
        reconciliation = solved["report"]
    else:
        result, after, warnings, reconciliation = deepcopy(project), before, [], None
    modes = {l["mode"] for l in before["legs"]}
    selected = moved[0] if moved else c["arc_options"][0]["start_point_id"] if c["arc_options"] else rigid[0]["id"]
    join_ids = set(moved) | {x[k] for x in arc_changes for k in ("start_point_id", "end_point_id")}
    old_joins, new_joins = _join_evidence(project, join_ids), _join_evidence(result, join_ids)
    report = {"operation":"arc_edit", "config":c, "changed":changed, "moved_point_ids":moved,
        "result_selection_point_id":selected, "arc_changes":arc_changes,
        "joins":[{"point_id":p["id"], "before":old_joins[p["id"]], "after":new_joins[p["id"]]} for p in oldpoints if p["id"] in join_ids],
        "generated_marker_ids":[p["id"] for p in result["route"]["points"] if p["id"] not in by],
        "before":{"surface_length_m":before["summary"]["surface_length_m"], "point_count":len(oldpoints)},
        "after":{"surface_length_m":after["summary"]["surface_length_m"], "point_count":len(result["route"]["points"])},
        "manufacturing":_manufacturing(before,after,next(iter(modes)) if len(modes)==1 else "mixed"),
        "constraint_reconciliation":reconciliation,
        "profile_invalidation":{"present":bool(project.get("profile")), "invalidated":changed and bool(project.get("profile")), "old_route_signature":route_signature(project), "new_route_signature":route_signature(result), "policy":"old_signature_retained_no_depth_recertification"},
        "side_slopes_invalidation":{"present":bool(project.get("side_slopes")), "invalidated":changed and bool(project.get("side_slopes")), "policy":"old_signature_retained"},
        "budget":{"arc_rebuild_count":len(tasks), "max_arc_rebuilds":c["max_arc_rebuilds"], "work_units":used, "max_work_units":c["max_work_units"], "max_output_bytes":c["max_output_bytes"],
                  "work_basis":"actual pure arc-reconstruction native and RouteSegment evaluations; excludes project admission, stock reconciliation, marker projection, join diagnostics, analysis and JSON encoding; not elapsed CPU or FLOPs"}}
    out = {"project":result, "report":report, "warnings":warnings}
    if len(json.dumps(out,ensure_ascii=False,allow_nan=False,separators=(",", ":")).encode("utf-8")) > c["max_output_bytes"]:
        raise ArcEditError("ARC_EDIT_OUTPUT_LIMIT", "完整候选超过输出预算；不截断")
    return out
