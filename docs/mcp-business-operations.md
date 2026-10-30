# MCP 显式业务操作

更新于 2026-10-30。工具目录当前七十项：原三十六，加五项文件只读／传输、六项下载器扩展、四项未来 Docker 部署、十五项原厂业务、三项缓存监控与一项项目资源读数。完整领域对照见[完成矩阵](mcp-completion-matrix.md)。有真实回调不表示当前 NAS 门槛已通过，更不表示替代全部 APP 功能。

所有新增工具只面向 policy v3、独立 Bearer Token、实际局域网来源，以及明确选择的角色、功能域、工具和资源。policy v1／v2 保留原十项，不自动升级。文件范围不会转换为 Docker 或下载器授权，管理员也不绕过项目范围。

在途调用绑定当前 Web 服务代际。服务关闭再重新启动，也不会恢复旧调用的发送权限或披露其结果；客户端须发起新的授权请求。

## 下载任务

| 工具 | 最低角色 | 行为 |
|---|---|---|
| `list_download_tasks` | 只读 | 显式分页读取固定 qB／aria2 的受限任务元数据 |
| `get_download_task` | 只读 | 读取精确任务、观察值和当前任务版本 |
| `pause_download_task` | 读写 | 暂停一个现有任务 |
| `resume_download_task` | 读写 | 按原下载器目标继续一个现有任务 |
| `remove_download_task` | 读写 | 移除任务，始终保留下载数据 |
| `set_download_task_limits` | 读写 | 以字节／秒设置单任务下载或上传限速，0 表示不限速 |
| `get_download_limits` | 只读 | 固定引擎全局下载／上传限速及独立配置版本 |
| `set_download_limits` | 管理员 | 确认、配置版本及幂等后设置全局限速，0 表示不限速，影响全部任务 |
| `recheck_download_task` | 读写 | 仅 qB 既有任务，重新校验数据，可能唤盘和改变状态 |
| `rename_download_task` | 读写 | 仅 qB 任务显示名，不调用文件／目录改名 |
| `set_download_task_force_start` | 读写 | 仅 qB 既有任务的强制运行开关，按原目标执行 |
| `set_download_task_queue_position` | 读写 | 仅 qB，调整单任务排队位置，要求引擎启用队列 |

使用 `downloads` 域，不改变原 `get_download_status` 的缓存语义。读取不包含绝对目录、Tracker、磁力链接、Cookie、凭据或原生 JSON。qB 的已配置限速由任务返回值投影；其明确的 -1 不限速标记统一为 0，未提供的值仍为 `null`。aria2 当前未额外查询配置限速，保持未知。

任务修改须提供 `engine`、`task_id`、`expected_version`、`confirmed:true` 和 `idempotency_key`。不支持 `all`、批量 ID、任意 RPC 或目录参数。任务版本绑定引擎 PID／启动时刻／监听 inode、任务身份与既有目标，变化时拒绝。管理员全局限速另使用 `get_download_limits` 返回的 `download_limits_*` 版本，不能拿任务版本替代；该授权影响整个引擎，不扩大为任意偏好设置。

继续任务会让原下载器在它原先配置的位置工作。此授权是对现有下载器任务的委托，**不能宣称将原生下载器的所有文件写入限制在 Toolbox 的 Users 文件范围内**。需要限制此类副作用时，不授予继续工具。新增链接／种子、设置保存目录、删除数据仍待文件后端权限闭合；不通过下载器接口提供任意文件访问。

qB 固定端口 8181，aria2 固定 6800；每次复核唯一服务监听和进程生命周期，IPv4／IPv6 不猜测。qB 4 使用 pause／resume，qB 5 使用 stop／start；不通过试发写请求探测版本。认证复用现有私有 SID，只允许固定版本查询遇到 401／403 后一次固定登录。连接建立后、发送前和响应后复核身份与授权；身份扫描后再次检查绝对期限，不因重试重置四秒／两秒窗口。与原监控共享 qB 认证锁和五分钟失败退避。

原生返回接受仅报告 `accepted:true, effect_confirmed:false`，不冒充任务已暂停、完成或限速已生效。超时或响应不明确时返回 `result_unconfirmed`，保留幂等结果；不会重新登录并重放写请求。幂等记录按本键、授权修订、凭据代际与请求摘要绑定，只保存在内存，容量满拒绝新写。服务重启后应先重新读取任务和版本，不盲目复用旧操作。

