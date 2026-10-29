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

    def test_cache_errors_identify_fixed_failure_without_file_contents(self):
        cases = ((b'not-json', 'invalid_json'), (b'\xff', 'invalid_utf8'),
                 (b'{"list":[],"list":[]}', 'json_duplicate_key'),
                 (b'{"list":[],"x":NaN}', 'json_nonstandard_constant'),
                 (b'{"private-token":"never-return"}', 'unsupported_cache_list'),
                 (b'{"list":[null]}', 'unsupported_application_entry'),
                 (b'{"list":[{"module_name":"a","version":"1","status":2}]}',
                  'unsupported_cached_status'))
        for raw, code in cases:
            with self.subTest(code=code):
                self.path.write_bytes(raw)
                result = self.collect()
                self.assertEqual(result['error_code'], code)
                self.assertIsNone(result['data'])
                self.assertNotIn('never-return', str(result))

    def probe(self, *, available=True, error=None):
        return {'schema_version': 1, 'source': apps.PROBE_SOURCE, 'available': available,
                'status': 'ok' if available else 'unavailable', 'error_code': error,
                'sampled_at': '2026-10-29T02:03:04.123+00:00',
                'credential': {'token': 'never-return'}, 'reason': 'never-return',
                'native': {'access_token': 'never-return'},
                'data': {'apps': [{'name': '相册', 'version': '1',
                                   'installation_status_code': 0, 'running': True,
                                   'path': '/private/apps', 'token': 'never-return'}],
                         'token': 'never-return', 'complete': True, 'live': True}}

    def test_successful_explicit_snapshot_is_reprojected_and_passively_read(self):
        state = self.root / 'toolbox-state'
        probe = self.probe()
        self.assertTrue(apps.record_application_snapshot(state, probe))
        raw = (state / apps.SNAPSHOT_FILE).read_text(encoding='utf-8')
        for value in ('never-return', '/private', 'credential', 'access_token'):
            self.assertNotIn(value, raw)
        with mock.patch('socket.socket', side_effect=AssertionError('passive sockets forbidden')), \
                mock.patch('subprocess.run', side_effect=AssertionError('no commands')):
            result = apps.collect(self.root, state_dir=state)
        self.assertTrue(result['available'])
        self.assertEqual(result['source'], apps.SNAPSHOT_SOURCE)
        self.assertEqual(result['status'], 'cached')
        self.assertEqual(result['data']['cache_modified_at'], probe['sampled_at'])
        self.assertFalse(result['data']['live'])
        self.assertFalse(result['data']['complete'])
        self.assertIsNone(result['data']['apps'][0]['running'])
        self.assertFalse(self.path.exists())
        self.assertEqual([path.name for path in state.iterdir()], [apps.SNAPSHOT_FILE])
        if os.name != 'nt':
            self.assertEqual((state / apps.SNAPSHOT_FILE).stat().st_mode & 0o777, 0o600)

    def test_failed_refresh_replaces_success_and_does_not_relabel_old_data(self):
        state = self.root / 'toolbox-state'
        self.assertTrue(apps.record_application_snapshot(state, self.probe()))
        failure = self.probe(available=False, error='certificate_mismatch')
        failure['sampled_at'] = '2026-10-29T02:05:00.000+00:00'
        self.assertTrue(apps.record_application_snapshot(state, failure))
        result = apps.collect(self.root, state_dir=state)
        self.assertFalse(result['available'])
        self.assertIsNone(result['data'])
        self.assertEqual(result['status'], 'refresh_failed')
        self.assertEqual(result['error_code'], 'certificate_mismatch')
        self.assertEqual(result['cache_error_code'], 'cache_missing')
        self.assertEqual(result['sampled_at'], failure['sampled_at'])
        self.assertNotIn('相册', (state / apps.SNAPSHOT_FILE).read_text(encoding='utf-8'))

    def test_failed_refresh_can_only_fallback_to_explicitly_old_native_cache(self):
        state = self.root / 'toolbox-state'
        self.write()
        os.utime(self.path, (1000000, 1000000))
        self.assertTrue(apps.record_application_snapshot(state, self.probe(available=False, error='timeout')))
        result = apps.collect(self.root, state_dir=state)
        self.assertTrue(result['available'])
        self.assertEqual(result['source'], apps.SOURCE)
        self.assertEqual(result['error_code'], 'timeout')
        self.assertEqual(result['latest_attempt_at'], self.probe()['sampled_at'])
        self.assertTrue(result['data']['cache_modified_at'].startswith('1970-'))
        self.assertIn('失败', result['reason'])

    def test_absent_snapshot_is_not_created_by_get_and_invalid_snapshot_is_not_empty(self):
        state = self.root / 'toolbox-state'
        result = apps.collect(self.root, state_dir=state)
        self.assertFalse(state.exists())
        self.assertEqual(result['status'], 'cache_missing')
        state.mkdir()
        (state / apps.SNAPSHOT_FILE).write_text('{"token":"never-return"}', encoding='utf-8')
        result = apps.collect(self.root, state_dir=state)
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'snapshot_invalid')
        self.assertIsNone(result['data'])
        self.assertNotIn('never-return', str(result))
        # A new explicit attempt can repair a damaged Toolbox-owned file.
        (state / apps.SNAPSHOT_FILE).write_bytes(b'')
        self.assertTrue(apps.record_application_snapshot(state, self.probe()))
        self.assertTrue(apps.collect(self.root, state_dir=state)['available'])

    def test_snapshot_is_revalidated_and_failure_codes_never_copy_upstream_strings(self):
        state = self.root / 'toolbox-state'
        failure = self.probe(available=False, error='never-return /private/token')
        self.assertTrue(apps.record_application_snapshot(state, failure))
        self.assertEqual(apps.collect(self.root, state_dir=state)['error_code'], 'refresh_failed')
        self.assertNotIn('never-return', (state / apps.SNAPSHOT_FILE).read_text(encoding='utf-8'))
        bad = self.probe()
        bad['data']['apps'][0]['name'] = '/private/never-return'
        self.assertFalse(apps.record_application_snapshot(state, bad))
        self.assertFalse(apps.collect(self.root, state_dir=state)['available'])

    def test_snapshot_failed_atomic_write_preserves_file_and_reports_false(self):
        state = self.root / 'toolbox-state'
        self.assertTrue(apps.record_application_snapshot(state, self.probe()))
        previous = (state / apps.SNAPSHOT_FILE).read_bytes()
        with mock.patch.object(apps.os, 'replace', side_effect=OSError('/private/never-return')):
            self.assertFalse(apps.record_application_snapshot(state, self.probe(available=False, error='timeout')))
        self.assertEqual((state / apps.SNAPSHOT_FILE).read_bytes(), previous)
        self.assertEqual(len(list(state.iterdir())), 1)

    def test_snapshot_links_are_rejected_without_touching_target(self):
        state, outside = self.root / 'toolbox-state', self.root / 'outside'
        outside.mkdir()
        try:
            state.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symbolic links unavailable')
        self.assertFalse(apps.record_application_snapshot(state / 'new-dir', self.probe()))
        self.assertFalse((outside / 'new-dir').exists())
        self.assertEqual(apps.collect(self.root, state_dir=state)['status'], 'cache_missing')

    def test_query_projection_accepts_catalog_and_retains_unknowns_as_null(self):
        data = apps.project_application_list({'count': 800, 'list': [
            {'module_name': 'app', 'version': '1', 'status': 4},
            {'module_name': 'unknown', 'version': '/private/token', 'status': 2},
        ]}, self.probe()['sampled_at'])
        self.assertEqual(data['cached_entries'], 2)
        self.assertIsNone(data['apps'][1]['version'])
        self.assertIsNone(data['apps'][1]['installation_status_code'])
        self.assertTrue(all(row['running'] is None for row in data['apps']))
        for malformed in ({}, {'list': [None]}, {'list': [
                {'module_name': 'same'}, {'module_name': 'same'}]}):
            with self.assertRaises(ValueError):
                apps.project_application_list(malformed, self.probe()['sampled_at'])


if __name__ == "__main__":
    unittest.main()
