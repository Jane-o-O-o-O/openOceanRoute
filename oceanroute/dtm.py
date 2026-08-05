"""Open-data terrain gridding, contours, slope, aspect and shaded relief."""

from __future__ import annotations

import base64
import math

import numpy as np
from pyproj import CRS, Transformer
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import cKDTree, QhullError

from .geodesy import finite_number, split_antimeridian
from .gis import import_xyz


def build_dtm(text: str, config: dict | None = None) -> dict:
    config = config or {}
    points, warnings = import_xyz(text)
    spacing = finite_number(config.get("grid_spacing_m", 1000), "grid_spacing_m", minimum=1, maximum=100_000)
    gap = finite_number(config.get("max_gap_m", 5000), "max_gap_m", minimum=1, maximum=1_000_000)
    interval = finite_number(config.get("contour_interval_m", 100), "contour_interval_m", minimum=.1, maximum=100_000)
    azimuth = math.radians(finite_number(config.get("azimuth_deg", 315), "azimuth_deg", minimum=0, maximum=360))
    elevation = math.radians(finite_number(config.get("sun_elevation_deg", 45), "sun_elevation_deg", minimum=0, maximum=90))
    lon = math.degrees(math.atan2(np.sin(np.radians(points[:,0])).mean(), np.cos(np.radians(points[:,0])).mean()))
    lat = float(points[:,1].mean())
    local = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs")
    forward = Transformer.from_crs("EPSG:4326", local, always_xy=True)
    backward = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
    px,py = forward.transform(points[:,0],points[:,1])
    xy = np.column_stack([px,py])
    if not np.isfinite(xy).all():
        raise ValueError("测深范围超出局部网格投影，请分海区处理")
    xmin,ymin = np.floor(xy.min(axis=0)/spacing)*spacing
    xmax,ymax = np.ceil(xy.max(axis=0)/spacing)*spacing
    nx = max(2,int(round((xmax-xmin)/spacing))+1)
    ny = max(2,int(round((ymax-ymin)/spacing))+1)
    if nx*ny>250_000:
        raise ValueError(f"网格 {nx}×{ny} 超过 250,000 单元，请增大网格间距或分块")
    x=xmin+np.arange(nx)*spacing
    y=ymin+np.arange(ny)*spacing
    xx,yy=np.meshgrid(x,y)
    queries=np.column_stack([xx.ravel(),yy.ravel()])
    nearest,idx=cKDTree(xy).query(queries)
    method=config.get("method","linear")
    if method=="linear":
        try:
            depth=LinearNDInterpolator(xy,points[:,2],fill_value=np.nan)(queries)
        except QhullError as exc:
            raise ValueError("测深点退化，无法生成三角网") from exc
    elif method=="idw":
        distances,indices=cKDTree(xy).query(queries,k=min(8,len(points)))
        weights=1/np.maximum(distances,1e-6)**2
        depth=(weights*points[indices,2]).sum(axis=1)/weights.sum(axis=1)
        try:
            from scipy.spatial import Delaunay
            depth[Delaunay(xy).find_simplex(queries)<0]=np.nan
        except QhullError as exc:
            raise ValueError("测深点共线，未授权网格外推") from exc
    else:
        raise ValueError("网格插值仅支持 linear 或 idw")
    depth[nearest>gap]=np.nan
    depth=depth.reshape(ny,nx)
    if not np.isfinite(depth).any():
        raise ValueError("网格没有有效水深，请调整范围、间距或最大测点距离")
    dy,dx=np.gradient(depth,spacing,spacing)
    gradient=np.hypot(dx,dy)
    slope=np.degrees(np.arctan(gradient))
    aspect=(np.degrees(np.arctan2(dx,dy))+360)%360
    aspect[gradient<1e-12]=np.nan
    normal=np.stack([dx,dy,np.ones_like(depth)],axis=-1)
    normal/=np.linalg.norm(normal,axis=-1,keepdims=True)
    sun=np.array([math.cos(elevation)*math.sin(azimuth),math.cos(elevation)*math.cos(azimuth),math.sin(elevation)])
    shade=np.maximum(0,(normal*sun).sum(axis=-1))

    try:
        import contourpy
    except ImportError as exc:
        raise ValueError("等深线需要 contourpy：pip install '.[terrain]'") from exc
    levels=np.arange(math.ceil(float(np.nanmin(depth))/interval)*interval, float(np.nanmax(depth))+interval*1e-10,interval)
    if len(levels)>500:
        raise ValueError("等深线级数超过 500，请加大等深距")
    contours=[]
    generator=contourpy.contour_generator(x=x,y=y,z=np.ma.masked_invalid(depth),line_type="Separate",corner_mask=False)
    for level in levels:
        for line in generator.lines(float(level)):
            if len(line)<2:
                continue
            longs,lats=backward.transform(line[:,0],line[:,1])
            coords=list(map(list,zip(longs.tolist(),lats.tolist())))
            segments=split_antimeridian(coords,"geodesic")
            geometry={"type":"LineString","coordinates":coords} if len(segments)<2 else {"type":"MultiLineString","coordinates":segments}
            contours.append({"type":"Feature","properties":{"depth_m":float(level)},"geometry":geometry})

    # Raster rows run north to south, while the calculation grid runs south to north.
    try:
        from rasterio.io import MemoryFile
        from rasterio.transform import from_origin
    except ImportError as exc:
        raise ValueError("GeoTIFF 导出需要 rasterio：pip install '.[terrain]'") from exc
    with MemoryFile() as memory:
        with memory.open(driver="GTiff",width=nx,height=ny,count=4,dtype="float32",crs=local,
                         transform=from_origin(x[0]-spacing/2,y[-1]+spacing/2,spacing,spacing),nodata=-9999,compress="deflate") as raster:
            for band,array,name in [(1,depth,"depth_m_positive_down"),(2,slope,"slope_degrees"),(3,aspect,"downslope_compass_degrees"),(4,shade,"hillshade_0_to_1")]:
                raster.write(np.where(np.isfinite(array),array,-9999)[::-1].astype("float32"),band)
                raster.set_band_description(band,name)
            raster.update_tags(vertical_datum=str(config.get("vertical_datum","user-unspecified")),algorithm=method,source="user-xyz",depth_positive="down")
        tif=memory.read()

    stride=max(1,math.ceil(max(nx,ny)/128))
    def values(array):
        return [[float(v) if math.isfinite(v) else None for v in row] for row in array[::stride,::stride]]
    finite_slopes=slope[np.isfinite(slope)]
    return {"metadata":{"source_points":len(points),"width":nx,"height":ny,"grid_spacing_m":spacing,"method":method,
                        "crs":local.to_string(),"vertical_datum":config.get("vertical_datum","user-unspecified"),
                        "missing_cells":int(np.isnan(depth).sum()),"valid_cells":int(np.isfinite(depth).sum()),"max_gap_m":gap,
                        "depth_min_m":float(np.nanmin(depth)),"depth_max_m":float(np.nanmax(depth)),
                        "max_slope_deg":float(finite_slopes.max()) if len(finite_slopes) else None,
                        "contour_count":len(contours),"contour_interval_m":interval},
            "preview":{"x_m":x[::stride].tolist(),"y_m":y[::stride].tolist(),"depth_m":values(depth),"slope_deg":values(slope),"aspect_deg":values(aspect),"hillshade":values(shade)},
            "contours":{"type":"FeatureCollection","features":contours},
            "geotiff_base64":base64.b64encode(tif).decode("ascii"),"warnings":warnings,
            "assumptions":["水深正向下、单位为米，水平 WGS84；垂直基准采用用户声明。",
                           "采用局部等距方位投影生成规则网格，坡度和阴影由有限差分估算。",
                           "保留凸包外和超出最大测点间距的缺测，不生成未知海底数据。",
                           "网格分辨率不代表原始测深精度；大范围需分区验证投影误差。"]}
