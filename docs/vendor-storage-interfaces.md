# 原厂存储与独立服务本地接口注册表

本文基于用户提供的 `R1_X01_GML_STD_V6.3.3_Build_260907.zip` 静态分析；复核日期为 2026-10-29。配套机器可读清单见 [`vendor-storage-interfaces.json`](vendor-storage-interfaces.json)。本轮没有运行厂商 ELF／脚本，没有访问 NAS、扫描端口或发起 SMART、自检、控制操作。

## 覆盖范围与使用方式

本轮扫描了已解包 `/histor/bin`、`/histor/basis`、rootfs `/usr/local`、`/opt` 中除 `emhistor` 外的 355 个 ELF，并读取有关启动脚本、NUT、D-Bus 激活配置。完整枚举的是下列固定注册表：

| 注册表 | 已枚举数量 | 证据边界 |
|---|---:|---|
| `disk_space` 命令表 | 22 | `.data` 文件偏移 `0x2231a0–0x2232f0`，连续 22 组字符串／handler 指针；全部逐项登记。 |
| `part_opt` 子分支 | 8 | `u_part_opt@0x4ac08` 明确区分 `opt_type=0–7`。 |
| `ipc_manager` 摄像机命令表 | 54 | `.data` 文件偏移 `0xc87f00–0xc88250`；完整列举名字和 handler，尚未逐项追踪下游行为。 |
| `image_gps` 命令表 | 2 | `.data` 文件偏移 `0x1600e0–0x1600f0`。 |
| `appcenter` 主分发表 | 6 | `.data` 文件偏移 `0x2e020–0x2e070`；有二级模块分发，不能把主表当成全部 HTTP 路由。 |

`proxyDaemon`、`emclient`、媒体服务只覆盖本文明确给出的分支／地址；D-Bus 只覆盖包内激活文件，不枚举标准服务的全部方法。`emhistor` RPC／REST 在另一份注册表中维护，本文不重复。第三方应用、容器端口及 `/screen` 分区中的实际屏幕应用均不在本轮完整覆盖范围中。

**二进制存在、字符串存在、注册项存在、当前正在监听，是四种不同证据。** 固件共用库在多个 daemon 中带有相同 `/var/tmp/disk`、SMART、灯光、升级字符串，不能据此把每个二进制都判成存储或硬件服务。机器清单分别标记 `handler_checked`、`registered_only`、`client_only`、`activation_file_only` 等证据等级，所有条目均为 `default_enabled: false`。

接口地址按 ELF 链接虚拟地址记录。PIE 进程运行时需加装载基址；有些程序是固定地址的 ET_EXEC。地址只用于复核此固件，生产代码不能硬编码函数地址。

## 接入等级

- **A：被动读取候选**。静态已确认读取已有状态，仍须核验当前监听者身份、协议边界、超时、返回大小和缓存时效，才能进入固定白名单。
- **B：继续复核**。运行可用性、参数、协议或副作用尚不完整，禁止默认调用。
- **C：仅明确用户操作**。允许作为未来功能候选讨论，必须沿用鉴权、用户确认和硬件身份等既有边界；本文不实现写功能。
- **D：禁止默认接入**。破坏性管理、通用命令执行、已知唤盘、账户／云状态操作或空桩。记录名字便于防误用，不提供执行模板。

读请求也可能写入日志；本表的“只读”指没有发现请求路径上的业务状态写入、控制或新增硬盘采集，不是承诺整个系统无任何文件变化。带 `get` 的名字不能代替调用链审计。

## `/var/tmp/disk`：22 条固定命令

服务为 `/histor/bin/disk_space`。主函数 `0x53780` 在 `0x53a43` 将该路径交给 socket 服务启动函数 `0x4cd3b`。这是本地 `AF_UNIX/SOCK_STREAM` 服务；表中的命令 token 不等于完整请求帧，不应照 token 拼任意请求或开放通用转发入口。

