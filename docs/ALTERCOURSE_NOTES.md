# Split AC 与 Radius AC 独立几何工具（0.11开发阶段）

本模块对应公开手册物理页195–198（印刷页187–190）。Split AC 文字要求指定最大转角和最小转弯距离，生成相同转角、相同内部KP距离；Radius AC 文字与窗口描述新增一个点并移动原点，使圆弧连接二点且入出航段方位不变。物理页198还单列“Radius - Fix points - convert to rhumb lines”；圆弧和显式恒向线转换不能混为一项。这里只实现独立WGS84几何模型和已有制造／约束关系的候选重排，不认证原厂算法、文件格式或工程测量精度。

## 调用与候选

```python
split_altercourse(project, {
    "point_id": "bend",
    "max_turn_angle_deg": 30,
    "min_turn_distance_m": 100,
})
radius_altercourse(project, {"point_id": "bend", "radius_m": 200})
```

返回对象为 `{project, report, warnings}`，不写库。只能明确选择内部Rigid转角：用户调用工具授权移动该Rigid；Clamped／Sliding不会被自动解锁。路线端点、相邻零腿、180度折返和数值未解的有限域明确拒绝。`point_id`非空字符串最长256字符；最大转角严格大于0、小于180度；最小内部距离、半径均为0.001–1000000米，不能自动缩小。很小合法参数并不保证浮点几何可解，必须通过实际结果验收。

可选 `max_solver_evaluations` 默认200、上限2000，实际包括有限差分残差调用；`max_work_units` 默认200000、上限2000000，计有限根求解中的地理primitive调用。Split另有 `max_generated_turns` 默认128、上限512。均为正整数，超预算整笔拒绝，不返回部分整形。嵌套圆弧积分由 [ROUTE_GEOMETRY_NOTES.md](ROUTE_GEOMETRY_NOTES.md) 的独立预算约束；旧工程分析、制造核验、来源规范化和返回地图加密不计入这项根求解计数，不应把它称为完整CPU／墙钟上限。

主工程集成入口由根模块提供 `/api/workspace/altercourse-preview`，`kind` 为 `split` 或 `radius`。工具候选经现有 `update_path`／`auto_exclusive` 全工作区校核，既不自动保存，也不自动为共享制造实体复制或增加库存。实际API外层字段以其正式接口为准；本模块只返回单路径候选。

## Split的几何约束

先取原航段的实际到达／离开切线，用最短有符号角得到原转角。已满足最大角时返回 `changed=false`，不新增点或库存。否则先以 `ceil(abs(turn)/max_turn)` 个转角建立候选。

每个内部航段长度等于用户指定 `min_turn_distance_m`，新航段依现路线的恒向线或测地线定义。未知数为两边截去距离和共同转角δ：从原入航段的切点、实际切线开始，每到达新节点按同一δ转向，并以相同实际距离推进；端点在原出航段上、最后到达切线加δ等于原出航段切线。两个真实WGS84位置残差和一个真实切线角残差共同求根，裁切域严格是原有限邻腿内部。平面公式只是初值，不作为椭球可达性判断。

若最少转角数量在曲面上不能满足用户最大值，尝试下一数量，仍受同一总预算约束。位置残差容差0.0001米、共同角残差容差0.0000001度。最终从实际持久航段再次复核全部转角和全部内部长度；求根成功标志本身不能代替几何验收。原点ID保留于最后转角；其余新增点有独立ID。

## Radius的真实连续圆弧

每条弧腿保存：

```json
{"geometry":{"type":"circular_arc","schema_version":1,
 "center":[120,23],"radius_m":200,
 "start_azimuth_deg":180,"sweep_deg":90}}
```

圆上位置是从圆心以径向方位角做WGS84测地线正算、距离恒为半径。切线来自真实径向到达方位±90度。弧长积分GeographicLib reduced length，KP分数是真弧长分数，不用`R×圆心角`代替椭球弧长，地图折线只是显示采样。

未知数是原入／出航段的截去距离。入切点P的圆心由其实际切线法向、声明半径正算；在原出航段求另一点Q，使圆心到Q的测地距离等于R，且真实径向切线等于出航段切线。只接受有符号短内切弧分支，半径残差和入出切线残差都独立核验。邻腿可为已有真实圆弧；保留段使用实际`subsegment`描述符。没有根或局部有界求解未收敛时拒绝，不宣称穷尽任意复杂邻腿的所有几何根。原ID保留于出切点，新增一个入切点。

## 制造域、标记与资料失效

