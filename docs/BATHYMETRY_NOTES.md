# 二维海底场与节点接触研究模型

这是开发中的独立研究功能。它真实进入材料节点动力求解，不等于 Makai 原厂算法、船上实时系统、完整海底连续接触或工程载荷认证。冻结的 0.3 源码包/PDF 不包含本轮开发修改。

## 输入与参考系

`simulate_lay(project, config)` 新增 `config.seabed_grid`；不能同时给 `seabed_profile`。接口仍可通过已有 `POST /api/simulation/dynamic` 的 `{config}` 载荷调用。物理模块不承担 DTM 投影采样或多来源拼接。

```json
{
  "schema": "oceanroute.bathymetry.v1",
  "x_m": [-100, 0, 100],
  "y_m": [-100, 0, 100],
  "z_m": [[-12,-10,-8],[-14,-12,-10],[-16,-14,-12]],
  "source": {
    "name": "explicit synthetic plane: z=-12+0.02*x-0.02*y",
    "horizontal_crs": "LOCAL_CARTESIAN_METRES",
    "origin_projected_m": [0,0],
    "vertical_datum": "already aligned to model mean sea surface z=0"
  }
}
```

数组行对应**递增 y**，列对应**递增 x**；x 向东、y 向北，均为局部米。`z_m` 是相对于模型海面 **z=0** 的向上高度，海底必须在 `[-12000,-0.001]` m；缺测写 JSON `null`。这与 GeoTIFF 常见的自北向南行序不同，桥接必须实际重排。水平轴无需等距，每轴 2–1000 点，总计最多 10000 节点；间距至少 0.001 m、单轴范围最多 1000 km，坐标绝对值不超过 1e7 m。已知单元四角的梯度模长不超过 5，垂直断崖与倒悬地形不属于该高度场。

