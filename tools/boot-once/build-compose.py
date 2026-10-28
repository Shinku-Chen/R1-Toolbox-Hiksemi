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
            "privileged": True,
            "pid": "host",
            "ipc": "host",
            "network_mode": "host",
            "environment": {
                "TASK_CONTAINER": "r1-toolbox",
                "TASK_REVISION": "14",
                "UI_MODE": "image",
                "UI_DIR": "",
                "R1_WEB_BIND": "0.0.0.0",
                "R1_WEB_PORT": "18732",
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
    "# R1 Toolbox 内置 UI：展开到 /run 内存目录并只读挂载，完成后容器自行停止。\n"
    "# UI_MODE: image 使用镜像内置界面；diagnostic 仅诊断；directory 使用 UI_DIR。\n"
    "# UI_DIR 仅 directory 模式填写 NAS 前端目录；原版小屏文件保持原样。\n"
    "# 设置保存在独立 Docker 数据卷；UI 和服务仍从 /run 内存目录运行。\n"
    "# 网页配置使用 NAS_IP:18732，在小屏生成一次性配对码；R1_WEB_PORT=0 禁用。\n"
    "# 修改任务配置后递增 TASK_REVISION；更换镜像会自动重新执行。\n"
    "# 可通过 R1_TOOLBOX_IMAGE 指定版本；发布包固定为对应构建的 sha 镜像标签。\n"
    + yaml.safe_dump(project, allow_unicode=True, sort_keys=False, width=100)
)
for path in (root / "compose.yaml", root.parents[1] / "composer.yml"):
    path.write_text(compose, encoding="utf-8")
