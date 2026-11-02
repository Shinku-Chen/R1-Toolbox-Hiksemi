# SMART 按需刷新与缓存

Toolbox 在无人查看相关页面、且没有已启用的风扇硬盘温控需求时不主动采集磁盘温度或容量。启动、库存、任意 GET、被动诊断和健康评估只消费合法缓存，不因缺测自动探盘。Web 进入存储页、磁盘详情或恢复显示该视图时显式刷新一次；概览和本机小屏仍只补缺测。实际启用的风扇策略可只为所选硬盘维持温度缓存。所有自动采集均普通 `-n standby`，休眠跳过，绝不自动 force。缺失、过期、设备代次不符或失败保持 `null`／`unknown`／`--`，不能把未知解释为健康、低温或休眠。

## Web 磁盘视图进入刷新

所有者固定 `POST /api/r1/storage/refresh`，严格 `{}`，沿用同源／JSON／CSRF、实际 owner 代次及响应序列化前后守卫；拒绝 Authorization、URL 参数和所有额外字段，不接受 force、view、设备或路径。本机令牌、开发白名单、Bearer 与 MCP 不获得该权限，也没有新的小屏刷新路由。

Web 进入存储页、打开磁盘详情或从 hidden 回到该视图时显式发送一次。即使 SMART 缓存仍新鲜，也重新普通采集温度和状态；同代待机／失败等负结果可在三十秒内复用，避免快速切页反复探盘。离页或隐藏后不发新请求；失败和 busy 不排队、不重试，服务器不建立页面租约或后台刷新任务。

完成回执与下述 fill 的 nested smart／capacity 结构相同，顶层 `force:false`。本次容量是否可测，由服务端绑定本次私有 SMART 收据与当前设备代次的依据决定：必须独立确认非待机，不能把 `standby:null`、单纯 refreshed 或历史成功视作证据。符合时重新测量已有容量快照；待机返回 `skipped_standby`／`standby`，电源不明返回 `unavailable`／`power_state_unverified`，均不执行 statvfs。合法旧容量可保留原值和原 sampled_at；这不代表本次刷新成功，也不延长旧温度／健康鲜度。换盘仍丢弃旧值。多段采集不是可回滚事务，不能用后段失败宣称前段未发生。

## 概览和小屏缺测补齐

网页固定 `POST /api/r1/storage/fill`，严格 `{}`，要求当前所有者和同源／JSON／CSRF；登录开启却没有有效会话时只能展示缓存。本机小屏固定 `POST /api/storage/fill`，使用原独立 `X-R1-Token`、本地 Origin 和服务生命周期，不借用网页登录授权，令牌不发给 Web。两条路由都拒绝 force、盘名、路径、命令及 URL 参数。

所有者可见的 `/api/r1.screen_fill` 只返回最近八条小屏补齐／预检事件的来源类别、令牌是否出现与是否匹配、结果和固定原因；不保存令牌、请求头、请求体、路径或 peer。它只用于区分“小屏未发出请求”“预检／来源被拒”“令牌不匹配”和“容量采集已开始”，不授予写权限，也不改变 GET、轮询或采集频率。

Web 以 `GET /api/auth/session` 的布尔 `owner_authorized` 判断是否显示自动补齐资格；该字段只委托当前 `panel_authorized` 的 password／pair／open 结果。不能用 `authorized && !dev_mode` 推断所有者：开发窗口可与正常所有者同时开启，pair 也可能在旧 authorized:false 时有效。旧服务没有此字段时，仅 logged_in:true，或 auth_mode:password、auth_required:false 且 authorized:true 可作兼容提示；旧 pair 无明确证明时只显示缓存。提示不授操作权，POST 仍独立检查 Bearer、同源／JSON／CSRF、当前授权和服务代次，撤权后旧提示不能继续使用。

上述 fill 只在概览或小屏磁盘容量／温度相关页面可见且缓存缺失时请求，每个可见周期最多一次。已有有效数据不重复请求；离页、Web 隐藏或屏保后不发新请求。休眠、不支持、失败和 busy 都结束本周期自动尝试，不因两秒 GET 或重绘循环重试。Web 存储页和磁盘详情改用前述 refresh，不受 fill 的正缓存命中规则限制。服务器没有无人查看仍运行的页面租约采集器。

完成回执形态：`{ok:true,force:false,status,started_at,finished_at,smart:{ok:true,force:false,status,started_at,finished_at,results:[...]},capacity:{ok:true,status,started_at,finished_at,results:[...]}}`。两段保留各自固定行结构，SMART 行标签为 device，容量行标签为 mount_id。有效命中增加 `status:cached`、reason:null 和原 sampled_at；负结果同代三十秒复用原状态／原因／采样时间，不能伪造新采样。普通 SMART 的 `skipped_standby`／`standby` 是跳过；容量缺测在待机时同样跳过，无法取得可信非待机证据则 unavailable／power_state_unverified。两段任一 partial 则总状态 partial；ok 仅表示批次处理完毕。

