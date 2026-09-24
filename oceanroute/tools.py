"""Planning calculators and structure-preserving route transformations.

Inputs/outputs use the same metre, degree and percent convention as core.py.
Mutation tools return {project, report, warnings}; split returns {projects,...}.
"""
from __future__ import annotations

import bisect
from copy import deepcopy
import math
from uuid import uuid4

from .core import analyze_project, route_signature
from .geodesy import GEOD, WGS84_E, finite_number, interpolate, inverse, isometric_latitude, wrap_longitude

EPS = 1e-6


def _warning(code, message, **extra):
    return {"code": code, "message": message, "severity": "warning", **extra}


def _config(config):
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("config 必须是对象")
    return config


def _clean_project(project):
    if project.get("route", {}).get("constraint_state") is not None:
        raise ValueError("CONSTRAINT_TRANSFORM_UNSUPPORTED: 此结构变换尚未映射 Path Link 域；请明确重新配置约束后操作，不能静默丢弃制造域")
    result = deepcopy(project)
    return result


def _profile_provenance(profile):
    """Copy exact sampled provenance; inserted stations are explicitly derived."""
    profile = profile or {}
    rows = profile.get("samples", [])
    keys = [row["kp_m"] for row in rows]
    bound = (profile.get("metadata") or {}).get("model") in {"priority-terrain-library-v1", "terrain-library-derived-profile-v1"}
    fields = ("source_id", "source_fingerprint", "fallback", "fallback_count", "provenance_interpolated")
    def sample(kp, depth, output_kp):
        row = {"kp_m": output_kp, "depth_m": depth}
        index = bisect.bisect_left(keys, kp)
        matching = next((i for i in (index, index-1) if 0 <= i < len(keys) and abs(keys[i]-kp) <= EPS), None)
        if matching is not None:
            row.update({key: deepcopy(rows[matching][key]) for key in fields if key in rows[matching]})
        elif bound:
            row.update(source_id=None, source_fingerprint=None, fallback=None, fallback_count=None,
                       provenance_interpolated=True)
        return row
    return sample


def _effective_leg(project, analysis, index):
    route = project["route"]
    options = route.get("legs", [])
    leg = deepcopy(options[index]) if index < len(options) else {}
    calculated = analysis["legs"][index]
    for key in ("mode", "slack_basis", "cable_type_id"):
        leg[key] = calculated[key]
    leg["slack_pct"] = leg.get("slack_pct", route.get("slack_pct", 0))
    if leg["mode"] == "fixed":
        leg["fixed_cable_length_m"] = calculated["cable_length_m"]
    return leg


def _canonical_allowances(project, analysis, include_leg=False):
    """Record the effective cable type before a structural route operation."""
    values = deepcopy(project["route"].get("allowances", project.get("allowances", [])))
    keys = [p["kp_m"] for p in analysis["rpl"]]
    for allowance in values:
        # Match core assembly defaults and its inclusive endpoint tolerance.
        # Structural operations must transform the validated effective station,
        # not leave a legal optional input as a missing key or move its excess.
        allowance["kp_m"] = min(keys[-1], finite_number(allowance.get("kp_m", 0), "kp_m", minimum=0, maximum=keys[-1] + 1e-7))
        allowance["length_m"] = finite_number(allowance.get("length_m", 0), "length_m", minimum=0)
        if allowance.get("cable_type_id") is None:
            i = max(0, min(len(analysis["legs"]) - 1, bisect.bisect_right(keys, allowance["kp_m"] + 1e-7) - 1))
            allowance["cable_type_id"] = analysis["legs"][i]["cable_type_id"]
    if include_leg:
        for i in range(len(analysis["legs"])):
            opt = _effective_leg(project, analysis, i)
            if opt.get("allowance_m", 0):
                values.append({"kp_m": analysis["legs"][i]["end_kp_m"], "length_m": opt["allowance_m"],
                               "cable_type_id": opt.get("allowance_cable_type_id", opt["cable_type_id"]), "name": "保留的段尾附加缆长"})
    return values


def _depth_at(profile, kp):
    if not profile:
        return None
    keys = [p["kp_m"] for p in profile]
    idx = bisect.bisect_left(keys, kp)
    if idx < len(profile) and abs(keys[idx] - kp) < EPS:
        return profile[idx]["depth_m"]
    if idx == len(profile) and abs(keys[-1] - kp) < EPS:
        return profile[-1]["depth_m"]
    if idx == 0 or idx == len(profile):
        return None
    a, b = profile[idx - 1], profile[idx]
    if a["depth_m"] is None or b["depth_m"] is None:
        return None
    return a["depth_m"] + (kp - a["kp_m"]) / (b["kp_m"] - a["kp_m"]) * (b["depth_m"] - a["depth_m"])


def _retain_signature(project, result):
    """Only operations proven to keep the same geographic curve call this."""
    if project.get("profile", {}).get("route_signature") == route_signature(project):
        result["profile"]["route_signature"] = route_signature(result)


def _sum_invariants(analyses):
    keys = ("surface_length_m", "cable_length_m", "material_length_m", "body_length_m", "material_cost", "body_cost", "time_hours", "cost_total")
    return {key: sum(a["summary"].get(key, 0) for a in analyses) for key in keys}


def _comparison(before, after):
    return {key: {"before": value, "after": after["summary"][key], "delta": after["summary"][key] - value}
            for key, value in before.items()}


