"""Regression coverage for Gakumas state and actions across the web UI.

The harness calls the real WebWindow methods without creating Chromium, running
background workers, or reading the user's installation and account files.
"""
import inspect
import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dmmclient.app import VersionWorker, Worker
from dmmclient.backend.helper import DmmHelper
from dmmclient.backend.session import DmmError, data_dir
from dmmclient.webapp import WebWindow


class WebHarness:
    """Bind production methods to local state, keeping Qt and network work out."""

    def __init__(self):
        self.settings = {'proxy': 'http://127.0.0.1:10808'}
        self.versions = {}
        self.theme = 'light'
        self.worker = None
        self.detail_product = 'gakumas'
        self.detail_data = {}
        self.detail_shots = {}
        self.detail_loading = None
        self.events = []
        self.dispatch = Mock()
        self.remember_launch = Mock()
        self.kick_version_check = Mock()
        self.kick_detail = Mock()
        self.ensure_install_location = Mock(
            side_effect=AssertionError('Gakumas must not enter the generic installer'))

    def __getattr__(self, name):
        attribute = inspect.getattr_static(WebWindow, name)
        return attribute.__get__(self, type(self)) if hasattr(attribute, '__get__') else attribute

    def send(self, **payload):
        self.events.append(payload)

    def account_data(self):
        return {'chip': '未登录', 'loggedIn': False}

    def node_data(self):
        return {'enabled': 0}

    def isVisible(self):
        return False


class GakumasWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {
            'LOCALAPPDATA': str(self.root / 'local'),
            'APPDATA': str(self.root / 'roaming'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        data_dir().mkdir(parents=True)
        self.official = self.root / 'roaming' / 'dmmgameplayer5'
        self.official.mkdir(parents=True)
        self.write_json(data_dir() / 'games.json', {})
        self.write_json(self.official / 'dmmgame.cnf', {'contents': []})
        self.window = WebHarness()

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def install(self, name='game', version='3.5.0'):
        root = self.root / name
        root.mkdir()
        (root / 'gakumas.exe').write_bytes(b'MZ-test-fixture')
        self.write_json(data_dir() / 'installation.json', {'root': str(root), 'version': version})
        return root.resolve()

    def card(self):
        return next(card for card in self.window.library_cards() if card['product'] == 'gakumas')

    def event(self, kind):
        events = [event for event in self.window.events if event['type'] == kind]
        self.assertTrue(events, f'No {kind} event; emitted: {self.window.events!r}')
        return events[-1]

    @staticmethod
    def actions(payload):
        return {button['a'] for button in payload['buttons']}

    def test_missing_or_invalid_installation_is_not_advertised_as_installed(self):
        empty = self.root / 'empty'
        empty.mkdir()
        for path in (None, self.root / 'removed', empty):
            with self.subTest(path=path):
                record = data_dir() / 'installation.json'
                if path is None:
                    record.unlink(missing_ok=True)
                else:
                    self.write_json(record, {'root': str(path), 'version': '3.5.0'})
                card = self.card()
                self.assertFalse(card['installed'])
                self.assertNotIn('已安装', {badge['t'] for badge in card['badges']})
                self.assertIn('game-folder', self.actions(card))
                self.assertTrue(self.actions(card).isdisjoint(
                    {'launch', 'check', 'update', 'game-launch', 'game-check', 'game-update'}))
                self.window.push_detail('gakumas', True)
                detail = self.event('detail')
                self.assertIn('game-folder', self.actions(detail))
                self.assertNotIn('game-update', self.actions(detail))
                self.assertNotIn('已安装', detail['stateLine'])
                self.window.push_snapshot()
                self.assertNotIn('gakumas', self.event('snapshot')['installedProducts'])
                self.assertIsNone(self.window.hero_data())

    def test_independent_installation_is_consistent_in_library_detail_and_snapshot(self):
        root = self.install()
        card = self.card()
        self.assertTrue(card['installed'])
        self.assertIn('已安装', {badge['t'] for badge in card['badges']})
        self.assertIn('3.5.0', card['state'])
        self.window.push_detail('gakumas', True)
        detail = self.event('detail')
        self.assertEqual(Path(detail['root']).resolve(), root)
        self.assertIn('已安装', detail['stateLine'])
        self.assertIn('3.5.0', detail['stateLine'])
        self.assertTrue({'launch', 'check', 'update'} <= self.actions(detail))
        self.assertTrue({'game-launch', 'game-check', 'game-update'}.isdisjoint(self.actions(detail)))
        self.window.push_snapshot()
        snapshot = self.event('snapshot')
        self.assertIn('gakumas', snapshot['installedProducts'])
        self.assertEqual(snapshot['hero']['product'], 'gakumas')
        self.assertIn('3.5.0', snapshot['hero']['meta'])
        self.assertIn('已安装', self.window.detail_row('gakumas')['meta'])

    def test_valid_official_directory_replaces_the_stale_path_in_the_detail(self):
        valid = self.root / 'official-game'
        valid.mkdir()
        (valid / 'gakumas.exe').write_bytes(b'MZ-test-fixture')
        stale = self.root / 'removed'
        self.write_json(data_dir() / 'installation.json', {'root': str(stale), 'version': 'old'})
        self.write_json(self.official / 'dmmgame.cnf', {'contents': [{
            'productId': 'gakumas', 'gameType': 'GCL',
            'detail': {'installed': True, 'path': str(valid), 'version': 'new'},
        }]})
        self.assertTrue(self.card()['installed'])
        self.window.push_detail('gakumas', True)
        detail = self.event('detail')
        self.assertEqual(Path(detail['root']).resolve(), valid.resolve())
        self.assertIn('new', detail['stateLine'])
        self.assertTrue({'launch', 'check', 'update'} <= self.actions(detail))

    def test_legacy_detail_actions_use_the_specialized_route(self):
        self.install()
        for action, expected in (('game-launch', 'launch'), ('game-check', 'check'),
                                 ('game-update', 'update')):
            with self.subTest(action=action):
                self.window.dispatch.reset_mock()
                self.window.handle_action({'action': action, 'product': 'gakumas'})
                self.window.dispatch.assert_called_once_with(expected, 'gakumas', '')
        self.window.ensure_install_location.assert_not_called()

    def test_missing_game_does_not_offer_the_generic_installer_for_a_legacy_action(self):
        try:
            self.window.handle_action({'action': 'game-update', 'product': 'gakumas'})
        except DmmError:
            pass  # A local missing-directory error is also a valid preflight result.
        self.window.ensure_install_location.assert_not_called()
        for call in self.window.dispatch.call_args_list:
            self.assertNotEqual(call.args[0], 'game-update')
        self.assertFalse((data_dir() / 'installation.json').exists())

    def test_interrupted_update_offers_recovery_without_claiming_the_game_is_installed(self):
        interrupted = self.root / 'interrupted'
        journal = interrupted / '.ok-gm-update' / 'journal.json'
        journal.parent.mkdir(parents=True)
        journal.write_text('{}', encoding='utf-8')
        self.write_json(data_dir() / 'installation.json', {'root': str(interrupted), 'version': 'old'})
        card = self.card()
        self.assertFalse(card['installed'])
        self.assertIn('update', self.actions(card))
        self.assertNotIn('launch', self.actions(card))
        self.assertNotIn('已安装', {badge['t'] for badge in card['badges']})
        self.window.push_detail('gakumas', True)
        detail = self.event('detail')
        self.assertIn('update', self.actions(detail))
        self.assertNotIn('game-update', self.actions(detail))
        self.assertNotIn('launch', self.actions(detail))

    def test_legacy_directory_action_binds_an_existing_directory_instead_of_moving_files(self):
        chosen = self.root / 'new-game'
        chosen.mkdir()
        (chosen / 'gakumas.exe').write_bytes(b'MZ-test-fixture')
        self.window.choose_game_directory = Mock(
            side_effect=AssertionError('Gakumas needs binding, not a generic move/install target'))
        with patch('dmmclient.webapp.QFileDialog.getExistingDirectory', return_value=str(chosen)):
            self.window.handle_action({'action': 'game-dir', 'product': 'gakumas'})
        self.assertEqual(DmmHelper.installed_game()[0], chosen.resolve())
        self.window.choose_game_directory.assert_not_called()
        self.window.dispatch.assert_not_called()

    def test_selecting_a_directory_refreshes_the_open_detail(self):
        old = self.root / 'removed'
        self.write_json(data_dir() / 'installation.json', {'root': str(old), 'version': 'old'})
        chosen = self.root / 'new-game'
        chosen.mkdir()
        (chosen / 'gakumas.exe').write_bytes(b'MZ-test-fixture')
        with patch('dmmclient.webapp.QFileDialog.getExistingDirectory', return_value=str(chosen)):
            self.window.handle_action({'action': 'game-folder', 'product': 'gakumas'})
        self.assertEqual(DmmHelper.installed_game()[0], chosen.resolve())
        self.assertIn('gakumas', self.event('snapshot')['installedProducts'])
        self.assertEqual(Path(self.event('detail')['root']).resolve(), chosen.resolve())

    def test_selecting_an_empty_directory_preserves_the_existing_binding(self):
        installed = self.install()
        before = (data_dir() / 'installation.json').read_bytes()
        empty = self.root / 'empty'
        empty.mkdir()
        with patch('dmmclient.webapp.QFileDialog.getExistingDirectory', return_value=str(empty)), \
                self.assertRaises(DmmError):
            self.window.handle_action({'action': 'game-folder', 'product': 'gakumas'})
        self.assertEqual((data_dir() / 'installation.json').read_bytes(), before)
        self.assertEqual(DmmHelper.installed_game()[0], installed)

    def test_version_result_from_the_previous_directory_does_not_replace_the_new_binding(self):
        previous = self.install('previous-game', 'previous')
        current = self.install('current-game', 'current')
        record = data_dir() / 'installation.json'
        before = record.read_bytes()
        current_info = {'version': 'current', 'latest': 'current', 'pending': 0, 'root': str(current)}
        self.window.versions['gakumas'] = dict(current_info)
        self.window.version_arrived('gakumas', {
            'version': 'obsolete', 'latest': 'obsolete-update', 'pending': 7, 'root': str(previous),
        })
        self.assertEqual(self.window.versions['gakumas'], current_info)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(DmmHelper.installed_game()[0], current)
        self.assertNotIn('obsolete', self.card()['state'])
        self.window.push_detail('gakumas', True)
        self.assertEqual(Path(self.event('detail')['root']).resolve(), current)

        fresh_info = dict(current_info, latest='current-update', pending=2)
        self.window.version_arrived('gakumas', fresh_info)
        self.assertEqual(self.window.versions['gakumas'], fresh_info)
        self.assertIn('current-update', self.card()['state'])

    def test_background_verification_cannot_overwrite_a_directory_bound_during_the_request(self):
        previous = self.install('previous-game', 'previous')
        current = self.root / 'current-game'
        current.mkdir()
        (current / 'gakumas.exe').write_bytes(b'MZ-test-fixture')
        current = current.resolve()
        record = data_dir() / 'installation.json'

        def finish_after_rebinding(session, product, root):
            self.assertEqual(product, 'gakumas')
            self.assertEqual(Path(root).resolve(), previous)
            self.write_json(record, {'root': str(current), 'version': 'current'})
            return {'version': 'obsolete', 'latest': 'obsolete', 'pending': 0}

        worker = VersionWorker({'proxy': 'http://127.0.0.1:10808'}, [('gakumas', str(previous))])
        emitted = []
        worker.verified.connect(lambda product, info: emitted.append((product, info)))
        with patch('dmmclient.app.operation_proxy', return_value=nullcontext('http://127.0.0.1:10808')), \
                patch('dmmclient.app.DmmSession', return_value=Mock()), \
                patch('dmmclient.app.verify_version', side_effect=finish_after_rebinding) as verify, \
                patch('dmmclient.app.record_version') as write_version:
            worker.run()
        verify.assert_called_once()
        write_version.assert_not_called()
        self.assertEqual(emitted, [])
        self.assertEqual(json.loads(record.read_text(encoding='utf-8')),
                         {'root': str(current), 'version': 'current'})

        # A result for the current root must still be delivered. This also
        # ensures a missing signal connection or swallowed setup error cannot
        # make the rejected-result assertions above pass accidentally.
        worker.targets = [('gakumas', str(current))]
        with patch('dmmclient.app.operation_proxy', return_value=nullcontext('http://127.0.0.1:10808')), \
                patch('dmmclient.app.DmmSession', return_value=Mock()), \
                patch('dmmclient.app.verify_version', return_value={
                    'version': 'current-verified', 'latest': 'current-verified', 'pending': 0,
                }), patch('dmmclient.app.record_version') as write_current:
            worker.run()
        write_current.assert_called_once_with('gakumas', 'current-verified', str(current))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0], 'gakumas')
        self.assertEqual(emitted[0][1]['root'], str(current))
        self.assertEqual(emitted[0][1]['version'], 'current-verified')


class GakumasWorkerTests(unittest.TestCase):
    def test_legacy_worker_actions_do_not_invoke_the_generic_service(self):
        for action, method in (('game-check', 'check'), ('game-update', 'prepare'),
                               ('game-launch', 'launch')):
            with self.subTest(action=action):
                helper = Mock(spec=DmmHelper)
                helper.check.return_value = {
                    'latest_version': '3.5.0', 'pending_files': 0, 'download_bytes': 0,
                }
                helper.prepare.return_value = SimpleNamespace(version='3.5.0')
                worker = Worker(action, {'proxy': 'http://127.0.0.1:10808'}, product='gakumas')
                with patch('dmmclient.app.GameService',
                           side_effect=AssertionError('Gakumas must use its existing helper')):
                    worker.game_action(helper)
                getattr(helper, method).assert_called_once_with(*(() if method == 'check' else (True,)))


if __name__ == '__main__':
    unittest.main()
