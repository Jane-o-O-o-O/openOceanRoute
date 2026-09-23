"""Explicit, portable bathymetry source library and point-wise priority queries.

This is an independent research sampler, not a Makai connection/native schema.
Only embedded user data are read: no paths, URLs, implicit vertical conversion,
or filling a failed source with zero. Existing single-source APIs are unchanged.
"""
from __future__ import annotations

import base64
from collections import OrderedDict, Counter
from copy import deepcopy
import csv
import hashlib
import json
import math
import struct
import threading
import warnings as python_warnings

import numpy as np
from pyproj import CRS
from pyproj.transformer import TransformerGroup
from scipy.spatial import Delaunay, QhullError, cKDTree

from .geodesy import coordinate, finite_number, inverse, interpolate
from .route_geometry import route_segments
from .surfer import read_grid, _sample as _surfer_sample
from .units import length_factor

SCHEMA = "oceanroute.terrain-source"
MODEL = "priority-terrain-library-v1"
MAX_SOURCES = 8
MAX_SOURCE_BYTES = 8 * 1024**2
MAX_TOTAL_SOURCE_BYTES = 12 * 1024**2
MAX_LIBRARY_JSON_BYTES = 16 * 1024**2
MAX_XYZ_POINTS = 200_000
MAX_RASTER_CELLS = 1_000_000
MAX_QUERY_POINTS = 50_000
MAX_CACHE_BYTES = 64 * 1024**2
MAX_CACHE_ENTRIES = 8
MAX_PREPARED_SOURCE_BYTES = 128 * 1024**2
_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()


class TerrainSourceError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _error(code, message):
    raise TerrainSourceError(code, message)


