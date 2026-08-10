"""Open-data terrain gridding, contours, slope, aspect and shaded relief."""

from __future__ import annotations

import base64
import math
import time

import numpy as np
from pyproj import CRS, Transformer
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import cKDTree, QhullError, Delaunay
from scipy import sparse, ndimage
from scipy.sparse.linalg import LinearOperator, lsmr
from shapely.geometry import LineString

from .geodesy import finite_number, split_antimeridian
from .gis import import_xyz
from .terrain_boundaries import boundary_from_config, boundary_mask, boundary_tag, boundary_geometry
from .terrain_slice import extract_dtm_slice


def _integer(value, field, minimum, maximum):
    number = finite_number(value, field, minimum=minimum, maximum=maximum)
    if number != int(number):
        raise ValueError(f"{field} 必须为整数")
    return int(number)


def _minimum_curvature(x, y, xy, values, active, config):
    """Minimize a discretized thin-plate/membrane residual energy, with free edges.

    Differences are constructed only inside the active domain. This is a soft
    constrained variational model; it is not the proprietary Makai/GMT SOR code.
    """
    started = time.perf_counter()
    if not isinstance(config, dict):
        raise ValueError("minimum_curvature 必须为对象")
    allowed = {"tension", "smoothing", "data_weight", "curvature_length_m", "max_iterations", "tolerance", "max_work_units", "max_nodes"}
    if set(config) - allowed:
        raise ValueError("minimum_curvature 含不支持参数：" + ", ".join(sorted(set(config) - allowed)))
    h = float(x[1] - x[0])
    tension = finite_number(config.get("tension", 0), "tension", minimum=0, maximum=1)
    smoothing = finite_number(config.get("smoothing", 1), "smoothing", minimum=1e-6, maximum=1e6)
    weight = finite_number(config.get("data_weight", 100), "data_weight", minimum=1e-6, maximum=1e8)
    length = finite_number(config.get("curvature_length_m", h), "curvature_length_m", minimum=1, maximum=1e6)
    iterations = _integer(config.get("max_iterations", 2000), "max_iterations", 1, 10_000)
    tolerance = finite_number(config.get("tolerance", 1e-8), "tolerance", minimum=1e-12, maximum=1e-2)
    budget = _integer(config.get("max_work_units", 50_000_000), "max_work_units", 1, 500_000_000)
    cap = _integer(config.get("max_nodes", 40_000), "max_nodes", 4, 40_000)
    if int(active.sum()) > cap or len(xy) > 20_000:
        raise ValueError("minimum_curvature 超出 max_nodes 或 20,000 源点预算，请增大网格间距或分区")
    if active.sum() < 4:
        raise ValueError("minimum_curvature 至少需要四个有效网格节点")
    ny, nx = active.shape
    unsupported = 0
    if tension == 0:
        # Sufficient uniqueness criterion for the masked Hessian: each active
        # node belongs to a complete 2x2 cell, and complete cells share edges
        # throughout each node component. Hxy makes each cell affine; the
        # Hxx/Hyy rows across shared edges equate neighbouring plane slopes.
        # Single-node tendrils do not have such a Hessian constraint; leave
        # them unknown instead of filling them with the LSMR minimum norm.
        cells = active[:-1,:-1] & active[:-1,1:] & active[1:,:-1] & active[1:,1:]
        covered = np.zeros_like(active)
        for dy, dx in ((0,0),(0,1),(1,0),(1,1)):
            covered[dy:ny-1+dy,dx:nx-1+dx] |= cells
        unsupported = int((active & ~covered).sum())
        active &= covered
        if not active.any():
            raise ValueError("纯薄板 minimum_curvature 的细窄掩膜没有足够二阶约束；请使用 tension > 0 或增大区域宽度")
        node_labels, node_components = ndimage.label(active)
        if node_components > 256:
            raise ValueError("minimum_curvature 独立区域超过 256，请分区计算")
        cell_labels, _ = ndimage.label(cells)
        for component in range(1,node_components+1):
            belongs = cells & (node_labels[:-1,:-1] == component)
            if len(np.unique(cell_labels[belongs])) != 1:
                raise ValueError("纯薄板掩膜仅角连接，二阶算子不能确定唯一曲面；请使用 tension > 0")
    # A source near an irregular mask edge uses the remaining bilinear weights,
    # renormalized. The affine trend is evaluated at the actual source position.
    fx, fy = (xy[:, 0] - x[0]) / h, (xy[:, 1] - y[0]) / h
    ix = np.clip(np.floor(fx).astype(int), 0, nx - 2)
    iy = np.clip(np.floor(fy).astype(int), 0, ny - 2)
    a, b = np.clip(fx - ix, 0, 1), np.clip(fy - iy, 0, 1)
    cols = np.column_stack((iy * nx + ix, iy * nx + ix + 1, (iy + 1) * nx + ix, (iy + 1) * nx + ix + 1))
    coefficients = np.column_stack(((1-a)*(1-b), a*(1-b), (1-a)*b, a*b))
    coefficients *= active.ravel()[cols]
    totals = coefficients.sum(axis=1)
    usable = totals > 1e-12
    if usable.sum() < 3:
        raise ValueError("minimum_curvature 裁剪后有效数据约束不足")
    coefficients = coefficients[usable] / totals[usable, None]
    cols, xy, values = cols[usable], xy[usable], values[usable]
    # Anchor the actual operator's nullspace, not merely the raw point geometry.
    # A bilinear footprint spanning disconnected components is discarded.
    labels, count = ndimage.label(active)
    if count > 256:
        raise ValueError("minimum_curvature 独立区域超过 256，请分区计算")
    sample_labels = labels.ravel()[cols[np.arange(len(cols)), np.argmax(coefficients, axis=1)]]
    component_labels = labels.ravel()[cols]
    one_component = np.all((coefficients <= 0) | (component_labels == sample_labels[:,None]),axis=1)
    ambiguous = int((~one_component).sum())
    cols, coefficients, xy, values, sample_labels = cols[one_component], coefficients[one_component], xy[one_component], values[one_component], sample_labels[one_component]
    dropped = 0
    for component in range(1, count + 1):
        selected = sample_labels == component
        positions = np.column_stack(((coefficients[selected]*(cols[selected]%nx)).sum(axis=1),
                                     (coefficients[selected]*(cols[selected]//nx)).sum(axis=1)))
        design = np.column_stack((np.ones(len(positions)), positions))
        singular = np.linalg.svd(design,compute_uv=False) if len(positions) else np.zeros(0)
        anchored = len(positions)>0 if tension>0 else len(singular)==3 and singular[-1]>1e-10*singular[0]
        if not anchored:
            dropped += int((labels == component).sum())
            active[labels == component] = False
    keep = active.ravel()[cols].any(axis=1)
    cols, coefficients, xy, values = cols[keep], coefficients[keep], xy[keep], values[keep]
    if not active.any() or len(values) < 3:
        raise ValueError("minimum_curvature 连通区域缺少实际算子所需数据锚定；纯薄板须三个非共线有效约束")
    coefficients *= active.ravel()[cols]
    coefficients /= coefficients.sum(axis=1)[:, None]
    ids = np.full(nx * ny, -1, dtype=int)
    ids[active.ravel()] = np.arange(int(active.sum()))
    compact = ids.reshape(ny, nx)
    trend_design = np.column_stack((np.ones(len(xy)), (xy[:, 0] - x[0]) / h, (xy[:, 1] - y[0]) / h))
    plane, _, rank, _ = np.linalg.lstsq(trend_design, values, rcond=None)
    if rank < 3:
        raise ValueError("minimum_curvature 数据共线，无法确定平面趋势")
    residual = values - trend_design @ plane
    # Avoid spending iterations fitting floating-point cancellation in an
    # exact affine plane. This is only machine-scale, not a data-noise filter.
    if np.max(np.abs(residual)) <= 5e-13 * max(1, float(np.max(np.abs(values)))):
        residual[:] = 0
    row = np.repeat(np.arange(len(cols)), 4)
    c = ids[cols.ravel()]
    v = coefficients.ravel()
    used = (c >= 0) & (v > 0)
    observation = sparse.coo_matrix((v[used], (row[used], c[used])), shape=(len(values), int(active.sum()))).tocsr()
    blocks = [observation * math.sqrt(weight)]
    curvature_blocks, gradient_blocks = [], []
    def difference(stencil, factors):
        indices = np.column_stack([part.ravel() for part in stencil])
        good = np.all(indices >= 0, axis=1)
        indices = indices[good]
        if not len(indices):
            return sparse.csr_matrix((0, int(active.sum())))
        return sparse.coo_matrix((np.tile(factors, len(indices)), (np.repeat(np.arange(len(indices)), len(factors)), indices.ravel())),
                                 shape=(len(indices), int(active.sum()))).tocsr()
    if tension < 1:
        curvature_blocks = [difference([compact[:, :-2], compact[:, 1:-1], compact[:, 2:]], [1, -2, 1]),
                            difference([compact[:-2, :], compact[1:-1, :], compact[2:, :]], [1, -2, 1]),
                            difference([compact[:-1, :-1], compact[:-1, 1:], compact[1:, :-1], compact[1:, 1:]], [math.sqrt(2), -math.sqrt(2), -math.sqrt(2), math.sqrt(2)])]
        blocks.extend(block * (math.sqrt(smoothing * (1-tension)) * length/h) for block in curvature_blocks)
    if tension > 0:
        gradient_blocks = [difference([compact[:, :-1], compact[:, 1:]], [-1, 1]),
                           difference([compact[:-1, :], compact[1:, :]], [-1, 1])]
        blocks.extend(block * math.sqrt(smoothing*tension) for block in gradient_blocks)
    matrix = sparse.vstack(blocks, format="csr")
    if np.any(matrix.getnnz(axis=0)==0):
        raise ValueError("minimum_curvature 存在不受任何方程约束的网格节点；请使用 tension > 0 或改变掩膜")
    rhs = np.zeros(matrix.shape[0])
    rhs[:len(residual)] = residual * math.sqrt(weight)
    nonzeros = int(matrix.nnz)
    work = nonzeros
    multiplies = 0
    # Count sparse nonzero visits, not hardware FLOPs. Enforce the bound before
    # every multiply, including initial/final diagnostic products.
    def charge(amount):
        nonlocal work
        if work + amount > budget:
            raise ValueError("minimum_curvature max_work_units 工作预算不足；请分区、增大间距或显式增加预算")
        work += int(amount)
    if work > budget:
        raise ValueError("minimum_curvature 矩阵装配超过 max_work_units")
    def multiply(vector, transpose=False):
        nonlocal multiplies
        charge(nonzeros)
        multiplies += 1
        return matrix.T @ vector if transpose else matrix @ vector
    operator = LinearOperator(matrix.shape, matvec=multiply, rmatvec=lambda v: multiply(v, True), dtype=np.float64)
    solved = lsmr(operator, rhs, atol=tolerance, btol=tolerance, conlim=1e8, maxiter=iterations)
    solution, stop, itn, normr, normar, norma, condition, normx = solved
    if not np.isfinite(solution).all():
        raise ValueError("minimum_curvature 求解产生非有限值")
    charge(int(observation.nnz) + sum(int(block.nnz) for block in curvature_blocks + gradient_blocks))
    errors = observation @ solution - residual
    xx, yy = np.meshgrid((x-x[0])/h, (y-y[0])/h)
    trend = plane[0] + plane[1]*xx + plane[2]*yy
    result = np.full(active.shape, np.nan)
    result[active] = trend[active] + solution
    energy_h = sum(float(np.sum((block @ solution)**2)) for block in curvature_blocks) * (length/h)**2
    energy_g = sum(float(np.sum((block @ solution)**2)) for block in gradient_blocks)
    # An overshoot above sea level is not silently clamped into fabricated data.
    if np.any(result[active] < 0):
        raise ValueError("minimum_curvature 曲率超调产生负水深；请核对数据、提高张力或改用 linear")
    finite = lambda v: float(v) if math.isfinite(v) else None
    return result, {"algorithm": "masked-variational-thin-plate-lsmr-v1", "validation_status": "research",
                    "converged": int(stop) in (0, 1, 2, 4, 5), "stop_code": int(stop), "iterations": int(itn),
                    "active_nodes": int(active.sum()), "data_constraints": len(values), "unanchored_missing_nodes": dropped,
                    "stencil_unidentified_missing_nodes":unsupported,"ambiguous_component_source_points":ambiguous,
                    "unique_solution_verified":True,"nullspace_check":"complete edge-connected 2x2 cells plus rank(B[1,x,y])" if tension==0 else "connected membrane graph plus observed component anchors",
                    "unusable_source_points": int((~usable).sum()), "matrix_rows": int(matrix.shape[0]), "matrix_nonzeros": nonzeros,
                    "work_units": int(work), "matrix_products": multiplies, "max_work_units": budget,
                    "elapsed_s": time.perf_counter()-started, "tension": tension, "smoothing": smoothing, "data_weight": weight,
                    "curvature_length_m": length, "tolerance": tolerance, "max_iterations": iterations,
                    "data_rmse_m": float(np.sqrt(np.mean(errors**2))), "data_max_abs_error_m": float(np.max(np.abs(errors))),
                    "least_squares_residual_norm": finite(normr), "normal_residual_norm": finite(normar), "condition_estimate": finite(condition),
                    "bending_energy_m2": energy_h, "membrane_energy_m2": energy_g,
                    "trend": {"depth_at_grid_origin_m": float(plane[0]), "dz_dx": float(plane[1]/h), "dz_dy": float(plane[2]/h)},
                    "boundary_condition": "free discrete edges: omitted differences across NoData, no fixed zero/edge elevations"}


def build_dtm(text: str, config: dict | None = None) -> dict:
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError("config 必须为对象")
    datum = config.get("vertical_datum", "user-unspecified")
    if not isinstance(datum, str) or not datum.strip() or len(datum) > 500:
        raise ValueError("vertical_datum 必须为不超过 500 字符的非空说明")
    if not isinstance(text, str):
        raise ValueError("XYZ text 必须为字符串")
    points, warnings = import_xyz(text)
    if np.max(points[:,2]) > 1_000_000:
        raise ValueError("DTM 源水深超过数值范围 1,000,000 m，请核对单位")
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
    source_count = len(points)
    boundary = boundary_from_config(config, local)
    source_keep = boundary_mask(boundary, xy)
    xy, points = xy[source_keep], points[source_keep]
    if len(points) < 3:
        raise ValueError("边界裁剪后至少需要三个有效测深点")
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
    solver = None
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
            depth[Delaunay(xy).find_simplex(queries)<0]=np.nan
        except QhullError as exc:
            raise ValueError("测深点共线，未授权网格外推") from exc
    elif method=="minimum_curvature":
        try:
            active = (Delaunay(xy).find_simplex(queries)>=0) & (nearest<=gap) & boundary_mask(boundary, queries)
        except QhullError as exc:
            raise ValueError("测深点共线，无法确定曲率网格范围") from exc
        depth, solver = _minimum_curvature(x, y, xy, points[:,2], active.reshape(ny,nx), config.get("minimum_curvature", {}))
        depth = depth.ravel()
        if not solver["converged"]:
            warnings.append({"code":"DTM_MINIMUM_CURVATURE_NOT_CONVERGED", "severity":"warning", "message":"最小曲率求解未收敛；返回的是候选网格，不应按已验收地形使用"})
        if solver["unanchored_missing_nodes"]:
            warnings.append({"code":"DTM_UNANCHORED_COMPONENT", "severity":"warning", "message":"部分独立网格区域缺少算子所需数据锚定，已保留为缺测"})
        if solver["stencil_unidentified_missing_nodes"]:
            warnings.append({"code":"DTM_UNIDENTIFIED_STENCIL_NODE", "severity":"warning", "message":"部分细窄边缘节点无充分二阶约束，已保留为缺测；可选择 tension>0 的膜张力模型"})
    else:
        raise ValueError("网格插值仅支持 linear、idw 或 minimum_curvature")
    depth[nearest>gap]=np.nan
    depth[~boundary_mask(boundary, queries)] = np.nan
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
    first_level=math.ceil(float(np.nanmin(depth))/interval)*interval
    level_count=max(0,math.floor((float(np.nanmax(depth))-first_level)/interval+1e-10)+1)
    if level_count>500:
        raise ValueError("等深线级数超过 500，请加大等深距")
    levels=first_level+np.arange(level_count)*interval
    contours=[]
    generator=contourpy.contour_generator(x=x,y=y,z=np.ma.masked_invalid(depth),line_type="Separate",corner_mask=False)
    for level in levels:
        for line in generator.lines(float(level)):
            if len(line)<2:
                continue
            clipped = LineString(line)
            if boundary is not None:
                included = boundary_geometry(boundary)
                if included is not None:
                    clipped = clipped.intersection(included)
                elif boundary["exclude"] is not None:
                    clipped = clipped.difference(boundary["exclude"])
            lines = [clipped] if clipped.geom_type == "LineString" else list(clipped.geoms) if hasattr(clipped, "geoms") else []
            for part in lines:
                if part.geom_type != "LineString" or part.is_empty or len(part.coords)<2:
                    continue
                line_xy = np.asarray(part.coords)
                longs,lats=backward.transform(line_xy[:,0],line_xy[:,1])
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
            raster.update_tags(vertical_datum=str(config.get("vertical_datum","user-unspecified")),algorithm=method,source="user-xyz",depth_positive="down",
                               boundary_projected_json=boundary_tag(boundary), solver_converged=str(solver["converged"]).lower() if solver else "true")
        tif=memory.read()

    stride=max(1,math.ceil(max(nx,ny)/128))
    def values(array):
        return [[float(v) if math.isfinite(v) else None for v in row] for row in array[::stride,::stride]]
    finite_slopes=slope[np.isfinite(slope)]
    return {"validation_status":"research", "metadata":{"source_points":source_count,"used_source_points":len(points),"boundary_excluded_source_points":source_count-len(points),
                        "solver":solver,"converged":solver["converged"] if solver else True,
                        "boundary":{"source_kind":boundary["source_kind"],"source_crs":boundary["source_crs"],"include_count":boundary["include_count"],"exclude_count":boundary["exclude_count"]} if boundary else None,
                        "width":nx,"height":ny,"grid_spacing_m":spacing,"method":method,
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
                           "minimum_curvature 是独立软约束薄板残差平滑；张力作用于去平面趋势后的残差，边界使用缺测掩膜内的自由离散差分，不等同原厂 SOR 或边界张力。",
                           "裁剪按网格节点屏蔽，等深线另作精确多边形相交；小于网格间距的孔洞需提高分辨率，完整栅格剖面仍核对原始边界。",
                           "网格分辨率不代表原始测深精度；大范围需分区验证投影误差。"]}
