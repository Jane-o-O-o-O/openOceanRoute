"""Explicit path/assembly links and fixed manufacturing-domain route editing.

Rigid vertices define geometry, clamped markers follow one rigid-to-rigid curve,
and fixed-mode sliding markers follow physical cable stations. This is an
independent, documented interpretation of the published planning semantics.
"""
from __future__ import annotations

import bisect
from copy import deepcopy
import hashlib
import json
import math

from .core import analyze_project, route_signature
from .geodesy import coordinate, finite_number, interpolate, inverse
from .tools import _canonical_allowances, _depth_at, _effective_leg

EPS = 1e-6


class ConstraintError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _config(config):
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("config 必须为对象")
    return config


def _signature(project):
    route = project["route"]
    body = {"curve": route.get("curve", "rhumb"), "mode": route.get("mode", "flexible"),
            "points": [{k: p.get(k) for k in ("id", "longitude", "latitude", "constraint", "anchor_start_id", "anchor_end_id", "fraction")}
                       for p in route.get("points", [])],
            "legs": [{k:l.get(k) for k in ("mode", "slack_basis", "slack_pct", "fixed_cable_length_m", "cable_type_id", "allowance_m", "allowance_cable_type_id")}
                     for l in route.get("legs", [])], "slack_pct": route.get("slack_pct", 0),
            "slack_basis": route.get("slack_basis", "surface"), "path_links": route.get("path_links", []),
            "allowances": [{k:a.get(k) for k in ("kp_m", "length_m", "cable_type_id", "reserved_for_body_id")}
                           for a in route.get("allowances", project.get("allowances", []))],
            "bodies": [{k:b.get(k) for k in ("id", "kp_m", "cable_kp_m", "length_m", "length_mode")}
                       for b in project.get("bodies", [])],
            "manufacturing": route.get("constraint_state", {}).get("manufacturing"),
            "assembly_references": [{k:r.get(k) for k in ("id", "cable_kp_m", "length_m")}
                                    for r in project.get("assembly_references", [])]}
    def normalize(value):
        # JSON in the browser has a single Number type: 118.0 -> 118 and -0
        # -> 0. Equal physical values must have equal material signatures.
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value
    return hashlib.sha256(json.dumps(normalize(body), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_materialized_constraints(project):
    """Called by core: plain coordinate/material patches cannot bypass the solver."""
    state = project.get("route", {}).get("constraint_state")
    if state is None:
        return
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ConstraintError("CONSTRAINT_STATE_INVALID", "不支持的约束状态版本，请重新配置")
    if state.get("materialized_signature") != _signature(project):
        raise ConstraintError("CONSTRAINT_UNSOLVED_EDIT", "约束路线被直接修改；请通过约束编辑入口重算，或明确重新配置")


def _points(project):
    points = project["route"]["points"]
    return points, {p["id"]: p for p in points}, {p["id"]: i for i, p in enumerate(points)}


def _station(points, curve):
    keys = [0.0]
    for a, b in zip(points, points[1:]):
        keys.append(keys[-1] + inverse(a["longitude"], a["latitude"], b["longitude"], b["latitude"], curve)[0])
    return keys


def _at(points, keys, kp, curve):
    kp = max(0.0, min(keys[-1], kp))
    i = max(0, min(len(points)-2, bisect.bisect_right(keys, kp)-1))
    width = keys[i+1]-keys[i]
    return interpolate(points[i]["longitude"], points[i]["latitude"], points[i+1]["longitude"], points[i+1]["latitude"],
                       (kp-keys[i])/width if width > EPS else 0, curve)


def _base_at(kp, analysis):
    keys = [p["kp_m"] for p in analysis["rpl"]]
    if kp >= keys[-1]:
        return sum(l["cable_length_m"] for l in analysis["legs"])
    i = max(0, min(len(keys)-2, bisect.bisect_right(keys, kp)-1))
    leg = analysis["legs"][i]
    return sum(l["cable_length_m"] for l in analysis["legs"][:i]) + (kp-keys[i])/leg["surface_length_m"]*leg["cable_length_m"]


def _surface_at(base, analysis):
    cumulative = 0.0
    for leg in analysis["legs"]:
        if base <= cumulative+leg["cable_length_m"]+EPS:
            fraction = (base-cumulative)/leg["cable_length_m"] if leg["cable_length_m"] > EPS else 0
            return leg["start_kp_m"] + max(0, min(1, fraction))*leg["surface_length_m"]
        cumulative += leg["cable_length_m"]
    return analysis["summary"]["surface_length_m"]


def _manufacturing(project, analysis):
    segments, cumulative = [], 0.0
    for i, leg in enumerate(analysis["legs"]):
        segments.append({"start_m": cumulative, "end_m": cumulative+leg["cable_length_m"], "cable_type_id": leg["cable_type_id"]})
        cumulative += leg["cable_length_m"]
    extras = []
    for body, computed in zip(project.get("bodies", []), analysis["bodies"]):
        if body.get("length_mode", "replace") == "additional" and computed["length_m"]:
            extras.append({"kind": "body", "body_id": computed["id"], "kp_m": computed["kp_m"], "length_m": computed["length_m"]})
    for allowance in _canonical_allowances(project, analysis, include_leg=True):
        extras.append({"kind": "allowance", "kp_m": allowance["kp_m"], "length_m": allowance["length_m"], "value": allowance})
    extras.sort(key=lambda e: e["kp_m"])
    added = 0.0
    for extra in extras:
        extra["base_station_m"] = _base_at(extra["kp_m"], analysis)
        extra["start_m"] = extra["base_station_m"]+added
        extra["end_m"] = extra["start_m"]+extra["length_m"]
        added += extra["length_m"]
    return {"base_length_m": cumulative, "physical_length_m": cumulative+added, "segments": segments, "extras": extras,
            "body_stations": {b["id"]: b["cable_kp_m"] for b in analysis["bodies"]},
            "reference_stations": {r["id"]: r["cable_kp_m"] for r in analysis.get("assembly_references",[])}}


def _physical_to_base(value, manufacture, interior=False):
    value = finite_number(value, "cable_kp_m", minimum=0, maximum=manufacture["physical_length_m"]+EPS)
    value = min(value, manufacture["physical_length_m"])
    removed = 0.0
    for extra in manufacture["extras"]:
        if value < extra["start_m"]-EPS:
            break
        if value <= extra["end_m"]+EPS:
            if interior and extra["start_m"]+EPS < value < extra["end_m"]-EPS:
                raise ConstraintError("CONSTRAINT_LINK_IN_INSERT", "余缆津贴或附加组件内部不能作为余缆域边界")
            return extra["base_station_m"]
        removed += extra["length_m"]
    return min(manufacture["base_length_m"], max(0, value-removed))


def _base_to_physical(value, manufacture):
    return value + sum(e["length_m"] for e in manufacture["extras"] if e["base_station_m"] <= value+EPS)


def _point_types(project):
    points, by_id, indices = _points(project)
    rigid = []
    for i, point in enumerate(points):
        point["constraint"] = point.get("constraint", "rigid")
        if point["constraint"] not in ("rigid", "clamped", "sliding"):
            raise ConstraintError("CONSTRAINT_POINT_TYPE", "constraint 必须为 rigid/clamped/sliding")
        if point["constraint"] == "rigid":
            rigid.append(i)
    if not rigid or rigid[0] != 0 or rigid[-1] != len(points)-1:
        raise ConstraintError("CONSTRAINT_ENDPOINT_TYPE", "路线两端必须为 rigid 点")
    curve = project["route"].get("curve", "rhumb")
    for i, point in enumerate(points):
        if point["constraint"] == "rigid":
            continue
        left = max(j for j in rigid if j < i)
        right = min(j for j in rigid if j > i)
        start = str(point.get("anchor_start_id", points[left]["id"]))
        end = str(point.get("anchor_end_id", points[right]["id"]))
        if start not in by_id or end not in by_id or indices[start] != left or indices[end] != right:
            raise ConstraintError("CONSTRAINT_ANCHOR_DOMAIN", "Clamped/Sliding 必须明确绑定相邻两个 rigid 点，不能跨转折")
        a, b = by_id[start], by_id[end]
        length = inverse(a["longitude"], a["latitude"], b["longitude"], b["latitude"], curve)[0]
        if length <= EPS:
            raise ConstraintError("CONSTRAINT_ZERO_DOMAIN", "约束锚线长度为零")
        fraction = point.get("fraction")
        if fraction is None:
            fraction = inverse(a["longitude"], a["latitude"], point["longitude"], point["latitude"], curve)[0]/length
        fraction = finite_number(fraction, "fraction", minimum=0, maximum=1)
        if fraction <= 1e-10 or fraction >= 1-1e-10:
            raise ConstraintError("CONSTRAINT_POINT_AT_ANCHOR", "非 rigid 点必须在两锚点内部，不能与锚点重合")
        expected = interpolate(a["longitude"], a["latitude"], b["longitude"], b["latitude"], fraction, curve)
        error = inverse(*expected, point["longitude"], point["latitude"], curve)[0]
        if error > max(.001, length*1e-9):
            raise ConstraintError("CONSTRAINT_ALTERCOURSE", f"点 {point['id']} 不在锚线，应先投影／插点；Clamped/Sliding 不允许转折")
        point.update(anchor_start_id=start, anchor_end_id=end, fraction=fraction)


def _links(project, analysis, supplied=None):
    points, by_id, indices = _points(project)
    manufacture = _manufacturing(project, analysis)
    if supplied is None:
        supplied = project["route"].get("path_links")
    if supplied is None:
        boundaries = {0, len(points)-1}
        for i in range(1, len(points)-1):
            a, b = analysis["legs"][i-1:i+1]
            oa, ob = _effective_leg(project, analysis, i-1), _effective_leg(project, analysis, i)
            if points[i]["constraint"] != "sliding" and (a["cable_type_id"] != b["cable_type_id"] or oa["slack_pct"] != ob["slack_pct"] or a["slack_basis"] != b["slack_basis"]):
                boundaries.add(i)
        supplied = [{"id": f"link-{p['id']}", "point_id": p["id"], "slack_change": i in boundaries}
                    for i, p in enumerate(points) if i in boundaries or p["constraint"] == "sliding"]
    if not isinstance(supplied, list) or len(supplied) > 10000:
        raise ValueError("path_links 必须为数组，最多 10,000 项")
    seen, linked, links = set(), set(), []
    for raw in supplied:
        if not isinstance(raw, dict):
            raise ValueError("path_link 必须为对象")
        value = deepcopy(raw)
        pid = str(value.get("point_id", ""))
        identifier = str(value.get("id", f"link-{pid}"))
        if pid not in by_id or not identifier or identifier in seen or pid in linked:
            raise ConstraintError("CONSTRAINT_LINK_REFERENCE", "Path Link 的 id、point_id 必须有效且唯一")
        i = indices[pid]
        boundary = value.get("slack_change", by_id[pid]["constraint"] != "sliding")
        if not isinstance(boundary, bool) or boundary and by_id[pid]["constraint"] == "sliding":
            raise ConstraintError("CONSTRAINT_SLIDING_BOUNDARY", "Sliding Link 不能是 Slack-Change 域边界")
        default = 0.0 if i == 0 else analysis["rpl"][i]["cable_kp_m"]
        station = finite_number(value.get("cable_kp_m", default), "cable_kp_m", minimum=0, maximum=manufacture["physical_length_m"]+EPS)
        expected_base = _base_at(analysis["rpl"][i]["kp_m"], analysis)
        base = _physical_to_base(station, manufacture, interior=boundary)
        if abs(base-expected_base) > max(1e-4, manufacture["physical_length_m"]*1e-10):
            raise ConstraintError("CONSTRAINT_LINK_MISMATCH", "配置时链接缆KP须匹配当前点，不能借配置改制造站位；使用编辑移动 Sliding")
        item = value.get("assembly_item_id")
        item_stations={**manufacture["body_stations"],**manufacture.get("reference_stations",{})}
        if item is not None and (str(item) not in item_stations or abs(item_stations[str(item)]-station) > 1e-4):
            raise ConstraintError("CONSTRAINT_ITEM_LINK", "链接组件不存在或组件起始实物KP与链接不一致")
        value.update(id=identifier, point_id=pid, cable_kp_m=min(station, manufacture["physical_length_m"]), slack_change=boundary)
        seen.add(identifier); linked.add(pid); links.append(value)
    links.sort(key=lambda l: indices[l["point_id"]])
    boundaries = [l for l in links if l["slack_change"]]
    if len(boundaries) < 2 or boundaries[0]["point_id"] != points[0]["id"] or boundaries[-1]["point_id"] != points[-1]["id"]:
        raise ConstraintError("CONSTRAINT_ENDPOINT_LINK", "两端必须有 Slack-Change 链接")
    if abs(boundaries[0]["cable_kp_m"]) > EPS or abs(boundaries[-1]["cable_kp_m"]-manufacture["physical_length_m"]) > EPS:
        raise ConstraintError("CONSTRAINT_ENDPOINT_STATION", "两端链接必须匹配 0 和装配总实物缆长")
    for a, b in zip(boundaries, boundaries[1:]):
        if _physical_to_base(b["cable_kp_m"], manufacture)-_physical_to_base(a["cable_kp_m"], manufacture) <= EPS:
            raise ConstraintError("CONSTRAINT_ZERO_DOMAIN", "约束域必须有正的基础制造缆量")
    if any(p["constraint"] == "sliding" and p["id"] not in linked for p in points):
        raise ConstraintError("CONSTRAINT_UNLINKED_SLIDING", "每个 Sliding 点必须有 Path Link")
    return links, manufacture


def configure_constraints(project, config=None):
    """Capture current manufacturing without rearranging its stations or lengths."""
    config = _config(config)
    source = deepcopy(project)
    # Reconfiguration is explicit; stale state is removed before normal analysis.
    source["route"].pop("constraint_state", None)
    before = analyze_project(source)
    if config.get("clear") is True:
        source["route"].pop("path_links", None)
        for point in source["route"]["points"]:
            for key in ("constraint", "anchor_start_id", "anchor_end_id", "fraction"):
                point.pop(key, None)
        return {"project":source,"warnings":[{"code":"CONSTRAINT_STATE_CLEARED","severity":"warning",
                "message":"已明确解除 Path Link 域；当前固定分段制造量仍保留，后续结构编辑不再按旧域联动"}],
                "report":{"operation":"clear_constraints","before_summary":before["summary"],"after_summary":before["summary"]}}
    if any(l["surface_length_m"] <= EPS or l["cable_length_m"] <= EPS for l in before["legs"]):
        raise ConstraintError("CONSTRAINT_ZERO_DOMAIN", "约束编辑要求各段有正的平面距离与基础缆长；请先合并重复点或明确附加缆长事件")
    result = deepcopy(source)
    route = result["route"]
    for i, point in enumerate(route["points"]):
        point["id"] = before["rpl"][i]["id"]
    for body, computed in zip(result.get("bodies", []), before["bodies"]):
        body["id"] = computed["id"]
    updates = config.get("points", [])
    if not isinstance(updates, list):
        raise ValueError("points 配置必须为数组")
    _, by_id, _ = _points(result)
    updated = set()
    for item in updates:
        if not isinstance(item, dict) or str(item.get("point_id", "")) not in by_id or item["point_id"] in updated:
            raise ConstraintError("CONSTRAINT_POINT_REFERENCE", "点配置引用无效或重复")
        updated.add(item["point_id"])
        point = by_id[item["point_id"]]
        for key in ("constraint", "anchor_start_id", "anchor_end_id", "fraction"):
            if key in item:
                point[key] = item[key]
    _point_types(result)
    mode = config.get("mode", route.get("mode", "flexible"))
    if mode not in ("fixed", "flexible"):
        raise ValueError("mode 必须为 fixed 或 flexible")
    if len({l["mode"] for l in before["legs"]}) > 1:
        raise ConstraintError("CONSTRAINT_MIXED_MODE", "约束引擎暂不支持域内固定／柔性混合；请先明确统一模式")
    links, manufacture = _links(result, before, config.get("path_links"))
    route["path_links"] = links
    route["mode"] = mode
    route["legs"] = [_effective_leg(source, before, i) for i in range(len(before["legs"]))]
    for i, opt in enumerate(route["legs"]):
        opt["mode"] = mode
        if mode == "fixed":
            opt["fixed_cable_length_m"] = before["legs"][i]["cable_length_m"]
        else:
            # Switching modes does not change a single length. The actual prior
            # surface slack becomes the next flexible target.
            opt["slack_basis"] = "surface"
            opt["slack_pct"] = before["legs"][i]["surface_slack_pct"]
            opt["fixed_cable_length_m"] = None
    route["constraint_state"] = {"version": 1, "manufacturing": manufacture}
    route["constraint_state"]["materialized_signature"] = _signature(result)
    after = analyze_project(result)
    return {"project": result, "warnings": [], "report": {"operation": "configure_constraints", "mode": mode,
            "manufacturing_captured": True, "path_link_count": len(links), "domain_count": sum(l["slack_change"] for l in links)-1,
            "before_summary": before["summary"], "after_summary": after["summary"], "mode_switch_length_delta_m": after["summary"]["cable_length_m"]-before["summary"]["cable_length_m"]}}


def _project_fraction(point, start, end, curve):
    """Bounded golden-section nearest point on the actual selected curve."""
    target = coordinate(point["longitude"], point["latitude"])
    def objective(f):
        p = interpolate(start["longitude"], start["latitude"], end["longitude"], end["latitude"], f, curve)
        return inverse(*p, *target, "geodesic")[0]
    a, b, ratio = 0.0, 1.0, (math.sqrt(5)-1)/2
    c, d = b-ratio*(b-a), a+ratio*(b-a)
    fc, fd = objective(c), objective(d)
    for _ in range(70):
        if fc < fd:
            b, d, fd = d, c, fc; c = b-ratio*(b-a); fc = objective(c)
        else:
            a, c, fc = c, d, fd; d = a+ratio*(b-a); fd = objective(d)
    return (a+b)/2


def _profile_after(source, result, before, rigid_old, rigid_new, old_keys, new_keys):
    curve = source["route"].get("curve", "rhumb")
    samples, changed = [], []
    for i, (a, b) in enumerate(zip(rigid_old, rigid_old[1:])):
        na, nb = rigid_new[i:i+2]
        unchanged = all(a.get(k) == na.get(k) and b.get(k) == nb.get(k) for k in ("longitude", "latitude"))
        lo, hi = old_keys[i:i+2]; nlo, nhi = new_keys[i:i+2]
        if unchanged:
            selected = [p for p in before["profile"] if lo-EPS <= p["kp_m"] <= hi+EPS]
            selected += [{"kp_m": lo, "depth_m": _depth_at(before["profile"], lo)}, {"kp_m": hi, "depth_m": _depth_at(before["profile"], hi)}]
            samples.extend({"kp_m": nlo+max(0,min(hi-lo,p["kp_m"]-lo)), "depth_m": p["depth_m"]} for p in selected)
        else:
            changed.append([nlo,nhi])
            samples.extend({"kp_m": kp, "depth_m": None} for kp in (nlo,(nlo+nhi)/2,nhi))
    # At shared endpoints an unchanged surveyed endpoint is reusable, although
    # the neighbouring changed interval still contains an explicit missing node.
    samples.sort(key=lambda p:p["kp_m"])
    clean=[]
    for sample in samples:
        if clean and abs(sample["kp_m"]-clean[-1]["kp_m"]) <= EPS:
            if clean[-1]["depth_m"] is None and sample["depth_m"] is not None:
                clean[-1]=sample
        else:
            clean.append(sample)
    oldmeta=before["profile_metadata"]
    original=source.get("profile", {})
    result["profile"]={"samples":clean,"route_signature":route_signature(result),
                       "source": "constraints_partial_profile" if changed else oldmeta["source"],
                       "measured": bool(oldmeta["measured"]) and not changed,
                       "metadata": {"original_source":oldmeta,"invalid_ranges_m":changed}}
    if not changed and original:
        result["profile"]={**deepcopy(original),"route_signature":route_signature(result)}
    return changed


def edit_constrained_project(project, config=None):
    config = _config(config)
    before = analyze_project(project)
    state = project["route"].get("constraint_state")
    if not state:
        raise ConstraintError("CONSTRAINT_NOT_CONFIGURED", "请先配置约束点与 Path Link")
    result = deepcopy(project)
    route = result["route"]
    points, by_id, _ = _points(result)
    oldpoints, oldby, _ = _points(project)
    mode, curve = route.get("mode","flexible"), route.get("curve","rhumb")
    links = {l["point_id"]: l for l in route["path_links"]}
    manufacture = state["manufacturing"]
    moves = config.get("moves",[])
    if not isinstance(moves,list) or len(moves)>10000:
        raise ValueError("moves 必须为数组，最多 10,000 项")
    seen, moved = set(), []
    for move in moves:
        if not isinstance(move,dict) or str(move.get("point_id","")) not in by_id or move["point_id"] in seen:
            raise ConstraintError("CONSTRAINT_POINT_REFERENCE", "移点引用无效或重复")
        pid=move["point_id"]; seen.add(pid); point=by_id[pid]
        typ=point["constraint"]
        if typ=="rigid":
            if config.get("automatic",False):
                raise ConstraintError("CONSTRAINT_RIGID_AUTOMOVE", "自动联动不能移动 Rigid；用户明确编辑时才可移动")
            point["longitude"],point["latitude"]=coordinate(move.get("longitude"),move.get("latitude"))
        elif typ=="sliding" and mode=="fixed":
            if "longitude" in move or "latitude" in move or "fraction" in move:
                raise ConstraintError("CONSTRAINT_SLIDING_STATION", "Fixed Sliding 只能明确修改实物 cable_kp_m")
            value=finite_number(move.get("cable_kp_m"),"cable_kp_m",minimum=0,maximum=manufacture["physical_length_m"])
            item=str(links[pid].get("assembly_item_id",""))
            if item in manufacture["body_stations"] and abs(value-links[pid]["cable_kp_m"])>EPS:
                raise ConstraintError("CONSTRAINT_ITEM_MANUFACTURE_EDIT", "修改已链接有限组件的制造位置须重新配置装配，不能借移点改变材料占用")
            links[pid]["cable_kp_m"]=value
            if item in manufacture.get("reference_stations",{}):
                reference=next(r for r in result["assembly_references"] if r["id"]==item)
                reference.update(cable_kp_m=value,start_m=value,end_m=value)
                route["constraint_state"]["manufacturing"]["reference_stations"][item]=value
        else:
            if "fraction" in move:
                fraction=finite_number(move["fraction"],"fraction",minimum=0,maximum=1)
            else:
                fraction=_project_fraction(move,by_id[point["anchor_start_id"]],by_id[point["anchor_end_id"]],curve)
            if not 1e-10 < fraction < 1-1e-10:
                raise ConstraintError("CONSTRAINT_POINT_AT_ANCHOR","沿线点不能与锚点重合")
            point["fraction"]=fraction
        moved.append(pid)
    rigid_old=[deepcopy(p) for p in oldpoints if p["constraint"]=="rigid"]
    rigid_new=[p for p in points if p["constraint"]=="rigid"]
    old_rigid_keys=_station(rigid_old,curve); new_rigid_keys=_station(rigid_new,curve)
    if any(b-a<=EPS for a,b in zip(new_rigid_keys,new_rigid_keys[1:])):
        raise ConstraintError("CONSTRAINT_ZERO_DOMAIN", "编辑后相邻 rigid 锚点重合，无法定义沿线约束")
    skeleton=rigid_new
    for point in points:
        if point["constraint"]=="clamped" or point["constraint"]=="sliding" and mode=="flexible":
            a,b=by_id[point["anchor_start_id"]],by_id[point["anchor_end_id"]]
            point["longitude"],point["latitude"]=interpolate(a["longitude"],a["latitude"],b["longitude"],b["latitude"],point["fraction"],curve)
    rindices={p["id"]:i for i,p in enumerate(rigid_new)}
    positions={p["id"]:new_rigid_keys[i] for i,p in enumerate(rigid_new)}
    for point in points:
        if point["constraint"]!="rigid" and not(point["constraint"]=="sliding" and mode=="fixed"):
            i=rindices[point["anchor_start_id"]]
            positions[point["id"]]=new_rigid_keys[i]+point["fraction"]*(new_rigid_keys[i+1]-new_rigid_keys[i])
    boundaries=[l for l in route["path_links"] if l["slack_change"]]
    original_order={p["id"]:i for i,p in enumerate(oldpoints)}
    boundaries.sort(key=lambda l:original_order[l["point_id"]])
    if any(positions[a["point_id"]]>=positions[b["point_id"]]-EPS for a,b in zip(boundaries,boundaries[1:])):
        raise ConstraintError("CONSTRAINT_LINK_ORDER", "Slack-Change 链接次序翻转或域缩成零长度")
    old_kps={p["id"]:p["kp_m"] for p in before["rpl"]}
    domains=[]
    for a,b in zip(boundaries,boundaries[1:]):
        base_a=_physical_to_base(a["cable_kp_m"],manufacture,True); base_b=_physical_to_base(b["cable_kp_m"],manufacture,True)
        pa,pb=positions[a["point_id"]],positions[b["point_id"]]
        oa,ob=old_kps[a["point_id"]],old_kps[b["point_id"]]
        inside=[p for p in rigid_new if oa-EPS<=old_kps[p["id"]]<=ob+EPS]
        changed=abs((pb-pa)-(ob-oa))>EPS or any(any(p[k]!=oldby[p["id"]][k] for k in ("longitude","latitude")) for p in inside)
        # A linked clamped boundary can move while both geographic rigid anchors
        # stay put. That changes the two neighbouring domains, not the outside.
        changed=changed or any(by_id[pid].get("fraction")!=oldby[pid].get("fraction") for pid in (a["point_id"],b["point_id"]))
        domains.append({"id":f"{a['id']}::{b['id']}","start_link_id":a["id"],"end_link_id":b["id"],
                        "start_point_id":a["point_id"],"end_point_id":b["point_id"],
                        "old_start_kp_m":oa,"old_end_kp_m":ob,"start_kp_m":pa,"end_kp_m":pb,
                        "base_start_m":base_a,"base_end_m":base_b,"available_base_length_m":base_b-base_a,
                        "physical_length_m":b["cable_kp_m"]-a["cable_kp_m"],"changed":changed})
    def domain_base(base):
        return next((d for d in domains if base<=d["base_end_m"]+EPS),domains[-1])
    def domain_surface(kp):
        return next((d for d in domains if kp<=d["end_kp_m"]+EPS),domains[-1])
    def new_at_base(base):
        d=domain_base(base)
        if d["changed"]:
            return d["start_kp_m"]+(base-d["base_start_m"])/d["available_base_length_m"]*(d["end_kp_m"]-d["start_kp_m"])
        return _surface_at(base,before)+d["start_kp_m"]-d["old_start_kp_m"]
    def base_at_new(kp):
        d=domain_surface(kp)
        if d["changed"]:
            return d["base_start_m"]+(kp-d["start_kp_m"])/(d["end_kp_m"]-d["start_kp_m"])*d["available_base_length_m"]
        return _base_at(kp-d["start_kp_m"]+d["old_start_kp_m"],before)
    if mode=="fixed":
        for point in points:
            if point["constraint"]=="sliding":
                positions[point["id"]]=new_at_base(_physical_to_base(links[point["id"]]["cable_kp_m"],manufacture))
        # Every manufactured cable-type boundary must materialize as a point.
        # A formerly unlinked bend stays geographic while its transition slides.
        segments=manufacture["segments"]
        for left,right in zip(segments,segments[1:]):
            if left["cable_type_id"]==right["cable_type_id"]:
                continue
            kp=new_at_base(left["end_m"])
            if any(abs(v-kp)<=EPS for v in positions.values()):
                continue
            pid=f"constraint-transition-{len(points)+1}"
            while pid in by_id:
                pid+="_"
            point={"id":pid,"label":"制造缆型转换","constraint":"sliding","generated_constraint_transition":True,
                   "note":"固定制造站位自动生成的缆型转换点"}
            points.append(point);by_id[pid]=point;positions[pid]=kp
            link={"id":f"link-{pid}","point_id":pid,"cable_kp_m":_base_to_physical(left["end_m"],manufacture),"slack_change":False}
            route["path_links"].append(link);links[pid]=link
        for point in points:
            if point["constraint"]=="sliding":
                kp=positions[point["id"]]
                if kp<=EPS or kp>=new_rigid_keys[-1]-EPS:
                    raise ConstraintError("CONSTRAINT_SLIDING_ENDPOINT", "Sliding 实物站位落在路线端点，请改为端点链接")
                point["longitude"],point["latitude"]=_at(skeleton,new_rigid_keys,kp,curve)
                i=max(0,min(len(skeleton)-2,bisect.bisect_right(new_rigid_keys,kp)-1))
                if abs(kp-new_rigid_keys[i])<=EPS or abs(kp-new_rigid_keys[i+1])<=EPS:
                    raise ConstraintError("CONSTRAINT_POINT_COLLISION", "Sliding 与 rigid 转折站位重合，需显式合并链接")
                point.update(anchor_start_id=skeleton[i]["id"],anchor_end_id=skeleton[i+1]["id"],
                             fraction=(kp-new_rigid_keys[i])/(new_rigid_keys[i+1]-new_rigid_keys[i]))
    ordered=sorted(points,key=lambda p:positions[p["id"]])
    if any(positions[b["id"]]-positions[a["id"]]<=EPS for a,b in zip(ordered,ordered[1:])):
        raise ConstraintError("CONSTRAINT_POINT_COLLISION", "沿线路点重合，请明确合并后重配约束")
    if len(ordered)>10000:
        raise ConstraintError("CONSTRAINT_RESULT_LIMIT","约束结果超过10,000点")
    route["points"]=ordered
    # Geographic attribute/events move within the same rigid anchor segment.
    def remap(kp):
        i=max(0,min(len(rigid_old)-2,bisect.bisect_right(old_rigid_keys,kp)-1))
        fraction=(kp-old_rigid_keys[i])/(old_rigid_keys[i+1]-old_rigid_keys[i])
        return new_rigid_keys[i]+fraction*(new_rigid_keys[i+1]-new_rigid_keys[i])
    def old_at_new(kp):
        i=max(0,min(len(rigid_new)-2,bisect.bisect_right(new_rigid_keys,kp)-1))
        f=(kp-new_rigid_keys[i])/(new_rigid_keys[i+1]-new_rigid_keys[i])
        return old_rigid_keys[i]+f*(old_rigid_keys[i+1]-old_rigid_keys[i])
    actual_keys=_station(ordered,curve)
    opts=[]
    basecuts=[s["start_m"] for s in manufacture["segments"]]
    oldkeys=[p["kp_m"] for p in before["rpl"]]
    for i,(a,b) in enumerate(zip(ordered,ordered[1:])):
        left,right=positions[a["id"]],positions[b["id"]]
        middle=(left+right)/2
        oi=max(0,min(len(before["legs"])-1,bisect.bisect_right(oldkeys,old_at_new(middle))-1))
        opt=_effective_leg(project,before,oi)
        opt["allowance_m"]=0
        opt["stop_hours"]=0;opt["extra_cost"]=0
        endpoint=original_order.get(b["id"])
        if endpoint is not None and endpoint>0:
            oldopt=_effective_leg(project,before,endpoint-1)
            opt["stop_hours"]=oldopt.get("stop_hours",0);opt["extra_cost"]=oldopt.get("extra_cost",0)
        if mode=="fixed":
            low,high=base_at_new(left),base_at_new(right)
            mi=max(0,min(len(basecuts)-1,bisect.bisect_right(basecuts,(low+high)/2)-1))
            opt.update(mode="fixed",fixed_cable_length_m=max(0,high-low),cable_type_id=manufacture["segments"][mi]["cable_type_id"])
        else:
            # Flexible sliding is clamped: order and original per-point cable /
            # target assignments remain meaningful; no manufacturing freeze.
            if [p["id"] for p in ordered]!=[p["id"] for p in oldpoints]:
                raise ConstraintError("CONSTRAINT_FLEXIBLE_ORDER","柔性沿线标记不能越过相邻点／转换点")
            opt=_effective_leg(project,before,i)
            opt.update(mode="flexible",fixed_cable_length_m=None)
            if opt["slack_basis"]=="bottom" and any(d["changed"] for d in domains):
                raise ConstraintError("CONSTRAINT_BOTTOM_REQUIRES_TERRAIN","改变几何的柔性底余缆须先重新采样二维地形")
        opts.append(opt)
    route["legs"]=opts
    if mode=="fixed":
        route["allowances"]=[]
        for extra in manufacture["extras"]:
            kp=new_at_base(extra["base_station_m"])
            if extra["kind"]=="allowance":
                route["allowances"].append({**deepcopy(extra["value"]),"kp_m":kp})
            else:
                next(b for b in result["bodies"] if str(b.get("id"))==extra["body_id"])["kp_m"]=kp
        for body,computed in zip(result.get("bodies",[]),before["bodies"]):
            if body.get("length_mode","replace")=="replace":
                body["id"]=computed["id"];body["cable_kp_m"]=manufacture["body_stations"][computed["id"]];body.pop("kp_m",None)
    else:
        route["allowances"]=[{**a,"kp_m":remap(a["kp_m"])} for a in _canonical_allowances(project,before)]
        for body,computed in zip(result.get("bodies",[]),before["bodies"]):
            body["id"]=computed["id"]
            linked=next((l for l in links.values() if str(l.get("assembly_item_id",""))==body["id"]),None)
            if linked:
                body["kp_m"]=positions[linked["point_id"]];body.pop("cable_kp_m",None)
            elif body.get("length_mode","replace")=="additional":
                body["kp_m"]=remap(computed["kp_m"])
            else:
                body["cable_kp_m"]=computed["cable_kp_m"];body.pop("kp_m",None)
    result.pop("allowances",None)
    result["events"]=[{**deepcopy(e),"kp_m":remap(e["kp_m"])} for e in project.get("events",project["route"].get("events",[]))]
    route.pop("events",None)
    invalid=_profile_after(project,result,before,rigid_old,rigid_new,old_rigid_keys,new_rigid_keys)
    for point in ordered:
        point["depth_m"]=_depth_at(result["profile"]["samples"],positions[point["id"]])
    # Numerical actual KP differs by sub-micrometres from symbolic skeleton KP.
    # Assembly endpoint events may use that tolerant last station in core.
    route["path_links"].sort(key=lambda l:positions[l["point_id"]])
    route["constraint_state"]["materialized_signature"]=_signature(result)
    if mode=="flexible" and any(l.get("assembly_item_id") in manufacture.get("reference_stations",{}) for l in links.values()):
        # Linked zero-length references follow the new point's computed cable
        # station. Analyze without the old reference first so a shorter route
        # cannot reject a reference which is about to move back into range.
        temporary=deepcopy(result)
        temporary["assembly_references"]=[]
        temporary["route"]["constraint_state"]["materialized_signature"]=_signature(temporary)
        preliminary=analyze_project(temporary)
        stations={p["id"]:p["cable_kp_m"] for p in preliminary["rpl"]}
        for reference in result.get("assembly_references",[]):
            linked=next((l for l in links.values() if l.get("assembly_item_id")==reference["id"]),None)
            if linked:
                value=stations[linked["point_id"]]
                reference.update(cable_kp_m=value,start_m=value,end_m=value)
        route["constraint_state"]["materialized_signature"]=_signature(result)
    after=analyze_project(result)
    if mode=="fixed":
        tolerance=max(1e-5,manufacture["physical_length_m"]*1e-10)
        old_material={m["cable_type_id"]:m["length_m"] for m in before["materials"]}
        new_material={m["cable_type_id"]:m["length_m"] for m in after["materials"]}
        same_material=all(abs(new_material.get(t,0)-length)<=tolerance for t,length in old_material.items())
        same_bodies=all(abs(b["cable_kp_m"]-manufacture["body_stations"][b["id"]])<=tolerance for b in after["bodies"])
        if abs(after["summary"]["cable_length_m"]-manufacture["physical_length_m"])>tolerance or not same_material or not same_bodies:
            raise ConstraintError("CONSTRAINT_MANUFACTURING_INVARIANT", "求解未通过制造总量、缆型或有限组件站位守恒；未返回违规候选")
    if mode=="flexible":
        rows={p["id"]:p for p in after["rpl"]}
        for link in route["path_links"]:
            link["cable_kp_m"]=0.0 if link["point_id"]==ordered[0]["id"] else rows[link["point_id"]]["cable_kp_m"]
        route["constraint_state"]["manufacturing"]=_manufacturing(result,after)
        route["constraint_state"]["materialized_signature"]=_signature(result)
    warnings=[]
    after_indices={p["id"]:i for i,p in enumerate(after["rpl"])}
    for d in domains:
        length=d["end_kp_m"]-d["start_kp_m"]
        d["surface_slack_pct"]=100*(d["available_base_length_m"]/length-1) if mode=="fixed" else None
        d["manufacturing_delta_m"]=0.0 if mode=="fixed" else None
        selected=after["legs"][after_indices[d["start_point_id"]]:after_indices[d["end_point_id"]]]
        bottom=sum(l["bottom_length_m"] for l in selected) if all(l["bottom_length_m"] is not None for l in selected) else None
        d["bottom_length_m"]=bottom
        d["bottom_slack_pct"]=100*(d["available_base_length_m"]/bottom-1) if mode=="fixed" and bottom is not None and bottom>EPS else None
        d["uniform_surface_slack_applied"]=mode=="fixed" and d["changed"]
        d["shortage_m"]=max(0,length-d["available_base_length_m"]) if mode=="fixed" else None
        if mode=="fixed" and d["shortage_m"]>EPS:
            warnings.append({"code":"CONSTRAINT_DOMAIN_SHORTAGE","severity":"error","message":"固定制造缆量不足以覆盖约束域，须增加缆量或重新分配链接","domain_id":d["id"],"shortage_m":d["shortage_m"]})
    if invalid:
        warnings.append({"code":"CONSTRAINT_PROFILE_INVALIDATED","severity":"warning","message":"改变的地理区间标为缺测；未改变曲线保留原剖面来源","ranges_m":invalid})
    return {"project":result,"warnings":warnings,"report":{"operation":"edit_constraints","mode":mode,"moved_point_ids":moved,
            "domains":domains,"invalid_profile_ranges_m":invalid,"before_summary":before["summary"],"after_summary":after["summary"],
            "link_placements":[{"id":l["id"],"point_id":l["point_id"],"cable_kp_m":l["cable_kp_m"],
                                "kp_m":positions[l["point_id"]],"slack_change":l["slack_change"]} for l in route["path_links"]],
            "physical_length_delta_m":after["summary"]["cable_length_m"]-before["summary"]["cable_length_m"],
            "assumptions":["Rigid坐标只由用户明确编辑；Clamped绑定相邻Rigid曲线的距离分数。",
                           "固定域基础缆量按统一平面余缆分配；无水平距离的津贴／附加组件单独保留制造站位。",
                           "组件以有限长度占用实物装配；未链接替换组件保持实物缆KP。",
                           "实际几何或装配的直接修改必须重新配置；不宣称原厂项目格式兼容。"]}}


def solve_constraints(project, config=None):
    config=_config(config)
    if config.get("moves"):
        raise ValueError("solve不接受moves，请使用edit")
    return edit_constrained_project(project,config)
