# 海况、用户 RAO 与研究误差预算

版本：独立线性海况研究模块 v1，2026-10-04。实现 `oceanroute/sea.py`，公开函数 `generate_sea_state(config)`、`simulate_sea(project, config)`、`monte_carlo(project, config)`。每项结果都保留 `validation_status: research`。它们真实驱动现有材料节点模型，不代表原厂 Power Module 等效实现、实船 RAO 或工程风险认证。

## 1. 波谱与随机相位

JONSWAP 使用频率形状 `f^-5 exp[-1.25(fp/f)^4] gamma^r`，其中 `r=exp[-(f/fp−1)^2/(2 sigma²)]`，sigma 在峰频左/右分别取 0.07/0.09；PM 使用 gamma=1。有限频带分成等宽区间，在中点评价形状，再归一化到 `Σ S(fj) Δfj=(Hs/4)^2`。单分量振幅 `aj=sqrt[2 S(fj) Δfj]`，相位由声明 seed 的 NumPy PCG64 生成器确定。这里指定的是有限带总方差，不能把它当成未截断谱的风速/风区传播模型。

`regular` 的 `hs_m` 表示波峰到波谷高度，振幅为 hs_m/2；因此规则波的 `Hm0=4sqrt(m0)` 是 `sqrt(2)*hs_m`，不会错误把两种波高定义混为一谈。`custom` 允许明确逐分量的频率、振幅、相位和传播方向。

有限时序的实测标准差通常不同于谱的总体标准差。程序分别返回二者，不用有限记录反向缩放随机曲线强行达到指定波高。谱分辨率、截断频带和模拟时长都需要加密/延长核对。

## 2. 用户 RAO 与船端运动

`heave_rao` 由用户提供，行格式 `{frequency_hz, amplitude_m_m, phase_deg}`。振幅单位为垂向响应 m/波幅 m；正 heave 向上，正 phase 表示相对当地波峰的滞后：`R=A exp(-i phase)`。程序在复数的实部/虚部上按频率线性插值，避免 179° 到 −179° 的相位错误折返；不作频带外外推。任何实际船舶 RAO 都需要说明船型、吃水、速度、方向和原始相位规范。

传播方向采用北 0°、顺时针；它是波传播去向。海面相位为 `theta=omega(t−time_origin)+phi−k direction·(xy−spatial_origin)`。船端的位置来自实际分段船速/艏向指令，因此相位包含运动遭遇效应。用户表仍按原始波频率使用，不求解随船速/相对方向变化的多维 RAO；不能把这一表当作全部方向和速度的实船响应。

生成 `z=sum Re(aj Rj exp(i theta_j))`，再减去起点响应，使首位移为 0。这个常量移位匹配解析或保存初态，`initial_heave_dc_shift_m` 明确报告它。高分辨率表作为 `vessel_motion_series=[{time_s,heave_m}]` 输入实际动态求解器；内插是分段线性，积分步上限包含运动表最短间隔。它规定的是缆端垂向运动，没有船舶平移/旋转刚体求解、舷侧杠杆臂或放缆机模型。

缺失用户 RAO 时，`generate` 仍返回波面、谱与分量，但运动表为 null，`simulate` 明确拒绝；只有显式 `fixed_vessel_heave:true` 才采用固定船高。固定模式优先于保留的 RAO 表，方便切换后仍保留用户数据；此时无效的 rao_scale 误差传播会拒绝。没有默认的虚构船舶响应数据。

## 3. Airy 水粒子速度及动力学接入

每个分量从 `omega²=g k tanh(kd)` 数值解波数。水平速度幅值按 `aj omega cosh[k(d+z)]/sinh(kd)` 衰减，垂向按 `aj omega sinh[k(d+z)]/sinh(kd)`；采用上述 theta 时水平为 cos(theta)，垂向为 −sin(theta)。海床处垂向速度为零，海面垂向速度与波面时间导数一致。实现用指数比值与 expm1 防止深水大 kd 的双曲函数溢出。

