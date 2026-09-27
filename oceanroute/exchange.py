"""Open engineering exchange formats. No proprietary Makai format claims."""

from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import html
import io
import json
import math
import re
import unicodedata
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring
from uuid import uuid4

from . import __version__


_EXPORT_EXTENSIONS = {
    "csv": "csv", "assembly": "csv", "kml": "kml", "geojson": "geojson",
    "dxf": "dxf", "sld": "svg", "report": "html", "project": "oceanroute.json",
    "workspace": "oceanroute.json", "automatic_rules": "json",
}


def _export_label(value: str, maximum_bytes: int = 90) -> str:
    """A readable, bounded filename component shared by every HTTP exporter."""
    text = unicodedata.normalize("NFC", str(value))
    text = "".join("-" if char in '<>:"/\\|?*' or unicodedata.category(char).startswith("C")
                   else char for char in text)
    text = re.sub(r"[-\s]+", "-", text).strip(" .-")
    bounded = []
    size = 0
    for char in text:
        length = len(char.encode("utf-8"))
        if size + length > maximum_bytes:
            break
        bounded.append(char)
        size += length
    return "".join(bounded).strip(" .-") or "未命名"


def export_filename(document: dict, format_name: str) -> str:
    """Identify full-workspace versus active-path exports without changing content."""
    if not isinstance(document, dict) or not isinstance(format_name, str) or format_name not in _EXPORT_EXTENSIONS:
        raise ValueError("Unsupported engineering export filename scope/format")
    name = _export_label(document.get("name") or "未命名")
    identity = str(document.get("id") or document.get("name") or "unnamed")
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    scope = "workspace" if format_name in {"workspace", "automatic_rules"} else "path"
    version = __version__.removesuffix(".0")
    return f"OceanRoute-{version}-{scope}-{name}-{token}-{format_name}.{_EXPORT_EXTENSIONS[format_name]}"


def export_content_disposition(document: dict, format_name: str) -> str:
    """RFC 5987 Unicode name plus an ASCII fallback, safe for HTTP headers."""
    filename = export_filename(document, format_name)
    token = hashlib.sha256(str(document.get("id") or document.get("name") or "unnamed").encode("utf-8")).hexdigest()[:16]
    scope = "workspace" if format_name in {"workspace", "automatic_rules"} else "path"
    fallback = f"OceanRoute-{__version__.removesuffix('.0')}-{scope}-{token}-{format_name}.{_EXPORT_EXTENSIONS[format_name]}"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def coordinate(value: str, *, latitude: bool) -> float:
    from .rpl_templates import parse_coordinate
    return parse_coordinate(value,latitude=latitude)


ALIASES = {
    "longitude": ["longitude", "longitude_decimal", "lon", "long", "lng", "经度"],
    "latitude": ["latitude", "latitude_decimal", "lat", "纬度"],
    "label": ["label", "name", "point", "point_name", "名称", "标签", "点名"],
    "depth_m": ["depth_m", "depth", "water_depth", "水深", "水深_m"],
    "note": ["note", "notes", "comment", "说明", "备注"],
    "kp_m": ["kp_m", "kp", "distance_m", "里程", "里程_m"],
    "cable_kp_m": ["cable_kp_m", "cumulative_cable_m", "cable_distance_m", "电缆里程_m"],
    "cable_type_id": ["cable_type_id", "cable_type", "缆型"],
    "slack_pct": ["slack_pct", "surface_slack_pct", "bottom_slack_pct", "余缆", "余缆_pct"],
    "slack_basis": ["slack_basis", "余缆基准"],
    "mode": ["mode", "缆长模式"],
    "fixed_cable_length_m": ["fixed_cable_length_m", "固定缆长_m"],
    "burial": ["burial", "埋设"],
    "stop_hours": ["stop_hours", "停时_h"],
    "extra_cost": ["extra_cost", "附加费"],
    "route_curve": ["route_curve"],
    "leg_geometry_json": ["leg_geometry_json"],
}


class _TableReader:
    """DictReader equivalent preserving original physical source positions."""
    def __init__(self,rows,fieldnames,delimiter,header_end):
        self.rows,self.fieldnames,self.delimiter=rows,fieldnames,delimiter
        self.header_end,self.line_num,self.record_start=header_end,header_end,header_end
    def __iter__(self): return self
    def __next__(self):
        row,start,end=next(self.rows)
        self.line_num,self.record_start=end,start
        result={field:(row[i] if i<len(row) else None) for i,field in enumerate(self.fieldnames)}
        if len(row)>len(self.fieldnames): result[None]=row[len(self.fieldnames):]
        return result


