# 海康用户设置的存储名称

Build_260907 固件有独立的用户命名机制，文件系统卷标、`/drives` 目录名与它不是同一个字段。2026-10-29 静态分析确认，新版改名操作实际修改存储空间名称；基本盘可以对应单块硬盘，RAID 的名称属于整个空间。

## 当前固件的入口

`emhistor` 的 `rest_set_disk_nickname`（`0x249c6f`）读取空间 ID，调用 `rename_space`（`0xa9f768`），通过 `/var/tmp/disk` 转发 `space_rename_new`。参数包括 `space_id`、`new_name`、`is_raid`、`port` 和 `group`。这是写操作，Toolbox 读取名称不调用此接口。

`disk_space` 的处理链为 `u_rename_space`（`0x4b53d`）→ `rest_rename_space`（`0x47d24`）→ `space_rename`（`0x3ddea`）。服务把当前空间信息保存在固定的 SQLite 文件 `/tmp/nand_config/disk_space.db`，`init_inner_disk_info`（`0x3cbc3`）从 `space_table` 恢复。关联字段如下：

| 字段 | 静态确认的语义 |
|---|---|
| `name` | 用户设置的存储空间名称 |
| `type` | 空间类型；`basic` 为基本盘 |
| `disk_id` | 原厂硬盘序列号；RAID 为冒号分隔的成员序列号串 |
| `member` | 端口号；RAID 为冒号分隔的端口号串，不能按它推导 `sda`／`sdb` |
| `id` | 空间 ID，不是物理盘编号 |

`printf_disk_list`（`0x305c1`）在 `0x308aa` 将物理盘结构 `+0x16c` 打印为 `disk_serial`。`update_all_space_info`（`0x37d58`）在 `0x38309` 将同一字段写入 `disk_id`；RAID 分支在 `0x385b6` 追加冒号与下一成员序列号。`init_inner_disk_info` 在 `0x3d6d8` 取当前物理盘序列号，与数据库第七列 `disk_id` 比较。这条关联不依赖 Linux 设备枚举顺序。

地址均为该固件 PIE ELF 的链接虚拟地址，运行时另加装载基址。分析只读取本地解包文件，没有执行厂商 ELF。

## Toolbox 的读取与展示

名称在硬盘后台采集任务中读取，网页和小屏只消费缓存，不为显示名称发 SMART 请求、打开块设备或唤醒磁盘。数据库使用固定路径、固定 `SELECT`、SQLite 只读模式与 `query_only`，限制文件大小、行数、字段长度和执行时间；不存在时不创建，损坏或不可读时不修复。

SQLite 同时启用 `immutable`，避免在原厂目录创建或修改 WAL／SHM；存在非空 `-wal` 或 `-journal` 时先标为不可确认，等待原厂完成事务，不能忽略未合并内容而显示旧名称。数据库在采集期间变化也不采用本轮结果。原厂默认名称可能为空、`NULL` 或控制字符哨兵，这类记录保持未知，不妨碍其他硬盘的有效名称；同一序列号同时出现有效名称和未知记录时按冲突处理。

只有当前物理盘的真实序列号唯一、基本盘记录只有一个成员、名称没有冲突且序列号严格相等时，才输出 `user_name`。只去除序列号首尾空白，不猜前缀、不按型号、卷标、端口或 `sdX` 顺序匹配。网页和小屏优先使用此名称；条件不满足时保留原有物理盘标识及网页卷标回退。

RAID 成员可以关联 `space_names`，该字段表示“所属空间”，不写入单盘 `user_name`。缺失、重复、冲突或不支持的元数据保持未知，不能借此猜出盘位或唯一昵称。

## 旧版昵称配置

旧 `/tmp/nand_config/config.txt` 顶层存在 `disk_nickname` 数组，每项含 `userid`、`diskuuid`、`nickname`。`cnf_load`（`0x7afc86`）调用 `json_conf_load("disk_nickname")`；`cnf_update`（`0x7aff68`）持久保存相同结构。新版设置入口已经转向空间改名，因此不把旧数组作为当前名称的回退，也不向网页公开整份原厂配置。

## 实机验证边界

本轮已确认固件路径、字段与关联方法，并以临时 SQLite 和 sysfs 树验证只读采集。当前旧部署只返回卷标和挂载目录，尚不能据此确认 NAS 数据库中的实际用户名称。部署新镜像后，应核对网页存储页及小屏存储／温度页的名称是否与海康管理页一致；无法严格匹配的磁盘继续显示原标识。实机序列号格式、旧磁盘元数据和 RAID 场景不能只凭静态分析宣称全部通过。
