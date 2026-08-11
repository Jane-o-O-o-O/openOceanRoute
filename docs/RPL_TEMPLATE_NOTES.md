# 固定宽度与多行RPL文本模板

这是OceanRoute独立实现的开放JSON模板，不读取或生成Makai二进制格式模板，也不执行模板中的Python、表达式、脚本或文件路径。依据用户提供手册物理页M234–238（书内页226–230）：文本可以分隔或定宽；用户声明文件头、每航点行数、注释、字段所在行/列或宽度、度分秒和半球、缆特征，并保存/加载格式。手册将累计缆里程与余缆变化标记区分；这里按下述独立规则转换为当前工程字段，不能据此宣称原厂算法或无损兼容。

## HTTP与Python合同

| 接口/函数 | 输入与实际输出 |
|---|---|
| `POST /api/import/rpl` | `{text, delimiter?, mapping?, template?, error_policy?}`；返回实际解析预览 |
| `POST /api/import/rpl/template` | `{template}`校验对象，或`{text}`加载模板JSON；返回`{template: normalized}` |
| `GET /api/import/rpl/templates` | `{examples:[{template,text},...]}`；两份可直接解析的示例，不是伪造结果 |
| `exchange.import_rpl(text, delimiter=None, mapping=None, template=None, error_policy=None)` | 保留原CSV/TSV前三参数，新增模板和错误策略 |
| `rpl_templates.validate_template(template)` | 规范化且校验；不修改传入对象 |
| `rpl_templates.dump_template(template)` / `load_template(text)` | 有限JSON保存/加载；不接触磁盘，调用方选择保存位置 |
| `rpl_templates.parse_rpl(text, template)` | 不依赖HTTP的真实解析与腿字段转换 |

无`template`仍读取带表头的CSV/TSV、现有中文/英文别名及列映射，默认保留旧的跳过坏记录行为；显式顶层`error_policy`可改变这个默认。新模板不使用外部`mapping`/`delimiter`，同时提交会拒绝。顶层与模板的错误策略同时给出时必须相同，不能悄悄覆盖。非法结构/模板/超预算为422；默认collect的逐记录错误作为200预览返回，但`can_apply=false`。

## 模板结构与字符列

```json
{
  "schema": "oceanroute.rpl-template",
  "schema_version": 1,
  "name": "两行定宽 / 累计缆公里",
  "format": "fixed_width",
  "column_units": "unicode_code_points",
  "index_base": 1,
  "lines_per_record": 2,
  "header_lines": 1,
  "skip_blank_lines": true,
  "comment_prefixes": ["#", ";"],
  "error_policy": "collect",
  "leg_assignment": "outgoing",
  "depth_positive": "down",
  "max_records": 10000,
  "units": {"cable_kp_m": "km"},
  "defaults": {"curve": "rhumb", "mode": "flexible", "slack_basis": "surface", "slack_pct": 0, "cable_type_id": "LW"},
  "cable_type_map": {"轻缆": "LW"},
  "fields": {
    "label": {"line": 1, "start": 1, "width": 6},
    "longitude": {"line": 1, "start": 7, "width": 14},
    "latitude": {"line": 1, "start": 21, "width": 14},
    "depth_m": {"line": 2, "start": 1, "width": 8},
    "cable_kp_m": {"line": 2, "start": 9, "width": 10},
    "cable_type_id": {"line": 2, "start": 19, "width": 6},
    "slack_pct": {"line": 2, "start": 25, "width": 8}
  }
}
```

`index_base`同时控制`line`与`start`（分隔格式中的`column`也同样适用），只能为0或1；省略时为1，规范化结果明确保存。`width`始终为正字符数，与索引基准无关。定宽读取半开区间`[start-index_base, start-index_base+width)`，不把行末之外当空格补齐。每行可有未映射文本，但所有声明切片均须存在；必填字段还必须非空。字段位置可加`required`及`trim`布尔选项，默认去首尾空白；经纬度/分列度数/累计缆里程默认必填。

唯一支持的列单位是Unicode代码点：中文字符和普通单码点emoji各占1；组合字符由多个代码点组成就占多列。它不是UTF-8字节、字符簇或屏幕格宽度；制表符在定宽格式计1字符，**不展开tab**。例如前6个代码点是标签，经度从第7个代码点开始，即使中文在屏幕上占两格也不移动列。仅剥离文件开头一个BOM，行内字符不重新规范化；用户须先解码文件为文本，当前不自动猜GBK或原厂ASCII。

