# 安全问题报告

## 当前范围

0.12.1 接受已证实的安全和稳定性问题反馈；这不是持续维护时限或修复 SLA。0.12 及更早版本为历史追溯对象，不建议新部署，尤其不能把 0.12 的 Windows 已知失败记录视为通过。

## 私密报告

仓库已启用 GitHub Private Vulnerability Reporting。请使用 [Report a vulnerability 私密报告入口](https://github.com/Jane-o-O-o-O/openOceanRoute/security/advisories/new)，不在公开 Issues／Discussions 发布未修复漏洞或利用细节。

若无法使用该入口，请通过维护者[GitHub资料页](https://github.com/Jane-o-O-o-O)中可用的非公开联系渠道请求安全联络方式；不要因此改用公开帖发送敏感内容。本项目不另公布私人邮箱，也不承诺响应时限。

报告可包含版本／安装方式、受影响入口、合成最小复现、可能影响和已知缓解方式。删除访问令牌、客户工程、真实敏感坐标与个人信息；不要对他人的系统进行测试。

## 部署事实

程序默认只监听本机 `127.0.0.1`，这不等于完整安全审计或互联网服务认证。不要直接将本地服务暴露到公网。安装器未签名；从项目 Releases 获取文件并核对摘要。工程数据目录与安装目录分离，升级／卸载前仍应备份。

数值正确性、工程安全和平台验证范围另见 [DISCLAIMER](DISCLAIMER.md)及[发行记录](docs/RELEASE_NOTES.md)。
