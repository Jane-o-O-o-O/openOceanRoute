# Windows 0.12.1 打包与复核

面向 Windows 10/11 x64 的当前用户 NSIS 安装器，保留现有本机浏览器界面。安装器内含 Windows CPython 3.13.9、31个第三方生产分发包和 OceanRoute 0.12.1，不包含 pytest、httpx、Node、pip 等测试或构建工具。运行和安装不下载依赖。

生产源码位于 `packaging/windows/`：`windows_app_0_12_1.py` 处理单实例、数据路径和服务关闭；`launcher_0_12_1.nsi` 生成启动 EXE；`installer_0_12_1.nsi` 生成向导；`uninstall-files-0.12.1.nsh` 和 `upgrade-owned-0.12.nsh` 精确列出本版安装文件及11条已核实旧版文件。删除操作不递归清空用户目录。

`build_windows_0_12_1.py` 的实际输入、执行命令、源文件哈希及工具版本记录在 `resources/validation/development_0.12.1_windows_packaging_source_manifest.json`，最终编译记录为 `release_0.12.1_windows_build_final.json`。用户可查看源码和构建记录；便携源码 ZIP 不附带私有工具缓存和约400MB Windows依赖运行时，不能据此称 ZIP 单独可复现同字节安装器。重新构建须按输入记录准备冻结的 Windows Runtime、NSIS工具、新wheel、编译界面和两份PDF。`requirements-frozen.txt` 保留此次版本选择；测试依赖在独立目录，不能整体混入产品。

稳定交付实际使用 Windows 二进制在 macOS Wine 11.0/Rosetta 中执行，而非原生 Windows 机器。后端全量2232测试中的2231通过、1项POSIX目录fsync自然跳过；真实Windows Node测试工具只用于独立QA，三项中文/浮点JSON往返均通过。安装器另行验证从真实0.12旧版本升级、双启动单实例、四项保存关闭重开、六项物理合成例、原始用户数据库与无关文件保留、实际卸载进程退出及重装4152生产文件一致。所有结果分别来自实测，不累计各次执行。

第一安装候选曾遗留旧版11个自有文件，使安装目录显示33个分发；原候选、最初范围较窄的通过报告及后续失败审计均保留。最终候选精确清理旧文件，并从原0.12实际安装重新验证，升级和重装均严格32个分发、唯一OceanRoute 0.12.1。卸载等待使用外部Windows Python的`Popen.wait`真实进程句柄，记录PID和退出码，未以文件消失代替进程退出。

安装器没有代码签名。当前证据不证明原生Windows内核、驱动和SmartScreen体验，也不证明Makai原厂格式或现场精度等效。原生Windows实机结果如有补充，应独立记录，不能改写本轮兼容层证据。
