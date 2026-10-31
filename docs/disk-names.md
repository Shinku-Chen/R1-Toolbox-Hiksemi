# 海康用户设置的存储名称

Build_260907 的当前用户名称应从原厂 `disk_space` 进程的实时空间列表读取。文件系统卷标、`/drives` 目录名及旧 SQLite 数据库不能代替该来源。

2026-10-29 复核纠正了此前的判断：`disk_space.db` 是旧格式迁移入口；迁移后变成 `disk_space_cache.db`，后者不随用户后续改名更新。当前实现已改为固定只读 socket 查询，并与已有 SMART 缓存核对物理身份；是否取得正确名称必须以部署后的实机结果为准。

## 设备昵称与主机名

海康设置的设备昵称来自 `/tmp/nand_config/config.txt` 的 `dev_info.dev_name`。原厂 `json_conf_get_dev_name`（`0x936200`）调用 `json_conf_load("dev_info")` 后读取该字段；REST 注册动作 `get_dev_name`（handler `0x27fc71`）也把它作为 `name` 返回。它与 `/proc/sys/kernel/hostname` 是两个独立字段。

`runtime/vendor_device.py` 只读这个固定配置文件，只返回名称，不回传设备序列号、账号或整份配置。文件须为受限大小的普通文件，拒绝符号链接、重复 JSON 键、读取期间替换和无效名称；未知时保留 `null`。该读取随原有后台设备采集刷新，网页和小屏请求只使用缓存。

- `/api/r1.device.name`、`/api/overview.system.device_name`：原厂设备昵称。
- 对应 `hostname`：保留真实 Linux 主机名，不用昵称覆盖。
- 小屏首页优先显示昵称，网页“关于”分别展示昵称与主机名。

## 当前名称的只读入口

- 宿主 Unix socket：`/var/tmp/disk`，类型为 `AF_UNIX/SOCK_STREAM`。
- 固定请求原始字节：`get_space_list;`，参数为空，不加 `emhistor;` 前缀，原厂客户端不主动发送 NUL。
- 响应：UTF-8 JSON，末尾一个 NUL，随后服务端关闭连接。原厂缓冲上限为 131072 字节，JSON 最多 131071 字节；错误也可能返回 `-1`，不得把非 JSON、截断或缺少完整结束标记的返回当作有效数据。
- 这是宿主本机 IPC，调用方必须具有 socket 的正常访问权限，不能直接转为接受任意命令的网页接口。原厂 REST 的实际路由、监听及认证要求本轮没有完整验证。

读取链为 `u_disk_get_space_list`（`0x49ca0`）→ `rest_get_space_list`（`0x42123`）→ `malloc_space_list`（`0x37922`）。最后一个函数加锁后复制全局空间结构和成员链表，再解锁。此链没有温度刷新、SMART、块设备读取或持久化写入；正常 IPC 和服务日志仍可能发生。

字段示意如下，不是从实机读取的名称：

```json
{
  "info": [
    {
      "space_name": "用户设置的空间名称",
      "space_disk_id": "原厂保存的序列号集合",
      "space_type": 2,
      "is_raid": 0,
      "port": 1,
      "member": [
        {
          "port": 1,
          "disk_name": "sda2",
          "root_name": "sda",
          "disk_id": "物理盘序列号"
        }
      ]
    }
  ]
}
```

基本单盘按 `is_raid == 0` 且只有一个有效成员判断。`space_type` 存在 1／2／3，描述不同的元数据处理路径，不是 `basic`／RAID 枚举，不能据此猜空间类型。RAID 名称只表示整个存储空间，不作为每个成员的单盘昵称。

同一物理盘可以有多条基本空间记录，例如原厂分别列出 `sda1` 和 `sda2`。在序列号、整盘名与端口均已严格核验后，只有不同 zone 上的名称完全一致且都有效，才收敛为一个物理盘昵称。同一个 zone 重复、名称冲突或任一记录缺名，仍保持未知；不能直接对名称去重后忽略身份或重复记录。

## 与物理硬盘的关联

同一 socket 的另一条只读缓存请求为 `get_disk_list;`，返回 `info[].disk_name`、`disk_port`、`disk_serial`、`disk_id` 等字段。两个列表里同名的 `disk_id` 语义不同：

| 字段 | 已确认语义 |
|---|---|
| `space.member[].disk_id` | 物理盘序列号，来自原厂物理盘结构 `+0x16c` |
| `space.member[].disk_name` | 分区／zone 名称，来自 zone 结构 `+0x90`，不是整盘名 |
| `space.member[].root_name` | 物理盘名称，来自物理盘结构 `+0x24` |
| `disk_list.info[].disk_serial` | 同一个物理盘序列号字段 |
| `disk_list.info[].disk_name` | 物理盘名称，来自同一个 `+0x24` 字段 |
| `disk_list.info[].disk_id` | 另一字段，来自物理盘结构 `+0x64`，不能当序列号 |
| `space.space_name` | 用户设置的存储空间名称 |

