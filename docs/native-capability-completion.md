# 原厂业务 MCP 第二批固定能力

本页补充三十六工具之后的原厂业务执行器。工具定义统一放在无依赖的 `runtime/native_capability_specs.py`，实现为 `runtime/vendor_native_capabilities.py` 的 `NativeCapabilities.execute(name, arguments, *, principal, authorizer)`。中央目录、角色和功能域须使用同一份定义；模块不会自行注册未实现工具。

实现验证包括离线固件审计、本地 fixture，以及下文记录的固定 NAS 查询收据复核；未向 NAS 安装应用、修改名称、切换识别、删除相册或控制备份。下面的“已实现”指有真实固定执行器及自动测试，不代表已经完成 NAS 写入验收。新增模块库存工具的本地验证也不等于已完成该接口的 NAS 验收。

## 已实现十七项

| 工具 | 最低角色／功能域 | 参数与实际效果 |
| --- | --- | --- |
| `list_native_applications` | 读写／`apps` | 固定 `get_apply_list`＋`opttype=0`，返回原厂 `apply_id`、名称、版本、原始安装状态；不把目录当完整已装或运行清单，不输出来源 URL、`vid`、路径 |
| `list_native_modules` | 读写／`apps` | 显式固定 `get_modulize_table`，将 `available_list/installed_list` 分开返回，仅有限模块身份、版本和原始整数状态；原厂可能联系厂商更新支持状态，不输出安装路径或参数，不推断运行，不授予安装或卸载权限 |
| `plan_native_application_install` | 管理员／`apps`／精确 `application_ids` | `application_id`；固定两目录交叉核对，仅预览本键选择的已审计未安装应用，返回 opaque 当前版本及该应用的实际原生委托影响；查询可能更新原厂支持状态，不执行安装 |
| `install_native_application` | 管理员／`apps`／精确 `application_ids` | `application_id/expected_version`＋确认／本键幂等；仅对明确授权的白名单新装应用重新核对两目录后固定 `install_apply/flag=0`，不接收包地址、路径、脚本或 action。原厂 ACK 仅代表受理，不伪造任务 ID、完成或进度 |
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

所有十七项原厂请求都可能通过共同认证初始化所有者资源，因此没有 `readOnlyHint` 或幂等查询承诺，最低角色也不降为只读。原有五项脱敏业务缓存保持被动只读，不会因为这些新工具而自动联网刷新。

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

八个显式写工具均需 `confirmed:true` 和本 Key 的 `idempotency_key`。进程内记录绑定 `key_id/grant_revision/credential_generation` 及规范化请求摘要，重复键不重放原厂写请求，不同请求复用键拒绝；存储满时拒绝新写而不淘汰未知结果。明确未发送才释放幂等记录；可能发送但没有可信回执或写后撤权时保留 `mutation_outcome:unknown`，返回 `result_unconfirmed`。原厂 `code=0` 仅返回 `factory_acknowledgement_only`，不编造异步完成和任务百分比。

应用进度查询本身是一次管理写操作。同一幂等键再次调用取得原记录；用户确实要再次观察新状态时应明确使用新幂等键，而不是轮询后台自动更换键。重启后内存记录消失，不能据此认定旧写没有执行，应先核对实际状态。

## 固定开发诊断与未闭合能力

`collect_native_capabilities_probe` 固定运行六项：应用目录、备份目录、自动备份设置、用户目录、设备名称、AI 设置。开发入口必须显式确认、核验真实连接来源的白名单及绝对开发窗口；返回每项独立脱敏结果与固定待接入原因。它不创建测试对象、不调用安装进度或任一 setter／任务控制／相册删除，不加入默认四项被动诊断。正常原厂认证可能初始化用户资源或唤盘，结果明确 `native_query_may_update_state:true/may_wake_disks:true`。

仍不注册下列假执行器：

- 非白名单应用安装、所有应用卸载／更新／通用启停。原厂安装器接收目录字段、应用 ID 和固定操作枚举，`source` 在本固件中是整数，不是包 URL。当前只接下文审计的固定普通应用新增安装，不因任意目录条目存在而开放写入。不要求额外的外部独立签名，也不据现有证据宣称原厂有或没有签名验证；禁止开放任意包 URL／安装脚本代理。
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

