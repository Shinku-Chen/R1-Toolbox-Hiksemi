#!/usr/bin/env python3
"""Check deliverables with synthetic Docker archives, without Docker or a NAS.

Run: python3 -m unittest discover -s tests -p 'test_delivery.py'
"""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class TemporaryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp(prefix="r1-delivery-test-"))
        self.addCleanup(self.recycle_fixture)

    def recycle_fixture(self):
        # Test cleanup follows the same Trash preference as the runtime tests.
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir():
            destination = trash / self.temporary.name
            if not destination.exists():
                shutil.move(str(self.temporary), str(destination))
                return
        print("测试临时目录已保留：", self.temporary)


class DeliveryTests(TemporaryFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.temporary / "repo"
        self.repo.mkdir()
        # Use the real sources, but commit only this isolated fixture. The user's
        # worktree remains untouched, even while other agents are editing it.
        sources = [
            "scripts/package.py", "composer.yml", "Dockerfile",
            "docker/entrypoint.sh", "requirements-dev.txt",
        ]
        sources.extend(
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "tools" / "boot-once").rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        )
        for name in sources:
            destination = self.repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        (self.repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
        self.git("init", "--quiet", "--template=")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "delivery test fixture")
        self.commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.reference = f"ghcr.io/shinku-chen/r1-toolbox:sha-{self.commit}"
        self.output = self.repo / "dist"
        self.output.mkdir()
        self.archive = self.output / "r1-toolbox-linux-amd64.tar"

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-c", "user.name=R1 delivery test",
             "-c", "user.email=delivery-test@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             *arguments],
            cwd=self.repo, text=True, capture_output=True, check=True,
        )

    def write_archive(self, *, reference=None, architecture="amd64",
                      config_path=None, layer_path="layer/layer.tar", path=None):
        # An empty tar is a valid layer and needs no Docker daemon to construct.
        layer = io.BytesIO()
        with tarfile.open(fileobj=layer, mode="w"):
            pass
        layer_bytes = layer.getvalue()
        config = json.dumps(
            {"architecture": architecture, "os": "linux",
             "rootfs": {"type": "layers",
                        "diff_ids": ["sha256:" + sha256(layer_bytes)]}},
            sort_keys=True,
        ).encode("utf-8")
        self.image_id = "sha256:" + sha256(config)
        config_path = config_path or sha256(config) + ".json"
        manifest = [{
            "Config": config_path,
            "RepoTags": [reference or self.reference],
            "Layers": [layer_path],
        }]
        members = {
            "manifest.json": json.dumps(manifest).encode("utf-8"),
            config_path: config,
            layer_path: layer_bytes,
        }
        self.write_members(members, path)

    def write_members(self, members, path=None):
        with tarfile.open(path or self.archive, mode="w") as bundle:
            for name, data in members.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))

    def write_modern_archive(self, *, root_kind="manifest", damage=None,
                             reference=None):
        """Model containerd's save archive, whose image ID is an OCI root digest.

        manifest.json remains the Docker compatibility view. Its config must be
        the same config reached through the OCI graph, not just a valid blob.
        """
        self.write_archive(reference=reference)
        with tarfile.open(self.archive) as bundle:
            original = {member.name: bundle.extractfile(member).read()
                        for member in bundle.getmembers()}
        compatible = json.loads(original["manifest.json"])[0]
        config = original[compatible["Config"]]
        layer = original[compatible["Layers"][0]]
        members = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}'}

        def blob(data, media_type):
            digest = "sha256:" + sha256(data)
            members["blobs/sha256/" + digest.split(":", 1)[1]] = data
            return {"mediaType": media_type, "digest": digest, "size": len(data)}

        def document(value):
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

        config_type = "application/vnd.oci.image.config.v1+json"
        manifest_type = "application/vnd.oci.image.manifest.v1+json"
        index_type = "application/vnd.oci.image.index.v1+json"
        config_descriptor = blob(config, config_type)
        layer_descriptor = blob(layer, "application/vnd.oci.image.layer.v1.tar")
        compatible["Config"] = "blobs/sha256/" + config_descriptor["digest"][7:]
        compatible["Layers"] = ["blobs/sha256/" + layer_descriptor["digest"][7:]]
        members["manifest.json"] = document([compatible])

        if damage == "different-config":
            # Every hash is valid, but this graph no longer describes the image
            # in Docker's compatibility view.
            unrelated = json.loads(config)
            unrelated["created"] = "2026-01-01T00:00:00Z"
            config_descriptor = blob(document(unrelated), config_type)
        root = blob(document({
            "schemaVersion": 2, "mediaType": manifest_type,
            "config": config_descriptor, "layers": [layer_descriptor],
        }), manifest_type)
        manifest_digest = root["digest"]
        index_depth = {"manifest": 0, "index": 1, "nested-index": 2}[root_kind]
        for _ in range(index_depth):
            child = dict(root, platform={"architecture": "amd64", "os": "linux"})
            root = blob(document({
                "schemaVersion": 2, "mediaType": index_type,
                "manifests": [child],
            }), index_type)
        self.image_id = root["digest"]
        root_with_name = dict(root, annotations={
            "org.opencontainers.image.ref.name": reference or self.reference,
            "io.containerd.image.name": reference or self.reference,
        })
        members["index.json"] = document({
            "schemaVersion": 2, "mediaType": index_type,
            "manifests": [root_with_name],
        })
        if damage in ("root-hash", "child-hash"):
            digest = self.image_id if damage == "root-hash" else manifest_digest
            members["blobs/sha256/" + digest[7:]] += b"\n"
        self.write_members(members)

    def package(self, *, image_id=None, archive=None):
        return subprocess.run(
            [sys.executable, "scripts/package.py", "--image", self.reference,
             "--image-id", image_id or self.image_id,
             "--archive", str(archive or self.archive),
             "--channel", "preview", "--output", str(self.output)],
            cwd=self.repo, text=True, capture_output=True,
        )

    def assert_rejected(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.output / "deployment-manifest.json").exists(),
                         "rejected input must not produce a deployment manifest")

    def assert_delivery(self, result, archive_digest):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        compose = yaml.safe_load((self.output / "composer.yml").read_text())
        self.assertEqual(
            [service["image"] for service in compose["services"].values()],
            [self.reference],
        )
        manifest_path = self.output / "deployment-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["commit"], self.commit)
        self.assertEqual(manifest["platform"], "linux/amd64")
        self.assertEqual(manifest["image"]["reference"], self.reference)
        self.assertEqual(manifest["image"]["id"], self.image_id)
        self.assertEqual(manifest["image"]["archive"], self.archive.name)
        self.assertEqual(sha256(self.archive.read_bytes()), archive_digest)

        expected_names = {"composer.yml", self.archive.name,
                          "deployment-manifest.json"}
        checksums = dict(
            (name, digest) for digest, name in (
                line.split("  ", 1)
                for line in (self.output / "SHA256SUMS").read_text().splitlines()
            )
        )
        self.assertEqual(set(checksums), expected_names)
        for name, digest in checksums.items():
            self.assertEqual(sha256((self.output / name).read_bytes()), digest)
        self.assertEqual(manifest["files"], {
            name: checksums[name] for name in expected_names
            if name != "deployment-manifest.json"
        })

    def test_delivers_compose_and_archive_for_the_same_image(self):
        self.write_archive()
        archive_digest = sha256(self.archive.read_bytes())
        self.assert_delivery(self.package(), archive_digest)

    def test_accepts_modern_single_manifest_image_id(self):
        self.write_modern_archive(root_kind="manifest")
        archive_digest = sha256(self.archive.read_bytes())
        self.assert_delivery(self.package(), archive_digest)

    def test_accepts_modern_index_image_id(self):
        for root_kind in ("index", "nested-index"):
            with self.subTest(root_kind=root_kind):
                self.write_modern_archive(root_kind=root_kind)
                archive_digest = sha256(self.archive.read_bytes())
                self.assert_delivery(self.package(), archive_digest)

    def test_rejects_modern_root_or_child_blob_hash_mismatch(self):
        for damage in ("root-hash", "child-hash"):
            with self.subTest(damage=damage):
                self.write_modern_archive(root_kind="index", damage=damage)
                self.assert_rejected(self.package())

    def test_rejects_modern_root_referring_to_a_different_config(self):
        self.write_modern_archive(root_kind="index", damage="different-config")
        self.assert_rejected(self.package())

    def test_rejects_a_forged_modern_root_id(self):
        self.write_modern_archive(root_kind="index")
        self.assert_rejected(self.package(image_id="sha256:" + "0" * 64))

    def test_rejects_modern_archive_with_a_different_tag(self):
        self.write_modern_archive(root_kind="index",
                                  reference="ghcr.io/shinku-chen/r1-toolbox:unrelated")
        self.assert_rejected(self.package())

    def test_rejects_archive_with_a_different_image_tag(self):
        self.write_archive(reference="ghcr.io/shinku-chen/r1-toolbox:unrelated")
        self.assert_rejected(self.package())

    def test_rejects_archive_for_a_different_architecture(self):
        self.write_archive(architecture="arm64")
        self.assert_rejected(self.package())

    def test_rejects_an_image_id_not_matching_the_config(self):
        self.write_archive()
        self.assert_rejected(self.package(image_id="sha256:" + "0" * 64))

    def test_rejects_tar_references_outside_the_archive(self):
        for field in ("config_path", "layer_path"):
            with self.subTest(field=field):
                self.write_archive(**{field: "../outside.json"})
                self.assert_rejected(self.package())

    def test_rejects_an_archive_outside_the_delivery_directory(self):
        outside = self.temporary / self.archive.name
        self.write_archive(path=outside)
        self.assert_rejected(self.package(archive=outside))


class EntrypointTests(TemporaryFixture):
    def test_informational_and_invalid_commands_do_not_call_host_tools(self):
        binary = self.temporary / "bin"
        binary.mkdir()
        event = self.temporary / "host-access"
        for name in ("chroot", "nsenter", "docker", "systemctl"):
            tool = binary / name
            tool.write_text('#!/bin/sh\nprintf touched >> "$HOST_EVENT"\nexit 99\n')
            tool.chmod(0o755)
        environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.defpath,
                           TOOLBOX_VERSION="contract-test", HOST_EVENT=str(event))
        for command in ("--help", "-h", "--version", "-V", "unknown-command"):
            with self.subTest(command=command):
                result = subprocess.run(
                    ["/bin/sh", str(ROOT / "docker" / "entrypoint.sh"), command],
                    env=environment, cwd=self.temporary,
                    text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 2 if command == "unknown-command" else 0,
                                 result.stdout + result.stderr)
                if command in ("--version", "-V"):
                    self.assertIn("contract-test", result.stdout)
                elif command != "unknown-command":
                    self.assertIn("Usage:", result.stdout)
                self.assertFalse(event.exists(), "command unexpectedly accessed host tools")


if __name__ == "__main__":
    unittest.main()
