# 原厂业务 MCP 第二批固定能力

本页补充三十六工具之后的原厂业务执行器。工具定义统一放在无依赖的 `runtime/native_capability_specs.py`，实现为 `runtime/vendor_native_capabilities.py` 的 `NativeCapabilities.execute(name, arguments, *, principal, authorizer)`。中央目录、角色和功能域须使用同一份定义；模块不会自行注册未实现工具。

本次仅做离线固件审计和本地 fixture，不向 NAS 安装应用、修改名称、切换识别、删除相册或控制备份。下面的“已实现”指有真实固定执行器及自动测试，不代表已经完成 NAS 写入验收。

## 已实现十四项

| 工具 | 最低角色／功能域 | 参数与实际效果 |
| --- | --- | --- |
| `list_native_applications` | 读写／`apps` | 固定 `get_apply_list`＋`opttype=0`，返回原厂 `apply_id`、名称、版本、原始安装状态；不把目录当完整已装或运行清单，不输出来源 URL、`vid`、路径 |
| `get_application_install_progress` | 管理员／`apps` | `application_id`＋确认／幂等键；先核对固定所有者目录，再调用原厂 `get_install_progress`。此动作会同步插件、修改完成状态并写缓存，属于管理操作。只保留原厂 `size/total_size/status` 等数值，不编造百分比 |
| `list_backup_tasks` | 读写／`backup` | 固定 `get_backup_task_list`，只接受当前管理员归属，投影运行／等待／完成组、原始状态、字节量和内部任务版本，不返回来源、目标、磁盘身份或动作原文。原厂缺失的组保持 `null`，不补零 |
| `get_auto_backup_settings` | 读写／`backup` | 固定 `get_auto_backup`，投影 `udisk_a_status/sd_status/udisk_a_type/sd_type` 原始整数，缺失为 `null`，不猜枚举意义 |
| `disable_auto_backup` | 管理员／`backup` | `source=usb/sd`＋确认／幂等键；仅发送固定 `status=0`，禁止开启新导入或提供任意路径。原厂同时可能清理该来源失败任务和停止既有导入；不是单纯保存 UI 复选框 |
| `pause_backup_task` | 管理员／`backup` | `task_id/expected_version`＋确认／幂等键；只对当前所有者目录中的既有运行任务发送原厂固定状态 `2` |
| `cancel_backup_task` | 管理员／`backup` | 同上，只对既有运行／等待任务发送固定状态 `1`。取消任务，不提供文件删除选项；原厂回执不证明工作线程已经退出 |
| `list_nas_users` | 管理员／`system` | 固定 `look_all_user`，仅投影 `user_id/user_name/nickname`；不输出云账号、邮箱、头像、权限原文、口令、会话或用量明细 |
| `get_nas_name` | 管理员／`system` | 固定 `get_dev_name`，只返回名称，设备 SN 留在宿主原文中 |
| `set_nas_name` | 管理员／`system` | `name`＋确认／幂等键；限制六十三个 UTF-8 字节，原厂通知云端后更新 `dev_info`。不接受 SN／账号／原厂参数，回执不等于云端传播完成 |
| `get_nas_ai_settings` | 管理员／`system` | 固定 `get_ai_switch`，仅原始值 `0/1` 映射开关，其余合法枚举 `enabled:null` |
| `set_nas_ai_enabled` | 管理员／`system` | `enabled` 布尔值＋确认／幂等键；映射原厂 **`mode=0/1`**，写 `ai_config.ai_switch` 并通知识别线程。可能启动原厂既有后台识别，不提供照片／人脸／文件内容读取 |
| `get_movie_library_scan_status` | 读写／`movie` | 明确 `library_id`，固定 POST `lib_scan_status`，仅投影 `status/scaned/total/timestamp/num` 原始数字，不启动扫描，不返回路径或推算进度 |
| `delete_album` | 读写／`albums` | `album_id`＋确认／幂等键；先核对当前所有者目录，只接受 `type=custom` 且 ID 大于七的普通自建相册，不允许保留／共享相册。原厂删除相册表、索引归属和相关共享任务元数据，异步清理 ACK 不表示已经完成 |

所有十四项原厂请求都可能通过共同认证初始化所有者资源，因此没有 `readOnlyHint` 或幂等查询承诺，最低角色也不降为只读。原有五项脱敏业务缓存保持被动只读，不会因为这些新工具而自动联网刷新。