## 实机目录兼容与一次结构诊断

2026-10-30，`45e75d3` 实机只查验收中，设备名称、AI、自动备份配置和用户目录正常；应用及备份列表返回 HTTP 200，但旧执行器投影为 `invalid_json`。这不等于原厂接口不可用，也不能用空列表补齐。

备份组包装已由同一固件的 `s_rest_get_backup_list_json@0x1905b3` 确认：`0x19065d` 创建对象，`0x190677` 创建数组，`0x19069f/0x1906a9` 将数组添加为固定 `list_cm` 字段。因此三组实际结构是 `run_list/wait_list/done_list → {list_cm:[...]}`。执行器增加这一固定包装兼容，继续逐项验证当前所有者整数 UID、任务 ID 和私有身份摘要；不递归猜测任意 `data/result` 包装，不把其他账户 UID 或数字字符串自动转为管理员。

应用新投影较旧业务缓存额外要求每条正整数 ID 和无重复；旧缓存仅展示名称并隔离异常条目。因此单条 ID／名称异常可能令新目录整表失败，具体原因必须由现场结构确认，不能以假设修改安装前的身份验证。为此，`probe` 的应用／备份项仅在显式开发诊断中附加 `response_structure/projection_issues`；普通 MCP 工具保持原来投影。诊断只允许固定字段名，返回 JSON 类型、字符串字节数、白名单嵌套层级、数组结构变体及固定失败码／行号；未知字段名和值只计数，不返回。最多检查每组二百五十六项，遇到深度或节点边界明确 `complete:false`，授权中途失效丢弃结构。

本轮同时将 `list_native_applications` 改为严格逐项隔离：先扫描全部原始节点中的合法正整数 ID，任何重复 ID 的全部记录均拒绝，再检查合规名称。即使重复项名称损坏，也不能让另一条记录“先到先得”成为唯一写前身份。结果给出 `accepted_entries/rejected_entries/partial/rejections`，固定计数原因区分非法节点、非法 ID、重复 ID 和非法名称；保留有效唯一节点，非空原表全部拒绝时仍返回不可用，不能补造空目录。安装状态查询预检只接受这份投影中实际保留的唯一 ID；异常节点不会取得调用资格。

`packages` 不再先通过严格应用投影才生成证据。异常 ID、名称或重复条目不会擦除其余记录的安全字段形状；结果标记 `catalog_projection_valid:false/status:diagnostic_only`，既不声明可执行安装，也不把缺失 ID 当作零。顶层不是预期列表、额外包装或 `code` 类型错误时，同样保留固定结构诊断。`364b858` 已各执行一次 `{"action":"probe","confirm":true}` 和 `{"action":"packages","confirm":true}`，确认正常备份投影与应用真实失败位置，结果记录在后文。

## 应用写执行器的具体准入契约

继续静态核对已定位到 `emhistor rest_modulize_set_oper@0x22bce3`。安装分支分别调用前台 `modulize_install_mod_frontground@0x75db5e` 或后台 `modulize_install_mod_background@0x75e6f1`；删除进入 `modulize_remove_mod@0x75ff7d`，先从模块表按 ID 取真实名称，再经 `s_modulize_mod_remove_func@0x7526bc` 调用已注册模块的停止／删除回调。原厂模块表和应用目录间的身份关系必须核实，不能只相信调用者传入的同名字符串。

前台安装函数对 `ipc/update_desc/frpc/nginx` 存在特殊处理分支，说明该模块系统涵盖核心服务，并非全部都是普通可卸载应用。`appcenter` 删除请求还固定携带 `data_clr=1`；`rest_modulize_set_oper@0x22c195–0x22c1a3` 将其写入删除结构 `+0xd48`。本批尚未逐模块证明该标记和删除回调的用户数据范围，不能把卸载包装成“保留数据”，也不能让安装器管理 Toolbox 的 Docker／运行环境依赖。

应用写工具统一采用以下契约；当前仅下文已审计的新装能力实现，更新与卸载仍不注册未闭合回调：

