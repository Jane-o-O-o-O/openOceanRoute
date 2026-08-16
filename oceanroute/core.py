"""Independent cable route planning and explicit engineering accounting.

All lengths are metres; depth is positive downward; slack values are percent.
The result is a planning estimate and has not been calibrated to Makai outputs.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from copy import deepcopy

from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform

from .geodesy import coordinate, densify, finite_number, interpolate, inverse, split_antimeridian


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    return value


def _list(value, name, maximum=100000):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} 必须是数组，最多 {maximum} 项")
    return value


def _number(obj, key, default=0.0, minimum=None, maximum=None):
    return finite_number(obj.get(key, default), key, minimum=minimum, maximum=maximum)


def route_signature(project: dict) -> str:
    """Hash only spatial geometry and distance model, not labels or slack edits."""
    project = _object(project, "project")
    route = _object(project.get("route", {}), "route")
    points = _list(route.get("points", []), "route.points", 10000)
    coordinates = [coordinate(_object(p, "point").get("longitude"), p.get("latitude")) for p in points]
    body = {"crs": project.get("crs", "EPSG:4326"), "curve": route.get("curve", "rhumb"), "points": coordinates}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def densify_route(project: dict, max_step_m: float = 5000.0) -> list[list[float]]:
    """Render actual chosen curve; interpolation honours surface-distance KP."""
    route = _object(_object(project, "project").get("route", {}), "route")
    points = _validate_points(route)
    spacing = finite_number(max_step_m, "max_step_m", minimum=1)
    curve = route.get("curve", "rhumb")
    coords = []
    for a, b in zip(points, points[1:]):
        leg = densify(a["longitude"], a["latitude"], b["longitude"], b["latitude"], curve, spacing)
        coords.extend(leg if not coords else leg[1:])
    return [[lon, lat] for lon, lat in coords]


def _warning(warnings, code, message, severity="warning", point_id=None, **extra):
    warnings.append({"code": code, "message": message, "severity": severity, "point_id": point_id, **extra})


def _validate_points(route):
    points = _list(route.get("points", []), "route.points", 10000)
    if len(points) < 2:
        raise ValueError("路线至少需要两个点")
    normalized = []
    seen = set()
    for i, original in enumerate(points):
        p = deepcopy(_object(original, f"point[{i}]"))
        p["longitude"], p["latitude"] = coordinate(p.get("longitude"), p.get("latitude"))
        p["id"] = str(p.get("id", f"p{i + 1}"))
        if p["id"] in seen:
            raise ValueError("路线点 id 不能重复")
        seen.add(p["id"])
        if p.get("depth_m") is not None:
            p["depth_m"] = _number(p, "depth_m", minimum=0)
        else:
            p["depth_m"] = None
        normalized.append(p)
    return normalized


def _profiles(project, points, kps, signature, warnings, terrain_signature=None):
    profile_obj = _object(project.get("profile", {}) or {}, "profile")
    raw = _list(profile_obj.get("samples", []), "profile.samples")
    source = profile_obj.get("source", "user")
    geometry_valid = bool(raw) and profile_obj.get("route_signature") == signature
    terrain_meta = _object(profile_obj.get("metadata", {}) or {}, "profile.metadata")
    library_bound = terrain_meta.get("model") in {"priority-terrain-library-v1", "terrain-library-derived-profile-v1"}
    library_valid = True
    if library_bound:
        from .terrain_sources import terrain_library_signature
        if terrain_signature is None:
            terrain_signature = terrain_library_signature(project.get("terrain_sources", []))
        library_valid = terrain_meta.get("terrain_library_signature") == terrain_signature
        if raw and not library_valid:
            _warning(warnings, "TERRAIN_LIBRARY_STALE", "共享地形内容、解释、启用状态或优先级已变化，旧剖面已停用；请重新采样")
    valid = geometry_valid and library_valid
    if raw and not geometry_valid:
        _warning(warnings, "PROFILE_UNBOUND" if not profile_obj.get("route_signature") else "PROFILE_STALE",
                 "导入剖面缺少当前路线签名，已停用" if not profile_obj.get("route_signature") else "路线几何已变化，旧剖面已停用",
                 "warning")
    samples = []
    # Validate even stale data: malformed values must never quietly escape checks.
    for i, p in enumerate(raw):
        p = _object(p, "profile.sample")
        kp = _number(p, "kp_m", minimum=0)
        depth = None if p.get("depth_m") is None else _number(p, "depth_m", minimum=0)
        if samples and kp <= samples[-1]["kp_m"]:
            raise ValueError("剖面 KP 必须严格递增，不能重复或乱序")
        samples.append({"kp_m": kp, "depth_m": depth})
    if raw and not valid:
        # An explicitly supplied survey profile takes precedence over incidental
        # waypoint depths. A changed route needs fresh sampling or an explicit
        # removal of that profile before waypoint approximation is selected.
        samples = [{"kp_m": kp, "depth_m": None} for kp in sorted(set(kps))]
        source = ("stale_terrain_library_unavailable" if library_bound and not library_valid else
                  "stale_profile_unavailable" if profile_obj.get("route_signature") else "unbound_profile_unavailable")
    elif not valid:
        samples = []
        # A repeated position with two inconsistent depths is not a vertical seabed.
        for kp, p in zip(kps, points):
            if samples and abs(kp - samples[-1]["kp_m"]) < 1e-8:
                if p["depth_m"] != samples[-1]["depth_m"]:
                    samples[-1]["depth_m"] = None
                    _warning(warnings, "DUPLICATE_DEPTH_CONFLICT", "同一路线位置的水深不同，不能计算该处海底距离", point_id=p["id"])
                continue
            samples.append({"kp_m": kp, "depth_m": p["depth_m"]})
        source = "waypoint_linear_approximation"
        if any(s["depth_m"] is not None for s in samples):
            _warning(warnings, "WAYPOINT_DEPTH_APPROXIMATION", "海底剖面由路线点水深分段线性插值；未代表实测连续地形", "info")
        else:
            _warning(warnings, "MISSING_DEPTH", "未提供有效水深，海底距离和底余缆未计算")
    elif str(source).lower().startswith("synthetic"):
        _warning(warnings, "SYNTHETIC_BATHYMETRY", "当前剖面为合成演示地形，请导入工程测深后重新核算", "info")
    sample_kps = [s["kp_m"] for s in samples]

    def depth_at(kp):
        if not samples or kp < sample_kps[0] - 1e-7 or kp > sample_kps[-1] + 1e-7:
            return None
        idx = bisect.bisect_left(sample_kps, kp)
        if idx < len(samples) and abs(sample_kps[idx] - kp) < 1e-7:
            return samples[idx]["depth_m"]
        if idx == len(samples) and abs(sample_kps[-1] - kp) < 1e-7:
            return samples[-1]["depth_m"]
        if idx == 0 or idx == len(samples):
            return None
        left, right = samples[idx - 1], samples[idx]
        if left["depth_m"] is None or right["depth_m"] is None:
            return None
        f = (kp - left["kp_m"]) / (right["kp_m"] - left["kp_m"])
        return left["depth_m"] + f * (right["depth_m"] - left["depth_m"])

    # Insert every leg boundary before integrating: summing whole samples outside
    # a leg would misallocate seabed length and slack to the neighbouring cable.
    def near_boundary(k):
        idx = bisect.bisect_left(kps, k)
        return (idx < len(kps) and abs(kps[idx] - k) < 1e-7) or (idx > 0 and abs(kps[idx - 1] - k) < 1e-7)

    grid = sorted(set(kps + [k for k in sample_kps if 0 <= k <= kps[-1] and not near_boundary(k)]))
    enriched, bottom_kp = [], 0.0
    for kp in grid:
        depth = depth_at(kp)
        slope = None
        if enriched:
            prev = enriched[-1]
            horizontal = kp - prev["kp_m"]
            if depth is None or prev["depth_m"] is None:
                bottom_kp = None
            else:
                delta = depth - prev["depth_m"]
                slope = math.degrees(math.atan2(delta, horizontal)) if horizontal > 0 else None
                if bottom_kp is not None:
                    bottom_kp += math.hypot(horizontal, delta)
        elif depth is None:
            bottom_kp = None
        enriched.append({"kp_m": kp, "depth_m": depth, "bottom_kp_m": bottom_kp, "slope_deg": slope})
    if any(p["depth_m"] is None for p in enriched) and any(p["depth_m"] is not None for p in enriched):
        _warning(warnings, "PROFILE_GAPS", "剖面未覆盖全路线或含缺测；有缺测的区间不计算海底距离和底余缆")
    metadata = {"source": source, "route_signature": signature, "imported_profile_valid": valid,
                                      "model": "piecewise_linear_kp_depth", "measured": valid and bool(profile_obj.get("measured", False)) and not str(source).lower().startswith("synthetic")}
    if library_bound:
        metadata.update(terrain_library_signature=terrain_signature, terrain_library_valid=library_valid,
                        profile_terrain_library_signature=terrain_meta.get("terrain_library_signature"))
    return enriched, depth_at, metadata


def _leg_bottom(profile, start, end, profile_kps=None):
    profile_kps = profile_kps if profile_kps is not None else [p["kp_m"] for p in profile]
    lo, hi = bisect.bisect_left(profile_kps, start - 1e-7), bisect.bisect_right(profile_kps, end + 1e-7)
    selected = profile[lo:hi]
    if start == end:
        return (0.0, None, 0.0) if selected and selected[0]["depth_m"] is not None else (None, None, None)
    if len(selected) < 2 or any(p["depth_m"] is None for p in selected):
        return None, None, None
    bottom, maximum = 0.0, 0.0
    for a, b in zip(selected, selected[1:]):
        dx, dz = b["kp_m"] - a["kp_m"], b["depth_m"] - a["depth_m"]
        bottom += math.hypot(dx, dz)
        maximum = max(maximum, abs(math.degrees(math.atan2(dz, dx))))
    slope = math.degrees(math.atan2(selected[-1]["depth_m"] - selected[0]["depth_m"], end - start))
    return bottom, slope, maximum


def _library(project, warnings):
    raw = _list(project.get("cable_types", []), "cable_types", 10000)
    if not raw:
        raw = [{"id": "GENERIC", "name": "待指定电缆", "cost_per_m": 0, "lay_speed_m_s": 1.0}]
        _warning(warnings, "CABLE_DEFAULT", "未设置缆型库，暂用零单价及 1 m/s 船速，请补充真实参数")
    types = {}
    for raw_type in raw:
        item = deepcopy(_object(raw_type, "cable_type"))
        key = str(item.get("id", ""))
        if not key or key in types:
            raise ValueError("电缆型号 id 必须非空且唯一")
        item["id"] = key
        item["cost_per_m"] = _number(item, "cost_per_m", minimum=0)
        item["lay_speed_m_s"] = _number(item, "lay_speed_m_s", 1.0, minimum=0)
        if item["lay_speed_m_s"] == 0:
            raise ValueError("lay_speed_m_s 必须大于零；停船时间请录入事件")
        for prop in ("diameter_m", "wet_weight_n_m", "ea_n", "ei_n_m2", "max_tension_n", "min_bend_radius_m"):
            if prop in item:
                item[prop] = _number(item, prop, minimum=0)
        types[key] = item
    return types


def _route_at(kp, points, kps, curve):
    idx = max(0, min(len(points) - 2, bisect.bisect_right(kps, kp) - 1))
    a, b = points[idx], points[idx + 1]
    f = (kp - kps[idx]) / (kps[idx + 1] - kps[idx]) if kps[idx + 1] > kps[idx] else 0
    return interpolate(a["longitude"], a["latitude"], b["longitude"], b["latitude"], max(0, min(1, f)), curve)


def _assembly(project, route, points, kps, legs, types, warnings):
    base_total = sum(l["cable_length_m"] for l in legs)
    base_starts, cumulative = [], 0.0
    for leg in legs:
        base_starts.append(cumulative)
        cumulative += leg["cable_length_m"]

    def base_cable_at(kp):
        if kp >= kps[-1]:
            return base_total
        idx = max(0, min(len(legs) - 1, bisect.bisect_right(kps, kp) - 1))
        length = legs[idx]["surface_length_m"]
        fraction = (kp - kps[idx]) / length if length > 0 else 0.0
        return base_starts[idx] + fraction * legs[idx]["cable_length_m"]

    def base_route_at(cable_kp):
        if cable_kp >= base_total:
            return kps[-1]
        idx = max(0, min(len(legs) - 1, bisect.bisect_right(base_starts, cable_kp) - 1))
        c = legs[idx]["cable_length_m"]
        f = (cable_kp - base_starts[idx]) / c if c > 0 else 0.0
        return kps[idx] + f * legs[idx]["surface_length_m"]

    bodies = deepcopy(_list(project.get("bodies", []), "bodies", 10000))
    body_ids = set()
    extra = []
    for i, body in enumerate(bodies):
        _object(body, "body")
        body["id"] = str(body.get("id", f"b{i + 1}"))
        if body["id"] in body_ids:
            raise ValueError("附属体 id 不能重复")
        body_ids.add(body["id"])
        body["length_m"] = _number(body, "length_m", minimum=0)
        body["cost"] = _number(body, "cost", minimum=0)
        body["length_mode"] = body.get("length_mode", "replace")
        if body["length_mode"] not in ("replace", "additional"):
            raise ValueError("body.length_mode 必须为 replace 或 additional")
        if body.get("cable_kp_m") is not None:
            body["cable_kp_m"] = _number(body, "cable_kp_m", minimum=0)
            # Explicit cable KP is final assembly coordinate. Additional inserts
            # are only unambiguous when tied to a spatial route KP.
            if body["length_mode"] == "additional":
                raise ValueError("additional 附属体请使用路线 kp_m 定位，不可同时用 cable_kp_m")
        else:
            body["kp_m"] = min(kps[-1], _number(body, "kp_m", minimum=0, maximum=kps[-1] + 1e-7))
            if body["length_mode"] == "additional" and body["length_m"] > 0:
                extra.append({"kp_m": body["kp_m"], "length_m": body["length_m"], "kind": "body", "body_id": body["id"], "name": body.get("name", "附属体")})
    allowances = _list(route.get("allowances", project.get("allowances", [])), "allowances", 10000)
    for a in allowances:
        a = _object(a, "allowance")
        tid = a.get("cable_type_id")
        if tid is not None and str(tid) not in types:
            raise ValueError(f"附加缆长引用未知缆型 {tid}")
        extra.append({"kp_m": min(kps[-1], _number(a, "kp_m", minimum=0, maximum=kps[-1] + 1e-7)),
                      "length_m": _number(a, "length_m", minimum=0), "kind": "allowance", "name": a.get("name", "附加缆长"),
                      "cable_type_id": str(tid) if tid is not None else None, "reserved_for_body_id": a.get("reserved_for_body_id")})
    for i, options in enumerate(route.get("legs", [])):
        if options.get("allowance_m", 0):
            tid = str(options.get("allowance_cable_type_id", legs[i]["cable_type_id"]))
            if tid not in types:
                raise ValueError(f"段尾附加缆长引用未知缆型 {tid}")
            extra.append({"kp_m": kps[i + 1], "length_m": _number(options, "allowance_m", minimum=0), "kind": "allowance", "name": "段尾附加缆长", "cable_type_id": tid})
    extra.sort(key=lambda x: x["kp_m"])
    extra_total = 0.0
    for event in extra:
        event["start_m"] = base_cable_at(event["kp_m"]) + extra_total
        event["end_m"] = event["start_m"] + event["length_m"]
        extra_total += event["length_m"]
    total = base_total + extra_total
    extra_kps = [e["kp_m"] for e in extra]
    extra_prefix = [0.0]
    for e in extra:
        extra_prefix.append(extra_prefix[-1] + e["length_m"])
    extra_starts = [e["start_m"] for e in extra]

    def cable_at(kp):
        return base_cable_at(kp) + extra_prefix[bisect.bisect_right(extra_kps, kp + 1e-8)]

    def route_at_cable(ckp):
        idx = bisect.bisect_right(extra_starts, ckp) - 1
        if idx >= 0 and ckp <= extra[idx]["end_m"]:
            return extra[idx]["kp_m"]
        removed = extra_prefix[idx + 1]
        return base_route_at(ckp - removed)

    body_spans = []
    for body in bodies:
        if body["length_mode"] == "additional" and body["length_m"] > 0:
            event = next(e for e in extra if e.get("body_id") == body["id"])
            start = event["start_m"]
        elif body.get("cable_kp_m") is not None:
            start = body["cable_kp_m"]
            body["kp_m"] = route_at_cable(start)
        else:
            # Additional events at a location precede a replacement located at
            # that same route KP. This also gives explicit allowance jumps.
            start = cable_at(body["kp_m"])
        end = start + body["length_m"]
        if end > total + 1e-7 or start > total + 1e-7:
            raise ValueError(f"附属体 {body['id']} 超出缆装配长度")
        body["cable_kp_m"] = start
        body["start_m"], body["end_m"] = start, min(end, total)
        body_spans.append(body)
    occupied = sorted([b for b in body_spans if b["length_m"] > 0], key=lambda b: b["start_m"])
    occupied_starts = [b["start_m"] for b in occupied]
    for a, b in zip(occupied, occupied[1:]):
        if b["start_m"] < a["end_m"] - 1e-7:
            raise ValueError(f"附属体 {a['id']} 与 {b['id']} 的实物长度重叠")

    boundaries = {0.0, total}
    for kp in kps:
        boundaries.add(cable_at(kp))
    for e in extra:
        boundaries.update((e["start_m"], e["end_m"]))
    for b in body_spans:
        boundaries.update((b["start_m"], b["end_m"]))
    boundaries = sorted(boundaries)
    sld, type_lengths = [], {key: 0.0 for key in types}
    for start, end in zip(boundaries, boundaries[1:]):
        if end - start < 1e-8:
            continue
        midpoint = (start + end) / 2.0
        body_index = bisect.bisect_right(occupied_starts, midpoint) - 1
        body = occupied[body_index] if body_index >= 0 and midpoint < occupied[body_index]["end_m"] else None
        if body:
            sld.append({"kind": "body", "body_kind": body.get("kind", "body"), "id": body["id"], "name": body.get("name", "附属体"),
                        "start_m": start, "end_m": end, "kp_m": body["kp_m"], "length_mode": body["length_mode"], "cable_type_id": None})
            continue
        route_kp = route_at_cable(midpoint)
        idx = max(0, min(len(legs) - 1, bisect.bisect_right(kps, route_kp + 1e-7) - 1))
        cable_type = legs[idx]["cable_type_id"]
        event_index = bisect.bisect_right(extra_starts, midpoint) - 1
        if event_index >= 0 and extra[event_index]["kind"] == "allowance" and midpoint < extra[event_index]["end_m"]:
            kind = "allowance"
            if extra[event_index].get("cable_type_id") is not None:
                cable_type = extra[event_index]["cable_type_id"]
        else:
            kind = "cable"
        segment = {"kind": kind, "name": types[cable_type].get("name", cable_type), "start_m": start, "end_m": end,
                   "cable_type_id": cable_type, "leg_index": idx, "route_start_kp_m": route_at_cable(start), "route_end_kp_m": route_at_cable(end)}
        sld.append(segment)
        type_lengths[cable_type] += end - start
    # Zero-length joints still need a visible manufacturing/event entry.
    for b in body_spans:
        if b["length_m"] == 0:
            sld.append({"kind": "body", "body_kind": b.get("kind", "body"), "id": b["id"], "name": b.get("name", "附属体"),
                        "start_m": b["start_m"], "end_m": b["end_m"], "kp_m": b["kp_m"], "cable_type_id": None})
    references, reference_ids = [], set()
    for i, original in enumerate(_list(project.get("assembly_references", []), "assembly_references", 10000)):
        reference = deepcopy(_object(original, "assembly_reference"))
        identifier = str(reference.get("id", f"reference-{i+1}"))
        if not identifier or identifier in reference_ids or identifier in body_ids:
            raise ValueError("制造参考 id 必须非空、唯一且不与组件冲突")
        reference_ids.add(identifier)
        station = min(total, _number(reference, "cable_kp_m", minimum=0, maximum=total + 1e-7))
        if _number(reference, "length_m", minimum=0) != 0:
            raise ValueError("制造参考不占实物长度，length_m 必须为零")
        reference.update(id=identifier, kind="reference", start_m=station, end_m=station, length_m=0.0,
                         cable_kp_m=station, kp_m=route_at_cable(station), cable_type_id=None)
        references.append(reference)
        sld.append(reference)
    sld.sort(key=lambda s: (s["start_m"], s["kind"] != "body"))
    if extra:
        _warning(warnings, "ASSEMBLY_ADDITIONAL_LENGTH", "附加缆长和 additional 附属体独立增加实物装配长度；其位置没有平面距离，局部余缆百分比不适用", "info")
    material = sum(type_lengths.values())
    return {"sld": sld, "bodies": body_spans, "cable_at": cable_at, "total": total,
            "material_length": material, "body_length": sum(b["length_m"] for b in body_spans),
            "allowance_length": sum(e["length_m"] for e in extra if e["kind"] == "allowance" and not e.get("reserved_for_body_id")),
            "type_lengths": type_lengths, "extras": extra, "assembly_references": references}


def _intersection_points(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if geometry.geom_type == "MultiPoint":
        return list(geometry.geoms)
    if geometry.geom_type in ("LineString", "LinearRing"):
        return [Point(geometry.coords[0]), Point(geometry.coords[-1])]
    if hasattr(geometry, "geoms"):
        return [p for part in geometry.geoms for p in _intersection_points(part)]
    return []


def _crossings(project, points, kps, curve, corridor, warnings):
    """Densified routes and per-leg local azimuthal equidistant GIS checks.

    This is a screening calculation, not a cadastral accuracy guarantee. The
    source GeoJSON's sparse edges are straight lines in the local projection.
    """
    layers = _list(project.get("layers", []), "layers", 1000)
    features = []
    for layer in layers:
        _object(layer, "layer")
        geojson = _object(layer.get("geojson", {}), "layer.geojson")
        feature_list = geojson.get("features", []) if geojson.get("type") == "FeatureCollection" else [geojson]
        for index, feature in enumerate(_list(feature_list, "geojson.features", 20000)):
            _object(feature, "feature")
            geometry = feature.get("geometry") if feature.get("type") == "Feature" else feature
            if geometry is None:
                continue
            try:
                geom = shape(geometry)
                if geom.is_empty:
                    continue
                if not geom.is_valid:
                    _warning(warnings, "INVALID_LAYER_GEOMETRY", f"图层 {layer.get('name', '')} 含无效几何，已跳过")
                    continue
                # Validate geometry bounds. Longitude unrolling is internal only.
                minx, miny, maxx, maxy = geom.bounds
                if not all(math.isfinite(v) for v in geom.bounds) or minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
                    raise ValueError("GeoJSON 经纬度越界")
                features.append((layer, feature.get("id", index), feature.get("properties", {}), geom))
            except (TypeError, KeyError, AttributeError, ValueError) as exc:
                raise ValueError(f"GeoJSON 几何无效: {exc}") from exc
    if not features:
        return []
    crossings = []
    dedupe = set()
    for leg_idx, (a, b) in enumerate(zip(points, points[1:])):
        if kps[leg_idx + 1] - kps[leg_idx] < 1e-6:
            continue
        middle_lon, middle_lat = interpolate(a["longitude"], a["latitude"], b["longitude"], b["latitude"], .5, curve)
        crs = CRS.from_proj4(f"+proj=aeqd +lat_0={middle_lat} +lon_0={middle_lon} +datum=WGS84 +units=m")
        forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        backward = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        coords = densify(a["longitude"], a["latitude"], b["longitude"], b["latitude"], curve, 2000, 2000)
        route_line = LineString([forward.transform(lon, lat) for lon, lat in coords])
        for layer, feature_id, props, geom in features:
            projected = transform(forward.transform, geom)
            if projected.is_empty or not all(math.isfinite(v) for v in projected.bounds):
                _warning(warnings, "LAYER_PROJECTION_FAILED", "图层几何超出局部投影可用范围，已跳过", layer_id=layer.get("id"))
                continue
            if not projected.is_valid:
                continue
            is_area = geom.geom_type in ("Polygon", "MultiPolygon")
            intersection = route_line.intersection(projected)
            if intersection.is_empty:
                if corridor > 0 and is_area and layer.get("kind") in ("restricted", "exclusion", "hazard"):
                    clearance = route_line.distance(projected)
                    if clearance < corridor:
                        _warning(warnings, "RULE_CORRIDOR_PROXIMITY", f"路线距图层 {layer.get('name', '')} 约 {clearance:.1f} m，小于走廊 {corridor:g} m", leg_index=leg_idx, layer_id=layer.get("id"), clearance_m=clearance)
                continue
            overlap = intersection.geom_type in ("LineString", "MultiLineString") and not is_area
            for pt in _intersection_points(intersection):
                lon, lat = backward.transform(pt.x, pt.y)
                distance, _ = inverse(a["longitude"], a["latitude"], lon, lat, curve)
                kp = min(kps[leg_idx + 1], max(kps[leg_idx], kps[leg_idx] + distance))
                token = (str(layer.get("id")), str(feature_id), round(kp, 2))
                if token in dedupe:
                    continue
                dedupe.add(token)
                angle = None
                if not is_area and geom.geom_type in ("LineString", "MultiLineString") and not overlap:
                    other = projected if projected.geom_type == "LineString" else min(projected.geoms, key=lambda line: line.distance(pt))
                    t = route_line.project(pt)
                    u = other.project(pt)
                    ra, rb = route_line.interpolate(max(0, t - 10)), route_line.interpolate(min(route_line.length, t + 10))
                    oa, ob = other.interpolate(max(0, u - 10)), other.interpolate(min(other.length, u + 10))
                    v = (rb.x - ra.x, rb.y - ra.y)
                    w = (ob.x - oa.x, ob.y - oa.y)
                    norm = math.hypot(*v) * math.hypot(*w)
                    if norm > 0:
                        angle = math.degrees(math.acos(max(-1, min(1, abs(v[0] * w[0] + v[1] * w[1]) / norm))))
                crossings.append({"layer_id": layer.get("id"), "layer_name": layer.get("name", ""), "feature_id": feature_id,
                                  "feature_name": props.get("name", str(feature_id)), "kind": "restricted_area" if is_area else "overlap" if overlap else "crossing",
                                  "kp_m": kp, "longitude": lon, "latitude": lat, "leg_index": leg_idx, "angle_deg": angle,
                                  "model": "densified_route_local_aeqd_screening"})
            if is_area and layer.get("kind") in ("restricted", "exclusion", "hazard"):
                _warning(warnings, "RULE_RESTRICTED_AREA", f"路线进入图层 {layer.get('name', '')} 的限制区域", severity="error", leg_index=leg_idx, layer_id=layer.get("id"))
            elif overlap:
                _warning(warnings, "CABLE_OVERLAP", f"路线与图层 {layer.get('name', '')} 存在重叠区间", leg_index=leg_idx, layer_id=layer.get("id"))
    return sorted(crossings, key=lambda x: x["kp_m"])


def analyze_project(project: dict, *, _terrain_signature=None) -> dict:
    project = _object(project, "project")
    if project.get("crs", "EPSG:4326") != "EPSG:4326":
        raise ValueError("项目内部坐标须为 EPSG:4326；请在导入时转换其他坐标系")
    route = _object(project.get("route", {}), "route")
    curve = route.get("curve", "rhumb")
    if route.get("constraint_state") is not None:
        # Constraints are materialized by the explicit editing solver. Direct
        # coordinate or assembly patches must not bypass domain conservation.
        from .constraints import validate_materialized_constraints
        validate_materialized_constraints(project)
    if curve not in ("rhumb", "geodesic"):
        raise ValueError("route.curve 必须为 rhumb 或 geodesic")
    mode = route.get("mode", "flexible")
    if mode not in ("flexible", "fixed"):
        raise ValueError("route.mode 必须为 flexible 或 fixed")
    basis = route.get("slack_basis", "surface")
    if basis not in ("surface", "bottom"):
        raise ValueError("slack_basis 必须为 surface 或 bottom")
    default_slack = _number(route, "slack_pct", 0.0)
    if default_slack <= -100:
        raise ValueError("slack_pct 必须大于 -100")
    warnings = []
    points = _validate_points(route)
    types = _library(project, warnings)
    costs = _object(project.get("costs", {}), "costs")
    day_rate = _number(costs, "vessel_day_rate", minimum=0)
    burial_rate = _number(costs, "burial_per_m", minimum=0)
    contingency = _number(costs, "contingency_pct", minimum=0)
    currency = str(costs.get("currency", "CNY"))
    for item in types.values():
        if item.get("currency", currency) != currency:
            raise ValueError("所有缆型和项目费用必须使用同一币种")
    options = _list(route.get("legs", []), "route.legs", 10000)
    if len(options) > len(points) - 1:
        raise ValueError("route.legs 数量不能超过路线区间数量")
    for option in options:
        _object(option, "route.leg")
    kps, distances, bearings = [0.0], [], []
    for a, b in zip(points, points[1:]):
        distance, bearing = inverse(a["longitude"], a["latitude"], b["longitude"], b["latitude"], curve)
        distances.append(distance)
        bearings.append(bearing)
        kps.append(kps[-1] + distance)
        if distance < 1e-7:
            _warning(warnings, "ZERO_LENGTH_LEG", "相邻路线点坐标重复，方位及余缆百分比不适用", "info", b["id"])
    signature = route_signature(project)
    profile, depth_at, profile_meta = _profiles(project, points, kps, signature, warnings, _terrain_signature)
    profile_kps = [p["kp_m"] for p in profile]
    rules = _object(project.get("rules", {}), "rules")
    max_slope = _number(rules, "max_slope_deg", 15.0, minimum=0, maximum=90)
    min_slack = _number(rules, "min_slack_pct", 0.0)
    max_slack = _number(rules, "max_slack_pct", 5.0)
    corridor = _number(rules, "corridor_m", 0.0, minimum=0)
    if min_slack > max_slack:
        raise ValueError("min_slack_pct 不能大于 max_slack_pct")
    legs = []
    for i, length in enumerate(distances):
        opt = options[i] if i < len(options) else {}
        type_id = str(opt.get("cable_type_id", next(iter(types))))
        if type_id not in types:
            raise ValueError(f"第 {i + 1} 段引用未知缆型 {type_id}")
        typ = types[type_id]
        bottom, slope, steepest = _leg_bottom(profile, kps[i], kps[i + 1], profile_kps)
        leg_mode = opt.get("mode", mode)
        leg_basis = opt.get("slack_basis", basis)
        if leg_mode not in ("flexible", "fixed") or leg_basis not in ("surface", "bottom"):
            raise ValueError("区间 mode/slack_basis 值无效")
        target_slack = _number(opt, "slack_pct", default_slack)
        if target_slack <= -100:
            raise ValueError("slack_pct 必须大于 -100")
        if leg_mode == "fixed":
            fixed = opt.get("fixed_cable_length_m")
            if fixed is None:
                raise ValueError(f"固定缆长模式第 {i + 1} 段缺少 fixed_cable_length_m")
            cable_length = finite_number(fixed, "fixed_cable_length_m", minimum=0)
        elif leg_basis == "bottom":
            if bottom is None:
                # Unknown physical terrain cannot produce a known requested
                # bottom slack, nor fall back to surface slack without consent.
                raise ValueError(f"第 {i + 1} 段使用底余缆，但有效剖面水深缺失")
            cable_length = bottom * (1.0 + target_slack / 100.0)
        else:
            cable_length = length * (1.0 + target_slack / 100.0)
        surface_slack = 100.0 * (cable_length / length - 1.0) if length > 1e-7 else None
        bottom_slack = 100.0 * (cable_length / bottom - 1.0) if bottom is not None and bottom > 1e-7 else None
        if surface_slack is not None and (surface_slack < min_slack - 1e-8 or surface_slack > max_slack + 1e-8):
            _warning(warnings, "RULE_SLACK_RANGE", f"第 {i + 1} 段平面余缆 {surface_slack:.3f}% 超出 [{min_slack:g}, {max_slack:g}]%", leg_index=i, point_id=points[i]["id"])
        if cable_length + 1e-7 < (bottom if bottom is not None else length):
            _warning(warnings, "CABLE_SHORTAGE", f"第 {i + 1} 段固定或目标缆长不足以覆盖已知路线长度", "error", points[i]["id"], leg_index=i)
        if steepest is not None and steepest > max_slope + 1e-8:
            _warning(warnings, "RULE_SLOPE", f"第 {i + 1} 段最大纵坡 {steepest:.2f}° 超过 {max_slope:g}°", leg_index=i, point_id=points[i]["id"])
        if bottom is None:
            _warning(warnings, "BOTTOM_LENGTH_UNAVAILABLE", f"第 {i + 1} 段缺少连续水深，底距和底余缆未计算", leg_index=i)
        stop_hours = _number(opt, "stop_hours", minimum=0)
        extra_cost = _number(opt, "extra_cost", minimum=0)
        legs.append({"index": i, "start_kp_m": kps[i], "end_kp_m": kps[i + 1], "surface_length_m": length,
                     "bottom_length_m": bottom, "cable_length_m": cable_length, "bearing_deg": bearings[i], "slope_deg": slope,
                     "max_slope_deg": steepest, "surface_slack_pct": surface_slack, "bottom_slack_pct": bottom_slack,
                     "cable_type_id": type_id, "mode": leg_mode, "slack_basis": leg_basis,
                     "time_hours": length / typ["lay_speed_m_s"] / 3600.0 + stop_hours,
                     "material_cost": cable_length * typ["cost_per_m"], "burial": bool(opt.get("burial", False)),
                     "burial_length_m": length if opt.get("burial", False) else 0.0, "extra_cost": extra_cost})
    assembly = _assembly(project, route, points, kps, legs, types, warnings)
    leg_material_lengths = [0.0] * len(legs)
    leg_material_costs = [0.0] * len(legs)
    for segment in assembly["sld"]:
        if "leg_index" in segment:
            leg_material_lengths[segment["leg_index"]] += segment["end_m"] - segment["start_m"]
            leg_material_costs[segment["leg_index"]] += (segment["end_m"] - segment["start_m"]) * types[segment["cable_type_id"]]["cost_per_m"]
    for i, leg in enumerate(legs):
        material_length = leg_material_lengths[i]
        leg["material_length_m"] = material_length
        leg["material_cost"] = leg_material_costs[i]
    rpl, bottom_cumulative = [], 0.0
    for i, point in enumerate(points):
        if i:
            previous_bottom = legs[i - 1]["bottom_length_m"]
            bottom_cumulative = bottom_cumulative + previous_bottom if bottom_cumulative is not None and previous_bottom is not None else None
        leg = legs[i] if i < len(legs) else legs[-1]
        turn = None
        if 0 < i < len(points) - 1 and bearings[i - 1] is not None and bearings[i] is not None:
            turn = (bearings[i] - bearings[i - 1] + 180) % 360 - 180
        rpl.append({"id": point["id"], "index": i, "label": point.get("label", ""), "longitude": point["longitude"], "latitude": point["latitude"],
                    "depth_m": depth_at(kps[i]), "kp_m": kps[i], "bottom_kp_m": bottom_cumulative,
                    "cable_kp_m": assembly["cable_at"](kps[i]), "bearing_deg": bearings[i] if i < len(bearings) else None,
                    "turn_deg": turn, "cable_type_id": leg["cable_type_id"],
                    "surface_slack_pct": leg["surface_slack_pct"] if i < len(legs) else None,
                    "bottom_slack_pct": leg["bottom_slack_pct"] if i < len(legs) else None, "note": point.get("note", "")})
    events = _list(project.get("events", route.get("events", [])), "events", 10000)
    event_hours, event_cost = 0.0, 0.0
    for event in events:
        event = _object(event, "event")
        _number(event, "kp_m", minimum=0, maximum=kps[-1] + 1e-7)
        event_hours += _number(event, "stop_hours", minimum=0)
        event_cost += _number(event, "extra_cost", minimum=0)
        if event.get("currency", currency) != currency:
            raise ValueError("作业事件币种与项目币种不同")
    body_cost = sum(b["cost"] for b in assembly["bodies"])
    for body in assembly["bodies"]:
        if body.get("currency", currency) != currency:
            raise ValueError("附属体币种与项目币种不同")
    material_cost = sum(assembly["type_lengths"][tid] * typ["cost_per_m"] for tid, typ in types.items())
    time_hours = sum(l["time_hours"] for l in legs) + event_hours
    vessel_cost = time_hours / 24.0 * day_rate
    burial_cost = sum(l["burial_length_m"] for l in legs) * burial_rate
    extras_cost = sum(l["extra_cost"] for l in legs) + event_cost
    subtotal = material_cost + body_cost + vessel_cost + burial_cost + extras_cost
    contingency_cost = subtotal * contingency / 100.0
    bottom_total = sum(l["bottom_length_m"] for l in legs) if all(l["bottom_length_m"] is not None for l in legs) else None
    surface_total = kps[-1]
    total = assembly["total"]
    summary = {"surface_length_m": surface_total, "bottom_length_m": bottom_total, "cable_length_m": total,
               "material_length_m": assembly["material_length"], "body_length_m": assembly["body_length"], "allowance_length_m": assembly["allowance_length"],
               "material_cost": material_cost, "body_cost": body_cost, "vessel_cost": vessel_cost, "burial_cost": burial_cost,
               "extra_cost": extras_cost, "subtotal": subtotal, "contingency_cost": contingency_cost, "cost_total": subtotal + contingency_cost,
               "time_hours": time_hours, "currency": currency,
               "slack_pct": 100.0 * (total / surface_total - 1) if surface_total > 1e-7 else None,
               "bottom_slack_pct": 100.0 * (total / bottom_total - 1) if bottom_total is not None and bottom_total > 1e-7 else None}
    crossings = _crossings(project, points, kps, curve, corridor, warnings)
    geometry_coords = []
    for a, b in zip(points, points[1:]):
        dense = densify(a["longitude"], a["latitude"], b["longitude"], b["latitude"], curve)
        geometry_coords.extend(dense if not geometry_coords else dense[1:])
    result = {"summary": summary, "rpl": rpl, "legs": legs, "profile": profile, "warnings": warnings,
              "sld": assembly["sld"], "crossings": crossings, "bodies": assembly["bodies"], "profile_metadata": profile_meta,
              "assembly_references": assembly["assembly_references"],
              "route_signature": signature, "route_geometry": {"type": "LineString", "coordinates": geometry_coords},
              "route_geometry_segments": split_antimeridian(geometry_coords, curve),
              "materials": [{"cable_type_id": tid, "name": types[tid].get("name", tid), "length_m": length,
                             "cost_per_m": types[tid]["cost_per_m"], "cost": length * types[tid]["cost_per_m"]}
                            for tid, length in assembly["type_lengths"].items()],
              "model": {"geometry": "WGS84_ellipsoidal_rhumb" if curve == "rhumb" else "WGS84_geodesic_PROJ",
                        "profile": "piecewise_linear_kp_depth", "validation_status": "planning_prototype",
                        "assumptions": ["水深正向下；平面 KP、海底距离和实物装配里程分开计算。",
                                        "附属体 kp_m 为路线平面 KP，cable_kp_m 为可选实物缆里程；长度从定位点起算。",
                                        "replace 附属体占用等长缆材；additional 附属体和 allowance 增加实物总长。",
                                        "工期由路线平面段长除船速及停时估算，未考虑缆角、海流、船舶响应或水深对作业速度的影响。",
                                        "GIS 检查采用加密线路及区间局部 AEQD 投影，稀疏图层边界仅作初步筛查。",
                                        "尚未与原厂算法或实海数据进行精度对照。"]}}
    # Final invariant ensures every API/export consumer can use strict JSON.
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("计算结果超出有限数值范围，请检查参数量级") from exc
    return result


def sample_project() -> dict:
    """A realistic geographic workflow, with conspicuously synthetic bathymetry."""
    coordinates = [
        (118.12, 24.43, 18, "厦门演示登陆点"), (118.40, 24.22, 55, "近岸埋设结束"),
        (118.87, 23.92, 95, "海峡浅滩"), (119.10, 23.51, 130, "既有海缆穿越"),
        (118.91, 23.11, 210, "陆架坡折"), (118.62, 22.70, 560, "上陆坡"),
        (118.22, 22.26, 1250, "中陆坡"), (117.66, 21.77, 2200, "南海盆地"),
        (117.10, 21.31, 2850, "深海段"), (116.68, 20.92, 3100, "演示海端站")]
    points = [{"id": f"p{i+1}", "longitude": lon, "latitude": lat, "depth_m": depth, "label": label,
               "note": "演示坐标；水深为合成值，不可用于实际海工作业"} for i, (lon, lat, depth, label) in enumerate(coordinates)]
    ids = ["DA", "SA", "SA", "LWP", "LWP", "LW", "LW", "LW", "LW"]
    project = {"schema_version": 1, "id": "demo-taiwan-strait", "name": "台湾海峡—南海海缆规划演示", "crs": "EPSG:4326",
               "description": "公开地理坐标上的独立演示；全部地形、缆材价格和附件参数均为合成假设。",
               "route": {"id": "route-main", "name": "东南沿海至南海演示路由", "curve": "rhumb", "mode": "flexible",
                         "slack_basis": "surface", "slack_pct": 1.5, "points": points,
                         "legs": [{"cable_type_id": tid, "slack_pct": 2.0 if i < 2 else 1.5, "burial": i < 2,
                                   "fixed_cable_length_m": None} for i, tid in enumerate(ids)]},
               "cable_types": [
                   {"id": "DA", "name": "双铠装近岸缆", "diameter_m": .044, "wet_weight_n_m": 28, "cost_per_m": 210, "lay_speed_m_s": .8, "ea_n": 1.5e8, "ei_n_m2": 100, "max_tension_n": 100000, "min_bend_radius_m": 1.5},
                   {"id": "SA", "name": "单铠装浅海缆", "diameter_m": .034, "wet_weight_n_m": 16, "cost_per_m": 130, "lay_speed_m_s": 1.2, "ea_n": 1.2e8, "ei_n_m2": 60, "max_tension_n": 80000, "min_bend_radius_m": 1.2},
                   {"id": "LWP", "name": "轻型保护缆", "diameter_m": .024, "wet_weight_n_m": 8, "cost_per_m": 75, "lay_speed_m_s": 1.5, "ea_n": 1e8, "ei_n_m2": 30, "max_tension_n": 60000, "min_bend_radius_m": 1.0},
                   {"id": "LW", "name": "深海轻型缆", "diameter_m": .019, "wet_weight_n_m": 4.2, "cost_per_m": 48, "lay_speed_m_s": 1.8, "ea_n": 8e7, "ei_n_m2": 12, "max_tension_n": 45000, "min_bend_radius_m": .8}],
               "bodies": [], "costs": {"currency": "CNY", "vessel_day_rate": 250000, "burial_per_m": 42, "contingency_pct": 10},
               "rules": {"max_slope_deg": 12, "min_slack_pct": .5, "max_slack_pct": 4, "corridor_m": 500},
               "events": [{"id": "landing", "name": "登陆作业", "kp_m": 0, "stop_hours": 18, "extra_cost": 150000}],
               "layers": [
                   {"id": "existing-1", "name": "既有海缆（演示）", "kind": "cable", "visible": True,
                    "geojson": {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "cable-a", "properties": {"name": "现有海缆 A（合成）"},
                                 "geometry": {"type": "LineString", "coordinates": [[118.75, 23.48], [119.40, 23.54]]}}]}},
                   {"id": "restricted-1", "name": "海底施工限制区（演示）", "kind": "restricted", "visible": True,
                    "geojson": {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "area-a", "properties": {"name": "演示限制区"},
                                 "geometry": {"type": "Polygon", "coordinates": [[[118.66, 22.62], [118.84, 22.62], [118.84, 22.82], [118.66, 22.82], [118.66, 22.62]]]}}]}}]}
    kps = [0.0]
    for a, b in zip(points, points[1:]):
        kps.append(kps[-1] + inverse(a["longitude"], a["latitude"], b["longitude"], b["latitude"])[0])
    samples = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        n = max(3, int(math.ceil((kps[i + 1] - kps[i]) / 2000)))
        for j in range(n):
            f = j / n
            depth = a["depth_m"] + (b["depth_m"] - a["depth_m"]) * f
            # Small interior ridges, zero at waypoint boundaries.
            depth += (8 + i * 5) * math.sin(math.pi * f) * math.sin(4 * math.pi * f)
            samples.append({"kp_m": kps[i] + f * (kps[i + 1] - kps[i]), "depth_m": max(0, depth)})
    samples.append({"kp_m": kps[-1], "depth_m": points[-1]["depth_m"]})
    project["profile"] = {"samples": samples, "route_signature": route_signature(project), "source": "synthetic_demo",
                          "vertical_datum": "synthetic, unspecified", "resolution_m": 2000}
    project["bodies"] = [{"id": f"rep-{i}", "name": f"演示中继器 {i}", "kind": "repeater", "kp_m": kps[-1] * f,
                          "cost": 350000, "length_m": 2.8, "length_mode": "replace"} for i, f in enumerate((.23, .41, .59, .77), 1)]
    project["bodies"].append({"id": "joint-1", "name": "铠装转换接头", "kind": "joint", "kp_m": kps[3], "cost": 32000, "length_m": .8})
    return project