def _table(text: str, delimiter: str | None = None) -> tuple[_TableReader, dict[str, str]]:
    if not isinstance(text,str) or len(text.encode("utf-8")) > 32 * 1024 * 1024:
        raise ValueError("文本文件超过 32 MB，请分批导入")
    text = text.lstrip("\ufeff")
    if delimiter == "\\t":
        delimiter = "\t"
    if delimiter is None:
        first=next((line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
        try:
            delimiter = csv.Sniffer().sniff(first[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    if not isinstance(delimiter,str) or len(delimiter) != 1 or delimiter in "\r\n\0":
        raise ValueError("分隔符必须为单个字符")
    from .rpl_templates import iter_csv_records
    rows=iter_csv_records(text,delimiter)
    try: fieldnames,start,end=next(rows)
    except StopIteration: raise ValueError("文件为空") from None
    reader=_TableReader(rows,fieldnames,delimiter,end)
    headers = {str(c).strip().casefold(): c for c in fieldnames if c}
    if not headers:
        raise ValueError("缺少表头")
    if len(headers)!=len([c for c in fieldnames if c]):
        raise ValueError("表头名称重复，不能确定字段位置")
    return reader, headers


def _fields(headers: dict[str, str], mapping: dict | None) -> dict[str, str | None]:
    result = {}
    for field, aliases in ALIASES.items():
        specified = (mapping or {}).get(field)
        result[field] = headers.get(str(specified).strip().casefold()) if specified else next(
            (headers[a.casefold()] for a in aliases if a.casefold() in headers), None
        )
    return result


def import_rpl(text: str, delimiter: str | None = None, mapping: dict | None = None, template: dict | None = None,
               error_policy: str | None = None) -> dict:
    from .rpl_templates import parse_rpl, validate_template, SCHEMA
    if error_policy is not None and error_policy not in ("collect","skip","reject"):
        raise ValueError("error_policy须为collect、skip或reject")
    if template is not None:
        if delimiter is not None or mapping is not None:
            raise ValueError("template自带字段位置与分隔符，不能同时传delimiter/mapping")
        template=validate_template(template)
        if error_policy is not None and error_policy!=template["error_policy"]:
            raise ValueError("顶层error_policy与template.error_policy冲突")
        return parse_rpl(text,template)
    if mapping is not None and not isinstance(mapping,dict):
        raise ValueError("mapping须为字段到表头的JSON对象")
    reader, headers = _table(text, delimiter)
    fields = _fields(headers, mapping)
    if not fields["longitude"] or not fields["latitude"]:
        raise ValueError("找不到经度和纬度列；请使用 longitude、latitude 或设置列映射")
    generated={"schema":SCHEMA,"schema_version":1,"name":"CSV/TSV表头映射","format":"delimited","index_base":0,
        "header_lines":reader.header_end,"delimiter":reader.delimiter,"error_policy":error_policy or "skip",
        "fields":{key:{"column":reader.fieldnames.index(column)} for key,column in fields.items() if column},"defaults":{}}
    if fields.get("slack_pct") and str(fields["slack_pct"]).strip().casefold()=="bottom_slack_pct":
        generated["defaults"]["slack_basis"]="bottom"
    result=parse_rpl(text,generated,expected_columns=len(reader.fieldnames))
    if len(result["points"])<2 and error_policy is None:
        raise ValueError("至少需要两个有效路由点"+(f"；首个问题：{result['warnings'][0]['message']}" if result["warnings"] else ""))
    result["metadata"]["legacy_header_mapping"]=True
    return result


def import_profile(text: str, delimiter: str | None = None) -> dict:
    reader, headers = _table(text, delimiter)
    fields = _fields(headers, None)
    if not fields["kp_m"] or not fields["depth_m"]:
        raise ValueError("剖面文本需包含 kp_m 和 depth_m 表头；两列单位均为米")
    samples, warnings = [], []
    previous = -math.inf
    for index, row in enumerate(reader, 2):
        try:
            kp = float(row[fields["kp_m"]])
            depth_raw = row.get(fields["depth_m"])
            depth = float(depth_raw) if depth_raw and depth_raw.strip() else None
            if not math.isfinite(kp) or kp < 0 or kp <= previous:
                raise ValueError("KP 必须为严格递增的非负有限数值")
            if depth is not None and (not math.isfinite(depth) or depth < 0):
                raise ValueError("水深须为非负米制数值；缺测留空")
            samples.append({"kp_m": kp, "depth_m": depth})
            previous = kp
        except (ValueError, TypeError, KeyError) as exc:
            warnings.append({"row": index, "message": str(exc)})
    if len(samples) < 2:
        raise ValueError("至少需要两个有效剖面采样点")
    return {"samples": samples, "warnings": warnings, "accepted_rows": len(samples), "rejected_rows": len(warnings)}


def import_geojson(text: str, name: str = "导入图层", kind: str = "survey") -> dict:
    if len(text.encode("utf-8")) > 32 * 1024 * 1024:
        raise ValueError("GeoJSON 超过 32 MB")
    try:
        data = json.loads(text, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"无效数值 {x}")))
    except json.JSONDecodeError as exc:
        raise ValueError(f"GeoJSON 语法错误：第 {exc.lineno} 行") from exc
    if not isinstance(data, dict):
        raise ValueError("GeoJSON顶层须为对象")
    declared_crs = data.get("crs")
    if declared_crs:
        crs_name = str((declared_crs.get("properties", {}) if isinstance(declared_crs, dict) else {}).get("name", ""))
        if crs_name.upper() not in {"EPSG:4326", "URN:OGC:DEF:CRS:OGC:1.3:CRS84", "OGC:CRS84", "CRS84"}:
            raise ValueError("GeoJSON声明了非WGS84或无法识别坐标系，请先转换")
    if data.get("type") == "Feature":
        data = {"type": "FeatureCollection", "features": [data]}
    elif data.get("type") in {"Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection"}:
        data = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": data}]}
    if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise ValueError("需要 GeoJSON FeatureCollection、Feature 或 Geometry")
    if len(data["features"]) > 100_000:
        raise ValueError("图层要素超过 100,000 个")
    from shapely.geometry import shape
    from shapely.errors import GEOSException
    for i, feature in enumerate(data["features"]):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"要素 {i+1} 不是有效Feature对象")
        geometry = feature.get("geometry")
        if not geometry:
            continue
        try:
            geom = shape(geometry)
        except (ValueError, TypeError, KeyError, IndexError, GEOSException) as exc:
            raise ValueError(f"要素 {i+1} 几何结构无法解析") from exc
        if geom.is_empty:
            continue
        minx, miny, maxx, maxy = geom.bounds
        if not all(math.isfinite(x) for x in geom.bounds) or minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
            raise ValueError(f"要素 {i+1} 坐标超出经纬度范围；请先转换为 WGS84")
        if not geom.is_valid:
            raise ValueError(f"要素 {i+1} 几何无效，请修复后导入")
    return {"id": str(uuid4()), "name": name, "kind": kind, "visible": True, "geojson": data, "crs": "EPSG:4326"}


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(project: dict, analysis: dict) -> str:
    columns = ["index", "label", "longitude", "latitude", "depth_m", "kp_m", "bottom_kp_m", "cable_kp_m", "bearing_deg",
               "cable_type_id", "surface_slack_pct", "bottom_slack_pct", "note"]
    arcs = any(o.get("geometry") is not None for o in project["route"].get("legs", []))
    if arcs:
        columns += ["route_curve", "leg_geometry_json"]
    output = io.StringIO(newline="")
    output.write("# OceanRoute RPL; WGS84; metres; depth positive down; slack percent\n")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for index, row in enumerate(analysis["rpl"]):
        values = {k: _cell(row.get(k)) for k in columns}
        if arcs:
            values["route_curve"] = project["route"].get("curve", "rhumb")
            options = project["route"].get("legs", [])
            geometry = options[index].get("geometry") if index<len(options) else None
            values["leg_geometry_json"] = json.dumps(geometry, ensure_ascii=False, allow_nan=False, separators=(",", ":")) if geometry is not None else ""
        writer.writerow(values)
    return "\ufeff" + output.getvalue()


def export_geojson(project: dict, analysis: dict) -> str:
    points = project["route"]["points"]
    segments = analysis.get("route_geometry_segments", [])
    route_geometry = ({"type":"MultiLineString", "coordinates":segments} if len(segments)>1 else
                      {"type":"LineString", "coordinates":analysis.get("route_geometry", {}).get("coordinates") or [[p["longitude"],p["latitude"]] for p in points]})
    features = [{"type": "Feature", "properties": {"name": project.get("name"), "curve": project["route"].get("curve", "rhumb"),
                 "depth_units": "m", "distance_units": "m", "summary": analysis["summary"],
                 **({"leg_geometry": [o.get("geometry") for o in project["route"].get("legs", [])],
                     "geometry_model": analysis.get("model", {}).get("geometry"),
                     "geometry_render": analysis.get("route_geometry_render"),
                     "geometry_representation": "sampled_visualization_of_intrinsic_arcs; descriptor_in_JSON_is_authoritative"}
                    if any(o.get("geometry") is not None for o in project["route"].get("legs", [])) else {})},
                 "geometry": route_geometry}]
    for p, row in zip(points, analysis["rpl"]):
        features.append({"type": "Feature", "properties": row, "geometry": {"type": "Point", "coordinates": [p["longitude"], p["latitude"]]}})
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, allow_nan=False, indent=2)