`constraints.reconcile_route_structure` 是共同结构求解入口：必须保留每个原Rigid ID及次序。新增Rigid不增加Slack-Change链接。Clamped保留原两个Rigid锚点间的距离分数，该原区间可变为多段真实曲线；实际落点再绑定其所在的相邻新Rigid段。Fixed Sliding继续按冻结实物缆KP定位，转换点可跨无域边界的几何点。

配置为Fixed时保留原 `constraint_state.manufacturing` 快照和全部原PathLink的实物站位／Slack-Change政策。几何变化的固定域在真正新曲线KP上均匀分配原基础缆量；未改变域保留原分配。旧制造缆型边界必须物化为实际节点，必要时只新增非Slack-Change的Sliding转换链接。基础量、缆型净量、附加组件／allowance、替换体领先边实物站位和零长参考站位经过实际core分析守恒核验。不删除state再capture，也不把固定域库存不足隐藏为新增缆量；不足随警告返回，由完整工作区准入政策处理。

声明 `assembly_item_id` 的链接还必须在求解后与真实body／reference的地理KP一致，容差0.0001米另加累计KP的8个浮点ULP；不一致整笔拒绝。判定不能比较点RPL的插入后实物KP与additional体的领先边实物KP：二者本来可以相差体长，却在同一地理位置。没有item的普通non-slack Rigid／Clamped链接仍是明确的冻结制造参考，允许其几何点移动，报告原／实际基础制造站及偏移、实际地理KP和插入后实物KP。这项严格实体地理一致性验收用于新结构求解及真实圆弧约束编辑；本阶段没有扩大旧直线编辑分支的历史行为范围。

未配置固定路线保留各旧腿实际固定数量，按替换路径距离分配；配置为Flexible时保留原mode、缆型与余缆目标，真正几何变化导致的柔性数量增减明确报告。共享装配若因此不同由工作区守卫拒绝，需要用户显式fork；工具不能全部改Fixed来制造假守恒。混合模式的未配置路线保留原各腿模式；已有约束域的混合模式仍不在当前支持范围。

津贴和附加体的固定制造站位经域映射；各旧腿端点停工时间／额外费用只保留一次。地理事件依其原Rigid锚段的距离分数映射；事件在原转角的映射位置就是保留ID的新转角／出切点，不能把此解释成保留原地理坐标。组件等无法保持的冲突拒绝，不静默删除。

新增／移动点水深为空。原 `profile` 和 `side_slopes` 连同旧签名原样保留，让新路线分析显示过期／未知；不填猜测水深，也不换签名把旧剖面认证为新曲线地形。柔性底余缆需要新几何的实际地形，当前单工具拒绝此联合变更，须使用明确地形重采样流程；不得自动转固定缆量规避缺测。原共享来源库、材料库、GIS层和保存修订保留。候选没有自动写入、动态初态或设备动作。

## 报告字段

`report` 固定含 `operation/config/changed/selected_point_id/result_selection_point_id/inserted_point_ids`；`before/after` 各含实际 `surface_length_m/point_count`。`manufacturing` 含 `mode/base_length_before_m/base_length_after_m/base_delta_m/physical_length_before_m/physical_length_after_m/physical_delta_m/by_cable_type`，每缆型列 `cable_type_id/before_m/after_m/delta_m`。

`geometry_evidence` 在Split含 `model/original_turn_deg/turns_deg/internal_lengths_m/trim_in_m/trim_out_m/common_turn_deg/endpoint_residual_m/terminal_tangent_residual_deg/solver`。Radius另含 `center/radius_m/sweep_deg/arc_length_m/radius_residual_m/tangent_residuals_deg/arc_solver`，其`turns_deg`是入弧、出弧切线接缝残差。两类明确 `position_tolerance_m/angle_tolerance_deg`。`solver`报告实际评估数、地理调用计数、有限截切域和预算；圆弧独立积分诊断另列。

`profile_invalidation`／`side_slopes_invalidation` 显示是否有旧资料和是否因几何变化失效，不能把已过期输入解释成新认证。`constraint_reconciliation`列真实制造域、沿线点重绑和 `link_placements`；后者逐条含冻结／实际基础制造站、站位偏移、点的插入后实物KP、点／已链接实体的真实地理KP与容差，说明插入前后站位差别。所有结果为有限JSON。独立测试覆盖真实WGS84入出切线、等角／等距、日期线／高纬、半径及独立Jacobi场积分、固定制造域／混合缆型／组件／津贴／事件／参考、真实圆弧标记与工具分割反向合并、柔性量变及缺测和预算拒绝。本开发阶段验收数量由实际最终执行记录提供，不把局部测试等同完整软件或海试。
