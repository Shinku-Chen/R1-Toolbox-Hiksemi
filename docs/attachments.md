# 附件功能与协议审查

本文保留首次附件的静态审查记录。2026-10-28新增两份HTML、fnMonitor参考及已实现功能的最新对照见[需求核对](requirements-audit.md)；灯光自动识别见[灯光策略](light-policy.md)，公开监控和五位配对见[API说明](public-api.md)。

审查时间：2026-10-27。来源为用户提供的 reference HTML、R1 Panel 1.1.7 RAR 与 R1 Toolbox 4.8.10 FPK。只静态读取、核对协议与用户可见功能；未执行附件安装脚本或二进制，未分析或导入授权模块，未复制其代码、界面或素材进入本项目。

## HTML 属于哪个服务

用户 HTML 与 R1 Panel 包内 app/www/index.html 完全一致，SHA-256 为 ac73b64bf7517fa1088c683e6cdc4697103a290227c4e927aaeb33c524188bc0。它是正式管理前端，真实 fetch 集中在第 2271 行，主数据轮询在第 5617、5638–5651 行。不是模拟原型。

但它依赖的是**自己安装包中的 server.py**，不是海康 NAS 官方 HTTP API。仅将此 HTML 挂载到 HistorSub，页面不会自动拥有它的 /api/overview、/api/panel、/api/fans 等后端。本项目用自有小屏布局和后端实现可用功能。

## R1 Panel 1.1.7

下列证据路径相对附件解包根，行号仅用于说明本次版本。

| 功能域 | 真实入口和实现 | 对官方系统的含义 |
|---|---|---|
| 总览/CPU/分核/内存/负载 | app/server.py:416–583、api_overview:10305 | 通用 /proc 可独立实现 |
| 所有温度/RPM/电压 | collect_sensors:2391、_hwmon_fans:2602 | 优先通用 sysfs；不是仅依赖IT87 |
| 风扇控制 | app/panel/fanctl.py:320、:428 | 可写控制仍依赖 hwmon PWM，不提供官方 subsystem 适配 |
| GPU/Intel引擎 | collect_gpu:2231、IntelGpuPmu:1903 | 需对应 sysfs/PMU/权限，不能保证当前核显有全部指标 |
| 功率/能耗 | get_rapl_power:1390、estimate_power:1439 | RAPL 是CPU域；整机估算必须标记 |
| 磁盘/休眠/SMART/巡检 | collect_disks:641、power_states:1519、DiskScanner:3272 | SMART读取与完整巡检系统是两个层次 |
| RAID | collect_raid:2810、collect_raid_card:1227 | 软件阵列可读；硬RAID依赖storcli/perccli |
| 网卡/端口/进程 | collect_net:5705、collect_ports:886、top_processes:11565 | 标准 /proc/sysfs 可适配 |
| Docker/虚拟机 | collect_docker:6107、VmMonitor:6463、vm_action:6640 | 宿主有对应服务才显示和开放操作 |
| fnOS应用/媒体统计 | collect_app_stats:6922、collect_modules:7254 | /var/apps 与飞牛数据库不能套到官方系统 |
| 历史/导出/报告/日月统计 | History:7337、daily:11143、report:11338 | 需要独立持久存储，不把短时趋势宣传为完整历史 |
| 天气 | fetch_weather:6831 | 外部联网功能，不是设备接口 |
| 通知 | notify_mail_send:3494、notify_sct_send:3537 | 需用户自己的SMTP/SendKey，不复用附件配置 |
| iDRAC | server.py:4528起 | 外部Dell带外管理，不是R1本机风扇接口 |

主要读取路由包括 /api/overview、/api/smart、/api/disk_health、/api/scan_log、/api/docker、/api/vms、/api/modules、/api/processes、/api/history、/api/system、/api/apps、/api/ports、/api/hardware、/api/sensors、/api/fans、/api/raidcard、/api/power、/api/traffic、/api/power_stats、/api/gpu、/api/memory、/api/weather、/api/daily、/api/monthly_summary、/api/report 和 /api/export。控制路由是 /api/docker/action、/api/vm/action、/api/panel/save、/api/panel/cmd 等。它们均属于附件服务。

面板配置 app/panel/config.py:20–53 包含页面排序/轮播、触摸方向、亮度/空闲息屏、跨午夜夜间亮度、主题/字号、CPU/HDD独立模式和曲线、RGB颜色/模式/亮度、定时屏幕及RGB。面板自己的IPC是 panel.json、panel_status.json、panel_metrics.json、panel_cmd.json，app/server.py:10056–10095负责与其守护进程通信，不是官方IPC。

## R1 Toolbox 4.8.10 FPK

