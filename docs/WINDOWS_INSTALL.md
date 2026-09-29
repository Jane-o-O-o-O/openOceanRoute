# OceanRoute 0.12.1：Windows 安装与启动

本版冻结现有功能，停止新增开发。安装包面向 Windows 10 / Windows 11 的 Intel / AMD 64 位系统（x64），使用系统默认浏览器显示本地工作空间。Windows ARM64、32 位 Windows 和旧版 Windows 尚未验收。

## 安装

1. 双击 `OceanRoute-0.12.1-Windows-x64-Setup.exe`，按向导选择安装目录。
2. 默认安装到当前用户的 `%LOCALAPPDATA%\Programs\OceanRoute`。安装包已带 Python、编译后的界面和地形/GIS依赖；无需安装 Python、Node.js，也无需在安装时下载依赖。
3. 安装完成后启动 OceanRoute，或双击桌面快捷方式。浏览器打开本机工作空间。服务只监听 `127.0.0.1`，默认使用 8765 端口；被占用时自动选择一个空闲端口。
4. 第一次使用时可打开合成示例，完成导入、分析、编辑和保存。已保存工程在当前用户的 `%LOCALAPPDATA%\OceanRoute`，与程序安装目录分开。

本安装包尚未使用商业代码签名证书签名。如果 Windows 显示未知发布者提示，请核对随交付记录提供的文件 SHA256 和文件来源；不要以关闭安全软件作为安装步骤。

## 关闭、重开和卸载

关闭浏览器窗口不会结束本地服务。先保存工作，再从开始菜单的 **OceanRoute → 关闭 OceanRoute** 结束服务；重开 OceanRoute 会重新打开工作空间。也可执行安装目录下的 `OceanRoute.exe --stop`。

卸载从 Windows 的“已安装的应用”或开始菜单的卸载入口执行。卸载会先停止本版服务，再移除程序和快捷方式；保留 `%LOCALAPPDATA%\OceanRoute` 中的已保存工程。自行删除此数据目录会丢失工程，请先备份。

## 排错与适用范围

启动失败时查看 `%LOCALAPPDATA%\OceanRoute\startup.log`。如果服务正在执行长任务，请等任务结束并保存，再关闭或升级。安装包内 `documents` 目录包含用户手册和设计文档；两份文档说明模型、数据限制和已实现的功能。

本软件是独立实现。稳定交付表示冻结现有功能并记录实际测试结果，不表示已取得 Makai 原厂兼容认证或可直接替代海上工程验证。Windows 实机、Wine 兼容层和 macOS 的测试结果在交付记录中分别列出，不能互相替代。
