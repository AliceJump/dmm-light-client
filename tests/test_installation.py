import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dmmclient.backend.games import (GameService, game_root, installed_record,
                                     installed_records, move_game, uninstall_game,
                                     verify_version)
from dmmclient.backend.helper import DmmHelper
from dmmclient.backend.installation import gakumas_installation
from dmmclient.backend.session import DmmError, data_dir, dmm_dir


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        environment = patch.dict(os.environ, {
            'LOCALAPPDATA': str(self.base / 'Local'),
            'APPDATA': str(self.base / 'Roaming'),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def game(self, name='gakumas'):
        root = self.base / name
        root.mkdir()
        (root / 'gakumas.exe').write_bytes(b'MZ')
        return root.resolve()

    def own(self, root, version='3.5.0'):
        self.write_json(data_dir() / 'installation.json', {'root': str(root), 'version': version})

    def official(self, root, version='3.6.0'):
        self.write_json(dmm_dir() / 'dmmgame.cnf', {'contents': [{
            'productId': 'gakumas', 'gameType': 'GCL',
            'detail': {'installed': True, 'path': str(root), 'version': version},
        }]})

    def session_for(self):
        session = Mock()
        session.device = {}
        session.root = data_dir()
        session.proxy = 'http://127.0.0.1:10808'
        manifest = {'domain': 'https://cdn-gameplayer.games.dmm.com', 'file_list': [{
            'local_path': 'gakumas.exe', 'path': '/gakumas.exe', 'size': 2,
            'hash': hashlib.md5(b'MZ').hexdigest(), 'check_hash_flg': True,
        }]}

        def request(path, *args, **kwargs):
            if path == '/v5/r2/launch/cl':
                return {'product_id': 'gakumas', 'has_installer': True,
                        'exec_file_name': 'gakumas.exe',
                        'execute_args': '/viewer_id=test-viewer /open_id=test-open /pf_access_token=test-token',
                        'latest_version': '3.6.0', 'file_list_url': '/manifest',
                        'sign': 'CloudFront-Policy=x;CloudFront-Signature=y;CloudFront-Key-Pair-Id=z'}
            if path == '/manifest':
                return manifest
            if path == '/manifest/totalpages':
                return {'total_pages': 1}
            self.fail(f'专用流程不应请求 {path}')

        session.request.side_effect = request
        return session

    def test_stale_local_record_falls_back_without_rewriting_records(self):
        self.own(self.base / 'removed')
        actual = self.game('moved')
        self.official(actual)
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        state = gakumas_installation()
        self.assertTrue(state.installed)
        self.assertEqual((state.root, state.version, state.source), (actual, '3.6.0', 'official'))
        self.assertEqual(DmmHelper.installed_game(), (actual, '3.6.0'))
        self.assertEqual(installed_records()['gakumas']['root'], str(actual))
        self.assertEqual(game_root('gakumas'), actual)
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_valid_local_record_takes_precedence(self):
        selected, legacy, official = self.game('selected'), self.game('legacy'), self.game('official')
        self.own(selected, '4')
        self.write_json(data_dir() / 'games.json', {'gakumas': {'root': str(legacy), 'version': '2'}})
        self.official(official, '3')
        self.assertEqual(DmmHelper.installed_game(), (selected, '4'))
        self.assertEqual(installed_record('gakumas')['root'], str(selected))

    def test_corrupt_local_record_can_use_legacy_managed_installation(self):
        actual = self.game()
        self.own(self.base / 'removed')
        (data_dir() / 'installation.json').write_text('{broken', encoding='utf-8')
        self.write_json(data_dir() / 'games.json', {'gakumas': {'root': str(actual), 'version': '3'}})
        state = gakumas_installation()
        self.assertTrue(state.installed)
        self.assertEqual(state.source, 'managed')
        self.assertEqual(state.root, actual)

    def test_empty_directory_is_not_installed_and_no_network_is_requested(self):
        empty = self.base / 'empty'
        empty.mkdir()
        self.own(empty)
        state = gakumas_installation()
        self.assertEqual(state.status, 'invalid')
        self.assertFalse(state.installed)
        self.assertFalse(state.recoverable)
        self.assertIsNone(state.as_record())
        self.assertIsNone(installed_record('gakumas'))
        self.assertNotIn('gakumas', installed_records())
        session = Mock()
        for action in ('check', 'update', 'launch'):
            with self.subTest(action=action), self.assertRaisesRegex(DmmError, '没有 gakumas.exe'):
                GameService(session, 'gakumas').execute(action)
        with self.assertRaisesRegex(DmmError, '首次安装'):
            GameService(session, 'gakumas').execute('install', empty)
        session.request.assert_not_called()

    def test_missing_record_does_not_create_an_installation(self):
        state = gakumas_installation()
        self.assertEqual(state.status, 'missing')
        self.assertFalse(state.installed)
        self.assertIsNone(state.root)
        self.assertIn('首次安装', state.error)
        session = Mock()
        with self.assertRaisesRegex(DmmError, '首次安装'):
            GameService(session, 'gakumas').execute('install', self.base / 'new-game')
        self.assertEqual(list(self.base.iterdir()), [])
        session.request.assert_not_called()

    def test_removed_directory_retains_the_true_path_error(self):
        missing = self.base / 'removed'
        self.own(missing)
        state = gakumas_installation()
        self.assertFalse(state.installed)
        self.assertEqual(state.root, missing)
        self.assertIn(str(missing), state.error)
        with self.assertRaisesRegex(DmmError, '目录已失效'):
            DmmHelper.installed_game()

    def test_installation_is_rechecked_after_files_disappear(self):
        root = self.game()
        self.own(root)
        self.assertIn('gakumas', installed_records())
        (root / 'gakumas.exe').unlink()
        self.assertNotIn('gakumas', installed_records())
        self.assertIsNone(installed_record('gakumas'))

    def test_interrupted_update_remains_recoverable_without_counting_as_installed(self):
        root = self.game()
        self.own(root)
        (root / 'gakumas.exe').unlink()
        self.write_json(root / '.ok-gm-update' / 'journal.json', {'state': 'applying', 'items': []})
        state = gakumas_installation()
        self.assertFalse(state.installed)
        self.assertTrue(state.recoverable)
        self.assertIsNone(state.as_record())
        self.assertNotIn('gakumas', installed_records())
        self.assertEqual(DmmHelper.installed_game(), (root, '3.5.0'))

    def test_all_public_game_actions_use_the_special_launch_manifest(self):
        root = self.game()
        self.own(root)
        session = self.session_for()
        with patch('dmmclient.backend.helper.windows_args', side_effect=str.split), \
                patch('dmmclient.backend.helper.ensure_game_stopped'), \
                patch('dmmclient.backend.helper.launch_game', return_value=Mock(pid=42)) as launch:
            for action in ('check', 'update', 'install', 'launch'):
                with self.subTest(action=action):
                    session.request.reset_mock()
                    result = GameService(session, 'gakumas').execute(action)
                    self.assertTrue(result)
                    self.assertEqual(session.request.call_args_list[0].args[0], '/v5/r2/launch/cl')
                    self.assertNotIn('/v5/r2/install/cl', [call.args[0] for call in session.request.call_args_list])
            launch.assert_called_once()
            session.request.reset_mock()
            verified = verify_version(session, 'gakumas', root)
            self.assertEqual(verified, {'latest': '3.6.0', 'pending': 0, 'version': '3.6.0'})
            self.assertEqual(session.request.call_args_list[0].args[0], '/v5/r2/launch/cl')

    def test_interrupted_executable_replacement_is_really_recovered(self):
        root = self.game()
        self.own(root)
        backup = root / '.ok-gm-update' / 'backup' / 'gakumas.exe'
        backup.parent.mkdir(parents=True)
        (root / 'gakumas.exe').replace(backup)
        journal = root / '.ok-gm-update' / 'journal.json'
        self.write_json(journal, {'state': 'applying', 'items': [{'local': 'gakumas.exe', 'existed': True}]})
        session = self.session_for()
        messages = []
        with patch('dmmclient.backend.helper.windows_args', side_effect=str.split), \
                patch('dmmclient.backend.helper.ensure_game_stopped'), \
                patch('dmmclient.backend.updater.VerifiedUpdater.download',
                      side_effect=AssertionError('备份可恢复，不应重新下载')):
            GameService(session, 'gakumas', messages.append).execute('update')
        self.assertEqual((root / 'gakumas.exe').read_bytes(), b'MZ')
        self.assertFalse(backup.exists())
        self.assertTrue(gakumas_installation().installed)
        self.assertEqual(json.loads(journal.read_text(encoding='utf-8'))['state'], 'committed')
        self.assertIn('已恢复上次中断的文件替换', messages)
        self.assertNotIn('/v5/r2/install/cl', [call.args[0] for call in session.request.call_args_list])

    def test_background_version_check_does_not_recover_or_write_installation(self):
        root = self.game()
        self.own(root, 'old')
        path = data_dir() / 'installation.json'
        before = path.read_bytes()
        session = Mock()
        spec = Mock(version='new')
        spec.plan.root = root
        spec.plan.pending.return_value = []
        with patch.object(DmmHelper, 'fetch', autospec=True, return_value=spec) as fetch, \
                patch.object(DmmHelper, 'check', side_effect=AssertionError('后台不可恢复更新')), \
                patch.object(DmmHelper, 'prepare', side_effect=AssertionError('后台不可安装更新')), \
                patch.object(DmmHelper, '_remember_version', side_effect=AssertionError('后台不可写安装记录')):
            result = verify_version(session, 'gakumas', root)
        self.assertIs(fetch.call_args.args[0].session, session)
        self.assertEqual(result, {'latest': 'new', 'pending': 0, 'version': 'new'})
        self.assertEqual(path.read_bytes(), before)

    def test_background_verification_rejects_a_rebound_directory(self):
        captured = self.game('old')
        selected = self.game('new')
        self.own(selected)
        path = data_dir() / 'installation.json'
        before = path.read_bytes()
        session = self.session_for()
        with patch('dmmclient.backend.helper.windows_args', side_effect=str.split), \
                self.assertRaisesRegex(DmmError, '游戏目录已变更'):
            verify_version(session, 'gakumas', captured)
        self.assertEqual(path.read_bytes(), before)

    def test_explicit_install_target_cannot_silently_update_another_directory(self):
        current = self.game('current')
        other = self.game('other')
        self.own(current)
        before = (data_dir() / 'installation.json').read_bytes()
        session = Mock()
        service = GameService(session, 'gakumas')
        with patch.object(DmmHelper, 'prepare', autospec=True, return_value=Mock(version='3.6.0')) as prepare:
            with self.assertRaisesRegex(DmmError, '请先选择游戏目录重新绑定'):
                service.execute('install', other)
            prepare.assert_not_called()
            service.execute('install', current)
            prepare.assert_called_once()
        session.request.assert_not_called()
        self.assertEqual((data_dir() / 'installation.json').read_bytes(), before)
        self.assertEqual((current / 'gakumas.exe').read_bytes(), b'MZ')
        self.assertEqual((other / 'gakumas.exe').read_bytes(), b'MZ')

    def test_missing_source_move_and_purge_leave_records_unchanged(self):
        record_path = data_dir() / 'games.json'
        self.write_json(record_path, {'game1': {'root': str(self.base / 'removed'), 'version': '1'}})
        before = record_path.read_bytes()
        with patch('dmmclient.backend.games.game_running', return_value=False):
            with self.assertRaisesRegex(DmmError, '原游戏目录已失效'):
                move_game('game1', self.base / 'destination')
            with self.assertRaisesRegex(DmmError, '原游戏目录已不存在'):
                uninstall_game('game1')
            self.assertEqual(record_path.read_bytes(), before)
            self.assertFalse((self.base / 'destination').exists())
            uninstall_game('game1', keep_files=True)
        self.assertEqual(json.loads(record_path.read_text(encoding='utf-8')), {})


if __name__ == '__main__':
    unittest.main()