| 命令 | handler → 实现 | 行为与副作用 | 等级 |
|---|---|---|---|
| `get_space_list` | `0x49ca0 → 0x42123` | 从内存空间链表返回名称、成员、盘身份和空间状态。 | A |
| `get_disk_list` | `0x49ee0 → 0x429f3` | 从内存盘链表返回物理盘、分区、RAID 已采集状态。 | A |
| `get_fix_status` | `0x49dc0 → 0x41210` | 只构造当前修复任务的 `id`／`status`，不会启动修复。 | A |
| `get_hide_path` | `0x4a306 → 0x43800` | 在已有空间链表查隐藏路径；内部路径不应直接公开。 | B |
| `get_smart_info` | `0x4b9fe → 0x492f9 → 0x23bab` | 本固件末端实现是空桩，不能取 SMART 缓存。 | D |
| `get_self_test_status` | `0x4bc3c → 0x49901 → 0x24842` | 同步运行 `smartctl -a`，没有 `-n standby`，可能唤盘。 | D |
| `start_self_test` | `0x4bb26 → 0x49543` | `smartctl -t short/long`，启动真实自检。 | D |
| `create_zhcc_part` | `0x4a1b1 → 0x4359c` | 创建私有分区。 | D |
| `format_disk` | `0x4a61c → 0x444c7` | 清理 RAID 元数据并格式化整盘。 | D |
| `format_usb` | `0x4a4c7 → 0x43a14` | USB 分区创建、格式化、挂载。 | D |
| `mount_disk` | `0x4a771 → 0x4478b` | 改变挂载状态，可能唤盘及重放文件系统日志。 | D |
| `umount_disk` | `0x4a8c6 → 0x44a48` | 卸载，可能中断用户访问。 | D |
| `basic_expan_done` | `0x4a9dc → 0x44cac` | 扩容完成后的内部元数据更新。 | D |
| `basic_mig_done` | `0x4aaf2 → 0x451f5` | 迁移完成后的内部成员和元数据更新。 | D |
| `part_opt` | `0x4ac08` | 格式化、挂卸载、RAID 成员和修复多路入口。 | D |
| `refresh_info` | `0x4b1fb → 0x45e46` | 会调用 `disk_dev_inner_info_refresh` 写私有元数据。 | D |
| `clean_inner_space` | `0x4b311 → 0x46cfe` | 清除空间私有元数据。 | D |
| `zone_create_raid` | `0x4b427 → 0x471f6` | 创建阵列，改变数据布局。 | D |
| `space_rename_new` | `0x4b53d → 0x47d24 → 0x3ddea` | 改名并写私有元数据；可能唤盘。 | C |
| `space_fix` | `0x4b653 → 0x414cb` | 启动真实修复。 | D |
| `refresh_space_list` | `0x4b769 → 0x48fff` | 清空后重建原厂全局内存空间列表，不是纯读快照。 | B |
| `inner_info_writeback` | `0x4b8b6 → 0x490bb` | 显式写回私有元数据并刷新挂载信息。 | D |

`part_opt` 的 `opt_type=0/1/2/3/4/5/6/7` 分别进入格式化、挂载、卸载、添加 RAID 成员、移除 RAID 成员、修复分区、另一添加成员分支、迁移成员。全部子分支的 handler、已识别输入字段和风险已列入 JSON。未知参数不提供猜测默认值。

### 可取得的存储监控字段

`get_space_list` 返回 `info` 数组；每个空间包含 `space_name`、`space_disk_id`、`space_member`、`space_group`、`space_level`、`is_raid`、`port`、`space_index`、`mount_point` 等字段，以及 `member[]` 中的 `port`、`disk_name`、`root_name`、`disk_id`。这些字段可以把海康用户设置的空间名称关联到真实物理盘，但 RAID 名称属于整个空间。

`get_disk_list` 包含 `disk_name`、`disk_port`、`disk_serial`、`disk_model`、`disk_cap` 等物理盘字段，分区成员包含 `zone_part_uuid`、`zone_fs_uuid`、`zone_lab`、`zone_mount_path`、`zone_cap`、`zone_fs_cap`，以及 `raid_state`、`raid_devices`、`raid_total_devices`、`raid_progress` 等 RAID 字段。它们是原厂后台状态，不是 Toolbox 本次实时测量；新增展示前还须核对单位、语义及状态更新时机，不能直接当成新鲜读数。

