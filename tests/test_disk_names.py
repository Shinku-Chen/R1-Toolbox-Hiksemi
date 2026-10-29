"""Fixed storage-getter fixtures: no NAS, disks, SMART or vendor executables."""

import copy
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import disk_names


def disk(name="sda", serial="SERIAL-A", port=1):
    return {"disk_name": name, "disk_port": port, "disk_serial": serial,
            "disk_id": "NOT-A-SERIAL", "private_path": "/private/media"}


def space(label="家庭照片", members=None, raid=0):
    members = members or [disk()]
    return {"space_name": label, "space_disk_id": ":".join(row["disk_serial"] for row in members),
            "space_type": 2, "is_raid": raid, "port": members[0]["disk_port"],
            "member": [{"port": row["disk_port"], "disk_name": row["disk_name"] + "2",
                        "disk_id": row["disk_serial"], "root_name": row["disk_name"],
                        "private_path": "/private/root"} for row in members]}


def snapshot(disks=None, spaces=None):
    return {"available": True, "reason": None, "source": disk_names.NAME_SOURCE,
            "disks": [disk()] if disks is None else disks,
            "spaces": [space()] if spaces is None else spaces}


def packet(rows):
    return json.dumps({"info": rows}, ensure_ascii=False).encode("utf-8") + b"\0"


class FakeSocket:
    def __init__(self, owner, chunks):
        self.owner, self.chunks = owner, list(chunks)

    def settimeout(self, seconds):
        self.owner.timeouts.append(seconds)

    def bind(self, address):
        self.path = Path(address)
        self.path.touch()
        self.owner.paths.add(self.path)
        self.owner.bound.append(self.path)

    def connect(self, address):
        self.owner.targets.append(address)
        if self.owner.connect_error:
            raise self.owner.connect_error

    def sendall(self, value):
        self.owner.sent.append(value)

    def recv(self, length):
        if self.owner.on_recv:
            callback, self.owner.on_recv = self.owner.on_recv, None
            callback(self.path)
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > length:
            self.chunks.insert(0, chunk[length:])
        return chunk[:length]

    def close(self):
        self.owner.closed += 1


class SocketFactory:
    def __init__(self, target, replies=()):
        self.paths, self.replies = {target}, list(replies)
        self.sent, self.targets, self.bound, self.types, self.timeouts = [], [], [], [], []
        self.closed, self.connect_error, self.on_recv = 0, None, None

    def __call__(self, family, kind):
        self.types.append((family, kind))
        if not self.replies:
            raise AssertionError("未安排的原厂接口调用")
        reply = self.replies.pop(0)
        return FakeSocket(self, reply if isinstance(reply, list) else [reply])


class StorageProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="s-"))
        self.target = self.root / "var/tmp/disk"
        self.target.parent.mkdir(parents=True)
        self.target.touch()
        self.ipc = self.root / "ipc"
        self.sockets = SocketFactory(self.target)
        original = Path.lstat

        def fixture_lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in self.sockets.paths:
                return os.stat_result((stat.S_IFSOCK | 0o700, *info[1:]))
            return info

        self.addCleanup(mock.patch.stopall)
        # These fake transports run on Windows too; Linux CI exercises the
        # platform's real AF_UNIX constant, never a different production path.
        mock.patch.object(socket, "AF_UNIX", getattr(socket, "AF_UNIX", 1), create=True).start()
        mock.patch.object(Path, "lstat", fixture_lstat).start()
        self.chmod = mock.patch.object(disk_names.os, "chmod", wraps=os.chmod).start()
        self.client = disk_names.VendorStorageClient(self.root, self.ipc, self.sockets)

    def test_constructor_is_inert_and_only_fixed_empty_getters_are_sent(self):
        self.assertFalse(self.ipc.exists())
        self.assertEqual(self.sockets.sent, [])
        self.sockets.replies = [packet([disk()]), packet([space()])]
        result = self.client.snapshot()
        self.assertTrue(result["available"])
        self.assertEqual(self.sockets.sent, [b"get_disk_list;", b"get_space_list;"])
        self.assertEqual(self.sockets.types, [(socket.AF_UNIX, socket.SOCK_STREAM)] * 2)
        self.assertEqual(self.sockets.targets, [str(self.target)] * 2)
        self.assertEqual(self.sockets.closed, 2)
        for path in self.sockets.bound:
            self.chmod.assert_any_call(path, 0o700)
            self.assertFalse(path.exists())
        if os.name != "nt":
            self.assertEqual(self.ipc.stat().st_mode & 0o777, 0o700)
        encoded = json.dumps(result)
        self.assertNotIn("private", encoded)
        self.assertNotIn("NOT-A-SERIAL", encoded)

    def test_rejects_arbitrary_commands_before_io(self):
        for command in ("space_rename_new;", "get_disk_list;evil", "get_disk_list", "emhistor;get_space_list;", None):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.client._request(command)
        self.assertEqual(self.sockets.sent, [])
        self.assertFalse(self.ipc.exists())

    def test_requires_complete_utf8_json_nul_and_eof(self):
        bad = (b"", b"-1\0", b'{"info":[]}', b'{"info":[]}\0extra', b'{"info":[]}\0\0',
               b'{"info":[]', b'\xff\0', b'{"info":[],"info":[]}\0', b'{"info":NaN}\0',
               b'{"info":{}}\0', b'{"info":[null]}\0')
        for reply in bad:
            with self.subTest(reply=reply):
                self.sockets.replies = [reply]
                self.assertFalse(self.client.snapshot()["available"])
                self.assertFalse(self.sockets.bound[-1].exists())
        self.sockets.replies = [[b'{"in', b'fo":', b'[]}\0'], packet([])]
        self.assertTrue(self.client.snapshot()["available"])

    def test_safe_error_codes_identify_failed_getter_without_exception_contents(self):
        cases = ((b'-1\0', "vendor_error"), (b'{"info":[]}', "frame_incomplete"),
                 (b'{"info":[]}\0extra', "frame_extra"), (b'\xff\0', "invalid_utf8"),
                 (b'not-json\0', "invalid_json"), (b'{"info":{}}\0', "schema_invalid"),
                 ([TimeoutError("/private/path SECRET-SERIAL")], "timeout"),
                 ([ConnectionRefusedError("/private/path SECRET-SERIAL")], "connect_failed"))
        for reply, code in cases:
            self.sockets.replies = [reply]
            result = self.client.snapshot()
            self.assertEqual(result["error_code"], code)
            self.assertEqual(result["failed_getter"], "get_disk_list;")
            self.assertNotIn("SECRET-SERIAL", json.dumps(result))
            self.assertNotIn("/private/path", json.dumps(result))
        self.sockets.replies = [packet([]), b'not-json\0']
        self.assertEqual(self.client.snapshot()["failed_getter"], "get_space_list;")

    def test_response_bound_is_exact_and_oversize_is_not_truncated_to_success(self):
        valid = b'{"info":[],"padding":"' + b'x' * (disk_names.MAX_RESPONSE - 25) + b'"}\0'
        self.assertEqual(len(valid), disk_names.MAX_RESPONSE)
        self.sockets.replies = [valid, packet([])]
        self.assertTrue(self.client.snapshot()["available"])
        for reply in (valid + b'x', b'x' * disk_names.MAX_RESPONSE, packet([disk()] * (disk_names.MAX_ROWS + 1))):
            self.sockets.replies = [reply]
            self.assertFalse(self.client.snapshot()["available"])

    def test_timeout_connect_error_and_total_deadline_leave_no_client_inode(self):
        for error in (TimeoutError("fixture"), OSError("fixture")):
            self.sockets.replies = [[error]]
            self.assertFalse(self.client.snapshot()["available"])
            self.assertFalse(self.sockets.bound[-1].exists())
        self.sockets.replies = [packet([])]
        self.sockets.connect_error = OSError("fixture")
        self.assertFalse(self.client.snapshot()["available"])
        self.assertFalse(self.sockets.bound[-1].exists())
        self.sockets.connect_error = None
        self.sockets.replies = [packet([])]
        with mock.patch.object(disk_names.time, "monotonic", side_effect=[0, 3]):
            self.assertFalse(self.client.snapshot()["available"])
        self.assertFalse(self.sockets.bound[-1].exists())

    def test_cleanup_does_not_unlink_replacement_inode(self):
        self.sockets.replies = [packet([]), packet([])]

        def replace(path):
            path.rename(path.with_suffix(".old"))
            path.write_text("replacement", encoding="utf-8")
            self.sockets.paths.discard(path)

        self.sockets.on_recv = replace
        self.assertTrue(self.client.snapshot()["available"])
        self.assertEqual(self.sockets.bound[0].read_text(encoding="utf-8"), "replacement")

    def test_unsafe_runtime_or_missing_target_never_connects(self):
        self.ipc.write_text("file", encoding="utf-8")
        self.assertFalse(self.client.snapshot()["available"])
        self.assertEqual(self.sockets.sent, [])
        self.target.unlink()
        self.assertFalse(self.client.snapshot()["available"])
        self.assertEqual(self.sockets.sent, [])

    def test_failed_second_getter_does_not_publish_partial_names(self):
        self.sockets.replies = [packet([disk()]), b'-1\0']
        result = self.client.snapshot()
        self.assertFalse(result["available"])
        self.assertEqual(result["disks"], [])
        self.assertEqual(result["spaces"], [])


