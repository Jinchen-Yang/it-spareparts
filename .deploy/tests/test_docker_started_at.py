"""Docker timestamps on Python 3.10; no production, network or Docker calls."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load(name):
    path = Path(__file__).resolve().parents[1] / (name + '.py')
    spec = importlib.util.spec_from_file_location(name + '_timestamp', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


itsp, shop = load('presales3_ops'), load('shop_pn_ops')
RAW = '2026-10-28T07:24:13.137207105Z'


class DockerStartedAtTests(unittest.TestCase):
    def test_actual_nine_digit_docker_timestamp_is_microsecond_utc(self):
        expected = dt.datetime(2026, 10, 28, 7, 24, 13, 137207, tzinfo=dt.timezone.utc)
        for module in (itsp, shop):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.docker_started_at(RAW), expected)
                self.assertIs(module.docker_started_at(RAW).tzinfo, dt.timezone.utc)

    def test_fractional_precision_and_offsets_are_explicit(self):
        for module in (itsp, shop):
            for suffix, microseconds in (('Z', 0), ('.1Z', 100000), ('.123Z', 123000),
                                        ('.123456Z', 123456), ('.123456789Z', 123456)):
                with self.subTest(module=module.__name__, suffix=suffix):
                    parsed = module.docker_started_at('2026-10-28T07:24:13' + suffix)
                    self.assertEqual(parsed.microsecond, microseconds)
            self.assertEqual(module.docker_started_at('2026-10-28T15:24:13.137207105+08:00'), module.docker_started_at(RAW))
            self.assertEqual(module.docker_started_at('2026-10-28T07:24:13.137207105+00:00'), module.docker_started_at(RAW))

    def test_invalid_strings_fail_closed(self):
        invalid = (None, True, 1, '', '2026-10-28', '2026-10-28T07:24:13',
                   '2026-10-28 07:24:13Z', '2026-10-28T07:24:13.1234567890Z',
                   '2026-10-28T07:24:13.Z', '2026-10-28T07:24:13Z\n',
                   '2026-02-30T07:24:13Z', '2026-10-28T24:24:13Z',
                   '2026-10-28T07:60:13Z', '2026-10-28T07:24:60Z',
                   '2026-10-28T07:24:13+24:00', '2026-10-28T07:24:13+00:60')
        for module in (itsp, shop):
            for value in invalid:
                with self.subTest(module=module.__name__, value=value), self.assertRaisesRegex(RuntimeError, 'invalid Docker StartedAt'):
                    module.docker_started_at(value)

    def test_comparison_truncates_conservatively_not_rounding_up(self):
        created = dt.datetime(2026, 10, 28, 7, 24, 13, 137207, tzinfo=dt.timezone.utc)
        for module in (itsp, shop):
            self.assertFalse(created < module.docker_started_at(RAW))
            self.assertTrue(created - dt.timedelta(microseconds=1) < module.docker_started_at(RAW))

    def test_identity_fingerprints_preserve_raw_nine_digits(self):
        row = {'Id': 'id', 'Image': 'image', 'RestartCount': 0,
               'State': {'Running': True, 'StartedAt': RAW, 'OOMKilled': False}}
        self.assertEqual(itsp.fingerprint(row)['started_at'], RAW)
        self.assertEqual(shop.fingerprint(row)['started'], RAW)
        changed = {**row, 'State': {**row['State'], 'StartedAt': RAW.replace('105Z', '106Z')}}
        self.assertEqual(itsp.docker_started_at(RAW), itsp.docker_started_at(changed['State']['StartedAt']))
        self.assertNotEqual(itsp.fingerprint(row), itsp.fingerprint(changed))
        self.assertNotEqual(shop.fingerprint(row), shop.fingerprint(changed))

    def test_shop_manifest_validates_without_rewriting_raw_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / ('pn-' + 'a' * 40)
            root.mkdir(mode=0o700)
            manifest = {'release': 'a' * 40, 'old_revision': '4651d1bf7e032ed0b7d0f298532b271578485149',
                'old_head': shop.OLD_HEAD, 'target_head': shop.NEW_HEAD,
                'old': {name: {'id': str(index + 1) * 64, 'image': 'sha256:' + 'b' * 64} for index, name in enumerate(shop.MANAGED)},
                'app_image': 'sha256:' + 'c' * 64, 'frontend_image': 'sha256:' + 'd' * 64,
                'source_sha256': 'e' * 64, 'controller_sha256': 'f' * 64, 'catalog_token_sha256': '1' * 64,
                'source_hold': {'release': '2' * 40, 'controller_sha256': '3' * 64, 'gate_sha256': '4' * 64,
                    'writers': [{'name': 'it-spareparts-app-synthetic' + str(index), 'id': str(index + 1) * 64,
                                 'image': 'sha256:' + '5' * 64, 'started_at': RAW} for index in range(4)]}}
            raw = (json.dumps(manifest) + '\n').encode()
            path = root / 'manifest.json'
            path.write_bytes(raw)
            original_stat = Path.stat
            def metadata(path, *args, **kwargs):
                actual = original_stat(path, *args, **kwargs)
                return SimpleNamespace(st_uid=0, st_mode=actual.st_mode) if path == root else actual
            with patch.object(shop, 'BASE', base), patch.object(Path, 'stat', metadata):
                release = shop.Release(path)
            self.assertEqual(release.raw, raw)
            self.assertEqual(release.m['source_hold']['writers'], manifest['source_hold']['writers'])
            self.assertEqual(path.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
