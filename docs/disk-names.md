# 海康用户设置的存储名称

Build_260907 的当前用户名称应从原厂 `disk_space` 进程的实时空间列表读取。文件系统卷标、`/drives` 目录名及旧 SQLite 数据库不能代替该来源。

2026-10-29 复核纠正了此前的判断：`disk_space.db` 是旧格式迁移入口；迁移后变成 `disk_space_cache.db`，后者不随用户后续改名更新。当前 Toolbox 仍读取旧数据库，实机尚未成功取得海康自定义名称；本文记录的是已确认的正确读取入口，适配器尚未接入。

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
          "disk_name": "sda",
          "root_name": "原厂分区名称",
          "disk_id": "物理盘序列号"
        }
      ]
    }
  ]
}
```

基本单盘按 `is_raid == 0` 且只有一个有效成员判断。`space_type` 存在 1／2／3，描述不同的元数据处理路径，不是 `basic`／RAID 枚举，不能据此猜空间类型。RAID 名称只表示整个存储空间，不作为每个成员的单盘昵称。

## 与物理硬盘的关联

同一 socket 的另一条只读缓存请求为 `get_disk_list;`，返回 `info[].disk_name`、`disk_port`、`disk_serial`、`disk_id` 等字段。两个列表里同名的 `disk_id` 语义不同：

| 字段 | 已确认语义 |
|---|---|
| `space.member[].disk_id` | 物理盘序列号，来自原厂物理盘结构 `+0x16c` |
| `disk_list.info[].disk_serial` | 同一个物理盘序列号字段 |
| `disk_list.info[].disk_id` | 另一字段，来自物理盘结构 `+0x64`，不能当序列号 |
| `space.space_name` | 用户设置的存储空间名称 |

`create_space_node`（`0x3ab47`）在基本盘、RAID 及其他构造分支将物理盘 `+0x16c` 复制到成员 `+0x94`；`rest_get_space_list` 随后把它序列化为成员的 `disk_id`。

接入时先核验成员的 `disk_id == disk_list.disk_serial`，并检查盘名、端口对应一致，再按真实序列号严格唯一匹配已有 SMART 缓存。不能按 `sdX` 顺序、型号、卷标或端口号猜匹配；同名字段也不能直接互相连接。缺失、重复或冲突保持未知。

已有 SMART 缓存的 `serial_number` 可参与严格一致性核验，不为名称额外运行 SMART。当前 `InsightsCollector._disk_serial` 仅读取 sysfs／udev；需要在接入时合并候选并保留冲突状态，不能用简单的 `sysfs值 or SMART值` 掩盖冲突，也不能跨硬件更换复用无法确认身份的旧缓存。

适配器应沿用 `runtime/histor.py` 已有的私有具名客户端、`0700` 权限目录、超时、返回大小与 NUL 校验及仅清理自己 inode 的约束，单独固定上述两个 getter。现有风扇 RPC 白名单不得放开。

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
- 四盘 sysfs／udev 采集的序列号为空，已有后台 SMART 原始缓存则都含真实 `serial_number`。
- 原厂 `disk_space` 正在运行，R1 确实使用 `restart_disk_space_v1.sh`。

当前已有受限调试接口尚不能发送这两个存储 getter，本轮未在 NAS 上直接调用它们，也未读取或写入私有磁盘元数据。因此已确认正确的固件读取入口和字段关联，但实际返回名称仍需通过固定只读适配器取样，再与海康管理页逐盘核对；不能把静态分析或旧数据库测试宣称为实机名称已通过。

以上函数地址均为用户提供 Build_260907 固件 PIE ELF 的链接地址，运行时需另加装载基址。分析只读取解包文件，没有执行厂商 ELF 或脚本。
