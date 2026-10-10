import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dmmclient.backend.games import GameService
from dmmclient.backend.session import DmmError, data_dir


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


class VersionTests(unittest.TestCase):
    """版本记录必须反映实际：游戏自己更新过时本地记录会过期，要能纠正回来。"""

    def test_check_records_the_version_when_files_already_match(self):
        session = Mock(); session.device = {}
        session.request.side_effect = [
            {'game_type': 'GCL'},
            {'product_id': 'priconner', 'latest_version': '12.8.0', 'file_list_url': '/m', 'sign': 's'},
        ]
        service = GameService(session, 'priconner')
        plan = Mock(); plan.pending.return_value = []
        service.plan = Mock(return_value=plan)
        service.updater = Mock(return_value=Mock())
        written = {}
        with patch('dmmclient.backend.games.records',
                   return_value={'priconner': {'root': str(Path(tempfile.gettempdir())), 'version': '12.7.2'}}), \
             patch('dmmclient.backend.games.atomic_write',
                   side_effect=lambda path, blob: written.update({path.name: json.loads(blob)})):
            service.execute('check')
        self.assertEqual(written['games.json']['priconner']['version'], '12.8.0',
                         '本地文件与清单一致时，记录应更新为服务器版本')

    def test_check_keeps_the_record_when_files_differ(self):
        session = Mock(); session.device = {}
        session.request.side_effect = [
            {'game_type': 'GCL'},
            {'product_id': 'priconner', 'latest_version': '12.8.0', 'file_list_url': '/m', 'sign': 's'},
        ]
        service = GameService(session, 'priconner')
        plan = Mock(); plan.pending.return_value = [Mock(size=10)]
        service.plan = Mock(return_value=plan)
        service.updater = Mock(return_value=Mock())
        with patch('dmmclient.backend.games.records',
                   return_value={'priconner': {'root': str(Path(tempfile.gettempdir())), 'version': '12.7.2'}}), \
             patch('dmmclient.backend.games.atomic_write') as write:
            service.execute('check')
        write.assert_not_called()

    def test_record_version_writes_to_the_right_record(self):
        from dmmclient.backend.games import record_version
        from dmmclient.backend.session import data_dir
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            data_dir().mkdir(parents=True, exist_ok=True)
            (data_dir() / 'games.json').write_text(
                json.dumps({'priconner': {'root': 'D:/g', 'version': '1'}}), encoding='utf-8')
            record_version('priconner', '2')
            self.assertEqual(json.loads((data_dir() / 'games.json').read_text(encoding='utf-8'))
                             ['priconner']['version'], '2')
            record_version('gakumas', '3.4.1', 'D:/GAMES/DMM_GAME/gakumas')
            self.assertEqual(json.loads((data_dir() / 'installation.json').read_text(encoding='utf-8')),
                             {'root': 'D:/GAMES/DMM_GAME/gakumas', 'version': '3.4.1'})

    def test_verify_version_only_reports_a_version_when_files_match(self):
        from dmmclient.backend.games import verify_version
        session = Mock(); session.device = {}
        with patch.object(GameService, 'request_info', return_value={'latest_version': '3.4.1'}), \
             patch.object(GameService, 'plan') as plan:
            plan.return_value.pending.return_value = []
            self.assertEqual(verify_version(session, 'priconner', 'D:/g')['version'], '3.4.1')
            plan.return_value.pending.return_value = [1, 2]
            result = verify_version(session, 'priconner', 'D:/g')
            self.assertIsNone(result['version'], '还有文件待更新时不能声称已是新版本')
            self.assertEqual(result['pending'], 2)
            self.assertEqual(result['latest'], '3.4.1')


