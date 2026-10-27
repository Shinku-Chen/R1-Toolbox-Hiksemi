# Hiksemi R1 原厂接口与独立接管

静态证据记录：2026-10-27；独立温控实现更新：2026-10-28。

分析对象：用户已有 R1_X01_GML_STD_V6.3.3_Build_260907.zip。本地仅静态分析，不运行固件二进制、脚本，不加载模块、不做硬件写操作。分析完整 histor.img、rootfs/sq.img，以及 initrd/rootfs 中两份不同的内核模块；另记录用户在 NAS 上明确执行的诊断结果。

## 结论与实机证据

- 原厂存在风扇模式 RPC 和 REST action；接口存在不等于当前驱动可执行物理调速。
- 用户真机 product_test -o 返回 cpu[-1]-sys[-1]，两个 ioctl 读取都失败；因此这条 RPM 路径在当前设备不可用，不能显示为 0 RPM。
- 当前 NAS localhost:7000 未监听（GET 连接被拒绝）；7000 只是固件里另一内部客户端所使用的端口，不能写死为当前 API 端口。应从 emhistor 进程拥有的 /proc/net/tcp(6) LISTEN socket 确认端口。
- 同一固件有两份不同的 hikio：rootfs 版缺 0x5798/0x5799 调速分支，initrd 版实现了这两个请求。两者都缺旧 RPM 0x5793/0x5794，不能用测速失败反证写入失败。
- 用户实际加载模块的完整 GNU note 已与支持调速的 initrd 版完全匹配；coresize=20480、状态 live。实际 Unix RPC get_fan_status 也返回精确正文 b'5'，与既有 auto 配置相符。驱动和协议已确认，部署后可由用户主动确认切换系统风扇策略；机械效果仍待观察，未宣称已经验证风速改变。
- 编译入多个用户态 ELF 的直接 SuperIO I/O 函数没有找到调用者；这不否定 initrd 驱动内部的内核 SuperIO 实现。不复制这些函数或擅自写原始端口。

## 校验身份

| 文件 | bytes | SHA-256 |
|---|---:|---|
| emhistor | 18425520 | 402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26 |
| temp_control | 2006264 | 745cd85d96fdcd1655455c652850abc49290dba5326b7f93ef2c5710e0de1719 |
| product_test | 2041784 | 29542a72ee0c06a071c772ddbf71b0cce67e62c5ebb08994fbea6b98b5c42bc0 |
| rootfs hikio.ko | 19688 | 6dafaf34fed014d1ba1dd34c79e9aa0f545f718836a99a40acc16e52562d4618 |
| initrd hikio.ko | 22880 | 27e5317a0435367da9fc1dcbf1c080a6f12857744f47a7f7f2583ff458ac9e11 |

两份模块的 vermagic 均为 6.1.66-001 SMP preempt mod_unload，license=GPL v2，无 srcversion/version 字段；路径尾部也相同。因此内核版本、文件名和磁盘上的 .ko 哈希不能区分**实际已经加载**的实现。

### 已加载模块的 GNU Build-ID

| 来源 | GNU Build-ID | .text 大小 |
|---|---|---:|
| initrd 支持调速版 | c909da338be2c4700bb00b233cb189e7a32655b7 | 4481 |
| rootfs 缺调速版 | 7a0ac48f741ff349b12b56cff270d4e1f9b1bc07 | 2577 |

对应完整 .note.gnu.build-id 各为 36 字节：

```text
initrd: 040000001400000003000000474e5500c909da338be2c4700bb00b233cb189e7a32655b7
rootfs: 040000001400000003000000474e55007a0ac48f741ff349b12b56cff270d4e1f9b1bc07
```

用户已经读取 `/sys/module/hikio/notes/.note.gnu.build-id`，结果完整匹配第一行，实际运行的是 initrd 支持版。该证据来自内核暴露的 loaded module note，不是猜测开机从哪个目录加载。coresize=20480/live 是补充状态，不能单独替代 Build-ID。

需要重新核对时，可在原 rootsh 终端只读取这一项：

```sh
chroot /host /bin/sh -c 'python3 - <<"PY"
from pathlib import Path
path = Path("/sys/module/hikio/notes/.note.gnu.build-id")
try:
    with path.open("rb") as stream:
        data = stream.read(257)
    print("hikio loaded GNU note:", data.hex() if len(data) <= 256 else "过长，无法确认")
except OSError as error:
    print("无法读取 loaded GNU note:", type(error).__name__)
PY'
```

没有 note 或出现未知值时保持只读，可将 `/sys/module/hikio/coresize`、`initsize`、`srcversion` 和 `/proc/modules` 的 hikio 行作为辅助诊断；这些值不能自动证明支持，不加载或替换模块。

## 原厂 Unix RPC 协议