def _canonical(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _json(value):
    try:
        return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        _error("TERRAIN_JSON", f"地形资料须为有限、可保存的JSON：{exc}")


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _error("TERRAIN_LIMIT", f"{name}须为{low}～{high}的整数")
    return value


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _error("TERRAIN_METADATA", f"{name}须为1～{maximum}字符的非空文本")
    return value.strip()


def _crs(value):
    _text(value, "source_crs", 8192)
    try:
        result = CRS.from_user_input(value)
    except Exception as exc:
        _error("TERRAIN_CRS", f"无法识别源坐标系：{exc}")
    if not (result.is_geographic or result.is_projected) or len(result.axis_info) != 2:
        _error("TERRAIN_CRS", "源坐标系须为明确的二维地理或投影CRS，垂直基准另行声明")
    if result.is_geographic and any(abs(a.unit_conversion_factor-math.pi/180) > 1e-12 for a in result.axis_info):
        _error("TERRAIN_CRS", "当前地理源坐标须使用角度，不能按弧度或其他角单位猜测")
    return result


def _strict_transformer(source, target):
    """Freeze an available non-ballpark operation, never silently datum-shift.

    Selection is for the whole source CRS rather than a separately selected
    region per sample. Missing best grids reject the attempted source; callers
    must not interpret this configuration failure as a bathymetric NoData hole.
    """
    try:
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("ignore", UserWarning)
            group = TransformerGroup(source, target, always_xy=True, allow_ballpark=False)
        if not group.best_available:
            _error("TERRAIN_CRS_OPERATION", "最佳水平坐标操作所需的PROJ基准网格不可用；不降级为其他基准操作")
        if not group.transformers:
            _error("TERRAIN_CRS_OPERATION", "源坐标系没有可用的非ballpark水平转换")
        transformer = group.transformers[0]
        return transformer, {"source_crs": CRS.from_user_input(source).to_string(),
                             "target_crs": CRS.from_user_input(target).to_string(),
                             "description": transformer.description,
                             "accuracy_m": transformer.accuracy if transformer.accuracy >= 0 else None,
                             "ballpark": False, "best_available": True,
                             "selection": "whole_source_crs_first_available_best",
                             "network_enabled": transformer.is_network_enabled}
    except TerrainSourceError:
        raise
    except Exception as exc:
        _error("TERRAIN_CRS_OPERATION", f"无法建立明确水平转换：{exc}")


def _bytes(source):
    if source["kind"] == "xyz":
        return source["text"].encode("utf-8")
    try:
        return base64.b64decode(source["data_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        _error("TERRAIN_DATA", f"{source.get('id', '源')}的data_base64无效：{exc}")


def _surfer_dimensions(data):
    """Check dimensions before the existing full parser allocates its array."""
    try:
        if data[:4] == b"DSAA":
            fields = data.split(None, 3)
            nx, ny = int(fields[1]), int(fields[2])
        elif data[:4] == b"DSBB":
            nx, ny = struct.unpack_from("<hh", data, 4)
        elif data[:4] == b"DSRB":
            offset, found = 12, False
            while offset < len(data):
                name, size = struct.unpack_from("<4si", data, offset)
                offset += 8
                if size < 0 or offset+size > len(data):
                    raise ValueError("DSRB节越界")
                if name == b"GRID":
                    ny, nx = struct.unpack_from("<ii", data, offset)
                    found = True
                    break
                offset += size
            if not found:
                raise ValueError("缺少GRID")
        else:
            raise ValueError("不支持的网格标记")
    except (IndexError, UnicodeError, ValueError, struct.error) as exc:
        _error("TERRAIN_DATA", f"Surfer表头损坏：{exc}")
    if nx < 2 or ny < 2 or nx*ny > MAX_RASTER_CELLS:
        _error("TERRAIN_LIMIT", f"源栅格须至少2×2且不超过{MAX_RASTER_CELLS:,}节点")
    return nx, ny


def _rasterio():
    try:
        from rasterio.io import MemoryFile
        return MemoryFile
    except ImportError:
        _error("TERRAIN_DEPENDENCY", "GeoTIFF需terrain可选依赖（rasterio）")


def _raster_header(source, data):
    MemoryFile = _rasterio()
    try:
        with MemoryFile(data) as memory, memory.open() as raster:
            if not raster.crs or not CRS(raster.crs).equals(_crs(source["source_crs"]), ignore_axis_order=True):
                _error("TERRAIN_CRS", "声明的GeoTIFF源CRS与文件内置CRS不一致或文件缺少CRS")
            if raster.width*raster.height > MAX_RASTER_CELLS or min(raster.width, raster.height) < 1:
                _error("TERRAIN_LIMIT", f"单源GeoTIFF最多{MAX_RASTER_CELLS:,}像素")
            if source["band"] > raster.count:
                _error("TERRAIN_DATA", "GeoTIFF请求的band超出文件波段数")
            if np.dtype(raster.dtypes[source["band"]-1]).kind not in "fiu":
                _error("TERRAIN_DATA", "GeoTIFF水深波段须为实数数值")
            affine = raster.transform
            if not all(math.isfinite(x) for x in affine[:6]) or abs(affine.a*affine.e-affine.b*affine.d) < 1e-30:
                _error("TERRAIN_DATA", "GeoTIFF仿射坐标变换无效")
            return raster.width, raster.height
    except TerrainSourceError:
        raise
    except Exception as exc:
        _error("TERRAIN_DATA", f"无法读取GeoTIFF：{exc}")


def _xyz_rows(source):
    rows, warnings = [], []
    factor = length_factor(source["depth_units"])*(1 if source["depth_positive"] == "down" else -1)
    first = True
    for row_number, raw in enumerate(source["text"].lstrip("\ufeff").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        cells = next(csv.reader([raw])) if "," in raw else raw.replace(";", " ").split()
        if first and any(word in raw.casefold() for word in ("longitude", "latitude", "depth", "elevation", "经度", "纬度", "水深")):
            first = False
            continue
        if first and [str(x).lower() for x in cells] in (["x", "y", "z"], ["y", "x", "z"]):
            first = False
            continue
        first = False
        try:
            if len(cells) != 3:
                raise ValueError("需三列XY和水深")
            x, y, z = map(float, cells)
            if not all(math.isfinite(n) for n in (x, y, z)):
                raise ValueError("数据不得含NaN/Inf；缺测用显式nodata_value")
            if source["coordinate_order"] == "yx":
                x, y = y, x
            depth = z*factor
            missing = source.get("nodata_value") is not None and z == source["nodata_value"]
            if missing or not 0 <= depth <= 1_000_000:
                depth = float("nan")
            rows.append((x, y, depth))
        except (TypeError, ValueError) as exc:
            _error("TERRAIN_XYZ_ROW", f"源{source.get('id', 'XYZ')}第{row_number}行：{exc}")
        if len(rows) > MAX_XYZ_POINTS:
            _error("TERRAIN_LIMIT", f"XYZ最多{MAX_XYZ_POINTS:,}点，不自动降采样")
    if len(rows) < 3:
        _error("TERRAIN_DATA", "XYZ至少需要三个坐标节点，缺测节点也保留以阻止跨缺测插值")
    points = np.array(rows, dtype=float)
    missing = int(np.count_nonzero(~np.isfinite(points[:, 2])))
    if missing:
        warnings.append({"code": "TERRAIN_SOURCE_INVALID_DEPTH", "severity": "warning",
                         "message": f"{missing}个源节点为声明缺测或转换后不在0～1,000,000m内；保留缺测，不删点跨越"})
    return points, warnings


def normalize_sources(sources):
    """Return JSON-safe stable descriptors; validate declarations and data headers.

    File bytes are embedded and bounded. Full geometry/read errors remain hard
    errors at preparation; a corrupt source never quietly becomes a fallback.
    """
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        _error("TERRAIN_LIMIT", f"terrain_sources须为最多{MAX_SOURCES}个源的数组")
    if len(_json(sources)) > MAX_LIBRARY_JSON_BYTES:
        _error("TERRAIN_LIMIT", "完整地形源库JSON最多16MiB")
    result, total = [], 0
    common = {"schema", "schema_version", "id", "name", "kind", "enabled", "priority", "source_crs",
              "depth_positive", "depth_units", "vertical_datum", "sampling", "text", "data_base64",
              "coordinate_order", "nodata_value", "band", "content_sha256", "fingerprint", "byte_count"}
    for raw in sources:
        if not isinstance(raw, dict) or set(raw)-common:
            _error("TERRAIN_STRUCTURE", "每个源须为支持字段的对象，不能包含路径、URL或可执行配置")
        source = deepcopy(raw)
        if source.setdefault("schema", SCHEMA) != SCHEMA:
            _error("TERRAIN_SCHEMA", "不支持的terrain source schema/version")
        source["schema_version"] = _int(source.setdefault("schema_version", 1), "source.schema_version", 1, 1)
        kind = source.get("kind")
        if kind not in ("xyz", "geotiff", "surfer"):
            _error("TERRAIN_STRUCTURE", "kind须为xyz、geotiff或surfer")
        crs = _crs(source.get("source_crs")); source["source_crs"] = crs.to_string()
        if source.get("depth_positive") not in ("up", "down"):
            _error("TERRAIN_METADATA", "须明确depth_positive为up或down")
        unit = _text(source.get("depth_units"), "depth_units", 32).lower(); factor = length_factor(unit)
        source["depth_units"] = {1: "m", .3048: "ft", 1.8288: "fathom", 1000: "km"}[factor]
        source["vertical_datum"] = _text(source.get("vertical_datum"), "vertical_datum", 128)
        if source["vertical_datum"].casefold() in ("unknown", "unspecified", "user-unspecified", "未知", "未指定"):
            _error("TERRAIN_METADATA", "须声明明确的垂直基准名称；未知基准不能证明多源一致")
        if not isinstance(source.setdefault("enabled", True), bool):
            _error("TERRAIN_STRUCTURE", "enabled须为布尔值")
        source["priority"] = _int(source.setdefault("priority", 100), "priority", -1_000_000, 1_000_000)
        sampling = source.setdefault("sampling", {})
        if not isinstance(sampling, dict) or set(sampling)-({"method", "max_gap_m"} if kind == "xyz" else {"method"}):
            _error("TERRAIN_STRUCTURE", "sampling含不支持的字段")
        methods = {"xyz": ("linear", "idw"), "geotiff": ("nearest", "bilinear"), "surfer": ("linear", "nearest")}
        if sampling.setdefault("method", methods[kind][0]) not in methods[kind]:
            _error("TERRAIN_STRUCTURE", f"{kind}采样方式须为{methods[kind]}")
        if kind == "xyz":
            if "data_base64" in source or "band" in source:
                _error("TERRAIN_STRUCTURE", "XYZ只接受text，不接受二进制band")
            if not isinstance(source.get("text"), str):
                _error("TERRAIN_STRUCTURE", "XYZ须有text")
            if source.setdefault("coordinate_order", "xy") not in ("xy", "yx"):
                _error("TERRAIN_STRUCTURE", "coordinate_order须为xy或yx，不能按表头猜测")
            source["nodata_value"] = None if source.get("nodata_value") is None else finite_number(source["nodata_value"], "nodata_value")
            sampling["max_gap_m"] = finite_number(sampling.setdefault("max_gap_m", 5000), "max_gap_m", minimum=1, maximum=1_000_000)
        else:
            if any(k in source for k in ("text", "coordinate_order", "nodata_value")) or not isinstance(source.get("data_base64"), str):
                _error("TERRAIN_STRUCTURE", "栅格源只接受data_base64，不接受XYZ列配置")
            if kind == "geotiff":
                source["band"] = _int(source.setdefault("band", 1), "band", 1, 64)
            elif "band" in source:
                _error("TERRAIN_STRUCTURE", "Surfer只有单一深度数据，不使用band")
        data = _bytes(source); size = len(data); total += size
        if not 0 < size <= MAX_SOURCE_BYTES or total > MAX_TOTAL_SOURCE_BYTES:
            _error("TERRAIN_LIMIT", "源库每源最多8MiB、总解码资料最多12MiB；大文件仍可用既有单源导入")
        content_hash = hashlib.sha256(data).hexdigest()
        if source.get("content_sha256", content_hash) != content_hash or source.get("byte_count", size) != size:
            _error("TERRAIN_INTEGRITY", "源内容与已有content_sha256/byte_count不一致")
        source["content_sha256"], source["byte_count"] = content_hash, size
        if kind != "xyz":
            source["data_base64"] = base64.b64encode(data).decode("ascii")
        identity = {k: source[k] for k in ("kind", "content_sha256", "source_crs", "depth_positive", "depth_units", "vertical_datum", "sampling")}
        identity.update({k: source[k] for k in ("coordinate_order", "nodata_value", "band") if k in source})
        fingerprint = _digest(identity)
        if source.get("fingerprint", fingerprint) != fingerprint:
            _error("TERRAIN_INTEGRITY", "源解释参数与已有fingerprint不一致；编辑解释时移除旧计算摘要后重新规范化")
        source["fingerprint"] = fingerprint
        source["id"] = _text(source.get("id", "terrain-"+fingerprint[:32]), "source.id", 128)
        source["name"] = _text(source.get("name", source["id"]), "source.name", 512)
        if kind == "xyz":
            _xyz_rows(source)
        elif kind == "geotiff":
            _raster_header(source, data)
        else:
            _surfer_dimensions(data)
        result.append(source)
    if len({s["id"] for s in result}) != len(result):
        _error("TERRAIN_DUPLICATE_ID", "地形源ID须唯一；相同内容需显式不同ID才可重复保存")
    if len(_json(result)) > MAX_LIBRARY_JSON_BYTES:
        _error("TERRAIN_LIMIT", "规范化后的源库JSON超过16MiB")
    return result


def _library_signature(normalized):
    return _digest([{k: s[k] for k in ("id", "fingerprint", "enabled", "priority")}
                    for s in sorted(normalized, key=lambda s: s["id"])])


def terrain_library_signature(sources):
    return _library_signature(normalize_sources(sources))


class _Budget:
    def __init__(self, maximum):
        self.maximum, self.used, self.cache_hits, self.cache_misses = maximum, 0, 0, 0

    def charge(self, units):
        units = int(units)
        if self.used+units > self.maximum:
            _error("TERRAIN_WORK_BUDGET", f"地形逻辑预处理/查询预算不足：{self.used+units}>{self.maximum}；不返回伪完成结果")
        self.used += units


def _prepared_size(prepared):
    # Retained array accounting; excludes opaque PROJ/Qhull/Python overhead.
    arrays = [prepared["values"]]
    if prepared["kind"] == "xyz":
        tri = prepared["tri"]
        arrays += [prepared["xy"], tri.simplices, tri.neighbors, tri.transform, tri.convex_hull,
                   tri.coplanar, tri.vertex_to_simplex, prepared["tree"].indices]
    seen, total = set(), 0
    for array in arrays:
        if id(array) not in seen:
            total += array.nbytes
            seen.add(id(array))
    return total


def _prepare(source, budget):
    key = source["fingerprint"]
    # Charge the same conservative logical preparation allowance on cache hits,
    # so a reproducible request is not admitted only because another warmed it.
    kind = source["kind"]
    if kind == "xyz":
        count = sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in source["text"].splitlines())
        budget.charge(count*(32+math.ceil(math.log2(max(count, 2)))))
        if count*512 > MAX_PREPARED_SOURCE_BYTES:
            _error("TERRAIN_MEMORY_BUDGET", "XYZ预处理保守数组预算超过128MiB")
    else:
        data = _bytes(source)
        nx, ny = _raster_header(source, data) if kind == "geotiff" else _surfer_dimensions(data)
        budget.charge(nx*ny*4)
    with _CACHE_LOCK:
        if key in _CACHE:
            value = _CACHE.pop(key); _CACHE[key] = value
            budget.cache_hits += 1
            return value
        budget.cache_misses += 1
        crs = _crs(source["source_crs"])
        factor = length_factor(source["depth_units"])*(1 if source["depth_positive"] == "down" else -1)
        warnings = []
        if kind == "xyz":
            raw, warnings = _xyz_rows(source)
            to_geo, geo_operation = _strict_transformer(crs, 4326)
            longitude, latitude = to_geo.transform(raw[:, 0], raw[:, 1])
            longitude, latitude = np.asarray(longitude), np.asarray(latitude)
            if not np.all(np.isfinite(longitude)&np.isfinite(latitude)&(np.abs(latitude)<=90)):
                _error("TERRAIN_CRS", "XYZ坐标无法转换到有效WGS84位置")
            radians = np.radians(longitude)
            mean_cos, mean_sin = float(np.mean(np.cos(radians))), float(np.mean(np.sin(radians)))
            if math.hypot(mean_cos, mean_sin) < 1e-6:
                _error("TERRAIN_XYZ_EXTENT", "XYZ跨越过大的经度范围，须按海区拆分")
            origin_lon, origin_lat = math.degrees(math.atan2(mean_sin, mean_cos)), float(np.mean(latitude))
            local = CRS.from_proj4(f"+proj=aeqd +lat_0={origin_lat} +lon_0={origin_lon} +datum=WGS84 +units=m +no_defs")
            converter, local_operation = _strict_transformer(4326, local)
            x, y = converter.transform(longitude, latitude); xy = np.column_stack([x, y])
            if not np.all(np.isfinite(xy)) or np.max(np.linalg.norm(xy, axis=1)) > 2_000_000:
                _error("TERRAIN_XYZ_EXTENT", "XYZ仅支持距本源中心2000km内的独立局部插值，请分海区")
            unique, indexes, back = np.unique(xy, axis=0, return_index=True, return_inverse=True)
            z = raw[:, 2]
            # Group reductions are linear; repeated survey coordinates must not
            # trigger an O(number_of_groups * number_of_points) scan.
            known = np.isfinite(z); counts = np.bincount(back)
            valid_counts = np.bincount(back, weights=known, minlength=len(unique))
            low, high = np.full(len(unique), np.inf), np.full(len(unique), -np.inf)
            np.minimum.at(low, back[known], z[known]); np.maximum.at(high, back[known], z[known])
            conflict = (counts > 1)&(((valid_counts > 0)&(valid_counts < counts)) | (high-low > 1e-8))
            if np.any(conflict):
                _error("TERRAIN_XYZ_DUPLICATE", "同一XYZ坐标含冲突水深或有效/缺测冲突，不自动选择")
            xy, values = unique, z[indexes]
            try:
                tri = Delaunay(xy)
            except QhullError:
                _error("TERRAIN_XYZ_GEOMETRY", "XYZ坐标不足三个不共线节点，无法建立明确凸包与插值域")
            prepared = {"kind": kind, "values": values, "xy": xy, "tri": tri, "tree": cKDTree(xy),
                        "converter": converter, "local_crs": local.to_string(), "warnings": warnings,
                        "horizontal_operations": [geo_operation, local_operation]}
        elif kind == "surfer":
            grid = read_grid(data)
            if grid["warnings"] and source["sampling"]["method"] == "linear" and any("断层" in row for row in grid["warnings"]):
                _error("TERRAIN_SURFER_FAULT", "含断层Surfer源须选nearest，不使用跨断层双线性推断")
            converter, operation = _strict_transformer(4326, crs)
            prepared = {"kind": kind, "values": grid["values"]*factor, "grid": grid,
                        "converter": converter, "horizontal_operations": [operation],
                        "geographic": crs.is_geographic,
                        "center_x": grid["xlo"]+(grid["nx"]-1)*grid["dx"]/2,
                        "warnings": [{"code": "SURFER_SECTION_LIMIT", "severity": "warning", "message": row} for row in grid["warnings"]]}
            prepared["grid"] = {**grid, "values": prepared["values"]}
        else:
            MemoryFile = _rasterio()
            try:
                with MemoryFile(data) as memory, memory.open() as raster:
                    masked = raster.read(source["band"], masked=True).astype("float64")
                    values = masked.filled(np.nan)*factor
                    affine = raster.transform
                    center_x = affine.a*raster.width/2+affine.b*raster.height/2+affine.c
                    converter, operation = _strict_transformer(4326, crs)
                    prepared = {"kind": kind, "values": values, "inverse_affine": ~raster.transform,
                                "converter": converter, "horizontal_operations": [operation],
                                "geographic": crs.is_geographic, "center_x": center_x, "warnings": []}
            except TerrainSourceError:
                raise
            except Exception as exc:
                _error("TERRAIN_DATA", f"无法读取GeoTIFF深度波段：{exc}")
        size = _prepared_size(prepared); prepared["estimated_array_bytes"] = size
        if size > MAX_PREPARED_SOURCE_BYTES:
            _error("TERRAIN_MEMORY_BUDGET", "源预处理保留数组超过128MiB，不自动抽稀")
        if size <= MAX_CACHE_BYTES:
            while _CACHE and (len(_CACHE) >= MAX_CACHE_ENTRIES or sum(p["estimated_array_bytes"] for p in _CACHE.values())+size > MAX_CACHE_BYTES):
                _CACHE.popitem(last=False)
            _CACHE[key] = prepared
        return prepared


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


def cache_info():
    with _CACHE_LOCK:
        return {"entries": len(_CACHE), "estimated_array_bytes": sum(p["estimated_array_bytes"] for p in _CACHE.values()),
                "max_entries": MAX_CACHE_ENTRIES, "max_estimated_array_bytes": MAX_CACHE_BYTES}


def _query_one(source, prepared, longitude, latitude, budget):
    count = len(longitude); budget.charge(count*(32 if source["kind"] == "xyz" else 8))
    x, y = prepared["converter"].transform(longitude, latitude)
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    finite = np.isfinite(x)&np.isfinite(y)
    numbers = np.full(count, np.nan); statuses = np.full(count, "transform_unavailable", dtype=object)
    if source["kind"] == "xyz":
        queries = np.column_stack([x[finite], y[finite]])
        indexes = np.flatnonzero(finite)
        if len(indexes):
            tri = prepared["tri"]; simplices = tri.find_simplex(queries)
            nearest, _ = prepared["tree"].query(queries)
            statuses[indexes] = "outside_convex_hull"
            inside = simplices >= 0
            statuses[indexes[inside]] = "gap_exceeded"
            valid = inside & (nearest <= source["sampling"]["max_gap_m"])
            ii, qq, ss = indexes[valid], queries[valid], simplices[valid]
            statuses[ii] = "nodata"
            if len(ii) and source["sampling"]["method"] == "linear":
                bary = np.einsum("ijk,ik->ij", tri.transform[ss, :2], qq-tri.transform[ss, 2])
                weights = np.column_stack([bary, 1-bary.sum(axis=1)])
                values = prepared["values"][tri.simplices[ss]]
                missing = np.any((~np.isfinite(values)) & (weights > 1e-12), axis=1)
                estimate = np.sum(np.where(np.isfinite(values), values, 0)*weights, axis=1)
                numbers[ii[~missing]] = estimate[~missing]
            elif len(ii):
                distances, neighbors = prepared["tree"].query(qq, k=min(8, len(prepared["values"])))
                values = prepared["values"][neighbors]
                weights = 1/np.maximum(distances, 1e-6)**2
                # Exactly sampled valid points do not inherit remote NoData.
                exact = distances[:, 0] <= 1e-7
                estimate = np.sum(np.where(np.isfinite(values), values, 0)*weights, axis=1)/weights.sum(axis=1)
                good = np.all(np.isfinite(values), axis=1)
                estimate[exact] = values[exact, 0]; good[exact] = np.isfinite(values[exact, 0])
                numbers[ii[good]] = estimate[good]
    else:
        if prepared["geographic"]:
            x = (x-prepared["center_x"]+180)%360-180+prepared["center_x"]
        for index in np.flatnonzero(finite):
            if source["kind"] == "surfer":
                grid = prepared["grid"]
                fx, fy = (x[index]-grid["xlo"])/grid["dx"], (y[index]-grid["ylo"])/grid["dy"]
                if not -1e-9 <= fx <= grid["nx"]-1+1e-9 or not -1e-9 <= fy <= grid["ny"]-1+1e-9:
                    statuses[index] = "outside_coverage"
                    continue
                value = _surfer_sample(grid, x[index], y[index], source["sampling"]["method"])
            else:
                affine = prepared["inverse_affine"]
                column = affine.a*x[index]+affine.b*y[index]+affine.c
                row = affine.d*x[index]+affine.e*y[index]+affine.f
                height, width = prepared["values"].shape
                if source["sampling"]["method"] == "nearest":
                    if not 0 <= column < width or not 0 <= row < height:
                        statuses[index] = "outside_coverage"
                        continue
                    value = prepared["values"][int(math.floor(row)), int(math.floor(column))]
                else:
                    fx, fy = column-.5, row-.5
                    if not -1e-9 <= fx <= width-1+1e-9 or not -1e-9 <= fy <= height-1+1e-9:
                        statuses[index] = "outside_coverage"
                        continue
                    fx, fy = min(width-1, max(0, fx)), min(height-1, max(0, fy))
                    ix, iy = min(width-1, int(fx)), min(height-1, int(fy))
                    ix1, iy1 = min(width-1, ix+1), min(height-1, iy+1)
                    tx, ty = fx-ix, fy-iy
                    corners = [(prepared["values"][iy, ix], (1-tx)*(1-ty)), (prepared["values"][iy, ix1], tx*(1-ty)),
                               (prepared["values"][iy1, ix], (1-tx)*ty), (prepared["values"][iy1, ix1], tx*ty)]
                    value = None if any(not math.isfinite(v) and w > 1e-12 for v, w in corners) else sum(v*w for v, w in corners if w > 1e-12)
            statuses[index] = "nodata"
            if value is not None:
                numbers[index] = value
    valid = np.isfinite(numbers)&(numbers >= 0)&(numbers <= 1_000_000)
    invalid = np.isfinite(numbers)&~valid
    statuses[invalid] = "depth_invalid"
    statuses[valid] = "valid"
    numbers[~valid] = np.nan
    return numbers, statuses


def _source_metadata(source):
    result = {key: source[key] for key in ("id", "name", "kind", "enabled", "priority", "source_crs", "vertical_datum", "sampling", "content_sha256", "fingerprint", "byte_count")}
    result.update(source_depth_positive=source["depth_positive"], source_depth_units=source["depth_units"])
    return result


def _config(config, profile=False):
    result = deepcopy({} if config is None else config)
    allowed = {"vertical_datum", "max_query_points", "max_work_units", "max_output_bytes"} | ({"spacing_m"} if profile else set())
    if not isinstance(result, dict) or set(result)-allowed:
        _error("TERRAIN_CONFIG", "查询config含不支持的字段")
    result["max_query_points"] = _int(result.get("max_query_points", MAX_QUERY_POINTS), "max_query_points", 1, MAX_QUERY_POINTS)
    result["max_work_units"] = _int(result.get("max_work_units", 30_000_000), "max_work_units", 1, 200_000_000)
    result["max_output_bytes"] = _int(result.get("max_output_bytes", 16*1024**2), "max_output_bytes", 1024, 64*1024**2)
    if "vertical_datum" in result:
        result["vertical_datum"] = _text(result["vertical_datum"], "vertical_datum", 128)
    if profile:
        result["spacing_m"] = finite_number(result.get("spacing_m", 1000), "spacing_m", minimum=1, maximum=100_000)
    return result


def _coordinates(points, maximum):
    if not isinstance(points, list) or not 1 <= len(points) <= maximum:
        _error("TERRAIN_LIMIT", f"查询点须为1～{maximum:,}个坐标，不能自动丢点")
    result = []
    for point in points:
        if isinstance(point, dict):
            if set(point)-{"longitude", "latitude", "kp_m", "id"}:
                _error("TERRAIN_QUERY", "点仅接受longitude/latitude和可选kp_m/id")
            longitude, latitude = coordinate(point.get("longitude"), point.get("latitude")); row = {"longitude": longitude, "latitude": latitude}
            if "kp_m" in point:row["kp_m"] = finite_number(point["kp_m"], "kp_m", minimum=0)
            if "id" in point:row["id"] = _text(point["id"], "point.id", 128)
        elif isinstance(point, list) and len(point) == 2:
            longitude, latitude = coordinate(*point); row = {"longitude": longitude, "latitude": latitude}
        else:
            _error("TERRAIN_QUERY", "查询点须为[longitude,latitude]或坐标对象")
        result.append(row)
    return result


def _query(normalized, points, config):
    enabled = sorted([s for s in normalized if s["enabled"]], key=lambda s: (-s["priority"], s["id"]))
    datums = {s["vertical_datum"] for s in enabled}
    selected = config.get("vertical_datum")
    if selected is None and len(datums) > 1:
        _error("TERRAIN_DATUM_CONFLICT", "多个启用源的垂直基准不一致；须显式选择vertical_datum，不能按优先级混合")
    if selected is None and datums:selected = next(iter(datums))
    excluded = [s["id"] for s in enabled if s["vertical_datum"] != selected]
    enabled = [s for s in enabled if s["vertical_datum"] == selected]
    budget = _Budget(config["max_work_units"]); warnings = []
    # Bound diagnostic allocation conservatively before any query result list.
    if len(points)*(400+len(enabled)*280) > config["max_output_bytes"]:
        _error("TERRAIN_OUTPUT_BUDGET", "逐点来源诊断保守输出预算不足；减少点数或明确增大max_output_bytes")
    samples = [{**p, "depth_m": None, "source_id": None, "source_fingerprint": None,
                "fallback": False, "fallback_count": 0, "attempts": []} for p in points]
    pending = np.arange(len(samples)); source_counts = Counter(); reason_counts = Counter(); operations = {}
    for source in enabled:
        if not len(pending):break
        prepared = _prepare(source, budget)
        operations[source["id"]] = deepcopy(prepared["horizontal_operations"])
        warnings.extend({**row, "source_id": source["id"]} for row in prepared["warnings"])
        longitude = np.array([samples[i]["longitude"] for i in pending]); latitude = np.array([samples[i]["latitude"] for i in pending])
        values, statuses = _query_one(source, prepared, longitude, latitude, budget)
        unresolved = []
        for index, value, status in zip(pending, values, statuses):
            row = samples[index]; status = str(status)
            row["attempts"].append({"source_id": source["id"], "status": status, "method": source["sampling"]["method"]})
            if status == "valid":
                row.update(depth_m=float(value), source_id=source["id"], source_fingerprint=source["fingerprint"],
                           fallback=len(row["attempts"]) > 1, fallback_count=len(row["attempts"])-1)
                source_counts[source["id"]] += 1
            else:
                reason_counts[status] += 1; unresolved.append(index)
        pending = np.array(unresolved, dtype=int)
    for index in pending:
        samples[index]["fallback_count"] = len(samples[index]["attempts"])
    if not enabled:
        warnings.append({"code": "TERRAIN_NO_ENABLED_SOURCE", "severity": "warning", "message": "所选垂直基准下没有启用源；全部水深保持缺测"})
    if excluded:
        warnings.append({"code": "TERRAIN_DATUM_FILTERED", "severity": "warning", "message": "明确选择基准后其他源被排除，没有执行垂直转换", "source_ids": excluded})
    if len(pending):
        warnings.append({"code": "TERRAIN_NODATA", "severity": "warning", "message": f"{len(pending)}/{len(samples)}点无有效水深；不外推、不补零"})
    quality = {"sample_count": len(samples), "valid_count": len(samples)-len(pending), "missing_count": len(pending),
               "fallback_count": sum(s["fallback"] for s in samples), "source_counts": dict(source_counts),
               "attempt_status_counts": dict(reason_counts), "vertical_datum": selected,
               "library_signature": _library_signature(normalized), "excluded_datum_source_ids": excluded,
               "complete": not len(pending)}
    result = {"model": MODEL, "validation_status": "research", "samples": samples, "quality": quality,
              "sources": [{**_source_metadata(s), "horizontal_operations": operations.get(s["id"], [])} for s in normalized], "warnings": warnings,
              "budget": {"work_units": budget.used, "max_work_units": budget.maximum, "max_query_points": config["max_query_points"],
                         "max_output_bytes": config["max_output_bytes"], "cache_hits": budget.cache_hits, "cache_misses": budget.cache_misses,
                         "cache": cache_info(), "work_basis": "conservative logical preparation allowance plus attempted point evaluations; not CPU FLOPs"},
              "assumptions": ["优先级仅表示用户选择，不是测量精度评级", "只混合明确同名垂直基准；不执行潮位/垂直基准转换",
                              "逐点插值和回退可能在来源边界产生深度跳变；剖面采样不证明点间海底连续",
                              "水平转换禁止ballpark且缺最佳网格时拒绝；整源选择一个操作，不按每点重新选区；PROJ操作精度不是测量精度",
                              "无测量精度/海试或原厂方法等效验证"]}
    if len(_json(result)) > config["max_output_bytes"]:
        _error("TERRAIN_OUTPUT_BUDGET", "实际逐点来源结果超过max_output_bytes，不截断诊断")
    return result


def query_terrain(sources, points, config=None):
    """Query a two-dimensional WGS84 point batch with genuine per-point provenance."""
    config = _config(config)
    normalized = normalize_sources(sources)
    return _query(normalized, _coordinates(points, config["max_query_points"]), config)


def _route_samples(project, config):
    if not isinstance(project, dict) or not isinstance(project.get("route"), dict):
        _error("TERRAIN_ROUTE", "路线采样需要schema1 route对象")
    route = project["route"]; points = route.get("points")
    if not isinstance(points, list) or not 2 <= len(points) <= 10_000:
        _error("TERRAIN_ROUTE", "路线须含2～10,000个点")
    curve = route.get("curve", "rhumb"); spacing = config["spacing_m"]
    segments, needed = [], 1
    # Legacy straight evaluation retains its previous admission/error contract.
    # Explicit arcs additionally obey a native numerical work counter.
    has_arc = any(isinstance(leg, dict) and leg.get("geometry") is not None for leg in route.get("legs", []))
    try:
        route_parts = route_segments(project, {"max_work_units": min(config["max_work_units"], 10_000_000)} if has_arc else None)
    except ValueError as error:
        if "max_work_units" in str(error):
            _error("TERRAIN_WORK_BUDGET", "真实圆弧几何求解工作超过max_work_units")
        raise
    for a, b, segment in zip(points, points[1:], route_parts):
        if not isinstance(a, dict) or not isinstance(b, dict):_error("TERRAIN_ROUTE", "每个路线点须为对象")
        distance = segment.length_m
        count = math.ceil(distance/spacing) if distance > 1e-9 else 0
        needed += count
        if needed > config["max_query_points"]:_error("TERRAIN_LIMIT", "路线采样超过max_query_points，请增大spacing_m，不自动删点")
        segments.append((a, b, distance, count, segment))
    first = points[0]; longitude, latitude = coordinate(first.get("longitude"), first.get("latitude"))
    samples, kp = [{"longitude": longitude, "latitude": latitude, "kp_m": 0.}], 0.
    for a, b, distance, count, segment in segments:
        for j in range(1, count+1):
            try:
                longitude, latitude = segment.point_at_fraction(j/count)
            except ValueError as error:
                if "max_work_units" in str(error):
                    _error("TERRAIN_WORK_BUDGET", "真实圆弧KP反解工作超过max_work_units，不采用角分数或端点弦回退")
                raise
            samples.append({"longitude": longitude, "latitude": latitude, "kp_m": kp+distance*j/count})
        kp += distance
    if kp <= 1e-9:_error("TERRAIN_ROUTE", "全部路线点重合，无正长度剖面")
    arc_work = sum(segment.solver["work_units"] for segment in route_parts if segment.is_arc)
    if arc_work >= config["max_work_units"]:
        _error("TERRAIN_WORK_BUDGET", "真实圆弧KP采样工作超过max_work_units，不按端点弦代替")
    return samples, arc_work


def profile_from_sources(project, config=None, *, sources=None):
    """Attach a real route profile; caller reviews/applies returned project explicitly."""
    from .core import route_signature
    config = _config(config, profile=True)
    normalized = normalize_sources(project.get("terrain_sources", []) if sources is None else sources)
    rows, arc_work = _route_samples(project, config)
    result = _query(normalized, rows, {**config, "max_work_units": config["max_work_units"]-arc_work})
    result["budget"].update(work_units=result["budget"]["work_units"]+arc_work,
                            max_work_units=config["max_work_units"], arc_geometry_work_units=arc_work)
    metadata = {"model": MODEL, "name": "共享多源地形", "terrain_library_signature": result["quality"]["library_signature"],
                "vertical_datum": result["quality"]["vertical_datum"], "depth_positive": "down", "units": "m",
                "spacing_m": config["spacing_m"], "priority_policy": "descending_priority_then_id", "sources": result["sources"],
                "source_counts": result["quality"]["source_counts"], "fallback_count": result["quality"]["fallback_count"],
                "missing_count": result["quality"]["missing_count"], "excluded_datum_source_ids": result["quality"]["excluded_datum_source_ids"],
                "query_budget": result["budget"],
                "route_geometry_policy": "explicit_circular_arc_integrated_KP_and_true_positions; otherwise route.curve"}
    profile = {"route_signature": route_signature(project), "source": metadata["name"], "metadata": metadata,
               "samples": [{key: row[key] for key in ("kp_m", "depth_m", "source_id", "source_fingerprint", "fallback", "fallback_count")} for row in result["samples"]]}
    candidate = deepcopy(project); candidate["terrain_sources"] = normalized; candidate["profile"] = profile
    return {**result, "project": candidate, "profile": profile}


def example_sources():
    """Reproducible explicitly synthetic fixture; no fabricated query output."""
    low = {"id": "example-background", "name": "合成背景XYZ，100m", "kind": "xyz", "enabled": True, "priority": 10,
           "source_crs": "EPSG:4326", "depth_positive": "down", "depth_units": "m", "vertical_datum": "synthetic-demo-datum",
           "text": "longitude latitude depth_m\n117.97 21.98 100\n118.03 21.98 100\n117.97 22.02 100\n118.03 22.02 100",
           "sampling": {"method": "linear", "max_gap_m": 10000}}
    grid = b"DSAA\n3 3\n117.99 118.01\n21.99 22.01\n300 300\n300 300 300\n300 1.70141e38 300\n300 300 300\n"
    high = {"id": "example-detail", "name": "合成详细Surfer，中心缺测", "kind": "surfer", "enabled": True, "priority": 100,
            "source_crs": "EPSG:4326", "depth_positive": "down", "depth_units": "m", "vertical_datum": "synthetic-demo-datum",
            "data_base64": base64.b64encode(grid).decode("ascii"), "sampling": {"method": "nearest"}}
    sources = normalize_sources([low, high])
    project = {"schema_version": 1, "id": "terrain-library-example", "name": "合成多源回退例子", "crs": "EPSG:4326", "terrain_sources": sources,
               "route": {"curve": "rhumb", "points": [{"id": "a", "longitude": 117.99, "latitude": 22},
                                                        {"id": "b", "longitude": 118.01, "latitude": 22}], "slack_pct": 1},
               "cable_types": [{"id": "example", "cost_per_m": 1}], "bodies": []}
    return {"sources": sources, "project": project, "points": [[117.99, 22], [118, 22], [118.02, 22], [119, 22]],
            "config": {"spacing_m": 200}, "source": "explicit_synthetic_example_not_field_data"}
