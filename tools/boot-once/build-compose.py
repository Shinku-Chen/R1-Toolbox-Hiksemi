#!/usr/bin/env python3
"""Compile the runner, generic NAS demo and standalone developer template."""
import copy
from pathlib import Path
import yaml

root = Path(__file__).resolve().parent
actions = (root / "screen-actions.sh").read_text(encoding="utf-8")
host_job = (root / "host-job.sh.in").read_text(encoding="utf-8").replace(
    "__SCREEN_ACTIONS__", actions.rstrip()
)
runner = (root / "runner.sh.in").read_text(encoding="utf-8").replace(
    "__HOST_JOB__", host_job.rstrip()
)
(root / "runner.sh").write_text(runner, encoding="utf-8")

project = {
    "services": {
        "r1-toolbox": {
            "image": "${R1_TOOLBOX_IMAGE:-ghcr.io/shinku-chen/r1-toolbox:latest}",
            "container_name": "r1-toolbox",
            "restart": "always",
            "stop_grace_period": "3m",
            "privileged": True,
            "pid": "host",
            "ipc": "host",
            "network_mode": "host",
            "environment": {
                "UI_MODE": "image",
                "R1_WEB_AUTH_MODE": "password",
            },
            "volumes": [
                "/:/host",
                "/dev:/host/dev",
                "/proc:/host/proc",
                "/sys:/host/sys",
                "/run:/host/run",
                "r1_toolbox_data:/data",
            ],
        }
    },
    "volumes": {"r1_toolbox_data": {}},
}

# Only these public deployment settings are editable in the examples. Optional
# values stay commented so showing every setting never grants new permissions.
environment_options = (
    ("UI_MODE", "image", (
        "界面来源：默认 image 使用镜像内置界面；directory 使用下方 UI_DIR；diagnostic 不接管小屏。",
    )),
    ("UI_DIR", "/填写已有前端目录", (
        "仅 UI_MODE=directory 时填写宿主绝对目录，须含非空 index.html，且与官方 dist 目录独立。",
        "默认不设置；image 模式不能同时设置 UI_DIR。",
    )),
    ("TASK_CONTAINER", "r1-toolbox", (
        "宿主定位部署容器的名称或 ID，默认 r1-toolbox；修改 container_name 时必须同步修改。",
    )),
    ("R1_WEB_BIND", "0.0.0.0", (
        "Web 监听地址：默认 0.0.0.0 供局域网访问；仅支持 0.0.0.0 或 127.0.0.1。",
    )),
    ("R1_WEB_PORT", "18732", (
        "Web 端口：默认 18732，范围 1024–65535；常驻服务不支持 0。",
        "18731 是本机只读 API 的保留端口，不能作为 Web 端口。",
    )),
    ("R1_WEB_AUTH_MODE", "password", (
        "网页写操作认证：默认 password；首次未设口令可直接进入，登录口令在网页设置。",
        "pair 仅用于旧版配对兼容；此项不启用 MCP，也不创建 API 密钥。",
    )),
    ("R1_SMALL_SCREEN_ENABLED", "1", (
        "小屏接管首次默认值：0 关闭、1 开启，默认 1；网页已保存的设置优先。",
    )),
    ("R1_DEV_NO_PAIRING", "1", (
        "开发模式首次默认关闭（0）；此处 1 是开启示例，须同时填写下方白名单。",
        "仅用于首次初始化；网页已保存的开发设置优先，不替代 MCP 独立密钥。",
    )),
    ("R1_DEV_ALLOW_IPS", "填写授权客户端IP或局域网网段", (
        "开发白名单默认不设置；开启开发模式时必填实际客户端 IP、局域网 CIDR 或点分掩码网段。",
        "多个地址以逗号分隔；按实际客户端 IP 校验，不读取代理转发头；网页已保存的设置优先。",
    )),
    ("R1_DEV_MINUTES", "60", (
        "开发窗口首次有效时长，单位分钟：默认 60，范围 1–1440；网页已保存的设置优先。",
    )),
    ("R1_DEV_ALLOW_COMMANDS", "0", (
        "开发固定诊断命令首次默认关闭（0）；1 仅开放固定白名单命令，不提供任意 Shell。",
        "仅在开发模式与白名单窗口内有效；网页已保存的设置优先。",
    )),
)


def render_environment(environment):
    """Document public settings beside their active or commented YAML entry."""
    known = {name for name, _, _ in environment_options}
    if set(environment) - known:
        raise ValueError("Compose 示例包含未登记的环境变量")
    lines = []
    for name, example, comments in environment_options:
        lines.extend("      # " + comment + "\n" for comment in comments)
        entry = yaml.safe_dump({name: environment.get(name, example)},
                               allow_unicode=True, sort_keys=False, width=100)
        if len(entry.splitlines()) != 1:
            raise ValueError("Compose 环境变量必须能表示为单行占位：" + name)
        lines.append("      " + ("" if name in environment else "# ") + entry)
    return "".join(lines)


