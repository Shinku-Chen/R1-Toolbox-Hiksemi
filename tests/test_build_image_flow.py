#!/usr/bin/env python3
"""Exercise the real publishing shell flow with a stateful Docker substitute.

No Docker daemon, registry, root privileges, or NAS devices are used here.
The separate delivery tests verify real docker-save archive validation.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
REFERENCE = f"ghcr.io/shinku-chen/r1-toolbox:sha-{COMMIT}"
BUILT_ID = "sha256:" + "b" * 64
OTHER_ID = "sha256:" + "c" * 64

DRIVER = r'''
import copy
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["FLOW_STATE"])
state = json.loads(path.read_text())
tool, args = sys.argv[1], sys.argv[2:]
state["calls"].append([tool, *args])

def finish(code=0, output="", error=""):
    path.write_text(json.dumps(state))
    print(output, end="")
    print(error, end="", file=sys.stderr)
    raise SystemExit(code)

if tool == "git":
    if args == ["rev-parse", "HEAD"]:
        finish(output="a" * 40 + "\n")
    if args == ["status", "--porcelain", "--untracked-files=all"]:
        finish()
elif tool == "sudo":
    finish(state.get("ui_exit", 0))
elif tool == "docker":
    if args[0] in ("login", "logout"):
        finish()
    if args[:2] == ["manifest", "inspect"]:
        state["registry_checks"] += 1
        if state.get("scenario") == "registry-error":
            finish(1, error="Get https://ghcr.io/v2/: context deadline exceeded\n")
        if state.get("scenario") == "race-before-push" and state["registry_checks"] == 2:
            state["remote"] = dict(state["built"], Id="sha256:" + "c" * 64)
        if state["remote"] is None:
            finish(1, error="no such manifest: " + args[-1] + "\n")
        finish(output='{"schemaVersion":2}\n')
    if args[0] == "build":
        state["local"] = copy.deepcopy(state["built"])
        finish()
    if args[0] == "pull":
        if state["remote"] is None:
            finish(1, error="manifest unknown\n")
        state["local"] = copy.deepcopy(state["remote"])
        finish()
    if args[:2] == ["image", "inspect"]:
        if "--format" in args:
            finish(output=state["local"]["Id"] + "\n")
        finish(output=json.dumps([state["local"]]))
    if args[0] == "run":
        if args[-1] == "--version":
            value = "wrong" if state.get("bad_runtime_version") else "sha-" + "a" * 40
            finish(output="R1 Toolbox " + value + "\n")
        if args[-1] == "--help":
            finish(output="Usage: r1-toolbox\n")
    if args[0] == "push":
        if ":sha-" in args[-1]:
            state["remote"] = copy.deepcopy(state["local"])
            if state.get("scenario") == "changed-after-push":
                state["remote"]["Id"] = "sha256:" + "c" * 64
        finish()
    if args[0] == "tag":
        finish()
    if args[0] == "save":
        Path(args[args.index("--output") + 1]).write_bytes(b"synthetic archive")
        finish()
    if args[0] == "compose":
        finish()
finish(97, error="Unexpected fixture command: " + repr([tool, *args]) + "\n")
'''

PACKAGE = r'''
import hashlib
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["FLOW_STATE"])
state = json.loads(path.read_text())
state["calls"].append(["package", *sys.argv[1:]])
path.write_text(json.dumps(state))
output = Path("dist")
(output / "composer.yml").write_text("services: {}\n")
(output / "deployment-manifest.json").write_text("{}\n")
names = ("composer.yml", "r1-toolbox-linux-amd64.tar", "deployment-manifest.json")
(output / "SHA256SUMS").write_text("".join(
    hashlib.sha256((output / name).read_bytes()).hexdigest() + "  " + name + "\n"
    for name in names
), newline="\n")
'''


def shell_path():
    """Use POSIX sh in Linux CI, or Git's sh for this Windows-only fixture."""
    found = shutil.which("sh")
    if found:
        return found
    candidate = Path("C:/Program Files/Git/bin/sh.exe")
    return str(candidate) if candidate.is_file() else None


