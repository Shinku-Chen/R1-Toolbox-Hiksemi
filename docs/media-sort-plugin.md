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
| `POST /api/plugins/media-sort/probe` | `{}` | 显式发现并核验候选。 |
| `POST /api/plugins/media-sort/apply` | `candidate_id`、`version`、`confirmed:true` | 已选候选的备份、安装原始规则。 |
| `POST /api/plugins/media-sort/revert` | `candidate_id`、`version`、`confirmed:true` | 撤销精确识别的对象，不回滚媒体数据。 |

这些接口仅接受当前网页所有者：默认 `password` 模式尚未设置口令时与其他网页管理一致直接可用；启用口令后需登录，显式 `pair` 模式需配对。保持真实来源、同源、JSON 与 `X-FNM-Request: 1`／配对 CSRF 校验。Bearer Token、MCP 和开发者白名单不能管理此插件，不能把已有原厂登录 Token 转成插件授权。

后台限定已审计 `scraper` ELF、进程启动生命周期、命名空间与根视图，并逐级 no-follow 打开数据路径，核验真实 SQLite 主文件句柄、辅助文件、属主／权限、完整表结构与 JSON1。来源不匹配、路径替换或未知结构时拒绝操作，不修改原厂目录权限。候选路径和备份位置只返回所有者，不进入公开快照、小屏、MCP、默认诊断或审计参数。

应用、撤销在执行、提交和返回前重复核验来源及所有者授权。提交后断线或撤权仍可能已经完成数据库事务，应重新检测，不能按错误响应断言未写入。服务停止只关闭授权与后台操作，不自动撤销已提交规则。

## 验证状态

本功能配有原始 SQL 合同、SQLite 事务／并发插入回归、后端备份／权限检查、真实 HTTP 授权与撤权回归，以及独立浏览器交互检查。Linux 文件句柄检查以 Linux CI 为准；Windows 不具备的检查标记 skip，不冒充通过。

已经存在相同原始规则的 NAS 会识别为“已启用”，不会重复安装、重新 seed 或升级。真实新一集是否入库、电视端“最近添加”是否实际前移仍未完成验收；本文不把合成 SQLite 结果当作实机或电视端通过。
