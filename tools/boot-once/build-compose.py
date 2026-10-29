#!/usr/bin/env python3
"""Compile the image runner and the two equivalent NAS Compose entry points."""
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
compose = (
    "# R1 Toolbox 常驻服务：Web 一直运行，小屏与 MCP 分别管理开关。\n"
    "# UI_MODE: image 使用镜像内置界面；diagnostic 不接管小屏；directory 使用 UI_DIR。\n"
    "# UI_DIR 仅 directory 模式填写 NAS 前端目录；原版小屏文件保持原样。\n"
    "# 设置保存在独立 Docker 数据卷；UI 和服务仍从 /run 内存目录运行。\n"
    "# 网页使用 NAS_IP:18732；首次不设密码，小屏默认开，MCP 默认关。\n"
    "# UI_MODE=image 必需；container_name、UI_DIR、R1_WEB_BIND/PORT 可按需覆盖。\n"
    "# 小屏／开发开关保存在原数据卷；停止容器会停止服务并恢复官方窗口。\n"
    "# 可通过 R1_TOOLBOX_IMAGE 指定版本；发布包固定为对应构建的 sha 镜像标签。\n"
    + yaml.safe_dump(project, allow_unicode=True, sort_keys=False, width=100)
)
for path in (root / "compose.yaml", root.parents[1] / "composer.yml"):
    path.write_text(compose, encoding="utf-8")
