# 原厂应用数据：显式刷新与被动快照

本页依据 `V6.3.3 Build_260907` 的 `emhistor`、独立 `appcenter` 静态反汇编及 Toolbox fixture，复核日期为 2026-10-29。没有执行固件程序、安装应用或修改 NAS 设置。下列地址为样本链接虚拟地址，不能跨固件版本作为 ABI 使用；实机当前响应仍须通过显式刷新验收。

## 两条数据来源

| 来源 | 触发方式 | 能确认什么 |
|---|---|---|
| 固定原厂 `get_apply_list`，显式携带 `opttype=0` | 用户授权的业务刷新操作，单次查询 | 原厂应用服务当时已有的目录条目；包含目录项，不等于全部已安装应用或运行进程 |
| `/tmp/nand_config/app_center.conf` | 被动读取既有普通文件，作为 fallback | 原厂先前落盘的部分安装元数据，不代表实时完整目录 |

`runtime/vendor_app_probe.py` 的 `collect_application_probe(root="/", *, session_reader=None, transport=None)` 只在显式调用时读取固定管理员的既有会话，派生一次签名，并发送：

```text
GET https://127.0.0.1:20443/rest/1.1/applycenter?action=get_apply_list&opttype=0&access_token=内部签名
```

它复用媒体探测的会话读取器、固定 `/tmp/Cert.pem` 叶证书 pin、限时 HTTP／JSON 解析器。证书匹配前不发送凭据；不使用代理、DNS、重定向、其他账号或会话重试，不接受网页传入 host、路径、动作、`opttype`、用户 ID 或原厂参数。token、签名、源标识和原始响应留在宿主，返回只允许的投影字段。

**显式刷新不是全程无副作用的被动监控。** `opttype=0` 确实跳过业务 handler 的应用同步分支，但主服务共同认证中间件仍可能启动当前管理员的后台资源。不能在页面打开、定时监控、`GET /api/business` 或 MCP 被动读取中自动签名查询。也不调用 `opttype=1`、`get_install_progress`、安装、卸载、更新或服务启动接口。

## 自有快照与返回契约

成功探测为 `available:true`、`status:ok`、`source:histor.applycenter.get_apply_list`。原厂序列化器没有固定 `code` 字段，因此缺失时保留 `native.code:null`、`code_present:false`；若 REST 包装层返回 `code`，则必须为整数 `0`，不能把字符串、布尔值、未知值或错误码当成功。

`data.apps` 仅投影 `name`、`version`、`installation_status_code`、始终为 `null` 的 `running`。当前只保留已在持久缓存证实出现的原始状态代码 `0/4`，其含义不翻译成“正在运行”；其他状态枚举和未确认的版本保持 `null`。`cached_entries` 为实际投影条目数，`live:false`、`complete:false`；不输出 `apply_id`、`vid`、安装路径、URL、用户 ID、账号名称或原始消息。目录响应本身不能作为安装成功、后台运行或全部 APP 功能均已接入的证据。

`runtime/vendor_app_diagnostics.py` 提供：

- `record_application_snapshot(state_dir, probe) -> bool`：仅把脱敏后的这一次结果原子写入 Toolbox 已有持久目录下的固定 `application-snapshot.json`，包括失败结果。不会向原厂缓存写数据。失败刷新覆盖旧成功，不把旧成功标成新成功；持久化本身失败返回 `false`，调用方必须报告存储失败。写入和读取都重新投影，文件不存会话、请求地址、原始 native 消息或异常文本。
- `collect(root="/", *, state_dir=None)`：只读上述快照；没有快照时读固定原厂缓存。最近刷新失败时，若原厂缓存可读，只回退该部分历史缓存，并在 `reason` 明确说明刷新失败、保留固定 `error_code`；两者均不可用时 `data:null`。损坏快照不会变成空目录或触发联网重试。无 `state_dir` 保留原先仅读原厂缓存的行为。

读取自有成功快照时，来源为 `toolbox.application_snapshot`、状态为 `cached`，`sampled_at` 和 `data.cache_modified_at` 均为该次显式查询时间，不是页面请求时间；原厂 fallback 的 `cache_modified_at` 仍为原厂文件的修改时间。刷新失败返回 `refresh_failed`，损坏快照为 `snapshot_invalid`，不会把此前的应用项伪装为本次结果。无需也没有隐含的 TTL 或后台刷新。

上述 `root`、`state_dir`、reader 和 transport 都是内部配置／测试注入，不能变成 HTTP 文件路径或任意调用参数。调用方负责沿用 owner 授权、同源／CSRF 和业务采集锁；它们不是机器密钥权限豁免。

## 固定原厂缓存的限制与错误分类

原厂缓存来源为 `histor.app_center.conf`、成功状态为 `cached`。有效文件内确实为空 `list` 时才返回空数组；缺少文件为 `cache_missing`、`data:null`。不遍历用户盘，不读取安装目录，不连接 8888 生成缓存。