1. 管理员只选择当前所有者目录的 `application_id` 和执行器生成的 opaque `expected_version`；版本绑定 ID、模块名称、`vid/module_type/source/version/size` 及当前安装状态。请求不接收 URL、`paramList`、脚本、路径、原厂 action 或任意模块名。
2. 发送前重读同一固定目录，核对目录条目与原厂模块表的精确对应。只允许已审计的普通应用；核心服务及 Toolbox 依赖不得因管理员角色而跳过保护，未知模块保持不可执行。
3. 安装／更新仅由服务端重新验证的条目构造固定 `install_apply` body 和 `flag=0/1`。卸载须先核实该模块的真实停止、程序清理、数据清理及依赖语义；不得暗改原厂 `data_clr`，也不得把原厂模块删除变成任意文件删除入口。
4. 三类写入均要求确认、本键幂等、发送前和结果后的当前授权／身份复验。仅保留原厂实际 ACK、错误码和已返回的任务标识；受理不表示完成，未知结果不重放，进度只沿现有显式安装状态工具读取原厂值。

对于其余模块和更新／卸载，阻塞点是实际目录身份与模块映射、逐模块清理边界及异步状态契约，不是额外独立签名要求。结构诊断提供上述当前列表证据；不能据缺失字段推断原厂不存在包校验，也不会为填满工具目录而接受任意地址安装。

## 已审计普通应用的新增安装

当前固定白名单为 `7/netdisk_bd`（百度网盘）、`8/contact_backup`（通讯录备份）、`34/netdisk_bd2`（百度网盘 v2）、`35/66_print`（66 打印），唯一来源为 `native_capability_specs.APPLICATION_INSTALL_SPECS`。仅实现新增安装，未实现更新或卸载。这些不是按 `source=0` 推导的普通应用：还须应用目录的精确 ID／名称／`source:0/module_type:0` 与模块可用表的精确 ID／名称／`type:extension` 对应，已安装表没有该 ID 或同名别名，当前原厂版本为字符串 `0`；目标 `latest_ver` 与应用目录 `version`、两处大小必须一致。任何原始重复 ID、同名异 ID、空安装身份、安装中或已安装状态都拒绝。核心 `ipc/update_desc/frpc/nginx`、Docker、下载引擎及其它未审计模块不在安装白名单内，管理员也不能跳过。

固件 `modulize_mod_netdisk_bd2_register@0x76f755` 在 `0x76f7a3` 注册固定名称 `netdisk_bd2`，在 `0x76f7ce/0x76f7d5` 绑定安装回调 `modulize_mod_netdisk_bd2_install@0x76f1ae`。回调核对当前用户资源，调用 `netd2_get_install_path@0x32bdd6` 获取原厂选定的独立 `netdisk_bd2` 程序目录；`netd2_get_download_path@0x32bedd` 以原厂选定磁盘和当前用户构造 `Users/<user>/Downloads` 应用数据位置，并写入模块运行上下文。安装后的启动回调 `modulize_mod_netdisk_bd2_start@0x76efb0` 调原厂 `netd2_init@0x31e775`；文件检查回调还核验该模块的 `libkernel.so/P2PClient/baiduNas_v2`。没有执行这些厂商 ELF 或回调地址。

原厂包选择委托固定模块 ID，不接收调用者的 URL 或脚本。`appcenter install_apply_request@0x3543` 清零结构，读取固定 JSON 字段；`flag=0` 进入新增安装，`flag=1` 更新不在本工具范围内。`client_id` 缺失仅记录日志，工具使用固定 `r1-toolbox`；`vid` 可为空字符串，缺失的可选 `paramList/support_terminal` 不伪造补齐，也不向调用者开放。传输只允许十个固定 body 字段：`client_id/flag/size/apply_id/module_name/vid/module_type/source/version/guest`，`flag/source/module_type/guest` 必须为整数零，ID 和名称必须同时匹配上述固定白名单；不能将某个获准 ID 与另一个模块名称拼接。原厂通过先前审计的固定模块安装链下载／部署包，既有安装检查会拒绝已经安装的模块；Toolbox 不添加外部独立签名要求，也不宣称进行了原厂不存在于当前证据中的包校验。

`plan_native_application_install` 参数只有 `application_id`，最低管理员＋`apps`＋精确工具授权，并要求本键独立 `application_ids` 包含目标 ID。该列表是精确资源集合，没有“所有应用”通配符。旧存储缺省字段只兼容原 `34`，新增 v3 键缺省 `[]`，显式 `[]` 拒绝全部安装；编辑省略保留原值，扩大软件白名单不扩大旧键权限。未获目标授权时在查询原厂目录前拒绝。