def geodetic(config: dict) -> dict:
    """Inverse or direct WGS84 calculator, with real along-curve interpolation.

    Inverse config: {from:{longitude,latitude},to:{longitude,latitude},curve}.
    Direct config: {from:{...},bearing_deg,distance_m,curve}.
    Flat longitude1/latitude1/longitude2/latitude2 aliases are also accepted.
    """
    config = _config(config)
    origin = config.get("from", {"longitude": config.get("longitude1"), "latitude": config.get("latitude1")})
    if not isinstance(origin, dict):
        raise ValueError("from 必须含 longitude 与 latitude")
    from .geodesy import coordinate
    lon1, lat1 = coordinate(origin.get("longitude"), origin.get("latitude"))
    curve = config.get("curve", "rhumb")
    if curve not in ("rhumb", "geodesic"):
        raise ValueError("curve 必须为 rhumb 或 geodesic")
    destination = config.get("to")
    if destination is None and config.get("longitude2") is not None:
        destination = {"longitude": config.get("longitude2"), "latitude": config.get("latitude2")}
    if destination is not None:
        if not isinstance(destination, dict):
            raise ValueError("to 必须含 longitude 与 latitude")
        lon2, lat2 = coordinate(destination.get("longitude"), destination.get("latitude"))
        distance, bearing = inverse(lon1, lat1, lon2, lat2, curve)
        operation = "inverse"
    else:
        distance = finite_number(config.get("distance_m"), "distance_m", minimum=0, maximum=40_000_000)
        bearing = finite_number(config.get("bearing_deg"), "bearing_deg") % 360
        if curve == "geodesic":
            lon2, lat2, _ = GEOD.fwd(lon1, lat1, bearing, distance)
        elif distance == 0:
            lon2, lat2 = lon1, lat1
        else:
            if abs(lat1) == 90:
                raise ValueError("恒向线直接计算不能从极点开始")
            angle = math.radians(bearing)
            meridian_distance = distance * math.cos(angle)
            _, _, north_limit = GEOD.inv(lon1, lat1, lon1, 90 if meridian_distance >= 0 else -90)
            if abs(meridian_distance) >= north_limit - 1e-7:
                raise ValueError("恒向线越过或到达极点，恒定航向终点不唯一")
            if abs(math.cos(angle)) < 1e-10:
                from .geodesy import WGS84_A, WGS84_E2
                phi = math.radians(lat1)
                radius = WGS84_A * math.cos(phi) / math.sqrt(1 - WGS84_E2 * math.sin(phi) ** 2)
                lon2 = wrap_longitude(lon1 + math.degrees(distance * math.sin(angle) / radius))
                lat2 = lat1
            else:
                _, lat2, _ = GEOD.fwd(lon1, lat1, 0 if meridian_distance >= 0 else 180, abs(meridian_distance))
                dpsi = isometric_latitude(math.radians(lat2)) - isometric_latitude(math.radians(lat1))
                lon2 = wrap_longitude(lon1 + math.degrees(math.tan(angle) * dpsi))
        operation = "direct"
    count_value = finite_number(config.get("segments", 16), "segments", minimum=1, maximum=10000)
    if not count_value.is_integer():
        raise ValueError("segments 必须是整数")
    count = int(count_value)
    # Direct paths beyond 180° longitude cannot be represented by the inverse
    # shortest-arc interpolator. Expose this limitation instead of a false KP.
    actual, _ = inverse(lon1, lat1, lon2, lat2, curve)
    if operation == "direct" and abs(actual - distance) > max(EPS, distance * 1e-8):
        raise ValueError("该恒向航迹绕地超过最短经度弧；请将计算分成较短航段")
    reverse_heading = inverse(lon2, lat2, lon1, lat1, curve)[1]
    points = [{"longitude": lon, "latitude": lat, "kp_m": distance * i / count}
              for i in range(count + 1)
              for lon, lat in [interpolate(lon1, lat1, lon2, lat2, i / count, curve)]]
    return {"operation": operation, "curve": curve, "model": "WGS84_ellipsoidal_rhumb" if curve == "rhumb" else "WGS84_geodesic_PROJ",
            "from": {"longitude": lon1, "latitude": lat1}, "to": {"longitude": lon2, "latitude": lat2},
            "distance_m": distance, "bearing_deg": bearing, "reverse_bearing_deg": reverse_heading, "points": points,
            "units": {"distance": "m", "angles": "degrees"}}


def _insert_kps(project, cuts, analysis=None):
    """Partition original legs without changing their spatial curves or constraints."""
    analysis = analysis or analyze_project(project)
    result = _clean_project(project)
    original_points = project["route"]["points"]
    curve = project["route"].get("curve", "rhumb")
    total = analysis["summary"]["surface_length_m"]
    cuts = sorted(set(finite_number(k, "cut_kp_m", minimum=0, maximum=total + EPS) for k in cuts))
    points, legs = [deepcopy(original_points[0])], []
    ids = {str(p.get("id", f"p{i+1}")) for i, p in enumerate(original_points)}
    inserted = []
    from .route_geometry import route_segments
    actual_segments = route_segments(project)
    for i, calculated in enumerate(analysis["legs"]):
        start, end = calculated["start_kp_m"], calculated["end_kp_m"]
        interior = [kp for kp in cuts if start + EPS < kp < end - EPS]
        partition = [start] + interior + [end]
        base_options = _effective_leg(project, analysis, i)
        assigned_fixed = 0.0
        for j, (left, right) in enumerate(zip(partition, partition[1:])):
            leg = deepcopy(base_options)
            child = actual_segments[i].subsegment(max(0., left-start), min(actual_segments[i].length_m, right-start))
            if child.geometry is None:
                leg.pop("geometry", None)
            else:
                leg["geometry"] = child.geometry
            for prop in ("allowance_m", "stop_hours", "extra_cost"):
                if j != len(partition) - 2:
                    leg[prop] = 0
            if leg["mode"] == "fixed":
                original_fixed = calculated["cable_length_m"]
                if j == len(partition) - 2:
                    leg["fixed_cable_length_m"] = original_fixed - assigned_fixed
                else:
                    leg["fixed_cable_length_m"] = original_fixed * (right - left) / (end - start)
                    assigned_fixed += leg["fixed_cable_length_m"]
            legs.append(leg)
            if j == len(partition) - 2:
                point = deepcopy(original_points[i + 1])
            else:
                a, b = original_points[i], original_points[i + 1]
                lon, lat = actual_segments[i].point_at_fraction((right-start)/(end-start))
                index = len(inserted) + 1
                point_id = f"insert-{index}"
                while point_id in ids:
                    index += 1
                    point_id = f"insert-{index}"
                ids.add(point_id)
                point = {"id": point_id, "longitude": lon, "latitude": lat, "depth_m": _depth_at(analysis["profile"], right),
                         "label": f"插值 KP {right / 1000:.3f} km", "note": "沿原线路曲线插值；水深来源见剖面说明"}
                inserted.append({"point_id": point_id, "kp_m": right})
            points.append(point)
    result["route"]["points"], result["route"]["legs"] = points, legs
    _retain_signature(project, result)
    return result, inserted


