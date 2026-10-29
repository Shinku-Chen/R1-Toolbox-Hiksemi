"""Passive application-cache diagnostics; no NAS or vendor requests."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import vendor_app_diagnostics as apps


class VendorAppDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r1-vendor-apps-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "tmp/nand_config/app_center.conf"
        self.path.parent.mkdir(parents=True)

    def write(self, rows=None, **extra):
        value = {"list": [{"module_name": "相册", "version": "1.2.3", "status": 0,
                            "path": "/private/install", "token": "never-return"}] if rows is None else rows,
                 **extra}
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def collect(self):
        return apps.collect(self.root)

    def test_projects_only_cached_names_versions_and_status_without_commands(self):
        self.write(count=77, userid=123, token="never-return", url="https://private.invalid/package")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with mock.patch("socket.socket", side_effect=AssertionError("no sockets")), \
                mock.patch("subprocess.run", side_effect=AssertionError("no commands")):
            result = self.collect()
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "cached")
        self.assertEqual(result["data"]["apps"], [{"name": "相册", "version": "1.2.3",
                                                    "installation_status_code": 0, "running": None}])
        self.assertFalse(result["data"]["complete"])
        self.assertFalse(result["data"]["live"])
        self.assertEqual(result["data"]["cached_entries"], 1)
        self.assertTrue(result["data"]["cache_modified_at"])
        for private in ("never-return", "private.invalid", "/private", "userid", "apply_id"):
            self.assertNotIn(private, json.dumps(result, ensure_ascii=False))
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_missing_cache_is_not_empty_success_and_is_not_created(self):
        result = self.collect()
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "cache_missing")
        self.assertIsNone(result["data"])
        self.assertFalse(self.path.exists())

    def test_true_empty_cache_is_explicitly_partial_historical_cache(self):
        self.write([], count=50)
        result = self.collect()
        self.assertTrue(result["available"])
        self.assertEqual(result["data"]["apps"], [])
        self.assertFalse(result["data"]["complete"])
        self.assertEqual(result["status"], "cached")

    def test_status_codes_are_not_running_states_and_empty_version_is_unknown(self):
        self.write([{"module_name": "下载", "version": "", "status": 4}])
        row = self.collect()["data"]["apps"][0]
        self.assertEqual(row["installation_status_code"], 4)
        self.assertIsNone(row["running"])
        self.assertIsNone(row["version"])
        for status in (1, -1, True, "0", None):
            self.write([{"module_name": "应用", "version": "1", "status": status}])
            self.assertIsNone(self.collect()["data"])

    def test_unknown_schema_duplicate_names_and_row_limit_are_rejected(self):
        for value in ({}, [], {"list": {}}, {"list": [None]}):
            self.path.write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(self.collect()["data"])
        self.write([{"module_name": "same", "version": "1", "status": 0}] * 2)
        self.assertIsNone(self.collect()["data"])
        self.write([{"module_name": str(i), "version": "1", "status": 0} for i in range(apps.MAX_APPS + 1)])
        self.assertIsNone(self.collect()["data"])

    def test_corrupt_duplicate_keys_constants_and_invalid_utf8_are_rejected(self):
        for raw in (b"not-json", b"\xff", b'{"list":[],"list":[]}', b'{"list":[],"x":NaN}',
                    b'{"list":[{"module_name":"a","module_name":"b","version":"1","status":0}]}'):
            self.path.write_bytes(raw)
            self.assertIsNone(self.collect()["data"])

    def test_labels_are_bounded_and_paths_urls_controls_are_not_labels(self):
        for text in ("", " ", "x" * 64, "温" * 22, "bad\nname", "bad\x85name", "../a", "C:\\private", "https://private.invalid"):
            self.write([{"module_name": text, "version": "1", "status": 0}])
            self.assertIsNone(self.collect()["data"])
        self.write([{"module_name": "x" * 63, "version": "温" * 21, "status": 0}])
        self.assertTrue(self.collect()["available"])

    def test_file_bounds_permissions_and_mutation_fail_without_exception_text(self):
        self.write()
        with mock.patch.object(apps, "MAX_CACHE_BYTES", 5):
            self.assertIsNone(self.collect()["data"])
        with mock.patch.object(apps.os, "open", side_effect=PermissionError("/private/auth-token")):
            result = self.collect()
            self.assertEqual(result["status"], "permission_denied")
            self.assertNotIn("/private", str(result))
        real_read = os.read

        def changed(descriptor, limit):
            data = real_read(descriptor, limit)
            self.path.write_bytes(data + b" ")
            return data

        with mock.patch.object(apps.os, "read", side_effect=changed):
            self.assertIsNone(self.collect()["data"])

    def test_symlink_cache_is_rejected(self):
        target = self.root / "outside.conf"
        target.write_text('{"list":[]}', encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except OSError:
            self.skipTest("symbolic links unavailable")
        self.assertIsNone(self.collect()["data"])


if __name__ == "__main__":
    unittest.main()
