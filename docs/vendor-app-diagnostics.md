# 原厂应用诊断：只读取既有安装缓存

本页复核 `V6.3.3 Build_260907` 的 `emhistor` 和独立 `appcenter`，日期为 2026-10-29。只做 ELF 静态反汇编，不执行厂商程序、调用原厂接口或访问 NAS。下面的地址是样本链接虚拟地址，不能跨版本作为 ABI 使用。

## 本版诊断契约

`runtime/vendor_app_diagnostics.py` 的 `collect(root="/")` 只读取固定文件 `/tmp/nand_config/app_center.conf`，不访问 REST、8888、云服务、应用安装目录或用户磁盘。`root` 仅用于内部 fixture，不是 HTTP 参数。

返回字段为 `available`、`status`、`reason`、`source`、`data`。成功状态明确为 `cached`，来源固定为 `histor.app_center.conf`。`data.apps` 只包含应用名称、缓存版本、原厂安装状态代码和始终为 `null` 的 `running`；另有 `cached_entries`、`cache_modified_at`、`live:false`、`complete:false`。

这些是**原厂先前保存的部分安装元数据**，不是当前完整应用目录、安装任务进度或实时运行状态。不能把 `status=0/4` 当成进程运行状态，也不能把读取时间当作缓存更新时间。缺少缓存时返回 `available:false`、`status:cache_missing`、`data:null`，不返回假的空应用清单；有效文件中确实是空 `list` 时，也仍标记部分历史缓存。

解析限制：普通文件、固定路径、拒绝符号链接／reparse 路径、文件不超过 1 MiB、最多 256 条、名称和版本各不超过 63 个 UTF-8 字节；拒绝重复 JSON 键、非标准常量、控制字符、路径／URL 冒充标签、未知 schema、重复应用名及当前未验证的状态代码。读取前后对比文件身份和修改时间，变更时不接受结果。不输出原始配置、`apply_id`、用户 ID、token、安装路径或下载 URL，不创建／修复厂商缓存。

## 固定缓存的读写证据

独立 `/histor/bin/appcenter`：

| 函数／位置 | 静态确认 |
|---|---|
| `load_conf @ 0x183da` | `0x183f9` 指定 `/tmp/nand_config/app_center.conf`，经 `0x1916a` 读取 |
| `load_conf: 0x18575` | 直接调用 `json_tokener_parse`；文件内容为 JSON，不是需要解密的 token 容器 |
| `load_conf: 0x18678` | 从顶层对象读取 `list` 数组 |
| `load_conf: 0x1879c / 0x18ab6 / 0x18c7b` | 分别读取 `module_name`、`version`、`status` |
| `getListJsonBufV2 @ 0x17bda` | 构造顶层 `list`／`count`，应用条目写入上述同名字段 |
| `0x17c73–0x17c8a` | 只把内存安装状态代码为 `0` 或 `4` 的节点加入持久化数组 |
| `0x17f78` | `count` 统计遍历的全部内存节点，可能不等于过滤后的 `list` 长度；不能据此推断清单完整 |
| `get_install_progress: 0x504d / 0x505d / 0x5065 / 0x506c` | 生成 V2 JSON 字符串并写固定 `/tmp/nand_config/app_center.conf` |
| `appcenter_write_file @ 0x18fce` | 使用 `open64` 的写入／创建／截断标志和 `write`，证实原厂 getter 可能落盘 |

Toolbox 仅投影已经存在的文件，不执行这里任何写入函数。文件修改时间作为 `cache_modified_at` 返回，不能代替实时采样时间。

## 为什么没有直接调用应用 getter

`emhistor` 注册的 `/rest/1.1/applycenter` 有 `get_apply_list`、`get_install_progress` 等动作，但 REST 名称本身不构成纯读保证。

| 入口 | 已确认链条与边界 |
|---|---|
| `emhistor rest_get_apply_list @ 0x22abc4` | `0x22ad2d` 调 `get_http_response @ 0x22a56e`，把请求交给独立应用服务 |
| `emhistor rest_get_install_progress @ 0x22b68e` | 读取当前用户属性并构造请求体，`0x22b8e6` 转发给同一服务 |
| `get_http_response @ 0x22a56e` | 固定连接 `127.0.0.1:8888`；无请求体时拼 GET，有请求体时拼 POST，含 `Connection: close`。这是原厂内部调用证据，不是 Toolbox 已审计的公开 REST transport |
| `appcenter get_apply_list_request @ 0x3200` | 读取 `opttype`；为 `1` 时在 `0x338c` 调插件图标／列表同步 `sync_app_icon @ 0x150c4`，后者继续进入请求／更新路径。非同步分支才序列化现有内存列表，仍依赖原厂用户上下文 |
| `appcenter get_install_progress_request @ 0x4aff` | 可改变 `apply_opt`、插件状态／查询完成标志、同步插件、写 `app_center.conf`，并调用 `touch_update_application_list_for_open @ 0x10b73`；不是无副作用的进度读取 |

因此本版不连接 8888，不伪造用户 ID，不靠省略参数或改变 `opttype` 绕过原厂前端认证。将来如果需要实时应用状态，必须先建立明确的正常授权来源、当前用户范围及无副作用的固定端到端契约。

## 主服务 REST 的共同认证和副作用

`rest_CGI_proc @ 0x174ec6` 从请求中解析 `action`、`access_token` 等，并在进入大部分业务路由前经过原厂认证、登录与权限检查。

- `rest_check_auth_only @ 0x173f37` 调 `user_auth_check @ 0x5170ce`，处理原厂 HTTP 认证路径。
- `0x175a50` 调 token 检查包装器 `0x173e41`，后者调用 `user_auth_token_check @ 0x514e13`。这不是 Toolbox 的网页口令／配对会话，两套会话不能混用。
- `0x175aba` 调 `rest_check_login @ 0x173b52`。该函数在 `0x173ccf` 查询用户资源；缺失时日志明确为“Try to start user resource”，随后在 `0x173dc3` 调 `0x50385e` 启动资源。因此即便业务 handler 仅读内存，整条 REST 请求仍可能启动后台用户资源。
- `0x175b34` 继续调用原厂权限检查 `0x17417f`，之后才分发注册动作。
- 原厂还有与初始化状态、来源和特殊路由有关的分支；本版不利用这些分支获取豁免，不自动登录、续 token、从配置或用户数据库搜取 token。

主服务 `appWebStart @ 0x97d1a0` 从调用方接收配置及回调，`isAppwebCfgChanged @ 0x97c885` 还会检测端口／认证变化。本轮没有形成当前 NAS 的固定主 REST 地址、认证凭据来源和无副作用调用合同，不能把固件字符串或惯用端口当作监听事实。

## 验收边界

下一版可以通过 Toolbox 的受限业务诊断返回：是否存在可读的原厂应用缓存、其修改时间、实际缓存中的应用名称／版本／安装状态代码，以及明确的实时能力限制。它不能据此宣称“原厂应用实时接口已通过”，不能用 Docker 容器列表替代原厂应用列表，也不能通过调用原厂 get 来生成缺失缓存。