“暂停／取消备份”和“关闭自动导入”只收紧既有原厂任务，不提供恢复、开启、创建任务或变更目的地。这些新增目标操作需要先证明真实 Users 根及 ACL；不能沿用本页的停用权限扩大为任意备份文件操作。AI 识别开关属于管理员对原厂后台功能的配置，不授予模型照片内容访问。

## 固件证据

样本为用户提供的 `R1_X01_GML_STD_V6.3.3_Build_260907.zip`。`/histor/bin/emhistor` SHA-256 为 `402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26`。下表地址均为样本 ELF 链接地址，只用于离线审计，不在运行时调用函数地址。没有执行厂商 ELF。

- **应用目录／进度**：`emhistor` 的 `rest_get_apply_list@0x22abc4`、`rest_get_install_progress@0x22b68e` 分别转发固定原厂 `appcenter`。独立 `appcenter` 的 `getListJsonBuf@0x176e2` 在 `0x177b4/0x177d8/0x1789b/0x178f0` 输出 `module_name/apply_id/version/status`。`get_install_progress_request@0x4aff` 在 `0x4cb8` 读取 POST JSON `apply_id`；`0x5005` 附近同步插件，`0x504d–0x506c` 写缓存。`0x5776–0x57db` 附近输出 `size/total_size/apply_opt`，多个完成／失败分支输出状态。不能把该 handler 当纯进度 GET。
- **备份目录与归属**：`rest_get_backup_task_list@0x190dfe` 以当前用户资源调用 `task_db_glance_backup@0x54022a`，分别生成 `run_list/wait_list/done_list`。序列化器 `0x1905b3` 在 `0x1908ac/0x1908d9/0x19096c` 输出整数 `task_id/task_status/user`，`0x190b99` 输出 `total_size`。额外的 `from/to/atid/disk_uuid` 只参与宿主内版本摘要，永不作为返回字段。
- **备份暂停／取消**：分派器 `0x15d60d` 将 `task_id/task_status` 放入固定结构 `+0x18/+0x30`。handler `0x18ee48` 在 `0x18efbd/0x18efd1` 解析 ID 与状态，`0x18efe5` 调 `set_task_status_v1@0x52b191`。后者 `0x52b3f5–0x52b40a` 将匹配活动工作者设为 `2`，`0x52b45a–0x52b4bb` 将取消设为 `1`，必要时唤醒已暂停的线程；两条原厂工作者链均有对应分支。状态 `0` 会恢复并触发任务，不在本执行器白名单中。原厂 handler 本身不接收 UID，Toolbox 因此额外核对固定当前用户目录及任务版本，拒绝任意外来任务 ID。
- **自动导入配置**：`rest_get_auto_backup@0x22e600` 从 `0x5b96e5/0x5b9750` 读取现有设置，构造四个固定原始字段。`rest_set_auto_backup@0x22e1d9` 读取 `disk_type/status/type`，`udisk_a/sd` 分别固定原厂来源 `2/1`；`0x22e3c7` 调 `fs_backup_set_arg@0x5b97bb`。该函数依据状态和类型位修改全局导入设置，状态 `1` 的分支能创建／扫描任务，而 `0` 进入停用、失败任务清理链。因此执行器仅允许明确关闭，不把开启备份混入设置保存。
- **用户目录**：`rest_look_all_user@0x27aa96` 构造 `user_list`，`0x27ac84/0x27acb0/0x27acf2` 等位置输出 `user_id/user_name/nickname`。后续 `account_id/account_name/account_email/permit/icon` 都不是本次的投影字段。
- **设备名称**：登录分派器 `0x1652b3` 的 `0x1655e7/0x16561c` 读取可选 `serial_number` 和 `name`。`rest_set_dev_name@0x27f804` 只强制名称存在；`0x27f91d–0x27f946` 仅在 SN 参数存在时比较，本执行器不发送 SN。`0x27fa33` 调 `client_json_post_devname`，随后 `0x27fb26–0x27fb64` 更新 `dev_info.dev_name` 与修改时间。`rest_get_dev_name@0x27fc71` 的 `0x280116` 输出 `name`，其余设备身份被投影丢弃。
- **AI 识别**：配置分派器 `0x1617ed` 在 `0x161af4–0x161b08` 将 **`mode`** 写入结构 `+0x1f0`；不能错误地发送 `status`。`rest_set_ai_switch@0x23c3fa` 从该位置取值并在 `0x23c477` 调 `json_conf_set_ai_switch@0x93a9b1`，将整数写入 `ai_config.ai_switch`，`0x23c4e1` 通知识别执行链 `0x713103`。getter `0x23c4ec` 返回原始 `status`，这与 setter 参数名称不同。
- **影视扫描状态**：`rest_scraper_lib_scan_status@0x28d429` 固定下游 `refresh_status`，必须 JSON body，`0x28d70c` 转换指定 `lid`；`0x28de44` 起构造顶层 `code/status/scaned/total/timestamp/num?`。本工具不调用写扫描 handler `0x28ca8c`。
- **普通相册删除**：`rest_delete_album_user_v1@0x1f9bde` 在当前用户资源中调用 `album_photo_delete_album_user@0x4cf918`。后者重命名／删除该相册表及 frame/dmsg/read 索引、删除 `album_list` 条目；handler 在 `0x1f9f3a` 发 ACK，随后取消相关共享任务并创建 `s_rest_delete_album_pthread@0x1f974e` 清理线程。线程调用 `album_db_delete_photo_belong_user@0x4537c4`，以 `UPDATE ... belong ... WHERE path=...` 清理照片与相册索引归属，而不是提供用户文件删除参数。由于原厂共享和保留类型的链路不同，本执行器额外要求目录明确证明普通 `custom` 和 ID 大于七，不把“数值 ID 合法”当所有类型都能删除的证明。

