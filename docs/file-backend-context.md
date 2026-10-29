# 文件后端上下文与字节传输

本批增加真实目录句柄诊断与有界文件传输状态机。**尚未有生产文件后端证明 NAS 主体、Unix 身份、原厂 ACL 与同一实际对象，因此不注册文件浏览、下载、上传或文件变更工具。** 下面的传输组件可以接受可信后端的句柄，并不意味着目前可以读取 NAS 文件。

## 已确认的原厂约束

固定固件 `V6.3.3_Build_260907` 的 `emhistor` 样本 SHA-256 为 `402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26`。

`rest_check_path_auth@0x281dc9` 使用路径、NAS 用户名、逻辑 user ID 和访问类别；调用 `rest_get_path_info@0x281689` 与管理员类别判断 `0x281280`，再按路径类别处理 `public`、用户名和外部盘权限。它不能等同为 POSIX uid／gid 的访问检查。文件下载调用者先按路径执行该检查，随后才进入另一个按路径打开的 handler；现有静态证据没有把原厂判权与最后的文件句柄闭合为同一对象。

因此，下列做法均不能启用文件功能：把管理员逻辑 ID `100000` 当成 Unix uid、使用宿主 root 直接读写、把根规划或目录 O_PATH 句柄当 ACL、用布尔 `acl_verified` 请求字段自证权限、根据固定动作名称猜测 HTTP／Range／上传协议。

## 固定开发检查

主线调用：

```python
from api_file_operations import collect_file_backend_context
context = collect_file_backend_context(panel)
```

内部签名为 `collect_file_backend_context(panel, *, root='/', root_collector=None, owner_reader=None, runtime_reader=None)`。后四项只用于固定宿主／测试依赖，不能成为 HTTP 参数、任意路径、SQL、账号或原厂动作代理。建议固定开发项目名 `file_backend_context`；主线仍须执行真实来源 IP 白名单、非空开发窗口、同源／JSON／CSRF，采集结束重新核验授权。该项目不得自动加入默认检测或定时轮询。

一次检查包括：

- 仅读取固定 `USER_AUTH_DB_101.tb_user_auth` 的 `user_id/user_type`，确认固定管理员逻辑身份；SQLite authorizer 拒绝令牌表、其他列和视图引入的凭据列，不登录或读取 access_token。
- 固定 `/etc/passwd` 与 `/etc/group` 的有界元数据，报告是否存在相同数字 UID 候选。输出只有 `unverified` 与固定原因，不输出用户名、口令字段、原数据库或原配置。数字巧合始终不是授权证明。
- 最多八个 Users 规划根，以 `O_PATH|O_DIRECTORY|O_NOFOLLOW` 逐级取得目录句柄，不枚举目录项、不读取文件内容。核对目录句柄 `mnt_id`、设备号及前后 boot、根规划和 mountinfo；变化后撤销本次句柄摘要。
- 当前固定 `emhistor` TLS 监听身份是否符合已有进程／唯一 socket 核验，以及原厂文件路由、分类链的静态参考范围。不会签名或发送原厂请求，静态参考仍为 `runtime_verified:false`。

目录 O_PATH 路径查找和元数据访问**可能唤醒数据盘**，因此返回 `effects.may_wake_disks:true`；不能宣传全程不唤盘。该检查不运行 SMART、空间扫描、文件内容读取、下载器或原厂业务动作。`complete` 只表示本次固定项目是否采够，不表示文件访问可用；`file_access_available` 始终为 `false`。

## 可信后端与传输组件的连接

`api_file_transfers.FileTransfers(is_current)` 接收 `ApiAccessStore.is_current`，不读取 HTTP 身份。传输 principal 的 `_peer` 必须由 HTTP Handler 从真实 peer 写入；缺少真实来源时拒绝准备传输。绑定包含键 ID、授权修订、Token 代际及 peer，全部接口重新核验。

### 下载

可信后端构造 `DownloadSource(root_id, path, name, open_reader, authorize_opened)`。`open_reader(principal)` 必须在 NAS 主体／ACL 已被验证的条件下返回本次独占的只读普通文件 fd；`authorize_opened(principal, fd, write=False)` 必须继续核验同一对象的原厂 ACL、根、挂载与生命周期。组件不会替后端打开路径，也没有 root 回退；不能直接从客户端提交的根 ID 构造该对象。