def subdivide_project(project: dict, config: dict | None = None) -> dict:
    config = _config(config)
    spacing = finite_number(config.get("spacing_m", 10000), "spacing_m", minimum=1, maximum=5_000_000)
    mode = config.get("mode", "same_curve")
    before = analyze_project(project)
    warnings = []
    if mode == "same_curve":
        cuts = []
        for leg in before["legs"]:
            count = max(1, math.ceil((leg["surface_length_m"] - EPS) / spacing))
            if len(cuts) + count - 1 + len(project["route"]["points"]) > 10000:
                raise ValueError("细分后超过 10,000 个路线点，请增加间距")
            cuts.extend(leg["start_kp_m"] + leg["surface_length_m"] * j / count for j in range(1, count))
        if len(cuts) + len(project["route"]["points"]) > 10000:
            raise ValueError("细分后超过 10,000 个路线点，请增加间距")
        result, inserted = _insert_kps(project, cuts, before)
    elif mode == "geodesic_as_rhumb":
        if any(l.get("geometry") is not None for l in project["route"].get("legs", [])):
            raise ValueError("圆弧不能通过更改全局 curve 转换；请使用显式圆弧转恒向线工具")
        # New shape: exact geodesic vertices connected by short rhumb segments.
        source = _clean_project(project)
        source["route"]["curve"] = "geodesic"
        # A profile from a rhumb path cannot certify the different geodesic path.
        if source.get("profile", {}).get("route_signature") == route_signature(project) and project["route"].get("curve", "rhumb") != "geodesic":
            source["profile"]["route_signature"] = "requires_resampling_geometry_changed"
        # Bottom slack needs known terrain and cannot be reinterpreted. Preserve
        # existing physical lengths as fixed constraints until fresh sampling.
        for i, leg in enumerate(before["legs"]):
            opt = _effective_leg(project, before, i)
            opt["mode"], opt["fixed_cable_length_m"] = "fixed", leg["cable_length_m"]
            if i < len(source["route"].get("legs", [])):
                source["route"]["legs"][i] = opt
            else:
                source["route"].setdefault("legs", []).append(opt)
        geodesic_analysis = analyze_project(source)
        cuts = []
        for leg in geodesic_analysis["legs"]:
            count = max(1, math.ceil((leg["surface_length_m"] - EPS) / spacing))
            if len(cuts) + count - 1 + len(project["route"]["points"]) > 10000:
                raise ValueError("细分后超过 10,000 个路线点，请增加间距")
            cuts.extend(leg["start_kp_m"] + leg["surface_length_m"] * j / count for j in range(1, count))
        if len(cuts) + len(project["route"]["points"]) > 10000:
            raise ValueError("细分后超过 10,000 个路线点，请增加间距")
        result, inserted = _insert_kps(source, cuts, geodesic_analysis)
        result["route"]["curve"] = "rhumb"
        if result.get("profile", {}).get("samples"):
            result["profile"]["route_signature"] = "requires_resampling_geometry_changed"
        # Preserve assembly physical coordinates when spatial path length changes.
        for body, calculated in zip(result.get("bodies", []), before["bodies"]):
            if body.get("length_mode", "replace") == "replace":
                body["cable_kp_m"] = calculated["cable_kp_m"]
            else:
                old_kp = calculated["kp_m"]
                _remap_spatial_kp(body, before, geodesic_analysis, old_kp)
        for event in result.get("route", {}).get("allowances", result.get("allowances", [])):
            _remap_spatial_kp(event, before, geodesic_analysis, event["kp_m"])
        for event in result.get("events", result.get("route", {}).get("events", [])):
            _remap_spatial_kp(event, before, geodesic_analysis, event["kp_m"])
        warnings.append(_warning("GEODESIC_RHUMB_APPROXIMATION", "路线改为测地线采样点间的短恒向线近似；原缆长转成固定约束，地形须重新采样"))
    else:
        raise ValueError("细分 mode 必须为 same_curve 或 geodesic_as_rhumb")
    after = analyze_project(result)
    return {"project": result, "warnings": warnings, "report": {"operation": "subdivide", "mode": mode,
            "inserted_points": inserted, "point_count": len(result["route"]["points"]), "invariants": _comparison(_sum_invariants([before]), after)}}


