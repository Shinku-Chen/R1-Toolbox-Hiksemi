# 文件变更与恢复

更新于 2026-10-30。文件执行器已提供新文件上传、已有文件覆盖、单目录创建、普通文件复制、改名、同盘移动和移入原厂回收站。代码、隔离测试和 NAS 实机验收分别记录；注册工具不代表当前磁盘已通过门槛。原 v2 SSD 根曾受 Users 下十四个 overlay 子挂载阻断；7fcd12b 实机已确认 v3 admin 根保留这十四个子挂载并通过真实句柄检查，原测试文件的信息、HEAD 与 1 KiB Range 读取通过。此前版本的恢复区预检因可写祖先及 Users 权限拒绝；c2acce7 已确认新位置的 admin 本身合格。本次新策略改为 admin 信任锚，外层权限不再否决；尚未将新策略作为 NAS 写入验收通过，也未执行 NAS 文件变更或应用新装。读取中的原厂服务错误及有限复核范围见[实机记录](file-users-boundary.md#2026-10-307fcd12b-实机只读核对)。

## 授权与范围

所有文件内容仍限定在 `/drives/*/Users/admin/`，以可信 `root_id` 和相对路径调用。读取仅支持经原厂固定管理员 ACL 核验的 admin 子树；写入仅支持已核验内置 SATA 的 `admin` 后代，不修改其他用户、`admin` 根、内部保留路径或授权外的父目录。相对 admin 根的固定原厂内部树 `.DATA_CACHE` 及后代不作为普通写入或复制源开放；真实回收仅由内部固定协议访问。

机器调用同时要求当前 Token、真实来源、读写或管理员角色、`files` 域、精确工具和对应子目录。管理员不能绕过范围。复制来源可只读，目标与目标父目录须读写；改名、移动、覆盖和回收需要原对象及其父目录的读写授权。源版本从 `get_file_info.entry.object_version` 取得；它绑定 inode 和 stat 状态，不宣称内容强 ETag。

| 工具 | 输入要点 | 实际行为 |
| --- | --- | --- |
| `prepare_file_upload` | `root_id/path/size_bytes/sha256/idempotency_key` | 只准备新文件；`overwrite` 只能为 `false` |
| `prepare_file_overwrite` | 同上，另加 `expected_target_version/confirmed:true` | 独立授权，不扩大旧上传键；旧目标会保留为恢复对象 |
| `commit_file_upload` | `transfer_id/idempotency_key` | 使用传输记录绑定的原始准备工具重新验权并显式发布 |
| `create_directory` | `root_id/path/idempotency_key` | 只创建一层，不递归补父目录，不自动改名或合并 |
| `copy_file` | `source_root_id/source_path/target_root_id/target_path/expected_source_version/idempotency_key` | 只复制普通文件；源读取与目标写入分别核验 |
| `rename_file` | `root_id/path/new_name/expected_source_version/idempotency_key` | 同父目录改名，目标已存在则保留双方 |
| `move_file` | 同复制输入 | 同一文件系统内可恢复移动；不做复制后删除回退 |
| `trash_file` | `root_id/path/expected_source_version/confirmed:true/idempotency_key` | 仅真实原厂回收站；不永久删除、不以 Toolbox 暂存区冒充回收站 |
| `get_file_operation` | `operation_id` | 重新核验原工具与源／目标范围，只查询本键／代际绑定任务 |

上传字节继续使用独立 Bearer 的固定 `PUT /api/integration/files/transfers/<id>/content`。准备、接收、校验和显式提交分开，最多 8 GiB，大小与 SHA-256 均须吻合。详情见[上传通道](file-upload-transport.md)。

## 所有者恢复区

Linux 不提供按 inode 比较后原子改名的系统调用；`stat` 后直接按路径 `rename` 不能保证改的是同一对象。Toolbox 先将源捕获到受信恢复区，再用已持有 FD、实际 inode、元数据和完整内容哈希核对。捕获到不同对象时保留现场，停止发布和原厂索引通知，返回结果未确认。此过程可能暂时移走原名，不能描述成单步源 CAS。

恢复区固定为所选 `Users/admin/.r1-toolbox-ops`，所有用户文件与保管对象均在 admin 内。只能由网页登录所有者显式初始化；机器 Token 和开发白名单不能自登记。该精确私有子树从普通文件列表隐藏，不能作为文件读取、下载、复制来源、写目标或新授权范围；整根 Token 也不能访问。文件信任锚为实际 admin 目录 FD，要求 root 属主、无组／其他用户写权限、无扩展 ACL；私有恢复区另要求 `0700`。namespace root、/drives、物理分区和 Users 的属主、可写位与 ACL 不作为新信任模型的权限门槛；仍保留固定路径、逐级 no-follow、真实句柄、物理盘身份和挂载核验，不提供这些目录的文件访问。不接管未知目录，不修改原厂目录权限。

新恢复登记写 `file-mutation-admin-authorities.json`，版本为 3。实际位置仍为 `layout:admin_custody_v1`，新项另存 `trust_model:admin_anchor_v1` 与 `admin_identity:[dev,ino,S_IFDIR]`。旧版此文件的版本 2 只读加载后仍按原完整祖先策略执行，不因升级程序而自动放宽；只有所有者显式确认初始化／核验升级、精确核对原恢复区 inode 后才绑定当前 admin，并原子保存新策略。版本 3 允许保留未升级的旧项，其旧策略不变。原 `file-mutation-authorities.json` 版本 1 保持只读与原字节：Users v2 根为 `legacy_boundary`／`legacy_users_boundary_v2`；admin v3 根但实际保管于 Users 的项为 `legacy_layout`／`legacy_recovery_layout`。两类旧位置均不可借本次核验升级搬动、接管或重新解释，须先用原镜像处理旧保管对象。本次 NAS 尚无恢复登记或保管对象。

预检显式返回 `trust_boundary:admin` 及 `recovery_location`，后者包含 `boundary:admin`、`relative_path:.r1-toolbox-ops`、`layout:admin_custody_v1`、`anchor_component:admin_root`。`recovery_anchor` 是已观察到的 admin 检查记录，未取得证据时保持 `null`。`checks` 保留逐级定位证据；admin 项为 `required_for_trust:true`／`gate:trust_anchor`，其余为 `required_for_trust:false`／`gate:location_only`／`trusted:null`，不能将父目录 UID 或可写位显示成恢复区资格失败。前两级 `same_mount:null` 不以数据盘 mount ID 判断；物理分区及其后代仍须实际匹配。`registration_trust_boundary` 和 `owner_revalidation_required` 区分已有登记与本次所有者核验目标，预检本身不保存或授予机器新策略。

初始化前后以及每次实际消费、检查和恢复时，重新从固定规范路径打开 admin，核对持久 admin inode、设备及本次实际挂载，再核对私有恢复区 inode。父 Users 可写意味着其他进程仍可重命名整个 admin；重复核验可以发现替换，不能阻止动作之间的重命名，也不是原子路径锁。检测到丢失或替换时拒绝后续操作并保留登记及对象，不追扫磁盘、不自动重建或换绑。重启后只有原 admin 在规范位置且身份一致才可重开；不能保证被其他进程移走的恢复区始终能自动找到。原厂回收的数据归档同样从 admin 起校验权限，其固定数据库与 journal 保留独立的严格身份、权限与 SQL 证明。

私有目录保护不依赖 `0700` 阻止宿主 root 读取：所有相对路径组件中的 `.r1-toolbox-ops` 名字及大小写变体都保留，不再允许普通子目录中的同名目录。真实打开每个后代时，另以实际父目录 FD 获取其固定私有名字的 inode 元数据，拒绝同对象的文件系统名称别名，再允许读取用户内容。因此旧 admin 即使被外部移入新 admin 的普通子目录，也不能通过深层路径或短名别名读取保管对象。检查不读取保管内容、不自动创建目录，证据无法确认时拒绝操作。`.r1-toolbox-ops2` 及其嵌套路径仍是普通名字。

网页“API 管理 → Token 操作 → 文件变更与恢复”提供预检、初始化、检查保留对象、停止新变更和恢复。固定所有者接口为：

| 方法和地址 | 严格请求 | 影响 |
| --- | --- | --- |
| `GET /api/files/authority` | 无查询参数 | 只读 Toolbox 登记，不打开 Users |
| `POST /api/files/authority/preflight` | `{"root_id":"root_…"}` | 显式检查 UID、权限、ACL、挂载和未知同名目录，可能唤盘；不创建目录 |
| `POST /api/files/authority/initialize` | `{"root_id":"root_…","confirmed":true}` | 创建、重新启用或显式核验升级当前身份一致的恢复区 |
| `POST /api/files/authority/inspect` | `{"root_id":"root_…"}` | 显式列出此根保留记录，可能唤盘 |
| `POST /api/files/authority/revoke` | `{"root_id":"root_…","confirmed":true}` | 停止新变更，不删除保留对象 |
| `POST /api/files/authority/restore` | `{"root_id":"root_…","recovery_id":"operation_…","confirmed":true}` | 只恢复记录绑定的原路径；目标存在时拒绝覆盖 |

这些接口沿用同源、JSON、CSRF 和当前 Web 授权，拒绝任意路径、命令和 SQL。GET、开网页、服务重启和被动监控不自动初始化或访问用户文件。显式预检的 `ready:false` 与 `blockers/checks/source_binding` 是实际阻断证据，不属于初始化成功。

## 冲突、索引与重启

复制使用安全源 FD 和匿名 `O_TMPFILE`，只通过内核无覆盖发布新 inode。目录在受信目录内出生，核验后无覆盖移入目的地。覆盖先保留并核验旧对象，再发布已校验的新 inode；旧对象不会作为“清理”被永久删除。改名和移动是可恢复的两阶段操作，跨文件系统拒绝。

原厂通知后必须独立核对 `FILE_DB_V102`：目录核对目录记录，新建／复制核对新记录，改名／移动核对旧路径消失及新记录，覆盖避免将同秒、同大小的旧行误当完成。真实 inode 和内容证明来自保留 FD，不能从 SQL 行推断。

写操作持久化幂等与恢复意图。进程重启时未终结任务转为 `result_unconfirmed`，不重新执行，不凭 HTTP 200、原厂通知或 ACK 宣称 `completed`。任务查询可能访问磁盘，返回 `may_wake_disks:true`。数据恢复与索引确认分开：`state:restored` 表示对象已回到原路径，只有 `index_verified:true` 才表示原厂索引也已核对；索引失败不抹掉真实恢复结果。

## NAS 验收

部署后先只读预检实际 Users 权限、SSD 根和原文件属性。用户指定的 `临时共享/FWA229P.bin`（v3 admin 根内相对路径） 继续只读，不能用于覆盖、改名或回收。写验收另建 `r1-toolbox-accept-<随机值>` 独立对象，逐项检查 SHA-256、无覆盖冲突、当前版本、任务状态、原厂索引及恢复。临时 Token 仅授测试目录、必要工具与实际来源，结束后撤销。恢复区预检失败时先报告完整阻断项，不修改目录权限试通。
