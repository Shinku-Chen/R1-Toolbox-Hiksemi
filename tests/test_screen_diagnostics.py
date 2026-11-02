"""Small-screen fill diagnostics stay bounded and credential-free."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))

import screen_diagnostics


class ScreenDiagnosticsTests(unittest.TestCase):
    def test_bounded_event_ring_records_only_safe_classes(self):
        sequence = screen_diagnostics.note_request(
            kind='fill', origin='null', token_present=True, outcome='received')
        self.assertIsInstance(sequence, int)
        screen_diagnostics.note_outcome(sequence, token_match=True, outcome='completed')
        event = [row for row in screen_diagnostics.snapshot()['events']
                 if row['sequence'] == sequence][-1]
        self.assertEqual(event['kind'], 'fill')
        self.assertEqual(event['origin'], 'null')
        self.assertIs(event['token_present'], True)
        self.assertIs(event['token_match'], True)
        self.assertEqual(event['outcome'], 'completed')
        self.assertNotIn('token', {key for key in event if key != 'token_present' and key != 'token_match'})

    def test_ring_never_exceeds_limit(self):
        for index in range(40):
            screen_diagnostics.note_request(
                kind='fill', origin='http://127.0.0.1:18731', token_present=False,
                outcome='received' if index % 2 else 'preflight_allowed')
        snapshot = screen_diagnostics.snapshot()
        self.assertEqual(snapshot['schema_version'], 1)
        self.assertEqual(snapshot['limit'], 8)
        self.assertLessEqual(len(snapshot['events']), 8)
        self.assertTrue(all(row['origin'] in ('none', 'null', 'loopback', 'other')
                            for row in snapshot['events']))

    def test_origin_class_does_not_keep_full_foreign_url(self):
        sequence = screen_diagnostics.note_request(
            kind='fill', origin='https://private.example/path?secret=1', outcome='received')
        event = [row for row in screen_diagnostics.snapshot()['events']
                 if row['sequence'] == sequence][-1]
        self.assertEqual(event['origin'], 'other')
        self.assertNotIn('private.example', str(event))

    def test_screen_token_reader_keeps_electron_bridge_fallbacks(self):
        source = (ROOT / 'ui' / 'assets' / 'panel.js').read_text(encoding='utf-8')
        self.assertIn("window.require('process').env", source)
        self.assertIn("window.process.env", source)
        self.assertIn("window.require('node:process').env", source)
        self.assertIn('本机控制令牌未就绪', source)
