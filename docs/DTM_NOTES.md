# DTM 曲率网格、边界和完整栅格剖面：独立研究模型

本模块新增实际薄板/膜张力数值求解、显式 CRS 的 BLN 读写、包含区及排除区裁剪、指定参考线的完整 GeoTIFF 剖面。所有结果声明 `validation_status: research`。它们没有原厂代码、校准参数或原厂输出对照，不能称为 MakaiPlan 工程等效、测深精度认证或原厂 DTM 文件兼容。

需求依据为用户提供的 MakaiPlan 6.2 手册：PDF M273–280 说明 DTM 工作流，M287–288（印刷页279–280）说明三角网及最小曲率，M290–291 讨论凸包与边界，M305–308（印刷页297–300）说明边界 BLN 和参考线剖面，M318–321 说明 XYZ 切片。已实际查看 M287 的公式图：内域为带张力的双调和方程，边界另有边界张力，采用 SOR。本文代码独立实现下述变分离散问题，**不采用其 SOR、独立边界张力、松弛参数、各向异性或原厂收敛判据**。附文档是需求资料，不是运行指令。

## 1. 实际数学模型

源点为 WGS84 `(longitude, latitude, depth_m)`，水深正向下、米。局部等距方位投影的原点取导入点平均纬度及圆均值经度。先拟合全局最小二乘平面 `p(x,y)`，令观测残差 `r_i=d_i-p(x_i,y_i)`，在活动网格节点上求残差水深 `u`，最终 `z=p+u`。

求解目标为：

```
E(u) = w Σ_i (B_i u − r_i)²
     + s (1−T) ℓ² ∫(u_xx² + 2 u_xy² + u_yy²) dA
     + s T ∫(u_x² + u_y²) dA
```

