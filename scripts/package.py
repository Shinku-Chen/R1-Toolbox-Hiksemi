#!/usr/bin/env python3
"""Prepare a pinned Compose deployment and verify its docker-save image archive."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "r1-toolbox"
IMAGE_REPOSITORY = "ghcr.io/shinku-chen/r1-toolbox"
ARCHIVE_NAME = "r1-toolbox-linux-amd64.tar"
OCI_INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
OCI_CONFIG_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
DIGEST_PATTERN = r"sha256:[0-9a-f]{64}"


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit():
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("git did not return a full commit hash")

    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT, text=True,
    )
    if dirty:
        raise ValueError("源码尚未提交，请先提交再打包，确保 manifest 的 commit 对应镜像源码")
    return commit


def archive_member(bundle, name, max_size=None):
    """Read archive metadata without ever extracting an untrusted archive."""
    if not isinstance(name, str):
        raise ValueError("docker-save member path must be a string")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("unsafe member path in docker-save archive")
    try:
        member = bundle.getmember(name)
    except KeyError:
        raise ValueError(f"docker-save archive is missing {name}") from None
    if not member.isfile():
        raise ValueError(f"docker-save member is not a regular file: {name}")
    if max_size is not None and member.size > max_size:
        raise ValueError(f"docker-save metadata is too large: {name}")
    return member


def read_member(bundle, name, max_size):
    member = archive_member(bundle, name, max_size)
    with bundle.extractfile(member) as source:
        return source.read()


def verify_image_id(bundle, image_id, config_digest):
    """Bind both classic Docker IDs and containerd OCI IDs to the saved config."""
    if image_id == config_digest:
        return

    # The classic image store exposes the config digest as .Id. The containerd
    # image store exposes its target manifest/index digest instead. Verify every
    # content-addressed metadata edge from that root, never trusting a filename
    # or an unrelated valid config blob alone.
    blob_count = 0
    total_bytes = 0

    def descriptor_digest(descriptor, allowed_types):
        if not isinstance(descriptor, dict):
            raise ValueError("invalid OCI metadata descriptor")
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(DIGEST_PATTERN, digest):
            raise ValueError("OCI metadata digest must be a full sha256 digest")
        media_type = descriptor.get("mediaType")
        if media_type is not None and media_type not in allowed_types:
            raise ValueError("unsupported OCI metadata mediaType")
        return digest

    def read_blob(descriptor, allowed_types):
        nonlocal blob_count, total_bytes
        digest = descriptor_digest(descriptor, allowed_types)
        blob_count += 1
        if blob_count > 64:
            raise ValueError("OCI metadata graph exceeds 64 blobs")
        data = read_member(bundle, "blobs/sha256/" + digest[7:], 2 * 1024 * 1024)
        total_bytes += len(data)
        if total_bytes > 8 * 1024 * 1024:
            raise ValueError("OCI metadata graph exceeds 8 MiB")
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("OCI metadata blob content does not match its digest")
        size = descriptor.get("size")
        if size is not None and (type(size) is not int or size != len(data)):
            raise ValueError("OCI metadata blob size does not match its descriptor")
        return digest, data

    def visit(descriptor, depth):
        if depth > 8:
            raise ValueError("OCI metadata graph exceeds 8 levels")
        _, data = read_blob(descriptor, OCI_INDEX_TYPES | OCI_MANIFEST_TYPES)
        document = json.loads(data)
        if not isinstance(document, dict) or document.get("schemaVersion") != 2:
            raise ValueError("invalid OCI manifest/index document")
        media_type = document.get("mediaType")
        if "manifests" in document:
            children = document["manifests"]
            if media_type is not None and media_type not in OCI_INDEX_TYPES:
                raise ValueError("OCI index has an invalid mediaType")
            if not isinstance(children, list) or not children or len(children) > 64:
                raise ValueError("OCI index must contain 1–64 manifest descriptors")
            matched = False
            for child in children:
                if visit(child, depth + 1):
                    matched = True
            return matched
        if media_type is not None and media_type not in OCI_MANIFEST_TYPES:
            raise ValueError("OCI manifest has an invalid mediaType")
        if not isinstance(document.get("layers"), list):
            raise ValueError("OCI manifest is missing its layer descriptors")
        digest, _ = read_blob(document.get("config"), OCI_CONFIG_TYPES)
        return digest == config_digest

    if not visit({"digest": image_id}, 0):
        raise ValueError("OCI image id does not reference the docker-save compatibility config")


def verify_archive(archive, image, image_id):
    if not re.fullmatch(DIGEST_PATTERN, image_id):
        raise ValueError("image id must be a full sha256 digest")
    with tarfile.open(archive, mode="r:*") as bundle:
        names = bundle.getnames()
        if len(names) != len(set(names)):
            raise ValueError("docker-save archive contains duplicate member names")
        manifest = json.loads(read_member(bundle, "manifest.json", 1024 * 1024))
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise ValueError("archive must contain exactly one docker-save image")
        entry = manifest[0]
        if not isinstance(entry, dict) or entry.get("RepoTags") != [image]:
            raise ValueError("docker-save RepoTags do not match the pinned Compose image")
        config_bytes = read_member(bundle, entry.get("Config"), 2 * 1024 * 1024)
        config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        verify_image_id(bundle, image_id, config_digest)
        config = json.loads(config_bytes)
        if not isinstance(config, dict) or (
            config.get("architecture"), config.get("os")
        ) != ("amd64", "linux"):
            raise ValueError("docker-save image must target linux/amd64")
        layers = entry.get("Layers")
        if not isinstance(layers, list) or not layers:
            raise ValueError("docker-save archive has no image layers")
        for layer in layers:
            archive_member(bundle, layer)


def has_unresolved_value(value):
    if isinstance(value, str):
        return "${" in value
    if isinstance(value, dict):
        return any(has_unresolved_value(item) for item in value.values())
    if isinstance(value, list):
        return any(has_unresolved_value(item) for item in value)
    return False


def render_compose(image):
    source = ROOT / "composer.yml"
    if source.is_symlink() or not source.is_file():
        raise ValueError("missing or unsupported composer.yml source")
    project = yaml.safe_load(source.read_text(encoding="utf-8"))
    services = project.get("services") if isinstance(project, dict) else None
    if not isinstance(services, dict) or len(services) != 1:
        raise ValueError("composer.yml must define exactly one toolbox service")
    service = next(iter(services.values()))
    if not isinstance(service, dict) or "image" not in service:
        raise ValueError("toolbox service is missing its image")
    if "env_file" in service:
        raise ValueError("deployment must not depend on an external env_file")
    service["image"] = image
    if has_unresolved_value(project):
        raise ValueError("deployment contains unresolved environment substitutions")
    return (
        "# 直接导入此文件；镜像已固定到本次构建，无需 .env。\n"
        + yaml.safe_dump(project, allow_unicode=True, sort_keys=False, width=100)
    ).encode("utf-8")


def package(image, image_id, archive, channel, output, version="dev"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version):
        raise ValueError("version must be 1–128 letters, digits, dots, underscores, or hyphens")
    if channel not in ("preview", "latest", "pull-request"):
        raise ValueError("unsupported image channel")
    commit = git_commit()
    if image != f"{IMAGE_REPOSITORY}:sha-{commit}":
        raise ValueError("image must use this source commit's full immutable sha tag")
    output = output.resolve()
    archive = archive.resolve()
    if archive.parent != output or archive.name != ARCHIVE_NAME or not archive.is_file():
        raise ValueError(f"archive must be {output / ARCHIVE_NAME}")
    verify_archive(archive, image, image_id)
    composer = output / "composer.yml"
    composer.write_bytes(render_compose(image))
    manifest = {
        "schema_version": 1,
        "name": PACKAGE,
        "version": version,
        "commit": commit,
        "channel": channel,
        "platform": "linux/amd64",
        "image": {
            "reference": image,
            "id": image_id,
            "archive": archive.name,
            "format": "docker-save",
        },
        "files": {
            path.name: sha256_file(path) for path in (composer, archive)
        },
    }
    manifest_path = output / "deployment-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    files = (composer, archive, manifest_path)
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in files),
        encoding="utf-8",
    )
    print(f"Verified linux/amd64 docker-save archive: {archive}")
    print(f"Pinned deployment image: {image}")
    return composer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="full immutable sha image reference")
    parser.add_argument("--image-id", required=True, help="built image's full sha256 id")
    parser.add_argument("--archive", required=True, type=Path, help="docker save archive path")
    parser.add_argument("--channel", choices=("preview", "latest", "pull-request"), default="preview")
    parser.add_argument("--version", default="dev", help="display version stored in the manifest")
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="output directory")
    args = parser.parse_args()
    try:
        package(args.image, args.image_id, args.archive, args.channel, args.output, args.version)
    except (OSError, ValueError, tarfile.TarError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"打包失败：{error}\n")


if __name__ == "__main__":
    main()
