# 文件变更与恢复

更新于 2026-10-30。文件执行器已提供新文件上传、已有文件覆盖、单目录创建、普通文件复制、改名、同盘移动和移入原厂回收站。代码、隔离测试和 NAS 实机验收分别记录；注册工具不代表当前磁盘已通过门槛。本批尚未执行 NAS 文件变更或应用新装。

## 授权与范围

所有文件内容仍限定在 `/drives/*/Users/`，以可信 `root_id` 和相对路径调用。读取仅支持经原厂固定管理员 ACL 核验的 `admin/public` 正子集；写入仅支持已核验内置 SATA 的 `admin` 后代，不修改其他用户、`admin` 根、隐藏路径或授权外的父目录。固定原厂内部树 `admin/.DATA_CACHE` 及后代不作为普通写入或复制源开放；真实回收仅由内部固定协议访问。

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

恢复区固定为所选 `Users/.r1-toolbox-ops`。只能由网页登录所有者显式初始化；机器 Token 和开发白名单不能自登记。初始化检查完整根属主、权限、ACL、挂载与同对象身份，创建 root 专用 `0700` 目录，不接管已有未知目录，不修改 Users 权限。真实目录若允许非 root 写入、含扩展 ACL 或身份不明，就拒绝初始化；不会以 fixture 的成功替代 NAS 结果。

网页“API 管理 → Token 操作 → 文件变更与恢复”提供预检、初始化、检查保留对象、停止新变更和恢复。固定所有者接口为：

| 方法和地址 | 严格请求 | 影响 |
| --- | --- | --- |
| `GET /api/files/authority` | 无查询参数 | 只读 Toolbox 登记，不打开 Users |
| `POST /api/files/authority/preflight` | `{"root_id":"root_…"}` | 显式检查 UID、权限、ACL、挂载和未知同名目录，可能唤盘；不创建目录 |
| `POST /api/files/authority/initialize` | `{"root_id":"root_…","confirmed":true}` | 创建或重新启用当前身份一致的恢复区 |
| `POST /api/files/authority/inspect` | `{"root_id":"root_…"}` | 显式列出此根保留记录，可能唤盘 |
| `POST /api/files/authority/revoke` | `{"root_id":"root_…","confirmed":true}` | 停止新变更，不删除保留对象 |
| `POST /api/files/authority/restore` | `{"root_id":"root_…","recovery_id":"operation_…","confirmed":true}` | 只恢复记录绑定的原路径；目标存在时拒绝覆盖 |

这些接口沿用同源、JSON、CSRF 和当前 Web 授权，拒绝任意路径、命令和 SQL。GET、开网页、服务重启和被动监控不自动初始化或访问用户文件。显式预检的 `ready:false` 与 `blockers/checks/source_binding` 是实际阻断证据，不属于初始化成功。

## 冲突、索引与重启

复制使用安全源 FD 和匿名 `O_TMPFILE`，只通过内核无覆盖发布新 inode。目录在受信目录内出生，核验后无覆盖移入目的地。覆盖先保留并核验旧对象，再发布已校验的新 inode；旧对象不会作为“清理”被永久删除。改名和移动是可恢复的两阶段操作，跨文件系统拒绝。

原厂通知后必须独立核对 `FILE_DB_V102`：目录核对目录记录，新建／复制核对新记录，改名／移动核对旧路径消失及新记录，覆盖避免将同秒、同大小的旧行误当完成。真实 inode 和内容证明来自保留 FD，不能从 SQL 行推断。

写操作持久化幂等与恢复意图。进程重启时未终结任务转为 `result_unconfirmed`，不重新执行，不凭 HTTP 200、原厂通知或 ACK 宣称 `completed`。任务查询可能访问磁盘，返回 `may_wake_disks:true`。数据恢复与索引确认分开：`state:restored` 表示对象已回到原路径，只有 `index_verified:true` 才表示原厂索引也已核对；索引失败不抹掉真实恢复结果。

## NAS 验收

部署后先只读预检实际 Users 权限、SSD 根和原文件属性。用户指定的 `admin/临时共享/FWA229P.bin` 继续只读，不能用于覆盖、改名或回收。写验收另建 `r1-toolbox-accept-<随机值>` 独立对象，逐项检查 SHA-256、无覆盖冲突、当前版本、任务状态、原厂索引及恢复。临时 Token 仅授测试目录、必要工具与实际来源，结束后撤销。恢复区预检失败时先报告完整阻断项，不修改目录权限试通。
