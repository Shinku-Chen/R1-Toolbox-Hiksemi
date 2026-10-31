"""Disposable UID-0 Linux mount tests; no NAS, media, or vendor programs.

CI must also run this file explicitly under sudo and reject all skips. The
only hardware substitution maps the temporary loop device to a stable test
identity; production still rejects loop devices. Mountinfo, namespace events,
read-only ext4, Python worker, JSON, SCM_RIGHTS and fstatvfs are real.
"""
import contextlib
import ctypes
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capacity_mount_service', ROOT / 'runtime/ui_service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


@unittest.skipUnless(sys.platform.startswith('linux') and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'Linux UID-0 disposable mount fixture; mandatory separate CI root step')
class CapacityRealMountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in ('mount', 'umount', 'mkfs.ext4'):
            if shutil.which(name) is None:
                raise RuntimeError('mandatory root mount fixture lacks ' + name)
        # Never change propagation or create test mounts in the runner's
        # original namespace. Failure is a real root-CI failure, not a skip.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.unshare(0x00020000) != 0:  # CLONE_NEWNS
            raise OSError(ctypes.get_errno(), 'root mount fixture unshare failed')
        subprocess.run(['mount', '--make-rprivate', '/'], check=True, capture_output=True, timeout=5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-capacity-mount-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.target = self.directory / 'drives/data'
        self.target.mkdir(parents=True)
        image = self.directory / 'empty.ext4'
        with image.open('wb') as output:
            output.truncate(24 * 1024 * 1024)
        subprocess.run(['mkfs.ext4', '-q', '-F', str(image)], check=True, capture_output=True, timeout=10)
        subprocess.run(['mount', '-o', 'loop,ro', str(image), str(self.target)],
                       check=True, capture_output=True, timeout=10)
        self.mounted = True
        self.addCleanup(self.unmount)
        descriptor = os.open(self.target, os.O_RDONLY | os.O_DIRECTORY)
        try:
            device = os.fstat(descriptor).st_dev
            self.mount_id = service._capacity_fd_mount(descriptor)
            self.assertTrue(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY)
        finally:
            os.close(descriptor)
        self.device = f'{os.major(device)}:{os.minor(device)}'
        self.identity = ('disposable-loop-only', self.device, str(image), 'fixture_loop')
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(mock.patch.object(service, '_CAPACITY_DRIVES_ROOT', str(self.target.parent)))
        self.context.enter_context(mock.patch.object(service, '_capacity_device',
            side_effect=lambda root, device: self.identity if device == self.device else None))
        self.real_popen = subprocess.Popen
        self.sampler = service.StatusSampler()
        self.addCleanup(self.sampler.close_capacity)
        self.workers, self.launched, self.started = [], [], threading.Event()

    def unmount(self):
        if self.mounted:
            subprocess.run(['umount', str(self.target)], check=True, capture_output=True, timeout=5)
            self.mounted = False

    def worker(self, *, stall=False):
        bootstrap = (
            'import importlib.util,sys,time;'
            f'spec=importlib.util.spec_from_file_location("capacity_real_child",{str(ROOT / "runtime/ui_service.py")!r});'
            's=importlib.util.module_from_spec(spec);spec.loader.exec_module(s);'
            f's._CAPACITY_DRIVES_ROOT={str(self.target.parent)!r};'
            f's._capacity_device=lambda root,device:{self.identity!r} if device=={self.device!r} else None;')
        if stall:
            bootstrap += 's._capacity_measure_entry=lambda *args,**kwargs:time.sleep(30);'
        bootstrap += 'sys.exit(s._capacity_worker_main())'
        def spawn(args, **kwargs):
            self.launched.append(args)
            child = self.real_popen([sys.executable, '-B', '-c', bootstrap], **kwargs)
            self.workers.append(child)
            self.started.set()
            return child
        return mock.patch.object(service.subprocess, 'Popen', side_effect=spawn)

    def assert_reaped_without_another_refresh(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.sampler._capacity_worker is not None:
            time.sleep(.01)
        self.assertIsNone(self.sampler._capacity_worker)
        for worker in self.workers:
            self.assertIsNotNone(worker.poll())
            self.assertTrue(worker.stdin.closed)
            self.assertTrue(worker.stdout.closed)

    def test_real_readonly_worker_and_no_long_lived_mount_pin(self):
        with self.worker():
            receipt = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(receipt['status'], 'complete')
        self.assertEqual(receipt['results'][0]['mount_id'], self.mount_id)
        self.assertEqual(self.launched[0][1], '-B')
        self.assertEqual(self.launched[0][-1], '--capacity-worker')
        self.assertEqual(self.sampler.capacity_snapshot()[0]['capacity_status'], 'cached')
        self.assertNotIn('anchor', self.sampler._capacity_cache[self.mount_id])
        # A held O_RDONLY/O_PATH mount pin would make this ordinary unmount
        # fail EBUSY. No lazy/forced unmount is allowed in this assertion.
        self.unmount()
        self.assertTrue(self.sampler._capacity_watch.changed())
        self.assertEqual(self.sampler.capacity_snapshot(), [])
        self.assertFalse(self.sampler._capacity_cache)

    def test_unrelated_namespace_event_invalidates_identical_selected_mount(self):
        with self.worker():
            self.sampler.refresh_capacity(authorizer=lambda: True)
        other = self.directory / 'unrelated'
        other.mkdir()
        subprocess.run(['mount', '-t', 'tmpfs', 'tmpfs', str(other)], check=True, capture_output=True, timeout=5)
        try:
            row = self.sampler.capacity_snapshot()[0]
            self.assertIsNone(row['total_bytes'])
            self.assertFalse(self.sampler._capacity_cache)
        finally:
            subprocess.run(['umount', str(other)], check=True, capture_output=True, timeout=5)

    def test_real_timeout_kills_then_idle_reaper_closes_pipes(self):
        with self.worker(stall=True), mock.patch.object(service, '_CAPACITY_WINDOW', .2):
            before = time.monotonic()
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertLess(time.monotonic() - before, 1.5)
        self.assert_reaped_without_another_refresh()
        self.assertFalse(self.sampler._capacity_cache)

    def test_close_during_real_worker_discards_request_and_reaps(self):
        outcome = []
        def refresh():
            try:
                self.sampler.refresh_capacity(authorizer=lambda: True)
            except service.CapacityRefreshError as error:
                outcome.append(error.reason)
        with self.worker(stall=True):
            thread = threading.Thread(target=refresh, daemon=True)
            thread.start()
            self.assertTrue(self.started.wait(3))
            self.sampler.close_capacity()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ['unavailable'])
        self.assert_reaped_without_another_refresh()
        self.assertFalse(self.sampler._capacity_cache)

    def test_revoke_during_real_worker_discards_request_and_reaps(self):
        allowed, outcome = [True], []
        def refresh():
            try:
                self.sampler.refresh_capacity(authorizer=lambda: allowed[0])
            except service.CapacityRefreshError as error:
                outcome.append(error.reason)
        with self.worker(stall=True):
            thread = threading.Thread(target=refresh, daemon=True)
            thread.start()
            self.assertTrue(self.started.wait(3))
            allowed[0] = False
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ['authorization_denied'])
        self.assert_reaped_without_another_refresh()
        self.assertFalse(self.sampler._capacity_cache)


if __name__ == '__main__':
    unittest.main()
