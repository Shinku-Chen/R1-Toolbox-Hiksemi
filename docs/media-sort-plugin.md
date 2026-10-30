# 影视追更排序

网页“设置 → 影视追更排序”提供内置可选插件。它针对已审计的原厂影视中心数据库，按用户提供的原始 SQL 规则调整排序时间，让同一部剧的新路径进入“最近添加”时有机会前移。Toolbox 不替换影视中心程序或电视客户端，实际列表顺序仍需用真实更新验收。

## 使用流程

1. 点击“检测数据库”。服务只从正在运行、身份匹配的原厂 `scraper` 进程读取 `-d` 数据目录，不搜索媒体目录，不要求填写路径。
2. 核对数据库位置并选择候选。多个数据库分别展示，不默认选择第一个；无法核验的候选不可写。
3. 点击“应用追更排序”并确认。后台先创建私有一致性备份，再以一个 SQLite 事务安装用户原始规则。
4. 需要停止时，点击“撤销追更排序”并确认。撤销只删除精确识别的 `metadata_path_guard` 与 `metadata_guard_insert`，不恢复整份旧数据库，不覆盖此后新增的媒体；已经调整的排序时间保留。

如果检测到完全相同的原始表与触发器，状态直接为“已启用”，撤销可用，应用不可重复执行；再次应用不会重新 seed、重建或覆盖现有基线。未知同名对象、部分对象、先前增强版残留或额外触发器均报告冲突，不自动接管、替换或删除。

打开设置只读取 Toolbox 内存缓存。“读取缓存”也不扫描原厂进程或数据库。点击检测、应用或撤销才访问原厂数据，可能唤盘或触发 SQLite 辅助文件的元数据变化。没有周期检测、自动应用或启动时重装。规则存放在原厂数据库中，因此 NAS／Toolbox 重启后继续存在；停止 Toolbox 容器不会撤销它。

网络超时、连接中断或结果未知时，页面不会重发写操作，并禁用应用／撤销，直到手动重新检测。缓存状态表示上次检查结果，不证明此刻数据库仍未变化。检测结果只绑定本进程中的候选 ID 与版本，Toolbox 重启或来源变化后需要重新检测。

## 原始排序规则

插件安装的就是用户提供的 SQL，不额外增加 `WHEN`、类型过滤、`UPDATE` 触发器、去重路径或跨类型基线：

```sql
CREATE TABLE IF NOT EXISTS metadata_path_guard (
  id INTEGER, lid INTEGER, type INTEGER, path TEXT,
  PRIMARY KEY (id, lid, type)
);
INSERT OR REPLACE INTO metadata_path_guard (id, lid, type, path)
  SELECT id, lid, type, path FROM metadata;
CREATE TRIGGER IF NOT EXISTS metadata_guard_insert
AFTER INSERT ON metadata
FOR EACH ROW
BEGIN
  UPDATE metadata
     SET timestamp = CAST((julianday('now') - 2440587.5) * 86400000000 AS INTEGER)
   WHERE id = NEW.id AND lid = NEW.lid AND type = NEW.type
     AND EXISTS (
       SELECT 1 FROM metadata_path_guard g
        WHERE g.id = NEW.id AND g.lid = NEW.lid AND g.type = NEW.type
          AND json_array_length(NEW.path) > json_array_length(g.path)
     );
  INSERT OR REPLACE INTO metadata_path_guard (id, lid, type, path)
    VALUES (NEW.id, NEW.lid, NEW.type, NEW.path);
END;
```

规则语义：

- 只监听 `AFTER INSERT`；原厂新增集合并后经 `INSERT OR REPLACE` 写回时会触发。
- 以 `(id, lid, type)` 为键，比较 `json_array_length(NEW.path)` 与影子表中上一次的路径数组长度。
- 只有新数组更长时才把 `timestamp` 写成当前微秒时间，然后无论如何用新数组替换影子值。
- 长度相同、变短或只重排时，触发器不额外修改时间。重复路径不做特殊处理：如果重复项使数组变长，也会更新时间；纯资料刷新是否触发同样只取决于数组长度。
- 直接 `UPDATE metadata SET path=...` 不触发；首次插入没有旧基线时保留原厂 `timestamp`。
- `type` 只是键的一部分，不是过滤条件；所有类型都按同一规则处理。
- 路径数组按原样保存，不做去重，不用并集或跨类型基线。

### JSON 行为

原始 SQL 直接调用 `json_array_length(NEW.path)` 与影子表的 `path`。JSON 语法损坏时，SQLite 可能报 `malformed JSON`，导致该次 `INSERT OR REPLACE` 失败；合法的对象或标量不等于损坏 JSON，其数组长度可为零。插件不增加数组类型过滤或合法性 `WHEN`，保留原 SQL 的行为。

