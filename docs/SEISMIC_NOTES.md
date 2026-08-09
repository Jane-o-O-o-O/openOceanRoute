# 应答器观测与均匀海流反演研究

独立实现 `oceanroute/seismic.py`：`predict_transponders(config)` 根据真实稳态缆形预测传感器位置；`estimate_current(config)` 用带测量协方差的观测反推共同的东/北向定常海流。结果均为 `validation_status: research`。它是有工作量上限的批次非线性估计，没有原厂算法、实时 Kalman 滤波或现场控制性能认证。

## 公开功能依据及范围

用户提供的 MakaiPlanPro 介绍第 9 页与 [Makai 官方 Seismic Module](https://www.makai.com/cable-software/seismic-module/) 描述了使用缆上应答器改善缆形/触底估计、强制缆形经过高精度观测、用多应答器 Kalman 估计海流，以及后台独立模型生成模拟观测。本模块独立实现可核查的**稳态观测算子与均匀海流加权反演**，覆盖其中的观测/参数估计研究流程；强制穿点、动态滤波、分层/时变海流估计、OBC 全程回收与设备选型能力尚未实现。

数值方法依据 [SciPy bounded least_squares 文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html)；局部协方差解释参照 [SciPy curve_fit 的绝对测量不确定度及线性协方差说明](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.curve_fit.html) 和 [NIST 非线性最小二乘统计定义](https://www.itl.nist.gov/div898/strd/nls/data/LINKS/c-nelson.shtml)。[SciPy chi2](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.chi2.html) 提供卡方上尾概率。公开公式不是对原厂精度的证明。

## 参考系、已知量与观测材料位置

必须明确 `reference_frame.kind:"local_enu"` 和 `origin_wgs84:[longitude_deg,latitude_deg,height_m]`；可给 `sea_surface_z_m`（默认 0）。这是所有船位/应答器共用的局部东 X、北 Y、上 Z 米制坐标及来源基准。软件**不将传入的经纬度自动当成 ENU 米制位置**，也不转换声学仪器自身坐标、不同测量原点或垂直基准。WGS84 原点保留为追溯元数据；用户须先完成实际坐标转换和基准一致性检查。

`line` 必须提供 `depth_m`、正 `wet_weight_n_m`、`diameter_m`、正 `bottom_tension_n`；`drag_coefficient` 默认 1.2、`water_density_kg_m3` 默认 1025、`nodes` 默认 128（16–500）。物性、水深、底端水平力幅值均作为已知量，没有在反演中同时拟合。湿重 N/m 是全浸水净向下重量，不是干质量。缆要求均匀、柔性、不可伸长、全浸水，海床为平床 `sea_surface_z_m-depth_m`。非零弯曲/升沉、混合材料、实体、动态状态、分层海流、变化地形和波浪等请求拒绝，不能悄悄降为均匀静态模型。

`snapshots` 包含 1–8 个独立稳态记录。每条要求唯一 id、`vessel_position_m:[x,y,z]`、已知 `ship_speed_m_s`（0–20）和 `heading_deg`；船端 z 必须等于声明的平均海面。`bottom_heading_deg` 默认船艏向，可独立指定已知底端力方位。方位为北 0°、顺时针。`time_s` 默认数组序号，若提供须严格递增；这里时间只是观测记录标签，**不表示已模拟船舶的动态轨迹或时序滤波**。

每条观测必须有快照内唯一 id，并二选一：

- `arc_from_vessel_m`：从当前船端向下的实际悬垂缆弧长 m；
- `material_m`：实物材料坐标，同时该快照必须给实际 `top_material_m`，换算 `arc=top_material−material`。材料坐标向船端增加，与当前局部材料模型的约定相同，但不自动关联其动态状态。

路线表面 KP、海床距离和材料坐标不能互换。不可伸长稳态近似忽略自然长度与拉伸后弧长的差别；实际伸长或放缆测量偏差没有作为未知量反演。每次评估的弧长必须在真实求解的悬垂段内；超出时拒绝整个请求并提示修正映射或缩小海流边界，不外推传感器、画在海床尾缆上或添加虚构位置。弧长不大于水深的测点能避开因候选悬垂长度变化造成的越界，但仍需核查真实测点是否在悬垂缆上。

## 观测算子及加权残差

算子直接调用已有 `repair.steady_tow` 的实际三维重力/法向二次阻力平衡求解，使用 `current−ship_velocity`。详见 [REPAIR_NOTES.md](REPAIR_NOTES.md)。节点从船端到海床末端排列；按该解的悬垂弧长，在节点间作分段线性位置插值，再加当前 ENU 船位。没有噪声或随机展示替代这个算子。节点数影响插值偏差，必须按观测精度进行网格收敛核对。

估计观测要求 `position_m:[x,y,z]`，某个缺测分量可为 null，但每项至少一轴有值；缺测轴从残差中删除，不填 0。每项必须给绝对标准差 `sigma_m:[sx,sy,sz]`，或对称正定 `covariance_m2` 3×3 矩阵，不可两者同时给。所有标准差至少 10⁻⁶ m，协方差最小特征值至少 10⁻¹² m²，条件数不得超过 10¹²。标准差不是 RMS 三维误差、置信区间宽度或系统标称百分比；这些数据必须先按实际定义转换。

预测模式不要求实测位置和误差；若给出二者则也返回实际残差。估计模式的不同应答器及不同快照误差被假定相互独立，同一观测内部各轴可以相关。共同 GPS 误差、声速/基线误差、材料弧长误差和时间相关噪声未建模；不能把重复的同源数据当成新的独立证据。

令实际算子位置为 `h_i(U)`，观测为 y_i、保留轴的测量协方差为 C_i，Cholesky 分解 `C_i=L_i L_iᵀ`。白化残差 `r_i=L_i⁻¹(h_i−y_i)`，目标为 `0.5 Σ |r_i|²`。采用 bounded trust-region least squares，只估计 `U=[current_x_m_s,current_y_m_s]`。没有正则先验、自动删点、鲁棒损失、Kalman 状态传播或硬性强制穿点。

## 可辨识性、协方差与接受状态

Jacobian 用实际算子有限差分，默认步长 10⁻⁴ m/s；接近边界采用可行侧差分。最终分别以 h 与 h/2 计算敏感度，矩阵相对变化超过 5% 时拒绝线性协方差。对细步 Jacobian 做 SVD，秩阈值 `max(10⁻⁸,最大奇异值×rank_relative_tolerance)`，默认相对容差 10⁻⁶。

位于船端（arc=0）的传感器位置由给定船位固定，对海流没有敏感度；零阻力同样不能估计流。单一分量/不足几何往往不能区分两个海流分量。几乎相同的切线方向、短悬垂范围或只有有限投影的测点可能病态；远离船端、不同弧长的空间测点以及已知不同船艏向的独立记录可提供更多信息，仍以实际 Jacobian 为准。不能根据“有三个应答器”就自动宣布可辨识。

纯二次速度阻力在相对流为零时一阶导数为零；有限差分可能产生随步长缩小的假敏感度。代码特意用步长加密检查，不能据很小的残差返回虚假的精确线性协方差。局部秩与步长检查也不证明非线性全局唯一性。

仅在优化收敛、秩为 2、Jacobian 稳定、没有活跃海流边界且残差符合给定误差时，`summary.estimate_accepted=true`。协方差为 `(JᵀJ)⁻¹`，标准差为对角线平方根，参数顺序明确东/北；使用绝对 C，不按实际残差将方差缩为零或强制 reduced chi-square=1。单位为 (m/s)²。无噪声合成观测仍有正的形式协方差，因为用户声明测量不确定度是正的。

观测 Mahalanobis 范数超过 `outlier_sigma`（默认 5）的点标记异常；该阈值是白化向量半径，不是每个原始坐标的独立 5σ。残差自由度为实测分量数−局部秩；有正自由度时，还按独立 Gaussian/局部线性假设计算卡方上尾概率，低于 `minimum_fit_pvalue`（默认 0.001）则不接受。自由度为 0 时没有冗余检验，结果明确告警。

未收敛、秩不足、边界解、Jacobian 不稳定或残差不一致时，保留实际 best-fit 数值和几何用于审查，但 accepted=false、协方差/标准差=null，并说明原因。非法输入、观测映射越界、底层求解失败或预算用尽则拒绝请求；不返回装饰性的成功状态。

形式协方差只传播所声明的测量噪声；缆参数、底力、测深、船位、材料位置和模型错误均未传播。它不是实船误差预算、传感器采购承诺或置信覆盖保证，低残差也可能由错误模型和参数补偿造成。

## 可执行例子

API 合同为 `POST /api/seismic/{predict|estimate}`，body `{"config": <对象>}`；HTTP 接线由应用层实现。下述 Python 示例既可生成清楚标记的无噪声合成观测，也可真实反演：

```python
from copy import deepcopy
from oceanroute.seismic import predict_transponders, estimate_current

config = {
    "reference_frame": {"kind": "local_enu", "origin_wgs84": [118, 22, 0]},
    "line": {"depth_m": 100, "wet_weight_n_m": 4, "diameter_m": 0.02,
             "bottom_tension_n": 250, "nodes": 128},
    "snapshots": [{
        "id": "steady-1", "time_s": 0, "vessel_position_m": [0, 0, 0],
        "ship_speed_m_s": 1, "heading_deg": 90,
        "observations": [
            {"id": "t0", "arc_from_vessel_m": 20, "sigma_m": [0.25, 0.25, 0.25]},
            {"id": "t1", "arc_from_vessel_m": 50, "sigma_m": [0.25, 0.25, 0.25]},
            {"id": "t2", "arc_from_vessel_m": 80, "sigma_m": [0.25, 0.25, 0.25]}
        ]
    }],
    "current_m_s": [0.25, 0.20]
}
truth = predict_transponders(config)
measured = deepcopy(config)
for obs, predicted in zip(measured["snapshots"][0]["observations"],
                          truth["snapshots"][0]["observations"]):
    obs["position_m"] = predicted["predicted_position_m"]
measured.pop("current_m_s")  # 固定真值不能作为估计模式参数
measured["initial_current_m_s"] = [0, 0]
fit = estimate_current(measured)
assert fit["summary"]["estimate_accepted"]
```

实际三点约为 `[-15.041835,3.537106,-12.697678]`、`[-37.752026,8.667167,-31.616384]`、`[-60.762198,13.484491,-50.251989]` m。从零初猜恢复 `[0.25,0.20]` m/s；该例约 29 次实际稳态求解、本机约 0.11 s。这仅证明程序用观测进行估计。真实使用须替换实测位置、物性和不确定度，不把合成记录标成声学仪器数据。

## 主要输出和计算预算

两种结果都返回 model、research 状态、明确参考系、规范化 `input_config`、假设、警告和 solver。`snapshots` 每项带船控/时间标签、真实 `nodes`/`node_tension_n`/`touchdown_m`/`suspended_length_m`/端力平衡；`observations` 带 id、弧长/材料位置、实际预测与观测位置、逐轴残差（缺测 null）、协方差、有效轴、白化残差和 Mahalanobis 范数。

估计另返回 `estimated_current_m_s`、初猜/边界、`parameter_order`、2×2 `parameter_covariance_m2_s2` 或 null、标准差、白化 Jacobian、信息矩阵及离群记录。`identifiability` 包含秩、奇异值、条件数（秩不足 null）、阈值、步长加密变化与活跃边界。`summary` 包含独立标记 estimate_accepted、测量数量、米制 RMS、初始/最终加权平方残差、自由度、reduced chi-square、拟合 p 值、误差一致性和 covariance_valid。`solver.converged` 仅是数值停止状态，不能替代 accepted。

预测必须给 `current_m_s:[east,north]`（各分量 ±20）。估计不得含此固定流输入，使用 `initial_current_m_s` 默认 `[0,0]`；`current_bounds_m_s` 默认 `{"lower":[-2,-2],"upper":[2,2]}`，各界限在 ±20，初猜须在界内。`max_evaluations` 默认 60（1–120）；有限差分额外调用不包含在 SciPy 的优化 nfev 内，另用实际计算计数和预算约束：`max_forward_solves` 默认 400（1–2000），`max_ode_evaluations` 默认 300000（100–3000000）。单次底层积分也保留既有 20000 次函数评价上限。结果报告实际 current_evaluations、forward_snapshot_solves 和 ode_function_evaluations。

每快照至多 64 个传感器，总计至多 256；完整输入 JSON 至多 2 MB。超过预算拒绝，不自动删观测/降网格或改变用户模型。可缩小参数范围、减少快照/迭代或在已明确上限内提高预算；必须先检查物理输入和可辨识性。

## 验证与尚缺工程证据

`tests/test_seismic.py` 包括独立无流解析悬链线位置基准、均匀流合成真值真实反演、固定 seed 的有噪声观测实际拟合、不同船速/艏向多记录、材料弧长对应、ENU 平移、绝对 σ 缩放/独立重复记录信息量、相关误差 Mahalanobis 平衡、缺测轴保留、秩不足和零流敏感度、离群/边界/迭代/预算失败、500 节点独立真值对较粗网格的估计偏差收敛，以及非法参考系/测量/物性/协方差和有限 JSON。两个实际 HTTP 接口另验证有限 JSON、真实节点/估计、秩不足仍未接受、非法值/工作预算 422 及未知工具 404。

这些验证不提供原厂黄金结果、实测声学定位误差、ADCP 对照、海试或设备接口证据。后续还需可授权真实数据、共同定位误差与材料映射误差建模、分层/时变海流可辨识性、动态状态估计、合理先验和独立工程验收；当前不能声称完成了原厂 Seismic 专项。
