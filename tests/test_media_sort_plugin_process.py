"""Synthetic process identity fixtures; never run a vendor binary or open a DB.

Linux cases keep real readlink/stat/open/fstat and kernel-identity parsing.
Namespace identities are real hardlinks, the process root links to the fixture
root, and the executable is a four-byte TEST file. Only its expected size/hash
and the fixture file-owner UID are normalized. This is not vendor/NAS evidence.
"""

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


RUNTIME = Path(__file__).resolve().parents[1] / 'runtime'
sys.path.insert(0, str(RUNTIME))
import media_sort_plugin as plugin


def write_stat(folder, *, pid=None, name='scraper', start=123, complete=True):
    """Build all 52 Linux stat fields, including field 22/starttime."""
    fields = ['S'] + ['0'] * 49
    fields[19] = str(start)
    if not complete:
        fields = fields[:20]
    number = folder.name if pid is None else str(pid)
    (folder / 'stat').write_bytes(
        (number + ' (' + name + ') ' + ' '.join(fields) + '\n').encode('ascii'))


def elf_read_hook(read, fstat, binary_identity, on_elf_read):
    """Leave shared proc reads intact; trigger once on the confirmed ELF FD."""
    triggered = False

    def read_block(descriptor, limit):
        nonlocal triggered
        block = read(descriptor, limit)
        if block and not triggered:
            try:
                info = fstat(descriptor)
            except OSError:
                return block
            if (info.st_dev, info.st_ino) == binary_identity:
                on_elf_read(block)
                triggered = True
        return block

    return read_block


class MediaSortProcessPortableTests(unittest.TestCase):
    def identity_folder(self):
        directory = tempfile.TemporaryDirectory(prefix='r1-media-process-parser-')
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name) / '41'
        folder.mkdir()
        (folder / 'comm').write_bytes(b'scraper\n')
        write_stat(folder)
        return folder

    def test_real_kernel_identity_accepts_complete_synthetic_stat(self):
        folder = self.identity_folder()
        self.assertEqual(plugin._kernel_identity(folder), (41, b'scraper', 123))

    def test_real_kernel_identity_rejects_truncation_pid_and_comm_mismatch(self):
        folder = self.identity_folder()
        for changes in ({'complete': False}, {'pid': 42}, {'name': 'different'}):
            with self.subTest(changes=changes):
                write_stat(folder, **changes)
                self.assertIsNone(plugin._kernel_identity(folder))
        write_stat(folder)
        (folder / 'stat').write_bytes((folder / 'stat').read_bytes().rstrip(b'\n'))
        self.assertIsNone(plugin._kernel_identity(folder))

    def test_opaque_arguments_rejected_before_discovery_or_database_access(self):
        value = plugin.MediaSortPlugin('synthetic-state-not-created')
        invalid = ((None, 'b' * 64), ('A' * 32, 'b' * 64),
                   ('a' * 31, 'b' * 64), ('a' * 32, 'B' * 64),
                   ('a' * 32, b'b' * 64), ('a' * 32, 'b' * 63))
        with mock.patch.object(plugin, '_discover') as discover, \
                mock.patch.object(plugin, '_Opened') as opened, \
                mock.patch.object(plugin.sqlite3, 'connect') as connect:
            for operation in (value.apply, value.revert):
                for candidate, version in invalid:
                    with self.subTest(operation=operation.__name__, candidate=candidate):
                        with self.assertRaises(plugin.MediaSortError) as error:
                            operation(candidate, version, confirmed=True, current=lambda: True)
                        self.assertEqual(error.exception.code, 'invalid_arguments')
            discover.assert_not_called()
            opened.assert_not_called()
            connect.assert_not_called()


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_NOFOLLOW'),
                     'Linux real proc-style links and file descriptors required')
class MediaSortProcessLinuxTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='r1-media-process-real-fds-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.proc = self.root / 'proc'
        self.local_ns = self.proc / 'self/ns'
        self.local_ns.mkdir(parents=True)
        for kind in ('mnt', 'net', 'pid'):
            (self.local_ns / kind).write_bytes(kind.encode('ascii'))
        self.binary = self.root / 'fixture-executable'
        self.binary.write_bytes(b'TEST')
        self.binary.chmod(0o600)
        self.guard = mock.Mock(return_value=None)

        # A trusted test fixture is owned by the current test user. Process
        # status Uid remains unmodified: production still requires four zeros.
        uid = os.getuid()

        def fixture_owned(value, mask=0o022):
            if value.st_uid != uid or value.st_mode & mask:
                raise plugin.MediaSortError('unavailable')

        for patcher in (mock.patch.object(plugin, '_SHA', hashlib.sha256(b'TEST').hexdigest()),
                        mock.patch.object(plugin, '_ELF_SIZE', 4),
                        mock.patch.object(plugin, '_owned', side_effect=fixture_owned)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folder = self.make_process(41)

    def make_process(self, pid, *, directory='/data', name='scraper', start=123):
        folder = self.proc / str(pid)
        (folder / 'ns').mkdir(parents=True)
        (folder / 'comm').write_bytes(name.encode('ascii') + b'\n')
        write_stat(folder, name=name, start=start)
        (folder / 'status').write_bytes(b'Name:\tscraper\nUid:\t0\t0\t0\t0\n')
        for kind in ('mnt', 'net', 'pid'):
            os.link(self.local_ns / kind, folder / 'ns' / kind)
        (folder / 'root').symlink_to(self.root, target_is_directory=True)
        (folder / 'exe').symlink_to(self.binary)
        (folder / 'cwd').symlink_to('/base', target_is_directory=True)
        self.cmdline(folder, ['/fixture/scraper', '-d', directory])
        return folder

    @staticmethod
    def cmdline(folder, argv):
        (folder / 'cmdline').write_bytes(
            b'\0'.join(value.encode('utf-8') for value in argv) + b'\0')

    def process(self, *, pid=41, expected=None, guard=None):
        return plugin._process(self.root, pid, self.guard if guard is None else guard,
                               expected=expected)

    def assert_unavailable(self, **kwargs):
        with self.assertRaises(plugin.MediaSortError) as error:
            self.process(**kwargs)
        self.assertEqual(error.exception.code, 'unavailable')

    def test_real_fixture_binds_pid_start_hash_namespace_root_and_absolute_directory(self):
        result = self.process()
        self.assertEqual(result[:3], (41, 123, str(self.binary)))
        self.assertEqual(result[3][0:2], (self.binary.stat().st_dev, self.binary.stat().st_ino))
        self.assertEqual(result[4], tuple(
            (os.stat(self.local_ns / kind).st_dev, os.stat(self.local_ns / kind).st_ino)
            for kind in ('mnt', 'net', 'pid')))
        self.assertIsNone(result[5])
        self.assertEqual(result[6], 'data/scraper.db')
        self.assertEqual(result[7], hashlib.sha256((self.folder / 'cmdline').read_bytes()).hexdigest())
        self.assertEqual(self.process(expected=result), result)
        self.assertGreater(self.guard.call_count, 0)

    def test_relative_directory_uses_readlink_of_dangling_absolute_cwd_only(self):
        self.cmdline(self.folder, ['/fixture/scraper', '-d', 'relative/data'])
        self.assertEqual(os.readlink(self.folder / 'cwd'), '/base')
        result = self.process()
        self.assertEqual(result[5:7], ('/base', 'base/relative/data/scraper.db'))

    def test_missing_duplicate_trailing_empty_and_oversized_data_argument_rejected(self):
        cases = (['/fixture/scraper'], ['/fixture/scraper', '-d'],
                 ['/fixture/scraper', '-d', ''],
                 ['/fixture/scraper', '-d', '/data', '-d', '/other'],
                 ['/fixture/scraper', '-d', '/' + 'x' * 4096])
        for argv in cases:
            with self.subTest(argument_count=len(argv)):
                self.cmdline(self.folder, argv)
                self.assert_unavailable()

    def test_unterminated_cmdline_rejected(self):
        (self.folder / 'cmdline').write_bytes(b'/fixture/scraper\0-d\0/data')
        self.assert_unavailable()

    def test_each_nonroot_status_uid_and_missing_uid_rejected(self):
        for index in range(4):
            values = ['0'] * 4
            values[index] = '1'
            with self.subTest(uid_column=index):
                (self.folder / 'status').write_bytes(
                    ('Uid:\t' + '\t'.join(values) + '\n').encode('ascii'))
                self.assert_unavailable()
        (self.folder / 'status').write_bytes(b'Name:\tscraper\n')
        self.assert_unavailable()

    def test_zero_start_and_mismatched_kernel_records_rejected(self):
        for changes in ({'start': 0}, {'complete': False}, {'pid': 42}, {'name': 'other'}):
            with self.subTest(changes=changes):
                write_stat(self.folder, **changes)
                self.assert_unavailable()

    def test_wrong_sha_rejected_with_real_file_hash(self):
        self.binary.write_bytes(b'FAIL')
        self.assert_unavailable()

    def test_wrong_size_rejected(self):
        self.binary.write_bytes(b'larger fixture')
        self.assert_unavailable()

    def test_group_writable_executable_and_extra_hardlink_rejected(self):
        self.binary.chmod(0o660)
        self.assert_unavailable()
        self.binary.chmod(0o600)
        os.link(self.binary, self.root / 'extra-executable-link')
        self.assert_unavailable()

    def test_each_different_namespace_inode_rejected(self):
        for kind in ('mnt', 'net', 'pid'):
            with self.subTest(namespace=kind):
                remote = self.folder / 'ns' / kind
                remote.unlink()
                remote.write_bytes(b'different namespace')
                try:
                    self.assert_unavailable()
                finally:
                    remote.unlink()
                    os.link(self.local_ns / kind, remote)

    def test_different_root_inode_rejected(self):
        other = self.root / 'different-root'
        other.mkdir()
        (self.folder / 'root').unlink()
        (self.folder / 'root').symlink_to(other, target_is_directory=True)
        self.assert_unavailable()

    def test_deleted_or_relative_executable_name_rejected_before_open(self):
        for target in (str(self.binary) + ' (deleted)', 'relative-executable'):
            with self.subTest(target=target):
                (self.folder / 'exe').unlink()
                (self.folder / 'exe').symlink_to(target)
                self.assert_unavailable()

    def test_deleted_or_relative_cwd_rejected(self):
        self.cmdline(self.folder, ['/fixture/scraper', '-d', 'relative'])
        for target in ('/base (deleted)', 'relative-base'):
            with self.subTest(target=target):
                (self.folder / 'cwd').unlink()
                (self.folder / 'cwd').symlink_to(target, target_is_directory=True)
                self.assert_unavailable()

    def test_expected_revalidation_rejects_pid_reuse(self):
        expected = self.process()
        write_stat(self.folder, start=124)
        self.assert_unavailable(expected=expected)

    def test_expected_revalidation_rejects_executable_identity_change(self):
        expected = self.process()
        self.binary.chmod(0o400)
        self.assert_unavailable(expected=expected)

    def test_expected_revalidation_rejects_cmdline_change_with_same_directory(self):
        expected = self.process()
        self.cmdline(self.folder, ['/fixture/scraper', '-d', '/data', '--fixture-option'])
        self.assert_unavailable(expected=expected)

    def test_pid_generation_change_during_real_hash_read_is_rejected(self):
        real_read, real_identity = plugin.os.read, plugin._kernel_identity
        binary_identity = (self.binary.stat().st_dev, self.binary.stat().st_ino)
        for observed in (False, True):
            with self.subTest(observer=observed):
                write_stat(self.folder, start=123)
                identities, changed_at_read = [], []
                observer = plugin._Diagnostics().process(41) if observed else None

                def capture_identity(folder):
                    value = real_identity(folder)
                    identities.append(value)
                    return value

                def change_after_elf_read(block):
                    # The shared hook has already selected the actual ELF FD;
                    # proc reads may occur before the first identity is known.
                    self.assertTrue(identities)
                    self.assertEqual(identities[0], (41, b'scraper', 123))
                    changed_at_read.append(block)
                    write_stat(self.folder, start=124)

                read_hook = elf_read_hook(real_read, os.fstat, binary_identity, change_after_elf_read)
                with mock.patch.object(plugin.os, 'read', side_effect=read_hook), \
                        mock.patch.object(plugin, '_kernel_identity', side_effect=capture_identity):
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        plugin._process(self.root, 41, self.guard, observer=observer)
                self.assertEqual(changed_at_read, [b'TEST'])
                self.assertEqual((caught.exception.code, caught.exception.stage, caught.exception.reason),
                                 ('unavailable', 'process_final', 'identity_changed'))
                if observer is not None:
                    self.assertEqual(observer.value['status'], 'identity_changed')
                    self.assertTrue(all(not stage['observations']
                                        for stage in observer.value['stages'].values()))

    def test_discover_collects_all_valid_pids_even_for_same_data_directory(self):
        self.make_process(42)
        self.make_process(43, name='other')
        for name in ('0', 'not-a-pid'):
            folder = self.proc / name
            folder.mkdir()
            (folder / 'comm').write_bytes(b'scraper\n')
        found = plugin._discover(self.root, self.guard)
        self.assertEqual(len(found), 2)
        self.assertEqual({source.relative for source in found}, {'data/scraper.db'})
        self.assertEqual({source.processes[0][0] for source in found}, {41, 42})
        self.assertTrue(all(len(source.processes) == 1 for source in found))

    def test_discover_rejects_over_process_limit_instead_of_truncating(self):
        for pid in range(42, 58):
            self.make_process(pid)
        with self.assertRaises(plugin.MediaSortError) as error:
            plugin._discover(self.root, self.guard)
        self.assertEqual(error.exception.code, 'unavailable')


if __name__ == '__main__':
    unittest.main()
