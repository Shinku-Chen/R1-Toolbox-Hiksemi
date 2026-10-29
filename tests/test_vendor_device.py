"""Factory nickname projection fixtures; no NAS/configuration credentials used."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import vendor_device


class VendorDeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r1-vendor-name-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "tmp/nand_config/config.txt"
        self.config.parent.mkdir(parents=True)

    def write(self, value):
        self.config.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def collect(self):
        return vendor_device.collect_vendor_device_name(self.root)

    def test_exact_field_and_no_hostname_fallback_or_unrelated_data(self):
        self.write({"dev_info": {"dev_name": "家庭 NAS", "serial": "private"},
                    "hostname": "unrelated-host", "password": "never-return-this"})
        before = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.assertEqual(self.collect(), {"available": True, "name": "家庭 NAS",
                                          "source": "histor.dev_info.dev_name", "reason": None})
        self.assertEqual(before, hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_missing_file_stays_unknown_and_is_not_created(self):
        result = self.collect()
        self.assertFalse(result["available"])
        self.assertIsNone(result["name"])
        self.assertFalse(self.config.exists())

    def test_missing_wrong_or_empty_field_stays_unknown(self):
        for value in [{}, [], {"dev_info": []}, {"dev_info": {"dev_name": None}},
                      {"dev_info": {"dev_name": 123}}, {"dev_info": {"dev_name": " \t"}},
                      {"dev_name": "wrong location"}, {"hostname": "do-not-use"}]:
            with self.subTest(value=value):
                self.write(value)
                self.assertFalse(self.collect()["available"])
                self.assertIsNone(self.collect()["name"])

    def test_utf8_name_limit_and_controls(self):
        for name in ["x" * 64, "温" * 22, "bad\x00name", "bad\nname", "bad\x7fname", "bad\x85name"]:
            with self.subTest(name=name):
                self.write({"dev_info": {"dev_name": name}})
                self.assertIsNone(self.collect()["name"])
        for name in ["x" * 63, "温" * 21, "  家庭 NAS  "]:
            with self.subTest(name=name):
                self.write({"dev_info": {"dev_name": name}})
                self.assertEqual(self.collect()["name"], name)

    def test_corrupt_duplicate_or_nonstandard_json_is_rejected(self):
        for raw in [b"not-json", b"\xff", b'{"dev_info":{"dev_name":"a","dev_name":"b"}}',
                    b'{"dev_info":{"dev_name":"a"},"dev_info":{"dev_name":"b"}}',
                    b'{"dev_info":{"dev_name":"a"},"other":NaN}',
                    b'{"dev_info":{"dev_name":"\\ud800"}}']:
            with self.subTest(raw=raw):
                self.config.write_bytes(raw)
                self.assertIsNone(self.collect()["name"])
        self.config.write_bytes(b'{"dev_info":{"dev_name":"name"}}\0\0')
        self.assertEqual(self.collect()["name"], "name")

    def test_file_size_limit_directory_and_read_failure(self):
        self.write({"dev_info": {"dev_name": "name"}})
        with mock.patch.object(vendor_device, "MAX_CONFIG_BYTES", 5):
            self.assertIsNone(self.collect()["name"])
        with mock.patch.object(vendor_device.os, "open", side_effect=PermissionError("do-not-return-path")):
            result = self.collect()
        self.assertNotIn("do-not-return-path", str(result))
        self.assertIsNone(result["name"])
        self.config.unlink()
        self.config.mkdir()
        self.assertIsNone(self.collect()["name"])

    def test_replacement_or_mutation_during_read_is_rejected(self):
        self.write({"dev_info": {"dev_name": "name"}})
        actual_read = os.read

        def changed(descriptor, size):
            raw = actual_read(descriptor, size)
            self.config.write_bytes(raw + b" ")
            return raw

        with mock.patch.object(vendor_device.os, "read", side_effect=changed):
            self.assertIsNone(self.collect()["name"])
        self.write({"dev_info": {"dev_name": "name"}})
        original = vendor_device._regular_path
        calls = []

        def changed_path(path):
            calls.append(path)
            if len(calls) > 1:
                raise ValueError("changed path")
            return original(path)

        with mock.patch.object(vendor_device, "_regular_path", side_effect=changed_path):
            self.assertIsNone(self.collect()["name"])

    def test_symlink_configuration_is_rejected(self):
        target = self.root / "other.txt"
        target.write_text('{"dev_info":{"dev_name":"outside"}}', encoding="utf-8")
        try:
            self.config.symlink_to(target)
        except OSError:
            self.skipTest("symbolic links unavailable")
        self.assertIsNone(self.collect()["name"])


if __name__ == "__main__":
    unittest.main()
