# MCP 功能完成矩阵

更新于 2026-10-30。目标是让局域网 AI 尽可能管理 NAS；当前有 **75 个真实工具回调**，并未完成全部海康 APP、客户端和云端功能替代。以下把「已实现」「当前 NAS 是否通过门槛」「仍缺什么」分开记录，不把固件动作表、工具注册、HTTP 200、原厂 ACK 或本地 fixture 当作实机完成证明。

目录唯一来源为 `runtime/mcp_service.py`，原厂第二批定义为 `runtime/native_capability_specs.py`；实际调用与 `api_permissions.py` 使用同一角色、域、精确工具和资源判定。75 项组成：原 36＋文件只读／传输 5＋上传／任务 3＋下载器扩展 6＋未来 Docker 部署 4＋原厂第二批 17＋缓存监控 3＋固定项目资源读数 1。二十项既定文件／Docker 合同中 15 项已实现，5 项文件变更待接入；覆盖也未开放，扩展工具不冒充这些未实现合同。

## 权限与网页管理

已实现独立 Token 的只读／读写／管理员、逐域及逐工具授权、文件子目录、现有项目、未来部署资源、真实来源 IP／CIDR、期限、编辑、启停、撤销和轮换。API 管理有三个页面；未来部署可直接在网页填写固定资源合同、登记、撤销和重核生命周期，机器不能自授权限。写资源默认不选，降级清除，提高角色不恢复；只改元数据省略未改的授权字段。

存储 v5 增加 `docker_deployments`，policy 仍为 v3。旧 v4 只读不改文件字节，真实写才原子迁移；旧字段与权限保留，新资源缺省为空，policy v1／v2 仍只原十工具。MCP 默认关闭；网页登录会话、小屏令牌和开发窗口不能替代机器 Bearer，管理员也不绕过资源范围。详见[MCP 与 API](mcp-api.md)和[管理界面](mcp-management-ui.md)。

## 各领域实际范围

