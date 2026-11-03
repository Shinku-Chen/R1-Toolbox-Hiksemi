"""Persistent display cache never becomes an authorization or live proof."""
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from disk_cache import DiskDisplayCache


class DiskDisplayCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-disk-cache-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'disk-cache.json'

    def status(self):
        return {'disks': [{'mount': '/drives/one', 'device': 'sda', 'total_bytes': 100,
                           'used_bytes': 25, 'capacity_sampled_at': 10,
                           'capacity_status': 'cached'}]}

    def insights(self):
        return {'disks': {'items': [{'id': 'sda', 'model': 'Disk', 'size_bytes': 200,
                                     'temp_c': 33, 'health': {'passed': True},
                                     'smart_available': True, 'smart_fresh': False}]}}

    def test_round_trip_overlays_missing_fields_and_never_stores_serials(self):
        cache = DiskDisplayCache(self.path)
        cache.record(self.status(), self.insights())
        cache2 = DiskDisplayCache(self.path)
        status = cache2.overlay_status({'disks': [{'mount': '/drives/one', 'device': 'sda'}]})
        self.assertEqual(status['disks'][0]['total_bytes'], 100)
        self.assertEqual(status['disks'][0]['used_bytes'], 25)
        insights = cache2.overlay_insights({'disks': {'items': [{'id': 'sda'}]}})
        self.assertEqual(insights['disks']['items'][0]['temp_c'], 33)
        self.assertEqual(insights['disks']['items'][0]['model'], 'Disk')
        raw = self.path.read_text()
        self.assertNotIn('serial', raw)
        if os.name != 'nt':
            self.assertEqual(os.stat(self.path).st_mode & 0o077, 0)

    def test_corrupt_cache_is_empty_and_does_not_raise(self):
        self.path.write_text('{not json')
        cache = DiskDisplayCache(self.path)
        self.assertEqual(cache.capacity, {})
        self.assertEqual(cache.disks, {})
        self.assertEqual(cache.overlay_status({'disks': []}), {'disks': []})

    def test_unmatched_device_is_not_overlaid(self):
        cache = DiskDisplayCache(self.path)
        cache.record(self.status(), self.insights())
        value = cache.overlay_status({'disks': [{'mount': '/drives/one', 'device': 'sdb'}]})
        self.assertNotIn('total_bytes', value['disks'][0])