## 授权、传输和幂等

固定传输复用 `VendorBusinessClient`：宿主回环 `20443`、唯一 emhistor PID／启动时刻／监听 inode、固定叶证书 pin、当前固定所有者既有会话。每次发送前重新核验当前授权，真实响应后再核验授权；角色、精确工具、功能域、Key 修订或凭据代际变化后丢弃结果。证书、令牌、签名、原文和固定身份参数不出宿主。不要以 API Token 自行管理开发白名单、服务开关或机器凭据。

一个普通工具最多一次固定请求；删除相册、应用进度和备份停用操作最多一次固定所有者目录预检加一次固定写请求。模块锁等待最长十秒，每次网络请求共享自身十秒绝对截止时间；不做自动重试或无限等待。两请求工具最长包含三个有界窗口。最终发请求前检查的是外层原工具权限，预检不会悄悄授予独立列表工具权限。暂停与取消共享同一原厂 `backup_task_halt` 标识，授权器必须绑定外层工具，不能从原厂动作一对一反推权限。

七个显式写工具均需 `confirmed:true` 和本 Key 的 `idempotency_key`。进程内记录绑定 `key_id/grant_revision/credential_generation` 及规范化请求摘要，重复键不重放原厂写请求，不同请求复用键拒绝；存储满时拒绝新写而不淘汰未知结果。明确未发送才释放幂等记录；可能发送但没有可信回执或写后撤权时保留 `mutation_outcome:unknown`，返回 `result_unconfirmed`。原厂 `code=0` 仅返回 `factory_acknowledgement_only`，不编造异步完成和任务百分比。

应用进度查询本身是一次管理写操作。同一幂等键再次调用取得原记录；用户确实要再次观察新状态时应明确使用新幂等键，而不是轮询后台自动更换键。重启后内存记录消失，不能据此认定旧写没有执行，应先核对实际状态。

## 固定开发诊断与未闭合能力

`collect_native_capabilities_probe` 固定运行六项：应用目录、备份目录、自动备份设置、用户目录、设备名称、AI 设置。开发入口必须显式确认、核验真实连接来源的白名单及绝对开发窗口；返回每项独立脱敏结果与固定待接入原因。它不创建测试对象、不调用安装进度或任一 setter／任务控制／相册删除，不加入默认四项被动诊断。正常原厂认证可能初始化用户资源或唤盘，结果明确 `native_query_may_update_state:true/may_wake_disks:true`。

仍不注册下列假执行器：

