# openOceanRoute — Independent submarine cable route planning

独立海底电缆路由规划 · OceanRoute 0.12.1 · MIT

openOceanRoute is an independent MIT-licensed project for submarine cable route planning. Its application and distribution files are named **OceanRoute**, with frozen delivery version **0.12.1**. A local browser workspace connects WGS84 route geometry, true circular arcs, route position lists (RPL), bathymetry, GIS reference layers, rule checks and bounded cable mechanics research models.

**MakaiPlan / MakaiPlan Pro are Makai commercial products. This project has no affiliation, authorization or endorsement from Makai and supplies no Makai license.** It makes no claim of original-engine equivalence, proprietary format compatibility or field accuracy. MIT covers original project code and documentation; third-party components and data retain their own licenses. Read the [disclaimer](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/DISCLAIMER.md) and [third-party notices](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/NOTICE.md).

## Download 0.12.1

| Distribution | Requirements |
| --- | --- |
| [Windows x64 installer](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/OceanRoute-0.12.1-Windows-x64-Setup.exe) | About 80 MiB. Target: Windows 10 / 11 Intel / AMD 64-bit. Bundled Python and production dependencies; offline installation |
| [Portable source ZIP](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/OceanRoute-0.12.1-portable.zip) | Includes the compiled interface. Launcher requires Python 3.10+ and internet access for first dependency installation |
| [Python wheel](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/oceanroute-0.12.1-py3-none-any.whl) | Python integration; install dependencies separately |
| [User manual PDF](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/OceanRoute-0.12.1-User-Manual.pdf) / [Design PDF](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/OceanRoute-0.12.1-Design-Document.pdf) | Frozen version documents: 26 and 30 pages respectively |
| [SHA256 checksums](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/SHA256SUMS-assets-v0.12.1.txt) / [Original manifest](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/manifest-0.12.1.json) | Verify download integrity; original artifacts retain their frozen bytes |

The installer is **unsigned, and native Windows hardware acceptance has not been completed**. Existing Windows binary and installer tests ran under Wine / Rosetta. Download from this repository's Releases and verify checksums; do not disable security software to bypass warnings.

Windows data is stored in `%LOCALAPPDATA%\OceanRoute`. Closing the browser does not stop the backend; use the Start menu's OceanRoute stop entry. Uninstallation preserves user data. See [installation guidance](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/docs/WINDOWS_INSTALL.md).

