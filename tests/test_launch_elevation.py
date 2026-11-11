import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dmmclient.backend import helper
from dmmclient.backend.games import GameService
from dmmclient.backend.session import DmmError


class GameElevationTests(unittest.TestCase):
    def setUp(self):
        self.exe = Path('game folder') / 'game.exe'
        self.arguments = ['--name=two words', 'quote="value"', 'end with space\\', '']

    def test_ordinary_game_does_not_request_elevation(self):
        process = Mock(pid=42)
        with patch.object(helper.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(helper, '_launch_elevated') as elevate, \
                patch.object(helper.ctypes, 'WinDLL', create=True) as dll:
            self.assertIs(helper.launch_game(self.exe, self.arguments, self.exe.parent), process)
        spawn.assert_called_once_with([str(self.exe), *self.arguments], cwd=self.exe.parent,
                                      shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elevate.assert_not_called()
        dll.assert_not_called()

    def test_server_admin_flag_requests_uac_only_for_the_game(self):
        shell = Mock()
        shell.IsUserAnAdmin.return_value = False
        progress = Mock()
        with patch.object(helper.ctypes, 'WinDLL', return_value=shell, create=True), \
                patch.object(helper.subprocess, 'Popen') as spawn, \
                patch.object(helper, '_launch_elevated', return_value=helper.ElevatedGame(42)) as elevate:
            process = helper.launch_game(self.exe, self.arguments, self.exe.parent, True, progress)
        self.assertEqual(process.pid, 42)
        spawn.assert_not_called()
        elevate.assert_called_once_with(self.exe, self.arguments, self.exe.parent)
        self.assertIn('管理员权限', progress.call_args.args[0])

    def test_already_elevated_client_uses_normal_process_creation(self):
        shell = Mock()
        shell.IsUserAnAdmin.return_value = True
        with patch.object(helper.ctypes, 'WinDLL', return_value=shell, create=True), \
                patch.object(helper.subprocess, 'Popen', return_value=Mock(pid=42)) as spawn, \
                patch.object(helper, '_launch_elevated') as elevate:
            helper.launch_game(self.exe, self.arguments, self.exe.parent, True)
        spawn.assert_called_once()
        elevate.assert_not_called()

    def test_executable_manifest_740_also_requests_uac(self):
        error = OSError('sensitive launch arguments')
        error.winerror = 740
        with patch.object(helper.subprocess, 'Popen', side_effect=error) as spawn, \
                patch.object(helper, '_launch_elevated', return_value=helper.ElevatedGame(42)) as elevate:
            self.assertEqual(helper.launch_game(self.exe, self.arguments, self.exe.parent).pid, 42)
        spawn.assert_called_once()
        elevate.assert_called_once_with(self.exe, self.arguments, self.exe.parent)

    def test_unrelated_launch_error_does_not_retry_with_admin_rights(self):
        for code in (2, 5, 32):
            with self.subTest(winerror=code):
                error = OSError('sensitive launch arguments')
                error.winerror = code
                with patch.object(helper.subprocess, 'Popen', side_effect=error), \
                        patch.object(helper, '_launch_elevated') as elevate:
                    with self.assertRaises(DmmError) as caught:
                        helper.launch_game(self.exe, self.arguments, self.exe.parent)
                elevate.assert_not_called()
                self.assertNotIn('sensitive', str(caught.exception))

    def shell_api(self, stack, *, success=True, error=0, pid=42):
        shell, kernel = Mock(), Mock()
        seen = {}

        def execute(pointer):
            info = pointer._obj
            seen.update(verb=info.lpVerb, file=info.lpFile, parameters=info.lpParameters,
                        directory=info.lpDirectory, mask=info.fMask)
            if success:
                info.hProcess = 123
            return success

        shell.ShellExecuteExW.side_effect = execute
        kernel.GetProcessId.return_value = pid
        stack.enter_context(patch.object(helper.ctypes, 'WinDLL', create=True,
                                         side_effect=lambda name, **_: shell if name == 'shell32' else kernel))
        stack.enter_context(patch.object(helper.ctypes, 'get_last_error', return_value=error, create=True))
        return seen, kernel

    def test_uac_launch_preserves_executable_working_directory_and_closes_handle(self):
        with ExitStack() as stack:
            seen, kernel = self.shell_api(stack)
            result = helper._launch_elevated(self.exe, self.arguments, self.exe.parent)
        self.assertEqual(result.pid, 42)
        self.assertEqual(seen['verb'], 'runas')
        self.assertEqual(seen['file'], str(self.exe.resolve()))
        self.assertEqual(seen['directory'], str(self.exe.parent.resolve()))
        self.assertTrue(seen['mask'] & 0x40)  # Obtain the process handle.
        self.assertTrue(seen['mask'] & 0x100)  # Worker thread has no message loop.
        self.assertIn('"--name=two words"', seen['parameters'])
        kernel.GetProcessId.assert_called_once_with(123)
        kernel.CloseHandle.assert_called_once_with(123)

    @unittest.skipUnless(os.name == 'nt', 'Uses the actual Windows command-line parser')
    def test_uac_arguments_roundtrip_through_windows_parser(self):
        with ExitStack() as stack:
            seen, _ = self.shell_api(stack)
            helper._launch_elevated(self.exe, self.arguments, self.exe.parent)
        self.assertEqual(helper.windows_args(seen['parameters']), self.arguments)

    def test_cancelled_uac_is_not_reported_as_a_success(self):
        with ExitStack() as stack:
            _, kernel = self.shell_api(stack, success=False, error=1223)
            with self.assertRaisesRegex(DmmError, '已取消管理员授权'):
                helper._launch_elevated(self.exe, self.arguments, self.exe.parent)
        kernel.GetProcessId.assert_not_called()
        kernel.CloseHandle.assert_not_called()

    def test_other_shell_failure_reports_windows_code(self):
        with ExitStack() as stack:
            self.shell_api(stack, success=False, error=5)
            with self.assertRaisesRegex(DmmError, 'Windows 错误 5'):
                helper._launch_elevated(self.exe, self.arguments, self.exe.parent)

    def test_pid_lookup_failure_still_closes_process_handle(self):
        with ExitStack() as stack:
            _, kernel = self.shell_api(stack, pid=0)
            with self.assertRaises(DmmError):
                helper._launch_elevated(self.exe, self.arguments, self.exe.parent)
        kernel.CloseHandle.assert_called_once_with(123)


class GameElevationIntegrationTests(unittest.TestCase):
    def test_fetch_keeps_admin_requirement_without_blocking_check_or_update(self):
        instance = helper.DmmHelper.__new__(helper.DmmHelper)
        instance.progress = Mock()
        instance.session = Mock()
        instance.session.device = {}
        info = {'is_administrator': True, 'exec_file_name': 'gakumas.exe', 'execute_args': '',
                'latest_version': '1', 'sign': 'signature', 'file_list_url': '/manifest'}
        instance.session.request.side_effect = [info, {'domain': 'https://example.dmm.com', 'file_list': []},
                                                {'total_pages': 1}]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(helper.DmmHelper, 'installed_game', return_value=(Path(folder), '0')), \
                patch.object(helper, 'windows_args', return_value=[
                    '/viewer_id=viewer', '/open_id=open', '/pf_access_token=secret']), \
                patch.object(helper.UpdatePlan, 'parse', return_value=Mock()), \
                patch.object(helper.ctypes, 'WinDLL', create=True) as dll:
            spec = instance.fetch()
        self.assertTrue(spec.requires_admin)
        dll.assert_not_called()

    def test_gakumas_launch_uses_admin_requirement_after_prepare(self):
        instance = helper.DmmHelper.__new__(helper.DmmHelper)
        spec = SimpleNamespace(exe=Path('gakumas.exe'), arguments=['/token=secret'], requires_admin=True)
        instance.prepare = Mock(return_value=spec)
        instance.progress = Mock()
        with patch.object(helper, 'ensure_game_stopped'), \
                patch.object(helper, 'launch_game', return_value=helper.ElevatedGame(42)) as launch:
            self.assertEqual(instance.launch().pid, 42)
        instance.prepare.assert_called_once_with(True)
        launch.assert_called_once_with(spec.exe, spec.arguments, spec.exe.parent, True, instance.progress)

    def test_generic_launch_uses_fresh_admin_flag_and_keeps_install_root_as_cwd(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            exe = root / 'bin' / 'game.exe'
            exe.parent.mkdir()
            exe.write_bytes(b'fixture')
            saved = {'priconner': {'root': str(root), 'title': 'Game'}}
            with patch('dmmclient.backend.games.catalog', return_value=[]), \
                    patch('dmmclient.backend.games.records', return_value=saved):
                service = GameService(Mock(), 'priconner')
                old = {'latest_version': '1', 'exec_file_name': 'bin/game.exe', 'execute_args': '',
                       'is_administrator': False}
                service.request_info = Mock(side_effect=[old, dict(old, is_administrator=True)])
                plan = Mock()
                plan.pending.return_value = []
                service.plan = Mock(return_value=plan)
                service.updater = Mock(return_value=Mock())
                service.stopped = Mock()
                service.install_modules = Mock(return_value={})
                with patch('dmmclient.backend.games.atomic_write'), \
                        patch('dmmclient.backend.games.data_dir', return_value=root), \
                        patch('dmmclient.backend.games.windows_args', return_value=['--token=secret']), \
                        patch('dmmclient.backend.games.launch_game', return_value=helper.ElevatedGame(42)) as launch:
                    result = service.execute('launch')
        launch.assert_called_once_with(exe, ['--token=secret'], root, True, service.progress)
        self.assertIn('PID 42', result)


if __name__ == '__main__':
    unittest.main()