- 应用安装／卸载／更新／通用启停。原厂安装器接收目录字段、应用 ID 和固定操作枚举，`source` 在本固件中是整数，不是包 URL。原厂目录条目加确认、幂等可以作为后续固定安装工具的输入设计；本批尚未闭合目录条目与实际模块包身份、完整异步任务及卸载效果，故不注册写回调。不要求额外的外部独立签名，也不据现有证据宣称原厂有或没有签名验证；禁止开放任意包 URL／安装脚本代理。
- 共享创建与相册成员变更。须核实服务端分享 ID／外链、共享相册归属及成员邀请枚举，不本地伪造分享链接或创建真实样本。
- 相册文件、影视来源／播放／字幕、备份新目标和恢复。须与真实 `/drives/*/Users/` 根、原厂 ACL 和最终文件句柄绑定；不能通过原厂媒体路径绕开文件能力，也不能用原厂目录规划摘要冒充最终文件打开权限。
- 用户创建、删除、重置口令和权限修改。需逐对象账号权限、所有者保护及凭据传输契约，不把已审计只读用户目录当通用用户管理桥。

应用包契约 collector `collect_application_package_context` 固定一次 `get_apply_list/opttype=0`，输出 schema v2。它一次覆盖应用 ID／名称／版本、固定字段形状、`module_type/source/status/guest/apply_opt/upgradable/support_terminal/support_ecloud` 的实际整数码；未审计的枚举只标记 `native_enum_unmapped`，布尔、超界或缺失码保持 `null`。可选 `download_url/package_url/url` 仅输出协议、主机类别及是否有凭据／查询／片段／端口，不返回主机、地址、路径或参数；可选 `md5/sha1/sha256/sha512/checksum/hash` 仅输出算法字段名、字符数、十六进制格式与长度是否相符，不能据长度证明算法或包校验通过。`vid/path/paramList/signature` 等只返回类型与字节数。始终明确 `trusted_package_manifest_verified:false/package_execution_available:false`，未出现的字段保持缺失，不补造下载地址、摘要或签名。入口为显式确认的 `POST /api/dev/native`，`{"action":"packages","confirm":true}`；不加入被动业务轮询。

应用安装链的本批静态核对记录如下，均为同一 Build 260907 的 ELF 链接地址，未运行厂商 ELF：

- `emhistor` 的 `rest_install_apply@0x22aeac`／`rest_remove_apply@0x22b29d` 转发至原厂 `appcenter`，不代表 Toolbox 可绕过现有固定认证直接访问该内部服务。
- `appcenter getListJsonBuf@0x176e2` 在 `0x1784c/0x17873` 序列化整数 `module_type/source`；`vid/version` 是字符串。不能将 `source` 猜成 URL，或将处理器名称猜成其整数码标签。
- `install_apply_request@0x3543` 在 `0x380c` 取 `flag`，分支只接受 `0` 安装、`1` 更新；读取 `apply_id/module_name/vid/module_type/source/version/size` 等目录参数。`0x46a6` 调用队列函数 `0xa816`，后者经互斥队列和 `sem_post@0xa8fc` 唤醒真实异步工作线程；ACK 不是安装完成。
- `install_apply_task@0x95ae` 调用 `general_install@0x155ed`，固定处理器 `zc_self_developed` 查函数表到 `zc_install@0x1467b`。`remove_apply_task@0x9e33`／`general_remove@0x1574a` 同样进入 `zc_remove@0x1499e`。处理器选择与目录 `source` 的整数枚举不可混为一谈。
- `get_short_conn_respons@0x1015c` 固定连接原厂 `127.0.0.1:80`；安装／更新分支将 `applycenter/install_apply` 改为 `update/modulize_set_oper`，构造固定 `id/opt=install|update/data_clr=1` 查询，删除分支使用 `opt=remove`。该内部再委派说明包来源与验证可能位于下游，不证明 URL 或校验字段存在。本批止于此固定调用链，未将未审计的模块包解析、校验、实际清理或原厂重试规则当成已实现能力。

诊断 HTTP fixture 另覆盖默认四项不触发新探测、`probe/packages` 的严格确认、真实 peer 与绝对白名单窗口、同源／CSRF／重复头／查询参数拒绝、共享锁及窗口中途关闭后结果丢弃；文件协议检查仅显式选择 `file_protocol` 时调用。所有这些是本地真实 HTTP 验证，不等于 NAS 原厂安装或文件访问验收。

验证覆盖真实本机 HTTP server 的固定 route／method／query／JSON body、目录归属、未知值、进度不造假、写后撤权、旧键幂等与不确定结果。真实 TLS、进程／socket、签名读取的攻击面沿用固定传输定向测试；本机 HTTP fixture 使用仅构造器可注入的测试连接，不修改生产固定端口。NAS 写入验收仍需用户指定明确对象，先核对只查诊断再验收单项写入。
