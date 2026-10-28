"""Read-only vendor-name fixtures; no NAS, block devices, or real commands."""

import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import disk_names
from insights import InsightsCollector


class DiskNamesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r1-disk-names-test-"))
        self.config = self.root / "tmp/nand_config"
        self.config.mkdir(parents=True)
        self.db = self.config / "disk_space.db"

    def create(self, rows=()):
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE space_table(type TEXT,name TEXT,member TEXT,disk_id TEXT)")
            connection.executemany("INSERT INTO space_table VALUES(?,?,?,?)", rows)

    def names(self, serials=None):
        return disk_names.read_disk_names(serials or {"sda": "SERIAL-A"}, self.config)

    def assert_unknown(self, result, name="sda"):
        self.assertIsNone(result["items"][name]["user_name"])
        self.assertEqual(result["items"][name]["space_names"], [])
        self.assertIsNone(result["items"][name]["name_source"])

    def test_basic_name_requires_exact_unique_serial_and_only_trims(self):
        self.create([("basic", "  家庭照片  ", "1", "SERIAL-A")])
        result = self.names({"sda": " SERIAL-A\n", "sdb": "serial-a", "sdc": "SERIAL"})
        self.assertTrue(result["available"])
        self.assertEqual(result["items"]["sda"], {"user_name": "家庭照片", "space_names": [],
                                                   "name_source": "hiksemi_space_db"})
        self.assert_unknown(result, "sdb")
        self.assert_unknown(result, "sdc")

    def test_raid_is_shared_space_and_member_ports_do_not_map_devices(self):
        self.create([("raid", "家庭 RAID", "1:2", "SERIAL-A:SERIAL-B"),
                     ("basic", "未知端口", "3", "ANOTHER-SERIAL")])
        result = self.names({"sda": "SERIAL-A", "sdb": "SERIAL-B", "sdc": None})
        for name in ("sda", "sdb"):
            self.assertIsNone(result["items"][name]["user_name"])
            self.assertEqual(result["items"][name]["space_names"], ["家庭 RAID"])
        self.assert_unknown(result, "sdc")

    def test_duplicate_physical_serials_are_not_assigned_a_name(self):
        self.create([("basic", "照片", "1", "SERIAL-A")])
        result = self.names({"sda": "SERIAL-A", "sdb": " SERIAL-A "})
        self.assert_unknown(result, "sda")
        self.assert_unknown(result, "sdb")

    def test_conflicting_basic_names_stay_unknown(self):
        self.create([("basic", "照片", "1", "SERIAL-A"),
                     ("basic", "影音", "2", "SERIAL-A")])
        self.assert_unknown(self.names())

    def test_empty_name_does_not_become_user_label_or_hide_a_conflict(self):
        self.create([("basic", "", "1", "SERIAL-A"),
                     ("basic", "照片", "1", "SERIAL-A")])
        self.assert_unknown(self.names())

    def test_missing_database_is_not_created(self):
        result = self.names()
        self.assertFalse(result["available"])
        self.assert_unknown(result)
        self.assertFalse(self.db.exists())

    def test_corrupt_database_is_unavailable(self):
        self.db.write_bytes(b"broken database")
        self.assertFalse(self.names()["available"])

    def test_bad_schema_and_views_are_unavailable(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE another_table(id INTEGER)")
            connection.execute("CREATE VIEW space_table AS SELECT 'basic' AS type")
        self.assertFalse(self.names()["available"])

    def test_long_name_and_control_characters_are_rejected(self):
        self.create([("basic", "长" * 129, "1", "SERIAL-A")])
        self.assert_unknown(self.names())
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE space_table SET name=?", ("照片\x00其他",))
        self.assert_unknown(self.names())

    def test_vendor_unnamed_sentinel_and_null_do_not_hide_another_disks_name(self):
        self.create([("basic", "\x04", "1", "SERIAL-A"),
                     ("basic", None, "2", "SERIAL-B"),
                     ("basic", "家庭资料", "3", "SERIAL-C")])
        result = self.names({"sda": "SERIAL-A", "sdb": "SERIAL-B", "sdc": "SERIAL-C"})
        self.assertTrue(result["available"])
        self.assert_unknown(result, "sda")
        self.assert_unknown(result, "sdb")
        self.assertEqual(result["items"]["sdc"]["user_name"], "家庭资料")

    def test_row_and_file_limits_reject_partial_names(self):
        self.create([("basic", "照片", "1", "SERIAL-A")] * (disk_names.MAX_ROWS + 1))
        self.assertFalse(self.names()["available"])
        with mock.patch.object(disk_names, "MAX_DB_BYTES", self.db.stat().st_size - 1):
            self.assertFalse(self.names()["available"])

    def test_database_is_read_only_including_mtime_hash_and_sidecars(self):
        self.create([("basic", "照片", "1", "SERIAL-A")])
        before = (self.db.stat().st_mtime_ns, hashlib.sha256(self.db.read_bytes()).digest(),
                  sorted(path.name for path in self.config.iterdir()))
        for _ in range(3):
            self.assertEqual(self.names()["items"]["sda"]["user_name"], "照片")
        after = (self.db.stat().st_mtime_ns, hashlib.sha256(self.db.read_bytes()).digest(),
                 sorted(path.name for path in self.config.iterdir()))
        self.assertEqual(before, after)

    def test_wal_without_shm_never_returns_old_name_or_creates_sidecars(self):
        source = self.root / "wal-source.db"
        writer = sqlite3.connect(source)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE space_table(type,name,member,disk_id)")
            writer.execute("INSERT INTO space_table VALUES('basic','旧名称','1','SERIAL-A')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            writer.execute("UPDATE space_table SET name='新名称'")
            writer.commit()
            # Copy a real uncheckpointed WAL fixture, deliberately without SHM.
            self.db.write_bytes(source.read_bytes())
            wal_bytes = source.with_name(source.name + "-wal").read_bytes()
            self.assertGreater(len(wal_bytes), 0)
        finally:
            writer.close()
        wal = self.db.with_name(self.db.name + "-wal")
        wal.write_bytes(wal_bytes)
        before = {path.name: path.read_bytes() for path in self.config.iterdir()}
        result = self.names()
        self.assertFalse(result["available"])
        self.assert_unknown(result)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.config.iterdir()})
        self.assertFalse(self.db.with_name(self.db.name + "-shm").exists())

    def test_path_symlinks_are_rejected(self):
        target = self.root / "real.db"
        with sqlite3.connect(target) as connection:
            connection.execute("CREATE TABLE space_table(type,name,member,disk_id)")
        try:
            self.db.symlink_to(target)
        except OSError:
            self.skipTest("Windows 未授权创建符号链接；Linux CI 验证 NOFOLLOW 边界")
        self.assertFalse(self.names()["available"])
        actual = self.root / "actual-config"
        actual.mkdir()
        with sqlite3.connect(actual / "disk_space.db") as connection:
            connection.execute("CREATE TABLE space_table(type,name,member,disk_id)")
        linked = self.root / "linked-config"
        linked.symlink_to(actual, target_is_directory=True)
        self.assertFalse(disk_names.read_disk_names({"sda": "SERIAL-A"}, linked)["available"])

    def test_query_progress_budget_is_enforced(self):
        self.create([("basic", "照片", "1", "SERIAL-A")])
        with mock.patch.object(disk_names, "QUERY_SECONDS", -1):
            self.assertFalse(self.names()["available"])

    def test_collector_uses_sysfs_and_udev_without_any_new_command(self):
        self.create([("basic", "家庭照片", "1", "SERIAL-A")])
        proc, sysroot, run = self.root / "proc", self.root / "sys", self.root / "run"
        proc.mkdir()
        folder = sysroot / "block/sda"
        (folder / "device").mkdir(parents=True)
        (folder / "device/serial").write_text("SERIAL-A\n")
        (folder / "dev").write_text("8:0\n")
        (run / "udev/data").mkdir(parents=True)
        (run / "udev/data/b8:0").write_text("E:ID_SERIAL_SHORT=SERIAL-A\n")
        collector = InsightsCollector(proc, sysroot, page_size=4096, clock_ticks=100,
                                      command_finder=lambda _: None)
        with mock.patch.object(collector, "runner", side_effect=AssertionError("禁止新增命令")):
            rows = collector._disks()
        self.assertEqual(rows["items"][0]["user_name"], "家庭照片")
        self.assertEqual(collector.disk_config_root, self.config)
        self.assertNotIn("serial", rows["items"][0], "内部匹配序列号不新增到公开快照")
        collector._state["disks"] = rows
        with mock.patch("insights.read_disk_names", side_effect=AssertionError("HTTP 不读 DB")):
            self.assertEqual(collector.snapshot()["disks"]["items"][0]["user_name"], "家庭照片")
        (run / "udev/data/b8:0").write_text("E:ID_SERIAL_SHORT=DIFFERENT-SERIAL\n")
        self.assertIsNone(collector._disk_serial("sda", folder))


if __name__ == "__main__":
    unittest.main()
