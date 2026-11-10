"""公開闸門的合成探針；不讀真實設定、密鑰或裝置，不修改 Git。"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import check_public as scanner


def ipv4(*parts):
    return '.'.join(map(str, parts))


class PublicScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='public-scan-synthetic-')
        self.home = Path(self.tmp.name)
        self.root = self.home / 'repo'
        self.root.mkdir()
        self.private_file = self.home / 'deny.json'

    def tearDown(self):
        self.tmp.cleanup()

    def deny(self, value):
        self.private_file.write_text(json.dumps(value), encoding='utf-8')
        return self.private_file

    def findings(self, text, terms=(), name='probe.txt'):
        return scanner.problems(name, text.encode(), scanner.private_patterns(terms))

    def run_main(self, argv):
        output = io.StringIO()
        with patch.object(scanner, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.home), \
                patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            code = scanner.main(argv)
        return code, output.getvalue()

    def test_fourteen_original_leak_classes(self):
        person = '測試' + '研究者'
        host = 'SYNTHETIC' + '-HOST'
        probes = [
            ('tailnet address', ipv4(100, 99, 12, 34) + ':18092', 'individual Tailscale address', []),
            ('tailnet host', 'sample.' + 'ts.' + 'net', 'private tailnet hostname', []),
            ('Mac home', '/' + 'Users/' + 'SyntheticOwner' + '/file', 'personal macOS home', []),
            ('Windows home', 'C:' + '\\Users\\' + 'SyntheticOwner' + '\\file', 'personal Windows home', []),
            ('GitHub key', 'gh' + 'p_' + 'A' * 36, 'GitHub token', []),
            ('email', 'reader' + '@' + 'private-mail.invalid', 'personal email address', []),
            ('gist', 'gist_' + 'id: ' + 'ab' * 16, 'private gist identifier', []),
            ('provider key', 's' + 'k-' + 'A' * 20, 'provider token', []),
            ('private class C', ipv4(192, 168, 44, 19), 'private RFC1918 address', []),
            ('private class A', ipv4(10, 44, 12, 19), 'private RFC1918 address', []),
            ('private class B', ipv4(172, 21, 44, 19), 'private RFC1918 address', []),
            ('MAC', ':'.join(['02', 'a1', 'b2', 'c3', 'd4', 'e5']), 'MAC address', []),
            ('private name', '姓名：' + person + '的資料', 'private denylist match', [person]),
            ('private hostname', host, 'private denylist match', [host]),
        ]
        self.assertEqual(len(probes), 14)
        for label, text, reason, terms in probes:
            with self.subTest(category=label):
                self.assertIn(reason, self.findings(text, terms))

    def test_rfc1918_network_description_requires_canonical_explicit_prefix(self):
        for address, prefix in [(ipv4(10, 0, 0, 0), 8), (ipv4(172, 16, 0, 0), 12),
                                (ipv4(192, 168, 44, 0), 24)]:
            with self.subTest(prefix=prefix):
                self.assertNotIn('private RFC1918 address', self.findings(address + '/' + str(prefix)))
                self.assertIn('private RFC1918 address', self.findings(address))
        for suffix in ['', '/24', '/32', '/99']:
            self.assertIn('private RFC1918 address', self.findings(ipv4(10, 44, 12, 19) + suffix))
        self.assertIn('private RFC1918 address', self.findings(ipv4(10, 0, 0, 0) + '/8junk'))
        self.assertIn('individual Tailscale address', self.findings(ipv4(100, 64, 0, 0) + '/32'))
        self.assertNotIn('individual Tailscale address', self.findings(ipv4(100, 64, 0, 0)))
        self.assertNotIn('individual Tailscale address', self.findings(ipv4(100, 64, 0, 0) + '/10'))
        for address in [ipv4(192, 0, 2, 17), ipv4(198, 51, 100, 8),
                        ipv4(203, 0, 113, 9), ipv4(127, 0, 0, 1), ipv4(172, 32, 1, 2)]:
            self.assertEqual(self.findings(address), [])

    def test_reserved_email_and_github_noreply_are_generic_exceptions(self):
        for domain in ['example.' + 'org', 'sub.example.' + 'com', 'example.' + 'test',
                       'users.noreply.' + 'github.com', 'noreply.' + 'github.com']:
            self.assertEqual(self.findings('reader' + '@' + domain), [])
        self.assertIn('personal email address', self.findings('reader' + '@' + 'notexample.org'))
        self.assertIn('personal email address', self.findings('reader' + '@' + 'users.noreply.github.com.invalid'))

    def test_private_denylist_overrides_reserved_email_exception(self):
        address = 'someone' + '@' + 'example.' + 'org'
        self.assertEqual(self.findings(address), [])
        self.assertEqual(self.findings(address, [address]), ['private denylist match'])

    def test_provider_slack_aws_and_tailnet_key_shapes(self):
        values = [('s' + 'k-' + 'a' * 20, 'provider token'),
                  ('xox' + 'b-' + 'a' * 24, 'Slack token'),
                  ('xox' + 'p-' + '1' * 24, 'Slack token'),
                  ('AK' + 'IA' + 'A1' * 8, 'AWS access key'),
                  ('ts' + 'key-auth-' + 'a' * 20, 'Tailscale token'),
                  ('-----BEGIN ' + 'PRIVATE KEY-----', 'private key')]
        for value, reason in values:
            with self.subTest(reason=reason):
                self.assertIn(reason, self.findings(value))
        self.assertEqual(self.findings('sk-' + '<TOKEN>'), [])

    def test_mac_forms_and_non_mac_hex(self):
        pairs = ['02', 'a1', 'b2', 'c3', 'd4', 'e5']
        for value in [':'.join(pairs), '-'.join(pairs), '.'.join(['02a1', 'b2c3', 'd4e5'])]:
            self.assertIn('MAC address', self.findings(value))
        self.assertNotIn('MAC address', self.findings('ab' * 32))

    def test_gist_context_accepts_both_id_lengths_but_not_unrelated_sha(self):
        for size in [16, 20]:
            self.assertIn('private gist identifier', self.findings('gist\nid = ' + 'ab' * size))
            self.assertNotIn('private gist identifier', self.findings('commit SHA: ' + 'ab' * size))
        self.assertNotIn('private gist identifier', self.findings('gist source SHA256: ' + 'ab' * 32))

    def test_private_words_use_case_insensitive_boundaries_not_substrings(self):
        owner = 'lz' + 'hs'
        public_owner = owner + '1995'
        self.assertEqual(self.findings(public_owner, [owner]), [])
        self.assertEqual(self.findings('x' + owner, [owner]), [])
        self.assertIn('private denylist match', self.findings(owner.upper() + '-PC', [owner]))
        self.assertIn('private denylist match', self.findings('name=' + owner, [owner]))
        self.assertEqual(self.findings('literal axb', ['a.b']), [])
        self.assertIn('private denylist match', self.findings('literal a.b', ['a.b']))

    def test_denylist_precedence_list_object_and_default_path(self):
        self.deny(['private' + '-one'])
        second = self.home / 'second.json'
        second.write_text(json.dumps({'terms': ['private' + '-two']}))
        patterns = scanner.load_private_denylist(self.private_file,
                    environ={'XDEV_PRIVATE_DENYLIST': str(second)}, home=self.home)
        self.assertTrue(patterns[0].search('private' + '-one'))
        patterns = scanner.load_private_denylist(environ={'XDEV_PRIVATE_DENYLIST': str(second)}, home=self.home)
        self.assertTrue(patterns[0].search('private' + '-two'))
        default = self.home / '.config/cross-device-remote/public-denylist.json'
        default.parent.mkdir(parents=True)
        default.write_text(json.dumps(['private' + '-default']))
        patterns = scanner.load_private_denylist(environ={}, home=self.home)
        self.assertTrue(patterns[0].search('private' + '-default'))

    def test_required_missing_empty_and_invalid_lists_fail_closed(self):
        self.assertEqual(scanner.load_private_denylist(environ={}, home=self.home), [])
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            scanner.load_private_denylist(required=True, environ={}, home=self.home)
        for value in [[], {}, [''], ['   '], [1], 'private' + '-value']:
            with self.subTest(value_type=type(value).__name__):
                self.deny(value)
                with self.assertRaises(ValueError):
                    scanner.load_private_denylist(self.private_file, True, environ={}, home=self.home)
        code, output = self.run_main(['--require-private-denylist', '--denylist', str(self.home / 'missing')])
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.home), output)

    def test_output_does_not_echo_private_matches_or_filenames(self):
        private = 'Synthetic' + '-Private-Owner'
        (self.root / (private + '.md')).write_text(private)
        self.deny([private])
        code, output = self.run_main(['--require-private-denylist', '--denylist', str(self.private_file)])
        self.assertEqual(code, 1)
        self.assertIn('private denylist match', output)
        self.assertNotIn(private, output)
        self.assertNotIn(str(self.home), output)

    def test_history_still_scans_reachable_blobs_and_applies_private_list(self):
        private = 'Old' + '-Private-Host'
        self.deny([private])
        tree, blob = 'a' * 40, 'b' * 40

        def git(command, **kwargs):
            self.assertEqual(kwargs['cwd'], self.root)
            if command[1:4] == ['rev-list', '--objects', '--all']:
                return tree + '\n' + blob + ' ' + private + '.md\n'
            if command[1:3] == ['cat-file', '-t']:
                return 'tree\n' if command[3] == tree else 'blob\n'
            if command[1:3] == ['cat-file', 'blob']:
                return private.encode()
            self.fail('Unexpected history command')

        with patch.object(scanner.subprocess, 'check_output', side_effect=git) as calls:
            code, output = self.run_main(['--history', '--denylist', str(self.private_file)])
        self.assertEqual(code, 1)
        self.assertEqual(calls.call_count, 4)
        self.assertIn('history: private denylist match', output)
        self.assertIn('files_and_blobs=1', output)
        self.assertNotIn(private, output)

    def test_generic_ci_mode_needs_no_private_file(self):
        (self.root / 'README.md').write_text('Public example only.')
        code, output = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn('files_and_blobs=1 findings=0', output)

    def test_probe_source_does_not_itself_contain_leaking_fixtures(self):
        self.assertEqual(scanner.problems('test_public.py', Path(__file__).read_bytes()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