def insert_comments(body, anchor, comments):
    """Fail on a changed dump structure instead of silently misplacing help."""
    if body.count(anchor) != 1:
        raise ValueError("Compose 注释锚点必须唯一：" + anchor.strip())
    return body.replace(anchor, comments + anchor, 1)


def render_project(configuration):
    """Keep generated YAML authoritative while exposing optional settings."""
    body = yaml.safe_dump(configuration, allow_unicode=True, sort_keys=False, width=100)
    body = insert_comments(body, "    image: ", (
        "    # R1_TOOLBOX_IMAGE 是此 image 行使用的外部 Compose 变量，不是容器 environment。\n"
        "    # 默认 latest（主分支）；开发者模板默认 preview（开发预览）；可改用 sha-提交哈希（固定版本）。\n"
    ))
    body = insert_comments(body, "    container_name: ", (
        "    # 默认容器名 r1-toolbox；若修改，请同步设置下方 TASK_CONTAINER。\n"
    ))
    for anchor, comment in (
        ("    restart: ", "always 随 Docker 启动并在异常退出后重启容器；服务与容器生命周期绑定。"),
        ("    stop_grace_period: ", "停止宽限期 3 分钟，等待官方小屏与风扇恢复后再结束容器。"),
        ("    privileged: ", "宿主设备与硬件控制所需的特权模式，请保留。"),
        ("    pid: ", "共享宿主进程空间，用于识别原厂进程与管理唯一运行实例。"),
        ("    ipc: ", "共享宿主 IPC，供原厂界面与本地服务通信。"),
        ("    network_mode: ", "直接使用宿主网卡；无需再增加 ports 端口映射。"),
        ("    - /:/host\n", "宿主根目录，用于宿主命名空间与路径身份校验。"),
        ("    - /dev:/host/dev\n", "宿主设备节点，供屏幕与硬件控制访问。"),
        ("    - /proc:/host/proc\n", "宿主进程信息，用于运行状态与进程身份校验。"),
        ("    - /sys:/host/sys\n", "宿主硬件与内核状态，供监控和硬件身份校验。"),
        ("    - /run:/host/run\n", "宿主 RAM 运行目录，存放展开的界面与运行时文件。"),
        ("    - r1_toolbox_data:/data\n", "持久化设置与授权，升级必须保留原数据卷；不要删除卷或使用 down -v。"),
    ):
        body = insert_comments(body, anchor, "    # " + comment + "\n")
    start, environment_anchor, rest = body.partition("    environment:\n")
    _, volumes_anchor, end = rest.partition("    volumes:\n")
    if not environment_anchor or not volumes_anchor or "    environment:\n" in rest:
        raise ValueError("Compose environment／volumes 注释锚点缺失或重复")
    return (start + environment_anchor
            + render_environment(configuration["services"]["r1-toolbox"]["environment"])
            + volumes_anchor + end)


common_header = (
    "# R1 Toolbox 常驻服务：Web 一直运行，小屏与 MCP 分别管理开关。\n"
    "# 设置保存在独立 Docker 数据卷；UI 和服务仍从 /run 内存目录运行。\n"
    "# 网页使用 NAS_IP:18732；首次不设密码，小屏默认开，MCP 默认关。\n"
    "# 小屏／开发开关保存在原数据卷；停止容器会停止服务，官方窗口需重启 R1 恢复。\n"
    "# 以下注释占位列出全部公开部署 ENV；按需取消对应行注释并填写，其他项保持默认。\n"
    "# MCP 开关／Token、网页口令、风扇／灯光策略均在网页管理，不通过部署 ENV 配置。\n"
)
compose = (
    "# 通用 demo：默认不授予开发者权限；请在现有 Docker 项目内使用，沿用原数据卷。\n"
    + common_header
    + render_project(project)
)
for path in (root / "compose.yaml", root.parents[1] / "composer.yml"):
    path.write_text(compose, encoding="utf-8")

# A complete single-file variant keeps project/service/volume selection aligned
# with the demo. It does not introduce an override or a second named project.
developer = copy.deepcopy(project)
developer["services"]["r1-toolbox"]["image"] = "${R1_TOOLBOX_IMAGE:-ghcr.io/shinku-chen/r1-toolbox:preview}"
developer["services"]["r1-toolbox"]["environment"].update({
    "R1_DEV_NO_PAIRING": "1",
    "R1_DEV_ALLOW_IPS": "${R1_DEV_ALLOW_IPS:?请设置开发者白名单IP或CIDR}",
    "R1_DEV_MINUTES": "1440",
    "R1_DEV_ALLOW_COMMANDS": "1",
})
developer_compose = (
    "# 开发者完整模板：在原 Docker 项目内替换配置，复用原数据卷，不要另建项目或叠加模板。\n"
    "# 启动前必须提供 R1_DEV_ALLOW_IPS，仅填写你授权的实际客户端 IP 或局域网 CIDR。\n"
    "# 开发授权的环境变量只用于首次初始化；网页已保存的设置优先。\n"
    + common_header
    + render_project(developer)
)
(root.parents[1] / "composer.dev.yml").write_text(developer_compose, encoding="utf-8")
