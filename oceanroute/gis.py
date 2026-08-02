"""Measured terrain sampling with explicit CRS and no-data provenance."""

from __future__ import annotations

from copy import deepcopy
import csv
import io
import math

import numpy as np
from pyproj import CRS, Transformer
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import cKDTree, QhullError

from .geodesy import coordinate, finite_number, inverse, interpolate
from .units import length_factor


def sample_route(project: dict, spacing_m: float = 1000) -> list[dict]:
    spacing = finite_number(spacing_m, "spacing_m", minimum=1, maximum=100_000)
    points = project["route"]["points"]
    curve = project["route"].get("curve", "rhumb")
    samples, kp = [], 0.0
    for index, (a, b) in enumerate(zip(points, points[1:])):
        distance, _ = inverse(a["longitude"], a["latitude"], b["longitude"], b["latitude"], curve)
        if distance < 1e-9:
            continue
        count = max(1, math.ceil(distance / spacing))
        if len(samples) + count > 50_000:
            raise ValueError("采样点超过 50,000 个，请加大采样间距或拆分路线")
        for j in range(count + 1):
            if index and j == 0:
                continue
            lon, lat = interpolate(a["longitude"], a["latitude"], b["longitude"], b["latitude"], j/count, curve)
            samples.append({"kp_m": kp + distance*j/count, "longitude": lon, "latitude": lat})
        kp += distance
    return samples


def _attach(project: dict, samples: list[dict], source: dict, warnings: list[dict]) -> dict:
    from .core import route_signature
    result = deepcopy(project)
    result["profile"] = {"samples": samples, "route_signature": route_signature(result), "source": source["name"], "metadata": source}
    missing = sum(s["depth_m"] is None for s in samples)
    if missing:
        warnings.append({"code": "TERRAIN_NODATA", "severity": "warning", "message": f"{missing}/{len(samples)} 个路线采样点没有有效水深，相关海底计算将不可用"})
    return {"project": result, "warnings": warnings, "quality": {"sample_count": len(samples), "missing_count": missing, "source": source}}


