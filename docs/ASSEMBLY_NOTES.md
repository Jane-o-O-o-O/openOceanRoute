# 装配文本与实制回写

此模块独立实现公开手册M168-182所描述的制造输入和装配管理用途，定义OceanRoute CSV，不能冒充Makai原生装配模板。

## 导出与输入

`POST /api/export/assembly` 输出制造CSV，含实物绝对起止、长度、缆型、有限体、零长度接头、参考点及物性/费用。重复跨路线点的同一body合并为一条有限跨度。`POST /api/assembly/import` 输入 `{project,text,config}`，返回 `{project,items,report,warnings}` 预览，不保存或原地修改工程。

CSV列为 `kind,id,name,cable_type_id,start_m,end_m,length_m,body_kind,cost,wet_weight_n,mass_kg,diameter_m,drag_area_m2,drag_coefficient,note`。kind为cable/allowance/body/reference，allowance作为同样计价的实物缆段导入。可省可选列；缆型须在工程库存在，或用type_mapping显式映射厂内型号。湿重为N，质量kg，其他物性字段按名称SI单位；cost是附件单件价，缆材价继续来自工程库。

```csv
kind,name,cable_type_id,length_m,cost
cable,轻型缆,LW,1000,
body,中继器,,2,5000
cable,铠装缆,DA,500,
body,终端接头,,0,1000
```

relative模式按行累计length_m，reference不推进cursor。absolute模式用start_m/end_m，可同时length_m并核对；非参考实物连续、无空隙、无重叠。有限体占据长度，零长接头仍计件。length_units可声明m/ft/fathom/km，明确转换为内部米，不按数字猜单位。错误行拒绝整个预览，不悄悄跳过坏缆段。

## 全替换与映射

config必须明确 `operation:"replace",position_mode:"relative"或"absolute",mapping_policy:"surface_fraction"`。保持原地理曲线，按总实物长/总平面长将制造转换点映射到路线并插点，所有区间改为固定制造量，附件采用显式实物起始端replace。原津贴表示被替换、旧地理附件位置可能变化，预览警告要求复核余缆、缆型位置和装配。

这种明确比例映射适合建立首次路线/实制链接；并非原厂任意复杂链接域的复刻。已有Path Link或材料约束状态时默认拒绝；显式clear_path_links才移除旧域，并要求之后用新装配重新配置，不能静默绕过。

生成后核对总实物量及每一缆型制造量，不满足守恒不返回候选。缆材与附件费用由最终分析重新计算，负余缆仍报告，制造输入不会擅自增加缆量来消除不足。

## 仅追加附件

`operation:"append_bodies",position_mode:"absolute"`，只接受body/reference，start_m为当前实物KP，有限体replace现有缆材，不增加总实物量。重叠/越界/重复实体拒绝；原制造段和未修改附件保留。若当前已物化约束域仍需显式移除并重新配置。

制造参考为 `project.assembly_references`，不占长度/费用，位置为实物KP，零长接头为有单件费用的body，两者不能混用。

## 验证

覆盖真实两缆型/有限体/零长度接头/参考计数与费用、CSV绝对往返、追加体扣除缆材但总长不变、厂内型号映射、英尺转换、空隙/重叠/未知型/重复id原子拒绝、映射意图及Path Link冲突。没有验证原厂专有模板兼容。
