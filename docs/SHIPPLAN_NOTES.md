# 船舶计划、前瞻比较与张力搜索

版本：独立研究工作流 v1，2026-10-04。实现：`oceanroute/shipplan.py`。公开函数为 `build_ship_plan(project, config)`、`look_ahead(project, config, scenarios)`、`optimize_tension(project, config)`。所有输出保留 `validation_status: "research"`。

这三个流程在已有规划与材料节点动态模型之上提供实际运算。动态窗口支持显式材料坐标的混合缆段与实体平移载荷，并可从真实保存状态继续和分支；项目KP到材料位置须明确映射。0.3新增 `/api/shipplan/prepare-voyage` 准备经校核的平床、同物性初态施工窗口，详见 [PLAN_VOYAGE_NOTES.md](PLAN_VOYAGE_NOTES.md)；它是显式预备流程，不能把任意初步计划直接当作完整动力状态。这些流程没有证明原厂等效精度，也没有自动完成全航次动态核验。基础力学模型、坐标和局限参见 [MODEL_NOTES.md](MODEL_NOTES.md)。

## 1. 初步船舶计划

`build_ship_plan` 首先调用规划内核，使用经核对的路由腿、测深剖面、缆型与装配长度。按 route KP 采样，插入转向、缆型边界和事件位置。每个有效正水深样本用 `steady_state` 估计海缆形状及船到触地点的平面偏移，再在 WGS84 上求出船舶位置。

连续偏移样本之间的船舶段用 WGS84 测地线连接，段时间等于船舶轨迹距离/设定船速；该段放缆速度由其预算缆长/实际段时间得到。因此平面路由 KP、船舶航行距离和物理放缆长度分别计量，水深变化导致的偏移变化不会默默丢失进时间预算。

转向或缆型边界可使稳态偏移发生变化。程序生成明确的 `offset_transition` 位置调整段，route KP 不增加、放缆率为零，移动时间按 `transition_speed_m_s` 计算。它是需要动态复核的初步动作；程序不保证这个转向动作的张力安全，也不声称它是最佳转向策略。

`additional` 附属体、路线 allowance 和区间 allowance 的额外实物长度，按声明 KP 停船放出。每个事件都有起止时间、位置、放缆率和放缆长度。`replace` 附属体已经计入基础区间缆长，不能再加一次；其 deployment pause 作为停船事件，但本版没有模拟实体质量、推进器或放缆机通过中继器的精确运动过程。用户显式定义的停船放缆会增加制造预算之外的材料，输出独立的 `operator_extra_cable_m` 和告警。

缺失水深或水深低于 0.001 m 时，悬空缆偏移返回 null，船样本暂放在底路由位置并告警，不把未知水深设为零。规划内核自己的缺失/近似测深告警也原样保留。

### 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `sample_spacing_m` | 5000 | 正采样间距，至多 1000000 m |
| `max_samples` | 500 | 5–2000；超过预算时增加普通采样间距，保留转向与事件 |
| `bottom_tension_n` | 1000 | 正稳态底部张力，至多 1000000000 N |
| `ship_speed_m_s` | 各缆型 lay_speed | 可统一覆盖，正值且≤20 m/s |
| `transition_speed_m_s` | 0.5 | 偏移调整段船速，正值且≤20 m/s |
| `body_pause_s` | 0 | 无单体 deployment_pause_s 时的停船秒数 |
| `event_payout_m_s` | 0.2 | additional/allowance 实物长度的停船放出速度，正值且≤25 m/s |
| `events` | 空 | `{kp_m,duration_s,name?,payout_m_s?}`，至多 1000 个 |
| `current_x_m_s/current_y_m_s` | 0 | 传入稳态模型的均匀海流 |
| `wet_weight_n_m/diameter_m` | 各缆型属性 | 可统一覆盖，否则按缆型选择 |
| `drag_coefficient/water_density_kg_m3` | 基础模型默认值 | 水动力参数 |

工程体属性可设置 `deployment_pause_s`，`stop_hours` 另外计入。区间 `stop_hours` 在区间终点安排暂停。不同缆型使用各自 lay_speed、湿重和直径。

### 输出

- `instructions`：完整操作段，包含 time_s、end_time_s、duration_s、speed_m_s、heading_deg、payout_m_s、kind、route KP、物理缆长、缆型和 WGS84 船舶起止位置。
- `ship_plan`：简化的时间/船速/艏向/放缆率指令，末尾加零船速、零放缆率的终止指令。
- `vessel_waypoints`：有时间和累积放缆量的船舶/底路由位置、偏移、缆型及稳态计算摘要。
- `target_route`：采样的底路由、KP 和水深；`events` 为显式事件列表。
- `summary`：总航行距离、route 长度、总时间、暂停时间、位置调整时间、装配长度、放缆量与守恒残差。

