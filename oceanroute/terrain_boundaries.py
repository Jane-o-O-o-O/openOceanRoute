"""Explicit-CRS BLN documents and validated terrain inclusion/exclusion polygons."""

from __future__ import annotations

import csv
import io
import json
import shlex

import numpy as np
from pyproj import CRS, Transformer
import shapely
from shapely.geometry import Polygon, shape, mapping
from shapely.ops import transform, unary_union
from shapely.validation import explain_validity

from .geodesy import finite_number

MAX_VERTICES = 10_000
MAX_TEXT_BYTES = 2 * 1024 * 1024


def explicit_crs(value, field="crs") -> CRS:
    if not isinstance(value, str) or not value.strip() or len(value) > 10_000:
        raise ValueError(f"{field} 必须明确为 CRS 字符串")
    try:
        result = CRS.from_user_input(value)
    except Exception as exc:
        raise ValueError(f"{field} 无效") from exc
    if not (result.is_geographic or result.is_projected):
        raise ValueError(f"{field} 必须为水平地理或投影坐标系")
    return result


def _coordinates(rows, field):
    if not isinstance(rows, list) or not rows or len(rows) > MAX_VERTICES:
        raise ValueError(f"{field} 必须为 1..{MAX_VERTICES} 个坐标")
    output = []
    size = None
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) not in (2, 3):
            raise ValueError(f"{field} 每点必须为 XY 或 XYZ")
        if size is None:
            size = len(row)
        if len(row) != size:
            raise ValueError(f"{field} 不得混合 XY 与 XYZ")
        output.append([finite_number(v, field) for v in row])
    return output


def _simple_polygon(coords, field):
    if len(coords) < 4 or coords[0][:2] != coords[-1][:2]:
        raise ValueError(f"{field} 多边形须显式闭合，至少四点")
    polygon = Polygon([p[:2] for p in coords])
    if not polygon.is_valid or polygon.area <= 0:
        raise ValueError(f"{field} 无效简单多边形：{explain_validity(polygon)}；不支持自接触 compound BLN")
    return polygon


