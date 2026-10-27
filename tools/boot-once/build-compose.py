#!/usr/bin/env python3
"""Build the self-contained Compose file; the NAS only needs compose.yaml."""
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

class Literal(str):
    pass

class Dumper(yaml.SafeDumper):
    pass

Dumper.add_representer(
    Literal, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
)

project = {
    "services": {
        "r1-boot-once": {
            "image": "docker.io/library/debian:bookworm-slim",
            "container_name": "r1-boot-once",
            "restart": "always",
            "privileged": True,
            "pid": "host",
            "ipc": "host",
            "network_mode": "host",
            "environment": {
                "TASK_CONTAINER": "r1-boot-once",
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
            "entrypoint": ["/bin/sh", "-c"],
            # Compose interprets dollars even inside quoted shell heredocs.
            "command": [Literal(runner.replace("$", "$$"))],
        }
    }
}
(root / "compose.yaml").write_text(
    "# 默认 NEW_HTML 为空：只验证依赖和开机一次/自停机制，不改动小屏。\n"
    "# 修改任务或 NEW_HTML 后，递增 TASK_REVISION 以便同次开机重新执行。\n"
    + yaml.dump(project, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=100),
    encoding="utf-8",
)