### SMART 两个易误用点

`disk_dev_smart_get@0x23bab` 仅 19 字节，函数保存参数后直接返回 `0`，不填写输出。因此注册了 `get_smart_info` 不代表具备有效 SMART 读取能力。

`disk_get_self_test_status@0x24842` 在 `0x248ff` 引用 `smartctl -a /dev/%s`，`0x24929` 调用 `popen`。这个 getter 没有休眠保护。Toolbox 仍只使用自己的后台缓存和带 `-n standby` 的采集链；不能为补字段临时调用该接口。

## APP 空间管理 REST：`space_manage`

用户给出的 APP 请求属于 `/rest/1.2/config?action=space_manage&check_warning=1`。原厂云代理地址和 `PROXY_TARGET` 负责到目标 NAS 的传输，不能把代理端口当作本机服务端口；签名和分配地址不写入仓库。本轮只核验了用户给定代理的 TLS 证书与连接，没有提交带签名的业务请求。

在 Build_260907 中，v1.2 的 handler 为 `rest_space_manage_v1@0x26f41d`，返回空间和未使用磁盘信息。它与本机 socket getter 的字段不同：

| HTTP 字段 | 已确认语义 |
|---|---|
| `space_list`、`unused_disk_list` | 空间列表、未使用磁盘列表；不同分支的结构不同 |
| `disk_sn` | 物理盘缓存结构 `+0x1118` 的序列号；handler 直接构造字符串，未见脱敏。实机缓存是否完整仍需核对 |
| `space_nickname` | 新空间分支从空间结构 `+0x460` 取得的自定义名称 |
| `space_name` | 新空间分支刻意为空；兼容分支可能是分区名或默认空间名，不能作为统一的用户昵称字段 |

这里没有 `disk_serial`／`disk_id` 输出键，不能套用 `/var/tmp/disk` 两个 getter 的同名语义或只看 `space_name` 判断名称缺失。

该 action 不符合 Toolbox 的被动采集边界：

- `0x26f7cf` 调用 `disk_creat_satalink@0xaf7fb5`，运行 `create_satalink.sh`；脚本在别名缺失时补建 `/dev/hda` 等链接。
- 多处分支调用 `disk_manage_info@0xafb4d6`；设备支持但未启用 SMART 时，下游 `0xb172f6` 会发送 ENABLE `0xd8` 和 AUTOSAVE `0xd2`。
- `disk_get_smartinfo@0xaf9e49` 读取缓存前可能重命名 `/tmp/smartinfo-N`，不是不改变状态的缓存 getter。
- 5／6 号位 SSD 分支包含未带 `-n standby` 的 `smartctl -A`。待机判断对大于 4 号位或部分错误返回非待机，不能保证不唤醒。

v1.2 dispatcher 在 `0x163dd3–0x163df5` 解析 `check_warning`，但此样本的 handler 不读取该字段；去掉参数也不能消除上述行为。v1.1 同名 handler `0x25a0c1` 是空函数，不能作为等价降级入口。未穷尽的间接分支保持未知，不因为已经确认部分查询行为就宣称整条链无副作用。