计划显式查询当前应用与模块库存，不安装；原厂查询可能联网更新支持状态。允许时返回 `app_install_<32hex>` 的 `expected_version`，绑定精确包字段、当前未安装证明、原厂私有目录摘要以及当前 Key／授权修订／凭据代际／服务代际和排序后的 `application_ids`。目录私有路径只在宿主内参与摘要，响应只有固定资源类别和各应用实际影响说明。目录或安装前提不满足时给出固定原因和 `allowed:false`，不产生可安装版本；权限错误不输出可用计划。

实际影响是：原厂可能联网下载模块包，写入独立程序目录及管理员原生应用状态，启动百度网盘后台进程；原生网盘后续任务可使用管理员 Downloads。此功能属于管理员明确委托原厂应用安装，不能宣传原生进程已被 Toolbox 文件授权沙箱化，也不能拿安装授权执行任意文件操作。计划中的版本是最后观察到的厂商候选版本；固定原厂后端按当前模块 ID 选择包，不提供目标字节或最终已安装版本证明。返回明确 `package_selection:factory_current_module_id/version_is_observed_not_final_attestation:true`，最终状态仍须核对。

`install_native_application` 要求同一应用 ID、当前 `expected_version`、`confirmed:true` 和本 Key 的 `idempotency_key`。发送前在有界共享锁内重新查询两目录并重建相同版本；版本变化、目标授权撤回、轮换、服务重开或身份变化拒绝。目录续验、发送前、接收 ACK 后及读取幂等回执时都复核目标应用授权；修改资源集合会使旧计划失效，撤权后不返回旧回执。固定调用 `POST /rest/1.1/applycenter?action=install_apply`，不把原厂用户参数、包地址或安装目录转成 HTTP 输入。

### 扩展应用的逐项静态证据与影响

以下地址均来自匹配固件 `V6.3.3 Build_260907` 的 `emhistor` 静态反汇编及异常展开表函数边界，没有执行厂商 ELF。安装链与目录前提继续适用；注册记录本身不会授予机器安装权限。

| ID／模块 | 安装与启动证据 | 必须呈现的影响与限制 |
| --- | --- | --- |
| `7/netdisk_bd` | 注册 `0x76eadb` 将安装回调 `0x76e7ee`、启动回调 `0x76e599` 绑定至固定 `netdisk_bd`；安装分别调用 `netd_get_install_path@0x2fee7c`、`netd_get_download_path@0x2fefe2`；启动调用 `netd_init@0x3102da` | 独立程序目录位于原厂选定磁盘；下载数据由原厂固定到 `Users/admin/Downloads`。安装可将原厂暂存的两份 `p2p.conf` 迁移回固定 `netdisk_bd` 子目录。初始化会创建数据库／表、读写配置、加载已有 `dirtask.json`、启动原生下载进程及后台任务。不是空白应用或文件沙箱保证；不能声称安装后一定没有既有任务。 |
| `8/contact_backup` | 注册 `0x76f97d` 将安装 `0x76f89b`、启动 `0x76f8d7` 绑定至固定 `contact_backup`；安装只记录原厂用户标识并返回 `0`，启动直接返回 `0` | 原厂安装链可能查询厂商并写模块元数据。该回调不证明已经备份通讯录、创建独立程序目录或启动后台备份；后续原厂业务仍按自身权限使用数据。本工具不调用备份入口或删除回调。 |
| `35/66_print` | 注册 `0x7737e1` 将安装 `0x773778`、启动 `0x7737b4` 绑定至固定 `66_print`；安装只记录原厂用户标识并返回 `0`，启动直接返回 `0` | 原厂安装链可能查询厂商并写模块元数据。ACK 不证明打印服务已启动、打印机已绑定或已发送打印任务；不制造后台进程、用户文件范围或额外依赖的证明。 |

这三个模块不通过 Toolbox 安装 Docker、下载引擎、`frpc`、`nginx` 或其他核心模块。静态证据没有逐项证明原厂下载包的最终字节和未来版本行为，当前目录精确版本只能作为当次观察，不能当作最终包验签。所有原生应用的后续访问仍由原厂权限决定；Toolbox 的文件根授权不会把原生安装器和应用进程变成 Users 范围沙箱。

