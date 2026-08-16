"""Explicit horizontal CRS transformation previews, without project mutation."""
from __future__ import annotations

import math
import warnings as python_warnings

from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.exceptions import CRSError, ProjError
from pyproj.transformer import TransformerGroup

from .geodesy import finite_number

MAX_POINTS = 10_000


def _crs(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 16_384:
        raise ValueError(f"{field} 须明确提供不超过16384字符的EPSG/WKT/PROJ水平坐标系")
    try:
        result = CRS.from_user_input(value)
    except (CRSError, ValueError) as exc:
        raise ValueError(f"{field} 不是有效坐标系") from exc
    if result.is_compound or result.is_vertical or result.is_geocentric or not (result.is_projected or result.is_geographic) or len(result.axis_info) != 2:
        raise ValueError(f"{field} 只支持二维水平坐标系；不转换垂直/地心/复合或三维坐标")
    return result


def _xy_axes(crs):
    axes = list(crs.axis_info)
    # PROJ always_xy normalizes CRS latitude/northing-first axis declarations.
    if crs.is_geographic:
        return sorted(axes, key=lambda a: a.direction not in ("east", "west"))
    if axes[0].direction in ("north", "south") and axes[1].direction in ("east", "west"):
        return axes[::-1]
    return axes


def _metadata(crs, supplied):
    authority = crs.to_authority()
    units = []
    for axis in _xy_axes(crs):
        factor = float(axis.unit_conversion_factor)
        units.append({"name": axis.unit_name, "axis_name": axis.name,
                      "direction": axis.direction,
                      "conversion_to_m": factor if crs.is_projected else None,
                      "conversion_to_rad": factor if crs.is_geographic else None})
    return {"input": supplied, "authority": authority[0] if authority else None,
            "code": authority[1] if authority else None, "name": crs.name,
            "kind": "projected" if crs.is_projected else "geographic",
            "axis_units": units, "area_of_use": _area(crs.area_of_use)}


def _area(value):
    if value is None:
        return None
    return {"name": value.name, "west": value.west, "south": value.south,
            "east": value.east, "north": value.north}


def _inside(area, lon, lat):
    if area is None:
        return True
    longitude = (area["west"]-1e-8 <= lon <= area["east"]+1e-8 if area["west"] <= area["east"]
                 else lon >= area["west"]-1e-8 or lon <= area["east"]+1e-8)
    return longitude and area["south"]-1e-8 <= lat <= area["north"]+1e-8


def _geographic_xy(crs, x, y, inverse=None):
    if crs.is_geographic:
        gx, gy = x, y
        geodetic = crs
    else:
        geodetic = crs.geodetic_crs
        inverse = inverse or Transformer.from_crs(crs, geodetic, always_xy=True, allow_ballpark=False)
        gx, gy = inverse.transform(x, y, errcheck=True)
    axes = _xy_axes(geodetic)
    lon = gx * axes[0].unit_conversion_factor * 180/math.pi * (-1 if axes[0].direction == "west" else 1)
    lat = gy * axes[1].unit_conversion_factor * 180/math.pi * (-1 if axes[1].direction == "south" else 1)
    meridian = geodetic.prime_meridian
    lon += meridian.longitude * meridian.unit_conversion_factor * 180/math.pi
    lon = (lon+180) % 360-180
    if not math.isfinite(lon) or not math.isfinite(lat) or abs(lat) > 90+1e-8:
        raise ValueError("坐标无法定位到有效地理范围")
    return lon, max(-90., min(90., lat))


def _check_geographic(crs, x, y):
    if crs.is_geographic:
        axes = _xy_axes(crs)
        lon = x * axes[0].unit_conversion_factor * 180/math.pi
        lat = y * axes[1].unit_conversion_factor * 180/math.pi
        if abs(lon) > 180+1e-9 or abs(lat) > 90+1e-9:
            raise ValueError("经纬度超出声明角度单位对应的经度±180°/纬度±90°范围")


def _operation(source, target, location=None):
    area = None if location is None else AreaOfInterest(
        max(-180., location[0]-1e-7), max(-90., location[1]-1e-7),
        min(180., location[0]+1e-7), min(90., location[1]+1e-7))
    # Missing grids are an explicit error, rather than a silently selected datum
    # fallback. Suppress only PROJ's duplicate textual missing-grid warning.
    with python_warnings.catch_warnings():
        python_warnings.simplefilter("ignore", UserWarning)
        group = TransformerGroup(source, target, always_xy=True, allow_ballpark=False, area_of_interest=area)
    if not group.best_available:
        raise ValueError("最佳坐标操作所需的基准网格不可用；请安装所声明地区的PROJ网格后重试")
    if not group.transformers:
        raise ValueError("所声明区域没有可用的非ballpark水平坐标操作")
    transformer = group.transformers[0]
    return transformer, {"description": transformer.description,
                         "accuracy_m": transformer.accuracy if transformer.accuracy >= 0 else None,
                         "ballpark": False, "best_available": True,
                         "area_of_use": _area(transformer.area_of_use),
                         "network_enabled": transformer.is_network_enabled}


def _safe_input(row):
    if not isinstance(row, dict):
        return {"x": None, "y": None}
    result = {}
    for key in ("x", "y"):
        value = row.get(key)
        try:
            safe = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            safe = False
        result[key] = value if safe else None
    return result


def transform_coordinates(config):
    """Return an all-or-none applicable preview with per-point real operations.

    Input/output follow CRS-native horizontal units, always X/Y order. The
    operation accuracy belongs to PROJ, not to source measurement accuracy.
    """
    if not isinstance(config, dict) or set(config)-{"source_crs", "target_crs", "points", "error_policy"}:
        raise ValueError("坐标请求仅支持source_crs、target_crs、points和error_policy")
    source = _crs(config.get("source_crs"), "source_crs")
    target = _crs(config.get("target_crs"), "target_crs")
    policy = config.get("error_policy", "collect")
    if policy not in ("collect", "reject"):
        raise ValueError("error_policy须为collect或reject；不自动应用部分坏点")
    points = config.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise ValueError(f"points须为1..{MAX_POINTS}点数组")
    metadata_source = _metadata(source, config["source_crs"])
    metadata_target = _metadata(target, config["target_crs"])
    same_datum = source.datum == target.datum
    fixed = _operation(source, target) if same_datum else None
    geographic_inverse = (Transformer.from_crs(source, source.geodetic_crs, always_xy=True, allow_ballpark=False)
                          if source.is_projected else None)
    result, errors, notices, operations, ids = [], [], [], [], set()
    for index, point in enumerate(points):
        identifier = point.get("id") if isinstance(point, dict) else None
        row = {"index": index, "id": identifier if isinstance(identifier, str) else None,
               "input": _safe_input(point), "output": None, "accepted": False}
        try:
            if not isinstance(point, dict) or set(point)-{"id", "x", "y"}:
                raise ValueError("每点仅支持id、x、y，不转换高度或其他工程字段")
            if identifier is not None:
                if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128 or identifier in ids:
                    raise ValueError("点id须为不重复的1..128字符标识")
                ids.add(identifier)
            x = finite_number(point.get("x"), "x"); y = finite_number(point.get("y"), "y")
            _check_geographic(source, x, y)
            location = _geographic_xy(source, x, y, geographic_inverse)
            transformer, operation = fixed if fixed is not None else _operation(source, target, location)
            ox, oy = transformer.transform(x, y, errcheck=True)
            ox = finite_number(ox, "output.x"); oy = finite_number(oy, "output.y")
            _check_geographic(target, ox, oy)
            if operation not in operations:
                operations.append(operation)
            row.update(input={"x": x, "y": y}, output={"x": ox, "y": oy}, accepted=True,
                       operation_index=operations.index(operation))
            if any(not _inside(area, *location) for area in (metadata_source["area_of_use"], metadata_target["area_of_use"], operation["area_of_use"])):
                notices.append({"code": "COORDINATE_OUTSIDE_AREA", "index": index, "id": row["id"],
                                "message": "该点位于所选坐标系或操作的声明适用区外；变换值不是当地精度证明"})
        except (ValueError, ProjError, OverflowError) as exc:
            error = {"index": index, "id": row["id"], "code": "COORDINATE_POINT_INVALID", "message": str(exc)}
            if policy == "reject":
                raise ValueError(f"点{index+1}：{exc}") from exc
            row["error"] = {"code": error["code"], "message": error["message"]}
            errors.append(error)
        result.append(row)
    if len(operations) == 1:
        operation = operations[0]
    elif not operations and fixed is not None:
        operation = fixed[1]
    else:
        operation = {"description": "各点使用对应地区的操作；见operations及operation_index", "accuracy_m": None,
                     "ballpark": False, "best_available": bool(operations), "area_of_use": None}
    return {"source_crs": metadata_source, "target_crs": metadata_target, "axis_order": "xy",
            "operation": operation, "operations": operations, "points": result,
            "errors": errors, "warnings": notices, "can_apply": not errors,
            "assumptions": ["Only horizontal coordinates are transformed; depths and vertical datum are unchanged.",
                            "Operation accuracy is the PROJ declaration, not a measurement error bound.",
                            "Regional selection uses the source's own geodetic position for its area of interest; datum shifts may alter exact area-edge classification."]}