@unittest.skipUnless(shell_path(), "POSIX sh is required for publishing flow checks")
class BuildImageFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="r1-build-flow-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/build-image.sh", self.root / "scripts/build-image.sh")
        (self.root / "scripts/package.py").write_text(PACKAGE, encoding="utf-8")
        self.driver = self.root / "driver.py"
        self.driver.write_text(DRIVER, encoding="utf-8")
        self.binary = self.root / "bin"
        self.binary.mkdir()
        for name in ("docker", "git", "sudo"):
            wrapper = self.binary / name
            wrapper.write_text(
                '#!/bin/sh\nexec "$FLOW_PYTHON" "$FLOW_DRIVER" ' + name + ' "$@"\n',
                encoding="utf-8", newline="\n",
            )
            wrapper.chmod(0o755)
        python = self.binary / "python"
        python.write_text('#!/bin/sh\nexec "$FLOW_PYTHON" "$@"\n',
                          encoding="utf-8", newline="\n")
        python.chmod(0o755)
        self.state_path = self.root / "state.json"
        self.image = {
            "Id": BUILT_ID, "Architecture": "amd64", "Os": "linux",
            "Config": {
                "Entrypoint": ["/usr/local/bin/r1-toolbox"], "Cmd": ["run"],
                "Env": [f"TOOLBOX_VERSION=sha-{COMMIT}"],
                "Labels": {
                    "org.opencontainers.image.revision": COMMIT,
                    "org.opencontainers.image.version": f"sha-{COMMIT}",
                },
            },
        }

    def run_flow(self, *, remote=None, scenario=None, publish=True,
                 ref="refs/heads/dev", event="push", **options):
        self.state_path.write_text(json.dumps({
            "calls": [], "registry_checks": 0, "remote": remote,
            "built": self.image, "local": None, "scenario": scenario, **options,
        }))
        binary_path = self.binary.as_posix()
        if os.name == "nt":
            binary_path = "/" + binary_path[0].lower() + binary_path[2:]
        environment = dict(
            os.environ, PATH=str(self.binary) + os.pathsep + os.environ["PATH"],
            FLOW_PYTHON=sys.executable, FLOW_DRIVER=str(self.driver),
            FLOW_STATE=str(self.state_path), GITHUB_SHA=COMMIT, GITHUB_REF=ref,
            GITHUB_EVENT_NAME=event, GITHUB_ACTOR="fixture", GHCR_TOKEN="fixture",
            PUBLISH_IMAGE="1" if publish else "0",
            FLOW_BIN=binary_path,
            # Keep MSYS from translating Docker arguments in the Windows fixture.
            MSYS_NO_PATHCONV="1",
        )
        result = subprocess.run([shell_path(), "-c",
                                 'PATH="$FLOW_BIN:$PATH"; export PATH; . scripts/build-image.sh',
                                 "scripts/build-image.sh"],
                                cwd=self.root, env=environment, text=True,
                                encoding="utf-8", errors="replace", capture_output=True)
        self.state = json.loads(self.state_path.read_text())
        self.calls = self.state["calls"]
        return result

    def docker_calls(self, command):
        return [call for call in self.calls if call[:2] == ["docker", command]]

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_not_published(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.docker_calls("push"), [])
        self.assertEqual(self.docker_calls("tag"), [])
        self.assertFalse(any(call[0] == "package" for call in self.calls))

    def test_version_tag_publishes_latest_and_same_version(self):
        self.assert_success(self.run_flow(ref="refs/tags/v1.2.3"))
        pushes = self.docker_calls("push")
        self.assertEqual(pushes[0], ["docker", "push", REFERENCE])
        self.assertEqual(pushes[1], ["docker", "push", "ghcr.io/shinku-chen/r1-toolbox:latest"])
        self.assertEqual(pushes[2], ["docker", "push", "ghcr.io/shinku-chen/r1-toolbox:v1.2.3"])
        package = next(call for call in self.calls if call[0] == "package")
        self.assertEqual(package[package.index("--channel") + 1], "latest")

    def test_new_commit_builds_checks_then_publishes_preview(self):
        self.assert_success(self.run_flow())
        self.assertEqual(len(self.docker_calls("build")), 1)
        self.assertIn("org.opencontainers.image.revision=" + COMMIT,
                      self.docker_calls("build")[0])
        self.assertEqual(self.docker_calls("push"), [
            ["docker", "push", REFERENCE],
            ["docker", "push", "ghcr.io/shinku-chen/r1-toolbox:preview"],
        ])
        checks = self.docker_calls("run")
        self.assertEqual([call[-1] for call in checks], ["--help", "--version"])
        for check in checks:
            self.assertIn("--read-only", check)
            self.assertEqual(check[check.index("--network") + 1], "none")
            self.assertEqual(check[check.index("--cap-drop") + 1], "ALL")
        ui_index = next(i for i, call in enumerate(self.calls) if call[0] == "sudo")
        push_index = self.calls.index(self.docker_calls("push")[0])
        self.assertLess(ui_index, push_index)
        self.assertEqual(self.state["registry_checks"], 2)

    def test_main_push_never_publishes_an_image(self):
        original = dict(self.image, Id=OTHER_ID)
        result = self.run_flow(remote=original, ref="refs/heads/main")
        self.assert_not_published(result)
        self.assertIn("请创建 v* 标签", result.stderr)

    def test_existing_sha_requires_correct_architecture_entrypoint_version_and_revision(self):
        for field in ("architecture", "os", "entrypoint", "command", "version",
                      "version_label", "revision", "missing_revision"):
            with self.subTest(field=field):
                image = json.loads(json.dumps(self.image))
                if field == "architecture":
                    image["Architecture"] = "arm64"
                elif field == "os":
                    image["Os"] = "windows"
                elif field == "entrypoint":
                    image["Config"]["Entrypoint"] = ["/bin/sh"]
                elif field == "command":
                    image["Config"]["Cmd"] = ["unexpected"]
                elif field == "version":
                    image["Config"]["Env"] = ["TOOLBOX_VERSION=dev"]
                elif field == "version_label":
                    image["Config"]["Labels"]["org.opencontainers.image.version"] = "dev"
                elif field == "revision":
                    image["Config"]["Labels"]["org.opencontainers.image.revision"] = "0" * 40
                else:
                    del image["Config"]["Labels"]["org.opencontainers.image.revision"]
                result = self.run_flow(remote=image)
                self.assert_not_published(result)
                self.assertEqual(self.docker_calls("build"), [])
                self.assertEqual(self.docker_calls("run"), [])
                self.assertTrue(self.docker_calls("image"))

    def test_pull_request_never_contacts_registry_or_publishes(self):
        self.assert_success(self.run_flow(publish=False, event="pull_request"))
        self.assertEqual(len(self.docker_calls("build")), 1)
        for command in ("login", "manifest", "pull", "push", "tag"):
            self.assertEqual(self.docker_calls(command), [])
        package = next(call for call in self.calls if call[0] == "package")
        self.assertEqual(package[package.index("--channel") + 1], "pull-request")

    def test_pull_request_cannot_enable_publication(self):
        result = self.run_flow(event="pull_request")
        self.assert_not_published(result)
        self.assertIn("PR 检查禁止发布镜像", result.stderr)
        self.assertEqual([call for call in self.calls if call[0] == "docker"], [])

    def test_registry_failure_is_not_treated_as_a_missing_tag(self):
        self.assert_not_published(self.run_flow(scenario="registry-error"))
        self.assertEqual(self.docker_calls("build"), [])
        self.assertEqual(len(self.docker_calls("logout")), 1)

    def test_competing_publisher_cannot_have_its_sha_overwritten(self):
        self.assert_not_published(self.run_flow(scenario="race-before-push"))
        self.assertEqual(self.state["remote"]["Id"], OTHER_ID)
        self.assertEqual(self.state["registry_checks"], 2)

    def test_reused_sha_is_rechecked_after_smoke_tests(self):
        self.assert_not_published(self.run_flow(remote=self.image, scenario="race-before-push"))
        self.assertEqual(self.docker_calls("build"), [])
        self.assertEqual(self.state["registry_checks"], 2)

    def test_registry_readback_precedes_channel_promotion(self):
        result = self.run_flow(scenario="changed-after-push")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.docker_calls("push"), [["docker", "push", REFERENCE]])
        self.assertEqual(self.docker_calls("tag"), [])
        self.assertFalse(any(call[0] == "package" for call in self.calls))

    def test_runtime_and_ui_checks_gate_even_an_existing_sha(self):
        for options in ({"bad_runtime_version": True}, {"ui_exit": 19}):
            with self.subTest(options=options):
                self.assert_not_published(self.run_flow(remote=self.image, **options))
                self.assertEqual(self.docker_calls("build"), [])


if __name__ == "__main__":
    unittest.main()
