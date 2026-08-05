"""Read documented Golden Software DSAA/DSBB/DSRB grids, not Makai files."""
from __future__ import annotations

import math
import struct
import numpy as np
from pyproj import CRS, Transformer

from .gis import _attach, sample_route
from .units import length_factor

MAX_CELLS = 4_000_000
BLANK = 1.70141e38


def _dimensions(nx, ny):
    if not 2 <= nx <= 100_000 or not 2 <= ny <= 100_000 or nx * ny > MAX_CELLS:
        raise ValueError("Surfer网格须至少2×2且不超过4,000,000单元")


def read_grid(data: bytes) -> dict:
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("Surfer网格超过128 MB")
    tag = data[:4]
    warnings, blank, version = [], BLANK, 1
    try:
        if tag == b"DSAA":
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("DSAA文本网格超过32 MB")
            words = data.decode("ascii").split()
            if len(words) < 9:
                raise ValueError("DSAA表头不完整")
            nx, ny = int(words[1]), int(words[2])
            _dimensions(nx, ny)
            xlo, xhi, ylo, yhi, zlo, zhi = map(float, words[3:9])
            if len(words) != 9 + nx * ny:
                raise ValueError("DSAA节点数量与表头不一致")
            values = np.array(words[9:], dtype=float).reshape(ny, nx)
            dx, dy = (xhi-xlo)/(nx-1), (yhi-ylo)/(ny-1)
            fmt = "Surfer6-DSAA"
        elif tag == b"DSBB":
            nx, ny, xlo, xhi, ylo, yhi, zlo, zhi = struct.unpack_from("<hh6d", data, 4)
            _dimensions(nx, ny)
            if len(data) != 56 + nx * ny * 4:
                raise ValueError("DSBB节点字节数与表头不一致")
            values = np.frombuffer(data, dtype="<f4", count=nx*ny, offset=56).astype(float).reshape(ny, nx)
            dx, dy = (xhi-xlo)/(nx-1), (yhi-ylo)/(ny-1)
            fmt = "Surfer6-DSBB"
        elif tag == b"DSRB":
            size, version = struct.unpack_from("<ii", data, 4)
            if size != 4 or version not in (1, 2):
                raise ValueError("不支持的DSRB头或版本")
            offset, header, values, prior = 12, None, None, None
            while offset < len(data):
                section, size = struct.unpack_from("<4si", data, offset)
                offset += 8
                if size < 0 or offset + size > len(data):
                    raise ValueError("DSRB节长度越界或文件截断")
                if section == b"GRID":
                    if header is not None or size != 72:
                        raise ValueError("DSRB需要一个72字节GRID节")
                    ny, nx, xlo, ylo, dx, dy, zlo, zhi, rotation, blank = struct.unpack_from("<ii8d", data, offset)
                    _dimensions(nx, ny)
                    if not math.isfinite(rotation) or abs(rotation) > 1e-12:
                        raise ValueError("当前不支持旋转Surfer网格，请转换为带坐标系GeoTIFF")
                    header = True
                elif section == b"DATA" and prior == b"GRID":
                    if header is None or size != nx * ny * 8 or values is not None:
                        raise ValueError("DSRB数据数量不匹配")
                    values = np.frombuffer(data, dtype="<f8", count=nx*ny, offset=offset).copy().reshape(ny, nx)
                elif section == b"FLTI":
                    if size < 8:
                        raise ValueError("DSRB断层节不完整")
                    traces, vertices = struct.unpack_from("<ii", data, offset)
                    if traces or vertices:
                        warnings.append("含断层轨迹，当前只采样节点数据，双线性插值未建模断层不连续")
                elif section == b"DATA" and prior == b"FLTI":
                    pass
                else:
                    warnings.append(f"跳过未知DSRB节 {section!r}")
                prior = section
                offset += size
            if header is None or values is None:
                raise ValueError("DSRB缺少GRID及紧随的数据节")
            fmt = "Surfer7-DSRB"
        else:
            raise ValueError("只支持Surfer DSAA、DSBB或DSRB网格")
    except (struct.error, UnicodeError, OverflowError) as exc:
        raise ValueError("Surfer文件损坏、截断或字符无效") from exc
    if not all(math.isfinite(v) for v in (xlo, ylo, dx, dy, zlo, zhi, blank)) or dx <= 0 or dy <= 0:
        raise ValueError("Surfer网格范围、间距或NoData声明无效")
    missing = values == blank if fmt == "Surfer7-DSRB" and version == 2 else values >= blank
    values[missing | ~np.isfinite(values)] = np.nan
    return {"values": values, "nx": nx, "ny": ny, "xlo": xlo, "ylo": ylo, "dx": dx, "dy": dy,
            "format": fmt, "version": version, "warnings": warnings, "blank_value": blank}