长路线的计划可跨越数日。简化指令列表能用于编辑和导出，但不能直接把数日控制时刻送入时长仅 30 s 的局部动态请求。预演需要选取时间窗口、把控制时刻平移到窗口起点，并匹配材料与实体。动态研究模型可通过 `ship_plan_horizon_s` 预存后续控制、从真实 `checkpoint` 继续多个窗口；节点和命令历史仍有计算上限，不自动合并整条路线的材料网格，也不能据此认为长计划的全部动作已经动态核验。

示例：

```python
plan = build_ship_plan(project, {
    "sample_spacing_m": 1000,
    "bottom_tension_n": 1500,
    "body_pause_s": 60,
    "events": [{"kp_m": 10000, "duration_s": 120, "name": "作业检查"}]
})
```

## 2. 前瞻分支与升沉情景

`look_ahead` 对 1–6 个局部情景调用实际 `simulate_lay`。每个情景用共同材料/实体、底部边界、水深、网格和初态；可修改船速、艏向、放缆率、定常/深度海流、船舶指令和规定升沉。结果逐分支检查初始节点、实际速度和材料坐标一致，并检查输出时间序列一致。

不提供 `resume_state` 时，分支从共同解析初态启动；修改初始船艏向导致缆形不同会拒绝。提供 `resume_state` 时，各分支恢复同一份真实求解器状态，包括速度、自然长度、材料、接触和船命令时基；可以在断点改变艏向而不旋转已有缆形。新船命令时间相对断点，完整合同见 [MODEL_NOTES.md 第 8 节](MODEL_NOTES.md#8-真实状态断点与续算)。工况控制改变可能产生过渡，需要解释短时响应。

各分支都使用相同 `dt_s`。新建初态情景含升沉时，共同输出间隔缩小到最短升沉周期/20。断点分支保留原输出网格，不在恢复时改变数值参数；顶部/单元峰值张力取内部积分步真实峰值，曲线仍可能较稀，并返回说明。内部积分由基础动态模型进一步细分。最小弯曲半径来自实际动态形状。该升沉只是船端规定正弦位移，没有波浪水粒子载荷、船舶 RAO、随机波谱或真实海况验证。

```python
comparison = look_ahead(project, {
    "depth_m": 100, "bottom_tension_n": 100,
    "duration_s": 10, "dt_s": 1, "nodes": 12,
    "ship_speed_m_s": 0.5, "payout_m_s": 0.5,
    "evaluation_window_s": 2.5,
    "include_frames": True
}, [
    {"name": "基线"},
    {"name": "横流", "overrides": {"current_y_m_s": 0.3}},
    {"name": "规定升沉", "overrides": {
        "heave_amplitude_m": 0.5, "heave_period_s": 4
    }}
])
```

各分支返回 config、metrics、solver、warnings 和最终 `checkpoint`；`include_frames=True` 时包含完整 simulation。metrics 包含最终窗口的平均底部张力、标准差、最终底部张力、本次时段顶部/单元峰值张力、最小离散半径、最终触地点和新增放出长度；`cumulative_paid_out_m` 保存全部历史累计放缆。相对于第一分支输出底张力变化和触地点位移。

已有真实断点的分支示例：

```python
comparison = look_ahead(project, {
    "resume_state": saved, "duration_s": 10,
    "evaluation_window_s": 2.5, "include_frames": True
}, [
    {"name": "保持当前命令"},
    {"name": "断点后转向", "overrides": {"heading_deg": 70}},
    {"name": "横流", "overrides": {"current_y_m_s": 0.3}}
])
continued = simulate_lay(project, {
    "resume_state": comparison["scenarios"][0]["checkpoint"], "duration_s": 5
})
```

### 平均张力的采样定义

评价区间为 `[end_time_s-evaluation_window_s, end_time_s]`；默认取本次时段最后四分之一，断点时间作为绝对时基保留，绝不积分断点之前的未知信号。按实际时间进行分段线性信号积分，并在窗口边缘插值，不使用简单帧平均。方差使用分段线性信号的平方积分。这样窗口位置不会随帧数变化，匀速线性信号也不会因粗细采样出现不同平均值。统一采样不能代替时间步收敛分析。

## 3. 基于动态计算的张力搜索

`optimize_tension` 在 `payout_min_m_s` 到 `payout_max_m_s` 间搜索一个正放缆率参数。每次候选都真实运行动态模型、计算同一末尾窗口的平均底部张力，再与目标比较。

算法先计算下界、上界、范围内的原始放缆率和中点，再反复细分当前最佳候选相邻的较宽区间。它不预设底张力与放缆率严格单调，不声称求出全局最佳值。只有基础离散求解 `solver.converged=True` 的候选可成为最终推荐；被拒绝的候选记录在 `search_failures` 中。目标没有达到时保留 `target_met=False`、实际 achieved/error 和告警，不用目标值替代测量结果。

对于 ship_plan，显式零放缆暂停保持零；其他显式正放缆率统一换为候选值，未显式给出放缆率的后续指令按动态模型的继承规则运行。返回完整 `optimized_config`，可直接重新计算核对。

提供 `resume_state` 时，各候选恢复同一真实状态，只搜索断点后的放缆率。已经发生的命令不重演；当前及未来控制转换为相对断点的完整指令，零放缆事件仍保留。候选峰值和评价窗口只覆盖本次搜索时段。

| 新增参数 | 默认值 | 说明 |
|---|---:|---|
| `target_bottom_tension_n` | 必填 | 0–100000000 N |
| `payout_min_m_s` | 0 | 0–25 |
| `payout_max_m_s` | 船速/原放缆率的 1.5 倍，最低 0.1、最高 25 | 必须大于下界 |
| `max_evaluations` | 9 | 3–16，包含被拒绝的候选 |
| `tension_tolerance_n` | max(10,目标的 5%) | 正值；判断目标达到与否 |
| `evaluation_window_s` | 时长的四分之一 | 正值，不超过时长 |
| `include_frames` | False | True 时附上最佳方案完整动态结果 |

输出包括选中放缆率、目标、实际平均底张力、误差、RMS 误差、容差、是否达到目标、最佳方案指标、逐次计算记录、失败记录和可复算 config。RMS 误差综合平均偏差与窗口内张力波动，优化目标本身仍为平均偏差。

具体程序验证例：水深 100 m、初始底张力 100 N、船速/原始放缆率 0.5 m/s、时长 10 s、12 节点，搜索 [0.3,0.8] m/s、目标 80 N。9 次计算找到约 0.3375 m/s，末尾 2.5 s 平均底张力约 79.839 N；推荐 config 独立重算得到相同结果。这证明程序进行了实际搜索和采样核对，并非证明此方案可在海上达到相同张力。

这个流程是局部参数搜索，不是带传感器误差、执行器动态、稳定性分析和连续反馈的 Auto-Tension 控制器。原厂 Power/Look Ahead 宣传的完整功能仍未由此证明完成。

## 4. 计算边界与验证

情景与搜索使用局部动态限制：duration≤180 s（优化≤120 s）、新建初态 6–48 初始节点、2–24 次每步迭代、内部步长 0.025–0.25 s；恢复断点保留原动态初始节点≤100、当前节点≤256、2–40 次迭代和 0.002–0.25 s 内部步长。最多 1000 输出间隔。整批预估 `steps*actual_nodes*iterations*runs`≤6000000，逐次 max_work_units≤6000000/runs，含波浪和运动表时也计入基础动态计算预算。超过范围明确拒绝请求，不更改保存物理状态或步长。

运行：

```sh
.venv/bin/python -m pytest tests/test_shipplan.py tests/test_simulation.py tests/test_heterogeneous.py tests/test_checkpoints.py -q
```

新增 31 项验证覆盖：直线定深船舶距离/时长/放缆守恒、混合缆型和转向连续性、附加材料单次计入、替代附属体和暂停、未知测深、采样预算、时间窗口积分的采样一致性、前瞻共同初态/时间网格、实际规定升沉结果、真实张力搜索与独立复算、不可达目标的失败状态、零放缆暂停保留、不修改输入、非法数据及计算上限。

另有断点回归验证普通/任意保存时刻续算一致、真实速度保留、混合材料/实体/新增节点/启停转向/升沉的共同状态、浏览器 JSON 往返、断点前瞻与搜索独立重算，以及不兼容状态和边界修改拒绝。

独立的 [SEA_NOTES.md](SEA_NOTES.md) 描述有限随机波谱、用户垂向 RAO 表、Airy 流速接入与真实 Monte Carlo 局部参数传播；它们不自动提供实船标定数据。

尚未完成：连续自适应张力控制、实体完整刚体动力学、全航次材料网格管理、非线性波浪和完整船舶响应、真实施工记录重建、硬件误差/设备选型概率预算，以及海试或独立模型校验。

## 5. 产品功能来源

[MakaiPlan Pro 公开官网](https://www.makai.com/cable-software/makaiplan-pro/) 与用户提供的 `MakaiPlanPro.pdf` 描述了稳态初步船舶计划、动态改进、Look Ahead 和规定作业情景；[Power Module 公开官网](https://www.makai.com/cable-software/power-module/) 描述了张力与船舶运动分析的产品方向。这些资料用于功能拆分，没有提供本项目所需的私有算法、标定参数或可直接复用的商业源代码。本模块通过已有公开力学基础独立实现计算流程。