`include_fluid_kinematics:true` 默认将这些速度加到海流，再进入既有法向二次阻力和实体各向同性阻力。**未包含波浪加速度惯性项、Froude–Krylov 力、波浪压力、波浪辐射/绕射或瞬时浸没变化。** 模型仍使用全浸水重量和质量。平均水面以上的速度保持表面值，平均海床以下保持底部值，并明确说明；当前 Airy 接入要求与声明水深一致的平海床，变化海床请求拒绝。

分量波陡或振幅/水深比超过保守小振幅范围时返回告警，不能据此证明线性波仍有效。波浪破碎、浅水非线性、多方向船舶 RAO、随机海況标定、疲劳和极端尾部风险都不在本实现范围。

## 4. 输入、输出和可执行例子

本地 API：`POST /api/sea/generate` 的 body 为 `{"config": <平铺海况参数>}`；`POST /api/sea/simulate` 和 `POST /api/sea/montecarlo` 的 body 为 `{"project": <当前工程>, "config": <下述 simulation/sea_state 两对象及评价参数>}`。错误物性/缺 RAO/计算越界返回 HTTP 422。API 已通过真实 TestClient 的有限 JSON 编码与状态续算回归。

`generate` 的主要字段：spectrum 默认 jonswap；hs_m 默认 1（0–15），tp_s 默认 8（0.5–60），gamma 默认 3.3（1–10）；component_count 默认 32（4–128），频带默认 0.3fp–5fp 且限制在 0.005–2 Hz；seed 默认 2026（32 位非负整数）；duration_s 默认 30，sample_dt_s 默认 0.1；depth_m 默认 100；wave_direction_deg 默认 90。实际采样还受分量及保守遭遇频率每周期至少 40 点限制，至多 10001 点，样本×分量≤1500000。

`simulate` 输入包含 `simulation` 和 `sea_state` 两个对象。船舶路径、时长和水深来自 simulation；传入 resume_state 时恢复真实位置与绝对船命令，重新生成海况表的时间从累计断点时刻开始。`wave_kinematics.time_origin_s` 是绝对相位原点，vessel_motion_series 的 time_s 是从本次断点起算的局部时间。直接从原动态断点继续且不提供新运动表时，保留原表原点/高度偏置，表必须覆盖继续时长。保存原表的全程计算与中途恢复保留相同状态；重新生成不同采样网格的运动表仍可能产生正常插值差异，需要采样加密。

```python
config = {
    "simulation": {
        "depth_m": 30, "bottom_tension_n": 100,
        "duration_s": 2, "nodes": 8, "dt_s": 0.25, "internal_dt_s": 0.05,
        "ship_speed_m_s": 0, "payout_m_s": 0
    },
    "sea_state": {
        "spectrum": "regular", "hs_m": 0.2, "tp_s": 4,
        "wave_direction_deg": 90,
        "heave_rao": [{"frequency_hz": 0.25, "amplitude_m_m": 1, "phase_deg": 0}],
        "include_fluid_kinematics": True
    }
}
sea_run = simulate_sea(project, config)
```

结果包括 sea_state 的谱、分量、波面/响应时序、生成的运动表与水运动配置；simulation 为实际动态帧、状态断点与求解状态；metrics 为同一末尾窗口的平均底张力、顶部/单元内部步峰值、最小离散半径和触地点；effective_simulation_config 可直接调用 simulate_lay 独立复算。首次位移为 0，规则波例在 2 s 的船高为 −0.2 m，0 与 −0.2 m 的 DC 选择已在上文说明。

## 5. Monte Carlo 参数传播

输入保留上述 nested config，增加 runs（默认 8，2–24）、seed、uncertainties、vary_wave_phases（默认 False）和 include_frames（默认 False；完整帧至多 8 次）。每个参数必须给 scope、parameter、distribution、lower/upper。uniform 在闭区间内抽样；normal 另外给 mean/std，用有界正态的逆 CDF 抽样，不以裁切替代分布。所有参数相互独立，相关误差与时间相关噪声没有建模。

支持 simulation 的 ship_speed_m_s、payout_m_s、heading_deg、current_x/y_m_s，及 sea_state 的 hs_m、tp_s、gamma、wave_direction_deg、rao_scale。它们是绝对参数；显式船命令覆盖标量时，标量控制的随机化会造成歧义，明确拒绝。custom 分量中无效的 hs/tp/gamma/方向、PM/regular 中无效的 gamma 也拒绝。保存状态仍不允许更改材料、海床或数值参数；分支控制遵循断点合同。