`create_space_node`（`0x3ab47`）在基本盘、RAID 及其他构造分支将物理盘 `+0x16c` 复制到成员 `+0x94`；`rest_get_space_list` 随后把它序列化为成员的 `disk_id`。

接入时先核验成员的 `disk_id == disk_list.disk_serial`、`root_name == disk_list.disk_name == 当前物理盘名`，以及端口对应一致，再按真实序列号严格唯一匹配已有 SMART 缓存。`member.disk_name` 是分区／zone 名，不能与整盘名直接比较；这个含义由 `printf_disk_list`（`0x30a6a`）和 `get_disk_list`（`0x42f81`）对 zone 结构同一字段的访问确认。不能按 `sdX` 顺序、型号、卷标或端口号猜匹配，也不能只靠剥离分区后缀替代真实身份。缺失、重复或冲突保持未知。

已有 SMART 缓存的 `serial_number` 参与严格一致性核验，不为名称额外运行 SMART。`InsightsCollector` 合并 sysfs／udev／缓存 SMART 候选，保留冲突状态；不使用简单的 `sysfs值 or SMART值` 掩盖冲突。缓存必须与当前 sysfs 设备生命周期和物理身份相符，采集前后再次核对，不能跨硬件更换复用同名设备的旧缓存。

已真实取得的序列号还会进入独立身份缓存，绑定同一 sysfs 生命周期；休眠响应不再含序列号时可以继续显示名称。这不延长温度、健康或原始 SMART 的时效。多源冲突、移除或替换后立即丢弃；sysfs 目录身份或修改时间发生变化也会保守失效，不能把无法确认连续性的旧值认作当前磁盘。

适配器沿用 `runtime/histor.py` 的私有具名客户端、`0700` 权限目录、超时、返回大小与 NUL 校验及仅清理自己 inode 的约束，单独固定上述两个 getter。现有风扇 RPC 白名单没有放开。名称会同步用于存储列表、磁盘详情以及温控／屏保测点标签；重命名不会改变温度测点的物理标识。

## 旧数据库为什么不存在

旧 `/tmp/nand_config/disk_space.db` 的 `space_table` 有 `type`、`name`、`member`、`disk_id` 等字段。旧 `type == "basic"` 和序列号规则只适用于这份迁移数据，不能套用到实时列表的字段枚举。

| 阶段 | 固件证据 |
|---|---|
| 旧数据迁移 | `init_inner_disk_info`（`0x3cbc3`）检查旧数据库；不存在时走“不需要适配”分支 |
| 写入新格式 | 将旧记录的名称等信息交给 `disk_dev_inner_info_refresh`；基本盘调用点 `0x3d997`，RAID 调用点 `0x3d5c3` |
| 保留迁移快照 | `0x3da71–0x3da7f` 调用 `rename("/tmp/nand_config/disk_space.db", "/tmp/nand_config/disk_space_cache.db")` |
| 建立当前列表 | `main`（`0x53780`）先迁移，再执行 `update_all_space_info` 和 `init_global_space_list` |
| 用户后续改名 | `space_rename`（`0x3ddea`）通过 `disk_dev_inner_info_get`（`0x330c8`）和 `disk_dev_inner_info_refresh`（`0x33ad0`）更新私有元数据及内存状态，不更新旧数据库 |

新版名称持久化涉及物理盘私有元数据。Toolbox 应读原厂服务提供的现成列表，不为名称打开原始分区或解析扇区；也不能把迁移快照作为当前名称的回退。

`restart_disk_space_v1.sh` 与另一启动器都运行同一个 `/histor/bin/disk_space`，主要区别是进程枚举使用 `ps -aux`／`ps`，没有 `chroot`、`unshare` 或 `nsenter`。`nand_opt.sh` 把 `/dev/mmcblk0p6` 以 ext4 挂载到 `/tmp/nand_config`；本轮没有发现该服务另设配置命名空间的证据。

## 旧昵称配置与写入口

旧 `/tmp/nand_config/config.txt` 顶层还有 `disk_nickname` 数组，含 `userid`、`diskuuid`、`nickname`。新版改名已经转向空间私有元数据，不能把这份旧数组当作当前名称，也不应回传整份配置。

写链为 `rest_set_disk_nickname`（`0x249c6f`）→ `rename_space`（`0xa9f768`）→ `/var/tmp/disk` 的 `space_rename_new`。查询名称不调用这条链。

## 实机证据与验证边界

2026-10-29 在 `sha-9c2495b087a427822199752b4ee21077de82a360` 上只读核对：

- 四块数据盘 `sda`／`sdb`／`sdc`／`sdd` 的 `user_name` 都为空，名称采集报告旧数据库不存在。
- 网页回退卷标依次是 `NAS-DISK2`、`NAS-DISK3`、`NAS-DISK1`、`NAS-DISK3`；重名不能证明物理身份或海康自定义名称。
- 四盘 sysfs／udev 采集的序列号为空，当时已有的后台 SMART 原始缓存则都含真实 `serial_number`。
- 原厂 `disk_space` 正在运行，R1 确实使用 `restart_disk_space_v1.sh`。