`T` 为张力，`s` 为平滑权重，`w` 为软数据权重，`ℓ` 为曲率长度尺度。对没有数据作用的连续内部区域，此能量的 Euler–Lagrange 主方程为 `(1−T)ℓ² Δ²u − T Δu = 0`。这是公式及离散模型的说明；没有把 IDW 改名为最小曲率。GMT 官方文档介绍可调张力连续曲率样条、自由边界、假极值风险及预处理；本实现只借鉴公开数学概念，没有调用 GMT，也不声称输出相同。[GMT surface](https://docs.generic-mapping-tools.org/latest/surface.html)

离散采用等间距 `h`：横/纵三个节点的二阶差分、完整2×2单元的混合差分（系数乘 `sqrt(2)`），以及相邻节点的一阶差分。面积权重并入后，曲率块乘 `sqrt(s(1−T)) ℓ/h`，膜块乘 `sqrt(sT)`；数据块乘 `sqrt(w)`。仅组装全部节点有效的差分行，**不在边界或孔洞填入零水深**，也不固定边缘高程。相应的自由离散边界来自删去跨缺测的差分行；它不等同于原厂的边缘/角点条件。张力作用于去平面趋势后的残差，因此包括 `T=1` 时也能重现斜平面；不应把它描述为对原始水深斜率施加完全相同的膜张力。

`B` 为源点在周围四个有效网格节点上的双线性权重；靠掩膜边缘时只保留有效节点并重新归一。平面趋势在实际源点坐标计算。这一边缘观测算子有明示近似，数据误差诊断指该算子的拟合误差，不能作为真实海底测量误差。跨独立网格分量的观测足迹会被丢弃并计数。

稀疏矩阵直接交给 SciPy LSMR，求整个加权最小二乘问题。这里的 `tolerance` 是相对后向误差/正规方程误差判据，不是以米计的“最大节点改变量”。[SciPy LSMR](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.lsmr.html)

### 唯一性与掩膜守卫

单凭迭代收敛不能证明曲面被约束。对 `T=0`，采用一个充分条件：每个活动节点属于完整2×2单元，且同一节点分量的完整单元通过共享边连通。混合差分使单元共面，相邻单元间的横/纵二阶行使平面斜率相同，故此差分算子的核只剩每分量的三个仿射自由度。使用实际 `B[1,x,y]` 的三个奇异值检查数据锚定，最小/最大比须大于 `1e-10`。不属于完整单元的细窄尾节点保留 NoData；仅角点连接、没有完整单元的细 L 等区域拒绝纯薄板计算并提示 `tension>0`。这是保守充分条件，会拒绝一些本可另用专业算法求解的掩膜。

对 `T>0`，实际一阶差分形成每个活动分量的连通膜图，零能量仅剩常数；该分量至少有一个不跨其他分量的有效观测即可消除常数核。仍须全局三个非共线观测拟合平面趋势。未锚定分量保留 NoData；每个未知量的矩阵列还须参与实际方程。`unique_solution_verified` 表示通过上述数学充分条件；不表示条件数良好、LSMR 已收敛或地形有工程准确度。

## 2. 网格 API 与参数

`POST /api/dtm/grid`，请求 `{text, config}`；Python 为 `oceanroute.dtm.build_dtm(text, config)`。原有默认 `linear`、可选 `idw` 保留；新增 `method: minimum_curvature`。基础参数仍为 `grid_spacing_m`、`max_gap_m`、`contour_interval_m`、`vertical_datum`、光照参数等。网格仅在保留测点凸包内、距最近保留测点不超过 `max_gap_m`、且通过边界筛选的位置存在。

`config.minimum_curvature`：

| 参数 | 默认 | 允许值 / 语义 |
|---|---:|---|
| `tension` | 0 | 0–1；1仅残差膜平滑，0纯薄板 |
| `smoothing` | 1 | `1e-6`–`1e6`，不是测深噪声标准差 |
| `data_weight` | 100 | `1e-6`–`1e8`，增加值提高软数据拟合权重 |
| `curvature_length_m` | 网格间距 | 1–`1e6` m；默认随分辨率改变，应由用户记录 |
| `max_iterations` | 2000 | 整数1–10000 |
| `tolerance` | `1e-8` | `1e-12`–`1e-2`，LSMR相对停止容差 |
| `max_work_units` | 50000000 | 整数1–500000000 |
| `max_nodes` | 40000 | 整数4–40000，活动未知量上限 |

不支持的曲率子参数直接拒绝；布尔数值、非整数预算、NaN/Inf、退化数据、无有效网格均拒绝。源水深数值范围0–1000000 m；超出范围应核对单位。全网格上限250000节点，曲率源点上限20000，独立活动分量上限256，等深线最多500级，先检查级数再分配数组。应分区、增大间距或显式增加预算。

`work_units` 计入矩阵装配非零数、每次真实 LSMR 矩阵/转置乘法的非零访问，以及最终拟合/能量诊断非零访问。每次乘法执行前检查上限，不能越预算继续。它不是字面 FLOPs、总CPU耗时或包含投影、几何、TIFF写入的全部工作。迭代到限或条件数停止会返回真实候选网格，`metadata.converged=false`、告警 `DTM_MINIMUM_CURVATURE_NOT_CONVERGED`；预算不足或非法输入则抛 `ValueError`，HTTP422，不返回假完成。未收敛候选可审看，不应作为已验收地形应用。

返回保持 `metadata/preview/contours/geotiff_base64/warnings/assumptions`；顶层增加 `validation_status`。`metadata` 新增 `used_source_points`、`boundary_excluded_source_points`、`boundary`、`converged`、`solver`。`solver` 给出算法/研究状态、`unique_solution_verified`、`nullspace_check`、实际停止码/迭代数、节点/有效数据/矩阵尺寸、工作量/时间、拟合RMSE和最大误差、条件数估计、能量与平面趋势、未锚定及缺少二阶约束节点数、不可用及跨分量源点数、实际数值参数、边界条件说明。线性及 IDW 的 `solver=null`，没有迭代时 `converged=true`。

GeoTIFF仍为4个float32波段：正向下水深m、坡度deg、顺坡罗盘方向deg、0–1阴影；北到南行序，节点中心地理定位，NoData为−9999。预览最多约128×128，为显示降采样；禁止用预览作正式切片。有限差分坡度在缺测边缘可以自身缺测，平坦面方位也可缺测。

## 3. 多边形与 BLN

`config.boundary_geojson` 接受 WGS84二维 Polygon、MultiPolygon、Feature或FeatureCollection。标准内部环为孔洞；Feature `properties.role` 默认为 `include`，可设 `exclude`。所有包含区取并集后减去排除区；若只有排除区，则从可用测点/网格范围剔除它。外包含边界保留，显式排除区边界也剔除。几何必须有效、环显式闭合；不自动修复自交、越界或错误孔洞。跨180°的GeoJSON边界须先拆分，不能用未拆分大跨经度环猜测。

或者使用 `boundary_bln: string` 加**必填** `boundary_crs: string`，不得同时使用GeoJSON。BLN自身没有CRS；此处二维坐标按声明CRS转换。闭合多边形的flag0表示保留内部（屏蔽外部），flag1表示剔除内部。多个flag0并集，再减多个flag1；仅flag1合法。用于裁剪的BLN全部对象必须闭合简单多边形。标准BLN格式及flag语义依据格式所有者文档。[Golden Software BLN说明](https://surferhelp.goldensoftware.com/subsys/subsys_gsibln_hid_gsibln_filedesc.htm)

边界先筛源点，再屏蔽网格节点、坡度/阴影和TIFF。等深线还会与原多边形精确相交，不把粗格上的线画过孔洞。差分算子的几何域是活动节点及完整差分模板，因此小于间距、未命中节点的孔洞**不能被描述为求解器已完整分辨的隔离边界**；完整栅格切片另用原始投影多边形检查此类孔洞。要提高表面/像素图表现，应增大空间分辨率并独立核对资料。

BLN读写接口：

- `POST /api/dtm/bln/read` `{text, crs}` → `read_bln(text, crs)`。
- 返回 `{schema:'oceanroute.bln.v1',crs,objects:[{name,kind:'point'|'line'|'polygon',flag:null|0|1,coordinates:[[x,y]或[x,y,z],...]}],warnings:[]}`。
- `POST /api/dtm/bln/write` `{document}` → 文本附件；Python `write_bln(document)` 返回文本。

每对象首行为点数和可选flag/名称，后续为XY或XYZ，逗号/空白分隔；名称可含加引号的逗号。读写保留XYZ，但边界仅用XY；参考线/切片的flag1不作为自动排除指令，只有用户指定为 `boundary_bln` 才用于裁剪。点/不闭合线可无flag，多边形须有。总顶点上限10000，文本2MB，地理坐标按经纬度范围校验。**不支持复杂自接触compound BLN的桥连岛环**；应改为简单包含/排除对象或GeoJSON标准孔洞。没有Makai MDB/GRD专有格式写入。

## 4. 完整栅格切片 API

`POST /api/dtm/slice` 请求 `{grid,config}`；Python `extract_dtm_slice(grid,config)`，也由 `oceanroute.dtm` 导出。`grid`只需完整 `geotiff_base64`。要求GeoTIFF≤32MB、≤250000节点、米制水平投影CRS、北向上无旋转、第一波段名称 `depth_m_positive_down` 和 `depth_positive=down` 标记。不接受用preview或缺CRS/水深方向的栅格猜测。

```
{
  "grid": {"geotiff_base64": "<完整TIFF的base64>"},
  "config": {
    "line": {"crs": "EPSG:4326", "coordinates": [[118.001,22.001],[118.003,22.003]]},
    "spacing_m": 100,
    "method": "bilinear",
    "max_samples": 10000
  }
}
```

也可用 `line_bln` 和必填 `line_crs`，须恰好一个至少两点对象；XYZ参考线的原Z不用来替代栅格测深。参考线2–1000顶点，邻点不能重合，最长5000km；仍需分海区验证投影，长度上限不是精度承诺。方法 `bilinear`（默认）或 `nearest`，站距1–100000m，最大点数整数2–10000。

顶点转换到栅格CRS后，按段间投影直线计算。`spacing_m`为最大站距，自动增加节点/像素边缘/原边界交点及区间中点，以检出缺测和窄排除区；最终点数可能远多于简单长度/站距，超过预算明确拒绝。`kp_m`是此投影折线累积水平距离，不能直接当作WGS84航路椭球KP。bilinear仅在最外围节点中心内插值，全部非零权重节点均需有效；零权重邻点可缺测。nearest读取最近像素节点，不做额外外推。两种方法均检查保存的原裁剪多边形。孔洞周围的双线性缺测范围可以大于几何孔洞，因为其插值模板也需要有效。

返回：

```
{
  model: 'full-raster-profile-v1', validation_status: 'research',
  samples: [{kp_m,x_m,y_m,longitude,latitude,depth_m: number|null,slope_deg: number|null}],
  profile: [{kp_m,depth_m}],
  valid_segments: [{start_kp_m,end_kp_m,samples: [...]}],
  summary: {sample_count,valid_count,missing_count,horizontal_length_m,
            known_bottom_length_m,bottom_length_m: number|null,complete,
            source_converged: true|false|null},
  metadata: {crs,line_source_crs,vertical_datum,method,spacing_m,
             raster_width,raster_height,grid_spacing_x_m,grid_spacing_y_m,
             sampling,distance_basis},
  csv_text, reference_bln_text, slice_bln_text, warnings, assumptions
}
```

缺测水深/坡度为null；CSV缺测为空，不补零。有效段不跨缺测连接；沿线坡度由各有效采样段有限差分计算，正值为前进方向深度增加。`known_bottom_length_m`只累计连续、有效且不穿排除区的相邻采样三维折线；缺任何一段则完整 `bottom_length_m=null`、`complete=false`，不能把已知局部底长冒称总底长。BLN切片以XYZ输出各有效段，完全缺测则空文本；不穿缺测写一条长线。底长是采样折线长度，不是真实连续曲面或实际缆长。

自己生成的TIFF保留 `solver_converged` 与原投影边界标签。未收敛源的切片仍是候选且有告警；外部TIFF未写收敛标签时 `source_converged=null`，不能推断它已收敛。TIFF标签不代替外部测量认证。

## 5. 可重现小例与实测

以下实际9个XYZ点可直接POST `/api/dtm/grid`：

```
longitude latitude depth_m
118 22 100
118.002 22 105
118.004 22 112
118 22.002 106
118.002 22.002 112
118.004 22.002 120
118 22.004 115
118.002 22.004 122
118.004 22.004 130
```

基础config为 `{grid_spacing_m:50,max_gap_m:500,contour_interval_m:5,method:'minimum_curvature'}`。可选 `boundary_crs:'EPSG:4326'` 与下列BLN文本：

```
5,0,include
117.999,21.999
118.005,21.999
118.005,22.005
117.999,22.005
117.999,21.999
5,1,hole
118.0015,22.0015
118.0025,22.0015
118.0025,22.0025
118.0015,22.0025
118.0015,22.0015
```

实际此例网格11×11、72有效节点、1个源点被孔洞排除；65次LSMR迭代、65303工作单位、已收敛且通过唯一性守卫。用第4节对角线切片实际49站、39缺测、完整底长null；10个有效站的已知底长约29.3812m。这个较大的缺测比例包括双线性模板靠孔洞时的保守失效，不能把它解释为原测深大面积空缺。去掉边界可验证无孔的连续剖面。

在当前工作机器、当前依赖版本的单次公开 `build_dtm` 实测（含投影/等深线/GeoTIFF）：

| 源点与网格 | 活动节点 | 迭代 / 工作单位 | 总耗时 / 求解耗时 |
|---|---:|---:|---:|
| 49点，27×26网格 | 584 | 147 / 1632609 | 0.0817s / 0.00426s |
| 121点，103×102网格 | 10004 | 727 / 143399397 | 0.1966s / 0.1822s |

性能例在原点22°N/118°E的AEQD平面均匀观测，水深 `100+0.000003x²+0.000005y²`；第一例 `x,y=-1200..1200` 步400m，第二例 `-5000..5000` 步1000m；网格100m、max_gap2000m、等深距10m、纯薄板默认权重与容差。两例显式设 `max_work_units:500000000`；约万节点例所需工作超过默认50M，不能隐去这项预算。耗时是单案例，不证明百万点性能或大海区精度。

`tests/test_dtm_extensions.py`包含：0/0.35/1张力平面重现（投影误差容差1e-4m），独立曲面留出误差，实际张力改变解，未收敛候选和预算拒绝，细/宽L、孤边、角连接、实际观测秩和无观测岛守卫，三种方法的BLN孔洞和等深线裁剪、GeoJSON孔洞、BLN XY/XYZ roundtrip、完整180×180栅格切片忽略假preview、解析坡度/底长、零权重缺测邻点、跨缺测及亚像素排除区断开、外部源未知收敛、四个实际HTTP接口有限JSON/422。当前地形59项通过（旧2项+新增57项）。独立21×21弯曲面中，36源点之外的内部节点上薄板RMSE小于0.2m，且小于线性三角网误差的15%；它只证明该合成函数的插值改进，不能推广为真实测深误差界。

运行 `python -m pytest -q tests/test_dtm.py tests/test_dtm_extensions.py`。运行依赖包含NumPy、SciPy、pyproj、Shapely，TIFF和等深线还需安装项目 `terrain` extra（rasterio、contourpy）。没有原厂MDB/GRD回写、多分辨率瓦片、大范围球面曲率、breakline强约束、独立边界张力、测深噪声校准、百万点吞吐保证或自动把剖面局部KP当作现有路线KP的能力。