## 备份与撤销

备份位于 Toolbox 网页状态目录下的 `media-sort-backups/`，私有目录权限为 `0700`，随机、独占创建的备份文件为 `0600`。使用 SQLite 在线备份读取一致的已提交状态，包含 WAL 中的已提交数据；备份失败不应用规则。页面展示本次 Toolbox 进程已成功记录的最近备份位置，重启后该显示缓存清空，已有备份文件继续保留，不扫描或自动清理。

撤销不是整库恢复按钮。需要人工恢复备份时，应先停止原厂写入并核对备份与后续数据，不能在原厂运行中覆盖数据库。产品接口不接受备份路径、任意 SQL 或整库恢复请求。撤销只删除插件自己的精确对象，`metadata` 行和后续新增数据不变。

## 接口与授权

| 接口 | 请求体 | 行为 |
| --- | --- | --- |
| `GET /api/plugins/media-sort` | 无参数 | 只读 Toolbox 内存缓存，不打开原厂库。 |
| `POST /api/plugins/media-sort/probe` | 严格空对象 `{}` | 显式发现、核验候选并缓存固定阶段诊断，不接受 PID、路径、命令或 SQL。 |
| `POST /api/plugins/media-sort/apply` | `candidate_id`、`version`、`confirmed:true` | 已选候选的备份、安装原始规则。 |
| `POST /api/plugins/media-sort/revert` | `candidate_id`、`version`、`confirmed:true` | 撤销精确识别的对象，不回滚媒体数据。 |

这些接口仅接受当前网页所有者：默认 `password` 模式尚未设置口令时与其他网页管理一致直接可用；启用口令后需登录，显式 `pair` 模式需配对。保持真实来源、同源、JSON 与 `X-FNM-Request: 1`／配对 CSRF 校验。Bearer Token、MCP 和开发者白名单不能管理此插件，不能把已有原厂登录 Token 转成插件授权。

后台限定已审计 `scraper` ELF、进程启动生命周期、命名空间与根视图，并逐级 no-follow 打开数据路径，核验真实 SQLite 主文件句柄、辅助文件、属主／权限、完整表结构与 JSON1。来源不匹配、路径替换或未知结构时拒绝操作，不修改原厂目录权限。候选路径和备份位置只返回所有者，不进入公开快照、小屏、MCP、默认诊断或审计参数。

应用、撤销在执行、提交和返回前重复核验来源及所有者授权。提交后断线或撤权仍可能已经完成数据库事务，应重新检测，不能按错误响应断言未写入。服务停止只关闭授权与后台操作，不自动撤销已提交规则。

## 固定阶段诊断

“检测数据库”复用同一个所有者 `POST /api/plugins/media-sort/probe {}`，在原操作锁和 12 秒绝对窗口内采集一次固定的只读检查结果。响应及后续缓存 GET 的 `plugin.diagnostics` 使用 `schema_version:1`，包含扫描计数与完整性、命中／接受／拒绝数、最多 16 个 `scraper` 进程的阶段记录，以及发布前复核结果。未检测或服务关闭后为 `unknown` 空状态；诊断完成表示该轮检查结束，不表示所有阶段通过。

构造插件、启动服务、打开页面、缓存 GET 和页面轮询均不为诊断读取 proc、可执行文件或数据库。没有新增诊断路由、通用执行接口或 MCP 工具；机器 Token 和开发窗口本身仍不能调用此所有者接口。

每个固定阶段记录 `passed`／`failed`／`not_run`、白名单 `reason_code`、`elapsed_ms` 和有限的白名单观测。未执行明确为 `not_run`，不能将未进入的数据库阶段写成失败。`unavailable` 消息用固定中文及安全阶段／原因代码指出首个拒绝点，不附异常原文。

| 检查组 | 固定阶段与判断范围 |
| --- | --- |
| 扫描与进程代次 | 有界扫描、`comm` 命中、完整 52 字段 `stat`、PID 与启动时刻；保留扫描是否完整，不能把截断当作无进程。 |
| 内核身份 | `Uid` 四列、mnt／net／pid 命名空间与本机一致、root 视图一致；可独立安全读取的字段不因另一独立门失败而全部隐藏。 |
| 可执行文件 | exe 链接为绝对路径且未删除；真实 FD 的文件类型、UID／GID、权限、链接数、大小；固定 ELF 大小／摘要及读取前后文件身份。 |
| 参数与末次进程身份 | 有界且 NUL 完整的 UTF-8 参数、`-d` 数量、绝对／相对类型及 cwd 合法性，再复核进程代次；只返回计数、类型和合法性，不返回实际参数或 cwd。 |
| 数据库依赖 | 仅全部生产进程门通过后，逐级 no-follow 真实句柄链、固定 WAL／SHM／journal 身份、`mode=ro`／`query_only` 与真实 SQLite FD、完整 schema、JSON1 和插件自有对象判定。 |
| 发布 | 再核来源与当前所有者授权、服务 epoch；来源失败不发布可执行候选，授权／epoch 失效不发布本轮新诊断。 |