服务：/var/tmp/emhistor，AF_UNIX / SOCK_STREAM。客户端必须 bind 一个可 stat 的临时 pathname socket、chmod 0700；原厂 accept 检查权限并 unlink 客户端 pathname。不要用未命名客户端冒充正常协议。

- 读取包：ASCII `emhistor;get_fan_status;0` + NUL。
- 设置包：ASCII `emhistor;set_fan_status;N` + NUL。
- get 回包：ASCII 单个十进制模式值，正常范围 2/3/4/5。
- set 成功 ACK：精确 `OK!`；并不是 `success`。失败路径为空格式时发 `xx` + NUL。
- 正常 comm_usock_sev_return 分配 128 KiB 清零缓冲，格式化内容后发送整个缓冲并关闭 socket；客户端按第一 NUL 取正文，剩余必须全零，并对总大小 / 总时长设限。
- 模式：2 低、3 中、4 高、5 自动，仅作用于系统风扇0x5799通道。CPU0x5798独立按原厂温度规则运行，不跟随此模式。1 是工厂停扇模式，不应暴露。
- 关键副作用：get_fan_status 不是无条件纯读，json_conf_get_fan_mode 会同步 temp_control 的手动/自动信号；配置缺失还会保存默认 5。后台探测不能调用。用户已明确执行一次真实 GET 并收到 b'5'；本版后续只在确认操作的协议流程中使用，核验 ACK 与 readback，不靠后台轮询维持状态。
- 存储：/tmp/nand_config/config.txt 为 JSON，全局 key `fan` 的 object 内 `fan_mode`。只读取这一白名单字段，不回传整个配置（可能有私密信息）。
- /tmp/fan 是原厂写入的 4-byte little endian int32 实际档位（1–4），不是 RPM。

### RPC / 配置证据地址

emhistor dispatch table 文件偏移 0xf2f460：get_fan_status -> 0x1130ee；set_fan_status -> 0x113056。

get 0x1130ee -> json_conf getter 0x93a89c -> fan_mode_stat_set 0xae43a0（同步信号）; 缺配置路径在 0x93a977 建默认 5 并写回。

set 0x113056 -> atoi -> json_conf setter 0x93aaf5 -> 配置保存 / 信号同步 -> 非自动时 pwm_fans_control 0xae544c。

成功字符串 0xc4e777 为 `OK!`；通用返回函数 comm_usock_sev_return 0xad1bb4。

json_conf_load("fan") 0x9341ef -> config-file reader 0xb0c31d -> 0xb0c5f4 -> 默认路径字符串 0xdd850a=/tmp/nand_config/config.txt。

## 驱动与真实执行链

product_test -o switch 0x195020 映射到 0x1af1e：

1. 0x2c87f GetSysFanSpeedBySuperio -> /dev/hikio open(O_RDWR) -> ioctl(0x5793, 0)，返回值即测速结果。
2. 0x2c781 GetCpuFanSpeedBySuperio -> ioctl(0x5794, 0)。
3. printf `cpu[%d]-sys[%d]`。

原厂 /histor/bin/cpu_temp.sh 在 CPU 超过 90℃持续 30 秒时也会运行 product_test -o 记日志。

emhistor pwm_fans_control 0xae544c -> low 0xaea5a8 / medium 0xaea5ce / high 0xaea5f4 -> SetCpuFanSpeedBySuperio(type 0x99) -> ioctl 0x5799；CPU 温控路径使用 type 0x98 -> ioctl 0x5798。

temp_control main -> pwm_fans_control 0x13b6e -> low 0x18cca / medium 0x18cf0 / high 0x18d16 -> SetCpuFanSpeedBySuperio 0x144d7(type 0x99) -> ioctl 0x5799。CPU 每 10 秒温控线程 0xfa35 -> 0x18f4f -> ioctl 0x5798。

### 控制范围：系统风扇与 CPU 必须区分

fan_mode 的切档通过 type0x99 / ioctl0x5799，仅改变原厂系统风扇策略，不能描述为“两路共同策略”。CPU 的 check_cpu_fan_thread 在 0xfa35：无条件调用温度读取0xf9af，接着0x18f4f写 type0x98 / ioctl0x5798，然后休眠10000毫秒并循环。main在0xfb7c无条件调用线程创建0xfaa5。USR1/USR2只改变主循环系统风扇自动标志0x3e952c，不控制该CPU线程；所以CPU仍由原厂自动温控。

`HistorAdapter` 的原厂策略状态继续以 `scope: "system_fan"`、`cpu_policy: "vendor_auto"` 表示这条 RPC 路径的边界。2026-10-27 的初版仅提供原厂系统风扇策略；2026-10-28 起，另有白名单 RAM 适配层与独立监督支持 CPU／系统分别接管，不将 CPU 配置偷换成 fan_mode。cpu/sys 是程序通道名，物理风扇对应和机械效果仍需实机核验。通用 hwmon/PWM 则是第三条有条件的后端。用户操作见 [独立温控指南](fan-policy.md)。

