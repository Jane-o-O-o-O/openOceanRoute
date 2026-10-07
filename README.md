# openOceanRoute · 独立海底电缆路由规划

![OceanRoute 原创海缆示意头图，非工程地图](assets/oceanroute-banner.svg)

[![Version 0.12.1](https://img.shields.io/badge/version-0.12.1-087e8b)](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/tag/v0.12.1)
[![MIT License](https://img.shields.io/badge/license-MIT-18794e)](LICENSE)
[![Windows x64](https://img.shields.io/badge/target-Windows%2010%2F11%20x64-0078d4)](docs/WINDOWS_INSTALL.md)
[![Research models](https://img.shields.io/badge/models-research-805ad5)](docs/MODEL_NOTES.md)

**[项目介绍](https://jane-o-o-o-o.github.io/openOceanRoute/) · [下载与发行记录](https://github.com/Jane-o-O-o-O/openOceanRoute/releases) · [用户手册](docs/USER_MANUAL.md) · [设计文档](docs/DESIGN.md) · [English](README.en.md)**

openOceanRoute 是独立开发、采用 MIT 许可的海底电缆路由规划项目。软件和安装包使用 **OceanRoute** 名称，当前稳定交付为 **0.12.1**：提供 WGS84 路线与真实圆弧、RPL 工程位置表、GIS 与水深地形、规则检查及有明确边界的力学研究模型，通过本地服务和浏览器工作区运行。

**MakaiPlan / MakaiPlan Pro 是 Makai 的商业软件。本项目与 Makai 无隶属、授权或背书关系，不提供原厂许可证，也不承诺原厂算法、文件兼容或现场精度等效。** MIT 仅覆盖本项目自有代码与文档，第三方组件和数据保留各自许可。请阅读 [免责声明](DISCLAIMER.md) 与 [第三方声明](NOTICE.md)。

## 下载 0.12.1

| 文件 | 用途与要求 |
| --- | --- |
| [Windows x64 EXE 安装包](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/OceanRoute-0.12.1-Windows-x64-Setup.exe) | 约 80 MiB；目标 Windows 10 / 11 Intel / AMD 64 位；内置 Python 和生产依赖，可离线安装 |
| [便携源码 ZIP](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/OceanRoute-0.12.1-portable.zip) | 含预编译界面；启动器需要 Python 3.10+，首次安装依赖需要联网 |
| [Python wheel](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/oceanroute-0.12.1-py3-none-any.whl) | 适合 Python 环境集成；需另行安装依赖 |
| [用户手册 PDF](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/OceanRoute-0.12.1-User-Manual.pdf) / [设计文档 PDF](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/OceanRoute-0.12.1-Design-Document.pdf) | 对应冻结版本的原始文档，分别 26 / 30 页 |
| [SHA256 校验值](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/SHA256SUMS-assets-v0.12.1.txt) / [原始发行清单](https://github.com/Jane-o-O-o-O/openOceanRoute/releases/download/v0.12.1/manifest-0.12.1.json) | 核对下载完整性；原始包和清单保持冻结字节 |

安装器**未签名，尚未完成原生 Windows 实机验收**。现有 Windows 二进制和安装流程验证在 Wine / Rosetta 兼容层中完成。请从本项目 Releases 下载并核对 SHA256；不要关闭安全软件来绕过提示。

安装后通过开始菜单启动 OceanRoute，浏览器会打开本地工作区。Windows 用户数据位于 `%LOCALAPPDATA%\OceanRoute`；关闭浏览器不会终止后台服务，可使用开始菜单中的“关闭 OceanRoute”。卸载保留用户数据。详情见 [Windows 安装说明](docs/WINDOWS_INSTALL.md)。

[全部 13 个版本](https://github.com/Jane-o-O-o-O/openOceanRoute/releases)包含相应源码 ZIP、wheel、手册、设计文档、校验文件及已有安装器。Windows EXE 从 0.12 开始提供；旧版本是历史快照，**0.12 候选保留已知失败记录，请优先使用 0.12.1**。GitHub 自动生成的 Source code 压缩包与上传的便携发行包不同。

## 实际界面

下图来自 0.12.1 完整浏览器验证的原截图副本。除公开 NOAA 海图参考数据外，内容为合成示例与测试用例，不是海上现场测量；头图是原创装饰性 SVG。

![路线工作区：地图、KP 水深剖面与工程概览](assets/screenshots/route-workspace.png)

*路线地图与 KP–Depth 剖面共享工程数据。工期、费用属于模型估算。*

![WGS84 圆弧编辑：拖动后的完整候选及明确缺测提示](assets/screenshots/arc-edit-candidate.png)

*圆弧端点移动后先审查候选，再显式应用和保存。截图尚未应用；过期剖面停用，缺测水深不补零。*

<details>
<summary>展开投影坐标、S-57、自动规则与三维模型截图</summary>

![米制投影坐标中的真实半径圆弧候选](assets/screenshots/projected-radius-preview.png)

*米制投影中的路线几何；连续水深缺测提示保留，预览不代替工程校核。*

![NOAA 原生 S-57 海图参考图层与来源信息](assets/screenshots/native-s57-chart.png)

*S-57 对象、属性和实际几何作为 GIS 参考，不是 S52 / ECDIS 航海呈现，也未自动转换为工程水深。*

![自动规则错误在地图和水深剖面中同步定位](assets/screenshots/automatic-rule-location.png)

*合成规则错误的只读定位。有限采样和规则检查不能证明连续海底安全。*

![海缆三维稳态研究模型：合成空间形态及张力摘要](assets/screenshots/steady-state-3d.png)

*独立三维研究算例，尚无原厂黄金输出对照或海试精度认证。*

</details>

## 功能范围

| 领域 | 当前实现 |
| --- | --- |
| 工程与制造关系 | SQLite 工程保存、多路径与共享装配、制造库存关系、固定 / 柔性缆长、修订与事务保护 |
| 路线几何与编辑 | WGS84 测地线与恒向线、Split AC 分段整形、Radius AC 真实圆弧、地理 / 投影坐标候选编辑、显式应用与整体撤销 |
| 规划成果 | RPL 与模板导入导出、路线地图、KP–Depth 剖面、SLD、余缆与工期费用估算 |
| GIS 与海图 | 开放格式参考图层、CRS 转换、图层顺序与透明度、原生 S-57 对象 / 属性 / 几何 |
| 水深与地形 | XYZ / GeoTIFF、多源地形与来源追踪、NoData 保留、基准信息、网格与纵向 / 侧向剖面 |
| 自动规则与路线搜索 | 交叉、邻近、纵坡 / 侧坡、显式条件组合、KP 定位；有限预算下的避障搜索与独立复核 |
| 电缆研究模型 | 解析悬链线、静力和三维稳态 / 动态算例、异质缆段、点载荷、海流和受限海床接触、检查点 |
| 施工 / 维修研究流程 | ShipPlan、LookAhead、张力搜索、海况 / RAO、海流反演与部分维修流程 |

逐项完成状态、输入合同和未实现部分见 [功能范围](docs/IMPLEMENTATION_STATUS.md) 与 [模型说明](docs/MODEL_NOTES.md)。当前实现不提供完整船舶六自由度、全寿命疲劳、完整摩擦历史、实时船载设备集成或连续海底安全证明；专有原厂格式兼容也不作保证。

## 已有验证与实际边界

以下数字来自**冻结 0.12.1 的完整实际执行记录**，不是本次文档更新的新测试，也不累计专项重跑次数：

| 验证 | 结果 |
| --- | --- |
| macOS 完整后端 | 2,232 通过，零失败 / 错误 / 跳过 |
| Chrome 浏览器场景 | 116 场景各执行一次，零重试 / 失败 / 跳过 |
| Windows PE 二进制，Wine / Rosetta | 2,231 通过，1 项 POSIX 专属测试自然跳过，零失败 / 错误 |
| 便携源码包全新环境安装 | 完整 2,232 通过；实际 HTTP、分析、工程保存和重开已执行 |
| Windows 安装流程，Wine / Rosetta | 升级、单实例、保存重开、卸载保留数据及重装载荷校验已执行 |
| 文档与界面视觉检查 | 用户手册 26 页、设计文档 30 页逐页检查；117 张浏览器截图检查 |

失败过程和修复过程保留，没有覆盖旧证据。详情见 [稳定交付记录](docs/STABLE_0.12.1.md)、[发行说明](docs/RELEASE_NOTES.md) 和 [Windows 构建验证](docs/WINDOWS_BUILD.md)。测试通过不构成原厂等效、工程安全、航海认证或现场精度证明。

## 从源码运行

Windows 用户可直接使用上方 EXE。便携 ZIP 解压后按其中启动器与手册运行，无需另行编译界面。

从 Git 克隆源码时，需要 Python 3.10+ 和满足 `web/package-lock.json` 的 Node.js / npm 环境，并编译界面：

```bash
git clone https://github.com/Jane-o-O-o-O/openOceanRoute.git
cd openOceanRoute
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[terrain]'
npm --prefix web ci
npm --prefix web run build
.venv/bin/python -m oceanroute --open
```

Windows 先运行 `py -m venv .venv`（或已安装的 `python -m venv .venv`），创建环境后各 Python 命令使用 `.venv\Scripts\python.exe`。源码 CLI 默认仅绑定本机 `127.0.0.1:8765`；端口占用时通过 `--port` 指定其他端口。安装器启动器可选择空闲端口。源码数据默认保存在 `.oceanroute`，可通过 `OCEANROUTE_DATA_DIR` 指定目录。请勿将该研究工作区直接暴露到公网。

## 文档与项目协作

| 资料 | 内容 |
| --- | --- |
| [用户手册](docs/USER_MANUAL.md) / [设计文档](docs/DESIGN.md) | 工作流程、输入输出与架构 |
| [FAQ](FAQ.md) / [支持渠道](SUPPORT.md) | 安装、数据、模型和使用问题 |
| [贡献说明](CONTRIBUTING.md) / [行为准则](CODE_OF_CONDUCT.md) | 稳定性、文档、缺陷修复及协作约定 |
| [安全报告](SECURITY.md) | 漏洞报告与支持范围 |
| [MIT 许可证](LICENSE) / [免责声明](DISCLAIMER.md) / [第三方声明](NOTICE.md) | 使用权、边界和第三方许可 |
| [致谢](ACKNOWLEDGMENTS.md) / [引用信息](CITATION.cff) | 依赖、数据来源与项目引用 |
| [Issues](https://github.com/Jane-o-O-o-O/openOceanRoute/issues) / [Discussions](https://github.com/Jane-o-O-o-O/openOceanRoute/discussions) | 缺陷反馈、公开问答和讨论 |

## 搜索与 AI 检索

[公开项目介绍页](https://jane-o-o-o-o.github.io/openOceanRoute/)提供准确的中英文描述、截图、FAQ、canonical、Open Graph、SoftwareApplication 结构化数据与 sitemap。[llms.txt](llms.txt)、[完整项目文本](llms-full.txt)、[结构化项目元数据](project.json)及[发行附件目录](release-catalog.json)便于读取事实和追踪来源。

这些材料用于改善搜索和 AI 检索的可读性，**不表示软件新增了 AI 搜索或 AI 路线规划功能，也不保证任何搜索引擎 / AI 服务的收录与排名**。

English keywords: independent submarine cable route planning, subsea cable engineering research, bathymetry, GIS, WGS84 geodesy, RPL, straight-line diagram, catenary, cable laying simulation, Windows installer.

## 历史记录说明

前 300 个提交是依据 0.1 至 0.12.1 冻结快照重建的历史，2026 年 8 月 140 个、9 月 160 个。它们的作者和提交者日期均为**重建日期**，实际生成于 2026-10-07，提交正文已标注 `Reconstructed-History: true`；详情见 [重建历史说明](HISTORY_RECONSTRUCTION.md)。本次 MIT、介绍页和文档完善采用真实提交时间，不改变这 300 个记录或原始发行包。