独立观测不构成可信来源。ELF 摘要只有在 root UID、普通且 root 所有的文件、无 `022` 写位、单链接、精确大小和 PID 生命周期满足时才读取；命名空间或 root 视图失败时可保留这项独立摘要检查结果，但仍拒绝构造可信来源。参数只在 UID、已审计 ELF 和 PID 代次通过后于内存解析。任一生产进程门失败，数据库及其后续依赖阶段均不执行，不为收集诊断继续打开已拒绝的来源。

原始 argv、完整 cmdline、环境、`-d` 值、cwd、密钥、签名和文件原文均不进入 JSON、缓存或页面；目录路径只沿用既有可信候选的展示边界。阶段与原因代码也不由异常文本或请求参数拼接。PID／文件身份变化时清除该代观测或明确标记 `identity_changed`，不拼接两代字段。诊断继续保留原先任一进程发现失败就拒绝整批发现的语义，不把其他候选提升为可执行。

超时和撤权沿用既有错误响应，丢弃本轮新私有结果；服务关闭后旧请求不能在再次启动时复活缓存。后续应用／撤销仍重新检查当前来源、授权与真实 FD，不把诊断缓存当作放行凭据。检测不安装、重建或撤销触发器，也不执行写库规则。

普通 I/O 或 UTF-8 读取异常仍保留原 HTTP 503 语义；所有者授权仍有效时，将固定安全阶段缓存下来供后续 GET 查看，清除可执行候选，不把异常原文放进响应或诊断。扫描目录完整性与 `comm` 可读完整性分别记录；进程退出或属性不可读不会被描述为所有字段都已核验。同库多个进程的进程门错误只归属实际失败的 PID，数据库阶段才关联共享该库的进程组。

## 验证状态

本功能配有原始 SQL 合同、SQLite 事务／并发插入回归、后端备份／权限检查、真实 HTTP 授权与撤权回归，以及独立浏览器交互检查。Linux 文件句柄检查以 Linux CI 为准；Windows 不具备的检查标记 skip，不冒充通过。

进程换代回归在已捕获初始身份后的首次非空 ELF 读取处触发，不依赖授权 `guard` 的调用次数。共享全局 `os.read` 也承接 proc 读取，因此测试补丁须先按 FD 的设备号与 inode 筛选 ELF，其他 FD 原样返回，不能在 comm／stat 尚未完成时断言初始身份。便携回归保留原 `vendor_runtime._read` 的真实 proc 读取，证明前面的 comm／stat 不触发换代，ELF 才触发；有／无诊断 observer 均须拒绝该来源。SQLite 连接记录器透明转发数据库位置与 `uri=True`，首参不得与 SQLite 关键字重名。便携回归检查真实只读 SQLite 连接与 `query_only`，其中可执行文件 FD、URI 定位和 SQLite FD 枚举是明确的合成边界，不能替代 Linux 的真实 no-follow／SQLite FD 全链验收。`7cd1563` 的 Linux CI 暴露了上述两项测试夹具问题；它们不构成 NAS 身份失败的证据，修复后的 Linux 结果仍须独立核验。

生产来源与数据库核验全部通过后，已经存在相同原始规则的 NAS 会识别为“已启用”，不会重复安装、重新 seed 或升级。

2026-10-31，NAS 已运行 `sha-d9a9639d0902cd1cc25f6586a958b4cfbf976db5`。一次显式 probe 返回 HTTP 200、`unavailable` 与空候选，且未达到 12 秒超时；结合该版本发现流程，只能定位为发现／进程身份阶段拒绝，尚不能确定是 UID、命名空间、ELF、参数或其他具体进程门。该结果不证明数据库、原始 INSERT 或触发器规则失败，也未执行应用／撤销。新增固定阶段诊断尚待实机验证，不能将其实现等同于根因已经查明。

真实新一集是否入库、电视端“最近添加”是否实际前移仍未完成验收；本文不把合成 SQLite 结果当作实机或电视端通过。