### CPU 每 10 秒调速在哪里，能否单独关闭

原厂 `/histor/bin/init.sh` 第 156–164 行将 `/histor/bin/temp_control` 放到后台。它是编译后的 ELF，并非一个可以直接编辑的 Python 或 shell 温控脚本。以下名称来自静态识别，地址对应本文 SHA256 的固件样本：

1. `temp_control` 的 main 在 `0xfb7c` 调用 `0xfaa5` 创建 CPU 温控线程。
2. CPU 循环 `check_cpu_fan_thread` 在 `0xfa35`：读取温度 `0xf9af`，调用 CPU 调速 `0x18f4f`，等待 10000 毫秒，再次循环。
3. 调速通过 `/dev/hikio` 的 `ioctl(0x5798, 整数PWM)`，不是系统风扇的 `fan_mode` 配置。

已核对的 `fan_mode`、USR1、USR2 没有单独停用 CPU 线程的功能。停止整个进程虽然能停止这次 CPU 写入，但它同时运行 `disk_autosleep` 硬盘休眠线程（main `0xfc49` 创建线程 `0xf2d7`），因此不能停止后弃用。该静态结论只说明没有找到可直接使用的原厂 CPU-only 开关；当前独立接管会重启同一原厂程序加载窄适配层，保留磁盘线程及相关请求，交接会重建休眠计时。

2026-10-27 提出的 RAM 动态符号适配候选，已在 2026-10-28 实现为受监督、短租约的独立接管。其静态依据未改变：当前样本动态导入 `ioctl@GLIBC_2.2.5`，CPU／系统写点 `0x14581/0x145e9` 均经 PLT `0xe580`。当前实现只在确认身份的 `/dev/hikio` 上处理被选通道的 `0x5798/0x5799`；未选通道和其他 ioctl 透传，短租约丢失或无效时请求安全高档，不靠更快写入与原厂周期线程争抢。适配层启动后清除注入环境，避免传播到磁盘休眠等 exec 子进程。

实际接管先保存原厂进程身份、argv/env、目录及既有策略，再由独立 systemd 监督服务重启同一 `temp_control`，核验适配层已加载并取得请求输出的监督回执。原厂二进制、启动脚本和模块不被替换；不重启 `emhistor`，不为接管持久修改原厂 `fan_mode`。恢复时先请求安全高档，再恢复原厂执行者并同步已有策略；自动策略的下一次调整仍由原厂采样周期决定，不宣称恢复不可读的旧 PWM/RPM。

`emhistor` 仍可能在未改变 `fan_mode` 时直接稀疏写入系统风扇。监督循环约每秒先核对模式和执行者，再重申目标；同模式外部写入仍存在一轮短暂竞态。检测到原厂模式变化时自定义控制让回；请求成功与“active”均不是 PWM 或 RPM 读回。原厂恢复未确认会保留错误和重试入口，不能因自定义停止、mode=vendor 或 active=false 就写恢复成功。

本地验证使用假执行者、内存原生适配器、真实本地 HTTP/临时数据卷，以及 Linux ioctl 替身；未在用户 NAS 上验证新接管的物理调速、风扇对应或所有运行时竞争。规则与持久化的完整语义以 [独立温控指南](fan-policy.md) 为准。

rootfs hikio.ko 的 hikio_ioctl 0x620–0x9f5 根据 type 0x57、低 byte 分发；0x93/0x94/0x98/0x99 落不支持路径。这条结论仅属于 rootfs 那份，不能代表 initrd 或本机实际 loaded 模块。

initrd hikio_ioctl 从 0x5a0 开始：0x719 比较 0x98，进入 0x766；0x8ff 比较 0x99，进入 0x961。handler 调用 0x470 hik_chip_id，识别 0x1502/0x8613，并在内核中执行 SuperIO 操作。0x96/0x97 温度请求也有分支，但 0x93/0x94 旧 RPM 请求没有。因此旧 product_test -o 失败与本机支持调速可以同时成立。以上仅静态阅读，没有执行任何调速 ioctl、iopl 或端口读写。

上层策略保存成功后，pwm_fans_control 没有将底层调速返回值传播给 RPC ACK，且会继续记录 /tmp/fan 档位。故 OK!、fan_mode 和 /tmp/fan 只能说明协议/配置状态，不能单独作为机械效果证明。

fan_control_r1_speed_change：emhistor 0xaea61a、temp_control 0x18d3c。iopl(3) + SuperIO index/data port 写，sys PWM 低/中/高值 0x3c/0x78/0xff。独立以共同的 44 字节前缀定位厂商 bin 中 16 个 ELF 的同函数拷贝，未发现外部直接 call/jmp 进入函数体或 8-byte 函数地址引用，不能仅凭多个 daemon 带 fan 字符串认定另一条活跃用户态控制链。这里讨论的是用户态函数，不是否定 initrd 模块已实现的内核路径。不要在实机直接执行。

