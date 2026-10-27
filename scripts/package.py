#!/usr/bin/env python3
"""Build and verify the boot-once ZIP using only Python's standard library."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "boot-once"
PACKAGE = "r1-boot-once"
# Explicit files keep unrelated toolbox assets, firmware, and secrets out.
TOOL_FILES = (
    "README.md",
    "build-compose.py",
    "compose.yaml",
    "host-job.sh.in",
    "runner.sh",
    "runner.sh.in",
    "screen-actions-notes.md",
    "screen-actions.sh",
    "test_runner.py",
)
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def package(version, output):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("version must be 1–64 letters, digits, dots, underscores, or hyphens")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("git did not return a full commit hash")

    sources = {name: TOOL / name for name in TOOL_FILES}
    sources["requirements-dev.txt"] = ROOT / "requirements-dev.txt"
    source_paths = [str(path.relative_to(ROOT)) for path in sources.values()]
    source_paths.append("scripts/package.py")
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *source_paths],
        cwd=ROOT, text=True,
    )
    if dirty:
        raise ValueError("工具源码尚未提交，请先提交再打包，确保 manifest 的 commit 对应包内内容")
    for path in sources.values():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing or unsupported package source: {path}")
    files = {name: path.read_bytes() for name, path in sources.items()}
    manifest = {
        "schema_version": 1,
        "name": PACKAGE,
        "version": version,
        "commit": commit,
        "files": {name: sha256(data) for name, data in sorted(files.items())},
    }
    files["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{PACKAGE}-{version}-{commit[:12]}.zip"
    # Stored entries avoid compressor-version differences between machines.
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name, data in sorted(files.items()):
            member = zipfile.ZipInfo(f"{PACKAGE}/{name}", date_time=FIXED_TIME)
            member.create_system = 3
            permission = 0o755 if name.endswith((".py", ".sh")) else 0o644
            member.external_attr = (stat.S_IFREG | permission) << 16
            bundle.writestr(member, data)

    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("ZIP CRC integrity check failed")
        expected = {f"{PACKAGE}/{name}": data for name, data in files.items()}
        if set(bundle.namelist()) != set(expected):
            raise ValueError("ZIP contains unexpected members")
        for name, data in expected.items():
            if bundle.read(name) != data:
                raise ValueError(f"ZIP content verification failed: {name}")

    digest = sha256(archive.read_bytes())
    sums = output / "SHA256SUMS"
    sums.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    checksum, filename = sums.read_text(encoding="utf-8").strip().split("  ", 1)
    if filename != archive.name or sha256((output / filename).read_bytes()) != checksum:
        raise ValueError("SHA256SUMS verification failed")
    print(f"Verified {archive}")
    print(f"SHA256 {digest}")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="dev", help="package version (default: dev)")
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="output directory")
    args = parser.parse_args()
    try:
        package(args.version, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Packaging failed: {error}\n")


if __name__ == "__main__":
    main()