这些前台路径只发布用途需要的测量字段，不扩大完整序列号或 SMART 原文权限。它们与手动及温控共用来源锁；忙碌 HTTP409 reason:busy，不排队重试。采集、提交与响应序列化保留真实授权和第一次撤权锁存；多个来源不是可回滚的跨设备事务，某段合法发布后下一段失败不能假称本轮从未采集。

## 条件性风扇温度需求

只有已保存、实际启用的 temperature 通道及其 active_config 所选硬盘来源形成条件性后台需求；仅保存待应用配置不授权采集。需求绑定当前策略代次和真实选择，采前及发布前重验；来源修改、释放、停止、设备换代或撤销即拒绝旧结果。不使用无条件 owner 谓词，不把页面可见性当温控授权。

一个稳定来源必须唯一对应一个当前库存设备；重复 WWID／序列号造成歧义时整组拒绝，不能轮流或同时采集。异步任务在调度时就绑定采集器代次，停止再启动后，尚未运行的旧任务也不能取得新代次继续采集或发布。

冷启动的旧 SMART serial 来源只在当前生效风扇需求下，依据当前同代合法元数据唯一匹配后恢复；用途映射只供温控目录消费，不改变公共 stable_identity 或完整 SN 授权。同代已有身份缓存与元数据冲突时拒绝，证据缺失不靠 SMART 扫描找盘；采集前、发布前及目录消费时仍复核策略和磁盘生命周期。详细恢复边界见 [风扇策略](fan-policy.md)。

只对所选盘在有效缓存缺失／到期时使用固定普通 SMART；未选盘零温控命令，不刷新容量。任务异步、有界且共用 SMART 锁，负结果短暂复用，不在每秒控制循环堆积任务。休眠跳过，缺测／过期仍执行原缺测和 CPU 过热保护，不续旧样本、不伪造温度或切换来源来解除保护。自动采样只消费必要测量字段，不扩大完整 SN 授权。灯光策略本身不新增后台采集授权。

## 所有者显式刷新

网页存储页提供普通刷新及单次强制刷新。普通刷新默认发送 `force:false`；强制刷新须由用户明确选择并确认可能唤醒休眠硬盘，只对当次请求有效，不保存为设置、不自动重试。

```javascript
const response = await fetch('/api/r1/storage/smart/refresh', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-FNM-Request': '1'},
  body: JSON.stringify({force: false})
});
console.log(await response.json());
```

请求体必须恰好是 `{"force":false}` 或 `{"force":true}`，布尔值必填；缺省、整数、字符串、额外的设备／路径／命令／时限参数均拒绝。服务端选择当前已核验的物理磁盘，不接受调用方指定磁盘或命令。

| 本次模式 | 固定命令形态 | 边界 |
|---|---|---|
| `force:false` | `smartctl -n standby --json=o -a` | 休眠跳过；设备不支持可靠待机保护时保持不可用，不自动改用强制模式 |
| `force:true` | `smartctl --json=o -a` | 仅本次省略 `-n standby`，可能唤醒硬盘；不启动 SMART 自检或更改设备配置 |

`-n standby` 是普通刷新的保护方式，不是对全部设备／驱动组合绝不唤盘的保证。强制模式不改变后续网页、小屏、后台或健康评估的缓存语义。

`--json=o` 仅把同一次命令已有的文字保留在私有 JSON `smartctl.output[]`，不新增命令或设备查询。自动设备识别、设备打开及驱动行为仍可能唤盘；事后没有取得可信电源依据，不能倒称命令没有读盘。`-g power` 不是合法选项，本实现不使用它，也不为补电源状态追加查询或重试。

