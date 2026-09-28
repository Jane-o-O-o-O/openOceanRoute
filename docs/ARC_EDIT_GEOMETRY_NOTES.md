# 真实 WGS84 圆弧的端点编辑几何

本模块是独立几何方法。它以现有 `circular_arc` 声明为依据，修改实际端点后重新求圆心、起始径向方位与有向 sweep；不新增显示采样点，不用投影圆或端点弦替代真实路径，不承诺与原厂隐藏的约束算法相同。原手册物理页 195–200 描述人工编辑与 AC 整形，但没有提供本模块的求解算法。项目事务、制造库存、相邻弧联动及工程准入由调用者负责。

## 1. 公开输入与结果

```python
rebuild_arc_endpoints(start_new, end_new, geometry, *, config=None) -> dict
```

端点采用 `[longitude, latitude]` 或含这两个字段的坐标对象。坐标是 WGS84，经度 −180…180、纬度 −90…90 度。`geometry` 严格使用原来的六字段：

```json
{
  "type": "circular_arc",
  "schema_version": 1,
  "center": [0.0, 0.0],
  "radius_m": 1000.0,
  "start_azimuth_deg": 0.0,
  "sweep_deg": 90.0
}
```

半径域是现有 primitive 的 0.001…1,000,000 m；开放弧满足 `0 < abs(sweep_deg) < 360`，整圈为 ±360。正 sweep 表示圆心方位角递增。角度 fraction 不是物理弧长 fraction：真实长度仍由 GeographicLib 的 reduced length 积分决定。

配置字段与默认值：

| 字段 | 默认值 | 语义与合法范围 |
|---|---:|---|
| `branch` | `preserve` | `preserve`、`minor`、`major`、`left`、`right` |
| `full_circle_policy` | `reject_move` | 或显式 `preserve_endpoint_center_bearing` |
| `radius_m` | 原半径 | 可显式覆盖，0.001…1,000,000 m；原 descriptor 仍作为旧几何参考 |
| `original_endpoints` | 从原 descriptor 回算 | 可供事务调用者传入 `[old_start,old_end]`；会先独立验证原 descriptor 绑定，不能用虚假旧点绕过重构 |
| `max_work_units` | 20,000 | 整数 1…2,000,000，实际原生几何评估的总硬上限 |
| `max_iterations` | 96 | 每个中心标量根的迭代上限，整数 1…256 |
| `radius_tolerance_m` | 0.000001 | 1e−8…1e−3 m；实际再受下述相对上限约束 |
| `branch_angle_tolerance_deg` | 1e−8 | 1e−12…1e−4 度；近 180°或共线分支的数值不可分辨范围 |

未知字段、错误类型、非有限值、越界坐标或 descriptor 抛 `ValueError`。有效输入但不闭合、不可分辨或超预算返回 `accepted=false`、`geometry=null`、`rejection_codes` 与已实际完成的诊断；调用者应整笔拒绝，不能选用半个候选或回退为弦。成功时 `geometry` 恰为六个合法字段，可由原 `RouteSegment` 绑定给定的新端点。

返回顶层为 `model='wgs84-fixed-radius-arc-edit-v1'`、`accepted`、`geometry`、`evidence`、`budget`、`rejection_codes`、`warnings`、`assumptions`。没有生产状态写入。

## 2. 两个真实中心分支

令新端点为 A、B，目标半径为 R，二者最短测地线距离为 d，初始方位为 β。潜在圆心写作：

```
C(theta) = Direct_WGS84(A, beta + theta, R)
f(theta) = distance_WGS84(C(theta), B) - R
```

当 `0 < d < 2R` 且数值分辨足够时，在两个半圆区间 `[-180°,0°]` 与 `[0°,180°]` 分别求根。0°中心位于 A→B 的同一测地线上，故 `f(0)=abs(d-R)-R<0`；±180°的相反方向中心有 `f=d>0`。整个 R≤1000 km 范围使用唯一短测地线和正 reduced length；端点与潜在中心间最长距离≤3R。这个短域内三角形方向固定，距离对圆心参数的第一变分由 reduced length 和非退化三角形夹角决定，在各开放半圆保持单调；这给出两个有向中心分支。这里没有将任意半径、接近地球对跖点的全域圆宣称同样适用。

求根采用真实 Direct/Inverse 与括区 Brent 方法。每个根随后重新作两个圆心→端点的独立 inverse，获得径向起止方位；保持旧 sweep 的正负方向并按 modulo 360 计算新 sweep。默认选择与原 sweep 相同的 `minor/major` 分支，并核对原中心相对有向 A→B 弦的侧别；不会按“离旧中心最近”偷偷择根。`minor/major` 在本合同中指径向 sweep 绝对值小于/大于 180°，并非将椭球整圈的长度假定为 `2*pi*R`。

显式 `left/right` 指圆心在 start→end 最短测地线的左/右侧；它们不改变旧 sweep 正负方向。显式 `minor/major` 允许调用者有意识地换另一中心。旧近 180°开放弧在真正修改时不能唯一确定 minor/major，必须显式选择；近直径退化的两个中心若仍不可分辨，即使显式选择也拒绝。未移动的原 180°弧仍可通过原 descriptor 的真实验证。