内部方法：

```python
manager = FileTransfers(access_store.is_current)
receipt = manager.prepare_download(principal, verified_source)
response = manager.open_download(receipt['transfer_id'], principal,
                                 head=False, range_header=None)
# HTTP 层发送已验证的 response.status / response.headers 后：
response.send(writer)
# 未消费响应、发送头失败或断开连接时：
response.close()
```

内容通道仍待主线有真实后端后接入。它只能使用 Bearer Token，拒绝 Cookie／开发窗口替代授权；HTTP 层还须拒绝重复 Range、条件请求、额外查询参数和片段，并保留连接名额与 socket 超时，不能仅因组件存在就开放内容路径。

组件已实现：固定随机 128 位传输 ID；每键两个、全局四个活动句柄；HEAD 不开始或延长窗口；单个完整／开放／后缀 Range；416 精确范围；安全附件名编码；不返回强 ETag；五分钟首次 GET、首次 GET 起固定三十分钟窗口；同句柄只允许一个活动 GET；每块前后复验当前授权与精确对象；断开、取消、到期和关闭释放活动 fd。完成只表示服务器按长度发送，不能替代客户端落盘或 SHA-256 验证。

`get_transfer()` 与 `cancel_transfer()` 同时核验对应具体工具和最初资源授权。轮换、改权限、换 peer 或服务关闭后不能用旧句柄接续。多个 Range 的发送总量可以重叠，不能据此伪造完整下载。

### 上传

可信后端构造 `UploadTarget(root_id, path, name, create_staging, authorize_opened, install)`。组件要求 staging 为 **未命名、链接数零、空的 O_RDWR 普通文件**，不接受已有目标、可见临时文件或追加模式。后端必须先证明原厂 ACL、配额、索引、目标前置条件和原子安装语义，不能只返回一个临时 fd 便声称通过。

`prepare_upload()` 固定目标、长度、SHA-256、覆盖前置条件及幂等键；`receive_upload()` 对单次 PUT 的每块做权限和长度检查，使用真实 fd 写入并核验完整散列。`commit_upload()` 重新读取散列及对象状态后调用可信 `install()`；后端还必须在实际变更点再次检查授权／ACL／根和目标 CAS。没有通用 `rename`、永久删除或绕过索引的写回退。组件拥有传入 fd；后端不能擅自关闭它。

安装回调抛错或无法给出结果版本时，任务记录 `result_unconfirmed`，不盲目重试或声称已回滚。上传提交幂等标识绑定当前键／修订／代际／peer 和固定传输，重复同键只返回原操作结果，参数冲突拒绝。匿名 staging 在中断或撤权后关闭；已经安装的对象不会被组件当清理样本删除。

ACL 回调、落盘同步与原厂安装返回后仍重新核验当前授权和传输期限。期间撤权、轮换、改权限或到期时丢弃请求结果；已执行安装的内部任务保留 `result_unconfirmed`，不向失效请求回传文件版本，也不尝试删除可能已经安装的对象。

这些流程仅是可测试的组件能力，**不是现有 NAS 上传后端的实现证明**。在主体／ACL／安全打开／索引／配额／原子安装未闭合前，`UploadTarget` 没有生产构造器，不得注册上传或变更工具。目录创建、改名、复制、移动及原厂回收站也保持未接入。

## 验证边界

```bash
python -B -m unittest discover -s tests -p test_api_file_operations.py -v
python -B -m unittest discover -s tests -p test_api_file_transfers.py -v
```

测试使用临时 SQLite、主机元数据和普通文件字节；ACL 与安装回调均为明确的内部 fixture，不是实机权限证明。Linux 用例覆盖真实 O_PATH／符号链接拒绝、真实 fd 字节、Range、对象变化、逐块撤权、匿名 staging、散列及安装前置条件。Windows 缺少 O_PATH、fcntl、pread／pwrite，对应测试跳过；纯参数、路径范围、peer 与固定证据脱敏测试继续运行。最终仍须 Linux CI，并在只读真实后端确认后才安排 NAS 文件读取验收。