命令拼写必须为长选项 `--json=o`。[官方选项声明](https://github.com/smartmontools/smartmontools/blob/0c0bada5eedb759c9d7bb50ff54959cf0809c5a9/smartmontools/smartctl.cpp#L313-L357) 的短选项 `-j` 不接参数，只有长选项 `json` 接可选参数；先前候选 `-j=o` 会把 `=` 当作非法短选项，在打开设备前失败。该候选已废弃，不能把此前 mock runner 通过当作命令可用证据；永久合同测试使用真实 GNU getopt 解析实际生产参数，并保留错误拼写被拒绝的反例，不执行 smartctl。

原始文字可能包含序列号、设备路径和自由文本，只在 `_probe_smart` 局部有界解析，并在首次复制 JSON 前删除；所有者完整详情、强制刷新、温控测量、失败结果和内存缓存均不保存或返回该数组。仅内部固定电源枚举可进入已有同代收据，不能把原始输出放进诊断或日志。

## 授权、并发与生命周期

接口仅授予当前所有者网页授权：已设置口令时使用有效会话，首次无口令沿用所有者网页的开放模式。独立 Bearer Token、MCP、小屏令牌和开发白名单均不获得刷新权限。仍须同源、JSON 与 CSRF 证明；它没有 GET、被动检测或机器工具入口。

刷新由 SMART 来源锁 `_smart_refresh_lock` 串行执行，缓存读写共用 `_lock`；不宣称它与所有原厂空间查询共用一把锁。并发忙碌返回 HTTP `409`、固定 `reason:"busy"`，不排队重复探测。此入口不调用存储 IPC，也不主动唤起磁盘库存工作线程。每批共用三十秒绝对单调时限，每个命令至多三秒且不超过本批剩余时间；不因下一块盘或模式切换续期。每块盘采集前后复核真实设备生命周期；设备改变的结果不写入新设备缓存。读取中逐步及发布前复核所有者授权，撤权后停止并丢弃整轮结果，不保留部分私有响应供随后授权恢复时发布。

## 固定回执

HTTP `200` 表示这批操作已处理，不代表每块盘都已取得 SMART 或健康正常：

```json
{
  "ok": true,
  "force": false,
  "status": "complete",
  "started_at": 1793419580.0,
  "finished_at": 1793419581.0,
  "results": [
    {"device": "sda", "status": "refreshed", "reason": null, "sampled_at": 1793419581.0},
    {"device": "sdb", "status": "skipped_standby", "reason": null, "sampled_at": null}
  ]
}
```

示例只展示合同，不是 NAS 实测。`started_at`、`finished_at`、`sampled_at` 均为 Unix 秒；`sampled_at` 可为 `null`，仅有观测时刻不能证明取得有效 SMART，仍须结合逐盘状态与新鲜度。回执不包含 SMART 原文、序列号、完整设备路径或异常文本。

`status` 为 `complete` 或 `partial`；逐盘状态仅为：

- `refreshed`：取得并绑定当前设备生命周期的有效样本。
- `skipped_standby`：普通刷新按待机保护跳过；这不是失败，单纯跳过不把整批标成 `partial`，也不证明所有磁盘物理停转。
- `failed`：本次采集失败，`reason` 仅为固定安全原因。
- `unavailable`：当前工具、设备或保护能力不可用，不自动提升为强制访问。
- `identity_changed`：前后设备身份不一致，结果拒绝发布。

## SMART 电源状态格式

非待机资格读取 smartmontools 官方 `power_mode.name`，并兼容旧 `power_mode.string`。官方 7.5 的 [JSON 写入与 ATA 对照表](https://github.com/smartmontools/smartmontools/blob/f276b249fdc84019403491303479485030ac7c80/smartmontools/ataprint.cpp#L3400-L3449) 同时输出 `ata_value`；该数值是 ATA 电源枚举，不是命令退出码。两个文本字段同时存在时必须表示同一规范化状态；存在数值时也必须与文本精确对应，不能选择一个字段掩盖另一个字段的矛盾。

旧版文字兼容只用于已审计的正式 7.0–7.4 版本、明确 `device.protocol:ATA`、正式电源字段缺省或空对象且私有输出中只有一条精确工具电源行的情况。[7.4 ATA 源码](https://github.com/smartmontools/smartmontools/blob/0c0bada5eedb759c9d7bb50ff54959cf0809c5a9/smartmontools/ataprint.cpp#L3355-L3413) 在 `-n` 前置检查取得电源模式，随后 [drive_info 输出](https://github.com/smartmontools/smartmontools/blob/0c0bada5eedb759c9d7bb50ff54959cf0809c5a9/smartmontools/ataprint.cpp#L3739-L3745) 的 `Power mode is:    ACTIVE or IDLE` 或 `Power mode was:   IDLE`／`IDLE_A`／`IDLE_B`／`IDLE_C` 可保留这次检查的依据；`was` 不表示测量结束时再次核对过电源。单词 `ACTIVE` 不是这些 ATA 分支的正式文字，不能据此授予容量。版本数组必须精确为 `[7,0]` 至 `[7,4]`，不自动接受补丁版、开发版或未知后续版；7.5 的正式结构化 JSON 路径继续独立校验。

官方 [7.0 的 CLI](https://github.com/smartmontools/smartmontools/blob/51e8d5e8ffb4039e0ef4b4f72323b87c7ef5217a/smartmontools/smartctl.cpp#L1106)、[7.1](https://github.com/smartmontools/smartmontools/blob/249b38c426330f5d267c8a9895d1ede591ee8519/smartmontools/smartctl.cpp#L1106)、[7.2](https://github.com/smartmontools/smartmontools/blob/59efad4c0000cf40e3441acdc8b0ee58adac7a11/smartmontools/smartctl.cpp#L1115)、[7.3](https://github.com/smartmontools/smartmontools/blob/ade59e8d9460e5e2b057855c6858d557dd4935ad/smartmontools/smartctl.cpp#L1136) 至上述 7.4 CLI 均将 `--json=o` 的既有文字放进 `smartctl.output[]`。7.0–7.2 的部分忽略保护警告只使用普通文字输出，不能只检查 `messages[]`；本解析同时检查有界私有文字，并删除原文。正式电源字段已存在但未知、空文字或畸形时，不用旧版文字覆盖它。

文字与正式字段必须一致；多行、矛盾、未知、畸形、超限、未审计版本或非 ATA 协议都不能由文字获得容量资格。SCSI 的默认 ACTIVE、NVMe 和过时 NV 模式不套用 ATA 依据。私有文本最多 1024 行、每行 1023 字符、合计 131072 字符；拒绝嵌入换行、NUL 和其它控制字符。保护不支持、忽略 `-n` 等文字会否决普通采集依据。合法温度／健康与容量资格仍独立，文字缺失或无资格不能把未知电源当作活动；强制模式始终不产生普通非待机证明。

- `IDLE`／`IDLE_A`／`IDLE_B`／`IDLE_C` 分别对应 `128`／`129`／`130`／`131`，`ACTIVE or IDLE` 对应 `255`。这些状态在固定 `-n standby` 下允许继续；原有 `ACTIVE` 文本也保留，但不为它猜测 ATA 数值。
- `SLEEP`／`STANDBY`／`STANDBY_Y` 分别对应 `-1`／`0`／`1`，只作跳过依据，不授予容量采集权。数值单独出现不足以确定状态；特别是 `-1` 不能单独证明睡眠。
- 过时的 `ACTIVE_NV_DOWN`／`ACTIVE_NV_UP` 不在容量白名单内。缺字段、未知、畸形、冲突或任何其它数值均不获得容量资格；不能把 `standby:null` 或普通 `refreshed` 当成非待机。合法温度／健康与容量资格独立，缺少电源状态不自动抹掉有效测量。

同样的明确证据约束用于普通手动采样后供缺测补齐消费的资格，不以旧记录的成功状态代替本次解析。显式强制采样仍不产生普通待机保护证明。根据官方 [低功耗选项说明](https://github.com/smartmontools/smartmontools/blob/f276b249fdc84019403491303479485030ac7c80/smartmontools/smartctl.8.in#L901-L936)，检查不支持或状态未知时 smartctl 默认可能忽略 `-n` 并继续；本地保留对 unsupported／ignored 等消息的拒绝，不自动强制或重试，也不能因为解析后拒绝就倒称原命令没有读盘。退出码 `2` 同样可能来自打开／识别失败，不能单凭退出码判待机。

## 新鲜度与使用范围

SMART 测量值、健康和温度只在采样后至多三十秒且设备生命周期仍有效时使用。GET、缓存命中、库存刷新或健康评估不会更新时间戳；只有新的合法采集才产生新样本时刻。过期后可保留历史采样记录，但当前测量／健康仍为未知，不能把历史值重新包装成新样本。Web 进入刷新失败时，也不能继续把此前仍新鲜的成功测量冒充本次状态。

磁盘库存的一整行也绑定同一生命周期：读取内核 size、介质类型、型号及其它元数据前先捕获可信设备代次，行采样结束及发布前再次核对。采样中或两轮之间换盘时丢弃旧行，等待下一次正常元数据采样；不能把旧盘的温度、型号或名称归到同名新盘。磁盘 hwmon 温度和阈值不再被后台、库存或温控读取，包括 nvme、drivetemp 与关联磁盘的别名；磁盘温度只来自合法 SMART 缓存。CPU／主板传感器和风扇仍采集。

独立温控目录也按三十秒上限拒绝 SMART 旧值，不把网页／库存时间当新测量。必要来源失效仍触发既有缺测保护；启用硬盘温控不保证待机盘持续提供温度。已核验的独立磁盘身份缓存与测量值不同，保留其原有设备生命周期与冲突门，不延长温度或健康寿命。

挂载容量是另一份独立的[容量快照](capacity-refresh.md)。所有者手动容量刷新会读取文件系统元数据，可能唤盘，不具备普通 SMART 的待机保护；前台缺测补齐则先要求同代普通 SMART 非待机依据。二者都不能保证采样后电源状态保持不变。

历史和报告只消费有效已有样本，缺测形成断点。停止 Toolbox 自动 SMART 查询不代表 NAS 的其它原厂服务、应用、历史数据卷写入或用户访问均无 I/O，也不构成实际待机／物理停转证明；休眠排障仍使用固定的 [disk_activity](disk-sleep-diagnostics.md)。

本轮实现与隔离测试不替代新版本在用户 NAS 上的普通／强制刷新、授权撤销和实际休眠验收。