| 用户目标 | 已实现工具 | 仍缺具体功能 | 证据与实机条件 |
| --- | --- | --- | --- |
| 健康、设备、网络、存储和应用状态 | `get_nas_status/get_monitoring_history/get_health_events/get_network_traffic_totals`；五类既有业务缓存 | 网络配置、存储池创建／格式化／修复、休眠阈值修改等 | 十一项固定指标历史、同代健康事件及日／月观测流量仅读缓存，未知保持 null，过期或缺测不报健康；不临时 SMART 或刷新原厂。完整 SN 不扩大机器权限。见[缓存监控工具](mcp-monitoring-tools.md) |
| 风扇与温控 | `set_fan_policy/release_fan` | 未核验固件／驱动的通用调速、无测速设备的真实 RPM | 保留原厂二进制、唯一进程、设备／驱动、租约及温度保护；系统自动档与高／中／低须真实交接，不把缓存／ACK 当物理转速。实机排障固定 `/api/dev/fan` |
| 灯效 | `set_light_policy/set_rgb` | 原厂四种效果之外的任意灯带／地址控制 | 固定硬件与现有效果，保留控制器预检；不盲扫 I²C |
| 小屏与屏保 | `set_screen/update_screen_settings` | 通过机器 Token 管理小屏服务授权、任意原厂窗口／脚本控制 | 既有设置与实际状态分开，不能由保存勾选推断接管成功；服务开关只能所有者管理 |
| 电源 | `request_power/cancel_power` | 固件升级、网络唤醒／自动开机等未审定控制 | 管理员、确认、五秒可取消；不拿其他系统 getter 扩展电源权限 |
| 文件浏览与下载 | `list_file_roots/list_directory/get_file_info/prepare_file_download/get_file_transfer/cancel_file_transfer` | 其他用户私有树、完整文件管理与样本内容验收 | 仅匹配固件的固定原厂所有者及 admin/public 正子集，完整逻辑 ACL、同挂载、最终句柄和逐块授权同时通过。364b858 已浏览三个实际根的 admin 目录；目录证明不等于下载字节验收。规范 /drives 外完整别名不再误伤唯一规范根，也不授别名权限；SSD 新规划仍待本批部署后实测。见[真实只读后端](native-file-readonly.md) |
| 文件上传及管理 | `prepare_file_upload/commit_file_upload/get_file_operation`，固定 Bearer PUT 字节通道 | 新建目录、覆盖、改名、复制、移动、原厂回收站和其他用户写入 | 仅已核验内置 SATA 的 admin 后代新文件，最低读写＋files＋具体工具＋目标及父目录读写范围。匿名暂存、大小/SHA-256、同 inode 无覆盖发布、固定原厂通知和独立真实索引读取分别核验；通知 ACK 不算完成，索引未确认保持 pending／result_unconfirmed，不明结果不重放。只通过代码／fixture，未 NAS 写验收。见[上传通道](file-upload-transport.md) |
| 已有 Docker 项目 | `list_docker_projects/get_docker_project_status/get_docker_project_summary/get_docker_project_resource_usage/control_docker_project/plan_docker_project_update/execute_docker_project_update/get_docker_project_task` | 任意 Compose 编辑、多服务复杂重建、任意 bind／设备／特权操作 | 所有者登记项目，精确 `inspect/control/update_image`；资源只读逐服务 stats，无实际双帧则 CPU 百分比为 null，不估算工作集或重复汇总。写保持管理员与固定身份，更新保留卷与未知现场。见[项目读取](docker-projects.md)及[项目操作](docker-project-operations.md) |
| 新建 Docker 服务 | `list_docker_deployments/plan_docker_deployment/execute_docker_deployment/get_docker_deployment_task`；支持 `deploy/update/start/stop/restart/remove` | 任意多服务 Compose、创建卷／网络、bind、环境／命令透传、私有仓库认证、无约束镜像运行 | 所有者先登记独立 `deployment_*` 固定合同，精确 `inspect/deploy/control/remove`。已有 local 具名卷／受限 bridge 或 none、固定仓库／IP／端口、非 root 和资源限额。Toolbox 卷按真实逻辑／挂载身份保护；删除已停止容器保留卷，健康／Running 同时核验后才清旧备份。仅隔离 HTTP／Unix fixture，NAS 写验收需专用资源。见[受控部署](docker-deployments.md) |
| 下载器 | `list_download_tasks/get_download_task/pause_download_task/resume_download_task/remove_download_task/set_download_task_limits/get_download_limits/set_download_limits/recheck_download_task/rename_download_task/set_download_task_force_start/set_download_task_queue_position`，另有缓存工具 | 新建链接／种子任务、修改下载目录、删除数据、Tracker／分类等完整设置、下载器安装 | 前六项任务工具支持固定 qB／aria2；全局限速写需管理员，后四项仅 qB。唯一监听、连接后授权、任务／配置版本、确认和本键幂等；不传目录或内容。委托原生引擎不等于整个进程被 Users 沙箱隔离。专用任务逐项验收 |
| 应用市场与安装 | `get_app_status/list_native_applications/list_native_modules/get_application_install_progress/plan_native_application_install/install_native_application` | 其他应用安装、更新／卸载、完整异步任务与失败恢复、通用应用启停 | 新装仅已审计 ID34/netdisk_bd2，两目录精确交叉核对未安装和包字段；管理员计划不会执行安装，执行另须当前版本、确认、本键幂等及 plan 授权。不收 URL／脚本／路径；可能联网、写独立目录和启动进程，ACK 仅受理、不明不重放。原厂包诊断不授通用安装权，未在 NAS 执行新装。见[原厂第二批](native-capability-completion.md) |
| 影视墙 | `get_movie_status/list_movie_libraries/rename_movie_library/get_movie_library_scan_status` | 新建／删除库、来源／成员、发起扫描、匹配／字幕、播放／收藏等 | 真实原厂目录／状态和名称修改，不返回媒体源路径、不推算进度；配置写须核对嵌套结构、归属及文件范围。当前需已有影视库比对，未创建样本 |
| 相册、识别与分享 | `get_album_status/get_share_status/get_media_status/list_albums/list_shared_albums/create_album/rename_album/delete_album/revoke_share`；系统另有 AI 开关工具 | 照片内容／上传／整理、人物／成员管理、相册邀请、创建分享链接／密码／有效期、完整手机备份 | 原厂目录也可能初始化／修复状态，最低读写。删除只当前所有者普通 custom 相册且 ID 大于七，不混用共享／保留类型；ACK 不代表清理完成。分享创建需服务端 ID 与真实外链，不拼 URL。写验收先指定独立对象 |
| 备份与同步 | `list_backup_tasks/get_auto_backup_settings/disable_auto_backup/pause_backup_task/cancel_backup_task` | 创建／恢复任务、开启新自动导入、改变源目标、同步计划与冲突恢复 | 固定当前所有者，未知原始状态不猜。管理员只关闭、暂停／取消既有任务；可能停止原厂线程或清理失败记录，未证明异步完成。新目标和恢复须文件 ACL／索引合同 |
| 用户、名称与系统设置 | `list_nas_users/get_nas_name/set_nas_name/get_nas_ai_settings/set_nas_ai_enabled` | 用户增删、口令重置、权限修改、网络／存储设置、升级、安全与云账号管理 | 最小用户字段不含云账号／邮箱／凭据；名称修改可能通知云端，AI 开关可能启动既有识别。全部管理员及精确工具；不授模型照片或文件内容。见[原厂第二批](native-capability-completion.md) |
| 手机 APP／云端功能 | 当前只有上述 NAS 端已审定能力 | 云账号／会员、推送、手机后台备份、投屏、远程隧道、客户端本地功能和外设交互 | 固件路由表不足以证明 APP 行为；需当前 APK／正常调用和客户端流程逐项核对。不承诺全部 APP 等价，也不将固件 902 个注册项换算成覆盖率 |

