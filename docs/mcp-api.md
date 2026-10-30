# 局域网 AI 与 API 管理

Toolbox 在常驻 Web 的 `18732` 端口提供 MCP 与固定 REST 接口，供局域网 AI／自动化客户端读取 NAS 状态和执行明确授权的操作。小屏可独立关闭，MCP 默认关闭；开启后仍需独立 Token。配置与 Token 摘要随原 `r1-toolbox-data` 数据卷保存，重建服务后继续有效。

当前八十一工具包括缓存监控、文件读取／上传／变更、下载器、受控 Docker 和固定原厂业务能力。目录、列表与执行共用精确工具及资源授权；原厂目录与包字段不授安装资格。文件变更及 owner 恢复区见[文件变更与恢复](file-mutations.md)。

## 网页配置

进入网页“设置 → API 管理”，卡片直接开关 MCP／REST，无需打开另一个窗口或再次确认；保存失败会回读真实状态，不重放写入。“添加 Token”填写新授权，“Token 管理”编辑、暂停、启用、轮换或撤销，“Token 操作”查看能力、接入地址和调用记录；三个页面可直接切换。具体交互见[管理页面](mcp-management-ui.md)。创建命名 Token 时选择角色、功能域、具体工具、文件范围、已登记 Docker 项目、来源 IP／CIDR 和有效期。默认只读，仅勾选状态读取，有效期九十天；可选择长期。创建或轮换成功时只显示一次完整 Token 与可复制的 MCP 配置，关闭密钥窗口即清空，后续列表不再提供明文。

列表支持编辑、停用／启用、轮换和撤销。停用保留 Token 与配置；轮换原子替换摘要，旧 Token 立即失效，新 Token 只显示一次，权限、停用状态和到期时间不变。撤销不可恢复；到期需明确修改有效期，不能靠启用或轮换绕过。编辑默认保持原权限与到期时间，旧密钥不会自动升级成管理员。

关闭 MCP 服务立即阻止所有机器访问，重新开启不会重新生成或删除密钥。管理页面默认无口令可使用，设置口令后需要网页登录；显式 `pair` 模式保留旧配对流程。同源、JSON、CSRF 始终校验；API Token 不能作为网页管理凭据，开发者白名单也不能代管密钥。

API 管理只在主动打开和操作后读取列表，不参加十秒监控轮询。最近调用记录只保留时间、密钥 ID／名称、操作、来源、结果和耗时，最多一百条，不记录请求参数、工具输出、请求头或凭据。

## MCP 接入

地址为 `http://NAS_IP:18732/mcp`，传输方式为 Streamable HTTP。在支持自定义请求头的 MCP 客户端中填入此地址，并设置 `Authorization: Bearer <网页创建的访问密钥>`。网页生成的参考配置如下，不同客户端的配置入口可能不同：

```json
{
  "mcpServers": {
    "toolbox": {
      "url": "http://NAS_IP:18732/mcp",
      "headers": {"Authorization": "Bearer <访问密钥>"}
    }
  }
}
```

实现兼容协议 `2026-07-28`、`2025-11-25`、`2025-06-18` 和 `2025-03-26`。新版使用逐请求 `_meta`、`server/discover` 和 `Mcp-Method`／`Mcp-Name` 镜像，旧版使用 `initialize`／`notifications/initialized`。返回普通 JSON，不建立协议会话，也不提供 SSE 长连接；GET／DELETE 返回 405。请求必须声明接受 `application/json` 和 `text/event-stream`，质量值须大于零。未实现 OAuth 自动登录，仅支持部署者明确配置的访问密钥。