def _remap_spatial_kp(item, old_analysis, new_analysis, old_kp):
    old_points = [p["kp_m"] for p in old_analysis["rpl"]]
    i = max(0, min(len(old_points) - 2, bisect.bisect_right(old_points, old_kp) - 1))
    width = old_points[i + 1] - old_points[i]
    fraction = (old_kp - old_points[i]) / width if width > 0 else 0
    new_leg = new_analysis["legs"][i]
    item["kp_m"] = new_leg["start_kp_m"] + fraction * new_leg["surface_length_m"]


def _bands(rules, project):
    config = rules if isinstance(rules, dict) else {"bands": rules}
    if not isinstance(config, dict) or not isinstance(config.get("bands"), list) or not config["bands"]:
        raise ValueError("深度分缆规则须为非空 bands 数组")
    known = {str(t["id"]) for t in project.get("cable_types", [])}
    bands = []
    for raw in config["bands"]:
        if not isinstance(raw, dict):
            raise ValueError("每条分缆规则必须是对象")
        minimum = finite_number(raw.get("min_depth_m", 0), "min_depth_m", minimum=0)
        maximum = math.inf if raw.get("max_depth_m") is None else finite_number(raw["max_depth_m"], "max_depth_m", minimum=0)
        if maximum <= minimum:
            raise ValueError("max_depth_m 必须大于 min_depth_m")
        type_id = str(raw.get("cable_type_id", ""))
        if type_id not in known:
            raise ValueError(f"未知分缆型号 {type_id}")
        band = {**deepcopy(raw), "min_depth_m": minimum, "max_depth_m": maximum, "cable_type_id": type_id}
        if "slack_pct" in band:
            finite_number(band["slack_pct"], "slack_pct", minimum=-99.999999)
        if band.get("slack_basis", "surface") not in ("surface", "bottom"):
            raise ValueError("slack_basis 必须为 surface 或 bottom")
        bands.append(band)
    bands.sort(key=lambda b: b["min_depth_m"])
    for a, b in zip(bands, bands[1:]):
        if abs(a["max_depth_m"] - b["min_depth_m"]) > EPS:
            raise ValueError("深度分缆区间必须连续且不重叠")
    if bands[0]["min_depth_m"] != 0 or not math.isinf(bands[-1]["max_depth_m"]):
        raise ValueError("深度规则须从 0 m 连续覆盖到 max_depth_m:null")
    return config, bands


def define_cables_by_depth(project: dict, rules) -> dict:
    before = analyze_project(project)
    if not before["profile_metadata"]["imported_profile_valid"]:
        raise ValueError("自动按深度分缆需要与当前路线签名绑定的有效剖面")
    config, bands = _bands(rules, project)
    total = before["summary"]["surface_length_m"]
    start = finite_number(config.get("start_kp_m", 0), "start_kp_m", minimum=0, maximum=total)
    end = finite_number(config.get("end_kp_m", total), "end_kp_m", minimum=start, maximum=total + EPS)
    if end - start < EPS:
        raise ValueError("分缆范围必须具有正长度")
    profile = before["profile"]
    thresholds = [b["min_depth_m"] for b in bands[1:]]
    cuts = [start, end]
    for a, b in zip(profile, profile[1:]):
        left, right = max(start, a["kp_m"]), min(end, b["kp_m"])
        if right <= left + EPS:
            continue
        if a["depth_m"] is None or b["depth_m"] is None:
            raise ValueError("分缆范围含缺测，不能推断深度转换点")
        difference = b["depth_m"] - a["depth_m"]
        if abs(difference) > 1e-12:
            for threshold in thresholds:
                fraction = (threshold - a["depth_m"]) / difference
                kp = a["kp_m"] + fraction * (b["kp_m"] - a["kp_m"])
                if -EPS <= fraction <= 1 + EPS and left + EPS < kp < right - EPS:
                    cuts.append(kp)
        # A threshold crossing exactly on an existing terrain sample must also
        # become a route link; midpoint selection alone could miss oscillations.
        for endpoint in (a, b):
            if start + EPS < endpoint["kp_m"] < end - EPS and any(abs(endpoint["depth_m"] - t) < EPS for t in thresholds):
                cuts.append(endpoint["kp_m"])
    result, inserted = _insert_kps(project, cuts, before)
    interim = analyze_project(result)
    warnings, transitions = [], []
    keep_fixed = not bool(config.get("convert_fixed", False))
    last_type = None
    for i, leg in enumerate(interim["legs"]):
        middle = (leg["start_kp_m"] + leg["end_kp_m"]) / 2
        if not start - EPS <= middle <= end + EPS or leg["surface_length_m"] <= EPS:
            continue
        depth = _depth_at(profile, middle)
        if depth is None:
            raise ValueError("分缆范围存在缺测")
        band = next(b for b in bands if b["min_depth_m"] <= depth < b["max_depth_m"])
        opt = result["route"]["legs"][i]
        opt["cable_type_id"] = band["cable_type_id"]
        if "slack_pct" in band:
            opt["slack_pct"] = band["slack_pct"]
        if "slack_basis" in band:
            opt["slack_basis"] = band["slack_basis"]
        if opt.get("mode") == "fixed":
            if keep_fixed:
                if "slack_pct" in band:
                    warnings.append(_warning("FIXED_LENGTH_PRESERVED", f"第 {i + 1} 段保留固定实物缆长，模板余缆仅记录为目标值", leg_index=i))
            else:
                opt["mode"] = "flexible"
                opt["fixed_cable_length_m"] = None
        if last_type is not None and last_type != band["cable_type_id"]:
            transitions.append({"point_id": result["route"]["points"][i]["id"], "kp_m": leg["start_kp_m"],
                                "depth_m": _depth_at(profile, leg["start_kp_m"]), "from_type": last_type, "to_type": band["cable_type_id"]})
        last_type = band["cable_type_id"]
    after = analyze_project(result)
    return {"project": result, "warnings": warnings, "report": {"operation": "define_cables_by_depth", "inserted_points": inserted,
            "transitions": transitions, "start_kp_m": start, "end_kp_m": end, "fixed_lengths_preserved": keep_fixed,
            "invariants": _comparison(_sum_invariants([before]), after)}}