def export_kml(project: dict, analysis: dict) -> str:
    root = Element("kml", {"xmlns": "http://www.opengis.net/kml/2.2"})
    doc = SubElement(root, "Document")
    SubElement(doc, "name").text = project.get("name", "OceanRoute")
    SubElement(doc, "description").text = "WGS84 route surface geometry. Depth is metadata, not KML altitude; survey datum is not converted to ellipsoid height."
    placemark = SubElement(doc, "Placemark")
    SubElement(placemark, "name").text = project["route"].get("name", "Route")
    geometry_points = analysis.get("route_geometry", {}).get("coordinates") or [[p["longitude"], p["latitude"]] for p in project["route"]["points"]]
    segments = analysis.get("route_geometry_segments") or [geometry_points]
    parent = SubElement(placemark,"MultiGeometry") if len(segments)>1 else placemark
    for segment in segments:
        line=SubElement(parent,"LineString")
        SubElement(line,"tessellate").text="1"
        SubElement(line,"altitudeMode").text="clampToGround"
        SubElement(line,"coordinates").text=" ".join(f"{p[0]},{p[1]},0" for p in segment)
    for row in analysis["rpl"]:
        point = SubElement(doc, "Placemark")
        SubElement(point, "name").text = str(row.get("label", ""))
        fields = SubElement(point, "ExtendedData")
        for key in ["kp_m", "depth_m", "cable_kp_m", "note"]:
            field = SubElement(fields, "Data", {"name": key})
            SubElement(field, "value").text = "" if row.get(key) is None else str(row[key])
        geometry = SubElement(point, "Point")
        SubElement(geometry, "coordinates").text = f"{row['longitude']},{row['latitude']},0"
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode")