Toolbox 继续使用固定 `get_space_list`／`get_disk_list` 与现有身份缓存。完整序列号可由显式 [`storage_identity`](disk-names.md#显式读取完整硬盘序列号) 检查取得，不把 APP 的空间管理查询加入监控轮询。

另提供需逐次确认的开发入口 [`POST /api/dev/storage/probe`](storage-probe.md)，在宿主使用固定所有者已有认证访问 `127.0.0.1:80`，仅投影 `disk_sn` 与空间身份；这是所有者明确授权的主动验证，保留上述原厂副作用，不改变被动采集边界。

## 旧数据库、当前名称与磁盘休眠

`init_inner_disk_info@0x3cbc3` 在启动时读取旧 `/tmp/nand_config/disk_space.db`，按真实磁盘身份把旧名称等信息写入私有元数据，再在 `0x3da71–0x3da7f` 执行 `rename`，转为 `disk_space_cache.db`。当前 `space_rename@0x3ddea` 走 `disk_dev_inner_info_get@0x330c8`／`disk_dev_inner_info_refresh@0x33ad0` 更新私有元数据和内存，未调用旧数据库更新函数。因此两个 DB 文件和旧 `config.txt:disk_nickname` 数组都不能当作当前名称的权威来源，详见 [`disk-names.md`](disk-names.md)。

`temp_control` 不是仅控制风扇的进程。其 `disk_autosleep@0xf2d7` 负责原厂磁盘休眠；不能为探查接口或处理 CPU 风扇而直接弃用该进程。没有确认它提供独立的“只读磁盘休眠状态” socket。`util_disk_notice_stat@0x21c20` 是 `/tmp/PORT%d_SUBDISK` 的生产者，还会通知 `disk_monitor`，不能把这个写通知函数误当成 getter；端口到物理盘的映射、文件格式和时效尚未形成可靠读取契约。

媒体客户端在 `127.0.0.1:7788` 使用过 `is_disk_sleep` 字面量，但本轮没有确认服务归属、参数格式和完整副作用，不纳入被动读取白名单。`standby_check_tool.sh` 会写 `/proc/hikio/hikioproc` 开关跟踪，也不是休眠状态读取接口。

## 其他独立服务

| 地址 | 服务／注册信息 | 已确认内容 | 接入判断 |
|---|---|---|---|
| `127.0.0.1:8888` | `/histor/bin/appcenter`，`listener_thread@0x8ab8`、`create_server_socket@0x8856` | 确认只绑定 loopback；`0x8ad3` 传入端口常量 `0x22b8`。主表为 `get_apply_list`、`install_apply`、`get_install_progress`、`remove_apply`、`cancel_apply_task`、`get_modulize_table`。 | get 路径也可能同步插件、更新配置、请求应用列表；B／D，默认不接。 |
| `/var/tmp/ipcm` | `/histor/bin/ipc_manager`，启动点 `0x491e09` | 54 条摄像机、录像、空间、账户、PTZ、GB28181 固定注册命令，JSON 逐项列举。 | 尚未逐项排除录像读盘、摄像机访问、账户泄露；B／D，默认不接。 |
| `/var/tmp/image_gps` | `/histor/bin/image_gps`，启动点 `0x1b1dd` | `get_gps_address@0x1ae14`、`set_gps_status@0x1af48`。地址查询会进入数据库和地图服务路径。 | getter 不是纯内存读取，B；状态改变为 C。 |
| `/var/tmp/proxydaemon` | `/histor/bin/network/proxyDaemon`，启动点 `0x1c46a` | `proxy_daemon_comm_usock_action@0x1c4e8` 识别 `create_dataconn`、`remove_dataconn`、`create_fileconn`。 | 都会改变云连接，D；不是网卡状态读取。 |
| `/var/tmp/ezviz` | `/histor/bin/emclient`，启动点 `0x454c9b` | `comm_usock_ezviz_modulize_action@0x45313f` 包含模块安装、检查、下载、云用户和通道分支；20 个已识别字面量见 JSON。 | 非通用监控；D，未声称完整枚举所有云客户端命令。 |
| `/var/tmp/net` | `/histor/bin/bonjour_tool`，入口 `0xf71e` | `active_cb@0xee1a` 识别 `ba` 后调用初始化 `0xf20e`。 | 改变网络发现状态，B；不是网卡统计 getter。 |
| `/var/tmp/system_master` | `/histor/bin/unix_sock_system`，主循环 `0x34fc` | worker `0x33af` → `run_sys_cmd_@0x330a` → `system(request)`。 | 通用执行入口，D；禁止任何用户文本透传。 |
| `/tmp/tr_video_sock` | `/histor/bin/tr_video_daemon`，主循环 `0x10e0` | 固定分支 `STOP`、`START:`；停止会 kill 进程，启动执行器 `0xdf2` 使用 `/bin/sh -c`。 | D；记录风险，不提供执行命令示例。 |
| `127.0.0.1:7769` | `image_convert` 启动点 `0x10f93` | 图片转换服务监听候选，完整回调表未审计。 | B，图片操作可能读写硬盘。 |
| `127.0.0.1:7778` | `image_ocr`，`ocr_server_init@0x8e1e` | 确认服务初始化使用该地址，处理图片／OCR。 | B，不能以监控名义调用算法任务。 |
| `127.0.0.1:7799` | `image_similar`，初始化 `0x29d5f` | 相似图片处理服务。 | B，完整协议与副作用未审计。 |
| `127.0.0.1:7766/7786/7788` | `zmq_cli`／OCR／转换客户端 | 仅确认客户端引用地址；未确认这些地址由谁监听。 | B；名字含 `zmq` 不证明采用 ZeroMQ 协议。 |

上述 loopback 地址是固件常量，不是本轮 NAS 实机监听结果。不能通过盲扫或随意发送请求来“确认”服务。

## UPS、网口、灯效、温度与小屏

NUT 配置 `/etc/nut/upsd.conf:5–6` 仅声明 `127.0.0.1:3493`、`::1:3493`。`nut.conf` 为 `MODE=standalone`；原厂 `ups-notice.sh` 以 `upsc hikups ups.status` 读取 `OB/OL`，并从 `upsc hikups` 结果读取 `battery.charge`。这是优先级 A 的额外状态来源，但仍需确认 UPS 是否连接、实例名和运行中的 NUT 身份。固件内有配置不代表已安装、已启动或已连接 UPS。

`startups.sh` 会运行 `nut-scanner` 并重写 `/etc/nut/ups.conf`；`ups-guard.sh` 包含蜂鸣器控制，`ups-poweroff.sh` 会停驱动／监控并发关机。都不能作为探测或只读刷新步骤；不读取、复制或公开 NUT 用户凭据。

网口等标准 D-Bus 激活文件已逐项收录，包含 `org.freedesktop.network1`、`fi.w1.wpa_supplicant1`、`org.freedesktop.login1` 等。这里只证明存在激活配置，没有确认活跃对象和方法；发送调用可能自动启动服务。未发现经过确认的海康专用存储名字／风扇／灯效总线 API。普通网卡统计优先继续用现有 `/sys/class/net`／内核快照，不因包内出现 D-Bus 文件而迁移到尚未核验的服务。

灯效、风扇和温度继续以已核验的 [`histor-interfaces.md`](histor-interfaces.md)、[`light-policy.md`](light-policy.md) 为准；独立 daemon 中相同的共用库字符串不构成新的控制接口。系统风扇信号同步仍是写操作，CPU 独立策略仍须监督接管，不绕过现有身份预检。

小屏的可见证据只有 `starthistor` 挂载 `/screen` 和 `check_screen.sh` 在 `DISPLAY=:0` 下运行 `xinput` 查 `CST0836`。本轮解包的 histor/rootfs 不含 `/screen` 分区实际应用，不能由此编造 HTTP、D-Bus 或 socket 控制接口。背光继续走项目已实现并识别的设备能力。

最后，`/histor/bin/reboot.sh` 名字具有误导性：脚本先强杀 `emhistor`，末尾实际是 `poweroff -f`，应列为 D，不能按名字当作重启 API。电源控制继续使用 Toolbox 已授权、有倒计时且可取消的流程。

## 后续可安全推进的范围

优先复用固定白名单的 `get_space_list`、`get_disk_list`、`get_fix_status`；只读取原厂已有状态，不调用任何 refresh、挂卸载或 SMART 自检接口。UPS 的已连接 NUT 缓存可作为第二批候选。其他条目先留在注册表，按实际功能需求完成 handler 和副作用审计后再讨论接入。

本文件和 JSON 是分析资料，没有新增运行时路由、硬件写功能或通用命令执行能力。完整参数未审计的地方明确保留未知；任何缓存空缺、服务不活跃或身份不符，都应返回不可用原因，不能用 0、目标值或历史快照填成当前读数。