`header_lines`先按原始物理行跳过；随后丢弃可配置空行和左侧去空白后以任一`comment_prefixes`开头的注释行，再组成记录。定宽每记录取`lines_per_record`个保留物理行。插入/漏掉非注释数据行会影响分组；解析器不会猜测恢复记录边界。残缺尾记录有明确错误。

## 分隔、多行与坐标

分隔格式设置`format:"delimited"`、单字符`delimiter`及可选`quotechar`（默认逗号和双引号）；字段位置改为`{line,column}`，`column`是CSV字段序号，不是字符位置。JSON制表符写`"\t"`。只支持一个分隔字符，不自动把多个分隔符混成一个规则。

每记录包含`lines_per_record`个**CSV逻辑行**。带引号字段中的逗号、双引号转义和换行由实际CSV解析器处理；引号内的`#`、`;`和空行属于字段内容，不当成注释。`records.line_start/line_end`仍显示原文件1基物理行。引号语法损坏无法可靠重同步时，停止解析，连显式skip也不允许应用；不能把吞入尾文件的一条坏CSV记录当作已定位全部损坏。

经纬度可各用一个完整字段：`longitude`/`latitude`支持`118.25`、`118 15 E`、`N22 30`或`22°30′0″N`。也可各用分列字段：

```json
{
  "schema": "oceanroute.rpl-template", "schema_version": 1,
  "format": "delimited", "index_base": 0, "header_lines": 1,
  "fields": {
    "label": {"column": 0},
    "longitude_degrees": {"column": 1},
    "longitude_minutes": {"column": 2},
    "longitude_seconds": {"column": 3},
    "longitude_hemisphere": {"column": 4},
    "latitude_degrees": {"column": 5},
    "latitude_minutes": {"column": 6},
    "latitude_seconds": {"column": 7},
    "latitude_hemisphere": {"column": 8},
    "slack_pct": {"column": 9},
    "note": {"column": 10}
  },
  "defaults": {"cable_type_id": "LW"}
}
```

对应示例：

```csv
label,lon_deg,lon_min,lon_sec,EW,lat_deg,lat_min,lat_sec,NS,slack,note
"起点,甲",118,0,0,E,22,0,0,N,1.5,"中文,备注"
终点,118,1,0,E,22,1,0,N,2,终点
```

完整坐标字段与同轴分列字段互斥。分列degrees是独立数值，分钟/秒可选；仅最后实际参与的一项允许小数，分秒在`[0,60)`内，半球须匹配该轴。中文东/西/南/北也可识别。文本`-0 30`保留负号，得到−0.5°；`-0 N`仍是方向矛盾，不能把负零的方向丢掉。完整数值标量可用科学计数法，如`-1e-8`或`1.2E+2`，实际检查有限性和经纬度范围，以保留自有CSV近零坐标往返；不把标量`1E2`误读为1°2′东。科学计数与DMS/半球混用、内嵌半球字符仍拒绝。

## 实际工程字段与单位

| 字段 | 转换与应用 |
|---|---|
| `label`, `note` | 航点文字；各最多4096代码点，不能执行 |
| `depth_m` | 航点水深；默认正向下，`depth_positive:"up"`将非正高程反号；缺测保留null |
| `kp_m` | 仅来源KP；保存到记录，不替代按经纬度与选定WGS84曲线计算的路线KP |
| `cable_kp_m` | 连续非递减累计缆里程；每个有效记录须填写，相邻差额成为固定制造量 |
| `cable_type_id` | 经`cable_type_map`明确映射后的真实工程缆型ID；不创建/猜测物性库 |
| `slack_pct`, `slack_basis` | 柔性段目标余缆及surface/bottom；默认1.5表示1.5%，不是比值1.5 |
| `mode`, `fixed_cable_length_m` | fixed/flexible；有明确固定长度且无冲突模式时导入固定段；缺少固定量不能应用 |
| `burial` | true/false、1/0、yes/no或是/否，进入实际埋设计费 |
| `stop_hours`, `extra_cost` | 非负停时和当前工程币种下的非负额外费用，进入实际腿费用/时间 |

`units`可对`depth_m/kp_m/cable_kp_m/fixed_cable_length_m`声明`m/km/ft/nm`（1海里=1852m、1ft=0.3048m），对`slack_pct`声明`percent/fraction`，对`stop_hours`声明`h/min/s`。省略时为米、百分数和小时。转换后检查有限性/范围。`defaults`始终使用**内部单位**，不跟随源字段单位再转换，支持curve/mode/slack_basis/slack_pct/cable_type_id/burial/stop_hours/extra_cost。敷设速度取当前缆型库，不接受会被core忽略的腿速度字段。