def apply_slack_template(project: dict, template) -> dict:
    before = analyze_project(project)
    config = template if isinstance(template, dict) else {"entries": template}
    if not isinstance(config, dict):
        raise ValueError("余缆模板必须为对象或数组")
    raw_entries = config.get("entries", config.get("by_cable_type", config.get("mapping", config)))
    if isinstance(raw_entries, list):
        entries = {str(e.get("cable_type_id")): e for e in raw_entries if isinstance(e, dict)}
        if len(entries) != len(raw_entries):
            raise ValueError("余缆模板 entries 须为不同缆型的对象")
    elif isinstance(raw_entries, dict):
        entries = {str(key): value if isinstance(value, dict) else {"slack_pct": value} for key, value in raw_entries.items() if key not in ("fixed_policy", "start_kp_m", "end_kp_m")}
    else:
        raise ValueError("余缆模板 entries 必须为数组或缆型映射")
    policy = config.get("fixed_policy", "keep")
    if policy not in ("keep", "convert", "reject"):
        raise ValueError("fixed_policy 必须为 keep、convert 或 reject")
    known = {str(t["id"]) for t in project.get("cable_types", [])}
    for key, values in entries.items():
        if key not in known:
            raise ValueError(f"余缆模板引用未知缆型 {key}")
        finite_number(values.get("slack_pct"), "slack_pct", minimum=-99.999999)
        if values.get("slack_basis", "surface") not in ("surface", "bottom"):
            raise ValueError("slack_basis 必须为 surface 或 bottom")
    total = before["summary"]["surface_length_m"]
    start = finite_number(config.get("start_kp_m", 0), "start_kp_m", minimum=0, maximum=total)
    end = finite_number(config.get("end_kp_m", total), "end_kp_m", minimum=start, maximum=total + EPS)
    result, inserted = _insert_kps(project, [start, end], before)
    interim = analyze_project(result)
    changed, warnings = [], []
    for i, leg in enumerate(interim["legs"]):
        if leg["end_kp_m"] <= start + EPS or leg["start_kp_m"] >= end - EPS or leg["cable_type_id"] not in entries:
            continue
        values = entries[leg["cable_type_id"]]
        opt = result["route"]["legs"][i]
        if opt["mode"] == "fixed":
            if policy == "reject":
                raise ValueError("模板范围含固定缆长区间，请显式选择保留或转换")
            if policy == "convert":
                opt["mode"], opt["fixed_cable_length_m"] = "flexible", None
            else:
                warnings.append(_warning("FIXED_LENGTH_PRESERVED", f"第 {i + 1} 段固定缆长保留，目标余缆不改变制造缆量", leg_index=i))
        opt["slack_pct"] = values["slack_pct"]
        if "slack_basis" in values:
            opt["slack_basis"] = values["slack_basis"]
        changed.append(i)
    after = analyze_project(result)
    return {"project": result, "warnings": warnings, "report": {"operation": "apply_slack_template", "changed_legs": changed,
            "inserted_points": inserted, "fixed_policy": policy, "invariants": _comparison(_sum_invariants([before]), after)}}