class UninstallTests(unittest.TestCase):
    """卸载：默认删文件 + 记录；可只删记录保留文件；不安全或运行中一律拒绝。"""

    def _tree(self, root):
        (root / 'data').mkdir(parents=True)
        (root / 'game.exe').write_bytes(b'MZ')
        (root / 'data' / 'a.bin').write_bytes(b'x' * 32)

    def _run(self, root, folder, keep=False):
        from dmmclient.backend.games import uninstall_game
        written = {}
        with patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
             patch('dmmclient.backend.games.records', return_value={'game1': {'root': str(root)}}), \
             patch('dmmclient.backend.games.atomic_write',
                   side_effect=lambda path, blob: written.update({path.name: json.loads(blob)})), \
             patch('dmmclient.backend.games.game_running', return_value=False):
            return uninstall_game('game1', keep_files=keep), written

    def test_uninstall_deletes_files_and_drops_the_record(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as data:
            root = Path(folder) / 'game1'; self._tree(root)
            result, written = self._run(root, data)
            self.assertFalse(root.exists(), '游戏目录应被删除')
            self.assertEqual(result['files'], 2)
            self.assertEqual(result['bytes'], 34)
            self.assertEqual(written['games.json'], {}, '安装记录应被移除')

    def test_keep_files_only_drops_the_record(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as data:
            root = Path(folder) / 'game1'; self._tree(root)
            result, written = self._run(root, data, keep=True)
            self.assertTrue(root.exists(), '保留文件时目录必须还在')
            self.assertTrue((root / 'game.exe').is_file())
            self.assertEqual(result['files'], 0)
            self.assertEqual(written['games.json'], {})

    def test_refuses_while_the_game_runs(self):
        from dmmclient.backend.games import uninstall_game
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as data:
            root = Path(folder) / 'game1'; self._tree(root)
            with patch.dict(os.environ, {'LOCALAPPDATA': data}), \
                 patch('dmmclient.backend.games.records', return_value={'game1': {'root': str(root)}}), \
                 patch('dmmclient.backend.games.game_running', return_value=True):
                with self.assertRaises(DmmError): uninstall_game('game1')
            self.assertTrue(root.exists())

    def test_refuses_a_root_that_would_take_the_data_dir_with_it(self):
        from dmmclient.backend.games import uninstall_game
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)          # LOCALAPPDATA 本身，数据目录就在它下面
            with patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                 patch('dmmclient.backend.games.records', return_value={'game1': {'root': str(root)}}), \
                 patch('dmmclient.backend.games.game_running', return_value=False):
                with self.assertRaises(DmmError): uninstall_game('game1')
            self.assertTrue(root.exists(), '不安全的目标必须原样保留')


class DetailTests(unittest.TestCase):
    """商店详情来自官方 /v5/detail/product，不抓网页。"""

    def test_normalize_shapes_the_api_payload(self):
        from dmmclient.backend.games import _normalize_detail
        value = _normalize_detail({
            'title': '游戏', 'content_text': '第一行<br>第二行<br/>第三行',
            'captions': ['需要 10GB'],
            'gallery_images': [{'thumbnail_url': 't', 'image_url': 'f'}, {'thumbnail_url': 'x'}],
            'gallery_movies': [{'thumbnail': 'mt', 'mp4_url': 'm'}],
            'link_tables': [{'disp_key': '提供元', 'disp_items': [{'disp_value': 'A'}, {'disp_value': 'B'}]},
                            {'disp_key': '空的', 'disp_items': []}],
            'specs': [{'target_os': 'Windows', 'spec_html': 'OS:Win11<br>内存:16GB'}],
        })
        self.assertEqual(value['title'], '游戏')
        self.assertEqual(value['description'], '第一行\n第二行\n第三行')
        self.assertEqual(value['captions'], ['需要 10GB'])
        self.assertEqual(value['images'], [{'thumb': 't', 'full': 'f'}])   # 缺 image_url 的丢掉
        self.assertEqual(value['movies'][0]['mp4'], 'm')
        self.assertEqual(value['info'], [{'key': '提供元', 'value': 'A / B'}])   # 空表丢掉
        self.assertEqual(value['specs'][0]['html'], 'OS:Win11\n内存:16GB')

    def test_store_detail_prefers_the_local_cache(self):
        from dmmclient.backend.games import store_detail
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            cache = data_dir() / 'details'; cache.mkdir(parents=True)
            (cache / 'game1.json').write_text(json.dumps({'title': '缓存标题'}), encoding='utf-8')
            session = Mock()
            self.assertEqual(store_detail(session, 'game1')['title'], '缓存标题')
            session.request.assert_not_called()

    def test_store_detail_tries_the_other_game_type(self):
        from dmmclient.backend.games import store_detail
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            session = Mock(); session.device = {}
            session.request.side_effect = [DmmError('拒绝'), {'title': '第二次成功'}, {'link_list': []}]
            with patch('dmmclient.backend.games.set_age_cookie'):
                self.assertEqual(store_detail(session, 'game1')['title'], '第二次成功')
            paths = [call.args[0] for call in session.request.call_args_list]
            self.assertEqual(paths[:2], ['/v5/detail/product'] * 2)   # 换类型重试了一次


if __name__=='__main__': unittest.main()