`leg_assignment:"outgoing"`把第i记录的腿字段应用到第i→i+1段；`incoming`使用终点记录的字段。默认outgoing，与当前core及导出RPL一致。返回`legs`始终是有效点数减一，末点没有出发段；调用方不能只采用points再全部重建默认腿，否则会丢失制造量/余缆/缆型。

只要声明`cable_kp_m`，来源累计长度是权威制造量，不能同时声明mode或fixed_cable_length字段；进口余缆值保留为来源与变化记录，不用其舍入值重新生成缆长。非零起始累计缆里程记录为`metadata.cable_distance_origin_m`，导入制造量从起点归零；总制造量为末值−首值。相同坐标但缆里程增加保留为零平面长度固定制造段，core的余缆百分比为null，并给出跳跃提示，不伪造Infinity。

仅有航点深度时，海底距来自明确标记的分段线性近似，不是实测连续测深。柔性bottom余缆必须有完整端点深度，缺失时不可应用，不能静默替换为surface。短缺的固定量可以导入并由实际core给出CABLE_SHORTAGE等工程警告，语法解析通过不等于数量足够。

## 记录错误、应用与预算

返回兼容`points/warnings/accepted_rows/rejected_rows`，并增加`legs/route_options/errors/records/can_apply/template/metadata`。错误例如：

```json
{
  "code": "RPL_RECORD_INVALID", "severity": "error",
  "record": 2, "row": 4, "line_start": 4, "line_end": 5,
  "field": "longitude", "message": "记录2，第4行：短行…"
}
```

错误行号和记录号始终为人读的1基，与模板的index_base无关。`records`逐项给出记录号、物理起止行、accepted；成功项有point_id、source_kp_m/source_cable_kp_m/source_slack_pct及computed_route_kp_m，失败项有error。错误也保留在warnings中供旧客户端显示；`rejected_rows`按拒绝记录数计，不把额外工程警告当坏记录。

- `collect`：尽可能解析预览，有任何记录错误时禁止应用；新模板默认此策略。
- `skip`：显式跳过坏记录，至少两个有效点且腿可用才允许应用。越过被拒记录的连接给出RPL_REJECTED_RECORD_BRIDGE；空间曲线、深度和缆型可能改变，不能声称恢复了缺失记录。累计缆里程仍取剩余有效点的差额。
- `reject`：遇到首个记录/腿错误直接422。

无法恢复的CSV语法或区间工程输入错误仍禁止应用。无template、未声明策略的旧CSV保持skip默认；新版UI应明确选择策略并重新预览。应用前应校核返回的缆型ID存在于当前共享库，清除/重建旧剖面与装配关联时遵循工作区/约束规则，不将新RPL默默覆盖捕获的制造域。预览结果必须绑定输入和模板，修改后不能应用旧结果。

硬上限：源文本/结果各32MiB，模板128KiB，每记录1–32逻辑行，最多10000记录（与core路线规模一致），原文件最多500000物理行，单行最多1000000代码点，CSV最多512列，映射库最多1000项。超限不截断后声称成功；计数包括坏记录。固定宽度未提供字段、编码猜测、多个分隔符、任意循环脚本、字节/屏幕格列、原厂原生模板/正文、附件装配自动恢复均不在本次范围。

## 实际验收

`tests/test_rpl_templates.py`使用真实示例、core及FastAPI TestClient：两行定宽、0/1索引、中文/emoji代码点、头/空行/注释、短行与残缺尾记录、quoted CSV换行、分列DMS及负零、米/公里/海里/ft/比例/停时转换、方向/数值错误、制造量差额与重复坐标跳跃、实际成本/时间、来源KP日期线差别、明确collect/skip/reject、模板JSON往返和拒绝可执行字段、资源预算与实际HTTP导入→分析。既有exchange/装配调用与API导入应一并回归，不能只用成功图标作为验收。

2026-10-04本轮新增31项模板用例；与既有exchange、API、装配及工作流定向回归合计61项全部通过，1.59秒。命令如下；没有据此预先声明新完整浏览器或发行包验收：

```sh
.venv/bin/python -m pytest -o addopts='' tests/test_rpl_templates.py tests/test_exchange.py tests/test_api.py tests/test_assembly.py tests/test_extended_api.py tests/test_workflows_api.py -q
```