def split_project(project: dict, config: dict) -> dict:
    config = _config(config)
    before = analyze_project(project)
    if "point_index" in config:
        value = finite_number(config["point_index"], "point_index", minimum=1, maximum=len(before["rpl"]) - 2)
        if not value.is_integer():
            raise ValueError("point_index 必须为整数")
        index = int(value)
        working = _clean_project(project)
    else:
        kp = finite_number(config.get("kp_m"), "kp_m", minimum=EPS, maximum=before["summary"]["surface_length_m"] - EPS)
        working, _ = _insert_kps(project, [kp], before)
        before = analyze_project(working)
        index = min(range(len(before["rpl"])), key=lambda i: abs(before["rpl"][i]["kp_m"] - kp))
    split_kp = before["rpl"][index]["kp_m"]
    split_cable = before["rpl"][index]["cable_kp_m"]
    for body in before["bodies"]:
        if body["start_m"] + EPS < split_cable < body["end_m"] - EPS:
            raise ValueError(f"分割点穿过附属体 {body['id']} 的实物长度，请换分割位置")
    projects = []
    for side in (0, 1):
        result = _clean_project(working)
        result["id"] = str(uuid4())
        result.pop("saved_revision", None)
        result["name"] = str(project.get("name", "海缆工程")) + (" · A" if side == 0 else " · B")
        result["route"]["id"] = str(uuid4())
        result["route"]["points"] = deepcopy(working["route"]["points"][:index + 1] if side == 0 else working["route"]["points"][index:])
        effective = [_effective_leg(working, before, i) for i in range(len(before["legs"]))]
        result["route"]["legs"] = deepcopy(effective[:index] if side == 0 else effective[index:])
        result["bodies"] = []
        result["assembly_references"] = []
        for reference in before.get("assembly_references", []):
            if (reference["cable_kp_m"] <= split_cable + EPS) == (side == 0):
                copied = deepcopy(reference)
                station = max(0.0, copied["cable_kp_m"] - (split_cable if side else 0))
                copied.update(cable_kp_m=station, start_m=station, end_m=station)
                copied.pop("kp_m", None)
                result["assembly_references"].append(copied)
        for original, body in zip(working.get("bodies", []), before["bodies"]):
            belongs_a = body["end_m"] <= split_cable + EPS and (body["start_m"] < split_cable - EPS or body.get("length_mode") == "additional")
            if belongs_a != (side == 0):
                continue
            copied = deepcopy(original)
            if copied.get("cable_kp_m") is not None:
                copied["cable_kp_m"] = body["cable_kp_m"] - (split_cable if side else 0)
                copied.pop("kp_m", None)
            else:
                copied["kp_m"] = max(0, body["kp_m"] - (split_kp if side else 0))
            result["bodies"].append(copied)
        result["route"]["allowances"] = []
        result.pop("allowances", None)
        for allowance in _canonical_allowances(working, before):
            belongs_a = allowance["kp_m"] <= split_kp + EPS
            if belongs_a == (side == 0):
                copied = deepcopy(allowance)
                copied["kp_m"] -= split_kp if side else 0
                result["route"]["allowances"].append(copied)
        result["events"] = []
        result["route"].pop("events", None)
        for event in working.get("events", working["route"].get("events", [])):
            if (event["kp_m"] <= split_kp + EPS) == (side == 0):
                copied = deepcopy(event)
                copied["kp_m"] -= split_kp if side else 0
                result["events"].append(copied)
        profile = working.get("profile", {})
        if profile.get("samples") and profile.get("route_signature") == route_signature(working):
            lo, hi = (0, split_kp) if side == 0 else (split_kp, before["summary"]["surface_length_m"])
            source_sample = _profile_provenance(profile)
            selected = [source_sample(p["kp_m"], p["depth_m"], p["kp_m"]-lo) for p in before["profile"] if lo - EPS <= p["kp_m"] <= hi + EPS]
            result["profile"]["samples"] = selected
            result["profile"]["route_signature"] = route_signature(result)
        projects.append(result)
    analyses = [analyze_project(p) for p in projects]
    totals = _sum_invariants(analyses)
    original = _sum_invariants([before])
    report = {"operation": "split", "boundary_events_owner": "A", "invariants": {
        key: {"before": original[key], "after": totals[key], "delta": totals[key] - original[key]} for key in original}}
    return {"projects": projects, "split_kp_m": split_kp, "split_cable_kp_m": split_cable, "warnings": [], "report": report}


def reverse_project(project: dict) -> dict:
    """Reverse physical spans including bodies' leading-edge convention."""
    before = analyze_project(project)
    result = _clean_project(project)
    total = before["summary"]["surface_length_m"]
    cable_total = before["summary"]["cable_length_m"]
    result["assembly_references"] = []
    for reference in reversed(before.get("assembly_references", [])):
        copied = deepcopy(reference)
        station = max(0.0, cable_total - copied["cable_kp_m"])
        copied.update(cable_kp_m=station, start_m=station, end_m=station)
        copied.pop("kp_m", None)
        result["assembly_references"].append(copied)

    def flip(kp):
        return min(total, max(0.0, total - kp))

    result["route"]["points"] = list(reversed(result["route"]["points"]))
    effective = [_effective_leg(project, before, i) for i in range(len(before["legs"]))]
    from .route_geometry import route_segments
    original_segments = route_segments(project)
    # A per-leg allowance is anchored to its old endpoint. Keeping it in the
    # reversed leg would anchor it to the opposite end, changing physical spans.
    allowances = _canonical_allowances(project, before, include_leg=True)
    for i, leg in enumerate(effective):
        if leg.get("allowance_m", 0):
            leg["allowance_m"] = 0
        if original_segments[i].geometry is not None:
            leg["geometry"] = original_segments[i].reversed().geometry
    result["route"]["legs"] = list(reversed(effective))
    result["route"]["allowances"] = [{**a, "kp_m": flip(a["kp_m"])} for a in reversed(allowances)]
    result.pop("allowances", None)
    result["events"] = [{**deepcopy(e), "kp_m": flip(e["kp_m"])} for e in reversed(project.get("events", project["route"].get("events", [])))]
    result["route"].pop("events", None)
    warnings = []
    result["bodies"] = []
    for original, calculated in reversed(list(zip(project.get("bodies", []), before["bodies"]))):
        body = deepcopy(original)
        if body.get("length_mode", "replace") == "additional":
            mixed = any(abs(a["kp_m"] - calculated["kp_m"]) <= EPS and a["length_m"] > 0 for a in allowances)
            if mixed:
                # Keep exactly the reversed physical body span by representing
                # its added space as an allowance, then replacing that space.
                # Provenance makes this deliberate schema conversion visible.
                body["length_mode"] = "replace"
                body["original_length_mode"] = "additional"
                body["cable_kp_m"] = max(0, cable_total - calculated["end_m"])
                body.pop("kp_m", None)
                result["route"]["allowances"].append({"kp_m": flip(calculated["kp_m"]), "length_m": calculated["length_m"],
                                                       "name": "反向附属体长度占位", "reserved_for_body_id": body["id"]})
                warnings.append(_warning("REVERSE_ASSEMBLY_REPRESENTATION", "同一 KP 的附属体和附加缆长反向时，将附属体额外长度转成占位 allowance + replace，以保留实物顺序", body_id=body["id"]))
            else:
                body["kp_m"] = flip(calculated["kp_m"])
                body.pop("cable_kp_m", None)
        else:
            body["cable_kp_m"] = max(0, cable_total - calculated["end_m"])
            body.pop("kp_m", None)
        result["bodies"].append(body)
    if project.get("profile", {}).get("samples") and project["profile"].get("route_signature") == route_signature(project):
        source_sample = _profile_provenance(project["profile"])
        result["profile"]["samples"] = [source_sample(p["kp_m"], p["depth_m"], flip(p["kp_m"])) for p in reversed(before["profile"])]
        result["profile"]["route_signature"] = route_signature(result)
    after = analyze_project(result)
    return {"project": result, "warnings": warnings, "report": {"operation": "reverse", "invariants": _comparison(_sum_invariants([before]), after)}}