def read_bln(text: str, crs: str) -> dict:
    """Read Golden Software XY/XYZ BLN; flag 0 includes, flag 1 excludes."""
    reference = explicit_crs(crs)
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("BLN 文本须为不超过 2 MB 的字符串")
    lines = [line.strip() for line in text.lstrip("\ufeff").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    objects, position, total = [], 0, 0
    while position < len(lines):
        header = lines[position]
        cells = next(csv.reader([header], skipinitialspace=True)) if "," in header else shlex.split(header)
        position += 1
        if not 1 <= len(cells) <= 3:
            raise ValueError("BLN 首行应为点数,flag[,名称]")
        try:
            count = int(cells[0])
            if str(count) != cells[0].strip() or count < 1 or count > MAX_VERTICES:
                raise ValueError()
            flag = int(cells[1]) if len(cells) >= 2 else None
            if flag not in (None, 0, 1) or (len(cells) >= 2 and str(flag) != cells[1].strip()):
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise ValueError("BLN 点数必须为正整数，flag 只能为 0 或 1") from exc
        if position + count > len(lines):
            raise ValueError("BLN 坐标行数不足")
        total += count
        if total > MAX_VERTICES:
            raise ValueError(f"BLN 总顶点超过 {MAX_VERTICES}")
        coords = []
        for line in lines[position:position + count]:
            values = next(csv.reader([line], skipinitialspace=True)) if "," in line else line.split()
            try:
                coords.append([float(v) for v in values])
            except ValueError as exc:
                raise ValueError("BLN 坐标必须为有限数字") from exc
        coords = _coordinates(coords, "BLN.coordinates")
        if reference.is_geographic and any(abs(p[0])>180 or abs(p[1])>90 for p in coords):
            raise ValueError("BLN 地理坐标超出经纬度范围")
        position += count
        kind = "point" if count == 1 else "polygon" if coords[0][:2] == coords[-1][:2] else "line"
        if kind == "polygon":
            _simple_polygon(coords, "BLN")
            if flag is None:
                raise ValueError("BLN 多边形必须明确 flag 0 或 1")
        objects.append({"name": cells[2] if len(cells) == 3 else "", "kind": kind, "flag": flag, "coordinates": coords})
    if not objects:
        raise ValueError("BLN 文件为空")
    return {"schema": "oceanroute.bln.v1", "crs": reference.to_string(), "objects": objects, "warnings": []}


def write_bln(document: dict) -> str:
    if not isinstance(document, dict) or document.get("schema") != "oceanroute.bln.v1":
        raise ValueError("BLN document.schema 必须为 oceanroute.bln.v1")
    reference = explicit_crs(document.get("crs"))
    objects = document.get("objects")
    if not isinstance(objects, list) or not objects:
        raise ValueError("BLN objects 不得为空")
    stream, total = io.StringIO(), 0
    writer = csv.writer(stream, lineterminator="\n")
    for obj in objects:
        if not isinstance(obj, dict):
            raise ValueError("BLN object 必须为对象")
        coords = _coordinates(obj.get("coordinates"), "BLN.coordinates")
        if reference.is_geographic and any(abs(p[0])>180 or abs(p[1])>90 for p in coords):
            raise ValueError("BLN 地理坐标超出经纬度范围")
        total += len(coords)
        if total > MAX_VERTICES:
            raise ValueError(f"BLN 总顶点超过 {MAX_VERTICES}")
        kind = "point" if len(coords) == 1 else "polygon" if coords[0][:2] == coords[-1][:2] else "line"
        if obj.get("kind", kind) != kind:
            raise ValueError("BLN kind 与坐标闭合状态不符")
        flag = obj.get("flag")
        if flag is not None and (type(flag) is not int or flag not in (0, 1)):
            raise ValueError("BLN flag 只能为 0 或 1")
        if kind == "polygon":
            _simple_polygon(coords, "BLN")
            if flag is None:
                raise ValueError("BLN 多边形必须明确 flag")
        name = obj.get("name", "")
        if not isinstance(name, str) or len(name) > 1000 or "\n" in name or "\r" in name:
            raise ValueError("BLN name 无效")
        if name and flag is None:
            raise ValueError("带名称的 BLN 对象必须明确 flag")
        writer.writerow([len(coords)] + ([] if flag is None else [flag]) + ([name] if name else []))
        for row in coords:
            writer.writerow([format(v, ".17g") for v in row])
    text = stream.getvalue()
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("BLN 文本超过 2 MB")
    return text


def _geojson_polygons(value):
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("boundary_geojson 必须是有限 JSON") from exc
    if len(encoded) > MAX_TEXT_BYTES or not isinstance(value, dict):
        raise ValueError("boundary_geojson 超出 2 MB 或类型无效")
    kind = value.get("type")
    features = value.get("features") if kind == "FeatureCollection" else [value] if kind == "Feature" else [{"type": "Feature", "geometry": value, "properties": {}}]
    if not isinstance(features, list) or not features:
        raise ValueError("boundary_geojson features 不得为空")
    result, total = [], 0
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("边界须为 GeoJSON Feature")
        properties = feature.get("properties") or {}
        if not isinstance(properties, dict):
            raise ValueError("边界 properties 须为对象")
        role = properties.get("role", "include")
        if role not in ("include", "exclude"):
            raise ValueError("boundary properties.role 须为 include 或 exclude")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("boundary_geojson 仅支持 Polygon/MultiPolygon")
        polygons = geometry.get("coordinates") if geometry["type"] == "MultiPolygon" else [geometry.get("coordinates")]
        if not isinstance(polygons, list) or not polygons:
            raise ValueError("GeoJSON 多边形坐标为空")
        for rings in polygons:
            if not isinstance(rings, list) or not rings:
                raise ValueError("GeoJSON rings 为空")
            checked = []
            for ring in rings:
                coords = _coordinates(ring, "boundary_geojson")
                total += len(coords)
                if total > MAX_VERTICES:
                    raise ValueError(f"边界总顶点超过 {MAX_VERTICES}")
                if any(len(p) != 2 or not -180 <= p[0] <= 180 or not -90 <= p[1] <= 90 for p in coords):
                    raise ValueError("GeoJSON 边界必须是 WGS84 二维经纬度")
                if any(abs(a[0] - b[0]) > 180 for a, b in zip(coords, coords[1:])):
                    raise ValueError("跨日期变更线的边界须先拆成不跨 180° 的多边形")
                _simple_polygon(coords, "boundary_geojson ring")
                checked.append(coords)
            polygon = Polygon(checked[0], checked[1:])
            if not polygon.is_valid or polygon.area <= 0:
                raise ValueError(f"GeoJSON 多边形或孔洞无效：{explain_validity(polygon)}")
            result.append((role, polygon))
    return result


def boundary_from_config(config: dict, target_crs) -> dict | None:
    has_json, has_bln = config.get("boundary_geojson") is not None, config.get("boundary_bln") is not None
    if has_json and has_bln:
        raise ValueError("boundary_geojson 与 boundary_bln 只能选一个")
    if not has_json and not has_bln:
        if config.get("boundary_crs") is not None:
            raise ValueError("boundary_crs 需要 boundary_bln")
        return None
    if has_json:
        if config.get("boundary_crs") is not None:
            raise ValueError("GeoJSON 使用 WGS84，不接受 boundary_crs 覆盖")
        source = CRS.from_epsg(4326)
        polygons = _geojson_polygons(config["boundary_geojson"])
        source_kind = "geojson"
    else:
        document = read_bln(config["boundary_bln"], config.get("boundary_crs"))
        source = explicit_crs(document["crs"])
        polygons = []
        for obj in document["objects"]:
            if obj["kind"] != "polygon":
                raise ValueError("裁剪 BLN 必须全为显式闭合多边形；参考线不能作为边界")
            polygons.append(("include" if obj["flag"] == 0 else "exclude", _simple_polygon(obj["coordinates"], "BLN")))
        source_kind = "bln"
    transformer = Transformer.from_crs(source, target_crs, always_xy=True)
    included, excluded = [], []
    for role, polygon in polygons:
        result = transform(transformer.transform, polygon)
        if not result.is_valid or not np.isfinite(np.asarray(result.bounds)).all():
            raise ValueError("边界不能有效转换到网格 CRS")
        (included if role == "include" else excluded).append(result)
    include = unary_union(included) if included else None
    exclude = unary_union(excluded) if excluded else None
    if include is not None:
        shapely.prepare(include)
    if exclude is not None:
        shapely.prepare(exclude)
    return {"include": include, "exclude": exclude, "source_kind": source_kind, "source_crs": source.to_string(),
            "include_count": len(included), "exclude_count": len(excluded)}


def boundary_mask(boundary: dict | None, xy: np.ndarray) -> np.ndarray:
    if boundary is None:
        return np.ones(len(xy), dtype=bool)
    points = shapely.points(xy)
    mask = np.ones(len(xy), dtype=bool)
    if boundary["include"] is not None:
        mask &= shapely.covers(boundary["include"], points)
    if boundary["exclude"] is not None:
        mask &= ~shapely.covers(boundary["exclude"], points)
    return mask


def boundary_geometry(boundary: dict | None):
    """Finite clipping geometry when inclusion exists; excludes alone handled separately."""
    if boundary is None or boundary["include"] is None:
        return None
    return boundary["include"].difference(boundary["exclude"]) if boundary["exclude"] is not None else boundary["include"]


def boundary_tag(boundary: dict | None) -> str:
    if boundary is None:
        return ""
    return json.dumps({"include": mapping(boundary["include"]) if boundary["include"] is not None else None,
                       "exclude": mapping(boundary["exclude"]) if boundary["exclude"] is not None else None}, separators=(",", ":"), allow_nan=False)


def boundary_from_tag(text: str) -> dict | None:
    if not text:
        return None
    if len(text) > 4 * MAX_TEXT_BYTES:
        raise ValueError("GeoTIFF boundary tag 超出工作预算")
    try:
        data = json.loads(text)
        output = {key: shape(data[key]) if data[key] is not None else None for key in ("include", "exclude")}
        for polygon in output.values():
            if polygon is not None and (polygon.geom_type not in ("Polygon", "MultiPolygon") or not polygon.is_valid or not np.isfinite(polygon.bounds).all() or shapely.get_num_coordinates(polygon) > MAX_VERTICES):
                raise ValueError()
            if polygon is not None:
                shapely.prepare(polygon)
        return output
    except Exception as exc:
        raise ValueError("GeoTIFF boundary tag 无效") from exc