显式 branch 选择即使端点坐标未移动也实际求对应中心，不会被“未移动”快路径忽略。原 descriptor 的快路径仅在 `branch=preserve` 时使用。事务服务宜提供真实旧端点 `original_endpoints`：descriptor 的 Direct 回算与已绑定端点可能差数纳米，这不等于用户真的移动了旧端点。引用仍须通过原半径、原起止方位的独立绑定验收。精确相同的 binary lon/lat 直接视为同点，避免 native Inverse 对同一坐标偶尔返回极小非零距离；没有把不同坐标通过宽公差归为“未移动”。

## 3. 整圈与不可闭合输入

整圈两端必须是同一真实位置。单端移动使两端不一致时返回 `ARC_EDIT_FULL_CIRCLE_NOT_CLOSED`，不能将整圈自动变为开放弧。未移动整圈可以原样验证。共同移动时，默认 `reject_move` 要求显式策略：

```
old_bearing = Inverse(old_endpoint, old_center).initial_azimuth
new_center = Direct(new_common_endpoint, old_bearing, target_radius)
```

`preserve_endpoint_center_bearing` 保持旧端点→圆心的初始方位、目标半径与原 ±360；重新求新圆心→新端点的初始方位。它不是保持原圆心，也不是地球表面不存在的全局欧氏刚体平移。其方位与生成位置在 evidence 中明确公开。两个新端点一致并不自动将开放弧升级为整圈：开放弧这种输入被拒绝。

整圈的 `branch` 必须为 `preserve`；minor/major/left/right 属于开放弧，不存在相应整圈分支，返回 `ARC_EDIT_FULL_CIRCLE_BRANCH_UNSUPPORTED`，不静默忽略显式请求。

## 4. 精度、拒绝与证据

两端的独立 inverse 半径误差与 Direct 绑定误差均受：

```
effective_tolerance_m = min(requested_radius_tolerance_m, R * 1e-4)
```

约束。1 mm 半径在默认值下的实际绝对 admission 上限为 100 nm，不继承大半径的宽绝对 tolerance。此上限不是保证所有纬度、所有退化输入都能达到同一相对精度；达不到则拒绝，不修改原半径或目标端点。proof 公开 requested/effective tolerance、相对上限及两类实际误差。

当 d 或 `2R-d` 不超过 `max(20 nm,16*ULP(2R))` 时，中心分支判为 `ARC_EDIT_INTERSECTION_UNRESOLVED`，不猜测共点或直径极限的唯一中心。若 native 括区符号、收敛、中心侧、sweep 或半径证明不能分辨，同样拒绝。这是明示的数值分辨边界；不把浮点结果当作带 directed rounding 的数学证明。端点太远则 `ARC_EDIT_ENDPOINTS_TOO_FAR`。迭代不足是 `ARC_EDIT_ROOT_NOT_CONVERGED`。

成功 proof 包含原半径与新目标半径、是否覆盖、原端点、原/新分支、方向、端点间距、两个实际中心候选与逐根残差、最终半径/绑定误差、实际弧长/解析长度上界、真实起终切线、原 primitive 的 solver 记录。失败仅公开已实际算出的字段，不伪造候选。切线为圆心→该点的实际 arrival 方位加 `sign(sweep)*90°`，不是端点弦方位。

## 5. 工作预算与实际验证

预算逐次 charge 本模块的 native Direct/Inverse，以及原 `RouteSegment` 在最终端点验证、reduced-length 积分和切线评估中的全部实际 native 调用。失败积分也 charge 已执行数量；到 cap 后拒绝，不扩预算或隐藏原 primitive 的工作。`budget` 中的 `work_units = direct_evaluations + inverse_evaluations + route_segment_work_units`。这是归一原生调用计数，不是 FLOPs、CPU 时间或 token 费用。

自有 `tests/test_arc_edit_geometry.py` 的 71 项针对验收已实际通过（0.37 s）。原 64 项首轮记录仍只属于首阶段；新增回归修复了同点 native 极小非零距离、未移动端点时显式 branch 被忽略和真实旧端点引用的独立验收。测试用预先构造的独立 PROJ/Geod 圆作为中心/端点 oracle，覆盖固定另一端点、双端编辑、正负有向 minor/major、极区/日期线、0.001 m 与 1000 km 边界、可分辨近直径分支、不可分辨/过远/共点拒绝、显式新半径、±360共同移位、硬预算与低迭代拒绝。独立极点平行圈 metric 与逐步加密的测地线弦长验证真实 m12 积分；仪器化实际 native 调用核对 charge 数量。此数字仅为模块专项，不能替代整个编辑事务、跨平台或现场精度验收。

## 6. 一手方法来源

- [GeographicLib：Geodesics on an ellipsoid](https://geographiclib.sourceforge.io/html/python/geodesics.html)：真实 Direct/Inverse、初末方位与 `m12*dalpha` 的横向位移定义。实际已读取。
- [Karney, Algorithms for geodesics, 2013](https://doi.org/10.1007/s00190-012-0578-z)：GeographicLib 官方说明所引用的底层测地线算法；不以其底层舍入精度声明替代本模块非线性求根和分支的真实误差验收。
- 现有 `docs/ROUTE_GEOMETRY_NOTES.md` 描述本项目的真实半径圆域、积分/反解和渲染；人工编辑仍使用同一个路径定义。
