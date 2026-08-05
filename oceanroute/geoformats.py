"""Explicit standard GIS imports: KML geometry and projected Shapefile ZIP.

KML altitude is never inferred to be surveyed water depth. ZIP entries are read
in memory, never extracted. Missing Shapefile CRS requires user declaration.
"""
from __future__ import annotations

import codecs
import io
import json
import math
import struct
from pathlib import PurePosixPath
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile

from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from .exchange import import_geojson

MAX_BYTES = 128 * 1024 * 1024


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _child(node, tag):
    return next((v for v in node if _tag(v) == tag), None)


def _text(node, tag, default=""):
    child = _child(node, tag)
    return "".join(child.itertext()).strip() if child is not None else default


def _coordinates(node):
    text = _text(node, "coordinates")
    result = []
    for token in text.split():
        pieces = token.split(",")
        if len(pieces) not in (2, 3):
            raise ValueError("KML坐标须为longitude,latitude[,altitude]")
        try:
            values = list(map(float, pieces))
        except ValueError as exc:
            raise ValueError("KML坐标不是数值") from exc
        if not all(math.isfinite(v) for v in values) or abs(values[0]) > 180 or abs(values[1]) > 90:
            raise ValueError("KML坐标超出WGS84范围或不是有限数值")
        result.append(values)
        if len(result) > 200_000:
            raise ValueError("KML几何点数超过200,000")
    return result


def _geometry(node):
    tag = _tag(node)
    if tag == "Point":
        coords = _coordinates(node)
        if len(coords) != 1:
            raise ValueError("KML Point须有一个坐标")
        return {"type": "Point", "coordinates": coords[0]}
    if tag == "LineString":
        coords = _coordinates(node)
        if len(coords) < 2:
            raise ValueError("KML LineString至少两个坐标")
        return {"type": "LineString", "coordinates": coords}
    if tag == "Polygon":
        rings = []
        outer = _child(node, "outerBoundaryIs")
        if outer is None:
            raise ValueError("KML Polygon缺少外边界")
        boundaries = [outer, *[v for v in node if _tag(v) == "innerBoundaryIs"]]
        for boundary in boundaries:
            ring = _child(boundary, "LinearRing")
            coords = _coordinates(ring) if ring is not None else []
            if len(coords) < 4 or coords[0] != coords[-1]:
                raise ValueError("KML多边形环须至少4点且闭合")
            rings.append(coords)
        return {"type": "Polygon", "coordinates": rings}
    if tag == "MultiGeometry":
        geometries = [_geometry(v) for v in node if _tag(v) in {"Point", "LineString", "Polygon", "MultiGeometry"}]
        if not geometries:
            raise ValueError("KML MultiGeometry没有支持的几何")
        return {"type": "GeometryCollection", "geometries": geometries}
    raise ValueError(f"当前未支持KML几何 {tag}")


def import_kml(text: str, name="KML图层", kind="reference") -> dict:
    if len(text.encode("utf-8")) > 32 * 1024 * 1024:
        raise ValueError("KML超过32 MB")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("KML包含不支持的DTD/实体声明")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"KML XML语法错误：{exc}") from exc
    if _tag(root) not in {"kml", "Document", "Folder", "Placemark"}:
        raise ValueError("不是支持的KML文档")
    features, warnings, routes = [], [], []
    for node in root.iter():
        if _tag(node) in {"NetworkLink", "GroundOverlay", "ScreenOverlay", "Track"}:
            warnings.append(f"未导入{_tag(node)}；不会下载外部链接或把影像当测深")
        if _tag(node) != "Placemark":
            continue
        properties = {"name": _text(node, "name"), "description": _text(node, "description")}
        for child in node.iter():
            if _tag(child) == "Data" and child.get("name"):
                properties[child.get("name")] = _text(child, "value")
            elif _tag(child) == "SimpleData" and child.get("name"):
                properties[child.get("name")] = child.text or ""
        for child in node:
            if _tag(child) not in {"Point", "LineString", "Polygon", "MultiGeometry"}:
                continue
            geometry = _geometry(child)
            props = {**properties, "kml_altitude_mode": _text(child, "altitudeMode", "clampToGround")}
            features.append({"type": "Feature", "properties": props, "geometry": geometry})
            if geometry["type"] == "LineString":
                routes.append({"name": properties["name"] or name,
                               "points": [{"id": str(uuid4()), "label": f"P{i+1:02d}", "longitude": p[0], "latitude": p[1], "depth_m": None, "note": "KML路线，水深未转换"} for i, p in enumerate(geometry["coordinates"])]})
        if len(features) > 100_000:
            raise ValueError("KML要素超过100,000")
    if not features:
        raise ValueError("KML没有可导入的点、线或多边形")
    layer = import_geojson(json.dumps({"type": "FeatureCollection", "features": features}, allow_nan=False), name, kind)
    layer["source"] = {"format": "KML", "crs": "EPSG:4326", "altitude_is_depth": False}
    layer["warnings"] = [*dict.fromkeys(warnings), "KML高度保留为几何/元数据，不转换为海床水深"]
    layer["route_candidates"] = routes
    return layer