class DiskNamesTests(unittest.TestCase):
    def names(self, data=None, serials=None):
        return disk_names.read_disk_names({"sda": "SERIAL-A"} if serials is None else serials,
                                          snapshot() if data is None else data)

    def assert_unknown(self, result, name="sda"):
        self.assertIsNone(result["items"][name]["user_name"])
        self.assertEqual(result["items"][name]["space_names"], [])
        self.assertIsNone(result["items"][name]["name_source"])

    def test_exact_unique_serial_matches_both_current_vendor_lists(self):
        result = self.names(serials={"sda": " SERIAL-A\n", "sdb": "serial-a", "sdc": "SERIAL"})
        self.assertTrue(result["available"])
        self.assertEqual(result["items"]["sda"], {"user_name": "家庭照片", "space_names": [],
                                                   "name_source": disk_names.NAME_SOURCE})
        self.assert_unknown(result, "sdb")
        self.assert_unknown(result, "sdc")

    def test_space_type_is_not_used_as_basic_raid_enum(self):
        for kind in (1, 2, 3):
            data = snapshot()
            data["spaces"][0]["space_type"] = kind
            self.assertEqual(self.names(data)["items"]["sda"]["user_name"], "家庭照片")

    def test_disk_list_disk_id_is_never_mistaken_for_serial(self):
        self.assertEqual(self.names()["items"]["sda"]["user_name"], "家庭照片")
        data = snapshot()
        data["disks"][0]["disk_serial"] = "DIFFERENT"
        data["disks"][0]["disk_id"] = "SERIAL-A"
        self.assert_unknown(self.names(data))

    def test_disk_name_port_serial_and_space_member_set_must_all_agree(self):
        for location, field, value in (("disk", "disk_name", "sdb"), ("disk", "disk_port", 2),
                                       ("member", "root_name", "sdb"), ("member", "port", 2),
                                       ("member", "disk_id", "OTHER"), ("space", "port", 2)):
            data = snapshot()
            target = data["disks"][0] if location == "disk" else data["spaces"][0]["member"][0] if location == "member" else data["spaces"][0]
            target[field] = value
            with self.subTest(location=location, field=field):
                self.assert_unknown(self.names(data))

    def test_partition_zone_name_is_not_compared_or_guessed_as_physical_disk(self):
        data = snapshot()
        self.assertEqual(data["spaces"][0]["member"][0]["disk_name"], "sda2")
        self.assertEqual(self.names(data)["items"]["sda"]["user_name"], "家庭照片")
        for root in (None, "/dev/sda", "sda2", "sdb"):
            data["spaces"][0]["member"][0]["root_name"] = root
            self.assert_unknown(self.names(data))

    def test_conflicting_record_with_wrong_serial_still_blocks_its_named_root(self):
        wrong = space("不能使用")
        wrong["member"][0]["disk_id"] = "WRONG-SERIAL"
        self.assert_unknown(self.names(snapshot(spaces=[space(), wrong])))

    def test_duplicate_local_or_vendor_names_ports_and_serials_are_unknown(self):
        self.assert_unknown(self.names(serials={"sda": "SERIAL-A", "sdb": "SERIAL-A"}))
        for extra in (disk(), disk("sdb", "SERIAL-A", 2), disk("sdb", "SERIAL-B", 1), disk("sda", "SERIAL-B", 2)):
            with self.subTest(extra=extra):
                self.assert_unknown(self.names(snapshot(disks=[disk(), extra])))

    def test_duplicate_or_conflicting_basic_spaces_stay_unknown(self):
        for other in (space(), space("影音"), space(None)):
            self.assert_unknown(self.names(snapshot(spaces=[space(), other])))

    def test_raid_name_is_shared_space_never_member_user_name(self):
        disks = [disk(), disk("sdb", "SERIAL-B", 2)]
        data = snapshot(disks=disks, spaces=[space("共享 RAID", disks, 1)])
        result = self.names(data, {"sda": "SERIAL-A", "sdb": "SERIAL-B"})
        for name in ("sda", "sdb"):
            self.assertIsNone(result["items"][name]["user_name"])
            self.assertEqual(result["items"][name]["space_names"], ["共享 RAID"])
        data["spaces"].append(space())
        self.assertIsNone(self.names(data)["items"]["sda"]["user_name"])

    def test_unnamed_raid_membership_blocks_basic_name_too(self):
        self.assert_unknown(self.names(snapshot(spaces=[space(), space(None, raid=1)])))

    def test_default_name_sentinel_does_not_hide_other_disk_names(self):
        disks = [disk(), disk("sdb", "SERIAL-B", 2), disk("sdc", "SERIAL-C", 3)]
        for label in (None, "", "\x04", "x" * 129, "x\x00y", "\ud800"):
            data = snapshot(disks, [space(label, disks[:1]), space("备份", disks[1:2]), space("文档", disks[2:])])
            result = self.names(data, {"sda": "SERIAL-A", "sdb": "SERIAL-B", "sdc": "SERIAL-C"})
            self.assertTrue(result["available"])
            self.assert_unknown(result)
            self.assertEqual(result["items"]["sdb"]["user_name"], "备份")

    def test_missing_invalid_and_ambiguous_fields_never_fabricate_names(self):
        for field in ("member", "is_raid", "port"):
            data = snapshot()
            data["spaces"][0].pop(field)
            self.assert_unknown(self.names(data))
        for value in (True, "1", -1, [], {}):
            data = snapshot()
            data["disks"][0]["disk_port"] = value
            self.assert_unknown(self.names(data))
        self.assert_unknown(self.names(snapshot(disks=[])))
        self.assert_unknown(self.names(snapshot(spaces=[])))
        self.assert_unknown(self.names(snapshot(), {"sda": None}))

    def test_space_disk_id_unverified_encoding_is_not_a_join_key(self):
        for value in (None, "OPAQUE-VENDOR-ID", "SERIAL-A:EXTRA", ""):
            data = snapshot()
            data["spaces"][0]["space_disk_id"] = value
            self.assertEqual(self.names(data)["items"]["sda"]["user_name"], "家庭照片")

    def test_reused_snapshot_does_not_issue_any_rpc_or_read_old_database(self):
        with mock.patch.object(disk_names.VendorStorageClient, "snapshot", side_effect=AssertionError("不能重复查询")), \
                mock.patch.object(Path, "open", side_effect=AssertionError("不能读取迁移数据库")):
            self.assertEqual(self.names()["items"]["sda"]["user_name"], "家庭照片")
        self.assertFalse(self.names({"available": False})["available"])


@unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name != "nt", "Linux CI 验证真实 Unix socket")
class RealStorageSocketTests(unittest.TestCase):
    def test_local_stream_named_client_packets_permissions_and_cleanup(self):
        root = Path(tempfile.mkdtemp(prefix="s-"))
        target = root / "var/tmp/disk"
        target.parent.mkdir(parents=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(target))
        server.listen(2)
        server.settimeout(3)
        requests, peers, failures = [], [], []

        def serve():
            try:
                for response in (packet([disk()]), packet([space()])):
                    client, address = server.accept()
                    with client:
                        peers.append(Path(address))
                        self.assertEqual(Path(address).stat().st_mode & 0o777, 0o700)
                        requests.append(client.recv(1024))
                        client.sendall(response)
            except BaseException as error:
                failures.append(error)
            finally:
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        result = disk_names.read_storage_snapshot(root, root / "ipc")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(result["available"], result)
        self.assertEqual(requests, [b"get_disk_list;", b"get_space_list;"])
        self.assertTrue(all(not path.exists() for path in peers))
        self.assertEqual((root / "ipc").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