def export_dxf(project: dict, analysis: dict) -> str:
    from pyproj import CRS, Transformer
    points = project["route"]["points"]
    lon = points[0]["longitude"]
    lat = points[0]["latitude"]
    # Local azimuthal equidistant preserves metre units at the project origin.
    local = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs")
    transform = Transformer.from_crs("EPSG:4326", local, always_xy=True)
    pairs = [(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1009"), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")]
    pairs.append((999, f"OceanRoute; local AEQD metres; origin lon={lon}, lat={lat}; geometry exchange only"))
    geometry_points = analysis.get("route_geometry", {}).get("coordinates") or [[p["longitude"], p["latitude"]] for p in points]
    xy = [transform.transform(p[0], p[1]) for p in geometry_points]
    if not all(math.isfinite(v) for pair in xy for v in pair):
        raise ValueError("路线超出当前 CAD 局部投影有效范围，请按海区分段导出")
    for a, b in zip(xy, xy[1:]):
        pairs += [(0, "LINE"), (8, "CABLE_ROUTE"), (10, a[0]), (20, a[1]), (30, 0), (11, b[0]), (21, b[1]), (31, 0)]
    for p in points:
        x, y = transform.transform(p["longitude"], p["latitude"])
        label = re.sub(r"[^\x20-\x7e]", "?", str(p.get("label", "")))
        pairs += [(0, "POINT"), (8, "ROUTE_POINTS"), (10, x), (20, y), (30, 0)]
        pairs += [(0, "TEXT"), (8, "LABELS"), (10, x), (20, y), (30, 0), (40, 20), (1, label)]
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    return "\n".join(f"{code}\n{value}" for code, value in pairs) + "\n"


