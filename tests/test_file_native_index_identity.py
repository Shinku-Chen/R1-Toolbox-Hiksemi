"""Linux metadata/socket fixtures for the fixed index bridge identity gates.

These are not NAS or native ELF acceptance. The SATA fixture uses real sysfs-
shaped directories, symlinks and mountinfo. Its block-node stat is an explicit
injected boundary unless the optional CAP_MKNOD test can create a real node.
The Unix fixture binds a real socket and scans this process's actual kernel fd
links; the fixed proc table path, native ELF proof and root uid are fixture data.
"""

import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_native_index as index
from api_file_operations import FileAccessError


LINUX = sys.platform.startswith('linux') and hasattr(os, 'O_PATH')
ROW = {'id': 'root_' + 'a' * 24, 'mount': '/drives/data/Users/admin'}


@unittest.skipUnless(LINUX, 'Linux sysfs, mknod and proc metadata semantics required')
class InternalSataIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-index-sata-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / 'proc/self'
        self.proc.mkdir(parents=True)
        self.partition = self.root / 'sys/block/sda/sda1'
        self.partition.mkdir(parents=True)
        (self.partition / 'dev').write_text('8:1\n')
        self.device = self.root / 'sys/block/sda/device'
        self.transport('pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:0')
        (self.root / 'dev').mkdir()
        self.node = self.root / 'dev/sda1'
        self.node.write_bytes(b'block stat is an explicit fixture boundary')
        self.rdev, self.mode = os.makedev(8, 1), stat.S_IFBLK | 0o600
        self.mountinfo()

    def mountinfo(self, *, device='8:1', source='/dev/sda1', fsroot='/', duplicate=False):
        line = f'605 1 {device} {fsroot} /drives/data rw - ext4 {source} rw\n'
        (self.proc / 'mountinfo').write_text(line + (line.replace('605 ', '606 ', 1) if duplicate else ''))

    def transport(self, relative):
        target = self.root / 'sys/devices' / relative
        target.mkdir(parents=True, exist_ok=True)
        if self.device.is_symlink():
            self.device.unlink()
        self.device.symlink_to(target, target_is_directory=True)

    def probe(self):
        original = os.stat
        def block_stat(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(self.node):
                self.assertIs(kwargs.get('follow_symlinks'), False)
                return SimpleNamespace(st_mode=self.mode, st_rdev=self.rdev)
            return original(path, *args, **kwargs)
        with mock.patch.object(index.os, 'stat', side_effect=block_stat):
            return index._internal_sata(self.root, ROW)

    def test_one_canonical_sata_partition_binds_all_three_device_numbers(self):
        self.assertTrue(self.probe())

    def test_usb_virtual_and_non_ata_device_paths_are_not_builtin(self):
        for transport in ('pci0000:00/usb1/1-2/ata1/host0', 'virtual/ata1/block/sda',
                          'pci0000:00/nvme/nvme0', 'pci0000:00/ata0/host0'):
            with self.subTest(transport=transport):
                self.transport(transport)
                self.assertFalse(self.probe())

    def test_sysfs_target_outside_fixed_devices_tree_is_refused(self):
        target = self.root / 'outside/ata1/host0'
        target.mkdir(parents=True)
        self.device.unlink()
        self.device.symlink_to(target, target_is_directory=True)
        self.assertFalse(self.probe())

    def test_mount_sysfs_and_block_rdev_must_match_independently(self):
        self.mountinfo(device='8:2')
        self.assertFalse(self.probe())
        self.mountinfo()
        (self.partition / 'dev').write_text('8:2\n')
        self.assertFalse(self.probe())
        (self.partition / 'dev').write_text('8:1\n')
        self.rdev = os.makedev(8, 2)
        self.assertFalse(self.probe())
        self.rdev = os.makedev(8, 1)
        self.assertTrue(self.probe())

    def test_source_cannot_select_another_disk_or_non_partition_node(self):
        for source in ('/dev/sdb1', '/dev/sda', '/dev/sda01', '/dev/nvme0n1p1',
                       '/dev/mapper/storage', 'UUID=fixture', '/dev/sda1/else'):
            with self.subTest(source=source):
                self.mountinfo(source=source)
                self.assertFalse(self.probe())

    def test_regular_character_and_symlink_nodes_are_not_block_proof(self):
        for kind in (stat.S_IFREG, stat.S_IFCHR, stat.S_IFLNK):
            with self.subTest(kind=kind):
                self.mode = kind | 0o600
                self.assertFalse(self.probe())

    def test_duplicate_canonical_missing_and_subroot_mounts_are_refused(self):
        self.mountinfo(duplicate=True)
        self.assertFalse(self.probe())
        self.mountinfo(fsroot='/subdirectory')
        self.assertFalse(self.probe())
        (self.proc / 'mountinfo').write_text('605 1 8:1 / /elsewhere rw - ext4 /dev/sda1 rw\n')
        self.assertFalse(self.probe())

    def test_missing_or_malformed_partition_number_never_means_verified(self):
        for value in ('', '8:01\n', '8:1\n8:1\n', '8:1 junk\n'):
            with self.subTest(value=value):
                (self.partition / 'dev').write_text(value)
                self.assertFalse(self.probe())

    def test_real_block_node_when_cap_mknod_is_available(self):
        self.node.unlink()
        try:
            os.mknod(self.node, stat.S_IFBLK | 0o600, os.makedev(8, 1))
        except (PermissionError, NotImplementedError) as error:
            self.skipTest('CAP_MKNOD unavailable; real block node not exercised: ' + type(error).__name__)
        self.assertTrue(stat.S_ISBLK(self.node.lstat().st_mode))
        # No open/read/ioctl of the node: only real inode and rdev metadata.
        self.assertTrue(index._internal_sata(self.root, ROW))
        self.mountinfo(device='8:2')
        self.assertFalse(index._internal_sata(self.root, ROW))


@unittest.skipUnless(LINUX and hasattr(socket, 'AF_UNIX'), 'Linux real Unix sockets and proc fd links required')
class NativeUnixListenerIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-index-unix-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pid = os.getpid()
        self.target = self.root / 'tmp/UNIX.domain'
        self.target.parent.mkdir()
        self.server = self.listen(self.target)
        self.inode = str(os.fstat(self.server.fileno()).st_ino)
        self.native = {'available': True, 'status': 'verified', 'pid': self.pid,
                       'start_ticks': 40, 'socket_inode': '99', 'connect_host': '127.0.0.1'}
        for directory in ('proc/self/ns', f'proc/{self.pid}/ns', 'proc/net'):
            (self.root / directory).mkdir(parents=True)
        for path in ('proc/self/ns/mnt', f'proc/{self.pid}/ns/mnt'):
            (self.root / path).symlink_to('/proc/self/ns/mnt')
        self.fd = self.root / 'proc' / str(self.pid) / 'fd'
        self.fd.symlink_to('/proc/self/fd', target_is_directory=True)
        self.table()
        self.lstat = Path.lstat
        self.socket_uid = 0
        patcher = mock.patch.object(index.Path, 'lstat', autospec=True, side_effect=self.normalized_owner)
        patcher.start()
        self.addCleanup(patcher.stop)
        factory = mock.patch.object(index, 'require_emhistor_listener', return_value=self.native)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        binary = mock.patch.object(index, '_runtime_binary_matches', return_value=True)
        self.binary = binary.start()
        self.addCleanup(binary.stop)

    def normalized_owner(self, path, *args, **kwargs):
        value = self.lstat(path, *args, **kwargs)
        if os.fspath(path) != os.fspath(self.target):
            return value
        # Root ownership is explicit fixture data on an unprivileged CI runner;
        # mode, inode, times and actual open socket ownership remain real.
        return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino,
            st_mode=value.st_mode, st_uid=self.socket_uid, st_gid=value.st_gid,
            st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns)

    def listen(self, target):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(target))
        server.listen(1)
        return server

    def table(self, *, inode=None, flags='00010000', kind='0001', state='01', duplicate=False):
        header = 'Num       RefCount Protocol Flags    Type St Inode Path\n'
        line = f'0000000000000000: 00000002 00000000 {flags} {kind} {state} {inode or self.inode} /tmp/UNIX.domain\n'
        (self.root / 'proc/net/unix').write_text(header + line + (line if duplicate else ''))

    def probe(self):
        return index._listener_signature(self.root, time.monotonic() + 3.0)

    def refused(self):
        with self.assertRaises(FileAccessError) as caught:
            self.probe()
        self.assertEqual(caught.exception.code, 'native_unavailable')

    def test_fixed_listener_uses_actual_kernel_socket_inode_and_process_fd_links(self):
        result = self.probe()
        self.assertEqual(result[0], (self.pid, 40, '99', '127.0.0.1'))
        self.assertEqual(result[1], self.inode)
        self.assertEqual(result[2][1], self.lstat(self.target).st_ino)
        self.factory.assert_called_once_with(root=self.root, port=20443)
        self.binary.assert_called_once_with(self.root, result[0])

    def test_exact_native_elf_is_mandatory_before_socket_proof(self):
        self.binary.return_value = False
        self.refused()

    def test_mount_namespace_mismatch_is_refused(self):
        native_ns = self.root / f'proc/{self.pid}/ns/mnt'
        native_ns.unlink()
        native_ns.write_bytes(b'not the same namespace inode')
        self.refused()

    def test_nonroot_socket_owner_and_ordinary_path_are_refused(self):
        self.socket_uid = 1000
        self.refused()
        self.socket_uid = 0
        self.target.rename(self.target.parent / 'old.socket')
        self.target.write_bytes(b'ordinary file is not a Unix listener')
        self.refused()

    def test_unix_table_must_have_one_listening_stream_entry(self):
        for flags, kind, state, duplicate in (
                ('00000000', '0001', '01', False), ('00010000', '0002', '01', False),
                ('00010000', '0001', '03', False), ('00010000', '0001', '01', True)):
            with self.subTest(flags=flags, kind=kind, state=state, duplicate=duplicate):
                self.table(flags=flags, kind=kind, state=state, duplicate=duplicate)
                self.refused()

    def test_named_socket_inode_must_be_owned_by_the_exact_native_pid(self):
        self.table(inode=str(int(self.inode) + 100000000000))
        self.refused()
        self.table()
        self.fd.unlink()
        self.fd.mkdir()
        self.refused()

    def test_missing_or_malformed_unix_listener_identity_is_refused(self):
        for inode in ('0', '-1', '12x', '1' * 25):
            with self.subTest(inode=inode):
                self.table(inode=inode)
                self.refused()
        (self.root / 'proc/net/unix').write_text('Num RefCount Protocol Flags Type St Inode Path\n')
        self.refused()

    def test_socket_replacement_during_ownership_scan_is_refused(self):
        scan = index._socket_inodes
        def replace(*args, **kwargs):
            result = scan(*args, **kwargs)
            self.target.rename(self.target.parent / 'old.socket')
            self.listen(self.target)
            return result
        with mock.patch.object(index, '_socket_inodes', side_effect=replace):
            self.refused()

    def test_incomplete_or_expired_fd_scan_cannot_verify_a_listener(self):
        with self.assertRaises(FileAccessError):
            index._listener_signature(self.root, time.monotonic() - 1.0)
        with mock.patch.object(index, '_socket_inodes', return_value=({self.inode}, False)):
            self.refused()


if __name__ == '__main__':
    unittest.main()
