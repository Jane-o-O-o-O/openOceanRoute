"""Independent, reviewable obstacle-aware cable-route candidate search.

This is an A* search on an eight-neighbour local metric grid, not a Makai native
interface or a claim of continuous/global engineering optimality. The published
manual describes obstacles, corridor checks and linked manual optimisation; it
does not disclose an automatic route-search algorithm.
"""
from __future__ import annotations

import bisect
from copy import deepcopy
import heapq
import itertools
import math
from uuid import uuid4

import numpy as np
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely.geometry import GeometryCollection, LineString, Point, box, mapping, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep

from .core import analyze_project, route_signature
from .geodesy import coordinate, densify, finite_number, interpolate, inverse, split_antimeridian
from .tools import _canonical_allowances, _effective_leg


EPS = 1e-6


class RoutingError(ValueError):
    """Expected search failure, with a stable code usable by API clients."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _warning(code, message, **extras):
    return {"code": code, "message": message, "severity": "warning", **extras}


def _index(value, name, maximum):
    number = finite_number(value, name, minimum=0, maximum=maximum)
    if not number.is_integer():
        raise ValueError(f"{name} 必须为整数")
    return int(number)


def _features(geojson):
    if not isinstance(geojson, dict):
        raise ValueError("区域／图层必须是 GeoJSON 对象")
    raw = geojson.get("features", []) if geojson.get("type") == "FeatureCollection" else [geojson]
    if not isinstance(raw, list) or len(raw) > 5000:
        raise ValueError("单个搜索图层最多 5,000 个 GeoJSON 要素")
    result = []
    for feature in raw:
        if not isinstance(feature, dict):
            raise ValueError("GeoJSON 要素必须是对象")
        data = feature.get("geometry") if feature.get("type") == "Feature" else feature
        if data is None:
            continue
        try:
            geom = shape(data)
        except Exception as exc:
            raise ValueError("无效 GeoJSON 几何") from exc
        if geom.is_empty:
            continue
        if not geom.is_valid:
            raise ValueError("搜索区域几何自交或无效，请先修复")
        bounds = geom.bounds
        if not all(math.isfinite(v) for v in bounds) or bounds[0] < -180 or bounds[2] > 180 or bounds[1] < -90 or bounds[3] > 90:
            raise ValueError("搜索图层坐标必须为 WGS84 经纬度")
        result.append(geom)
    return result


class TerrainGrid:
    """Rectilinear depth grid, bilinear interpolation, explicit NoData boundary.

    Accepts {crs,x_m,y_m,depth_m} or build_dtm()'s {metadata,preview} result.
    Input axes are in the declared CRS; names x_m/y_m reflect the DTM contract.
    """
    def __init__(self, config, search_crs):
        if not isinstance(config, dict):
            raise ValueError("terrain_grid 必须为对象")
        values = config.get("preview", config)
        metadata = config.get("metadata", {})
        if not isinstance(values, dict) or not isinstance(metadata, dict):
            raise ValueError("terrain_grid 数据结构错误")
        try:
            self.crs = CRS.from_user_input(config.get("crs", metadata.get("crs")))
            self.x = np.asarray(values["x_m"], dtype=float)
            self.y = np.asarray(values["y_m"], dtype=float)
            self.z = np.asarray([[float("nan") if v is None else finite_number(v, "depth_m", minimum=0) for v in row] for row in values["depth_m"]], dtype=float)
        except (TypeError, KeyError, ValueError, CRSError) as exc:
            raise ValueError(f"地形网格 CRS、坐标轴或水深无效: {exc}") from exc
        if self.x.ndim != 1 or self.y.ndim != 1 or len(self.x) < 2 or len(self.y) < 2:
            raise ValueError("地形网格 x/y 坐标轴至少各两个值")
        if self.z.shape != (len(self.y), len(self.x)) or self.z.size > 250000:
            raise ValueError("地形矩阵须为 [y][x]，且不超过 250,000 单元")
        if not np.isfinite(self.x).all() or not np.isfinite(self.y).all() or not (np.diff(self.x) > 0).all() or not (np.diff(self.y) > 0).all():
            raise ValueError("地形网格坐标轴须有限且严格递增")
        self.to_grid = Transformer.from_crs(search_crs, self.crs, always_xy=True)
        self.metadata = {"crs": self.crs.to_string(), "width": len(self.x), "height": len(self.y),
                         "source": config.get("source", metadata.get("source", "用户地形网格")),
                         "measured": bool(config.get("measured", False)), "preview_grid": "preview" in config,
                         "vertical_datum": config.get("vertical_datum", metadata.get("vertical_datum", "user-unspecified")),
                         "interpolation": "bilinear_without_extrapolation", "missing_cells": int(np.isnan(self.z).sum())}

    def sample(self, points):
        points = np.asarray(points, dtype=float).reshape(-1, 2)
        x, y = self.to_grid.transform(points[:, 0], points[:, 1])
        x, y = np.asarray(x), np.asarray(y)
        inside = (x >= self.x[0] - 1e-8) & (x <= self.x[-1] + 1e-8) & (y >= self.y[0] - 1e-8) & (y <= self.y[-1] + 1e-8) & np.isfinite(x) & np.isfinite(y)
        x, y = np.clip(x, self.x[0], self.x[-1]), np.clip(y, self.y[0], self.y[-1])
        ix = np.clip(np.searchsorted(self.x, x, side="right") - 1, 0, len(self.x) - 2)
        iy = np.clip(np.searchsorted(self.y, y, side="right") - 1, 0, len(self.y) - 2)
        fx = (x - self.x[ix]) / (self.x[ix + 1] - self.x[ix])
        fy = (y - self.y[iy]) / (self.y[iy + 1] - self.y[iy])
        result, missing = np.zeros(len(points)), ~inside
        for dx, dy, weight in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)), (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
            values = self.z[iy + dy, ix + dx]
            missing |= (~np.isfinite(values)) & (weight > 1e-12)
            result += np.where(np.isfinite(values), values, 0) * weight
        result[missing] = np.nan
        return result


class _EdgeCost:
    def __init__(self, blocked, allowed, cables, weighted, bounds, config, terrain):
        self.blocked = blocked
        self.blocked_prepared = prep(blocked) if not blocked.is_empty else None
        self.allowed = allowed
        self.allowed_prepared = prep(allowed) if allowed is not None else None
        self.cables, self.weighted, self.bounds = cables, weighted, bounds
        self.terrain = terrain
        self.slope_weight = finite_number(config.get("slope_weight", 0), "slope_weight", minimum=0, maximum=1_000_000)
        self.crossing_penalty = finite_number(config.get("crossing_penalty_m", 0), "crossing_penalty_m", minimum=0, maximum=10_000_000)
        self.max_slope = None if config.get("max_slope_deg") is None else finite_number(config["max_slope_deg"], "max_slope_deg", minimum=0, maximum=89.9999)
        self.minimum_depth = None if config.get("min_depth_m") is None else finite_number(config["min_depth_m"], "min_depth_m", minimum=0)
        self.maximum_depth = None if config.get("max_depth_m") is None else finite_number(config["max_depth_m"], "max_depth_m", minimum=0)
        if self.minimum_depth is not None and self.maximum_depth is not None and self.minimum_depth > self.maximum_depth:
            raise ValueError("min_depth_m 不可大于 max_depth_m")
        self.terrain_required = any(value is not None for value in (self.max_slope, self.minimum_depth, self.maximum_depth)) or self.slope_weight > 0
        if self.terrain_required and terrain is None:
            raise ValueError("地形坡度／深度约束和权重需要二维 terrain_grid，不能从沿线 KP 剖面推断侧向地形")
        spacing = config.get("grid_spacing_m", 1000)
        self.step = finite_number(config.get("terrain_sample_step_m", min(spacing / 2, 500)), "terrain_sample_step_m", minimum=1, maximum=100000)
        self.cache = {}
        self.evaluated_edges = 0

    def point_ok(self, point):
        shape_point = Point(point)
        if not self.bounds.covers(shape_point):
            return False
        if self.blocked_prepared is not None and self.blocked_prepared.intersects(shape_point):
            return False
        if self.allowed_prepared is not None and not self.allowed_prepared.covers(shape_point):
            return False
        if self.terrain_required:
            depth = self.terrain.sample([point])[0]
            if not math.isfinite(depth) or self.minimum_depth is not None and depth < self.minimum_depth or self.maximum_depth is not None and depth > self.maximum_depth:
                return False
        return True

    def evaluate(self, a, b):
        key = tuple(sorted((tuple(a), tuple(b))))
        if key in self.cache:
            return self.cache[key]
        self.evaluated_edges += 1
        result = self._evaluate(a, b)
        if len(self.cache) < 250_000:
            self.cache[key] = result
        return result

    def _evaluate(self, a, b):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length < EPS:
            return {"total": 0, "distance_m": 0, "slope_cost_m": 0, "crossing_cost_m": 0, "zone_cost_m": 0, "crossing_units": 0, "max_slope_deg": 0}
        line = LineString([a, b])
        if not self.bounds.covers(line) or self.blocked_prepared is not None and self.blocked_prepared.intersects(line) or self.allowed_prepared is not None and not self.allowed_prepared.covers(line):
            return None
        slope_cost, maximum_slope = 0.0, None
        if self.terrain_required:
            count = max(2, int(math.ceil(length / self.step)))
            if count > 10000:
                return None
            locations = np.linspace(a, b, count + 1)
            depths = self.terrain.sample(locations)
            if not np.isfinite(depths).all():
                return None
            if self.minimum_depth is not None and float(depths.min()) < self.minimum_depth or self.maximum_depth is not None and float(depths.max()) > self.maximum_depth:
                return None
            changes = np.abs(np.diff(depths))
            slopes = np.degrees(np.arctan2(changes, length / count))
            maximum_slope = float(slopes.max())
            if self.max_slope is not None and maximum_slope > self.max_slope + 1e-8:
                return None
            slope_cost = float(changes.sum()) * self.slope_weight
        crossing_units = 0.0
        for cable in self.cables:
            intersection = line.intersection(cable)
            if intersection.is_empty:
                continue
            # Half charge at endpoints keeps a crossing split over two edges
            # chargeable, rather than allowing free passage through a grid node.
            for point in _point_intersections(intersection):
                crossing_units += .5 if point.distance(Point(a)) < EPS or point.distance(Point(b)) < EPS else 1.0
        crossing_cost = crossing_units * self.crossing_penalty
        zone_cost = sum(line.intersection(geom).length * multiplier for geom, multiplier in self.weighted)
        total = length + slope_cost + crossing_cost + zone_cost
        if not math.isfinite(total):
            raise ValueError("搜索边代价溢出，请减小权重")
        return {"total": total, "distance_m": length, "slope_cost_m": slope_cost,
                "crossing_cost_m": crossing_cost, "zone_cost_m": zone_cost, "crossing_units": crossing_units, "max_slope_deg": maximum_slope}


def _point_intersections(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if geometry.geom_type in ("LineString", "LinearRing"):
        return [Point(geometry.coords[0]), Point(geometry.coords[-1])]
    if hasattr(geometry, "geoms"):
        return [point for part in geometry.geoms for point in _point_intersections(part)]
    return []


def _search_segment(start, goal, grid, costs, max_expansions):
    x0, y0, spacing, nx, ny = grid
    start_id, goal_id = nx * ny, nx * ny + 1
    positions = {start_id: tuple(start), goal_id: tuple(goal)}
    valid = {}

    def position(node):
        if node in positions:
            return positions[node]
        return x0 + (node % nx) * spacing, y0 + (node // nx) * spacing

    def usable(node):
        if node not in valid:
            valid[node] = costs.point_ok(position(node))
        return valid[node]

    def anchors(point):
        ix, iy = int(round((point[0] - x0) / spacing)), int(round((point[1] - y0) / spacing))
        return [y * nx + x for y in range(max(0, iy - 1), min(ny, iy + 2)) for x in range(max(0, ix - 1), min(nx, ix + 2))]

    start_anchors, goal_anchors = anchors(start), set(anchors(goal))
    if not costs.point_ok(start) or not costs.point_ok(goal):
        raise RoutingError("ROUTING_ENDPOINT_BLOCKED", "起点、终点或必经点违反禁止／允许区域或地形条件")
    def neighbours(node):
        if node == start_id:
            return start_anchors + [goal_id]
        if node == goal_id:
            return []
        x, y = node % nx, node // nx
        values = [(y + dy) * nx + x + dx for dx, dy in ((1,0), (-1,0), (0,1), (0,-1), (1,1), (-1,1), (1,-1), (-1,-1))
                  if 0 <= x + dx < nx and 0 <= y + dy < ny]
        if node in goal_anchors:
            values.append(goal_id)
        return values

    sequence = itertools.count()
    best, parent, queue, expanded = {start_id: 0.0}, {}, [], 0
    heapq.heappush(queue, (math.dist(start, goal), next(sequence), start_id, 0.0))
    while queue:
        _, _, node, score = heapq.heappop(queue)
        if score > best.get(node, math.inf) + EPS:
            continue
        if node == goal_id:
            nodes = [node]
            while node in parent:
                node = parent[node]
                nodes.append(node)
            nodes.reverse()
            return [position(n) for n in nodes], {"expanded_nodes": expanded, "visited_nodes": len(best), "graph_cost_m_equivalent": score}
        expanded += 1
        if expanded > max_expansions:
            raise RoutingError("ROUTING_SEARCH_LIMIT", f"超过 {max_expansions} 个展开节点；请加大网格间距、缩小范围或拆段")
        for neighbour in neighbours(node):
            if neighbour != goal_id and not usable(neighbour):
                continue
            edge = costs.evaluate(position(node), position(neighbour))
            if edge is None:
                continue
            candidate = score + edge["total"]
            if candidate + 1e-8 < best.get(neighbour, math.inf):
                best[neighbour] = candidate
                parent[neighbour] = node
                heuristic = math.dist(position(neighbour), goal)
                heapq.heappush(queue, (candidate + heuristic, next(sequence), neighbour, candidate))
    raise RoutingError("ROUTING_NO_PATH", "当前范围、分辨率和约束下没有可达网格路径；不能据此断言连续空间无路")


def _smooth(path, costs):
    """Visibility shortcuts accepted only if they do not increase objective."""
    if len(path) < 3:
        return path
    prefix = [0.0]
    for a, b in zip(path, path[1:]):
        prefix.append(prefix[-1] + costs.evaluate(a, b)["total"])
    result, index = [path[0]], 0
    while index < len(path) - 1:
        chosen = index + 1
        for end in range(min(len(path) - 1, index + 64), index + 1, -1):
            value = costs.evaluate(path[index], path[end])
            if value is not None and value["total"] <= prefix[end] - prefix[index] + EPS:
                chosen = end
                break
        result.append(path[chosen])
        index = chosen
    return result


def _cost_report(path, costs):
    values = [costs.evaluate(a, b) for a, b in zip(path, path[1:])]
    if any(value is None for value in values):
        raise RoutingError("ROUTING_VALIDATION_FAILED", "候选路径未通过区间约束校核")
    keys = ("total", "distance_m", "slope_cost_m", "crossing_cost_m", "zone_cost_m", "crossing_units")
    result = {key: sum(value[key] for value in values) for key in keys}
    slopes = [value["max_slope_deg"] for value in values if value["max_slope_deg"] is not None]
    result["max_sampled_slope_deg"] = max(slopes) if slopes else None
    return result


def _constraints(project, config, forward, clearance, cable_clearance):
    layers = project.get("layers", [])
    known = {str(layer.get("id")): layer for layer in layers}
    obstacle_ids = config.get("obstacle_layer_ids")
    if obstacle_ids is None:
        obstacle_ids = [str(layer.get("id")) for layer in layers if layer.get("kind") in ("restricted", "exclusion", "hazard", "land")]
    allowed_ids = config.get("allowed_layer_ids", [])
    if not isinstance(obstacle_ids, list) or not isinstance(allowed_ids, list):
        raise ValueError("obstacle_layer_ids 和 allowed_layer_ids 必须为数组")
    for key in obstacle_ids + allowed_ids:
        if str(key) not in known:
            raise ValueError(f"搜索图层不存在: {key}")
    blocked, allowed, cables, preview = [], [], [], []
    for layer in layers:
        key = str(layer.get("id"))
        if key not in list(map(str, obstacle_ids)) + list(map(str, allowed_ids)) and layer.get("kind") != "cable":
            continue
        geometries = _features(layer.get("geojson", {}))
        for geometry in geometries:
            projected = transform(forward.transform, geometry)
            if not all(math.isfinite(v) for v in projected.bounds) or not projected.is_valid:
                raise ValueError("图层超出局部投影可用范围或投影后无效，请按海区裁剪")
            if key in list(map(str, obstacle_ids)):
                blocked.append(projected.buffer(clearance) if clearance > 0 else projected)
                preview.append({"layer_id": layer.get("id"), "name": layer.get("name", ""), "kind": "obstacle"})
            if key in list(map(str, allowed_ids)):
                if projected.geom_type not in ("Polygon", "MultiPolygon"):
                    raise ValueError("允许区域必须为 Polygon／MultiPolygon")
                allowed.append(projected)
            if layer.get("kind") == "cable":
                cables.append(projected)
                if config.get("avoid_existing_cables", False):
                    blocked.append(projected.buffer(cable_clearance) if cable_clearance > 0 else projected)
    for geojson in config.get("forbidden_areas", []):
        for geometry in _features(geojson):
            projected = transform(forward.transform, geometry)
            blocked.append(projected.buffer(clearance) if clearance > 0 else projected)
    for geojson in config.get("allowed_areas", []):
        for geometry in _features(geojson):
            projected = transform(forward.transform, geometry)
            if projected.geom_type not in ("Polygon", "MultiPolygon"):
                raise ValueError("允许区域必须为 Polygon／MultiPolygon")
            allowed.append(projected)
    blocked_union = unary_union(blocked) if blocked else GeometryCollection()
    allowed_union = unary_union(allowed) if allowed else None
    if allowed_union is not None and clearance > 0:
        allowed_union = allowed_union.buffer(-clearance)
        if allowed_union.is_empty:
            raise RoutingError("ROUTING_ALLOWED_AREA_EMPTY", "允许区域扣除走廊后为空")
    weighted = []
    for item in config.get("weighted_areas", []):
        if not isinstance(item, dict):
            raise ValueError("weighted_areas 每项须为对象")
        weight = finite_number(item.get("cost_per_m"), "cost_per_m", minimum=0, maximum=1_000_000)
        for geometry in _features(item.get("geojson", {})):
            projected = transform(forward.transform, geometry)
            if projected.geom_type not in ("Polygon", "MultiPolygon"):
                raise ValueError("加权区域必须为多边形")
            weighted.append((projected, weight))
    return blocked_union, allowed_union, cables, weighted, preview


def _path_station(coords, curve):
    keys = [0.0]
    for a, b in zip(coords, coords[1:]):
        keys.append(keys[-1] + inverse(*a, *b, curve)[0])
    return keys


def _at_station(coords, keys, kp, curve):
    index = max(0, min(len(coords) - 2, bisect.bisect_right(keys, kp) - 1))
    fraction = (kp - keys[index]) / (keys[index+1] - keys[index]) if keys[index+1] > keys[index] else 0
    return interpolate(*coords[index], *coords[index+1], max(0, min(1, fraction)), curve)


def _preview_project(project, before, coordinates, anchor_old, anchor_new, start_index, end_index, terrain, forward, config):
    result = deepcopy(project)
    result.pop("saved_revision", None)
    result["id"] = str(uuid4())
    result["name"] = str(project.get("name", "海缆工程")) + " · 避障候选"
    route = result["route"]
    curve = route.get("curve", "rhumb")
    original_points = project["route"]["points"]
    old_keys = [p["kp_m"] for p in before["rpl"]]
    path_keys = _path_station(coordinates, curve)
    old_start, old_end = old_keys[start_index], old_keys[end_index]
    shift = path_keys[-1] - (old_end - old_start)

    def remap(kp):
        if kp <= old_start:
            return kp
        if kp >= old_end:
            return kp + shift
        index = max(0, min(len(anchor_old) - 2, bisect.bisect_right(anchor_old, kp) - 1))
        width = anchor_old[index+1] - anchor_old[index]
        fraction = (kp - anchor_old[index]) / width if width > 0 else 0
        return old_start + anchor_new[index] + fraction * (anchor_new[index+1] - anchor_new[index])

    links = {remap(old_keys[i]) - old_start: i for i in range(start_index, end_index + 1)}
    positions = sorted(set(path_keys + list(links)))
    merged_positions = []
    for position in positions:
        if not merged_positions or position - merged_positions[-1] > EPS:
            merged_positions.append(position)
        elif position in links:
            merged_positions[-1] = position
    points = deepcopy(original_points[:start_index])
    ids = {str(p.get("id", f"p{i+1}")) for i, p in enumerate(original_points)}
    for i, kp in enumerate(merged_positions):
        lon, lat = _at_station(coordinates, path_keys, kp, curve)
        link_key = next((key for key in links if abs(key - kp) <= EPS), None)
        if link_key is not None:
            old_index = links[link_key]
            point = deepcopy(original_points[old_index])
            point["id"] = str(point.get("id", before["rpl"][old_index]["id"]))
            point["routing_original_coordinate"] = [point["longitude"], point["latitude"]]
            point["routing_link"] = "preserved_station_constraint"
            if any(abs(old_keys[old_index] - value) <= EPS for value in anchor_old):
                lon, lat = original_points[old_index]["longitude"], original_points[old_index]["latitude"]
                point["routing_link"] = "preserved_geographic_anchor"
        else:
            key = f"route-search-{i+1}"
            while key in ids:
                key += "_"
            ids.add(key)
            point = {"id": key, "label": f"避障候选 {i+1}", "note": "独立 A* 栅格候选路径，需人工复核"}
        point["longitude"], point["latitude"] = lon, lat
        if terrain is not None:
            depth = terrain.sample([forward.transform(lon, lat)])[0]
            point["depth_m"] = float(depth) if math.isfinite(depth) else None
        else:
            # A moved waypoint's old depth does not describe its new position.
            point["depth_m"] = None
        points.append(point)
    points += deepcopy(original_points[end_index+1:])
    if len(points) > 10000:
        raise RoutingError("ROUTING_RESULT_LIMIT", "候选超过 10,000 个路线点，请加大搜索间距")
    route["points"] = points
    geometry_changed = route_signature(result) != route_signature(project)
    new_coords = [(p["longitude"], p["latitude"]) for p in points]
    new_keys = _path_station(new_coords, curve)
    old_new_keys = [remap(key) for key in old_keys]
    length_policy = config.get("length_policy", "preserve")
    if length_policy not in ("preserve", "recalculate"):
        raise ValueError("length_policy 必须为 preserve 或 recalculate")
    opts, remaining, originals = [], {}, {}
    for i in range(len(before["legs"])):
        originals[i] = _effective_leg(project, before, i)
        remaining[i] = before["legs"][i]["cable_length_m"]
    for i, (left, right) in enumerate(zip(new_keys, new_keys[1:])):
        middle = (left + right) / 2
        old_index = max(0, min(len(before["legs"])-1, bisect.bisect_right(old_new_keys, middle + EPS) - 1))
        opt = deepcopy(originals[old_index])
        segment_end = old_new_keys[old_index+1]
        at_end = abs(right - segment_end) <= max(EPS, abs(segment_end) * 1e-10)
        if not at_end:
            for field in ("allowance_m", "stop_hours", "extra_cost"):
                opt[field] = 0
        selected = start_index <= old_index < end_index
        preserve = selected and geometry_changed and length_policy == "preserve" or opt["mode"] == "fixed"
        # Bottom slack cannot be re-evaluated on missing lateral terrain.
        if selected and geometry_changed and length_policy == "recalculate" and opt["mode"] == "flexible" and opt["slack_basis"] == "bottom" and terrain is None:
            raise ValueError("柔性底余缆重新计算需要有效二维地形网格")
        if preserve:
            width = old_new_keys[old_index+1] - old_new_keys[old_index]
            amount = remaining[old_index] if at_end else before["legs"][old_index]["cable_length_m"] * (right-left) / width if width > EPS else 0
            opt["mode"], opt["fixed_cable_length_m"] = "fixed", max(0, amount)
            remaining[old_index] -= amount
        opts.append(opt)
    route["legs"] = opts
    route["mode"] = "fixed" if all(o["mode"] == "fixed" for o in opts) else "flexible"
    route["allowances"] = [{**a, "kp_m": remap(a["kp_m"])} for a in _canonical_allowances(project, before)]
    result.pop("allowances", None)
    result["events"] = [{**deepcopy(e), "kp_m": remap(e["kp_m"])} for e in project.get("events", project["route"].get("events", []))]
    route.pop("events", None)
    for body, computed in zip(result.get("bodies", []), before["bodies"]):
        if body.get("cable_kp_m") is not None or length_policy == "preserve" and body.get("length_mode", "replace") == "replace":
            body["cable_kp_m"] = computed["cable_kp_m"]
            body.pop("kp_m", None)
        else:
            body["kp_m"] = remap(computed["kp_m"])
            body.pop("cable_kp_m", None)
    profile = []
    if terrain is not None:
        for i, (a, b) in enumerate(zip(new_coords, new_coords[1:])):
            length, _ = inverse(*a, *b, curve)
            count = max(1, math.ceil(length / min(float(config.get("grid_spacing_m", 1000)) / 2, 500)))
            if len(profile) + count + 1 > 100000:
                raise ValueError("候选地形采样超过 100,000 点，请加大搜索间距或拆分")
            for j in range(count + 1):
                if i and j == 0:
                    continue
                lon, lat = interpolate(*a, *b, j / count, curve)
                depth = terrain.sample([forward.transform(lon, lat)])[0]
                profile.append({"kp_m": new_keys[i] + length * j / count, "depth_m": float(depth) if math.isfinite(depth) else None})
        source = "route_search_terrain_grid"
        metadata = terrain.metadata
        measured = terrain.metadata["measured"]
    else:
        old_profile = before["profile"]
        for sample in old_profile:
            kp = sample["kp_m"]
            depth = sample["depth_m"] if kp <= old_start + EPS or kp >= old_end - EPS else None
            profile.append({"kp_m": remap(kp), "depth_m": depth})
        # A missing interior sample is necessary even when only the old endpoints
        # existed: it prevents a false linear measured seabed across the new path.
        for i in range(len(new_keys)-1):
            middle = (new_keys[i] + new_keys[i+1]) / 2
            if old_start + EPS < middle < old_end + shift - EPS:
                profile.append({"kp_m": middle, "depth_m": None})
        source = "route_search_partial_profile" if before["profile_metadata"]["imported_profile_valid"] else "route_search_waypoint_approximation"
        if str(before["profile_metadata"]["source"]).startswith("synthetic"):
            source = "synthetic_" + source
        metadata = {"original_source": before["profile_metadata"], "unmeasured_range_m": [old_start, old_end + shift],
                    "retained_geometry": "unchanged_prefix_and_suffix_only"}
        measured = False
    profile.sort(key=lambda p: p["kp_m"])
    clean = []
    for sample in profile:
        if clean and abs(sample["kp_m"] - clean[-1]["kp_m"]) <= EPS:
            if sample["depth_m"] is None:
                clean[-1]["depth_m"] = None
            continue
        clean.append(sample)
    result["profile"] = {"samples": clean, "route_signature": route_signature(result), "source": source,
                         "measured": measured, "metadata": metadata}
    if not geometry_changed and terrain is None:
        if "profile" in project:
            result["profile"] = deepcopy(project["profile"])
        else:
            result.pop("profile", None)
        old_depths = {str(p.get("id", before["rpl"][i]["id"])): p.get("depth_m") for i,p in enumerate(original_points)}
        for point in result["route"]["points"]:
            if point["id"] in old_depths:
                point["depth_m"] = old_depths[point["id"]]
    return result, {"length_policy": length_policy, "old_selected_length_m": old_end-old_start,
                    "new_selected_length_m": path_keys[-1], "station_shift_m": shift,
                    "point_count": len(points), "geometry_changed": geometry_changed,
                    "preserved_constraint_links": end_index-start_index+1}


def search_route(project: dict, config: dict | None = None) -> dict:
    """Return a candidate project; never save or overwrite the input project."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("搜索 config 必须为对象")
    before = analyze_project(project)
    if project.get("route", {}).get("constraint_state") is not None:
        raise RoutingError("ROUTING_CONSTRAINT_DOMAIN_UNSUPPORTED", "自动避障尚未变换已配置的 Path Link 制造域；请明确取消旧域并在候选上重新配置，不能静默绕过域约束")
    points = project["route"]["points"]
    start_index = _index(config.get("start_point_index", 0), "start_point_index", len(points)-1)
    end_index = _index(config.get("end_point_index", len(points)-1), "end_point_index", len(points)-1)
    if end_index <= start_index:
        raise ValueError("搜索终点必须在起点之后")
    spacing = finite_number(config.get("grid_spacing_m", 1000), "grid_spacing_m", minimum=1, maximum=100000)
    padding = finite_number(config.get("padding_m", max(5*spacing, 10000)), "padding_m", minimum=spacing, maximum=500000)
    clearance = finite_number(config.get("clearance_m", 0), "clearance_m", minimum=0, maximum=100000)
    cable_clearance = finite_number(config.get("cable_clearance_m", clearance), "cable_clearance_m", minimum=0, maximum=100000)
    max_cells = _index(config.get("max_cells", 200000), "max_cells", 250000)
    max_expansions = _index(config.get("max_expansions", 200000), "max_expansions", 250000)
    if max_cells < 9 or max_expansions < 1:
        raise ValueError("max_cells 至少 9，max_expansions 至少 1")
    raw_via = config.get("via_point_indices", [])
    if not isinstance(raw_via, list):
        raise ValueError("via_point_indices 必须为数组")
    via = [_index(v, "via_point_index", len(points)-1) for v in raw_via]
    if via != sorted(set(via)) or any(not start_index < i < end_index for i in via):
        raise ValueError("必经点索引须在搜索范围内部严格递增，不能重复")
    via = sorted(set(via + [i for i in range(start_index+1, end_index) if points[i].get("constraint") == "rigid" or points[i].get("fixed_position") is True]))
    anchors = [start_index] + via + [end_index]
    anchor_coords = [coordinate(points[i]["longitude"], points[i]["latitude"]) for i in anchors]
    if any(inverse(*a, *b, project["route"].get("curve", "rhumb"))[0] < EPS for a, b in zip(anchor_coords, anchor_coords[1:])):
        raise ValueError("相邻搜索锚点位置重复，请去除重复必经点")
    longitudes = np.radians([p[0] for p in anchor_coords])
    lon = math.degrees(math.atan2(float(np.sin(longitudes).mean()), float(np.cos(longitudes).mean())))
    lat = float(np.mean([p[1] for p in anchor_coords]))
    local = CRS.from_proj4(f"+proj=aeqd +lon_0={lon} +lat_0={lat} +datum=WGS84 +units=m")
    forward = Transformer.from_crs("EPSG:4326", local, always_xy=True)
    backward = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
    xy = np.array([forward.transform(*p) for p in anchor_coords])
    if not np.isfinite(xy).all() or float(np.linalg.norm(xy, axis=1).max()) + padding > 1000000:
        raise ValueError("搜索范围离局部投影中心超过 1,000 km，请分海区搜索")
    xmin, ymin = np.floor((xy.min(axis=0) - padding)/spacing)*spacing
    xmax, ymax = np.ceil((xy.max(axis=0) + padding)/spacing)*spacing
    nx, ny = int(round((xmax-xmin)/spacing))+1, int(round((ymax-ymin)/spacing))+1
    if nx*ny > max_cells:
        raise RoutingError("ROUTING_GRID_LIMIT", f"搜索网格 {nx}×{ny} 超过 {max_cells} 单元，请增加间距或减少范围")
    bounds = box(xmin, ymin, xmax, ymax)
    blocked, allowed, cables, weighted, obstacles = _constraints(project, config, forward, clearance, cable_clearance)
    terrain = TerrainGrid(config["terrain_grid"], local) if config.get("terrain_grid") is not None else None
    costs = _EdgeCost(blocked, allowed, cables, weighted, bounds, {**config, "grid_spacing_m": spacing}, terrain)
    projected_path, segment_reports, anchor_new, old_keys = [], [], [0.0], [p["kp_m"] for p in before["rpl"]]
    curve = project["route"].get("curve", "rhumb")
    for index, (start, goal) in enumerate(zip(xy, xy[1:])):
        remaining_expansions = max_expansions - sum(r["expanded_nodes"] for r in segment_reports)
        if remaining_expansions < 1:
            raise RoutingError("ROUTING_SEARCH_LIMIT", f"必经区间累计超过 {max_expansions} 个展开节点")
        path, report = _search_segment(tuple(start), tuple(goal), (xmin,ymin,spacing,nx,ny), costs, remaining_expansions)
        if config.get("simplify", True):
            path = _smooth(path, costs)
        coordinates = [backward.transform(*p) for p in path]
        # Keep user anchors bit-for-bit, avoiding endpoint projection roundoff.
        coordinates[0], coordinates[-1] = anchor_coords[index], anchor_coords[index+1]
        # Validate actual selected geographic curves, not merely the straight
        # projected grid edges. Denser edges remain explicit numerical screening.
        actual = []
        check_step = max(1.0, min(100.0, spacing/8))
        for a, b in zip(coordinates, coordinates[1:]):
            dense = densify(*a, *b, curve, check_step, 20000)
            actual.extend([forward.transform(*p) for p in dense] if not actual else [forward.transform(*p) for p in dense[1:]])
        actual_cost = _cost_report(actual, costs)
        report["candidate_objective"] = actual_cost
        report["geographic_validation_step_m"] = check_step
        segment_reports.append(report)
        projected_path.extend(coordinates if not projected_path else coordinates[1:])
        anchor_new.append(anchor_new[-1] + _path_station(coordinates, curve)[-1])
    candidate, transformations = _preview_project(project, before, projected_path, [old_keys[i] for i in anchors], anchor_new,
                                                  start_index, end_index, terrain, forward, {**config, "grid_spacing_m": spacing})
    after = analyze_project(candidate)
    warnings = [_warning("ROUTE_SEARCH_CANDIDATE", "结果为局部投影、指定网格和搜索范围下的可审查候选，不代表连续空间或全工程全局最优")]
    if terrain is None and transformations["geometry_changed"]:
        warnings.append(_warning("ROUTE_SEARCH_DEPTH_UNAVAILABLE", "改变的路径没有二维地形测深；保留未改变部分的来源，改变区间标缺测"))
    elif terrain is not None and terrain.metadata["preview_grid"]:
        warnings.append(_warning("ROUTE_SEARCH_PREVIEW_TERRAIN", "使用 DTM 的降采样预览网格；有效分辨率为预览轴间距，不能视为原始测深精度"))
    if transformations["length_policy"] == "preserve" and transformations["geometry_changed"]:
        warnings.append(_warning("ROUTE_SEARCH_FIXED_ASSEMBLY", "搜索范围内保留既有制造缆量并转换为固定约束，负余缆或缆量不足仍须在规划校核中处理"))
    geometry_segments = split_antimeridian(projected_path, curve)
    geometry = {"type": "LineString", "coordinates": projected_path} if len(geometry_segments) < 2 else {"type": "MultiLineString", "coordinates": geometry_segments}
    objectives = {key: sum(report["candidate_objective"][key] for report in segment_reports)
                  for key in ("total", "distance_m", "slope_cost_m", "crossing_cost_m", "zone_cost_m", "crossing_units")}
    return {"project": candidate, "warnings": warnings, "preview": {"geometry": geometry, "obstacle_layers": obstacles,
            "blocked_area": mapping(transform(backward.transform, blocked)),
            "allowed_area": mapping(transform(backward.transform, allowed)) if allowed is not None else None},
            "report": {"operation": "route_search", "algorithm": "A*_8_neighbour_grid_with_nonincreasing_visibility_shortcuts",
                       "validation_status": "planning_candidate", "optimality": "no_continuous_global_optimality_claim",
                       "projection": local.to_string(), "grid_spacing_m": spacing, "grid_width": nx, "grid_height": ny,
                       "grid_cells": nx*ny, "bounds_m": [xmin,ymin,xmax,ymax], "clearance_m": clearance,
                       "cable_clearance_m": cable_clearance, "obstacle_layer_ids": sorted(set(str(p["layer_id"]) for p in obstacles)),
                       "selected_start_index": start_index, "selected_end_index": end_index, "via_point_indices": via,
                       "segments": segment_reports, "objective_m_equivalent": objectives, "evaluated_edges": costs.evaluated_edges,
                       "constraint_validation": {"selected_section_passed": True, "obstacle_collision": False,
                                                "within_allowed_area": True if allowed is not None else None,
                                                "terrain_constraints_checked": costs.terrain_required,
                                                "max_slope_deg": costs.max_slope, "min_depth_m": costs.minimum_depth,
                                                "max_depth_m": costs.maximum_depth, "terrain_sample_step_m": costs.step if terrain is not None else None},
                       "terrain": terrain.metadata if terrain is not None else None, "transformations": transformations,
                       "before_summary": before["summary"], "after_summary": after["summary"],
                       "assumptions": ["搜索边代价 = 局部平面距离 + slope_weight×累计水深变化绝对值 + 穿越罚距 + 加权区域长度代价。",
                                       "所有代价非负，启发式为目标点欧氏距离；目标值单位是等效米，不是费用报价。",
                                       "沿边地形采用二维网格双线性插值与明确采样间距；缺测和网格外部不外推。",
                                       "允许区域扣除走廊宽度，禁止区域按指定净距缓冲；连边碰撞检测避免仅检测节点导致的穿障。",
                                       "完成后加密检查实际恒向／测地曲线；投影误差、地形采样误差、测深质量和工程可施工性仍需复核。",
                                       "保持必经锚点；其间旧缆型／固定约束链接按分段比例映射，原地形只保留空间未改变部分。"]}}