`source` 必须包含非空 `name/horizontal_crs/vertical_datum`（各至多 1000 字符）与两数 `origin_projected_m`。只接受这四键与可选 64 位十六进制 `sha256`。LOCAL 只允许 `[0,0]` 原点；投影来源需要 pyproj 可解析、两个 east/north 水平轴、单位确实为米的投影 CRS，拒绝地理经纬度与英尺。轴坐标**输入时就已是局部米**：若来源为投影 E/N，调用者须先做 `x=E-E0,y=N-N0`；本模块不把 provenance 原点再加减一遍。检查轴单位依据 [pyproj CRS/coordinate-system 文档](https://pyproj4.github.io/pyproj/stable/api/crs/coordinate_system.html)。

`vertical_datum` 只记录来源，不能自动把海图基准、椭球高、潮位或其他深度基准换成模型海面；调用者必须已完成所需校正并明确声明。`source.sha256` 也是外部来源摘要声明，本模块不会把任意声明认证为网格 hash；断点的一致性校核覆盖整张实际网格及来源标签。

## 双线性值、缺测与法向

新增 Python 对象 `BathymetryGrid(document)`：`evaluate(points, gradient=False)` 返回高度数组；`evaluate(points)` 返回 `(height, gradient_xy)`，`surface(points)` 返回 `(height, unit_normal_xyz)`，`metadata()` 返回完整网格和覆盖策略。points 是局部 `[x,y]` 或 `[x,y,z]` 行，z 不参与采样。

每个矩形单元令 `t=(x-x0)/(x1-x0),u=(y-y0)/(y1-y0)`；高度由四角乘 `(1-t)(1-u),t(1-u),(1-t)u,tu` 求和，两个导数由该多项式直接解析计算。法向 `n=(-f_x,-f_y,1)/sqrt(1+f_x²+f_y²)`，向水侧。单元交界的导数一般不连续：内部结点选择右/上单元，最大边界选择左/下单元；法向不是额外平滑过的曲面。

单独的 height-only 查询只要求非零插值权重的角点已知，因此一个已知节点可单独取高；**梯度/法向以及所有动态查询要求所选单元四角全部已知**，不会从缺角猜法向。网格必须至少有一个完整已知单元。域外、NoData 或船端/悬垂节点脚下缺测均抛 `ValueError`，不存在边界外推、持平、补零或改用原 `depth_m` 的成功路径。此检查作用于节点及初始化背向射线；没有声称每个材料段的中点都得到连续碰撞检测。旧 1D profile 仍按原合同沿 y 挤出并在 x 域外保持端点值，不得把这一旧行为混入新 2D。

## 实际求解选择

轴向和弯曲仍使用既有材料节点模型。每次轴向迭代后求解点与刚性高度场的局部单侧约束。高度差 `C=z-f(x,y)`，局部近似法向间隙 `phi=C*n_z`；自由节点逆质量 `w=1/m`，累计正常乘子 `lambda>=0`。局部修正为：

```text
delta_lambda = max(lambda - phi/w, 0) - lambda
p += w * delta_lambda * n
lambda += delta_lambda
```

每次修正重新取当前实际单元的高度和法向，因而正常移动可以跨 x/y 单元；不是只提升 z 的显示后处理。每轮最多 16 次局部修正，初始化最多 64 次；跨单元无法收敛或修正进入未知/域外则明确失败。这个单位法向线性化在平面上准确，在弯曲双线性单元上是迭代近似，**不是全局最近点投影或完整非光滑接触 Newton 法**。船端与原锚点是运动学边界，不把它们的约束力当自由节点海床反力。累计乘子、质量加权位置修正和 `lambda/h²` 力估计的出发点来自 [Macklin 等 XPBD 原论文](https://mmacklin.com/xpbd.pdf)，此处自行实现高度场接触，没有原厂参数。

位置更新与轴向速度阻尼后，接触自由节点删除内向法向速度，增加 `J_v=m*max(-v·n,0)`。总正常冲量 `J_n=lambda/h+J_v`。切向速度 `v_t=v-(v·n)n`，摩擦冲量 `J_t=-min(m*|v_t|,mu*J_n)*v_t/|v_t|`；静止时取零。它真实改变后续节点速度与几何，满足切向性和 `|J_t|<=mu*J_n`，且该摩擦操作不会增加动能。

这是**速度级滑动/停止近似**，没有完整位置级静摩擦粘着互补求解，可能产生数值蠕动。位置约束修正不能由速度阻尼完整代表静摩擦，方法学区别见 [Bender/Müller/Macklin 2015 PBD 教程](https://mmacklin.com/EG2015PBD.pdf)。正常力是最后内部步冲量除以该步时长；碰撞峰值、粗糙结点法向和张力均必须比较步长/网格/迭代数，残差通过不能认证工程载荷。

初态先沿初始船向反方向，在已知单元射线上求“水平触地点悬链线 layback 与位置”根，再把内部相交节点正常投影到床外。根搜索按网格交叉点/区间中点检查覆盖，最多 100 次 Brent 迭代；无足够范围或遇到缺测就失败。零底张力用船下垂直极限。结果的 `initialization.slope_equilibrium=false`：这是有真实物料/预应力的启动形状，**不是坡面静力平衡**；混合缆和附属体仍需要原有启动收敛分析。canonical checkpoint `depth_m` 是实际求得的初始触点深度。

## 输出与完整状态恢复

仅 2D 动态使用 `model=material-lumped-mass-xpbd-cable-lay-v3`，`validation_status=research`。顶级 `seabed` 包含完整 `grid`、`interpolation=bilinear`、`coverage_policy=reject_outside_or_missing_cell`、`normal_policy`、`grid_nodes/known_cells/bounds_m/vertical_reference`。它不是供物理求解替代全栅格的抽稀预览。

2D 每帧新增：

|字段|实际含义|
|---|---|
|`node_seabed_z_m`|每个当前材料节点脚下床高，n 数组|
|`node_seabed_normal`|当前单元水侧单位法向，n×3|
|`node_contact_mask`|实际高度差 ≤1e-8 m，n 个 boolean；含运动学锚点|
|`node_contact_normal_impulse_n_s`|最后内部步自由节点总正常冲量，n 非负数；船/锚为0|
|`node_contact_friction_impulse_n_s`|最后内部步实际切向摩擦向量，n×3 N·s|
|`last_contact_step_s`|上述冲量所属步长；初始无内部步时为0|
|`energy`|`kinetic_j/submerged_gravity_potential_j/axial_elastic_j/cumulative_contact_friction_dissipation_j`|

前三项能量只是当前等效节点动能、湿重势能 `sum(W*z)`、拉伸弹性能 `sum(EA/(2*rest)*max(length-rest,0)²)`，弯曲能没有并入；有船端做功、海流、阻尼、放缆的开系统不能用其相加宣称总能量守恒。摩擦耗散是每一步仅该摩擦冲量操作前后的实际动能差，累加非负值。

`summary.contact` 四项累计量：`max_contact_normal_force_n`、`max_contact_penetration_m`、`max_inward_contact_velocity_m_s`、`friction_dissipation_j`。`solver.contact` 记录 method、局部迭代上限、当前run实际正常投影扫数、实际双线性节点采样数 `actual_bilinear_node_queries_this_run`、穿透/内向速度容差和 friction_model。整体 `solver.converged` 同时要求既有轴向残差以及正常穿透/内向速度不大于 1e-8（m、m/s）；仍只代表这些数值守卫。

2D 断点用原 schema 名、`schema_version=2`、model-v3、`numerical.scheme=implicit-compliant-material-nodes-2d-contact-v3`。整张 `seabed_grid` 冻结；恢复不可换网格、datum、来源或数值步长。保存原有所有真实位置/速度/自然长/物性/材料/命令/边界，另保存上列正常/摩擦冲量、实际法向、所属步长和四项累计接触统计。读取还校验实际节点覆盖、法向、接触mask、单侧性、切向/库仑限幅与运动学边界冲量，不由最后显示帧猜状态。旧 `schema_version=1/model-v2` 的无-grid平床/1D断点继续走原路径；不能假升级或在旧断点中添加 grid。

## 可执行小例、预算与限制

将上面 grid 对象置于以下 config 的 `seabed_grid`：

```json
{
  "depth_m":12,"wet_weight_n_m":4,"bottom_tension_n":10,"nodes":12,
  "ship_speed_m_s":0.3,"payout_m_s":0.35,"heading_deg":90,
  "duration_s":2,"dt_s":0.25,"internal_dt_s":0.025,"solver_iterations":24
}
```

实际首轮 smoke：80 内部步、2 个最终接触节点、最大正常穿透约 3.55e-15 m、最大内向正常速度约 7.3e-18 m/s、正常力峰值约 13.71 N、摩擦耗散约 0.00554 J，材料余额误差浮点量级。数值是该明确合成案例的证据，不是精度或其他环境的承诺。

仍保留 256 实际节点、30000 内部步、2001 帧、每次 normalized work 上限 12M、单断点 2 MB、批量断点估计 16 MB 等既有限制。2D预算额外按每步每预计节点 `32*solver_iterations+8` 计双线性采样上界：每轮至多16个局部正常投影，每次修正前后两次采样；另计每预计帧4次节点采样、初始化64轮投影的128次节点采样、网格交叉点/中点与100次Brent求值上界。即使很多点未接触也预先计入，并报告实际采样数与上界。`solver.work_basis` 明确这是归一化节点约束/物料/波浪项加节点插值次数，并非FLOPs、全程序操作成本或运行时保证；CRS验证、JSON编解码、渲染不计其中，另有输入/输出体积上限。voyage各块累计真实返回的同一 `estimated_work_units`，不能只用旧node×iterations忽略2D采样。窄网格/高分辨率NoData边缘/反复跨梯度折角会实际拒绝，不扩大预算或补平床来“完成”。

验证包含非均匀网格仿射与双线性解析值/梯度、真实跨单元投影、NoData与域外失败、跨/沿坡材料节点非穿透与法向速度、摩擦限幅/耗散与不同滑动几何、弯曲双线性格不同法向、完整中途断点连续轨迹一致、旧模型回归。一个显式 `EA=1e4 N`、坡 `z=-12+0.2x+0.1y`、16节点、2s、speed=payout=.2 的研究例：内部步 .04/.02/.01 s 的相邻最终位置差约 .00607/.00396 m，最终张力差约 .746/.225 N，均下降。不能把这个较柔弹性例套用于默认 EA=1e8；高刚度/放缆转态的正常力峰值和瞬时张力可能明显依赖步长，必须单独收敛研究。

本轮只支持 `simulate_lay` 中真实 2D床场，catenary/steady_state/span_analysis 给 grid 明确拒绝；1D span仍需显式提取一条线。海况模块可把已有用户RAO船端升沉序列送入 dynamic，但任何 grid 加 constant-depth `wave_kinematics` 都拒绝，尚无变深波传播/折射。长航程允许连续完整状态块的范围由 voyage 管理；旧平床粗化不适用 grid。未实现缆径/段间连续碰撞、刚体附属体转动和实体接触、埋设/土体阻力、海底黏着、三维流固耦合、潮位自动校正、工程校准与全海区精度保证。
