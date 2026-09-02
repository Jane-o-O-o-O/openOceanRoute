# 曲床静力初态与地理计划桥接的独立审查

本文针对冻结0.5之后的开发代码，不改变0.5发行物或验收计数。独立文件 `tests/test_plan_equilibrium_review.py` 本轮26项真实验收通过；它不是完整回归、下一版本发行验收、海试或原厂精度验证。本次只写独立测试与本文，业务修复由规划桥接模块维护者完成。

## 1. 受审核的实际合同

入口为 `POST /api/shipplan/prepare-voyage`，请求仍是 `{project,config}`。曲床分支同时明确提供 `config.seabed_grid` 和 `config.equilibrium_start`：

```json
{
  "plan": {"bottom_tension_n": 10, "sample_spacing_m": 30},
  "duration_s": 0.1,
  "simulation": {"nodes": 10, "internal_dt_s": 0.01, "dt_s": 0.02},
  "voyage": {"adaptive_mesh": {"enabled": false}, "chunk_duration_s": 0.04},
  "seabed_grid": "此处须替换为实际oceanroute.bathymetry.v1对象",
  "equilibrium_start": {
    "anchor": {"longitude": 0, "latitude": 0, "z_model_m": -10},
    "vessel_z_m": 0,
    "natural_length_m": 20
  }
}
```

此JSON示意字段，不是可直接运行的完整床格。测试文件提供真实3×3变深床格，模型高程不是从一维剖面挤出的默认假场。初始缆长以 `natural_length_m` 或 `rest_lengths_m` 二选一，后者恰有 `nodes-1` 个正自然段长。可选 `initial_positions_m` 是**原床格的局部米坐标**下N×3迭代种子；它不是绝对投影E/N，也不是已验收平衡状态。`anchor.z_model_m` 与 `vessel_z_m` 使用已声明模型海面z=0的高程；没有自动椭球高、海图基准或潮位转换。

返回配置用于真实 `POST /api/voyage/run` 或已有后台任务流程，仍须完整工程和实际物性；预览结果不是resume_state。保存规划映射schema2，实际物理检查点schema3，连续航程外层检查点仍用原有独立schema。未提供新两字段的旧规划分支保持原合同，不把变深剖面悄悄改成平床。

## 2. 地理坐标和完整床格必须一起重基准

令原格的局部坐标为p、来源投影原点为E₀，计划起始真实地理船位变换后的投影坐标为Eᵥ。新局部坐标为：

```text
shift = Eᵥ − E₀
grid.x_new = grid.x_old − shift.x
grid.y_new = grid.y_old − shift.y
seed.xy_new = seed.xy_old − shift
anchor.xy_new = projected(anchor.lonlat) − Eᵥ
vessel.xy_new = (0,0)
new source.origin_projected_m = Eᵥ
grid.z_new = grid.z_old
seed.z_new = seed.z_old
```

只改变共同水平原点，双线性床的实际形状、高程和NoData保持。需要真实投影、east/north轴和米单位；不能给LOCAL网格补一个EPSG标签，不能把英尺当米，也不能把经纬度度数当米。真实坐标操作不采用ballpark替代最佳缺网格操作，具体PROJ操作与警告保留。床格范围与有效单元检查仍实际执行，没有外推或平床补救。

独立北半球对照用Mercator解析式 `E=a·lon_rad`、`N=a·log(tan(π/4+lat_rad/2))`，a=6378137 m，起点110°E、30°N。特意选不同原始原点，使共同平移为 `[37,-19] m`；锚、完整x/y轴、非均匀自然段种子与来源原点逐一检查，z数组严格不变，输入对象也不改变。规划起始船位另一对照从公开equatorial计划行的制造坐标分数直接插值，经上述解析式求投影期望；没有调用被测桥接内部的 `_at`、坐标转换或初始化helper生成黄金值。

这是声明投影米平面中的研究力学。投影单位为米并不保证当地投影比例为1，例如球面Mercator在30°纬度的尺度约为1/cos30°。当前过程不自动把投影格反算为地面测距或曲面力学；应选择适合区域的工程坐标系并独立核实尺度、床测量和垂直基准。正确坐标变换不是地理动力精度证明。

## 3. 自然材料、库存和首次放缆

令声明初始自然材料L=Σrest，计划窗口起点的制造顶站为K₀：

```text
manufacturing_origin O = K₀ − L ≥ 0
initial top material = O + L = K₀
initial anchor material = O
paid_out(0) = 0
Σrest(t) = L + integral(payout dt)
top material(t) = K₀ + integral(payout dt)
```