```python
budget = monte_carlo(project, {
    **config, "runs": 4, "seed": 42,
    "uncertainties": [
        {"scope": "simulation", "parameter": "current_y_m_s",
         "distribution": "uniform", "lower": -0.3, "upper": 0.3},
        {"scope": "sea_state", "parameter": "rao_scale",
         "distribution": "normal", "lower": 0.8, "upper": 1.2,
         "mean": 1, "std": 0.1}
    ]
})
```

先计算声明参数的基线，再逐次运行真实动力学，不用随机条形图代替计算。各次返回输入、真实 metrics、solver 和 warnings；可选完整帧。distributions 给平均底张力、峰值、半径、放缆和相对基线触地点偏移的均值/样本标准差/5%、50%、95% 分位。统计只使用收敛成功的结果；失败与未收敛分别保留，summary.complete=False，绝不用编造值填补。少量样本的分位是探索性的模型结果，不是覆盖率保证、平均值置信区间或真实施工故障概率。

没有误差参数且不改变波相位时，所有真实试验与基线一致、传播方差为零；这验证数据与结果的对应关系。vary_wave_phases=True 通过记录的独立种子变化随机波相位；规则波和 custom 则明确重新抽取并记录 phase_deg，实际环境也发生变化，参数传播与环境随机化明确区分。这里没有自动推断 GPS、ADCP、张力传感器或放缆机精度，未实现信号滤波、执行器动态及反馈控制，也未完成原厂硬件误差选择工具。

时间、节点、积分次数和样本量沿用基础动态/局部工作流限制。动态 max_work_units 可进一步收紧每次预算；海况分量计算计入预估工作量，批次也按剩余预算限制每次求解。大量波分量、短周期、长时段或大量试验会明确拒绝，不能以减少物理载荷或更改已保存步长来偷偷通过。

## 6. 验证与公开方法来源

`.venv/bin/python -m pytest tests/test_sea.py tests/test_sea_api.py tests/test_checkpoints.py tests/test_simulation.py tests/test_heterogeneous.py tests/test_shipplan.py -q`

覆盖单频波面与 RAO 振幅/相位解析解、相位折返复数插值、波速色散、海床零垂向速度、表面速度与深水有限性、Hs 方差归一化、PM 与 gamma=1 一致、seed 可复现、船速遭遇频率、零谱与静水一致、RAO 与流速分别改变实际缆动力学、原运动表断点续算、运动插值加密、Monte Carlo 固定输入零传播方差、抽样范围、独立逐次复算、实算分位及失败记录。

- [SIMA 官方 JONSWAP 公式](https://simasite.azurewebsites.net/docs/latest/simo/modelling/environment/waves/Jonswap.html)：峰形、峰频及左右 sigma 参数。本项目采用声明频带的离散方差归一化。
- [SWAN 官方变量定义](https://swanmodel.sourceforge.io/online_doc/swanuse/node35.html)：Hs 与方差谱积分的关系。
- [Orcina 官方 RAO 与相位](https://www.orcina.com/webhelp/OrcaFlex/Content/html/Vesseltheory%2CRAOsandphases.htm)：响应与波幅关系、相位滞后及船型/吃水/速度/方向条件需要说明。本项目仅使用用户垂向表。
- [Orcina 官方 Airy 运动与拉伸](https://www.orcina.com/webhelp/OrcaFlex/Content/html/Waves%2CKinematicstretching.htm)：有限深水平运动与平均水面以上近似边界。
- [MoorDyn 官方水运动输入](https://moordyn.readthedocs.io/en/latest/waterkinematics.html)：波分量及网格/时间插值需核查分辨率。本项目直接分量叠加，不调用 MoorDyn。
- [NIST Uncertainty Machine](https://uncertainty.nist.gov/)：声明输入分布并传播到模型结果。本项目是独立、少量且有预算限制的研究试验，不能因此宣称符合完整 GUM 认证或设备误差标定。
