# 同工程多路径与制造装配关系库（schema 2）

`oceanroute/workspace.py` 管理同一工程中的 Cable Path、制造装配和显式关联；`workspace_storage.py` 提供 SQLite 事务及修订保护。路径共享缆材库和 GIS，每条路径保留一个 schema 1 子实体，以复用现有路线、剖面、实物长度、Path Link、成本与约束核。当前 `kind` 只接受 `cable`。实际调查成果继续保存在 GIS 图层中，不将测量线或仿真结果伪造为可编辑设计路线。

## 公开资料依据与实现边界

提供的 MakaiPlan 6.2 手册 **PDF 物理页 79–85（印刷页 71–77）** 介绍工程、Cable Assembly、Cable Path、As-Laid、Look-Ahead、Ship Plan 及文件之间的关系。印刷页 69 说明工程包含 GIS、缆材/缆体库和一条或多条路径；印刷页 79–85 介绍视图、当前路径选择与 GIS 操作。印刷页 175–176 区分各路径的编辑限制、Flexible/Fixed 制造量及离散 Path Link。这些内容支持将工程、几何路径和线性制造装配分为实体。

本模块的 schema、`deployment/alternative` 分配策略、库存计费与 SQLite 表均由 OceanRoute 独立定义。没有原厂 `.mpxml`、`.pthmdb`、GeoMedia 编码或数据库表结构的兼容证明；没有宣称原厂多路径、装配链接或计费算法相同。完整装配的关联校核建立在本项目 core 模型上，也不等于已制造实物的测量验证。

## 数据模型及唯一来源

| 实体/字段 | 实际语义 |
| --- | --- |
| `workspace.id/name/schema_version` | 工程身份、名称与版本；`schema_version=2` |
| `currency` | 一个工程使用一个币种；库、路径及装配不得混币，不做隐含换汇 |
| `cable_types/layers` | 工程共享缆材库与 GIS；子路径不另存副本 |
| `active_path_id` | 当前编辑路径；有路径时必须有效，空工程为 null；选择不改变制造分配 |
| `paths[].id/name/kind/project` | 路径实体；`kind=cable`，子 `project.schema_version=1`，子 `project.id` 与路径 ID 相同 |
| `assemblies[]` | 单独的线性制造库存，含条目、币种、总长、来源及制造签名 |
| `associations[]` | `{id,path_id,assembly_id,role}`；关联身份独立，可保存 `note` 等关系追溯元数据 |
| `saved_revision` | 完整工程唯一保存修订；子路径与编辑投影没有独立保存修订 |
| `origin_project_id/origin_workspace_id` | 迁移/导入追溯来源，不作为活跃关系引用 |

子路径保留 route、profile、bodies、assembly_references、events、rules、costs 等完整原有字段；禁止子实体携带 `cable_types/layers/saved_revision`。编辑投影由 `materialize_path` 注入当前共享资源，并提供 `workspace_context={workspace_id,path_id,assembly_id,role,workspace_revision}`。投影的 `id/name` 是路径的 ID/名称；工程名称是 `workspace.name`。

前端可保留活跃路径草稿，最终经 `update_path` 提交到工作区。后端会重新校核所有关联路径。投影不能独立存入旧 `/api/projects` 作为同工程保存；完整工程通过 `/api/workspaces` 保存。普通操作保留现有 `saved_revision`，成功保存后才换新修订。

## 制造条目及关联守恒

装配条目使用实物缆里程，单位为米：

| 字段/类型 | 规则 |
| --- | --- |
| `id` | 制造实体 ID；同一装配内唯一，不同装配不得复用任何条目 ID |
| `start_m/end_m/length_m` | 从实物起点累计；`length_m=end_m-start_m`，非负；有长度实体从 0 连续覆盖，无空隙/重叠 |
| `kind=cable` | 正长度，引用共享 `cable_type_id`；制造采购长度按实际占用量计算 |
| `kind=body` | 可为有限长度或零长；`body_kind/cost/properties`；`cost` 为该个实物的费用，properties 支持 wet_weight_n、mass_kg、diameter_m、drag_area_m2、drag_coefficient |
| `kind=reference` | 零长制造参考，保留身份和实物站位；不占长度、不产生采购费 |
| `name/note` | 编辑说明，不影响物理关联比较 |
| `total_length_m` | 最大制造端点，等于实际连续实体总长 |
| `manufacturing_signature` | 当前制造内容的独立追溯摘要，验证时重新生成，不作原厂身份或测量精度证明 |

