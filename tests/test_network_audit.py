"""Network/account regressions with temporary data and mocked external boundaries."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from dmmclient.backend import accounts, helper, launcher, session, xray
from dmmclient.backend.session import DmmApiError, DmmError, DmmSession, OperationCancelled


def response(status=200, value=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(value if value is not None else {}).encode()
    result._content_consumed = True
    return result


class ProxyRetryTests(unittest.TestCase):
    def pool(self):
        store = Mock()
        store.ordered.return_value = [{'id': 'one', 'name': 'one'}, {'id': 'two', 'name': 'two'}]
        pool = xray.NodePool(store, Path('xray.exe'), 19080, Mock(), Mock())
        pool.index = 0
        return pool

    def test_unreachable_or_rejected_nodes_stop_after_a_bounded_request(self):
        for failure in (requests.ConnectionError('private-endpoint-token'), 403, 502):
            with self.subTest(failure=type(failure).__name__ if isinstance(failure, Exception) else failure):
                pool = self.pool()
                adapter = xray.PoolAdapter(pool)
                request = requests.Request('GET', 'https://example.dmm.com/').prepare()
                replies = []

                def send(*args, **kwargs):
                    if isinstance(failure, Exception):
                        raise failure
                    item = response(failure)
                    item.close = Mock(wraps=item.close)
                    replies.append(item)
                    return item

                with patch('requests.adapters.HTTPAdapter.send', side_effect=send) as transport, \
                        patch.object(pool, 'failover') as failover:
                    with self.assertRaises(DmmError) as caught:
                        adapter.send(request)
                self.assertEqual(transport.call_count, 2)
                self.assertEqual(failover.call_count, 1)
                self.assertNotIn('private-endpoint-token', str(caught.exception))
                for item in replies:
                    item.close.assert_called_once()
                # Failure is per request: the next user operation can retry.
                with patch('requests.adapters.HTTPAdapter.send', return_value=response()) as transport:
                    self.assertEqual(adapter.send(request).status_code, 200)
                transport.assert_called_once()

    def test_shared_hub_does_not_replace_foreground_cancellation(self):
        pool = self.pool()
        hub = xray.ProxyHub()
        settings = {xray.NODE_ENABLED_KEY: True, 'proxy': 'http://127.0.0.1:10808'}
        cancelled = threading.Event()

        def foreground_cancel():
            if cancelled.is_set():
                raise OperationCancelled('已暂停')

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hub._pool, hub._address = pool, 'http://127.0.0.1:19080'
            hub._fingerprint = (str(root.resolve()), hub._fingerprint_of(settings))
            with patch.object(hub, '_alive', return_value=True), \
                    hub.acquire(settings, cancel_check=foreground_cancel, root=root) as foreground:
                with hub.acquire(settings, root=root) as background:
                    with requests.Session() as first, requests.Session() as second:
                        xray.attach_pool(first, foreground)
                        xray.attach_pool(second, background)
                        cancelled.set()
                        with patch('requests.adapters.HTTPAdapter.send', return_value=response()) as transport:
                            with self.assertRaises(OperationCancelled):
                                first.get('https://example.dmm.com/')
                            transport.assert_not_called()
                            self.assertEqual(second.get('https://example.dmm.com/').status_code, 200)
                            transport.assert_called_once()

    def test_retired_pool_cannot_restart_an_unmanaged_process(self):
        pool = self.pool()
        context = Mock()
        context.__exit__ = Mock()
        pool.context = context
        pool.shutdown()
        context.__exit__.assert_called_once_with(None, None, None)
        with patch.object(xray, 'node_process') as start:
            with self.assertRaisesRegex(DmmError, '配置已改变'):
                pool.failover()
        start.assert_not_called()
        self.assertIsNone(pool.context)

    def test_cancellation_does_not_stop_another_operations_current_node(self):
        pool = self.pool()
        context = Mock()
        context.__exit__ = Mock()
        pool.context = context
        with self.assertRaises(OperationCancelled):
            pool.failover(cancel_check=Mock(side_effect=OperationCancelled('已暂停')))
        context.__exit__.assert_not_called()
        self.assertIs(pool.context, context)

    def test_body_transfer_failures_are_bounded_and_sanitized(self):
        instance = DmmSession.__new__(DmmSession)
        instance.auth, instance.cancel_check, instance.save = {}, Mock(), Mock()
        instance.http = Mock(proxy_attempts=2)
        instance.http.request.side_effect = requests.exceptions.ChunkedEncodingError('private-token')
        with self.assertRaises(DmmError) as caught:
            instance.request('/manifest', method='GET')
        self.assertEqual(instance.http.request.call_count, 2)
        self.assertEqual(instance.http.proxy_failover.call_count, 1)
        instance.save.assert_not_called()
        self.assertNotIn('private-token', str(caught.exception))

    def test_streaming_limit_and_cancellation_close_the_response(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                seen = []

                def chunks(_size):
                    for index in range(4):
                        seen.append(index)
                        yield b'abcd'

                item = Mock()
                item.iter_content.side_effect = chunks
                check = Mock(side_effect=OperationCancelled('已暂停')) if cancel else Mock()
                with self.assertRaises(OperationCancelled if cancel else DmmError):
                    xray._bounded_content(item, 6, check)
                self.assertEqual(seen, [0] if cancel else [0, 1])
                item.close.assert_called_once()

    def test_node_import_retires_old_pool_and_stale_success_cannot_restore_deleted_id(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(helper.msvcrt, 'locking'), \
                patch.object(xray.win32crypt, 'CryptProtectData', side_effect=lambda data, *_: b'fixture:' + data), \
                patch.object(xray.win32crypt, 'CryptUnprotectData', side_effect=lambda data, *_: ('', data[8:])):
            store = xray.NodeStore(Path(folder))
            hub = xray.ProxyHub()
            with patch.object(xray, 'HUB', hub):
                store.save(xray.parse_node('vless://11111111-1111-1111-1111-111111111111@example.com:443#one'))
                first = store.read()['nodes'][0]['id']
                pool = xray.NodePool(store, Path('xray.exe'), 19080, Mock(), Mock())
                pool.context = Mock()
                pool.context.__exit__ = Mock()
                hub._pool, hub._address = pool, 'http://127.0.0.1:19080'
                store.save(xray.parse_node('vless://22222222-2222-2222-2222-222222222222@example.com:443#two'))
                self.assertTrue(pool.retired)
                self.assertIsNone(hub._pool)
                saved = store.read()
                self.assertEqual(len(saved['nodes']), 2)
                second = next(node['id'] for node in saved['nodes'] if node['id'] != first)
                store.change(first, 'remove')
                before = store.path.read_bytes()
                store.pin(first)
                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(store.read()['next'], second)

    def test_preferred_node_metadata_contention_does_not_fail_a_successful_request(self):
        pool = self.pool()
        pool.store.pin.side_effect = DmmError('metadata is temporarily locked')
        request = requests.Request('GET', 'https://example.dmm.com/').prepare()
        with patch('requests.adapters.HTTPAdapter.send', return_value=response()):
            self.assertEqual(xray.PoolAdapter(pool).send(request).status_code, 200)
        self.assertFalse(pool.recorded)


class SessionBoundaryTests(unittest.TestCase):
    def test_invalid_urls_and_oauth_redirects_fail_without_echoing_secrets(self):
        for value in (None, [], 'https://example.dmm.com:private-secret/path',
                      'https://user:private-secret@example.dmm.com/path',
                      'https://example.dmm.com/\nprivate-secret'):
            with self.subTest(kind=type(value).__name__), self.assertRaises(DmmError) as caught:
                session.dmm_url(value)
            self.assertNotIn('private-secret', str(caught.exception))
        base = 'https://webdgp-gameplayer.games.dmm.com/login/success'
        self.assertEqual(DmmSession._code(base + '?code=test-code'), 'test-code')
        for url in (base.replace('https:', 'http:') + '?code=test-code',
                    base + '?code=first&code=second', base + '?code=line%0Abreak',
                    base + '?code=test-code#fragment',
                    base.replace('.com/', '.com:8443/') + '?code=test-code'):
            self.assertIsNone(DmmSession._code(url))

    def test_unexpected_json_shape_is_a_sanitized_api_error(self):
        instance = DmmSession.__new__(DmmSession)
        instance.auth, instance.cancel_check, instance.save = {}, Mock(), Mock()
        instance.http = Mock(proxy_attempts=1)
        instance.http.request.return_value = response(value=['private-response'])
        with self.assertRaisesRegex(DmmError, '无法识别') as caught:
            instance.request('/test')
        self.assertNotIn('private-response', str(caught.exception))

    def test_session_uses_the_credential_path_from_its_original_index_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / 'first.dpapi', root / 'second.dpapi'
            first.write_bytes(b'first-snapshot')
            second.write_bytes(b'second-snapshot')
            store = Mock()
            store.active.return_value = {'key': 'a' * 32}
            store.path.return_value = first
            store.current_path.return_value = second

            def unprotect(blob, *args):
                value = {'auth': {'accessToken': blob.decode()}, 'device': {'user_os': 'win'}}
                return '', json.dumps(value).encode()

            with patch.object(accounts, 'AccountStore', return_value=store), \
                    patch.object(session.win32crypt, 'CryptUnprotectData', side_effect=unprotect):
                instance = DmmSession(root=root)
            self.assertEqual(instance.profile_key, 'a' * 32)
            self.assertEqual(instance.auth['accessToken'], 'first-snapshot')
            store.current_path.assert_not_called()
            instance.http.close()

    def test_failed_login_candidate_construction_cleans_its_staging_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'session.dpapi').write_bytes(b'encrypted-fixture')
            original = SimpleNamespace(root=root, profile_key=None, proxy='http://127.0.0.1:10808')
            wrapper = SimpleNamespace(session=original)
            with patch.object(launcher, 'DmmSession', side_effect=DmmError('invalid snapshot')):
                with self.assertRaises(DmmError):
                    with launcher.login_candidate(wrapper):
                        self.fail('Construction failed before yielding')
            self.assertIs(wrapper.session, original)
            self.assertEqual(list((root / 'pending').iterdir()), [])
            self.assertEqual((root / 'session.dpapi').read_bytes(), b'encrypted-fixture')

    def test_account_switch_cannot_split_a_refresh_check_and_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = accounts.AccountStore(root)
            first, second = 'a' * 32, 'b' * 32
            index = {'active': first, 'accounts': [
                {'key': first, 'number': '1', 'name': 'first'},
                {'key': second, 'number': '2', 'name': 'second'},
            ]}
            store.index.write_text(json.dumps(index), encoding='utf-8')
            entered, release, switch_started, switch_done = [threading.Event() for _ in range(4)]
            errors = []
            real_write = accounts.atomic_write

            def write(path, blob):
                if path == store.path(first):
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError('Test did not release refresh')
                return real_write(path, blob)

            def refresh():
                try:
                    store.persist(first, b'refreshed-first')
                except Exception as error:
                    errors.append(error)

            def switch():
                switch_started.set()
                try:
                    store.switch('2')
                except Exception as error:
                    errors.append(error)
                finally:
                    switch_done.set()

            with patch.object(helper, 'helper_lock', side_effect=lambda *_: nullcontext()), \
                    patch.object(accounts, 'atomic_write', side_effect=write), \
                    patch.object(store, 'checked_blob', return_value=b'second-snapshot'):
                refreshing = threading.Thread(target=refresh)
                switching = threading.Thread(target=switch)
                refreshing.start()
                self.assertTrue(entered.wait(1))
                switching.start()
                self.assertTrue(switch_started.wait(1))
                finished_while_refresh_locked = switch_done.wait(0.1)
                release.set()
                refreshing.join(2)
                switching.join(2)
            self.assertFalse(refreshing.is_alive() or switching.is_alive())
            self.assertFalse(finished_while_refresh_locked)
            self.assertEqual(errors, [])
            self.assertEqual(store.active()['key'], second)
            self.assertEqual((root / 'session.dpapi').read_bytes(), b'second-snapshot')

    def test_nested_helper_lock_keeps_the_outer_lock_and_cleans_up_after_errors(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(helper.msvcrt, 'locking') as lock:
            root = Path(folder)
            with helper.helper_lock(root):
                with self.assertRaisesRegex(RuntimeError, 'fixture'):
                    with helper.helper_lock(root / '.'):
                        raise RuntimeError('fixture')
                with helper.helper_lock(root):
                    pass
                self.assertEqual([call.args[1] for call in lock.call_args_list], [helper.msvcrt.LK_NBLCK])
            self.assertEqual([call.args[1] for call in lock.call_args_list],
                             [helper.msvcrt.LK_NBLCK, helper.msvcrt.LK_UNLCK])
            self.assertEqual(helper._LOCK_STATE.held, {})

    @unittest.skipUnless(os.name == 'nt', 'Uses actual Windows cross-process file locking')
    def test_other_process_cannot_persist_while_an_account_operation_holds_the_lock(self):
        script = (
            'import sys\nfrom pathlib import Path\n'
            'from dmmclient.backend.accounts import AccountStore\n'
            'from dmmclient.backend.session import DmmError\n'
            'try:\n AccountStore(Path(sys.argv[1])).persist(None, b"replacement")\n'
            'except DmmError:\n sys.exit(0)\n'
            'sys.exit(3)\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mirror = root / 'session.dpapi'
            mirror.write_bytes(b'original-fixture')
            with helper.helper_lock(root):
                result = subprocess.run([sys.executable, '-c', script, str(root)],
                                        cwd=Path(__file__).resolve().parents[1],
                                        capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(mirror.read_bytes(), b'original-fixture')


class HelperManifestTests(unittest.TestCase):
    arguments = ['/viewer_id=1', '/open_id=fixture', '/pf_access_token=fixture']

    def manifest(self):
        return {'domain': 'https://dl.dmm.com/', 'file_list': [
            {'local_path': 'gakumas.exe', 'path': '/gakumas.exe', 'size': 0,
             'hash': 'd41d8cd98f00b204e9800998ecf8427e', 'check_hash_flg': True},
        ]}

    def client(self, root, replies):
        (root / 'gakumas.exe').write_bytes(b'')
        instance = Mock(root=root / 'state', device={})
        instance.request.side_effect = [
            {'exec_file_name': 'gakumas.exe', 'execute_args': 'fixture',
             'latest_version': 'new', 'sign': 'fixture', 'file_list_url': 'https://dl.dmm.com/manifest'},
            *replies,
        ]
        wrapper = helper.DmmHelper.from_session(instance)
        # No recovery journal in this fixture; retain the real plan and version persistence.
        wrapper.updater = Mock(return_value=SimpleNamespace(journal=root / 'missing-journal'))
        return wrapper

    def test_totalpages_requires_exact_404_before_recording_a_single_page_as_complete(self):
        for error, accepted in ((DmmApiError(4040), False), (DmmError('HTTP 4040'), False),
                                (DmmApiError(404), True), (DmmError('HTTP 404'), True)):
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                wrapper = self.client(root, [self.manifest(), error])
                record = root / 'state' / 'installation.json'
                record.parent.mkdir()
                original = b'{"version":"old"}'
                record.write_bytes(original)
                with patch.object(wrapper, 'installed_game', return_value=(root, 'old')), \
                        patch.object(helper, 'windows_args', return_value=self.arguments):
                    if accepted:
                        self.assertEqual(wrapper.check()['pending_files'], 0)
                        self.assertEqual(json.loads(record.read_bytes())['version'], 'new')
                    else:
                        with self.assertRaises(DmmError) as caught:
                            wrapper.check()
                        self.assertIs(caught.exception, error)
                        self.assertEqual(record.read_bytes(), original)
                        wrapper.updater.assert_not_called()

    def test_invalid_manifest_pagination_and_chunk_shapes_are_rejected(self):
        cases = [
            [None],
            [{'domain': 'https://dl.dmm.com/', 'file_list': {}}],
            [self.manifest(), {'total_pages': True}],
            [self.manifest(), []],
            [self.manifest(), {'total_pages': 2}, {'domain': 'https://dl.dmm.com/', 'file_list': {}}],
        ]
        for index, replies in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                wrapper = self.client(root, replies)
                with patch.object(wrapper, 'installed_game', return_value=(root, 'old')), \
                        patch.object(helper, 'windows_args', return_value=self.arguments), \
                        self.assertRaisesRegex(DmmError, '格式不受支持'):
                    wrapper.fetch()
                self.assertFalse((root / 'state' / 'installation.json').exists())


class InstallerExecutionTests(unittest.TestCase):
    def test_gakumas_update_gate_does_not_block_an_unrelated_running_game(self):
        other = Mock(info={'name': 'other-game.exe', 'exe': 'D:/games/other/game.exe'})
        with patch.object(helper.psutil, 'process_iter', return_value=[other]), \
                patch('dmmclient.backend.games.records', return_value={'other': {'root': 'D:/games/other'}}):
            helper.ensure_game_stopped()
        gakumas = Mock(info={'name': 'GAKUMAS.EXE'})
        with patch.object(helper.psutil, 'process_iter', return_value=[gakumas]):
            with self.assertRaisesRegex(DmmError, '游戏正在运行'):
                helper.ensure_game_stopped()

    def test_normal_installer_preserves_failure_code_and_waits_before_cancellation(self):
        exe = Path('installer.exe')
        with patch.object(helper.subprocess, 'run', return_value=Mock(returncode=1603)) as run, \
                patch.object(helper, '_launch_elevated') as elevated:
            self.assertEqual(helper.run_installer(exe, ['/quiet'], exe.parent), 1603)
        run.assert_called_once()
        elevated.assert_not_called()
        cancel = Mock(side_effect=[None, OperationCancelled('已暂停')])
        with patch.object(helper.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            with self.assertRaises(OperationCancelled):
                helper.run_installer(exe, [], exe.parent, cancel_check=cancel)
        run.assert_called_once()

    def test_only_error_740_permits_an_elevated_installer_retry(self):
        exe = Path('installer.exe')
        for code in (2, 5, 740):
            with self.subTest(code=code):
                error = OSError('private-command-line')
                error.winerror = code
                with patch.object(helper.subprocess, 'run', side_effect=error), \
                        patch.object(helper, '_launch_elevated', return_value=3010) as elevated:
                    if code == 740:
                        self.assertEqual(helper.run_installer(exe, [], exe.parent), 3010)
                        elevated.assert_called_once_with(exe, [], exe.parent, wait=True)
                    else:
                        with self.assertRaises(DmmError) as caught:
                            helper.run_installer(exe, [], exe.parent)
                        self.assertNotIn('private-command-line', str(caught.exception))
                        elevated.assert_not_called()

    def shell_api(self, stack, *, accepted=True, error=0, wait_result=0, exit_ok=True, exit_code=3010):
        shell, kernel = Mock(), Mock()

        def execute(pointer):
            if accepted:
                pointer._obj.hProcess = 123
            return accepted

        def code(_handle, pointer):
            pointer._obj.value = exit_code
            return exit_ok

        shell.ShellExecuteExW.side_effect = execute
        kernel.WaitForSingleObject.return_value = wait_result
        kernel.GetExitCodeProcess.side_effect = code
        stack.enter_context(patch.object(helper.ctypes, 'WinDLL', create=True,
                                         side_effect=lambda name, **_: shell if name == 'shell32' else kernel))
        stack.enter_context(patch.object(helper.ctypes, 'get_last_error', return_value=error, create=True))
        return kernel

    def test_elevated_installer_waits_reads_exit_code_and_always_closes_handle(self):
        exe = Path('installer.exe')
        for wait_result, exit_ok in ((0, True), (0xFFFFFFFF, True), (0, False)):
            with self.subTest(wait_result=wait_result, exit_ok=exit_ok), ExitStack() as stack:
                kernel = self.shell_api(stack, wait_result=wait_result, exit_ok=exit_ok)
                if wait_result == 0 and exit_ok:
                    self.assertEqual(helper._launch_elevated(exe, [], exe.parent, wait=True), 3010)
                else:
                    with self.assertRaises(DmmError):
                        helper._launch_elevated(exe, [], exe.parent, wait=True)
                kernel.WaitForSingleObject.assert_called_once_with(123, 0xFFFFFFFF)
                kernel.CloseHandle.assert_called_once_with(123)

    def test_cancelled_installer_uac_is_never_a_success(self):
        with ExitStack() as stack:
            kernel = self.shell_api(stack, accepted=False, error=1223)
            with self.assertRaisesRegex(DmmError, '已取消管理员授权'):
                helper._launch_elevated(Path('installer.exe'), [], Path('.'), wait=True)
            kernel.WaitForSingleObject.assert_not_called()
            kernel.GetExitCodeProcess.assert_not_called()
            kernel.CloseHandle.assert_not_called()


if __name__ == '__main__':
    unittest.main()