def _sample(grid, x, y, method):
    fx, fy = (x-grid["xlo"])/grid["dx"], (y-grid["ylo"])/grid["dy"]
    nx, ny, values = grid["nx"], grid["ny"], grid["values"]
    if not math.isfinite(fx) or not math.isfinite(fy) or fx < -1e-9 or fy < -1e-9 or fx > nx-1+1e-9 or fy > ny-1+1e-9:
        return None
    fx, fy = min(nx-1, max(0, fx)), min(ny-1, max(0, fy))
    if method == "nearest":
        number = values[int(math.floor(fy+.5)), int(math.floor(fx+.5))]
    else:
        ix, iy = min(nx-2, int(fx)), min(ny-2, int(fy))
        tx, ty = fx-ix, fy-iy
        weighted = [(values[iy, ix], (1-tx)*(1-ty)), (values[iy, ix+1], tx*(1-ty)),
                    (values[iy+1, ix], (1-tx)*ty), (values[iy+1, ix+1], tx*ty)]
        # Zero-weight missing corners do not erase an exactly sampled good node.
        if any(not math.isfinite(v) and w > 1e-14 for v, w in weighted):
            return None
        number = sum(v*w for v, w in weighted if w > 1e-14)
    return float(number) if math.isfinite(number) else None


def profile_from_surfer(project, data, *, source_crs, spacing_m=1000, depth_positive="down", vertical_datum="user-unspecified", method="linear", depth_units="m"):
    factor = length_factor(depth_units)
    if not source_crs:
        raise ValueError("Surfer文件不包含可靠CRS，须明确声明源坐标系")
    if depth_positive not in ("up", "down") or method not in ("linear", "nearest"):
        raise ValueError("Surfer需明确水深方向及linear/nearest采样方式")
    try:
        crs = CRS.from_user_input(source_crs)
        converter = Transformer.from_crs(4326, crs, always_xy=True)
    except Exception as exc:
        raise ValueError("Surfer源坐标系无法识别") from exc
    grid = read_grid(data)
    if grid["warnings"] and method == "linear" and any("断层" in warning for warning in grid["warnings"]):
        raise ValueError("含断层Surfer网格当前需选择nearest，避免双线性跨断层推断")
    samples = []
    for point in sample_route(project, spacing_m):
        x, y = converter.transform(point["longitude"], point["latitude"])
        value = _sample(grid, x, y, method)
        if value is not None and depth_positive == "up":
            value = -value
        if value is not None:
            value *= factor
        samples.append({"kp_m": point["kp_m"], "depth_m": value if value is not None and value >= 0 else None})
    source = {"name": "用户Surfer测深", "format": grid["format"], "version": grid["version"], "crs": crs.to_string(), "units": "m", "method": method,
              "depth_positive": depth_positive, "vertical_datum": vertical_datum, "spacing_m": spacing_m, "shape": [grid["ny"], grid["nx"]],
              "source_depth_units": depth_units,
              "bounds": [grid["xlo"], grid["ylo"], grid["xlo"]+(grid["nx"]-1)*grid["dx"], grid["ylo"]+(grid["ny"]-1)*grid["dy"]]}
    warnings = [{"code": "SURFER_SECTION_LIMIT", "severity": "warning", "message": warning} for warning in grid["warnings"]]
    return _attach(project, samples, source, warnings)