解析限制为：普通文件、固定路径、拒绝符号链接／reparse 路径、文件不超过 1 MiB、最多 256 条、名称和版本各不超过 63 个 UTF-8 字节；拒绝重复 JSON 键、非标准常量、控制字符、路径／URL 冒充标签、未知 schema、重复名称及未经确认的缓存状态代码。读取前后对比文件身份和修改时间，期间变化则拒绝该结果。

缓存不可用时提供固定 `error_code`，帮助一次采样区分原因，不返回原始文件或异常内容：

| 分类 | 固定错误码示例 |
|---|---|
| 不存在／权限／文件与路径 | `cache_missing`、`permission_denied`、`unsafe_cache_path`、`cache_file_bounds` |
| 替换／读取中变化 | `cache_replaced`、`cache_read_length_changed`、`cache_changed_during_read` |
| JSON 与编码 | `invalid_json`、`invalid_utf8`、`json_duplicate_key`、`json_nonstandard_constant` |
| 不符合当前已证实 schema | `unsupported_cache_list`、`unsupported_application_entry`、`unsupported_application_label`、`unsupported_cached_status`、`duplicate_application_name` |

`cache_invalid` 仅为其余本地读取失败的兜底，不能直接断言缓存为空或应用未安装。

## 静态证据：同步分支、目录与缓存不同

独立 `/histor/bin/appcenter`：

| 函数／位置 | 已确认行为 |
|---|---|
| `get_apply_list_request @ 0x3200`，`0x32b4–0x3327` | 从原始 URI 查找 `opttype=`，用 `atoi` 解析，比较是否等于 `1` |
| `0x338c` | 仅 `opttype=1` 路径调用 `sync_app_icon @ 0x150c4`；固定 `0` 跳转到 `0x33e4`，不调用该同步分支 |
| `0x3439` | 以已有请求用户上下文调用 `getListJsonBuf @ 0x176e2`，不是缓存 V2 序列化器 |
| `0x17740`、`0x177b4`、`0x1789b`、`0x178f0`、`0x17979` | 构造顶层 `list`，条目包含 `module_name`、`version`、当前用户对应的原始 `status` |
| `0x17b06`、`0x17b33` | 另有 `count`、`support_ecloud`；当前不输出或解释这些字段 |
| `load_conf @ 0x183da`，`0x183f9` | 固定 `/tmp/nand_config/app_center.conf`，经 `0x1916a` 读取 |
| 读取器 `0x1916a`，`0x191b1 / 0x192fe` | 普通 `open64(O_RDONLY)`／`read`，没有解密或解压；终止换行／NUL 加在内存缓冲区 |
| `0x18575 / 0x18678` | 直接 `json_tokener_parse` 并读取顶层 `list` |
| `0x1879c / 0x18ab6 / 0x18c7b` | 缓存读取 `module_name`、`version`、`status`；版本在 `0x18af5–0x18b00` 通过 `strncpy(...,0x3f)` 读取 |
| `getListJsonBufV2 @ 0x17bda`，`0x17c73–0x17c8a` | 持久化时只序列化状态为 `0/4` 的节点；与实时目录未过滤的遍历不同 |
| `0x17f78` | 缓存 `count` 统计全部内存节点，不能按它推断 `list` 完整性 |
| `get_install_progress_request @ 0x4aff` | 会修改完成状态、同步插件，并在 `0x504d–0x506c` 写 `app_center.conf`，还可触发应用列表更新；禁止拿它补全实时数据 |
| 写入器 `0x18fce`，`0x18ffb / 0x1906d` | `open64` 写入／创建／截断与普通 `write`，证实该缓存为原厂 getter 写出的 JSON |

主服务 `emhistor rest_get_apply_list @ 0x22abc4` 在 `0x22ad2d` 调 `get_http_response @ 0x22a56e`，将原始 URI 经固定内部 `127.0.0.1:8888` 交给 `appcenter`；在 `0x22ae4a` 按 `application/json` 返回原始结果。Toolbox 走已验证的主 REST 认证入口，不直接连接 8888、伪造用户 ID 或绕过认证。

共同 `rest_CGI_proc @ 0x174ec6` 在分发前执行 token 检查 `0x175a50 → 0x173e41 → 0x514e13`、登录检查 `0x175aba → 0x173b52` 和权限检查 `0x175b34 → 0x17417f`。登录检查内 `0x173ccf` 查询用户资源，缺失时 `0x173dc3 → 0x50385e` 启动资源。因此仅能确认应用 handler 的固定非同步分支，不把整条正常授权请求宣传为全程无副作用。

## 实机验收

部署包含上述实现的新镜像后，通过 owner 授权的业务刷新入口执行一次应用刷新，核对 `attempted`、固定 `native` 信息、HTTP 状态、应用 `name/version` 及脱敏结果；同时核对 `application-snapshot.json` 的写入结果。随后只读业务页应复用同一次快照时间，不再触发原厂请求。查询失败必须看到固定错误码与 `data:null` 或明确标注的原厂历史缓存 fallback；不能以进程监听、Docker 列表或空列表代替原厂应用数据。