init_wake.sh 中 /proc/it86/fan 仅在 MODLE=MAGE50X 分支，ARM 的 1c000000/980070d0 PWM 也限定其他机型，不能直接套用 R1。当前有效候选仍是已加载支持版 hikio 与原厂 daemon 的现有链路。

## REST action（存在性证据；认证和实际监听须实机确认）

路径 /rest/1.1/config，通过 action 参数 dispatch，原厂中间件管理 access_token / 用户权限。不要凭空生成凭证、不要把 token 打到屏幕或日志。

| action | handler / 输出 |
|---|---|
| get_hw_resource_info | 0x242824；cpu,memory,temperature,fanspeed,fanmode,upload,download,cpu_temp,disk_manage |
| get_hardware | 0x23c0e0；fan_mode 等元数据。调用有副作用的 getter，勿后台探测 |
| set_hardware | 0x23c00b；fan_mode query string。缺值返回 -2003；内部配置 setter |
| set_power | 原厂整套停业务/卸载阵列/关机流程；未调用，mode 数值未验证 |
| get_atmo_light / set_atmo_light | 灯光模式。getter 可 modprobe i2c-dev，非完全纯读 |
| get_atmo_color / set_atmo_color | color_r/color_g/color_b |
| get_brightness / set_brightness | screen_brightness；get 也可能默认初始化 |
| get_screen_switch / set_screen_switch | 息屏与唤醒 |
| get_off_time / set_off_time | 息屏时间 |
| get_screen_theme / opt_screen_theme | 主题安装、切换 |

注意：一些原厂温度 getter 在 ioctl 失败时回退固定 40℃；不要把这个兜底值当实测。当前 toolbox /proc、/sys 温度采集应保持为首选。

## RGB 独立接口证据

原厂I2C芯片0x26，身份0x01应为0x55；模式0x90、独立亮度0x91、RGB 0x92／0x93／0x94。进一步静态追踪发现，`emhistor` 的 `get_atmo_light_sumbus`（0xaeb4e2）按适配器名称DesignWare枚举候选，再读身份寄存器；bus编号不是固定常数。本版沿用该识别依据，从sysfs和设备节点定位，唯一通过身份与寄存器验证才开放，不需用户手选、不盲扫地址、不复用原厂`-f`。同时避免原厂全部探测失败仍可能返回最后编号的问题。每次写前核验、写后读回，失败撤销旧能力。

本版亮度 0–100% 独立映射为 round(255×百分比/100) 后写 0x91，不缩放 RGB 颜色分量；这一点比附件的颜色缩放实现更接近官方协议。模式 0关/1常亮/2彩虹/3呼吸。具体灯效与亮度效果仍需实机观察。

新灯光服务支持手动、最高温颜色渐变及夜间亮度上限，默认只读探测，用户应用的规则才启用写入。检测到原厂主题或其他程序改灯则让回控制，详细寄存器、固件证据和恢复语义见[灯光策略](light-policy.md)。


## 当前发布策略

`HistorAdapter.hardware_supported` 默认 None，生产被动读取实际 loaded module 的完整 GNU note，只对白名单中的 initrd 支持版开放硬件能力；未知、缺失或 rootfs 版保持 policy_only。设备、配置、socket 或 RPC ACK 不能单独打开此门。每次确认设置前重新核对 note，失败撤销控制并保持失败状态，不循环尝试写入。

原厂策略路径：当前用户已加载支持版，真实 GET 回应 b'5'/auto 也已确认，可在独立接管已释放且恢复确认后，由用户在小屏明确选择低／中／高／自动。`HistorAdapter` 普通后台探测不调用有副作用的 getter，不主动发送 SET 或信号，不加载模块；确认原厂操作才按固定协议执行。该策略由官方服务持久保存，恢复 UI 不自动撤销，仍不能用回执证明机械效果。

独立温控路径：另外核验温控二进制 SHA256、唯一原厂进程、字符设备、同步接口及 Linux amd64 适配层，用户确认应用后协调重启与既有模式同步，保存待用配置本身不触发接管。CPU/sysfs 来源每秒刷新、有效期 3 秒；SMART 通常约 30 秒更新、有效期 90 秒，保留原采样时间。`fan-policy.json` 将待用配置、最后成功应用配置和开机恢复意图分开保存；只有成功应用的规则才恢复，明确释放或故障则停用自动接管。具体开关与保护说明见 [独立温控指南](fan-policy.md)。新接管与原厂策略均未获得用户 NAS 的物理风速验证，RPM 保持未知。