从路径捕获装配时，core 的 cable/allowance 物理区间转成 `cable` 条目，有限体、零长体和制造参考分别保留。`replace` 体占用缆材区间，`additional` 体和津贴增加实物总长；这些原始工程语义继续在子路径中保留，库存不另行重复加长。

路径关联必须同时满足总长、缆型顺序与各区间、体的身份/实物站位/长度/费用/物性、制造参考身份/站位一致。总长相同而缆型交换或体移位仍拒绝。使用绝对数值容差 `1e-5`（长度量对应米），不能把容差当作制造公差或测量精度。相邻同型缆区间在比较时合并；原库存的条目 ID/分区会保留，因此沿同一曲线细分路径不会凭空替换制造实体。签名采用稳定数字规范化，同值 int/float 与正负零一致；关联判断使用实际数值容差，不只比较哈希。

一个路径最多关联一套**完整**装配；一套装配最多有一个 `deployment`。`alternative` 是同一实物库存的互斥几何方案，必须存在该装配的明确投放路径。当前不支持任意实物子区间共享、同一装配同时敷设两次或自动裁切库存。模型直接拒绝这些冲突，不静默复用制造数量。

## 编辑、复制与共享策略

`workspace_action(workspace, config)` 是完整校核的纯变换，不原地修改输入：

| action | 配置与行为 |
| --- | --- |
| `set_active` | `path_id`；只选择当前路径，不改变安装归属或费用 |
| `set_deployment` | `path_id`；该路径必须有关联，将其设为该装配唯一 deployment，其余同装配路径成为 alternative |
| `update_metadata` | `name`；只改工程名称 |
| `update_shared` | `cable_types? / layers?`；更新全工程共享资源，重新核验所有路径和库存；删除被用型号拒绝 |
| `add_path` | `project,name?,assembly_policy?,assembly_id?`；来源须匹配当前共享资源，生成新路径身份 |
| `copy_path` | `path_id?（默认当前）,name?,assembly_policy?,assembly_id?`；复制同工程路径 |
| `update_path` | `path_id?,project,assembly_policy?,update_shared?,successor_path_id?`；投影 ID 须匹配目标，project.name 同步路径名称 |
| `remove_path` | `path_id?,delete_orphan_assembly?,active_path_id?,successor_path_id?`；删除路径，默认保留制造库存；删除当前路径可指定下一活跃路径 |
| `detach` | `path_id?,delete_orphan_assembly?,successor_path_id?`；只取消关联，保留方案 |
| `associate` | `path_id?,assembly_id,role?（默认deployment）,replace_existing?`；建立完整装配关联；替换已有关系须显式 true |
| `add_assembly` | `assembly` 或 `project,name?`；新增未分配库存；从路径捕获时生成新的物理实体 ID |
| `remove_assembly` | `assembly_id`；仍有关联则拒绝 |

新增/复制的 `assembly_policy`：

- `independent`（默认）：生成新路径和新装配；体、参考及缆条目均获得新物理 ID，真实采购与制造总量增加。
- `alternative`：显式关联既有装配；复制时可推定来源装配，新增时通过 `assembly_id` 指定；不产生新制造量。
- `unassigned`：仅保留路线方案，不计入完整工程的采购与安装。

`update_path` 默认策略为 `auto_exclusive`：唯一 deployment 引用的装配随 core 计算的制造量自然更新，Flexible 路径几何/余缆变化可以正常提交；Fixed 段与已有 Path Link 约束仍由原核保持。`update_exclusive` 具有同样的显式独占语义。

同一装配已被 alternative 共享时，单条路线不得静默改变制造量、顺序或体/参考。物理量保持一致的几何修改允许；量变会返回 `WORKSPACE_SHARED_ASSEMBLY_CHANGED`。选择 `fork` 才创建新实体和新装配；旧库存保留并计费。`reject` 在制造变化时拒绝，在制造不变时允许更新。

投影中的共享库/图层变更，只有 `update_shared:true` 才会提升到工程共享资源；否则返回 `WORKSPACE_SHARED_EDIT_REQUIRED`，不丢弃修改也不私存子路径副本。新增路径不自动合并另一工程的材料或 GIS，应先明确 `update_shared`。

删除、detach 或 fork 一个 deployment 时，若旧装配仍有 alternative，必须通过 `successor_path_id` 明确接管旧库存的路径。`delete_orphan_assembly:true` 只允许真正无引用的装配被删。`associate` 替换一个仍有备选引用的主关联会拒绝，应先 detach 并指定接管者，或直接 `set_deployment`。