def export_sld(project: dict, analysis: dict) -> str:
    items = analysis.get("sld", [])
    total = max(float(analysis["summary"]["cable_length_m"]), 1)
    palette = ["#06b6d4", "#f59e0b", "#8b5cf6", "#34d399"]
    ids = [c["id"] for c in project.get("cable_types", [])]
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="260" viewBox="0 0 1200 260">',
             '<rect width="1200" height="260" fill="#f3f7fa"/>',
             f'<text x="50" y="35" font-family="sans-serif" font-size="20" fill="#153448">{html.escape(project.get("name", "OceanRoute"))} · SLD</text>',
             '<line x1="50" y1="120" x2="1150" y2="120" stroke="#98b2c1" stroke-width="2"/>']
    for i, item in enumerate(items):
        start = float(item.get("start_m", item.get("cable_kp_m", 0)))
        end = float(item.get("end_m", start))
        x = 50 + 1100 * start / total
        if item.get("kind") == "cable":
            width = max(1100 * (end - start) / total, 1)
            color = palette[(ids.index(item.get("cable_type_id")) if item.get("cable_type_id") in ids else i) % len(palette)]
            parts.append(f'<rect x="{x:.2f}" y="103" width="{width:.2f}" height="34" rx="3" fill="{color}"/>')
            parts.append(f'<text x="{x+4:.2f}" y="92" font-size="12" font-family="sans-serif">{html.escape(str(item.get("name", "Cable")))}</text>')
        else:
            parts.append(f'<circle cx="{x:.2f}" cy="120" r="8" fill="#e43a5c"/>')
            parts.append(f'<text x="{x:.2f}" y="166" font-size="12" font-family="sans-serif">{html.escape(str(item.get("name", "Body")))}</text>')
    parts.append(f'<text x="50" y="215" font-family="sans-serif" font-size="12">Cable distance: 0 — {total/1000:.3f} km. Diagram is a linear assembly schematic.</text></svg>')
    return "\n".join(parts)


def export_report(project: dict, analysis: dict) -> str:
    escape = lambda x: html.escape(str(x))
    rows = "".join("<tr>" + "".join(f"<td>{escape(row.get(key) if row.get(key) is not None else '—')}</td>"
                     for key in ["index", "label", "longitude", "latitude", "depth_m", "kp_m", "cable_kp_m", "cable_type_id"])
                   + "</tr>" for row in analysis["rpl"])
    summary = "".join(f"<tr><th>{escape(k)}</th><td>{escape(v if v is not None else '未计算')}</td></tr>" for k, v in analysis["summary"].items())
    warnings = "".join(f"<li>{escape(w.get('message',w))}</li>" for w in analysis.get("warnings", []))
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{escape(project.get('name','工程'))}</title>
<style>body{{font:14px sans-serif;margin:40px;color:#173247}}table{{border-collapse:collapse;width:100%;margin:20px 0}}th,td{{border:1px solid #cedce4;padding:8px;text-align:left}}th{{background:#eef4f8}}h1{{color:#007c85}}@media print{{body{{margin:10mm;font-size:10px}}}}</style>
<h1>{escape(project.get('name','OceanRoute'))} · 路由规划报告</h1><p>OceanRoute {__version__.removesuffix(".0")} · WGS84 · 长度 m / 水深正向下 / 余缆 % · 路线模型 {escape(project['route'].get('curve','rhumb'))}</p>
<h2>规划汇总与假设</h2><table>{summary}</table><h2>校核信息</h2><ul>{warnings or '<li>当前规则未发现问题</li>'}</ul>
<h2>RPL</h2><table><thead><tr><th>序号</th><th>标签</th><th>经度</th><th>纬度</th><th>水深 m</th><th>表面 KP m</th><th>电缆里程 m</th><th>缆型</th></tr></thead><tbody>{rows}</tbody></table>
<h2>SLD</h2>{export_sld(project,analysis)}<p>报告表示输入数据与明确模型下的规划结果。测深来源、缺测信息及近似应结合校核信息解读。成本为所设单价和速度下的初步估算。</p></html>'''


def reverse_project(project: dict, analysis: dict | None = None) -> dict:
    """Compatibility entry point; engineering transformations live in tools."""
    from .tools import reverse_project as transform_project
    return transform_project(project)["project"]