节点顺序为船→锚，制造站沿该方向递减。L可含悬垂和床接触部分，不是路线平面KP，不是显示折线长，不是伸长弧长，也不是此窗口中新放出的材料。更早计划过程只用于确定当前制造库存，不能首块再放出L。固定锚离床时，保留明确外部端支持；不得贴床、称为已发现touchdown或强制拟合计划触点。

独立非均匀自然段为 `[8,1,2,1,2,1,2,1,2] m`，总量20 m，计划起时60 s，故O为正。测试直接从自然半段湿重构造可解离床力平衡：各段 `Vᵢ=10+w(Σ后方自然段+rᵢ/2)`，`Tᵢ=hypot(H,Vᵢ)`，段向量 `rᵢ(1+Tᵢ/EA)(H,0,Vᵢ)/Tᵢ`。独立标量根使总垂向落差为10 m；由公开计划行插值的船位和解析Mercator构造完整原格种子及地理锚。该期望不是静力/动力helper生成的帧。

实际prepare→run检查初始自然量20 m、初始paid=0、初始top−anchor=20 m、原rest快照不改变、后续真实放缆积分与材料总量/顶站一致、制造最旧站不漂移，且离床锚z=-10 m保留、touchdown=null。全程0.12 s与0.06 s保存JSON后继续0.06 s的实际节点位置、速度、自然段和制造站在独立1e−8容限内一致；无重新初始化或重复放缆。非空恢复工程的剖面深度或EA改动被拒绝，空工程恢复使用已保存物理状态。

## 4. 实际找到并修复的反例

以下均在真实prepare生成的映射上修改字段，再重算公开校验和；没有mock静力、动力、投影或HTTP。

| 原实测问题 | 最终拒绝依据 |
|---|---|
| terrain_frame改CRS、原点或垂直平移仍接受 | 保留原床实际快照；重新做地理变换，共同平移后的床digest、CRS/原点/垂直语义与来源一致 |
| 初始锚、source制造原点或地理原点可与实际输入不符 | 实际局部端点、自然库存、初始top与初始化完整proof交叉复核 |
| 改目标XY并同时改残差，可声称另一计划目标 | 保存目标WGS84并实际变换为同一局部框架；残差必须为实际锚与目标的距离 |
| 改行末船位100 m，source控制未改仍接受 | 行XY起止与真实speed/heading/time积分一致，行间几何连续 |
| 改最终库存100 m或末行制造量1 m仍接受 | 每行制造增量等于真实payout×dt，最终站与末行一致，实际放缆残差复核 |
| source计划起时改为-100 s仍接受 | 非负源时间、本地行时间、horizon和真实终止stop一致 |
| rest为字符串时抛TypeError、巨大整数抛OverflowError | 严格有限数与段数量/范围校核，统一ValueError；真实HTTP为422而非500 |

这些字段一致性检查不能替代身份认证。公开SHA256校验和用于检测失配；若调用者完整构造一套新的自洽工程、来源和证明，它不是受信签名。实际平衡proof也必须复算公开力/材料校核，不能只读 `accepted:true`。

## 5. 验收范围和保留的限制

命令：`.venv/bin/python -m pytest tests/test_plan_equilibrium_review.py -q`。本轮26项包含实际解析投影对照、非法CRS/单位拒绝、初始库存和不均匀材料、13种重算checksum后的失配拒绝、5种坏rest、实际恢复/工程篡改拒绝，以及TestClient真实prepare/run和422错误接口。与[初始化独立审查](EQUILIBRIUM_INITIAL_REVIEW.md)的19项分属不同文件，不能把这两个局部验收数当完整发行总计。

完整二维床和真实静力初态不使已有船舶计划偏移自动成为曲床准稳态解。现有计划船位偏移仍为均匀局部平底初估，返回 `PLAN_OFFSETS_REMAIN_FLAT_LOCAL_FIRST_CUT` 明确说明；后续动力不承诺自动沿计划海底目标移动。初始锚与计划目标的真实偏差必须显示，不能以看见一条节点曲线推断路线跟随成立。

当前初始区间仍限均匀湿重/EA、EI=0、无已部署组件/初始海流波浪，零速度快照不重建静摩擦或加载历史。自然材料不足、无覆盖、静力不收敛、预算不足或初始化proof不一致必须失败，不自动加库存、flatten床或改固定端。曲床粗化/长期误差、混合初始物性、实体接触、安装测量重建和海试校准仍是完整目标中的独立后续工作。

相关实际合同见 [PLAN_VOYAGE_NOTES.md](PLAN_VOYAGE_NOTES.md)、[BATHYMETRY_NOTES.md](BATHYMETRY_NOTES.md)、[STATIC_BATHYMETRY_NOTES.md](STATIC_BATHYMETRY_NOTES.md) 与 [EQUILIBRIUM_INITIAL_REVIEW.md](EQUILIBRIUM_INITIAL_REVIEW.md)。
