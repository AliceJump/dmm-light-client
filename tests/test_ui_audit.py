"""Behavior regressions found during the post-fix UI and CLI audit.

All paths and settings are temporary. Python UI methods use lightweight hosts;
the real JavaScript runs in Qt's JS engine without Chromium or any network.
"""
import inspect
import json
import os
import sys
import tempfile
import unittest
from collections import deque
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt, QUrl
from PySide6.QtQml import QJSEngine
from PySide6.QtWidgets import QApplication

from dmmclient import app, cli, webapp
from dmmclient.backend import settings
from dmmclient.backend.session import DmmError, data_dir
from dmmclient.backend.xray import NODE_ENABLED_KEY


class WindowHarness:
    def __init__(self, owner):
        self.owner = owner
        self.settings = {'proxy': settings.DEFAULT_PROXY, NODE_ENABLED_KEY: False}
        self.closing = False
        self.worker = None
        self.cover_worker = self.version_worker = self.account_worker = None
        self.detail_worker = self.shot_worker = None
        self.pending_actions = deque()
        self.detail_pending = self.detail_loading = None
        self.detail_product = 'game_a'
        self.detail_data = {}
        self.detail_shots = {}
        self.versions = {}
        self.status = Mock()
        self.proxy = Mock()
        self.proxy.text.return_value = 'socks5h://localhost:1080'
        self.mode = Mock()
        self.mode.currentIndex.return_value = 1
        for name in ('report', 'push_snapshot', 'push_detail', 'snap_fill', 'cancel',
                     'repopulate_library', 'repopulate_store', 'update_hero'):
            setattr(self, name, Mock())

    def __getattr__(self, name):
        attribute = inspect.getattr_static(self.owner, name)
        return attribute.__get__(self, type(self)) if hasattr(attribute, '__get__') else attribute


class LocalDataTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {
            'LOCALAPPDATA': str(self.root / 'local'),
            'APPDATA': str(self.root / 'roaming'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        data_dir().mkdir(parents=True, exist_ok=True)
        self.write_json(data_dir() / 'games.json', {})
        official = self.root / 'roaming' / 'dmmgameplayer5'
        official.mkdir(parents=True)
        self.write_json(official / 'dmmgame.cnf', {'contents': []})

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')


class SettingsAuditTests(LocalDataTest):
    def test_native_settings_save_preserves_existing_game_preferences(self):
        original = {'install_base': str(self.root / 'library'),
                    'install_bases': {'game_a': str(self.root / 'separate')},
                    'last_launched': 'game_a', 'ask_install_dir': False}
        settings.save_settings(original)
        window = WindowHarness(app.Window)
        window.run_action('settings')
        saved = settings.load_settings()
        for key, value in original.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(saved['proxy'], 'socks5h://localhost:1080')
        self.assertFalse(saved[NODE_ENABLED_KEY])
        self.assertIn('已保存', window.status.setText.call_args.args[0])

    def test_web_saves_merge_game_paths_written_after_the_window_loaded(self):
        window = WindowHarness(webapp.WebWindow)
        chosen = settings.remember_install_base('game_a', self.root / 'selected')
        window.remember_launch('game_a')
        self.assertEqual(settings.load_settings()['install_bases'], {'game_a': str(chosen)})
        window.handle_action({'action': 'settings', 'proxy': settings.DEFAULT_PROXY,
                              'mode': True, 'ask': False, 'install_base': ''})
        saved = settings.load_settings()
        self.assertEqual(saved['install_bases'], {'game_a': str(chosen)})
        self.assertEqual(saved['last_launched'], 'game_a')
        self.assertFalse(saved['ask_install_dir'])
        self.assertNotIn('install_base', saved)

    def test_global_install_base_is_used_unless_the_game_has_its_own(self):
        default = self.root / 'default-library'
        chosen = self.root / 'chosen-library'
        settings.save_settings({'install_base': str(default)})
        self.assertEqual(settings.game_install_root('game_a'), default / 'game_a')
        settings.remember_install_base('game_a', chosen)
        self.assertEqual(settings.game_install_root('game_a'), chosen / 'game_a')
        self.assertEqual(settings.game_install_root('game_b'), default / 'game_b')

    def test_invalid_settings_are_reported_without_replacing_the_file(self):
        path = settings.settings_path()
        for contents in (b'[]', b'null', b'{broken json', b'\xff'):
            with self.subTest(contents=contents):
                path.write_bytes(contents)
                with self.assertRaises(DmmError):
                    settings.update_settings({'proxy': settings.DEFAULT_PROXY})
                self.assertEqual(path.read_bytes(), contents)

    def test_settings_accept_a_utf8_bom(self):
        settings.settings_path().write_text('{"last_launched":"game_a"}', encoding='utf-8-sig')
        self.assertEqual(settings.load_settings()['last_launched'], 'game_a')


class CliAuditTests(LocalDataTest):
    def test_mutating_commands_hold_the_shared_lock_during_dispatch(self):
        cases = [(['install', 'game_a'], 'cmd_install'),
                 (['update', 'game_a'], 'cmd_update'),
                 (['launch', 'game_a'], 'cmd_launch'),
                 (['move', 'game_a', '--dir', str(self.root)], 'cmd_move'),
                 (['uninstall', 'game_a', '--yes'], 'cmd_uninstall'),
                 (['accounts', '--switch', '1'], 'cmd_accounts'),
                 (['dir', 'game_a', '--set', str(self.root)], 'cmd_dir'),
                 (['proxy', settings.DEFAULT_PROXY], 'cmd_proxy'),
                 (['refresh'], 'cmd_refresh'), (['check', 'game_a'], 'cmd_check')]
        for argv, command in cases:
            with self.subTest(argv=argv):
                held = []

                @contextmanager
                def lock(root):
                    self.assertEqual(root, data_dir())
                    held.append(True)
                    try:
                        yield
                    finally:
                        held.pop()

                def execute(args):
                    self.assertTrue(held, 'Dispatch must remain inside the shared operation lock')
                    return 0

                with patch.object(cli, 'helper_lock', side_effect=lock) as shared_lock, \
                        patch.object(cli, command, side_effect=execute) as dispatch:
                    self.assertEqual(cli.main(argv), 0)
                shared_lock.assert_called_once()
                dispatch.assert_called_once()
                self.assertFalse(held)

    def test_read_only_commands_remain_available_during_an_operation(self):
        cases = [(['list'], 'cmd_list'), (['catalog'], 'cmd_catalog'), (['nodes'], 'cmd_nodes'),
                 (['accounts'], 'cmd_accounts'), (['dir', 'game_a'], 'cmd_dir'),
                 (['proxy'], 'cmd_proxy')]
        for argv, command in cases:
            with self.subTest(argv=argv), \
                    patch.object(cli, 'helper_lock', side_effect=AssertionError('Read should not lock')), \
                    patch.object(cli, command, return_value=0) as dispatch:
                self.assertEqual(cli.main(argv), 0)
                dispatch.assert_called_once()

    def test_invalid_product_is_rejected_before_any_install_side_effect(self):
        with patch.object(cli, 'say'), patch.object(cli, 'cmd_install') as install, \
                patch.object(cli, 'helper_lock') as shared_lock:
            self.assertEqual(cli.main(['install', '../escape', '--dir', str(self.root)]), 1)
        install.assert_not_called()
        shared_lock.assert_not_called()
        self.assertFalse(settings.settings_path().exists())

    def test_empty_json_list_is_still_machine_readable_json(self):
        with patch.object(cli, 'say') as say:
            self.assertEqual(cli.main(['list', '--json']), 0)
        self.assertEqual(json.loads(say.call_args.args[0]), {})


class ThreadAuditTests(LocalDataTest):
    def test_foreground_queue_waits_for_finished_before_replacing_its_worker(self):
        for owner in (app.Window, webapp.WebWindow):
            with self.subTest(window=owner.__name__):
                window = WindowHarness(owner)
                old = Mock()
                old.isRunning.return_value = False
                window.worker = old
                window.start_operation = Mock()
                first = ('switch-account', {}, '2', '', '', '', '')
                second = ('game-launch', {}, '', '', '', 'game_a', '')
                window.enqueue_or_start(first)
                window.enqueue_or_start(second)
                window.start_operation.assert_not_called()
                self.assertIs(window.worker, old)
                self.assertEqual(list(window.pending_actions), [first, second])
                window.operation_finished()
                old.deleteLater.assert_called_once()
                window.start_operation.assert_called_once_with(first)
                self.assertEqual(list(window.pending_actions), [second])

    def test_running_background_threads_keep_both_windows_alive(self):
        stop = app.stop_background_workers
        for module, owner, field in ((app, app.Window, 'detail_worker'),
                                     (webapp, webapp.WebWindow, 'shot_worker')):
            with self.subTest(window=owner.__name__):
                window = WindowHarness(owner)
                worker = Mock()
                worker.isRunning.return_value = True
                setattr(window, field, worker)
                window.pending_actions.append(('launch',))
                window.detail_pending = 'game_b'
                event = Mock()
                with patch.object(module, 'stop_background_workers',
                                  side_effect=lambda workers: stop(workers, timeout=0)), \
                        patch.object(module.HUB, 'shutdown') as shutdown:
                    window.closeEvent(event)
                worker.requestInterruption.assert_called_once()
                event.ignore.assert_called_once()
                event.accept.assert_not_called()
                shutdown.assert_not_called()
                self.assertFalse(window.pending_actions)
                self.assertIsNone(window.detail_pending)

    def test_finished_background_workers_can_close_after_cooperative_cancellation(self):
        running = [True]
        worker = Mock()
        worker.isRunning.side_effect = lambda: running[0]
        worker.wait.side_effect = lambda _timeout: running.__setitem__(0, False)
        self.assertTrue(app.stop_background_workers([None, worker], timeout=1))
        worker.requestInterruption.assert_called_once()
        worker.wait.assert_called_once()

    def test_detail_queue_waits_for_finished_and_keeps_only_the_latest_request(self):
        for module, owner in ((app, app.Window), (webapp, webapp.WebWindow)):
            with self.subTest(window=owner.__name__):
                window = WindowHarness(owner)
                old = Mock()
                old.isRunning.return_value = False  # run() returned, finished is still queued.
                window.detail_worker = old
                new = Mock()
                with patch.object(module, 'DetailWorker', return_value=new) as factory, \
                        patch.object(module, 'catalog', return_value=[]):
                    window.detail_product = 'game_b'
                    window.kick_detail('game_b')
                    window.detail_product = 'game_c'
                    window.kick_detail('game_c')
                    window.detail_ready('game_a', {'detail': {'title': 'A'}, 'shots': []})
                    factory.assert_not_called()
                    self.assertIs(window.detail_worker, old)
                    window.push_detail.assert_not_called()
                    window.snap_fill.assert_not_called()
                    window.detail_finished()
                old.deleteLater.assert_called_once()
                self.assertIs(window.detail_worker, new)
                self.assertEqual(factory.call_args.args[1], 'game_c')
                new.start.assert_called_once()
                new.finished.connect.assert_called_once()
                self.assertIsNone(window.detail_pending)

    def test_switching_to_cached_detail_discards_an_older_pending_request(self):
        for module, owner in ((app, app.Window), (webapp, webapp.WebWindow)):
            with self.subTest(window=owner.__name__):
                window = WindowHarness(owner)
                window.detail_worker = Mock()
                window.detail_pending = 'game_b'
                window.detail_product = 'game_c'
                window.detail_data['game_c'] = {'title': 'Cached C'}
                with patch.object(module, 'DetailWorker') as factory:
                    window.kick_detail('game_c')
                    window.detail_finished()
                factory.assert_not_called()
                self.assertIsNone(window.detail_pending)
                self.assertEqual(window.detail_product, 'game_c')

    def test_account_autosave_keeps_identity_lookup_and_slot_write_under_one_lock(self):
        (data_dir() / 'session.dpapi').write_bytes(b'not-read-real-session')
        held = []

        @contextmanager
        def lock(_root):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def under_lock(value):
            def run(*_args, **_kwargs):
                self.assertTrue(held)
                return value
            return run

        store = Mock()
        store.read.side_effect = under_lock({'accounts': []})
        store.save_current.side_effect = under_lock(None)
        session = Mock()
        session.ensure_login.side_effect = under_lock(None)
        session.user_profile.side_effect = under_lock({'number': '42', 'name': 'Test'})
        worker = app.AccountWorker({})
        results = []
        worker.ready.connect(results.append)
        with patch.object(app, 'helper_lock', side_effect=lock), \
                patch.object(app, 'AccountStore', return_value=store), \
                patch.object(app, 'DmmSession', return_value=session), \
                patch.object(app, 'operation_proxy', return_value=nullcontext(settings.DEFAULT_PROXY)):
            worker.run()
        store.save_current.assert_called_once_with('42', 'Test', new=True)
        self.assertTrue(results[-1]['registered'])

    def test_version_result_cannot_change_a_moved_or_uninstalled_game(self):
        original = self.root / 'original'
        moved = self.root / 'moved'
        original.mkdir()
        moved.mkdir()
        records = data_dir() / 'games.json'
        for replacement in ({'game_a': {'root': str(moved), 'version': 'new-location'}}, {}):
            with self.subTest(replacement=replacement):
                self.write_json(records, {'game_a': {'root': str(original), 'version': 'before'}})

                def finish_after_change(*_args):
                    self.write_json(records, replacement)
                    return {'version': 'obsolete', 'latest': 'obsolete', 'pending': 0}

                worker = app.VersionWorker({}, [('game_a', str(original))])
                results = []
                worker.verified.connect(lambda product, info: results.append((product, info)))
                with patch.object(app, 'operation_proxy', return_value=nullcontext(settings.DEFAULT_PROXY)), \
                        patch.object(app, 'DmmSession', return_value=Mock()), \
                        patch.object(app, 'verify_version', side_effect=finish_after_change) as verify, \
                        patch.object(app, 'record_version') as write_version:
                    worker.run()
                verify.assert_called_once()
                write_version.assert_not_called()
                self.assertEqual(results, [])
                self.assertEqual(json.loads(records.read_text(encoding='utf-8')), replacement)
                for owner in (app.Window, webapp.WebWindow):
                    window = WindowHarness(owner)
                    window.version_arrived('game_a', {'root': str(original), 'version': 'obsolete'})
                    self.assertNotIn('game_a', window.versions)


class WebEntryAuditTests(LocalDataTest):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_store_operations_reacquire_the_current_proxy_after_a_node_edit(self):
        for module, owner in ((app, app.Window), (webapp, webapp.WebWindow)):
            with self.subTest(window=owner.__name__):
                window = WindowHarness(owner)
                retired, current = object(), object()
                with patch.object(module, 'operation_proxy',
                                  side_effect=[nullcontext(retired), nullcontext(current)]) as acquire:
                    self.assertIs(window.ensure_store_proxy(), retired)
                    self.assertIs(window.ensure_store_proxy(), current)
                self.assertEqual(acquire.call_count, 2)
                self.assertIs(window.store_proxy_url, current)

    def test_store_link_opens_using_a_valid_qt_delete_attribute_and_view(self):
        window = WindowHarness(webapp.WebWindow)
        window.ensure_store_proxy = Mock(return_value=settings.DEFAULT_PROXY)
        view = Mock()
        module = SimpleNamespace(QWebEngineView=Mock(return_value=view))
        with patch.dict(sys.modules, {'PySide6.QtWebEngineWidgets': module}), \
                patch.object(webapp, 'QDialog') as dialog, patch.object(webapp, 'QVBoxLayout'):
            window.open_store_page('https://games.dmm.co.jp/detail/game_a/')
        dialog.return_value.setAttribute.assert_called_once_with(Qt.WidgetAttribute.WA_DeleteOnClose)
        module.QWebEngineView.assert_called_once_with(dialog.return_value)
        self.assertEqual(view.load.call_args.args[0].toString(), 'https://games.dmm.co.jp/detail/game_a/')

    def test_store_links_reject_local_files_or_active_content_before_starting_a_proxy(self):
        for owner in (app.Window, webapp.WebWindow):
            for url in ('file:///secret.txt', 'javascript:alert(1)', 'data:text/html,test',
                        'https://name:password@example.com/'):
                with self.subTest(window=owner.__name__, url=url):
                    window = WindowHarness(owner)
                    window.ensure_store_proxy = Mock()
                    window.open_store_page(url)
                    window.ensure_store_proxy.assert_not_called()
                    window.report.assert_called_once()

    def test_directory_with_an_apostrophe_is_inserted_as_javascript_data(self):
        window = WindowHarness(webapp.WebWindow)
        window.view = Mock()
        chosen = self.root / "someone's 游戏库"
        chosen.mkdir()
        with patch.object(webapp.QFileDialog, 'getExistingDirectory', return_value=str(chosen)):
            window.handle_action({'action': 'browse-base'})
        script = window.view.page.return_value.runJavaScript.call_args.args[0]
        engine = QJSEngine()
        engine.evaluate('var input={}; var document={getElementById:function(){return input;}};')
        result = engine.evaluate(script)
        self.assertFalse(result.isError(), result.toString())
        self.assertEqual(engine.evaluate('input.value').toString(), str(chosen.resolve()))

    def test_invalid_bridge_products_are_rejected_before_opening_a_dialog(self):
        window = WindowHarness(webapp.WebWindow)
        window.open_detail = Mock()
        for payload in ({'action': 'open-detail', 'product': '../escape'}, [], {'action': 7}):
            with self.subTest(payload=payload), self.assertRaises(DmmError):
                window.handle_action(payload)
        window.open_detail.assert_not_called()


class StoreLoginAuditTests(LocalDataTest):
    def login_fixture(self):
        window = WindowHarness(app.Window)
        window.ensure_store_proxy = Mock(return_value=settings.DEFAULT_PROXY)
        window.refresh = Mock()
        view = SimpleNamespace(urlChanged=Mock(), destroyed=Mock(), load=Mock(),
                               _dmm_cancel_login=None,
                               _dmm_store_url='https://games.dmm.co.jp/detail/game_a/')
        original = SimpleNamespace(profile_key='selected-a', identity='dmm:account-a',
                                   verify_game_identity=Mock())
        candidate = Mock()
        candidate.request.return_value = {'url': 'https://www.dmm.com/my/-/login/'}
        context = Mock()
        context.__enter__ = Mock(return_value=candidate)
        context.__exit__ = Mock()
        return window, view, original, candidate, context

    def test_embedded_login_verifies_the_candidate_before_adopting_credentials(self):
        for fail_at in ('identity', 'account-switch', None):
            with self.subTest(fail_at=fail_at):
                window, view, original, candidate, context = self.login_fixture()
                events = []
                candidate._exchange.side_effect = lambda code: events.append(('exchange', code))

                def verify():
                    events.append(('verify', None))
                    if fail_at == 'identity': raise DmmError('账号身份不一致')

                def adopt(source, pending):
                    self.assertIs(source, original)
                    self.assertIs(pending, candidate)
                    events.append(('adopt', None))
                    if fail_at == 'account-switch': raise DmmError('所选账号已变更')

                candidate.verify_game_identity.side_effect = verify
                with patch.object(app, 'DmmSession', return_value=original) as session_type, \
                        patch.object(app, 'login_candidate', return_value=context) as stage, \
                        patch.object(app, '_adopt', side_effect=adopt) as save:
                    session_type._code.return_value = 'new-authorization-code'
                    window.start_store_login(view)
                    callback = view.urlChanged.connect.call_args.args[0]
                    callback(QUrl('https://webdgp-gameplayer.games.dmm.com/login/success?code=test'))
                self.assertIs(stage.call_args.args[0].session, original)
                self.assertFalse(stage.call_args.kwargs['fresh'])
                self.assertEqual(events[:2], [('exchange', 'new-authorization-code'), ('verify', None)])
                if fail_at == 'identity':
                    save.assert_not_called()
                else:
                    save.assert_called_once()
                if fail_at is None:
                    self.assertEqual(view.load.call_args.args[0].toString(), view._dmm_store_url)
                    window.refresh.assert_called_once()
                else:
                    window.refresh.assert_not_called()
                    self.assertEqual(view.load.call_count, 1)
                context.__exit__.assert_called_once()
                self.assertIsNone(view._dmm_cancel_login)

    def test_unverified_selected_account_cannot_be_bound_by_embedded_login(self):
        window, view, original, _candidate, context = self.login_fixture()
        original.identity = None
        original.verify_game_identity.side_effect = DmmError('当前账号无法验证')
        with patch.object(app, 'DmmSession', return_value=original), \
                patch.object(app, 'login_candidate', return_value=context) as stage:
            window.start_store_login(view)
        stage.assert_not_called()
        view.load.assert_not_called()
        self.assertIn('尚未确认账号身份', window.report.call_args.args[0])

    def test_closing_the_store_view_discards_pending_credentials_once(self):
        window, view, original, _candidate, context = self.login_fixture()
        with patch.object(app, 'DmmSession', return_value=original), \
                patch.object(app, 'login_candidate', return_value=context), \
                patch.object(app, '_adopt') as save:
            window.start_store_login(view)
            cancel = view.destroyed.connect.call_args.args[0]
            cancel()
            cancel()
        context.__exit__.assert_called_once()
        save.assert_not_called()
        view.urlChanged.disconnect.assert_called_once()
        self.assertIsNone(view._dmm_cancel_login)


class JavascriptAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])
        cls.source = (Path(webapp.__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')

    def setUp(self):
        self.engine = QJSEngine()
        self.prelude = '''
            'use strict';
            const elements = {};
            function makeElement() {
                return {dataset: {}, innerHTML: '', style: {},
                    classList: {contains: name => name === 'hidden', add() {}, remove() {}, toggle() {}},
                    addEventListener() {}, querySelectorAll: () => [],
                    querySelector: () => makeElement(), removeAttribute() {}};
            }
            const document = {documentElement: {dataset: {}},
                getElementById: id => elements[id] || (elements[id] = makeElement()),
                querySelectorAll: () => [], addEventListener() {}};
            const qt = {webChannelTransport: {}};
            function QWebChannel() {}
        '''

    def evaluate(self, source):
        # QJSEngine isolates top-level lexical declarations between evaluate()
        # calls. Run the real script and each complete scenario in one scope.
        result = self.engine.evaluate('(function(){' + self.prelude + self.source + '\n' + source + '})();')
        self.assertFalse(result.isError(), result.toString())
        return json.loads(result.toString())

    def test_game_library_initially_shows_library_only_games_and_install_filters(self):
        result = self.evaluate('''
            state = {library: [{product:'gakumas', title:'学园偶像大师', state:'目录未设置'}],
                     store: [{product:'game_a', title:'A'}], installedProducts:['game_a']};
            let rows = null;
            patchGamelist = (_container, value) => { rows = value; };
            renderGameLib();
            const all = rows;
            listFilter='installed'; renderGameLib();
            const installed = rows;
            listFilter='uninstalled'; renderGameLib();
            return JSON.stringify({all, installed, uninstalled: rows});
        ''')
        self.assertEqual([row['product'] for row in result['all']], ['gakumas', 'game_a'])
        self.assertEqual(result['all'][0]['meta'], '目录未设置')
        self.assertEqual([row['product'] for row in result['installed']], ['game_a'])
        self.assertEqual([row['product'] for row in result['uninstalled']], ['gakumas'])

    def test_background_details_update_the_active_product_without_changing_navigation(self):
        result = self.evaluate('''
            detail = {product:'game_b', title:'old'};
            currentPage = 'gamelib';
            let rendered = 0, navigated = 0;
            renderGameLib = () => { rendered++; };
            showPage = () => { navigated++; };
            applyDetail({product:'game_b', title:'updated', open:false});
            const updated = {title:detail.title, rendered};
            applyDetail({product:'game_a', title:'stale', open:false});
            const stale = {product:detail.product, rendered};
            currentPage='network'; applyDetail({product:'game_b',title:'cached',open:false});
            const away = {title:detail.title, rendered, navigated};
            applyDetail({product:'game_a', title:'selected', open:true});
            return JSON.stringify({updated, stale, away, selected:{product:detail.product, navigated}});
        ''')
        self.assertEqual(result['updated'], {'title': 'updated', 'rendered': 1})
        self.assertEqual(result['stale'], {'product': 'game_b', 'rendered': 1})
        self.assertEqual(result['away'], {'title': 'cached', 'rendered': 1, 'navigated': 0})
        self.assertEqual(result['selected'], {'product': 'game_a', 'navigated': 1})


if __name__ == '__main__':
    unittest.main()