[All 13 releases](https://github.com/Jane-o-O-o-O/openOceanRoute/releases) include the corresponding source ZIP, wheel, manual, design document and integrity files. Windows EXE distributions start at 0.12. Earlier versions are historical snapshots; **0.12 has retained known failures, so prefer 0.12.1**. GitHub-generated source archives are different from uploaded portable distributions.

## Actual workspace

The images below are unchanged copies from the 0.12.1 browser verification. They show synthetic examples and test cases, except for public NOAA chart reference data. They are not field survey results.

![Route map, KP–depth profile and project overview](https://jane-o-o-o-o.github.io/openOceanRoute/assets/screenshots/route-workspace.png)

*Route workspace with model-based duration and cost estimates.*

![WGS84 arc edit candidate with explicit missing-depth state](https://jane-o-o-o-o.github.io/openOceanRoute/assets/screenshots/arc-edit-candidate.png)

*Review the complete candidate before applying and saving. This screenshot shows an unapplied candidate; stale profiles are disabled and unknown depth is not replaced with zero.*

<details>
<summary>Projected geometry, S-57, automatic rules and three-dimensional model</summary>

![True radius arc preview in projected metre coordinates](https://jane-o-o-o-o.github.io/openOceanRoute/assets/screenshots/projected-radius-preview.png)

*Synthetic geometry example with missing continuous depth still reported.*

![Native NOAA S-57 reference chart layers](https://jane-o-o-o-o.github.io/openOceanRoute/assets/screenshots/native-s57-chart.png)

*S-57 object geometry and metadata are reference GIS data, not S52 / ECDIS navigation rendering or automatically validated engineering bathymetry.*

![Automatic rule location in the map and depth profile](https://jane-o-o-o-o.github.io/openOceanRoute/assets/screenshots/automatic-rule-location.png)

*Read-only location of a synthetic rule error; finite sampling cannot prove continuous seabed safety.*

![Three-dimensional steady-state cable research model](https://jane-o-o-o-o.github.io/openOceanRoute/assets/screenshots/steady-state-3d.png)

*Independent synthetic research case, without original-vendor golden outputs or field accuracy certification.*

</details>

## Implemented scope

| Area | Implementation |
| --- | --- |
| Projects | SQLite persistence, multiple paths and shared assemblies, manufacturing relationships, fixed / flexible cable lengths and revisions |
| Route geometry | WGS84 geodesic and rhumb lines, Split AC shaping, Radius AC true arcs, geographic / projected edit candidates, explicit apply and undo |
| Planning outputs | RPL templates and import / export, maps, KP–depth profiles, straight-line diagrams, slack and duration / cost estimates |
| GIS and terrain | CRS transformations, open reference formats, native S-57 objects, XYZ / GeoTIFF, multiple sources, provenance and explicit NoData |
| Rules and search | Crossing, proximity, longitudinal / lateral slope checks, condition combinations and KP locations; finite-budget avoidance with independent review |
| Cable models | Analytical catenary, static and bounded three-dimensional steady / dynamic examples, heterogeneous segments, point loads, currents and limited seabed contact |
| Operations research | ShipPlan, LookAhead, tension search, sea-state / RAO, current inversion and partial repair workflows |

See the detailed [implementation status](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/docs/IMPLEMENTATION_STATUS.md) and [model notes](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/docs/MODEL_NOTES.md). Full vessel six-degree-of-freedom motion, lifetime fatigue, complete friction history, live vessel equipment integration and continuous seabed safety proof are outside the implemented scope.

## Recorded verification

These are **actual full-run results for the frozen 0.12.1 delivery**, not new tests performed by this documentation update. Specialized reruns are not added to the totals.

| Verification | Result |
| --- | --- |
| macOS backend | 2,232 passed; no failures, errors or skips |
| Chrome scenarios | 116 passed once each; no retries, failures or skips |
| Windows PE binaries under Wine / Rosetta | 2,231 passed; one POSIX-specific test naturally skipped; no failures or errors |
| Portable fresh installation | Full 2,232 passed; actual HTTP, analysis, persistence and reopening exercised |
| Installer under Wine / Rosetta | Upgrade, single instance, save / reopen, uninstall retaining data and reinstall payload checked |
| Visual review | 26 manual pages, 30 design pages and 117 browser screenshots reviewed |

Historical failures and fixes remain documented. Read the [stable delivery record](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/docs/STABLE_0.12.1.md), [release notes](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/docs/RELEASE_NOTES.md) and [Windows build record](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/docs/WINDOWS_BUILD.md). Passing tests does not establish vendor equivalence, field accuracy, navigation approval or marine engineering safety certification.

## Run a Git checkout

The installer needs no separate Python or Node.js installation. The portable ZIP includes the built interface. A Git checkout requires Python 3.10+, Node.js / npm compatible with the checked-in lockfile, and an interface build:

```bash
git clone https://github.com/Jane-o-O-o-O/openOceanRoute.git
cd openOceanRoute
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[terrain]'
npm --prefix web ci
npm --prefix web run build
.venv/bin/python -m oceanroute --open
```

On Windows, first create the environment with `py -m venv .venv` (or an installed `python -m venv .venv`); then use `.venv\Scripts\python.exe` for subsequent Python commands. The source CLI defaults to local `127.0.0.1:8765`; use `--port` if that port is occupied. The installer launcher can select an available port. Source data defaults to `.oceanroute`; `OCEANROUTE_DATA_DIR` can select another directory. Do not expose this research workspace directly to the public internet.

## Documentation and community

- [User manual](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/docs/USER_MANUAL.md), [design](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/docs/DESIGN.md), [FAQ](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/FAQ.md) and [support](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/SUPPORT.md).
- [Contributing](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/CONTRIBUTING.md), [code of conduct](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/CODE_OF_CONDUCT.md), [security reporting](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/SECURITY.md), [Issues](https://github.com/Jane-o-O-o-O/openOceanRoute/issues) and [Discussions](https://github.com/Jane-o-O-o-O/openOceanRoute/discussions).
- [MIT license](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/LICENSE), [disclaimer](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/DISCLAIMER.md), [third-party notices](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/NOTICE.md), [acknowledgments](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/ACKNOWLEDGMENTS.md) and [citation metadata](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/CITATION.cff).

## Search and AI discovery

The public website includes descriptive metadata, canonical URLs, Open Graph, SoftwareApplication structured data, a sitemap and factual FAQ. [llms.txt](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/llms.txt), [full project text](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/llms-full.txt), [structured metadata](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/project.json) and the [release asset catalog](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/release-catalog.json) provide readable facts and source links.

These improve machine-readable discovery; they do not add AI search or AI route planning to the application, and do not guarantee indexing or ranking by any search engine or AI service.



## Frequently asked questions / 常见问题

### openOceanRoute 和 OceanRoute 是什么关系？

openOceanRoute 是公开项目与仓库名称；当前软件界面与发行文件使用 OceanRoute，稳定交付版本为 0.12.1。部分界面标签简写为 0.12。

### 它与 MakaiPlan / MakaiPlan Pro 有授权或关联吗？

没有。MakaiPlan / MakaiPlan Pro 是 Makai 的商业软件；本项目依据公开资料独立开发，不含原厂程序、授权或付费资源，不是其官方产品或获授权的兼容替代品。

### 这是 AI 自动规划软件吗？

不是。当前功能由明确的几何、工程规则与数值研究模型实现。网页提供可检索的项目说明，不表示产品具有 AI 路线规划功能。

### Windows 安装包需要另装 Python 或 Node.js 吗？

不需要。Windows x64 离线安装包内置 Python 与生产依赖，通过系统默认浏览器打开本地工作区。便携源码 ZIP 含预编译界面，启动器需要 Python 3.10+ 和首次联网；从 Git 克隆源码还需用 Node.js / npm 编译界面。

### 安装器签名了吗？Windows 实机验证完成了吗？

均未完成。现有 Windows 二进制与安装流程在 Wine / Rosetta 兼容层中测试，不能代替原生 Windows 实机验收。请从项目 Releases 下载，并依据发行记录核对 SHA256；不要把关闭安全软件作为安装步骤。

### 地图、海图和自动规则能证明工程安全吗？

不能。S-57 是参考 GIS 图层，不是官方航海呈现或已转换工程水深；有限规则检查、地形采样与研究计算不构成连续海底安全、原厂等效或现场工程认证。

### 圆弧或路线坐标编辑会直接覆盖已保存工程吗？

圆弧端点编辑先产生完整工程候选，用户显式应用后更新工作区；保存由用户另行执行。工作区支持整体撤销，过期输入或文档的候选不能继续应用。

### 源码许可、功能限制和文档在哪里？

项目代码使用 MIT 许可。第三方依赖与 NOAA 测试资料保留各自来源和许可；详细操作见 用户手册，未完成能力与验证限制见 状态矩阵。

## History and discovery

The initial 300 commits were reconstructed with August/September 2026 dates and actually generated on 2026-10-07. [Disclosure](https://github.com/Jane-o-O-o-O/openOceanRoute/blob/main/HISTORY_RECONSTRUCTION.md). Search and AI-readable metadata provide factual project discovery, not an application AI feature or an indexing guarantee.

[Illustrated website](https://jane-o-o-o-o.github.io/openOceanRoute/) · [Repository](https://github.com/Jane-o-O-o-O/openOceanRoute) · [llms.txt](https://jane-o-o-o-o.github.io/openOceanRoute/llms.txt)