活跃刚性/Clamped/Sliding/Path Link 域不会被工作区绕过；非法直接改坐标仍由 constraints/core 拒绝。独立复制会重建物理体/参考 ID，并重新捕获既有约束状态及相关实物锚点。此操作不放松原来的固定制造量。

## 汇总、单位与费用

平面路线 KP、底距和实物制造 KP 是不同账目。所有长度以米、时长以小时、经纬度以 WGS84 十进制度报告，金额使用 `workspace.currency`。`deployment_bottom_length_m` 在任一投放路径底距未知时为 null，不以 0 替代缺测。

`analysis.summary` 的主要字段为：path_count、assembly_count、deployment_path_count、alternative_path_count、unassigned_path_count、unallocated_assembly_count、manufactured_total_m、material_length_m、body_length_m、material_cost、body_cost、procurement_cost、deployment_surface_length_m、deployment_bottom_length_m、time_hours、vessel_cost、burial_cost、extra_cost、installation_subtotal、contingency_cost、cost_total、currency。

费用政策明确为：

```
唯一库存采购费 = Σ装配实际缆材长度×当前共享型号单价 + Σ装配体费用
安装小计       = Σ deployment 路径（船费 + 埋设费 + 事件/额外费用）
预备费         = Σ deployment 路径 core 的 contingency_cost
工程总费用     = 唯一库存采购费 + 安装小计 + 预备费
```

core 的路径预备费按照该路径的采购与安装小计乘 contingency_pct；每套已分配装配只有一个 deployment，因此采购不会被备选路径再次纳入预备费。未分配库存仍计一次采购费，没有安装或预备费；未关联方案的独立 core 费用只供对照，不进入总账。警告分别为 `WORKSPACE_UNALLOCATED_INVENTORY` 和 `WORKSPACE_UNASSIGNED_PATH`。

`analysis.paths` 按工程顺序返回各路径摘要、关联身份/角色、included_in_installation，以及 core 的 `route_geometry`、`route_geometry_segments`、`route_signature`。地图必须使用真实加密曲线；日期线两侧用 segments 分开画。`analysis.assemblies` 返回每套库存的制造总长、缆型数量、采购费、体/参考数量和 deployment/alternative 路径列表。`active_path_analysis` 是当前路径完整 core 分析。所有路径的警告附带 path_id。

## Python 与 HTTP 合同

Python 导出函数：

- `migrate_project(project, config=None)`：schema1 → 新 schema2 工程。
- `validate_workspace(workspace)`：完整校核并返回规范化副本。
- `materialize_path(workspace, path_id=None)`：有效活跃投影，空工程返回 None。
- `analyze_workspace(workspace)`：只返回分析对象。
- `workspace_action(workspace, config)`：完整关系变换。
- `import_workspace(text, config=None)`：schema1 迁移或 schema2 导入。
- `export_workspace(workspace)`：完整可下载 JSON **字符串**。

迁移、变换及导入返回 `{workspace,project,analysis,report,warnings}`；report 包括操作名以及可用的 before_summary/after_summary、制造总量与工程费差值。空工程 `project=null`。

| HTTP | 实际请求/返回 |
| --- | --- |
| `POST /api/workspace/migrate` | `{project,config?}` → envelope |
| `POST /api/workspace/action` | `{workspace,config}` → envelope |
| `POST /api/workspace/analyze` | 直接 workspace → analysis |
| `POST /api/workspace/import` | `{text,config?}` → envelope |
| `POST /api/workspace/export` | 直接 workspace → JSON 下载 |
| `GET /api/workspaces` | 工程列表，含 revision/path_count/assembly_count |
| `POST /api/workspaces` | 直接 workspace → `{workspace,id,revision,updated_at}` |
| `GET /api/workspaces/{id}` | 完整工作区与 saved_revision |
| `GET /api/workspaces/{id}/revisions` | 按新到旧列出修订元数据 |
| `POST /api/workspaces/{id}/restore/{revision}` | `{expected_revision}` → 恢复的完整 workspace，产生新修订 |

非法结构、空引用、重复部署、混币、数值非法及制造不一致等使用 HTTP 422；不存在的已保存工程/历史使用 404。JSON 数组、null、标量、NaN/Infinity 均不接受为工作区。

## SQLite 修订与并发

`WorkspaceStore(path)` 与旧 ProjectStore 可共用同一 SQLite 文件，使用独立的 workspace_projects、workspace_paths、workspace_assemblies、workspace_associations、workspace_revisions 表。当前路径/装配/关联为真正关系行，历史为完整不可变 JSON 快照。关联有复合外键、每路径单关联约束及每装配唯一 deployment 的部分唯一索引。关系额外元数据存 document；已有无 document 列的关系表自动增列。