协议参考：[qBittorrent 官方 WebUI API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29)、[aria2 官方 RPC 文档](https://aria2.github.io/manual/en/html/aria2c.html#rpc-interface)。

## 原厂媒体

相册目录、共享相册目录、影视库目录、创建／重命名普通相册、重命名影视库和撤销分享共七项。它们是显式原厂请求，列表也可能初始化资源或修复内部状态，最低为读写，不标记为无副作用或幂等读取；只读键仍可读已有业务缓存。写入另需确认及本键幂等标识。

复用固定管理员已有会话，固定 `127.0.0.1:20443`、唯一 emhistor 身份及叶证书 pin，签名与原始响应不出宿主。相册创建／重命名核验响应对象；影视重命名和撤销分享只确认原厂接受，不把 HTTP 200 当业务完成。完整参数、返回投影和固件证据见[原厂媒体操作](media-business-operations.md)。

显式开发验证沿用 `POST /api/dev/media`，严格请求 `{"action":"probe_business","confirm":true}`，一次读取三类固定目录。可能更新原厂内部状态并唤盘，不加入被动采集。依旧要求非空实际来源白名单、有效开发窗口、同源 JSON／CSRF，结束时再核对授权。不能传路径、账号、令牌、相册 ID 或原厂动作。

## 原厂业务第二批

新增十四项：应用目录和安装进度；备份目录／自动导入设置、关闭导入、暂停／取消任务；最小用户目录、设备名称读取／修改、AI 开关读取／修改；影视库扫描状态；普通自建相册删除。精确工具、最低角色、固件地址和投影见[原厂能力说明](native-capability-completion.md)。全部原厂请求都可能通过共同认证初始化资源，不能标为只读或幂等查询；原五个缓存工具不改变语义。

安装进度本身会同步插件和写完成状态，需管理员、确认与幂等，不能后台自动轮询换键。备份只收紧现有导入／任务，不创建、恢复或接受目标路径；相册删除只接受当前所有者普通自建类型，不等于读取／删除照片内容。七个写入口的 ACK 仅表示接受，未知结果保留并禁止重放。

显式 `POST /api/dev/native` 仅接受 `{"action":"probe","confirm":true}` 或 `{"action":"packages","confirm":true}`。前者固定查询六类目录／设置，后者只采应用包字段存在性、类型与长度；不返回包 URL／签名原文，不安装或执行 setter，不把有摘要／签名字段当作可信包证明。均需真实来源开发白名单和绝对窗口，不加入被动诊断。

## Docker 与文件

固定项目有 `inspect/control/update_image` 授权；启停重启和镜像更新需管理员与精确工具。更新限定单服务和同仓库固定 digest，见[固定项目操作](docker-project-operations.md)。另有四项未来部署工具，使用独立 `deployment_*` 和 `docker_deployments` 的 `inspect/deploy/control/remove`；所有者网页先登记已有卷／网络、固定仓库／端口／非 root 用户和配额，再规划／执行 `deploy/update/start/stop/restart/remove`。不支持任意 Compose／命令／bind，删除保留卷，失败保留实际现场；见[受控部署](docker-deployments.md)。

文件边界保持 `/drives/*/Users/admin/`。五项文件只读／下载执行器已接入精确固件、固定原厂所有者和 admin 正子树；完整 ACL、同挂载和最终句柄均须实时通过，不以 root 权限或根规划代替。二十一项文件／Docker 合同已有适配，文件写入仍须独立通过恢复区、当前对象和索引等实际门槛。此前 `d5075ed` 实机七根未通过挂载门，未有样本读取证明；新增 `file_protocol` 一次返回阶段 errno、mount／device 和 namespace，需新镜像显式取证。见[真实只读后端](native-file-readonly.md)与[上下文](file-backend-context.md)。

应用安装／卸载／更新仍缺可信包、来源、版本、签名、依赖及恢复合同。影视来源／成员、照片内容、分享创建、备份新建／恢复、账户权限与凭据、网络／存储变更仍待接入；不能将部分设置／停用工具、原厂动作表或管理员角色宣传为全功能完成。

## 验证范围

自动化使用本地 HTTP／Unix socket 服务、真实授权存储和 Linux 文件句柄；验证角色、精确工具、任务版本、撤权、跨引擎并发、幂等、错误投影及不泄密。fixture 成功不是 NAS 验收。实机先显式读取目录和项目状态；写入需指定专用任务、相册或 Docker 项目，上传／下载需样本和内容校验。当前没有对 NAS 执行本批业务写入。