| 功能域 | 入口 | 所需条件 |
|---|---|---|
| RGB/定时灯光 | app/web/routes/device.py:19–168，/api/device/rgb 与 /schedule | 自带服务、I²C芯片和配置监控 |
| 背光/息屏/定时 | 同文件:169–315，/timeout、/brightness、/schedule | backlight/blank，独立调度逻辑 |
| 轮播 | :316–353，/api/device/scroll_interval | 自带面板程序 |
| 风扇模式/曲线 | :354–429，/api/device/fan 与 /curve | IT87 hwmon/PWM；不是海康官方策略接口 |
| CPU/HDD/RPM监控 | :458–548，/api/device/fan/realtime | 各自的真实传感器来源 |
| 主题编辑与包 | app/web/routes/theme.py:22–119 | list/current/resources/package/save/apply/delete，自有 .theme zip |
| 本地音乐/歌词/声卡 | app/web/services/music.py:112–146；app/media/* | mpv IPC、音频输出、MPRIS/AVRCP、媒体库 |
| 飞牛音乐代理 | music.py:202–238，/api/music/fnos/* | 飞牛音乐登录/刷新令牌/播放列表/音频流 |
| 天气/城市 | app/web/routes/weather.py:22–25 | 心知天气与API Key，不是官方硬件接口 |
| MAC修复 | cmd/install_callback:21–159 | 修改NetworkManager并重连网口；针对刷飞牛后的问题 |

此包默认飞牛网关 /app/r1toolbox →应用 Unix socket，app/web/server.py:645–701可启用TCP调试回退。TRIM_APPDEST/TRIM_PKGETC 等环境与 /var/apps 是飞牛应用框架约定，不应直接要求官方固件具备。

## 风扇、RGB与测量单位

| 项目 | 查到的事实 | 实现注意 |
|---|---|---|
| 风扇PWM | 0–255占空比，附件常用低85/中170/高255 | 不是RPM；附件同时注释PWM90会停转，不能照搬低档 |
| RPM | fanN_input，单位RPM | 0应保留，不能等同未知或隐藏故障 |
| CPU/机箱映射 | Panel fanctl.py:51–59标pwm3=CPU/pwm2=HDD；collector.py:31–33却标fan2=CPU/fan3=机箱 | 附件互相矛盾，不按编号推断物理角色 |
| 温度 | hwmon/thermal通常为m℃，换成℃；sensors JSON已使用工程单位 | 不盲目再次除1000；保留驱动、标签和通道 |
| 自动控制 | CPU35–85℃、HDD30–60℃，PWM50–255，滑动平均/迟滞 | 并未在用户官方系统上校准 |
| enable | 通常1手动/2硬件自动 | 不假设主板自动曲线可靠；临时接管记录并恢复原值 |
| RGB | 地址0x26；模式0x90；R/G/B=0x92/0x93/0x94 | 模式0关/1常亮/2彩虹/3呼吸，分量0–255 |
| RGB亮度 | 附件用颜色分量乘0–100% | 这是附件实现的限制；本项目另从官方协议确认0x91独立亮度，按0–255写入，不缩放RGB分量 |
| RGB接管 | 附件大量使用i2c工具-f强制访问 | 本项目不自动强制抢占厂商正在使用的设备 |
| 背光 | max_brightness常见96000；附件40步渐变 | 用户官方机brightness=0而画面正常，不能据0推断息屏 |
| 功耗 | Panel屏幕stats.py:83–87使用8W+CPU%×0.25等估算 | 不能标成真实整机功耗或据此给精确电费 |

Panel app/server.py:2744–2755 的实验性EC邮箱采集需要向 /dev/port 的0xA20/0xA21写入选通，源于NEC/LENOVO/ODM协议。它不是纯只读，也没有海康R1兼容证据。本项目不自动执行这种探测。

FPK cmd/main:73 会尝试 modprobe it87 force_id=0x8628。官方内核存在 gpio-it87 只说明有GPIO模块，不代表存在对应hwmon驱动；本项目不执行附件脚本、安装驱动或绕过系统直接写IO。

## 官方体系的单独发现

官方控制需要另外核对。当前静态证据包括 /dev/hikio、配套 product_test 读数入口，以及 subsystem 中的固定 set_fan_status 系统风扇策略。低/中/高/自动是模式 2/3/4/5，模式 1 不开放。它不是附件的 /api/device/fan 路由，也不是 hwmon 曲线。

当前 NAS 的旧 `product_test -o` 查询返回 `cpu[-1]-sys[-1]`、HTTP 7000 连接被拒绝。继续分析发现同固件两份 hikio 不同：rootfs 版缺 0x5798/0x5799 调速，initrd 版实现这两个请求与内核 SuperIO。两份都缺旧 RPM 0x5793/0x5794，故旧测速失败不反证调速。用户读取的实际 loaded GNU note 已完全匹配 initrd 支持版，原厂 Unix GET 也已实测返回 b'5'/auto，因此可以在确认操作后开放系统风扇策略；实际风速变化尚待用户观察。未知/旧版驱动仍只读。详见 [原厂接口证据](histor-interfaces.md)。

范围进一步核实：set_fan_status 只改变 type0x99/ioctl0x5799 的系统风扇策略；CPU 的 check_cpu_fan_thread 每10秒独立写0x5798，不受 fan_mode 或系统风扇手动标志影响。本版保留原厂CPU自动，不提供独立CPU手动/曲线、不停止厂商温控服务；物理风扇对应仍需实机核验。

独立检查厂商 16 个含相同 direct SuperIO 用户态函数的 ELF，未发现外部直接 call/jmp 进入函数体或八字节函数指针引用。多个 daemon 出现 fan 字符串可由静态公共库链接解释，不能当成另一条活跃用户态控制链；这不否定 initrd 模块内部存在的内核 SuperIO 路径。init_wake 的 /proc/it86/fan 仅在 MAGE50X 分支，其他 ARM PWM 也有限定机型，不能直接套到 R1。

`get_fan_status` 也并非无条件无副作用：缺配置时可能初始化默认值并通知守护进程。本版后台仅读取已有策略字段，不调用 GET/SET/信号；匹配 loaded note 的硬件支持、服务条件和用户确认均满足后，才进入设置并验证。`/tmp/fan` 的四字节硬件级别不是 2/3/4/5 策略，也不是 RPM。

按接口归属区分能力，才能保留已有官方固件功能，并避免把刷飞牛专用修复错误应用到官方系统。当前实现范围和未提供项目见 [功能矩阵](features.md)。
