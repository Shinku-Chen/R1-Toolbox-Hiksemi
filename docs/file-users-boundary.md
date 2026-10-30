# MCP 文件的 admin 总边界

文件数据授权的最外层范围固定为 `/drives/*/Users/admin/`。`*` 恰好是一个已核验直接数据盘挂载的名称；`Users`、`admin` 均区分大小写。Token 只能进一步收窄该根的相对目录，不能选择整个数据盘、Users、public、其他用户或任意路径。

例如，数据盘为 `/drives/space_1` 时，公开根为 `/drives/space_1/Users/admin`。`path:""` 表示该 admin 目录；`path:"临时共享/FWA229P.bin"` 对应该目录内的文件，不再写 `admin/临时共享/FWA229P.bin`。角色、功能域、精确工具、当前根和文件范围共同授权。

## 根规划与旧授权

`runtime/api_file_roots.py` 仅读取完整有界的内核 mountinfo 以及既有磁盘身份缓存。`overview(include_identity=True)` 内部复用已验证缓存与受限 sysfs／udev 生命周期元数据；不运行 SMART，不访问、stat 或枚举 Users／admin，不读取用户文件，不调用原厂空间接口。根规划不能证明 admin 目录存在或可读。

根 ID 仍为 `root_<24hex>`，但 `boundary_version` 为 3，摘要命名域为 `r1-file-admin-root-v3`，绑定物理序列号、分区来源、主次设备号、文件系统和精确 admin 根路径。改标签不换 ID；换盘、分区、来源或路径使旧绑定失效。重启后的 mount ID 不进入持久授权摘要，实际操作仍逐次核验当前 mount ID。

旧整盘根和 Users v2 根均不会按磁盘标签自动改绑，也不会自动删除相对路径开头的 `admin/`。旧 Token 的名称、来源、启停等元数据可以继续编辑，旧文件范围显示绑定不可用。所有者必须显式重新选择 v3 根并填写 admin 内相对目录；其他工具与资源权限不因此增加。

内部恢复区固定在 `Users/admin/.r1-toolbox-ops`，用户文件与保管对象均在 admin 内。此子树由所有者恢复界面专用，普通列表隐藏入口，直接文件读取、下载、复制来源、写入与范围新增均拒绝；admin 整根授权也不例外。新布局使用独立登记与显式标记，旧 Users 位置的登记与对象只读保留，不自动接管或转换；有旧恢复对象的设备须先用原镜像处理，再使用新布局。原厂固定 Users `get_info` 仅用于内部 ACL 前置证明，不提供 Users 枚举、public 或其他用户的文件访问。完整协议见[文件变更与恢复](file-mutations.md)。

该根级私有名字的大小写变体同样保留；文件后端还以真实句柄拒绝指向同一恢复区 inode 的名称别名，不能仅靠字符串和目录权限隔离宿主 root。已有 Token 的私有子树范围保持原记录，不改写成整根授权；机器能力判定与实际访问拒绝该范围，所有者可继续仅编辑密钥元数据。

## 子挂载与真实访问

根下存在额外挂载不再整体排除 admin 根。包括 SSD 的 overlay 子挂载在内，规划仍保留脱敏现场，但不能仅凭这些子挂载的存在宣称 admin 内所有普通文件不可访问。

实际打开继续从固定宿主根以目录句柄逐级执行 O_PATH／NOFOLLOW，核验原物理分区的设备号和 mount ID：

- 访问未跨挂载的普通 admin 后代，可以继续经过原厂 ACL、对象、版本和 Token 授权校验。
- 进入任何子挂载继续拒绝，包括设备号相同的 bind。不能把设备号相同当作未跨挂载。
- Users 或 admin 本身被另一个挂载覆盖时，真实打开仍按 mount ID 拒绝；规划成功不等于目录就绪。
- 列目录跳过不能安全打开的子挂载入口，直接请求该入口返回明确拒绝；普通兄弟文件不因另一入口存在而整页失败。
- 符号链接、非常规对象、硬链接以及对象／挂载变化的原有拒绝规则保持。

以上规则允许根有子挂载，未授予访问容器合并文件系统、其他分区或挂载别名的能力。

## 继续拒绝的规划条件