`netdisk_bd` 的 `bd_start_netdisk@0x2ff5df` 经 `0x2ff8cc–0x2ff942` 委派原厂固定 `netdisk_start.sh`。该脚本联网探测百度域名，监督 `baiduNas/netd_bd`；`netdisk_downloader.sh` 同样监督 `P2PClient`，已有配置还可能启用其原生 `raichu` 功能。同名进程重复时原脚本会用 `killall` 重启，长周期故障恢复分支可能向 `/proc/sys/vm/drop_caches` 写 `1`，还会维护原厂 `/tmp` 崩溃日志。这些是本应用的真实原生委托影响，已纳入预览的 `may_restart_native_processes/may_adjust_host_cache/may_load_existing_tasks` 与文案；没有修改或执行厂商脚本，也不将这些行为宣传为受文件授权范围限制。

2026-10-30 的既有 NAS 只读库存显示 `7/netdisk_bd`、`8/contact_backup` 在已安装表；`35/66_print` 在可用表但 `curr_ver:V1.0.0`，因此这三项在该设备上全部拒绝新增安装。即使已安装项不再出现在可用表，也明确返回 `application_not_uninstalled`。`34/netdisk_bd2` 是此前观察到的未安装候选；这不等于已经安装或验收。本轮仅完成静态审计、单元与真实 HTTP fixture 验证，没有为验收卸载／重装现有应用或向 NAS 发送安装。实机安装仍需先展示该设备的具体计划，由所有者选择应用并确认。

`appcenter` 在 `0x4651` 保存等待状态，`0x46a6` 排入真实异步队列。成功分支 `0x4700–0x479d` 只输出精确 `module_name/apply_id`，可能省略 `code`；工具只在这两个身份字段精确匹配且没有明确非零错误码时标记 `accepted:true/factory_acknowledgement_only:true/effect_confirmed:false`。没有真实独立任务 ID 时返回 `native_task_id:null`，不拿应用 ID 冒充任务 ID。继续使用已有 `get_application_install_progress` 显式查询原厂状态，该查询本身仍须管理员确认与新幂等标识。

可能已发送但没有可信 ACK，或写入后撤权，保留 `mutation_outcome:unknown` 和 `result_unconfirmed`，相同 Key／修订／凭据代际的同幂等标识不会再次发送。明确未发送可重试；已受理、拒绝或未知结果都保留内存回执；进程重启后的内存回执不构成持久幂等保证。共享锁等待最长十秒，安装最多两个固定目录查询与一个写请求，各自使用原有十秒绝对网络窗口。

2026-10-30 的模块实机收据已证明 ID `34` 的应用版本与可用表候选 `V1.0.14` 相同、当前版本为 `0` 且不在已安装表；`usable:0` 在已安装记录中也普遍存在，因此本实现保留该原始码参与版本，不将它猜为不可用布尔。实现与本地真实 HTTP fixture 已覆盖计划、固定 POST、身份／版本变化、核心保护、幂等、撤权和不确定 ACK；尚未在 NAS 实际安装该应用。真实写入验收须先向用户展示当前计划和上述具体影响，再由用户选择该普通应用；不会为查询形状再次添加零散诊断字段打包试探。

## `364b858` 实机复核与模块库存工具

2026-10-30，`sha-364b8586bc0a191d308fb19ed62b40c302d5a053` 的显式固定验收确认：原厂六项检测全部 `status:ok`；应用十五条中接受十三条，原始行十三／十四（ID `2/35`）的空名称被隔离，`rejected_entries:2/partial:true`。备份三组固定 `list_cm` 均为真实空数组并正常投影；这只证明该接口当次返回无对应任务，不代表整个 NAS 没有其他后台任务。

`packages` 保留全部十五条安全形状，因两个空名称标记 `diagnostic_only/catalog_projection_rejected`，并未启用安装。十三条有名记录的 `source/module_type` 都是整数零，但其中包含 Docker（ID `38`）；因此“来源零、类型零、合法名称”不足以证明普通可安装／卸载应用。所有记录的 `vid` 为空字符串，`paramList/support_terminal` 缺失；不能伪造这些值，也不能据其缺失单独断言原厂无法安装。