每次保存先校验，随后 `BEGIN IMMEDIATE` 比较当前修订，在同一事务中写入工程元数据、关系行及历史。读取也使用事务，避免把两次修订的元数据/实体混装。SQLite 开启 foreign_keys 与 WAL。首次保存可省略修订；修改已有 ID 必须带最新 saved_revision 或 Python 显式 expected_revision。冲突不覆盖，返回 `WORKSPACE_REVISION_CONFLICT`；API 为 422。

`restore(id, revision, expected_revision=当前修订)` 同样受保护；恢复旧快照会追加 `当前修订+1`，不倒退修订号或抹去后续历史。两位同时编辑同一修订只会有一个写入成功。注入的写入故障会回滚父记录、全部实体关系及修订。

## JSON 迁移与可复算小例

schema1 迁移生成新工程 ID、路径 ID 和装配 ID，保留来源 origin_project_id，不继承旧工程 saved_revision。原来有效的几何、剖面、约束、制造体和参考不改工程含义。若 schema1 未设置缆库，显式保留 core 的 GENERIC 零单价/1 m/s 默认，并给出 `WORKSPACE_DEFAULT_CABLE_LIBRARY` 警告。

schema2 导入生成新的工程 ID，保留内部路径/装配/关联及制造身份关系，记录 origin_workspace_id，移除 saved_revision；这样文件导入不会覆盖本地原工程或复用旧乐观锁。JSON 导出保留整个工程、来源、关系与修订信息；规范化时重新校核制造总长和签名。制造 items 使用本节定义的字段，非任意厂商扩展 schema。

下面是一份完整的小工作区：赤道上约 1,000 m 的 Fixed 路径，1,010 m A 缆；零船费，单价 2 CNY/m，所以采购/总费用均为 2,020 CNY：

```json
{
  "schema_version": 2, "id": "example-workspace", "name": "同工程示例", "currency": "CNY",
  "active_path_id": "path-main",
  "cable_types": [{"id":"A","name":"A缆","cost_per_m":2,"lay_speed_m_s":1}],
  "layers": [],
  "paths": [{"id":"path-main","name":"主路径","kind":"cable","project":{
    "schema_version":1,"id":"path-main","name":"主路径","crs":"EPSG:4326",
    "route":{"curve":"rhumb","mode":"fixed","slack_basis":"surface","slack_pct":1,
      "points":[{"id":"start","longitude":0,"latitude":0,"depth_m":20},
                {"id":"end","longitude":0.008983152841195215,"latitude":0,"depth_m":20}],
      "legs":[{"cable_type_id":"A","fixed_cable_length_m":1010}]},
    "bodies":[],"costs":{"currency":"CNY"}
  }}],
  "assemblies":[{"id":"assembly-main","name":"完整制造装配","currency":"CNY",
    "items":[{"id":"cable-main","kind":"cable","cable_type_id":"A","start_m":0,"end_m":1010,"length_m":1010}],
    "total_length_m":1010}],
  "associations":[{"id":"association-main","path_id":"path-main","assembly_id":"assembly-main","role":"deployment"}]
}
```

对该工作区执行 `copy_path` 默认 independent 后，实际制造量变成 2,020 m、采购费 4,040 CNY；显式 alternative 则仍为 1,010 m、2,020 CNY。两种操作均有两条同工程路径，但实物数量不同。改变 active_path_id 不改变汇总；`set_deployment` 才改变哪条路线计入安装。

## 上限和验证

完整 JSON 上限 32 MiB；ID 1–128 字符，工程名称最多 512 字符；各最多 100 路径、100 装配、100 关联，共享缆型最多 10,000、GIS 图层最多 1,000。所有路径累计最多 50,000 个原始点，每路径仍受 core 的 10,000 点限制。每装配最多 10,000 个制造条目，全工程累计最多 100,000；全工程 core 默认地图加密顶点预算 250,000，先检查再分配几何，远距离反复折返需拆分工程。严格 JSON 序列化拒绝非有限结果。

`tests/test_workspace.py` 现有 31 个测试用例，包含迁移工程量/费用等值、原核省略 ID 的实体捕获、独立复制实体身份、备选不重复计费、固定路径几何变化、柔性独占更新与共享 fork、部署接管、缆型/体/参考守恒、共享价格/GIS 同步、约束不可绕过、同曲线分区身份保存、真实曲线日期线地图、计算上限、混币与非法引用、完整导入导出、真实 SQLite 双线程竞争、外键/唯一部署和写入故障回滚，以及 TestClient 的导入/分析/保存/恢复/下载往返。