协议依据：[当前 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[当前服务发现](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)、[2025 传输与兼容](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。

## 权限与工具

新策略同时要求角色、功能域、精确工具及资源范围。只读 `readonly` 可查询；读写 `readwrite` 增加已接入的普通修改；管理员 `admin` 增加已接入的管理操作。管理员仍受具体工具、文件范围、项目和未来部署资源限制。切换角色不会自动勾选其他工具；未接入的文件变更和任意 Compose 代理不会因角色提高而可用。

工具列表和实际调用使用同一个权限判断；缺少域、具体工具或所需资源时拒绝，角色等级不足也不能调用。HTTP 200 或工具出现在客户端缓存中均不代表操作已获授权。policy v1／v2 固定保留原十工具，再受其原角色与域限制；以前勾选过 `files/docker` 的预授权也不会自动激活新能力。所有者须明确选择升级并勾选具体工具，提交 `tool_grants` 后才进入 policy v3。

当前共八十一个真实 MCP／REST 工具，上一批新增独立覆盖与五项文件变更；目录按角色、功能、精确工具与资源过滤。工具有回调仍须通过实际生产门槛，不等于实机验收。

| 权限 | 新密钥最低角色 | 工具 | 行为 |
|---|---|---|---|
| `read` | 只读 | `get_nas_status` | 读取 `overview/system/storage/temperatures/network/containers/apps/fans/lights/screen`；默认概览 |
| `fan` | 读写 | `set_fan_policy`、`release_fan` | 保存／应用完整风扇策略或恢复原厂管理；必须明确传入 `confirmed:true` |
| `light` | 读写 | `set_light_policy`、`set_rgb` | 修改灯光策略或四种原生灯效 |
| `screen` | 读写 | `set_screen`、`update_screen_settings` | 亮度、息屏、唤醒及既有屏幕／屏保设置 |
| `power` | 管理员 | `request_power`、`cancel_power` | 重启／关机要求 `confirmed:true`，保留五秒可取消窗口 |
| `media` | 读写 | `get_media_status` | `face` 查询相册识别；`all` 批量验证七类状态；原厂可能写完成标记，明确按需调用 |
| `files` | 只读 | `list_file_roots` | 仅投影本键已授权根和子目录的绑定元数据，不列盘内文件或传输内容 |
| `files` | 只读 | `list_directory`、`get_file_info`、`prepare_file_download`、`get_file_transfer`、`cancel_file_transfer` | 精确固件下固定原厂所有者的 `admin/public` 只读子集；每次原厂 ACL、同挂载／同句柄核验，实机门槛未通过时拒绝 |
| `files` | 读写 | `prepare_file_upload`、`commit_file_upload`、`get_file_operation` | 仅已核验内置 SATA 的 admin 后代新文件；准备／固定 PUT／显式提交分开，无覆盖发布，独立原厂索引确认后才完成，目标及父目录均须读写授权 |
| `files` | 读写 | `prepare_file_overwrite` | 独立覆盖准备，必须目标版本和确认；commit 使用可信记录恢复精确准备权限，旧对象保留 |
| `files` | 读写 | `create_directory`、`copy_file`、`rename_file`、`move_file`、`trash_file` | 单层目录、普通文件复制／可恢复改名／同盘移动／原厂回收；源目标独立授权，恢复区和原厂记录另行核验，缺证据拒绝 |
| `docker` | 只读 | `list_docker_projects`、`get_docker_project_status`、`get_docker_project_summary` | 仅读取本键获授且已登记项目的状态及脱敏摘要，要求项目 `inspect` |
| `apps` | 只读 | `get_app_status` | 已有原厂应用脱敏列表缓存及最近失败，不发起安装或原厂刷新 |
| `downloads` | 只读 | `get_download_status` | 既有下载业务快照，不为 MCP 查询或登录下载器 |
| `movie` | 只读 | `get_movie_status` | 已有刮削状态缓存 |
| `albums` | 只读 | `get_album_status` | 已有人脸、OCR、缩略图、相似图片及 AI 状态缓存 |
| `sharing` | 只读 | `get_share_status` | 已有共享相册状态缓存，不创建或更改分享 |
| `downloads` | 只读 | `list_download_tasks`、`get_download_task` | 显式读取固定下载器的任务元数据及版本，不返回文件路径或内容 |
| `downloads` | 读写 | `pause_download_task`、`resume_download_task`、`remove_download_task`、`set_download_task_limits` | 逐任务确认、版本及幂等检查；移除始终保留数据 |
| `downloads` | 只读／管理员 | `get_download_limits`、`set_download_limits` | 全局限速读取与管理员修改；修改需独立配置版本和确认，影响该引擎全部任务 |
| `downloads` | 读写 | `recheck_download_task`、`rename_download_task`、`set_download_task_force_start`、`set_download_task_queue_position` | 仅 qB 既有单任务：校验、显示名、强制运行和排队优先级，不接收新内容或保存路径 |
| `albums` | 读写 | `list_albums`、`list_shared_albums`、`create_album`、`rename_album` | 原厂目录、普通相册创建与改名；目录查询也可能更新内部状态 |
| `movie` | 读写 | `list_movie_libraries`、`rename_movie_library` | 原厂影视库目录及仅名称修改，不修改来源或成员 |
| `sharing` | 读写 | `revoke_share` | 撤销当前原厂管理员的一个既有分享，不创建外链 |
| `docker` | 管理员 | `control_docker_project` | 仅控制已登记且明确授予 `control` 的项目启停／重启 |
| `docker` | 管理员 | `plan_docker_project_update`、`execute_docker_project_update` | 需项目 `update_image`；受控单服务、同仓库固定 digest 更新，执行返回真实任务 |
| `docker` | 只读 | `get_docker_project_task` | 需项目 `inspect`；仅查询同键、同授权修订和同凭据代际提交的任务 |
| `docker` | 只读 | `list_docker_deployments`、`get_docker_deployment_task` | 只读本键获授的未来资源登记与本键任务，须独立资源 `inspect` |
| `docker` | 管理员 | `plan_docker_deployment`、`execute_docker_deployment` | 独立 `deployment_*` 固定资源的部署、同仓库 digest 更新、启停重启和停止后删除；需对应资源操作和有效确认计划 |
| `apps` | 读写／管理员 | `list_native_applications`、`get_application_install_progress` | 目录需读写；安装进度会同步插件并写完成状态，需管理员、确认与幂等，不等于允许安装 |
| `apps` | 读写 | `list_native_modules` | 显式读取原厂可用／已安装模块两表，可能更新支持状态并联系厂商；不推断运行状态、不授安装和卸载权 |
| `apps` | 管理员 | `plan_native_application_install`、`install_native_application` | 只新装明确 application_ids 授权的 ID7／8／34／35；实时双目录、当前计划、确认与幂等分别核验，不更新或卸载，不以受理冒充完成 |
| `backup` | 读写 | `list_backup_tasks`、`get_auto_backup_settings` | 当前管理员备份目录与原始自动导入设置，未知枚举保持未知 |
| `backup` | 管理员 | `disable_auto_backup`、`pause_backup_task`、`cancel_backup_task` | 只关闭自动导入、暂停／取消既有任务；不创建或恢复备份，不接受新目的地 |
| `system` | 管理员 | `list_nas_users`、`get_nas_name`、`set_nas_name`、`get_nas_ai_settings`、`set_nas_ai_enabled` | 最小用户目录、设备名和原厂 AI 开关；不管理账号凭据或读取照片 |
| `movie` | 读写 | `get_movie_library_scan_status` | 指定影视库的真实扫描状态／计数，不发起扫描、不伪造百分比 |
| `albums` | 读写 | `delete_album` | 只删除当前所有者普通自建相册及索引关系，拒绝共享／保留相册，ACK 不表示异步完成 |

风扇／灯光的完整配置先通过状态读取取得，再修改所需字段；设备控制器继续校验内部结构及硬件能力。写入与网页使用同一操作锁，媒体与开发调试使用同一媒体锁；忙碌时返回明确失败，客户端应重新读取后判断，不把未确认结果当作已生效。

原有 `get_nas_status` 使用已有监控、SMART 后缓存和 Toolbox／原厂应用缓存，不临时启动 SMART 自检或唤醒休眠盘，不猜测未知读数、RPM 或整机功耗。磁盘温度保留自定义名称。应用缓存不可用时明确报告来源状态，不用空列表表示“没有应用”。显式 `get_media_status` 查询结果同时写入 Toolbox 媒体快照，供网页被动读取逐项时间和结果；缓存写失败单独报告，不重复原厂查询。见[业务来源](business-diagnostics.md)。

媒体权限独立于普通读取：原厂状态查询可能写完成标记、发布事件或进入厂商内部资源修复分支，不能当作无副作用轮询。七项实际请求和零计数限制见[媒体验证](media-probe.md)。

五项业务缓存工具均为空参数只读工具，返回真实观察时间、缓存年龄、来源及最近失败，未知值保持 `null`。它们按独立功能域和精确工具授权，不沿用 `media` 的主动查询权限；缺少缓存时不会自动采集。下载缓存在首次启动或业务快照尚未发布时报告未采集，不为填数据联网。细节见[业务只读工具](mcp-business-tools.md)。

原厂业务查询可能初始化或更新资源，不用于被动轮询。应用新装仅审计 ID7／8／34／35，需具体安装资源；可能联网、写原厂应用数据、加载既有任务、监督后台进程，部分原生恢复分支还可能调整宿主页缓存。安装预览明确具体影响，不宣称原生进程受文件子目录沙箱限制。本批未实机安装，见[原厂能力](native-capability-completion.md)。

### 文件根元数据、受限读取与新文件上传

文件与固定项目合同见[接口方案](file-docker-api-plan.md)。读取支持固定原厂管理员的 admin/public 正子集，写入仅已核验 admin 后代，上传、独立覆盖、目录、复制、可恢复改名／移动及原厂回收均有适配，真实调用仍须逐项生产门槛。

二十一项接口的[机器定义](api/file-docker-contracts.json)均有实际适配；定义入口继续为所有者查阅，不授执行权、不迁移旧键，也不替代 NAS 验收。

`files/docker/apps/downloads/movie/albums/sharing/backup/system` 均部分接入，新增工具与资源默认不选。任意应用安装、更新／卸载、分享创建、完整媒体／备份管理和任意 Compose 仍未完成，八十一工具不是 APP 覆盖率。见[完成矩阵](mcp-completion-matrix.md)。

勾选文件管理时，必须选择当前可信的 `/drives/<disk>/Users` 根，可填写其内相对 POSIX 子目录，并为每个范围选择只读或读写。文件总边界固定为 `/drives/*/Users/`，不授予整个数据盘、兄弟目录或系统目录。只读角色不能授予读写范围；只读挂载不能授予写入。规划来自已有物理盘身份缓存、sysfs／udev 生命周期核验与 `/proc/self/mountinfo`，不检查 Users 是否存在，也不访问用户文件。缺少身份、歧义挂载、Users 内额外挂载、无法核对的 RAID／虚拟源等不会生成可选目录。详见[Users 文件边界](file-users-boundary.md)。

根 ID 绑定物理身份、分区源、文件系统与挂载路径，修改显示名称不改变 ID；换盘或路径变化后旧绑定不匹配。子目录不接受绝对路径、`.`／`..`、反斜杠、重复斜杠或控制字符。网页支持同根多条子目录的无损编辑，精确重复拒绝；授权按允许范围并集计算，子目录只读授权不会收窄上级已授读写范围。

规范 /drives 之外的完整文件系统别名不再误排唯一规范根，别名自身也不会成为第二授权根；多个规范 /drives 根同源、同设备、堆叠或 Users 内额外挂载继续拒绝。此修复未代替 SSD 实机安全打开或下载验收。

Users 边界使用独立 v2 摘要。旧整盘根不会自动改为 Users 根；原 Token 的名称／IP 等仍可编辑，但旧根显示不可用，需所有者显式重新选择。更换根也不自动注册文件内容工具。

`list_file_roots` 继续只返回 `verification:metadata_only`，不暴露宿主挂载路径或授予内容权限。内容读取由精确固件、固定逻辑主体、完整原厂 ACL 正分支、同挂载无链接拓扑和最终文件句柄共同核验；不把 NAS 逻辑 ID 当 Unix UID，不回退宿主 root 读取。每个下载块前后续验授权与对象。内容只使用独立 Bearer 的 `GET|HEAD /api/integration/files/transfers/<transfer_id>/content`，单 Range 有界，不接受 Cookie 或开发白名单代替授权。详见[真实只读后端](native-file-readonly.md)。

`364b858` 的 NAS 验收已确认视频资源、备份磁盘和主磁盘的三个实际 `Users` 根均通过挂载校验，并用当前来源限定的临时只读 Token 成功浏览各自的 `admin` 目录。视频资源和主磁盘的目录属性读取也成功；备份磁盘的一次属性读取返回 `acl_unverified`，随后浏览成功，不能据此宣称已经解释该次拒绝或保证所有操作稳定通过。四个不存在 `Users` 的分区候选继续拒绝。尚未取得用户指定小文件的下载、HEAD 和 Range 比对证据；`GET /api/access/file-readiness` 和开发诊断自身均不发执行授权。

上传仍最低读写，只新建文件的旧工具保持原语义；覆盖新增 prepare_file_overwrite 精确授权、目标版本与确认。固定 Bearer PUT 仅暂存，显式 commit 后独立索引确认；其他文件变更、冲突恢复和 owner 固定入口见[文件变更与恢复](file-mutations.md)。GET／网页监控不打开恢复区，不自动初始化或重放未确认写入。

### 固定 Docker 项目读取

所有者先从项目候选中登记，再将项目 ID 的 `inspect` 授予具体密钥。网页提供 `GET /api/docker/projects/catalog`、`GET /api/docker/projects`，以及 `POST /api/docker/projects/register`、`/update`、`/revoke`。登记、改标签和撤销仅修改 Toolbox 私有登记，不创建、部署、启停或删除原项目。API 管理自己的登记目录只读本地元数据；主动读取上述项目接口或调用项目工具时才核验引擎来源。

读取器固定使用本机 Docker Unix socket 和允许的 GET，核验 socket 与 root `dockerd` peer 身份；一轮查询共享八秒绝对网络窗口，并有单体、累计字节和并发上限。以引擎、项目、容器服务及配置文件元数据绑定资源，重建或配置身份变化后不按同名换绑。仅取得配置文件身份，不读取 Compose 原文，也不返回环境变量、命令、日志或绝对宿主路径。

当前状态和摘要均使用引擎证据；项目状态返回 `source:engine`，`native_status/pop_status` 保持 `null`，不把引擎响应伪装成原厂 REST 状态。Docker 三项工具及文件根元数据工具均在短读前后执行 `is_current()`，重新核验有效存储、服务开关、键状态、自然到期、授权修订和 Token 代际，失效则丢弃未发送结果。详见[固定项目读取](docker-projects.md)。

### 独立未来 Docker 部署

网页「Token 操作 → 部署资源管理」先登记未来固定资源，Token 再选择独立 `docker_deployments`：`{"id":"deployment_<24hex>","operations":["inspect","deploy","control","remove"]}`。新建和同仓库 digest 更新需 `deploy`，启停重启需 `control`，删除需 `remove`，读取登记和任务需 `inspect`；写操作仅管理员且须对应工具。Docker 域可只选现有项目或未来资源，不强制同时选择。

所有者固定路由为 `GET /api/docker/deployments`、`GET /api/docker/deployments/catalog` 和 `POST /api/docker/deployments/register`、`/revoke`、`/refresh`。登记具体仓库、已有 local 具名卷／bridge 或 none 网络、固定局域网 IP／端口、非 root 用户与配额，不运行部署；重核只更新同资源与既有容器的生命周期，旧计划失效。机器或开发白名单均不能管理这些授权。

规划支持 `deploy/update/start/stop/restart/remove`，执行须本键有效计划、确认和幂等标识，返回真实任务。只接受同登记仓库的 sha256 digest，不接收任意 Compose、环境、命令或宿主路径。删除须已停止，固定 `v=0&force=0` 保留卷；更新旧服务先保留备份，新服务真实运行并健康确认后才删除旧容器。故障或撤权保留未知终态，不自动回滚／重放。完整合同见[受控部署](docker-deployments.md)。

## REST 接入

与 MCP 使用相同 Bearer 密钥、IP 限制、权限及调用记录，不接受 Cookie 或开发者白名单代替密钥。

- `GET /api/integration/status?section=overview`：返回 `ok/section/data`；状态类别与 `get_nas_status` 相同。
- `GET /api/integration/capabilities`：任意有效密钥可查询自身 `role/policy_version/scopes/tool_grants/file_roots/docker_projects/docker_deployments/tools/domains`，无需 `read` 域。工具列表与 MCP 一致，域只返回已选择的项；文件范围仅投影当前密钥所选根，失效或变为只读的绑定标记不可用，不输出绝对 `mount`。此接口不读取业务文件、不探测 Docker 引擎、不调用媒体或安装 getter，不接受 URL 参数。
- `POST /api/integration/action`：JSON 为 `{"tool":"set_screen","arguments":{"action":"wake"}}`，返回标准工具结果 `content/structuredContent/isError`。HTTP 200 不替代 `isError` 或业务 `ok`。

网页所有者管理使用以下固定接口，全部拒绝 Bearer Token 代替网页授权：

| 接口 | 请求与含义 |
|---|---|
| `GET /api/access` | 安全元数据、角色／功能／文件根目录与有界审计，不含摘要或 Token |
| `GET /api/access/contracts` | 二十一项定义与实际适配标记；不以查定义授执行权 |
| `GET /api/access/file-readiness` | 文件访问就绪证据与未通过门槛；不打开用户文件 |
| `POST /api/access/settings` | `{"enabled":true}`；与“服务与开发”的 MCP 总开关共用状态 |
| `POST /api/access/keys` | 创建具名 Token；成功响应只在本次含明文 |
| `POST /api/access/keys/update` | `id` 加要修改的 `name/role/scopes/tool_grants/file_roots/docker_projects/docker_deployments/allowed_ips/expires_days`，至少一项修改 |
| `POST /api/access/keys/enable` | `{"id":"...","enabled":false}` 暂停；`true` 恢复，不能恢复已撤销 Token |
| `POST /api/access/keys/rotate` | `{"id":"..."}`；新 Token 只在本次响应显示，旧摘要立即移除 |
| `POST /api/access/keys/revoke` | `{"id":"..."}`；永久撤销 |

创建 policy v3 显式发送 `role`、`scopes`、`tool_grants` 及所需资源。文件范围项为 `{"id":"root_<24 位十六进制>","path":"相对/子目录","access":"readonly"}`，空 `path` 表示整个已选根，`access` 也可为 `readwrite`；勾选 `files` 须至少一项，未选则必须为空。已有项目使用 `docker_projects` 的 `project_*` 和 `inspect/control/update_image`；未来资源使用独立 `docker_deployments` 的 `deployment_*` 和 `inspect/deploy/control/remove`。选 `docker` 至少获授其中一类资源，须对应未撤销登记。Docker 写操作须管理员及精确工具，单独提高角色不勾选写权限；文件新上传最低读写，仍须具体工具、读写范围和真实后端证明。旧读写策略中的不可执行更新预授权保留存储兼容，仍不能调用管理员工具。

没有 `role` 的旧创建请求仍为 policy v1，仅允许旧六 scope；有角色但没有 `tool_grants` 的旧请求保持 policy v2，两者均只能使用原十工具。policy v3 中即使保留某个域，未选具体工具也不获授权。

更新只合并显式字段：省略 `role/scopes/tool_grants/file_roots/docker_projects/docker_deployments` 保留原权限，省略 `expires_days` 保留原到期时间。明确提供有效期时从本次保存重新计算，`0` 为长期，`1～365` 为天数。旧键单独选择角色最多按兼容行为转 policy v2，只有明确提交工具集才转 policy v3；已有 v3 改角色不会降回 v2。降低角色或移除域时须同步收紧不兼容的工具和资源，否则整项拒绝。尚未挂载的旧根不阻止只改名称／IP 等元数据，实际资源调用仍重新核验身份。

服务所有者另有 `GET /api/services` 和固定 `POST /api/services/screen`、`/api/services/mcp`、`/api/services/developer`，管理可选小屏、MCP 开关与有时限的开发白名单。它们不属于机器工具，拒绝 API Token、URL 查询参数和片段，写入保留网页授权、同源、JSON、CSRF 和操作锁。小屏保存意图与实际接管分开；开发窗口有效不代表任意来源获权，见[开发诊断](dev-diagnostics.md)。

三类入口不能混用：外部 AI 的 MCP／REST 始终使用独立 Bearer；Toolbox 内部 Python 模块直接调用已限定的函数，不需要 Toolbox Token，也不能据此开放 HTTP 免授权操作；开发者固定诊断不需要 Token，但必须处于开发模式、实际 IP／CIDR 非空白名单及绝对有效窗口内，并保留同源／JSON／CSRF。开发白名单不能管理自己、机器密钥或项目登记。

开发诊断目录有十项，可显式请求 `file_access_readiness`、`file_backend_context`、`file_protocol`、`docker_project_readiness` 等；默认快照仍只有原 `device/storage/business/fan` 四项。`file_protocol` 在精确固件与真实句柄门后固定核对根／admin／public 的原厂目录协议，不返回目录名称或内容。独立 `POST /api/dev/native` 严格接受 `{"action":"probe","confirm":true}` 或 `{"action":"packages","confirm":true}`，分别采集六项原厂业务或应用包字段形状；可能初始化资源或唤盘，不安装软件、不运行 setter、不加入默认轮询。采集后授权失效即丢弃结果。

公网来源、伪造转发头、任意原厂地址／SQL／命令代理和 URL 中传入密钥均不受支持。文件工具将使用明确的根范围和结构化参数，不能由当前预授权推出任意系统文件访问。

## 存储与运行边界

每个密钥包含三十二字节随机熵，持久化文件 `web/api-access.json` 只保存 SHA-256 摘要和元数据，以原子替换和 `0600` 权限写入。最多三十二个有效密钥、六十四条历史记录。文件损坏或权限异常时拒绝授权；持久写入失败会锁定当前服务实例的访问能力，修复存储并重启后恢复，避免继续使用没有成功保存的授权状态。

状态文件 v6 独立保存 application_ids，兼容读取 v1～v5；读取和校验旧键不改字节，真实配置或审计写才原子迁移。新键默认空，旧安装键缺省仅保留原 ID34；省略编辑字段保持原值，资源修改推进 grant_revision，撤权／轮换后丢弃未发送结果。policy v1／v2 仍冻结原十工具。

键保存 `grant_revision` 和 `credential_generation`。权限、来源、到期、启停及首次撤销的实际变化推进授权修订，轮换推进凭据代际；纯改名、审计、最近使用时间或没有实际变化的保存不推进。`is_current()` 只续验已经由 `authorize()` 认证的内部 principal，不是初始认证入口，不接受 HTTP 提交 principal，也不返回摘要。自然到期独立比较当前时间，不能因修订号未变而继续放行。

只允许真实连接来源为 RFC1918 IPv4、IPv6 ULA 或 loopback。来源规则为空时允许持 Token 的上述客户端；填写后必须命中精确 IP 或局域网网段。支持 IPv4／IPv6、CIDR 和 IPv4 点分掩码，例如 `192.168.31.0/24`、`fd00::/64`、`192.168.31.0/255.255.255.0`；服务端规范化网络地址，单主机 `/32`／`/128` 归并为精确 IP，拒绝等价重复规则。网段必须完整位于允许的私网或回环范围，拒绝域名、通配符、`0.0.0.0/0` 和部分覆盖公网的网络。`Forwarded`／`X-Forwarded-For` 不替代真实 peer。HTTP `Host` 必须是实际本机地址；提供 `Origin` 时必须同源，非浏览器客户端可省略 `Origin`，无 CORS 放行。

当前复用网页的 HTTP 监听，密钥在局域网链路中不受 TLS 加密保护；需要跨不可信网络连接时应在部署层提供 HTTPS。此版本没有开放公网接入或任意主机转发能力。

## 验证

自动化包括临时密钥库、真实本机 HTTP、Mock 硬件和网页管理测试：

```sh
python -m unittest discover -s tests -p 'test_api_access.py' -v
python -m unittest discover -s tests -p 'test_api_permissions.py' -v
python -m unittest discover -s tests -p 'test_api_file_roots.py' -v
python -m unittest discover -s tests -p 'test_api_file_readiness.py' -v
python -m unittest discover -s tests -p 'test_api_docker_projects.py' -v
python -m unittest discover -s tests -p 'test_mcp_service.py' -v
python -m unittest discover -s tests -p 'test_integration_http.py' -v
python -m unittest discover -s tests -p 'test_integration_roles_http.py' -v
python -m unittest discover -s tests -p 'test_integration_resources_http.py' -v
node tests/check_api_management.cjs
node tests/check_service_settings.cjs
```

2026-10-29，使用官方 `@modelcontextprotocol/sdk@1.32.2` 对本机隔离 WebConfig 服务完成初始化、权限过滤、状态读取、屏幕 Mock 调用与关闭连接；越权电源调用未触发控制器。新版协议另有原始 HTTP discovery／tools-list 验证。

2026-10-30，在 NAS 的 `364b858` 镜像上，用独立临时只读 Token 验证三项缓存监控工具和上述三个 `admin` 目录浏览。Token 只选五个具体工具、三个目录范围和当前真实来源 IP，MCP 列表精确返回五项；结束后立即撤销，原 Token 再访问返回 HTTP 401。健康缓存缺测时返回 `assessment:unknown`，未把有新鲜数据等同于完整健康。固定 Docker 项目尚未登记，项目资源读数仍只有本地／Linux 自动测试证据；文件下载及全部写操作仍须各自验收。