上述结果来自改动前的 `9c2495b`。该版本没有存储 getter 调试入口，不能通过它上传或执行任意代码；当前也没有这台 NAS 的已配置 SSH 通道。不能把静态分析或本地 fixture 测试宣称为实机名称已通过。

用户提供的验收参考为设备昵称“小可爱NAS”和四个存储名称“SSD、视频资源、主磁盘、备份磁盘”。它们只用于对照，不写入采集器、不用于推导名称与 `sdX` 的对应关系。部署新实现后必须以真实序列号匹配结果核对各盘。

2026-10-29 用户部署 `065abc9` 后，固定只读实机快照已确认设备昵称为“小可爱NAS”，原厂空间列表与本机已有序列号缓存相符：

| 物理盘 | 原厂端口 | 原厂名称 | 返回 zone |
|---|---:|---|---|
| `sdd` | 1 | SSD | `sdd1`、`sdd2` |
| `sdc` | 2 | 主磁盘 | `sdc1`、`sdc2` |
| `sdb` | 3 | 视频资源 | `sdb1`、`sdb2` |
| `sda` | 4 | 备份磁盘 | `sda1`、`sda2` |

实机同时定位到 `065abc9` 的展示缺口：原厂每块盘返回两条同名基本空间，旧投影只允许一条，因而 `user_name` 仍为空。现按上述“不同 zone、一致名称”规则修正，并用脱敏实机快照重放核对四盘映射；原厂读取与身份关联已实测，修正后的网页／小屏展示仍需部署包含修复的新镜像验收。

## 固定只读诊断与实机验收

新增 `POST /api/dev/storage`，唯一允许的 JSON 为 `{"action":"snapshot"}`。沿用开发者总开关、精确客户端 IP、有效时间窗口、`R1_DEV_ALLOW_COMMANDS=1`、正常登录口令会话、同源与 CSRF 校验；它不是公开监控接口，不接受路径、命令、序列号覆盖或上传代码。

```javascript
const response = await fetch('/api/dev/storage', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-FNM-Request': '1'},
  body: JSON.stringify({action: 'snapshot'})
});
console.log(await response.json());
```

诊断只读原厂两份当前列表，以及已有 SMART 缓存和 sysfs／udev 身份。不会调用 `_smart`、触发 `_disks` 刷新或额外运行 `smartctl`。响应仅保留名称、物理盘／分区标识、端口和关联状态，不回传挂载路径或其他无关字段，序列号仅返回哈希；同时列出来源、候选是否冲突、是否匹配及不可用原因。一次只允许一个诊断请求，避免并发占用原厂 IPC。

验收应核对：

1. `device.name` 为实际海康昵称，`device.hostname` 仍是 Linux 主机名。
2. 四块数据盘通过唯一序列号、原厂盘名和端口交叉核验；`user_name` 与海康管理页一致，系统 eMMC 不被误命名。
3. 网页、小屏、温度选择和屏保测点使用同一名称；缺失或冲突回退为未知，不使用卷标猜昵称。
4. 诊断前后没有额外 SMART 命令、主动自检、原厂改名或磁盘私有元数据读写。
5. 未授权 IP、过期窗口、缺口令会话和跨站请求被拒绝；请求不能指定其他原厂命令。

以上函数地址均为用户提供 Build_260907 固件 PIE ELF 的链接地址，运行时需另加装载基址。分析只读取解包文件，没有执行厂商 ELF 或脚本。

## 显式读取完整硬盘序列号

所有者网页的 `/api/overview` 与 `/api/smart?dev=...` 现可显示经过核验的完整 SN。只读取已取得的独立身份缓存并重新检查设备生命周期，后台不再采集 SMART，不调用原厂空间管理或重新运行 SMART；缺失、冲突、换盘和缓存失效仍为 `null`。已有口令时需要网页登录；首次未设口令的 `open` 模式沿用网页授权。Bearer、开发白名单本身、MCP、公开 v1 以及小屏快照不会因此获得完整 SN。数据覆盖和未接入项见[三端数据覆盖](data-coverage.md)。

默认 `storage` 检查和原 `POST /api/dev/storage` 继续只返回序列号摘要。用户明确需要完整序列号时，在已授权的开发窗口使用统一入口 `POST /api/dev/diagnostics`，显式请求：

```json
{"action":"snapshot","checks":["storage_identity"]}
```

结果位于 `snapshot.checks.storage_identity.data.storage.items[]`。`serial` 只在当前 sysfs 生命周期有效、已有身份缓存无冲突且与唯一原厂物理盘记录和端口一致时返回；换盘、缺测或冲突为 `null`。名称仍按原有空间关联规则取得，不用名称反推序列号。默认四项快照不会自动增加此检查，不为它运行 SMART 或打开块设备。来源白名单、到期时间、同源／JSON／CSRF 和存储并发锁与统一诊断相同，完整调用边界见[开发检测](dev-diagnostics.md)。
