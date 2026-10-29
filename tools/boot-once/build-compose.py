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
            "image": "${R1_TOOLBOX_IMAGE:-ghcr.io/shinku-chen/r1-toolbox:preview}",
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
                "r1-toolbox-data:/data",
            ],
        }
    },
    "volumes": {"r1-toolbox-data": {}},
}
common_header = (
    "# R1 Toolbox 常驻服务：Web 一直运行，小屏与 MCP 分别管理开关。\n"
    "# UI_MODE: image 使用镜像内置界面；diagnostic 不接管小屏；directory 使用 UI_DIR。\n"
    "# UI_DIR 仅 directory 模式填写 NAS 前端目录；原版小屏文件保持原样。\n"
    "# 设置保存在独立 Docker 数据卷；UI 和服务仍从 /run 内存目录运行。\n"
    "# 网页使用 NAS_IP:18732；首次不设密码，小屏默认开，MCP 默认关。\n"
    "# UI_MODE=image 必需；container_name、UI_DIR、R1_WEB_BIND/PORT 可按需覆盖。\n"
    "# 小屏／开发开关保存在原数据卷；停止容器会停止服务并恢复官方窗口。\n"
    "# 可通过 R1_TOOLBOX_IMAGE 指定版本；发布包固定为对应构建的 sha 镜像标签。\n"
)
compose = (
    "# 通用 demo：默认不授予开发者权限；请在现有 Docker 项目内使用，沿用原数据卷。\n"
    + common_header
    + yaml.safe_dump(project, allow_unicode=True, sort_keys=False, width=100)
)
for path in (root / "compose.yaml", root.parents[1] / "composer.yml"):
    path.write_text(compose, encoding="utf-8")

# A complete single-file variant keeps project/service/volume selection aligned
# with the demo. It does not introduce an override or a second named project.
developer = copy.deepcopy(project)
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
    + yaml.safe_dump(developer, allow_unicode=True, sort_keys=False, width=100)
)
(root.parents[1] / "composer.dev.yml").write_text(developer_compose, encoding="utf-8")