def import_xyz(text: str, *, depth_positive="down", depth_units="m") -> tuple[np.ndarray, list[dict]]:
    factor = length_factor(depth_units)
    if depth_positive not in ("up", "down"):
        raise ValueError("水深方向须明确为up或down")
    if len(text.encode("utf-8")) > 32 * 1024 * 1024:
        raise ValueError("XYZ 文本超过 32 MB，请分块处理")
    lines = [(i, line.strip()) for i, line in enumerate(text.lstrip("\ufeff").splitlines(), 1) if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise ValueError("XYZ 文件为空")
    header = lines[0][1].casefold()
    has_header = any(x in header for x in ("longitude", "latitude", "depth", "经度", "纬度", "水深"))
    points, warnings = [], []
    for index, line in lines[1:] if has_header else lines:
        cells = next(csv.reader([line])) if "," in line else line.replace(";", " ").split()
        try:
            if len(cells) != 3:
                raise ValueError("XYZ 必须为 longitude latitude depth_m 三列")
            lon, lat = coordinate(float(cells[0]), float(cells[1]))
            depth = finite_number(float(cells[2]), "source_depth") * factor * (-1 if depth_positive == "up" else 1)
            if depth < 0:
                raise ValueError("转换后水深为负；陆地高程不能当海床水深")
            points.append((lon, lat, depth))
        except (ValueError, TypeError) as exc:
            warnings.append({"row": index, "message": str(exc), "severity": "warning"})
        if len(points) > 200_000:
            raise ValueError("本次 XYZ 导入上限为 200,000 点，请分块处理")
    if len(points) < 3:
        raise ValueError("至少需要三个有效 XYZ 测深点")
    return np.asarray(points, dtype=float), warnings


def profile_from_xyz(project: dict, text: str, *, spacing_m=1000, max_gap_m=5000, method="linear", allow_extrapolation=False,
                     depth_positive="down", depth_units="m", vertical_datum="user-unspecified") -> dict:
    gap = finite_number(max_gap_m, "max_gap_m", minimum=1, maximum=1_000_000)
    samples = sample_route(project, spacing_m)
    points, warnings = import_xyz(text, depth_positive=depth_positive, depth_units=depth_units)
    first = project["route"]["points"][0]
    local = CRS.from_proj4(f"+proj=aeqd +lat_0={first['latitude']} +lon_0={first['longitude']} +datum=WGS84 +units=m +no_defs")
    transform = Transformer.from_crs("EPSG:4326", local, always_xy=True)
    x, y = transform.transform(points[:, 0], points[:, 1])
    xy = np.column_stack([x, y])
    sx, sy = transform.transform([s["longitude"] for s in samples], [s["latitude"] for s in samples])
    queries = np.column_stack([sx, sy])
    nearest, ids = cKDTree(xy).query(queries)
    if method == "linear":
        try:
            values = np.asarray(LinearNDInterpolator(xy, points[:, 2], fill_value=np.nan)(queries))
        except QhullError as exc:
            raise ValueError("测深点共线或几何退化，无法生成三角网；可显式选择 IDW") from exc
    elif method == "idw":
        tree = cKDTree(xy)
        distances, neighbors = tree.query(queries, k=min(8, len(points)))
        weights = 1.0 / np.maximum(distances, 1e-6) ** 2
        values = np.sum(weights * points[neighbors, 2], axis=1) / np.sum(weights, axis=1)
        if not allow_extrapolation:
            try:
                from scipy.spatial import Delaunay
                values[Delaunay(xy).find_simplex(queries) < 0] = np.nan
            except QhullError:
                raise ValueError("测深点共线；使用 IDW 外推需显式允许并核对适用性")
    else:
        raise ValueError("测深插值方式须为 linear 或 idw")
    values[nearest > gap] = np.nan
    output = [{"kp_m": float(s["kp_m"]), "depth_m": float(depth) if math.isfinite(depth) else None}
              for s, depth in zip(samples, values)]
    source = {"name": "用户 XYZ 测深", "crs": "EPSG:4326", "depth_positive": "down", "units": "m",
              "method": method, "spacing_m": spacing_m, "max_gap_m": gap, "allow_extrapolation": allow_extrapolation,
              "source_count": len(points), "horizontal_interpolation_crs": local.to_string(), "vertical_datum": vertical_datum,
              "source_depth_positive": depth_positive, "source_depth_units": depth_units}
    return _attach(project, output, source, warnings)


def profile_from_geotiff(project: dict, data: bytes, *, spacing_m=1000, depth_positive="down", vertical_datum="user-unspecified", depth_units="m") -> dict:
    factor = length_factor(depth_units)
    try:
        from rasterio.io import MemoryFile
    except ImportError as exc:
        raise ValueError("GeoTIFF 需要 terrain 可选依赖：pip install '.[terrain]'") from exc
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("当前单个栅格上限为 128 MB，请分块处理")
    if depth_positive not in ("up", "down"):
        raise ValueError("depth_positive 必须为 up 或 down")
    samples = sample_route(project, spacing_m)
    try:
        with MemoryFile(data) as memory:
            with memory.open() as raster:
                if not raster.crs:
                    raise ValueError("GeoTIFF 未声明 CRS")
                transform = Transformer.from_crs("EPSG:4326", raster.crs, always_xy=True)
                coords = [transform.transform(s["longitude"], s["latitude"]) for s in samples]
                values = list(raster.sample(coords, indexes=1, masked=True))
                output = []
                for sample, value, (x, y) in zip(samples, values, coords):
                    inside = raster.bounds.left <= x <= raster.bounds.right and raster.bounds.bottom <= y <= raster.bounds.top
                    number = float(value[0]) if inside and not np.ma.is_masked(value[0]) else float("nan")
                    if depth_positive == "up":
                        number = -number
                    number *= factor
                    output.append({"kp_m": sample["kp_m"], "depth_m": number if math.isfinite(number) and number >= 0 else None})
                source = {"name": "用户 GeoTIFF 测深", "crs": raster.crs.to_string(), "units": "m", "method": "nearest-pixel",
                          "depth_positive": depth_positive, "spacing_m": spacing_m, "vertical_datum": vertical_datum,
                          "resolution": list(raster.res), "nodata": raster.nodata if raster.nodata is None or math.isfinite(raster.nodata) else "NaN"}
                source["source_depth_units"] = depth_units
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取 GeoTIFF，请检查格式、文件完整性和坐标基准") from exc
    return _attach(project, output, source, [])