def import_shapefile_zip(data: bytes, *, name="Shapefile图层", kind="reference", crs_override="", encoding="", source_name="") -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError("Shapefile ZIP超过128 MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("Shapefile需要有效ZIP文件") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > 1000 or sum(v.file_size for v in infos) > MAX_BYTES:
            raise ValueError("ZIP解压规模超过128 MB或文件数超过1000")
        for item in infos:
            path = PurePosixPath(item.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or item.flag_bits & 1:
                raise ValueError("ZIP包含不支持的路径或加密文件")
        members = {v.filename.casefold(): v.filename for v in infos if not v.is_dir() and not v.filename.startswith("__MACOSX/")}
        if len(members) != len([v for v in infos if not v.is_dir() and not v.filename.startswith("__MACOSX/")]):
            raise ValueError("ZIP包含重复或大小写冲突文件名")
        candidates = [v for v in members if v.endswith(".shp")]
        if source_name:
            candidates = [v for v in candidates if v == source_name.casefold() or PurePosixPath(v).name == source_name.casefold()]
        if len(candidates) != 1:
            raise ValueError("ZIP须包含一个Shapefile，多个时用source_name指定；候选：" + ", ".join(candidates[:10]))
        stem = candidates[0][:-4]
        def read(ext):
            member = members.get(stem + ext)
            return archive.read(member) if member else None
        shp, dbf, shx, prj, cpg = (read(ext) for ext in (".shp", ".dbf", ".shx", ".prj", ".cpg"))
        if dbf is None:
            raise ValueError("ZIP缺少同名.dbf属性文件")
        if not crs_override and prj is None:
            raise ValueError("缺少.prj；须明确填写源坐标系，不能默认WGS84")
        try:
            crs = CRS.from_user_input(crs_override) if crs_override else CRS.from_wkt(prj.decode("utf-8-sig"))
        except Exception as exc:
            raise ValueError("Shapefile源坐标系无法识别") from exc
        selected_encoding = encoding or (cpg.decode("ascii", errors="strict").strip() if cpg else "utf-8")
        selected_encoding = {"65001": "utf-8", "936": "gbk"}.get(selected_encoding, selected_encoding)
        try:
            codecs.lookup(selected_encoding)
        except LookupError as exc:
            raise ValueError("无法识别DBF字符编码") from exc
        import shapefile
        reader = None
        try:
            args = {"shp": io.BytesIO(shp), "dbf": io.BytesIO(dbf), "encoding": selected_encoding}
            if shx is not None:
                args["shx"] = io.BytesIO(shx)
            reader = shapefile.Reader(**args)
            if reader.numRecords > 100_000:
                raise ValueError("Shapefile要素超过100,000")
            converter = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            features, null_count, point_count = [], 0, 0
            for record in reader.iterShapeRecords():
                if record.shape.shapeType == 0:
                    null_count += 1
                    continue
                point_count += len(record.shape.points)
                if point_count > 1_000_000:
                    raise ValueError("Shapefile总顶点超过1,000,000")
                geom = transform(converter.transform, shape(record.shape.__geo_interface__))
                properties = {key: value if isinstance(value, (str, int, float, bool, type(None))) else str(value) for key, value in record.record.as_dict().items()}
                features.append({"type": "Feature", "properties": properties, "geometry": mapping(geom)})
        except (ValueError, UnicodeError, shapefile.ShapefileException, KeyError, IndexError, OSError, struct.error) as exc:
            raise ValueError(f"Shapefile读取失败：{exc}") from exc
        finally:
            if reader is not None:
                reader.close()
        if not features:
            raise ValueError("Shapefile没有非空几何")
        layer = import_geojson(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, allow_nan=False), name, kind)
        layer["source"] = {"format": "Shapefile", "source_crs": crs.to_string(), "output_crs": "EPSG:4326", "encoding": selected_encoding, "file": members[candidates[0]]}
        layer["warnings"] = ([f"跳过{null_count}个空几何"] if null_count else []) + (["未提供.cpg，按UTF-8读取；若文字异常请声明DBF编码"] if cpg is None and not encoding else [])
        return layer
