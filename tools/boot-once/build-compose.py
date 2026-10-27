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
                "TASK_REVISION": "1",
                "NEW_HTML": "",
            },
            "volumes": [
                "/:/host",
                "/dev:/host/dev",
                "/proc:/host/proc",
                "/sys:/host/sys",
                "/run:/host/run",
            ],
        }
    }
}
compose = (
    "# R1 Toolbox 镜像部署；首次默认仅诊断，完成后容器自行停止。\n"
    "# 修改任务或 NEW_HTML 后，递增 TASK_REVISION 以便同次开机重新执行。\n"
    "# 可通过 R1_TOOLBOX_IMAGE 指定版本；发布包固定为对应构建的 sha 镜像标签。\n"
    + yaml.safe_dump(project, allow_unicode=True, sort_keys=False, width=100)
)
for path in (root / "compose.yaml", root.parents[1] / "composer.yml"):
    path.write_text(compose, encoding="utf-8")
