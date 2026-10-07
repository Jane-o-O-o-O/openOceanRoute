# 常见问题

## 1. openOceanRoute 与 OceanRoute 是什么关系？

openOceanRoute 是项目和仓库名称；当前程序界面、接口及发行文件使用 OceanRoute，稳定版为 0.12.1。部分界面标签简写为 0.12。

## 2. 代码许可是什么？可以商业使用吗？

项目原创部分使用 [MIT License](LICENSE)，可依其条件使用、修改和商业再分发。第三方组件与数据仍受各自许可约束。历史自有代码的许可补充与冻结原包的关系见 [NOTICE](NOTICE.md)；这不意味着取得 Makai 软件使用权。

## 3. 与 MakaiPlan／MakaiPlan Pro 有授权或关联吗？

没有原厂授权、合作或背书关系。本项目依据参考资料独立实现，不包含原厂程序、源码、许可证、原始手册 PDF 或受限模型库，也不是获认证的兼容替代品。原厂产品和许可须另行取得，见[免责声明](DISCLAIMER.md)。

## 4. 这是 AI 自动规划或 AI 助手吗？

不是。当前功能由几何、工程规则与数值研究模型实现。项目页和机器可读说明便于搜索与 AI 检索理解，不代表程序具有大模型助手、AI 路线规划或自动决策功能，也不保证被搜索系统收录。

## 5. Windows 安装包需要另装 Python 或 Node.js 吗？

不需要。Windows x64 离线 EXE 内置 Python、编译界面和生产依赖，使用系统浏览器显示本地工作区。安装器未签名；Windows 实机尚未验收。下载与摘要见[Releases](https://github.com/Jane-o-O-o-O/openOceanRoute/releases)，安装步骤见[Windows说明](docs/WINDOWS_INSTALL.md)。

## 6. Git 克隆与源码便携包有什么不同？

Git 克隆不带编译界面或发行附件，需要 Python 3.10+ 和与 `web/package-lock.json` 兼容的 Node.js／npm（如 Node.js 20 或 22 系列）；`launcher.py` 在缺少界面时会调用 npm 安装并构建。源码便携 ZIP 已带编译界面，无需 Node.js，但需要 Python 3.10+，首次安装依赖需要联网。两者与离线 EXE 是不同安装路径，见 [README](README.md)。

`python -m oceanroute` 的默认端口固定为 8765；占用时需显式指定 `--port`。安装器启动器可选择空闲端口，不能把该行为套用于源码 CLI。

## 7. 当前验证数字是什么？

冻结 0.12.1 正式 macOS 后端 2232 项通过，Chrome 116 个场景各一次通过；Windows PE 在 Wine／Rosetta 中 2231 项通过、1 项 POSIX 专属测试自然跳过。源码便携包另有全新环境完整 2232 项通过记录。这些轮次不相加，Wine 也不是 Windows 实机验证。详见[稳定交付记录](docs/STABLE_0.12.1.md)。

## 8. 可以直接用于航海或海上施工决策吗？

不能据此推定具备这类资格。海图为参考图层，模型未经现场校准；有限规则与采样不证明连续海底安全。工程使用仍需适当数据、专业审查、现场验证及适用要求，详见[模型限制](docs/IMPLEMENTATION_STATUS.md)。

## 9. 数据保存在哪里？关闭浏览器就退出了吗？

Windows 安装版保存于 `%LOCALAPPDATA%\OceanRoute`；源码默认使用当前目录 `.oceanroute/projects.sqlite3`，可通过 `OCEANROUTE_DATA_DIR` 指定目录。关闭浏览器不会结束本地服务，应先保存：Windows 安装版使用开始菜单“关闭 OceanRoute”或 `OceanRoute.exe --stop`；源码 CLI 在运行终端按 Ctrl+C 停止。备份与卸载数据保留见[安装说明](docs/WINDOWS_INSTALL.md)。

## 10. 如何提问题，旧版本还能用吗？

优先使用 0.12.1；0.12 已知 Windows 失败被保留，不推荐部署。普通问题见 [SUPPORT](SUPPORT.md)与[Issues](https://github.com/Jane-o-O-o-O/openOceanRoute/issues)，安全漏洞按 [SECURITY](SECURITY.md) 私密报告。当前冻结现有功能，支持不承诺 SLA 或新功能排期。
