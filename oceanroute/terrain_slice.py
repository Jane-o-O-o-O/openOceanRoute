"""Profile a declared reference polyline against a complete positive-down GeoTIFF."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import math

import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point

from .geodesy import finite_number
from .terrain_boundaries import explicit_crs, read_bln, write_bln, boundary_from_tag, boundary_mask, _coordinates


def _integer(value, field, minimum, maximum):
    number = finite_number(value, field, minimum=minimum, maximum=maximum)
    if number != int(number):
        raise ValueError(f"{field} 必须为整数")
    return int(number)


def _line(config, raster_crs):
    if (config.get("line") is None) == (config.get("line_bln") is None):
        raise ValueError("必须且只能提供 line 或 line_bln")
    if config.get("line") is not None:
        value = config["line"]
        if not isinstance(value, dict):
            raise ValueError("line 必须为对象")
        source = explicit_crs(value.get("crs"), "line.crs")
        coordinates = _coordinates(value.get("coordinates"), "line.coordinates")
    else:
        document = read_bln(config["line_bln"], config.get("line_crs"))
        if len(document["objects"]) != 1 or document["objects"][0]["kind"] == "point":
            raise ValueError("line_bln 必须为一条至少两点的参考线")
        source = explicit_crs(document["crs"])
        coordinates = document["objects"][0]["coordinates"]
    if not 2 <= len(coordinates) <= 1000:
        raise ValueError("参考线须包含 2..1000 个顶点")
    xy = np.asarray(coordinates)[:, :2]
    if source.is_geographic and (np.any(np.abs(xy[:, 0]) > 180) or np.any(np.abs(xy[:, 1]) > 90)):
        raise ValueError("参考线经纬度超出范围")
    forward = Transformer.from_crs(source, raster_crs, always_xy=True)
    x, y = forward.transform(xy[:, 0], xy[:, 1])
    projected = np.column_stack((x, y))
    if not np.isfinite(projected).all():
        raise ValueError("参考线无法有效投影到栅格 CRS")
    lengths = np.linalg.norm(np.diff(projected, axis=0), axis=1)
    if np.any(lengths < 1e-7):
        raise ValueError("参考线相邻顶点不能重合")
    if lengths.sum() > 5_000_000:
        raise ValueError("参考线超过 5,000 km，请分海区计算")
    return projected, source.to_string()


def _intersection_positions(geometry, origin, vector, length):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [float(np.dot(np.asarray(geometry.coords[0]) - origin, vector) / length**2)]
    if geometry.geom_type in ("LineString", "LinearRing"):
        return [float(np.dot(np.asarray(p) - origin, vector) / length**2) for p in (geometry.coords[0], geometry.coords[-1])]
    if hasattr(geometry, "geoms"):
        return [position for part in geometry.geoms for position in _intersection_positions(part, origin, vector, length)]
    return []


def extract_dtm_slice(grid: dict, config: dict | None = None) -> dict:
    """Never read grid.preview; all elevations are sampled from the full raster.

    Stations include grid/stencil crossings and interval midpoints, so unknown
    cells and even sub-pixel declared exclusions cannot be bridged by a long
    sampling interval. Distances refer to the raster's projected XY plane.
    """
    config = {} if config is None else config
    if not isinstance(grid, dict) or not isinstance(config, dict):
        raise ValueError("grid 和 config 必须为对象")
    encoded = grid.get("geotiff_base64")
    if not isinstance(encoded, str) or not encoded or len(encoded) > 44_740_000:
        raise ValueError("必须提供不超过 32 MB 的完整 geotiff_base64，不接受 preview 代替")
    try:
        binary = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("geotiff_base64 无效") from exc
    if len(binary) > 32 * 1024 * 1024:
        raise ValueError("GeoTIFF 超过 32 MB")
    spacing = finite_number(config.get("spacing_m", 100), "spacing_m", minimum=1, maximum=100_000)
    limit = _integer(config.get("max_samples", 10_000), "max_samples", 2, 10_000)
    method = config.get("method", "bilinear")
    if method not in ("bilinear", "nearest"):
        raise ValueError("slice method 须为 bilinear 或 nearest")
    try:
        from rasterio.io import MemoryFile
    except ImportError as exc:
        raise ValueError("完整栅格剖面需要 rasterio：pip install '.[terrain]'") from exc
    try:
        memory = MemoryFile(binary)
        raster = memory.open()
    except Exception as exc:
        raise ValueError("geotiff_base64 不是可读取的栅格") from exc
    with memory, raster:
        if raster.driver != "GTiff" or raster.width * raster.height > 250_000 or raster.count < 1:
            raise ValueError("剖面须使用 GeoTIFF，最多 250,000 个栅格节点")
        if raster.crs is None:
            raise ValueError("栅格缺少明确 CRS")
        crs = CRS.from_user_input(raster.crs)
        if not crs.is_projected or not crs.axis_info or any(abs(axis.unit_conversion_factor - 1) > 1e-12 for axis in crs.axis_info[:2]):
            raise ValueError("栅格必须为以米计的水平投影 CRS")
        transform = raster.transform
        if abs(transform.b) > 1e-12 or abs(transform.d) > 1e-12 or transform.a <= 0 or transform.e >= 0:
            raise ValueError("剖面仅支持北向上、无旋转的规则栅格")
        tags = raster.tags()
        if tags.get("depth_positive") != "down" or raster.descriptions[0] != "depth_m_positive_down":
            raise ValueError("栅格须明确 band 1 为 depth_m_positive_down 并声明 depth_positive=down")
        boundary = boundary_from_tag(tags.get("boundary_projected_json", ""))
        array = raster.read(1, masked=True).astype(float).filled(np.nan)
        if np.any(array[np.isfinite(array)] < 0):
            raise ValueError("栅格包含负水深；不能将陆地高程当作海床")
        array[~np.isfinite(array)] = np.nan
        vertices, source_crs = _line(config, crs)
        width, height = raster.width, raster.height
        x0, y0 = transform.c + transform.a/2, transform.f + transform.e/2
        hx, hy = float(transform.a), float(-transform.e)
        points, stations, offset = [], [], 0.0
        for index, (a, b) in enumerate(zip(vertices, vertices[1:])):
            vector = b-a
            length = float(np.linalg.norm(vector))
            uniform = math.ceil(length/spacing)
            if uniform > limit:
                raise ValueError("slice max_samples 不足，请增大 spacing_m 或拆分参考线")
            fractions = set(np.linspace(0, 1, uniform+1).tolist())
            # Node-centre and nearest-cell boundaries are both breakpoints.
            for dimension, origin, increment, size in ((0, x0, hx, width), (1, y0, -hy, height)):
                if abs(vector[dimension]) > 1e-12:
                    edges = [(a[dimension]-origin)*2/increment, (b[dimension]-origin)*2/increment]
                    low, high = max(-1,math.ceil(min(edges))), min(2*size,math.floor(max(edges)))
                    if high-low-1 > limit:
                        raise ValueError("slice max_samples 不足以核对完整栅格缺测，请拆分参考线或显式增加预算")
                    if high >= low:
                        crossings = origin + np.arange(low,high+1)*increment/2
                        fractions.update(float(t) for t in (crossings-a[dimension])/vector[dimension] if 0 < t < 1)
            if boundary is not None:
                line = LineString([a, b])
                for polygon in (boundary["include"], boundary["exclude"]):
                    if polygon is not None:
                        fractions.update(t for t in _intersection_positions(line.intersection(polygon.boundary), a, vector, length) if 0 < t < 1)
            ordered = sorted(fractions)
            # Remove near-duplicate projection/edge coincidences.
            reduced = [ordered[0]]
            for fraction in ordered[1:]:
                if (fraction-reduced[-1])*length > min(1e-7,length*1e-10):
                    reduced.append(fraction)
                elif fraction == 1:
                    reduced[-1] = 1.0
            ordered = reduced
            if len(stations) + 2*len(ordered)-1-(1 if index else 0) > limit:
                raise ValueError("slice max_samples 不足以核对完整栅格缺测，请拆分参考线或显式增加预算")
            refined = sorted(ordered + [(u+v)/2 for u, v in zip(ordered, ordered[1:])])
            if index:
                refined = refined[1:]
            points.extend((a+t*vector).tolist() for t in refined)
            stations.extend(offset+t*length for t in refined)
            offset += length
        points = np.asarray(points)
        permitted = boundary_mask(boundary, points)
        def sample(point, permitted_here):
            if not permitted_here:
                return None
            fx, fy = (point[0]-x0)/hx, (y0-point[1])/hy
            if method == "nearest":
                ix, iy = math.floor(fx+.5), math.floor(fy+.5)
                if not 0 <= ix < width or not 0 <= iy < height:
                    return None
                value = array[iy, ix]
                return float(value) if math.isfinite(value) else None
            # Bilinear interpolation is defined over node centres, not beyond
            # the outermost centres. Zero-weight neighbours need not exist.
            eps = 1e-9
            if not -eps <= fx <= width-1+eps or not -eps <= fy <= height-1+eps:
                return None
            fx, fy = np.clip(fx, 0, width-1), np.clip(fy, 0, height-1)
            ix, iy = math.floor(fx), math.floor(fy)
            ax, ay = fx-ix, fy-iy
            result = 0.0
            for x, y, weight in ((ix, iy, (1-ax)*(1-ay)), (ix+1, iy, ax*(1-ay)), (ix, iy+1, (1-ax)*ay), (ix+1, iy+1, ax*ay)):
                if weight <= 1e-12:
                    continue
                if x >= width or y >= height or not math.isfinite(array[y, x]):
                    return None
                result += weight*array[y, x]
            return float(result)
        depths = [sample(point, valid) for point, valid in zip(points, permitted)]
        inverse = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        longitude, latitude = inverse.transform(points[:, 0], points[:, 1])
        if not np.isfinite(longitude).all() or not np.isfinite(latitude).all():
            raise ValueError("栅格坐标无法反投影到 WGS84")
        samples = [{"kp_m": float(kp), "x_m": float(point[0]), "y_m": float(point[1]), "longitude": float(lon), "latitude": float(lat),
                    "depth_m": depth, "slope_deg": None} for kp, point, lon, lat, depth in zip(stations, points, longitude, latitude, depths)]
        # The extra interval check prevents touching an exclusion from being
        # counted as known bottom length even if its endpoints are valid.
        connected = []
        for i in range(len(samples)-1):
            known = depths[i] is not None and depths[i+1] is not None
            if known and boundary is not None:
                line = LineString(points[i:i+2])
                known = (boundary["include"] is None or boundary["include"].covers(line)) and (boundary["exclude"] is None or not boundary["exclude"].intersects(line))
            connected.append(bool(known))
        runs, run = [], []
        for i, item in enumerate(samples):
            if item["depth_m"] is None:
                if run:
                    runs.append(run)
                    run = []
            else:
                if run and not connected[i-1]:
                    runs.append(run)
                    run = []
                run.append(item)
        if run:
            runs.append(run)
        for run in runs:
            if len(run) > 1:
                kp = np.asarray([item["kp_m"] for item in run])
                depth = np.asarray([item["depth_m"] for item in run])
                slopes = np.degrees(np.arctan(np.gradient(depth, kp)))
                for item, slope in zip(run, slopes):
                    item["slope_deg"] = float(slope)
        known_length = sum(math.hypot(stations[i+1]-stations[i], depths[i+1]-depths[i]) for i, known in enumerate(connected) if known)
        all_known = all(depth is not None for depth in depths) and all(connected)
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        fields = ["kp_m", "x_m", "y_m", "longitude", "latitude", "depth_m", "slope_deg"]
        writer.writerow(fields)
        for item in samples:
            writer.writerow(["" if item[field] is None else format(item[field], ".17g") for field in fields])
        line_doc = {"schema":"oceanroute.bln.v1", "crs":crs.to_string(), "objects":[{"kind":"polygon" if np.array_equal(vertices[0], vertices[-1]) else "line", "flag":1,
                     "name":"reference-line", "coordinates":vertices.tolist()}]}
        slice_doc = {"schema":"oceanroute.bln.v1", "crs":crs.to_string(), "objects":[]}
        for i, run in enumerate(runs):
            coordinates = [[item["x_m"], item["y_m"], item["depth_m"]] for item in run]
            kind = "point" if len(run)==1 else "polygon" if coordinates[0][:2]==coordinates[-1][:2] else "line"
            slice_doc["objects"].append({"kind":kind,"flag":1,"name":f"valid-slice-{i+1}","coordinates":coordinates})
        warnings = []
        missing = sum(depth is None for depth in depths)
        convergence_tag = tags.get("solver_converged")
        converged = None if convergence_tag is None else convergence_tag == "true"
        if missing:
            warnings.append({"code":"DTM_SLICE_NODATA", "severity":"warning", "message":f"{missing}/{len(samples)} 个剖面点缺测；完整底长不可用，不跨缺测连接"})
        if converged is False:
            warnings.append({"code":"DTM_SOURCE_NOT_CONVERGED", "severity":"warning", "message":"源栅格为未收敛候选，剖面不能视为已验收地形"})
        elif converged is None:
            warnings.append({"code":"DTM_SOURCE_CONVERGENCE_UNSPECIFIED", "severity":"warning", "message":"源栅格未声明求解收敛状态，不能从文件内容推断收敛或测深精度"})
        return {"model":"full-raster-profile-v1", "validation_status":"research", "samples":samples,
                "profile":[{"kp_m":item["kp_m"],"depth_m":item["depth_m"]} for item in samples],
                "valid_segments":[{"start_kp_m":run[0]["kp_m"],"end_kp_m":run[-1]["kp_m"],"samples":run} for run in runs],
                "summary":{"sample_count":len(samples),"valid_count":len(samples)-missing,"missing_count":missing,
                           "horizontal_length_m":offset,"known_bottom_length_m":float(known_length),
                           "bottom_length_m":float(known_length) if all_known else None,"complete":all_known,"source_converged":converged},
                "metadata":{"crs":crs.to_string(),"line_source_crs":source_crs,"vertical_datum":tags.get("vertical_datum","user-unspecified"),
                            "method":method,"spacing_m":spacing,"raster_width":width,"raster_height":height,"grid_spacing_x_m":hx,"grid_spacing_y_m":hy,
                            "sampling":"maximum-spacing stations + node/cell/boundary crossings + interval midpoints","distance_basis":"straight segments in raster projected XY metres"},
                "csv_text":output.getvalue(),"reference_bln_text":write_bln(line_doc),
                "slice_bln_text":write_bln(slice_doc) if runs else "", "warnings":warnings,
                "assumptions":["全部水深来自完整 GeoTIFF 第一波段，预览数组不参与采样；缺测不补零，不外推。",
                               "参考线顶点转为栅格 CRS，段间按投影直线计算；KP 是局部投影距离，不能直接代替航路椭球里程。",
                               "bilinear 要求所有非零权重节点有效；nearest 为节点像素值，均检查原始裁剪多边形。",
                               "沿线坡度和底长来自采样折线；slice BLN 仅导出各有效段，不跨缺测连接，不声明工程测深精度。"]}