后续本地投影兼容仅对这两个已观察的空字符串名称使用静态注册名：`2→ipc`、`35→66_print`，显式标注 `name_source:audited_module_registration`。它不会从不可信的 `name` 字段或其它 ID 猜名称；字段缺失、`null`、控制字符、未知 ID 及重复 ID 仍隔离。该回退仅改善目录显示，安装依然要求原始 `module_name` 精确匹配；仅靠回退名的条目也不能取得会推进原厂状态的安装进度前检资格。此前实机的十三／十五收据仍保留历史原样，本地兼容与 fixture 通过不代表新版已部署或完成安装验收。

本轮新增的实际能力为显式模块库存查询 `list_native_modules`，不是新增开发探针。固定 `GET /rest/1.1/update?action=get_modulize_table` 使用现有管理员会话、叶证书 pin 和唯一 emhistor 监听验证；入参为空，不接收模块 ID、action、路径、URL 或 body。工具最低角色读写、功能域 `apps`，还须精确工具授权。它不加入原厂 `probe` 固定六项、`packages`、被动业务轮询或网页 GET。

固件契约来自 `rest_get_modulize_table@0x229a88`：`0x229b8e` 调 `modulize_list_update_support_status_list@0x756ab4`，可能联系厂商更新内部支持状态；当前固定所有者 UID `100000` 分支分别在 `0x229c01/0x229c93` 调 `modulize_get_tbl@0x760efe`，传入 `0/1`，明确生成 `available_list/installed_list`。其他用户走 `moddb_get_tbl@0x77e81c`，不属于该固定管理员认证契约。成功返回含整数 `code=0`，缺失、空或类型异常不补成成功。

模块表序列化 `0x7610f8` 读取 `module_list`，在 `0x761162/0x76118a` 选择上述两个表；`0x761392–0x7613da` 投影原厂 `module_name/id`，`0x761446–0x76161f` 处理 `latest_ver/curr_ver`，`0x761722–0x761870` 处理 `status/paramList/size/type/upgradable/usable/device_need_update`。Toolbox 只返回其中以下固定有限合同：

- `data.groups.available/installed` 为两个独立对象，各有 `items/returned_count/accepted_entries/rejected_entries/partial/rejections`；不跨表合并，也不将一个表的记录补入另一个表。
- 单条为 `module_id/name/current_version/available_version/native_status_code/native_type/upgradable_code/usable_code/device_update_required_code/running`。版本缺失或包含路径／URL 语法时为 `null`；枚举仅保留合法整数，未知类型为 `null`，不猜测含义，`running` 始终 `null`。
- 每表先统计全部原始合法正整数 ID，重复 ID 的所有节点都拒绝，再隔离坏名称／坏节点。表缺失、类型不符、超过二百五十六条或非空表全部无效均拒绝，不能补空库存。真实空数组可正常返回。
- 不输出 `inst_path/paramList`、包地址、签名、原始 JSON 或认证信息。明确 `catalog_confers_write_identity:false/running_state_available:false`，返回 `may_contact_vendor:true`；模块库存是原厂观察结果，不是安装授权或模块白名单。

安装后端使用这两个真实表与有效应用目录核对 `apply_id↔id`、名称、当前／候选版本和安装成员关系；只有上文逐项完成安装／启动回调及依赖审计的普通模块可新增安装，卸载所需数据清理语义仍未开放。库存工具已经提供固定业务查询通道；无需再为相同目录形状添加字段重打包。`modulize_get_status@0x22d1c6` 仍未接入：它是 multipart 状态循环，涉及内部状态处理，不能冒充一次无副作用的运行状态查询。安装／更新／卸载尚未完成实机业务验收。

验证覆盖真实本机 HTTP server 的固定 route／method／query／JSON body、目录归属、未知值、进度不造假、写后撤权、旧键幂等与不确定结果。真实 TLS、进程／socket、签名读取的攻击面沿用固定传输定向测试；本机 HTTP fixture 使用仅构造器可注入的测试连接，不修改生产固定端口。NAS 写入验收仍需用户指定明确对象，先核对只查诊断再验收单项写入。
