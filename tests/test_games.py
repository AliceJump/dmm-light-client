import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dmmclient.backend.games import GameService
from dmmclient.backend.session import DmmError


class GameTests(unittest.TestCase):
    def test_does_not_accept_different_product(self):
        session=Mock(); session.device={}; session.request.side_effect=[{'game_type':'GCL'},{'product_id':'gakumas'}]
        with self.assertRaises(DmmError): GameService(session,'priconner').request_info(True)

    def test_installer_requires_support(self):
        session=Mock(); session.device={}; session.request.side_effect=[{'game_type':'GCL'},{'product_id':'priconner','has_installer':True}]
        with self.assertRaises(DmmError): GameService(session,'priconner').request_info(True)

    def test_independent_game_identity_payload(self):
        session=Mock(); session.device={'user_os':'win'}
        session.request.side_effect=[{'game_type':'GCL'},{'product_id':'priconner','file_list_url':'/manifest','sign':'signature'}]
        GameService(session,'priconner').request_info(False)
        path,payload=session.request.call_args.args
        self.assertEqual(path,'/v5/r2/launch/cl')
        self.assertEqual(payload['product_id'],'priconner')
        self.assertEqual(payload['launch_type'],'LIB')
        session.accept_identity.assert_not_called()

    def test_update_and_refetch_before_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'game.exe').write_bytes(b'fixture')
            session=Mock(); session.cancel_check=lambda:None
            service=GameService(session,'priconner')
            old={'product_id':'priconner','latest_version':'1','exec_file_name':'game.exe','execute_args':'old'}
            fresh=dict(old,execute_args='new')
            events=[]
            service.request_info=Mock(side_effect=lambda installing:(events.append('fetch') or (old if len(events)==1 else fresh)))
            plan=Mock(); plan.pending.return_value=[]
            service.plan=Mock(return_value=plan)
            updater=Mock(); updater.run.side_effect=lambda:events.append('update')
            service.updater=Mock(return_value=updater); service.stopped=Mock()
            with patch('dmmclient.backend.games.records',return_value={'priconner':{'root':str(root)}}), patch('dmmclient.backend.games.atomic_write'), patch('dmmclient.backend.games.subprocess.Popen',side_effect=lambda *a,**kw:(events.append('launch') or Mock(pid=1))) as spawn:
                service.execute('launch')
            self.assertEqual(events,['fetch','update','fetch','launch'])
            self.assertEqual(spawn.call_args.args[0][1:],["new"])


class ModuleTests(unittest.TestCase):
    """「额外运行模块」= 游戏要求的系统运行库（VC++ / DirectX），下载后以管理员身份运行。"""

    MODULE = {'domain': 'https://cdn-gameplayer.games.dmm.com', 'dir_name': 'vc',
              'exec_file_name': 'VC_redist.x64.exe', 'module_version': '1.0',
              'module_file_url': '/product/x/module/vc/data/VC_redist.x64.exe', 'sign': 'k=v'}

    def _service(self):
        session = Mock(); session.device = {}
        with patch('dmmclient.backend.games.catalog', return_value=[
                {'product': 'assaultlily', 'title': 'A', 'adult': False, 'link': ''}]):
            return GameService(session, 'assaultlily')

    def test_module_is_downloaded_then_run(self):
        service = self._service()
        with tempfile.TemporaryDirectory() as folder:
            events = []
            service._download_module = lambda url, target, sign: events.append(('download', target.name, url, sign))
            service._run_module = lambda target: events.append(('run', target.name))
            done = service.install_modules({'modules': [self.MODULE]}, Path(folder), None)
            self.assertEqual([event[0] for event in events], ['download', 'run'])
            self.assertEqual(events[0][2], 'https://cdn-gameplayer.games.dmm.com/product/x/module/vc/data/VC_redist.x64.exe')
            self.assertEqual(events[0][3], 'k=v')
            self.assertEqual(done, {'vc/VC_redist.x64.exe': '1.0'})

    def test_same_version_module_is_not_run_again(self):
        service = self._service()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); target = root / 'vc' / 'VC_redist.x64.exe'
            target.parent.mkdir(parents=True); target.write_bytes(b'MZ')
            events = []
            service._download_module = lambda *a: events.append('download')
            service._run_module = lambda *a: events.append('run')
            service.install_modules({'modules': [self.MODULE]}, root, {'vc/VC_redist.x64.exe': '1.0'})
            self.assertEqual(events, [], '同版本已装过不应重复下载或运行')

    def test_module_path_is_validated(self):
        service = self._service()
        with tempfile.TemporaryDirectory() as folder:
            bad = dict(self.MODULE, dir_name='../../evil')
            with self.assertRaises(DmmError): service.install_modules({'modules': [bad]}, Path(folder), None)

    def test_non_executable_module_is_rejected(self):
        service = self._service()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'x.dll'; path.write_bytes(b'MZ')
            with self.assertRaises(DmmError): service._run_module(path)


class MoveTests(unittest.TestCase):
    """移动游戏目录：先完整复制、再删源目录，并更新安装记录。"""

    def _tree(self, root):
        (root / 'data').mkdir(parents=True)
        (root / 'game.exe').write_bytes(b'MZ')
        (root / 'data' / 'a.bin').write_bytes(b'x' * 16)

    def test_move_relocates_files_and_keeps_record_in_sync(self):
        from dmmclient.backend.games import move_game
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            root = Path(source) / 'game1'; self._tree(root)
            written = {}
            with patch('dmmclient.backend.games.records',
                       return_value={'game1': {'root': str(root), 'version': '1'}}), \
                 patch('dmmclient.backend.games.atomic_write',
                       side_effect=lambda path, blob: written.update({path.name: json.loads(blob)})), \
                 patch('dmmclient.backend.games.remember_install_base'), \
                 patch('dmmclient.backend.games.game_running', return_value=False):
                moved = move_game('game1', target)
            self.assertEqual(moved, Path(target).resolve() / 'game1')
            self.assertFalse(root.exists(), '源目录应已删除')
            self.assertTrue((moved / 'game.exe').is_file())
            self.assertTrue((moved / 'data' / 'a.bin').is_file())
            self.assertEqual(written['games.json']['game1']['root'], str(moved))

    def test_move_refuses_while_the_game_runs(self):
        from dmmclient.backend.games import move_game
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            root = Path(source) / 'game1'; self._tree(root)
            with patch('dmmclient.backend.games.records', return_value={'game1': {'root': str(root)}}), \
                 patch('dmmclient.backend.games.game_running', return_value=True):
                with self.assertRaises(DmmError): move_game('game1', target)
            self.assertTrue(root.exists(), '拒绝后源目录必须完好')

    def test_move_refuses_non_empty_target(self):
        from dmmclient.backend.games import move_game
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            root = Path(source) / 'game1'; self._tree(root)
            occupied = Path(target) / 'game1'; occupied.mkdir(); (occupied / 'keep').write_bytes(b'1')
            with patch('dmmclient.backend.games.records', return_value={'game1': {'root': str(root)}}), \
                 patch('dmmclient.backend.games.game_running', return_value=False):
                with self.assertRaises(DmmError): move_game('game1', target)
            self.assertTrue(root.exists())


if __name__=='__main__': unittest.main()