## 固定开发入口与下一次实机验收

1. 开发模式必须有非空实际来源 IP／CIDR 白名单和有效绝对窗口；同源、JSON、CSRF 不豁免。调试只走镜像固定 API，不用 SSH，不反复为少量字段换镜像试探。
2. `/api/dev/diagnostics` 目录十项，默认仍仅 `device/storage/business/fan`。`file_backend_context` 只看身份／句柄元数据；新增 `file_protocol` 显式采集四阶段 errno、预期／实际 mount／device、namespace 和固定协议形状。诊断不授文件执行权，失败保持真实原因。
3. `/api/dev/media` 的 `probe_business`＋`confirm:true` 固定三目录；`/api/dev/native` 严格 `{"action":"probe","confirm":true}` 或 `{"action":"packages","confirm":true}`。前者固定六类原厂目录／设置，后者为脱敏包字段、枚举、URL 类别和校验格式；不安装、执行 setter 或造样本，可能更新原厂状态／唤盘，均不加入默认检测。
4. `364b858` 已在实机确认三个实际 `Users` 根的挂载身份，并以当前来源限定的临时只读 Token 浏览视频资源、备份磁盘和主磁盘的 `admin` 目录；Token 已撤销并验证 HTTP 401。视频资源和主磁盘目录属性成功，备份磁盘的一次属性读取拒绝原因尚未确定，随后浏览成功；四个缺少 `Users` 的分区候选继续拒绝。下一步用所有者指定的小文件核对整文件 SHA-256、HEAD、单 Range、撤权续验；目录浏览通过不能替代内容下载和写入验收。
5. Docker、下载、相册、备份和系统写入各用明确指定的专用对象；先读取比对，再单项确认／执行并核对真实终态。部署复用原 Toolbox 项目与数据卷，不用 `down -v`，不将现有业务当压力／破坏样本。

自动化覆盖固定 HTTP／Unix 传输、真实 Linux 句柄、ACL 正子集、权限矩阵、旧格式兼容、幂等、并发、撤权、对象变化和故障保留。Linux CI、镜像发布、NAS 来源／Token 调用、实际业务终态须分别记录；任何一层通过都不能替代其余证据。