没有唯一缓存身份、同序列号对应多个设备、不支持的物理来源／文件系统、盘根堆叠、同源或同设备的多个完整规范盘根、格式损坏或不完整的 mountinfo，以及缓存关联无法核对时，继续不生成授权根。

同物理盘的不同分区可以各自登记，只要来源和设备号唯一并与缓存一致。`/home`、`/mnt` 等规范 `/drives/<名称>` 外的别名不成为额外授权根，也不误伤已核验的唯一规范根。只读挂载与超级块分别校验，不能授予写入。

所有相对路径均为未经解码的 POSIX 字面量，不能包含绝对路径、反斜杠、空组件、`.`、`..`、控制或代理字符；上限为 1024 个 Unicode 字符及 4096 个 UTF-8 字节。`%2e%2e` 不在后续层再次解码。这些词法检查不代替 ACL 与真实句柄。

## 能力和验收

文件读取、下载及受限写执行器已经接入；根规划、定义目录和 `GET /api/access/file-readiness` 不授执行权。实际读操作要求匹配固件、固定原厂所有者的完整逻辑许可、当前对象及每块持续授权。写操作另须 SATA、父目录范围、配额、索引、无覆盖发布和必要恢复区证据。

公开文件根和私有恢复区均限定在 admin。恢复区由 Web 所有者显式预检与初始化，保留完整可信祖先、权限和 ACL 要求，不修改原厂目录权限。预检明确给出 admin 根锚和恢复区位置，上级 /drives、Users 的权限仍可能阻断初始化。因此既有 NAS 的恢复区预检失败不能因新的根规划成功而自动变成通过，也不能开始原先被阻断的写验收。

```bash
python -B -m unittest discover -s tests -p test_api_file_roots.py -v
python -B -m unittest discover -s tests -p test_api_file_root_topology.py -v
python -B -m unittest discover -s tests -p test_api_file_executor.py -v
```

Linux 的隔离 runner 另以 root 执行真实挂载检查：

```bash
sudo "$(command -v python)" -B -m unittest discover -s tests -p test_admin_root_mount_boundary.py -v
```

四项分别覆盖同设备 bind 子挂载、overlay 子挂载，以及 Users、admin 本体的 bind 覆盖。子挂载在保留旧 mountinfo 与刷新 mountinfo 两个阶段检查真实 FD mount ID；CI 必须实际执行全部四项且零跳过。夹具反序正常卸载并确认无残留挂载后才删除临时目录，失败保留现场。

回归须覆盖精确 admin 边界、旧 ID 失效、根下存在子挂载而普通兄弟路径可用、实际同设备 bind／overlay 跨入拒绝、链接／挂载变化、短读完整性和脱敏。隔离测试不代表 NAS 字节、索引或恢复验收已经完成。

### 2026-10-30：7fcd12b 实机只读核对

SSD 的 `/drives/BSJQZGLBMYA/Users/admin` 已进入 v3 目录，真实目录句柄验证通过；原有 14 个 overlay 子挂载仍作为拓扑证据保留，不再整体排除该根。原厂固定 Users `get_info`、admin `get_info/get_list` 成功，物理分区、Users 和 admin 的句柄同属 mount ID 539、设备号 `8:49`。

仅对所有者指定的 `临时共享/FWA229P.bin` 创建临时只读授权，文件信息返回 72,690,236 字节，下载准备及 HEAD 返回成功。独立 Range 请求返回 `206`、`bytes 0-1023/72690236`，实际接收 1024 字节；这只证明该段字节读取，不是整文件校验或持续稳定性验收。过程中出现过一次 `native_unavailable` 和一次 Range `503`；固定协议检查中另一个根的一项查询为 `identity_unverified`，SSD 三项查询成功。小范围复核通过，原失败记录保留，不据此断言这些错误具有相同根因或瞬态原因已定位。

本轮三枚临时 Token 均已撤销，并以原 Token 请求复核 `401`；Token 原文、原厂会话和文件字节未写入验收日志。没有变更原文件或初始化恢复区。SSD 恢复预检仍为 `ready:false`：`/drives` 的 UID 1046／0775 和 Users 的 0777 未通过可信祖先规则，故没有开始写入验收。
