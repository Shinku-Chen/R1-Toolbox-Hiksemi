# 文件变更与恢复

更新于 2026-10-31。文件执行器已提供新文件上传、已有文件覆盖、单目录创建、普通文件复制、改名、同盘移动和移入原厂回收站。代码、隔离测试和 NAS 实机验收分别记录；注册工具不代表当前磁盘已通过门槛。原 v2 SSD 根曾受 Users 下十四个 overlay 子挂载阻断；7fcd12b 实机已确认 v3 admin 根保留这些子挂载并通过真实句柄检查，原测试文件的信息、HEAD 与 1 KiB Range 读取通过。此前恢复区因可写祖先及 Users 权限拒绝；a4d5bc6 的 admin 信任锚策略已在 SSD 上完成所有者初始化，登记修订为 1。首次独立目录创建返回失败；后续新名称的上传准备明确返回 `write_backend_unverified`，b3d2039 的固定预检已定位到读取 sysfs 分区设备号。当前只修正已证明的读取器缺陷，尚未完成 NAS 文件写入或应用安装验收。详见本文末的实机记录；此前只读范围见[实机记录](file-users-boundary.md#2026-10-307fcd12b-实机只读核对)。

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

新恢复登记写 `file-mutation-admin-authorities.json`，版本为 3。实际位置仍为 `layout:admin_custody_v1`，新项另存 `trust_model:admin_anchor_v1` 与 `admin_identity:[dev,ino,S_IFDIR]`。旧版此文件的版本 2 只读加载后仍按原完整祖先策略执行，不因升级程序而自动放宽；只有所有者显式确认初始化／核验升级、精确核对原恢复区 inode 后才绑定当前 admin，并原子保存新策略。版本 3 允许保留未升级的旧项，其旧策略不变。原 `file-mutation-authorities.json` 版本 1 保持只读与原字节：Users v2 根为 `legacy_boundary`／`legacy_users_boundary_v2`；admin v3 根但实际保管于 Users 的项为 `legacy_layout`／`legacy_recovery_layout`。两类旧位置均不可借本次核验升级搬动、接管或重新解释，须先用原镜像处理旧保管对象。a4d5bc6 初始化前 NAS 没有登记；初始化后 SSD 为 `registered/enabled:true/revision:1`。

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

### 原厂写入索引预检

`POST /api/dev/files/write-preflight` 只接受 `{"action":"preflight","confirm":true,"root_id":"root_…"}`。根必须来自当前 admin v3 目录；目标是服务端在该 admin 根内生成的随机、未发布叶名，不接受路径、操作 ID、URL、原厂 action 或 SQL。此接口要求有效开发窗口和真实来源白名单，不用网页会话替代；任何 `Authorization` 头均拒绝。仍须同源 JSON／CSRF，不接受 URL 参数，采集共用存储锁，并在取得锁后及返回前复核开发授权。它不属于默认检测、统一诊断目录、被动业务刷新或 MCP 工具。

接口复用生产 `NativeOwnerWriteAuthority` 与 `NativeFileIndex.preflight`，逐阶段记录内置 SATA 身份、原厂 ELF／20443／挂载命名空间、固定 Unix 套接字及其所有者、原厂固定缓存链、DB／WAL／SHM、真实 SQLite FD、严格 schema 和精确目标索引查询。原门槛、异常和时间窗口不变；observer 失败不能影响生产判定。前置失败即停止，依赖项保持 `not_run`；`failure_stage` 标记最深未完成的门，外层尚未结束的阶段为 `unconfirmed`，不把它们都算作独立根因。

结果 `preflight.scope:native_index_preflight`、`ready`、固定 `error_code` 和 `stages[]` 分开表达请求已处理与原厂门是否通过。阶段仅含固定 ID、状态、尝试次数与耗时，不返回原厂会话、DB 行、SQL、异常文本或用户内容。`trace_schema_valid` 仅表示阶段记录符合白名单，并不意味着每个门均通过。`ready:true` 也不是写入验收：这里的父目录是 admin 根，未检查调用者选定的业务子目录、恢复区捕获、容量、匿名上传、发布、通知及后续索引完成。

这项检查可能唤盘，固定只读 SQL 也不保证零文件元数据副作用：SQLite 内部可能以读写模式打开已存在 WAL、对 SHM 执行属主处理，WAL 轮转竞态可能初始化空 WAL。保留既有真实 inode、只读事务与 sidecar 身份核验；不修改 SQL 数据，不创建测试目录、上传暂存或用户文件，不发送原厂索引事件，不对失败动作重放。

### 2026-10-30：a4d5bc6 首次写入验收

- 所有者在 SSD 初始化 `Users/admin/.r1-toolbox-ops`，HTTP 200；随后登记复核为 `registered/enabled:true/revision:1/trust_boundary:admin`。
- 用独立临时 Token，仅授 `临时共享` 父目录下创建一次随机测试目录及查询本键任务。该 `create_directory` 返回任务 `failed`；脚本立即停止，未创建后续完整验收 Token，未发送上传内容、覆盖、复制、改名、移动或回收操作。原任务错误字段未保存在本地日志，不能据此猜测单一根因；日志已补固定错误码投影，原任务不重放。
- 所有者 `inspect` 返回 `recoveries:[]`；另一次临时 Token 的真实 `get_file_info` 对原测试目录返回 `not_found`。这两项共同保留现场，未调用恢复或停止登记。
- 对不同随机文件名只发起一次 `prepare_file_upload`，其 MCP 固定错误为 `write_backend_unverified`；未执行 PUT 或 commit。它说明共用写入预检仍未通过，不能直接归因为 admin ACL、SATA 或数据库中的某一门。
- 两个实际创建的临时 Token 均撤销成功并用原 Bearer 复核 HTTP 401；测试未请求或修改原来的 `FWA229P.bin`。恢复区继续登记启用。文件上传、覆盖、目录创建、复制、改名、移动、真实原厂回收和恢复仍待实机通过，不以隔离测试或此预检替代。

### 2026-10-30：b3d2039 固定写入预检

实机已核对运行精确 `b3d2039eab539fa702ae32d439a2fc8d81c832af`。恢复登记仍为 `registered/enabled:true/revision:1/trust_boundary:admin`，未重新初始化。只发起一次明确确认的固定 `write-preflight`，HTTP 200；内部 `ready:false/error_code:write_backend_unverified/failure_stage:index.sata.sysfs_device`。原厂固定所有者、路径、挂载表、唯一规范挂载和设备来源格式已通过，后续块设备节点、传输类型、套接字及索引 DB 依赖步骤均未运行。本轮未创建 Token、上传暂存、测试目录或用户文件，未发送原厂索引通知。

代码检查确认此处曾用普通 `_read_fixed(...,64)` 读取 `sys/block/<磁盘>/<分区>/dev`，既拒绝声明 `st_size>64`，又要求非零声明长度等于实际内容长度。Linux 的 sysfs 文本属性按 `PAGE_SIZE` 声明容量，实际由属性的 `show()` 返回内容长度，二者不能等同；见 [Linux sysfs 实现](https://github.com/torvalds/linux/blob/v4.14/fs/sysfs/file.c)。这证明读取器存在独立缺陷，并与实机失败阶段吻合；本次诊断未输出 NAS 的实际 `st_size` 或 errno，不能把推断写成实测，也不能据此宣称其他后续门都已通过。

修复仅新增严格构造的 `sys/block/sdX/sdXN/dev` 专用读取器，以实际 EOF 为准，上限 64 字节，持续处理短读，并保留 no-follow、普通属性类型、打开前后设备／inode／模式／声明大小／mtime／ctime 稳定性。普通 `_read_fixed` 原有长度检查保持不变；读取成功后仍必须与挂载设备号、真实块设备 `rdev` 和内置 ATA 传输路径相符。定向回归包含声明 4096 字节而实际只有短文本、超限、短读、身份变化及 Linux 真实 sysfs 属性；修复后须部署并重新跑固定预检，通过后才继续新随机目录的写入验收，不重放旧操作。

### 2026-10-31：0fa458b 的 Unix 监听预检

实机已核对运行精确 `0fa458b386bf4167019a150e35f82a62045f3851`，只发起一次固定 `write-preflight`，HTTP 200。此前的 `index.sata.sysfs_device`、块设备节点、设备号匹配和内置 ATA 传输检查均已通过；原厂进程、二进制、挂载命名空间及固定套接字路径也已通过。新的 `ready:false/error_code:native_unavailable/failure_stage:index.listener.unix_table` 停止了后续 FD 归属及索引数据库检查；这些依赖阶段保持 `not_run`。本轮没有创建 Token、测试文件或上传暂存，也没有发送原厂索引通知。

代码与可信 fixture 复现了一项独立解析缺陷：原实现要求 `/proc/net/unix` 中每条同名 `/tmp/UNIX.domain` 记录都是监听条目；正常 Unix stream 连接也会继承监听者的路径，因而可能把已连接条目误判为第二个非法监听者。[Linux v4.14 的连接实现](https://github.com/torvalds/linux/blob/v4.14/net/unix/af_unix.c#L1264)复制监听地址，表输出则分别标记监听、待接收和已连接状态。该证据不是 NAS 本轮原始套接字行：现有阶段回执没有返回其行内容，读取异常、超限及实际监听状态仍须与现场区分，不能据此声称唯一根因已确认。

修复只调整同名表项的分类，监听者仍须唯一、为 stream 监听状态且有合法正 inode；其他条目不能成为监听候选。原厂 ELF／PID 与真实 FD 归属、完整 FD 枚举、root 属主套接字身份、命名空间、`SO_PEERCRED` 和通知前后的持续核验保持不变。回归使用合成混排行及 Linux 临时随机路径上的真实连接，不连接 NAS 或原厂 `/tmp/UNIX.domain`。仍须部署后通过固定预检，才能开始新一轮独立文件写入验收。
