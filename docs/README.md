# R1 Toolbox 文档

R1 Toolbox 是海康 HikSemi R1 NAS 的开源增强工具箱。本文档面向**安装了原版海康系统**的 R1；其他机型或第三方系统不在当前支持范围。

- 项目主页与快速开始：[README](../README.md)
- 在线预览（演示数据）：[https://shinku-chen.github.io/R1-Toolbox-Hiksemi/](https://shinku-chen.github.io/R1-Toolbox-Hiksemi/)
- 问题与发布：[Issues](https://github.com/Shinku-Chen/R1-Toolbox-Hiksemi/issues) · [Releases](https://github.com/Shinku-Chen/R1-Toolbox-Hiksemi/releases)

## 先读这些

- [部署与升级](deployment.md)：Docker Compose、端口、数据卷 `r1_toolbox_data`、升级与回退。
- [小屏与网页使用说明](interface-guide.md)：小屏六标签、网页入口、登录口令和常用操作。
- [功能与支持条件](features.md)：当前能力、依赖条件和不可用时的表现。
- [架构与运行时](architecture.md)：容器前台监督、宿主服务和恢复路径。
- [开发与测试](development.md)：构建、测试、CI 和发布契约。

## 使用与界面

- [网页面板](web-panel.md)：fnMonitor 框架、R1 扩展和页面分组。
- [屏幕保护](screen-saver.md)：时间、亮度、显示项目和超时设置。
- [屏幕与触摸布局](interface-guide.md)：376 × 960 小屏和窄屏适配。
- [功能矩阵](features.md)：小屏、网页、API 的能力对照。

## 存储、风扇与灯光

- [容量刷新与显示缓存](capacity-refresh.md)：按需容量采集、缓存边界和失效规则。
- [SMART 按需刷新](smart-refresh.md)：待机保护、三十秒缓存和手动强制刷新。
- [磁盘名称](disk-names.md)：原厂名称、文件系统卷标和 `/drives` 回退。
- [独立风扇温控](fan-policy.md)：CPU／系统双通道、温度来源和手动曲线。
- [灯光策略](light-policy.md)：原生灯效、夜间亮度和温度联动。
- [风扇实机验收](fan-acceptance.md)：接管预检、转速和恢复记录。

## 网络、监控与业务

- [监控、趋势与本地健康记录](monitoring.md)：实时采集、历史曲线和健康分级。
- [三端数据覆盖与缺口](data-coverage.md)：小屏、网页和 MCP 的数据来源核对。
- [原厂应用、下载与媒体业务状态](business-diagnostics.md)：被动业务快照和显式刷新。
- [原厂应用数据诊断](vendor-app-diagnostics.md)：应用清单、缓存和刷新边界。
- [原厂下载任务诊断](vendor-download-diagnostics.md)：qB／aria2 查询、会话和副作用。
- [原厂媒体任务诊断](vendor-media-diagnostics.md)：相册、影视、刮削任务的读取边界。
- [磁盘空闲与休眠排查](disk-sleep-diagnostics.md)：I/O 归因、休眠资格和原厂轮询。

## 文件、Docker 与恢复

- [文件变更与恢复](file-mutations.md)：上传、覆盖、复制、移动和私有恢复区。
- [文件上传字节通道](file-upload-transport.md)：分片传输、任务状态和提交边界。
- [文件 admin 总边界](file-users-boundary.md)：可信根、真实句柄和目录核验。
- [原厂文件只读与受限写入](native-file-readonly.md)：固定读取器和写入授权。
- [文件后端上下文](file-backend-context.md)：原厂索引、SQLite 和唤盘影响。
- [受控 Docker 部署](docker-deployments.md)：新服务资源、数据卷和固定模板。
- [固定 Docker 项目读取与授权](docker-projects.md)：项目身份、资源限制和只读统计。
- [Docker 项目操作执行器](docker-project-operations.md)：固定启动、停止、重启和日志。
- [影视追更排序](media-sort-plugin.md)：所有者 Web 插件、数据库规则和撤销。

## API、MCP 与自动化

- [MCP 与 API 管理](mcp-api.md)：局域网 MCP、Token 角色、权限域和来源范围。
- [公开 API](public-api.md)：公开监控与网页设置接口。
- [MCP 能力完成矩阵](mcp-completion-matrix.md)：工具数量、生产门槛和未覆盖范围。
- [MCP 管理界面](mcp-management-ui.md)：服务开关、Token 编辑和轮换。
- [MCP 缓存监控工具](mcp-monitoring-tools.md)：固定监控工具和只读数据来源。
- [MCP 业务状态工具](mcp-business-tools.md)：应用、下载和媒体只读快照。
- [MCP 显式业务操作](mcp-business-operations.md)：原生业务写入、确认和幂等边界。
- [原厂业务固定能力](native-capability-completion.md)：第二批固定执行器和审计。
- [AI 能力接入路线](ai-capability-roadmap.md)：后续领域、限制和待接入项。
- [开发者白名单检测](dev-diagnostics.md)：精确 IP／CIDR、限时窗口和固定检测。
- [原厂媒体显式验证](media-probe.md)：清晰度、监听和文件探针边界。
- [原厂空间管理验证](storage-probe.md)：空间查询、会话复用和副作用。

## 原厂接口与验收记录

以下文档包含固件版本、静态分析或历史实机验收信息；结论以当时证据为限，不自动代表当前设备。

- [原厂接口与 Toolbox 接入范围](vendor-interfaces.md)
- [原厂 REST 与内置网页后端](vendor-web-interfaces.md)
- [原厂 emhistor 静态接口](vendor-emhistor-interfaces.md) · [JSON 清单](vendor-emhistor-interfaces.json)
- [原厂存储与本地服务接口](vendor-storage-interfaces.md) · [JSON 清单](vendor-storage-interfaces.json)
- [Hiksemi R1 原厂接口与独立接管](histor-interfaces.md)
- [用户需求、附件与实现核对](requirements-audit.md)
- [附件功能与协议审查](attachments.md)
- [文件与 Docker 接口方案](file-docker-api-plan.md)
- [文件与 Docker 只读实机核对](file-docker-readonly-acceptance.md)

## 文档约定

- 未知读数保持 `null`／`--`，不用 0 或猜测补齐。
- 涉及硬件、原厂服务或用户文件的能力，以真实身份、挂载和授权核验为前提。
- 部署与升级复用当前 `r1_toolbox_data` 数据卷，不执行 `down -v`。
- 文档中的命令、路径、接口字段保持原样；界面文案使用中文全角标点。

发现文档与当前实现不一致时，以对应代码和自动化测试为准，并提交 Issue 或 PR 修正。