def merge_projects(projects: list[dict], config: dict | None = None) -> dict:
    config = _config(config)
    if not isinstance(projects, list) or len(projects) < 2 or len(projects) > 100:
        raise ValueError("合并须提供 2 至 100 个工程")
    analyses = [analyze_project(p) for p in projects]
    from .terrain_sources import terrain_library_signature, normalize_sources
    terrain_signatures = [terrain_library_signature(p.get("terrain_sources", [])) for p in projects]
    if len(set(terrain_signatures)) > 1:
        raise ValueError("MERGE_TERRAIN_LIBRARY_CONFLICT: 合并路径须使用同一明确地形源库；请先统一共享源并重新采样，不能丢弃或混合优先级")
    terrain_bound = any(((p.get("profile") or {}).get("metadata") or {}).get("model") in
                        {"priority-terrain-library-v1", "terrain-library-derived-profile-v1"} for p in projects)
    if any(p.get("route", {}).get("constraint_state") is not None for p in projects):
        raise ValueError("CONSTRAINT_TRANSFORM_UNSUPPORTED: 合并尚未映射 Path Link 域；请明确重新配置约束后操作")
    curve = projects[0]["route"].get("curve", "rhumb")
    if any(p["route"].get("curve", "rhumb") != curve for p in projects):
        raise ValueError("合并工程的路线曲线模型须相同；请先显式转换")
    tolerance = finite_number(config.get("join_tolerance_m", .01), "join_tolerance_m", minimum=0, maximum=1000)
    cost_policy = config.get("cost_policy", "require_equal")
    if cost_policy not in ("require_equal", "first"):
        raise ValueError("cost_policy 必须为 require_equal 或 first")
    cost_keys = {"currency": "CNY", "vessel_day_rate": 0, "burial_per_m": 0, "contingency_pct": 0}
    first_costs = {key: projects[0].get("costs", {}).get(key, default) for key, default in cost_keys.items()}
    warnings = []
    for p in projects[1:]:
        costs = {key: p.get("costs", {}).get(key, default) for key, default in cost_keys.items()}
        if costs["currency"] != first_costs["currency"]:
            raise ValueError("合并工程币种不同，需要先换算并记录汇率")
        if costs != first_costs:
            if cost_policy == "require_equal":
                raise ValueError("合并工程船费、埋设费或预备费参数不同，请显式选择 cost_policy:first")
            warnings.append(_warning("MERGE_COST_REBASED", "合并工程使用首个工程的费率重新计算"))
    result = _clean_project(projects[0])
    result["terrain_sources"] = normalize_sources(projects[0].get("terrain_sources", []))
    result["id"], result["name"] = str(uuid4()), config.get("name", " + ".join(str(p.get("name", f"工程 {i + 1}")) for i, p in enumerate(projects)))
    result.pop("saved_revision", None)
    result["route"]["id"] = str(uuid4())
    result["route"]["points"], result["route"]["legs"], result["route"]["allowances"] = [], [], []
    result["route"].pop("events", None)
    result.pop("allowances", None)
    result["bodies"], result["events"], result["layers"], result["cable_types"] = [], [], [], []
    result["assembly_references"] = []
    type_defs, point_ids, body_ids, layer_ids = {}, set(), set(), set()
    reference_ids = set()
    surface_offset, cable_offset, profile_samples, sources, bridges = 0.0, 0.0, [], [], []
    for source_index, (project, analysis) in enumerate(zip(projects, analyses)):
        mapping = {}
        for cable in project.get("cable_types", []):
            old = str(cable["id"])
            key = old
            if key in type_defs and type_defs[key] != cable:
                key = f"source{source_index + 1}-{old}"
                while key in type_defs:
                    key += "_"
            mapping[old] = key
            if key not in type_defs:
                copy = deepcopy(cable)
                copy["id"] = key
                type_defs[key] = copy
                result["cable_types"].append(copy)
        points = deepcopy(project["route"]["points"])
        if source_index:
            end = result["route"]["points"][-1]
            start = points[0]
            gap, heading = inverse(end["longitude"], end["latitude"], start["longitude"], start["latitude"], curve)
            if gap > EPS:
                # A tolerance may classify a small connector as acceptable, but
                # we retain that real physical length rather than moving a point.
                if gap > tolerance and not config.get("connect_gaps", False):
                    raise ValueError(f"第 {source_index + 1} 个工程端点相距 {gap:.3f} m，请显式允许 connect_gaps")
                tid = str(config.get("bridge_cable_type_id", next(iter(type_defs))))
                if tid not in type_defs:
                    raise ValueError("bridge_cable_type_id 未定义")
                slack = finite_number(config.get("bridge_slack_pct", result["route"].get("slack_pct", 0)), "bridge_slack_pct", minimum=-99.999999)
                bridge_length = gap * (1 + slack / 100)
                result["route"]["legs"].append({"mode": "flexible", "slack_basis": "surface", "slack_pct": slack,
                                                "cable_type_id": tid, "burial": False, "fixed_cable_length_m": None})
                profile_samples.append({"kp_m": surface_offset + gap / 2, "depth_m": None})
                bridges.append({"source_index": source_index, "start_kp_m": surface_offset, "length_m": gap, "cable_length_m": bridge_length, "bearing_deg": heading})
                surface_offset += gap
                cable_offset += bridge_length
                warnings.append(_warning("MERGE_NEW_CONNECTOR", f"合并新增 {gap:.3f} m 连接区间；连接区间无测深，海底距离待补充"))
            else:
                points = points[1:]
        for i, point in enumerate(points):
            key = str(point.get("id", f"p{i + 1}"))
            if key in point_ids:
                key = f"source{source_index + 1}-{key}"
                while key in point_ids:
                    key += "_"
            point["id"] = key
            point_ids.add(key)
            result["route"]["points"].append(point)
        for i in range(len(analysis["legs"])):
            opt = _effective_leg(project, analysis, i)
            opt["cable_type_id"] = mapping.get(opt["cable_type_id"], opt["cable_type_id"])
            result["route"]["legs"].append(opt)
        for original, body in zip(project.get("bodies", []), analysis["bodies"]):
            copied = deepcopy(original)
            key = body["id"]
            if key in body_ids or key in reference_ids:
                key = f"source{source_index + 1}-{key}"
                while key in body_ids or key in reference_ids:
                    key += "_"
            copied["id"] = key
            body_ids.add(key)
            if copied.get("cable_kp_m") is not None:
                copied["cable_kp_m"] = body["cable_kp_m"] + cable_offset
                copied.pop("kp_m", None)
            else:
                copied["kp_m"] = body["kp_m"] + surface_offset
            result["bodies"].append(copied)
        for allowance in _canonical_allowances(project, analysis):
            copied = deepcopy(allowance)
            copied["kp_m"] += surface_offset
            copied["cable_type_id"] = mapping.get(copied["cable_type_id"], copied["cable_type_id"])
            result["route"]["allowances"].append(copied)
        for reference in analysis.get("assembly_references", []):
            copied = deepcopy(reference)
            key = reference["id"]
            if key in reference_ids or key in body_ids:
                key = f"source{source_index+1}-reference-{key}"
                while key in reference_ids or key in body_ids:
                    key += "_"
            reference_ids.add(key)
            station = copied["cable_kp_m"] + cable_offset
            copied.update(id=key, cable_kp_m=station, start_m=station, end_m=station)
            copied.pop("kp_m", None)
            result["assembly_references"].append(copied)
        for event in project.get("events", project["route"].get("events", [])):
            copied = deepcopy(event)
            copied["id"] = f"source{source_index + 1}-{copied.get('id', len(result['events']) + 1)}"
            copied["kp_m"] += surface_offset
            result["events"].append(copied)
        for layer in project.get("layers", []):
            # Identical source layers shared by split projects are not doubled.
            if any(existing == layer for existing in result["layers"]):
                continue
            copied = deepcopy(layer)
            key = str(copied.get("id", f"layer{len(layer_ids) + 1}"))
            if key in layer_ids:
                key = f"source{source_index + 1}-{key}"
            copied["id"] = key
            layer_ids.add(key)
            result["layers"].append(copied)
        source_sample = _profile_provenance(project.get("profile", {}))
        for sample in analysis["profile"]:
            copied = source_sample(sample["kp_m"], sample["depth_m"], sample["kp_m"]+surface_offset)
            if profile_samples and abs(profile_samples[-1]["kp_m"] - copied["kp_m"]) <= EPS:
                existing_depth = profile_samples[-1]["depth_m"]
                new_depth = copied["depth_m"]
                if existing_depth is None:
                    profile_samples[-1].update(copied)
                elif new_depth is not None and abs(existing_depth - new_depth) > EPS:
                    profile_samples[-1]["depth_m"] = None
                    if terrain_bound:
                        profile_samples[-1].update(source_id=None, source_fingerprint=None, fallback=None,
                                                   fallback_count=None, provenance_interpolated=True)
                    warnings.append(_warning("MERGE_DEPTH_CONFLICT", "相接端点水深冲突；该处标记缺测，相关海底距离待核对", kp_m=copied["kp_m"]))
            else:
                profile_samples.append(copied)
        sources.append({"project_id": project.get("id"), "surface_offset_m": surface_offset, "cable_offset_m": cable_offset,
                        "profile": deepcopy(analysis["profile_metadata"])})
        surface_offset += analysis["summary"]["surface_length_m"]
        cable_offset += analysis["summary"]["cable_length_m"]
    modes = {leg.get("mode", "flexible") for leg in result["route"]["legs"]}
    result["route"]["mode"] = modes.pop() if len(modes) == 1 else "flexible"
    source_name = "merged_profiles" if all(s["profile"]["imported_profile_valid"] for s in sources) else "merged_mixed_sources"
    if any(str(s["profile"].get("source", "")).lower().startswith("synthetic") for s in sources):
        source_name = "synthetic_" + source_name
    result["profile"] = {"samples": profile_samples, "route_signature": route_signature(result),
                         "source": source_name,
                         "measured": all(s["profile"].get("measured", False) for s in sources) and not bridges,
                         "metadata": {"sources": sources, "unmeasured_connectors": bridges}}
    if terrain_bound:
        result["profile"]["metadata"].update(model="terrain-library-derived-profile-v1",
                                             terrain_library_signature=terrain_signatures[0])
    if any(not s["profile"]["imported_profile_valid"] for s in sources):
        warnings.append(_warning("MERGE_PROFILE_PROVENANCE", "合并剖面含路线点近似或缺测，请查看各来源，未将其标记为实测"))
    after = analyze_project(result)
    return {"project": result, "warnings": warnings, "report": {"operation": "merge", "sources": sources, "new_connectors": bridges,
            "invariants": _comparison(_sum_invariants(analyses), after)}}
