"""Pure LAN IP/CIDR source checks, without sockets or forwarded headers."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from network_access import normalize_peer, normalize_rule, normalize_rules, peer_matches


class NetworkAccessTests(unittest.TestCase):
    def test_peer_keeps_existing_private_and_loopback_boundary(self):
        accepted = {'10.0.0.1': '10.0.0.1', '172.16.0.1': '172.16.0.1',
                    '172.31.255.254': '172.31.255.254', '192.168.31.193': '192.168.31.193',
                    '127.0.0.1': '127.0.0.1', '127.255.255.254': '127.255.255.254',
                    '::1': '::1', 'fc00::1': 'fc00::1', 'FDAB:0000::1': 'fdab::1',
                    '::ffff:192.168.31.193': '192.168.31.193',
                    '::FFFF:c0a8:1fc1': '192.168.31.193'}
        for supplied, canonical in accepted.items():
            with self.subTest(supplied=supplied):
                self.assertEqual(normalize_peer(supplied), canonical)
        for value in ('8.8.8.8', '100.64.0.1', '169.254.0.1', '172.32.0.1',
                      '0.0.0.0', '::', 'fe80::1', '2001:db8::1', '::ffff:8.8.8.8',
                      'localhost', '*.local', '192.168.1.1/24', '192.168.1.1:80',
                      'fd00::1%eth0', '192.168. 1.1', ' 127.0.0.1', '127.0.0.1\n',
                      None, True, 1, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(normalize_peer(value))

    def test_rules_accept_hostbits_masks_and_canonicalize_single_hosts(self):
        cases = {
            '192.168.31.193/24': '192.168.31.0/24',
            '192.168.31.193/255.255.255.0': '192.168.31.0/24',
            '10.10.20.30/8': '10.0.0.0/8', '172.31.1.2/12': '172.16.0.0/12',
            '127.3.4.5/8': '127.0.0.0/8', '192.168.1.2/16': '192.168.0.0/16',
            '192.168.31.193/32': '192.168.31.193', '::1/128': '::1',
            'fDab:1234:5678::abcd/48': 'fdab:1234:5678::/48',
            'fc00:ffff::1/7': 'fc00::/7', 'fd00::123/128': 'fd00::123',
            '::ffff:192.168.31.193/120': '192.168.31.0/24',
            '::ffff:c0a8:1fc1/112': '192.168.0.0/16',
            '::ffff:192.168.31.193/128': '192.168.31.193',
        }
        for supplied, expected in cases.items():
            with self.subTest(supplied=supplied):
                self.assertEqual(normalize_rule(supplied), expected)
                self.assertEqual(normalize_rule(expected), expected)

    def test_rules_reject_public_cross_boundary_zero_masks_and_bad_inputs(self):
        invalid = ('0.0.0.0/0', '10.0.0.1/0', '::/0', '::ffff:192.168.1.1/96',
                   '::ffff:192.168.1.1/95', '::ffff:8.8.8.8/120',
                   '192.168.0.0/15', '172.16.0.0/11', '10.0.0.0/7',
                   '127.0.0.1/7', 'fc00::/6', '::1/127', '::1/0',
                   '2001:db8::/32', 'fe80::/10', '169.254.0.0/16',
                   '192.168.1.0/255.0.255.0', '192.168.1.1/33', 'fd00::1/129',
                   '192.168.1.1/24/1', '192.168.1.1 /24', '192.168.1.1/ 24',
                   '192.168.1.1/24\n', ' 192.168.1.1/24', 'fd00::1%eth0/64',
                   'localhost/24', '*', '192.168.*', '', None, [], {}, True)
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(normalize_rule(value))

    def test_lists_reject_duplicates_after_normalization_and_bounded_inputs(self):
        self.assertEqual(normalize_rules([]), [])
        self.assertEqual(normalize_rules(['192.168.1.8/24', 'fd00::99/64']),
                         ['192.168.1.0/24', 'fd00::/64'])
        invalid = (None, {}, '192.168.1.1', ['8.8.8.8'],
                   ['192.168.1.1', '::ffff:192.168.1.1'],
                   ['192.168.1.1', '192.168.1.1/32'],
                   ['192.168.1.2/24', '192.168.1.3/255.255.255.0'],
                   ['fd00::1', 'fd00::1/128'], ['fd00::1/64', 'fd00::99/64'],
                   ['127.0.0.%d' % number for number in range(1, 34)])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_rules(value)
        with self.assertRaises(ValueError):
            normalize_rules([], required=True)
        with self.assertRaises(ValueError):
            normalize_rules(['127.0.0.1'], required='true')
        self.assertEqual(normalize_rules(['127.0.0.1'], required=True), ['127.0.0.1'])
        self.assertEqual(len(normalize_rules(['127.0.0.%d' % number for number in range(1, 33)])), 32)

    def test_actual_peer_matching_handles_families_mapped_and_subnet_edges(self):
        rules = ['192.168.31.10/24', 'fdab:1234::/48', '127.0.0.1', '::1']
        for peer in ('192.168.31.0', '192.168.31.255', '192.168.31.193',
                     '::ffff:192.168.31.193', 'fdab:1234:0:ffff::1', '127.0.0.1', '::1'):
            with self.subTest(peer=peer):
                self.assertTrue(peer_matches(peer, rules))
        for peer in ('192.168.30.255', '192.168.32.0', 'fdab:1234:1::1',
                     '127.0.0.2', '8.8.8.8', '192.168.31.0/24', 'localhost', None):
            with self.subTest(peer=peer):
                self.assertFalse(peer_matches(peer, rules))
        self.assertTrue(peer_matches('192.168.31.193', ['::ffff:192.168.31.0/120']))
        self.assertTrue(peer_matches('::ffff:192.168.31.193', ['192.168.31.0/24']))
        self.assertFalse(peer_matches('fd00::1', ['192.168.31.0/24']))

    def test_empty_invalid_or_forwarded_rule_never_matches(self):
        for rules in ([], None, {}, ['*'], ['0.0.0.0/0'], ['localhost'],
                      ['192.168.31.193, 8.8.8.8'], ['192.168.1.1', '192.168.1.1/32']):
            with self.subTest(rules=rules):
                self.assertFalse(peer_matches('192.168.31.193', rules))


if __name__ == '__main__':
    unittest.main()
