"""File-operation regressions use disposable trees; no game or installer runs."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from dmmclient.backend.games import (GameService, fetch_detail_shots, load_shot_pairs,
                                     move_game, store_detail, uninstall_game)
from dmmclient.backend.session import DmmError, OperationCancelled, data_dir
from dmmclient.backend.updater import UpdatePlan, VerifiedUpdater


DOMAIN = 'https://cdn-gameplayer.games.dmm.com'
SIGN = 'CloudFront-Policy=p;CloudFront-Signature=s;CloudFront-Key-Pair-Id=k'


def entry(name, content=b'new', digest=True):
    return {'local_path': name, 'path': '/' + name, 'size': len(content),
            'hash': hashlib.md5(content).hexdigest() if digest else ''}


class FakeResponse:
    def __init__(self, chunks, status=200, headers=None):
        self.chunks, self.status_code, self.headers = chunks, status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class FileIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {
            'LOCALAPPDATA': str(self.base / 'Local'), 'APPDATA': str(self.base / 'Roaming')})
        environment.start()
        self.addCleanup(environment.stop)

    def register(self, root):
        root.mkdir(parents=True, exist_ok=True)
        (root / 'game.exe').write_bytes(b'original')
        data_dir().mkdir(parents=True, exist_ok=True)
        self.record_path = data_dir() / 'games.json'
        self.record_path.write_text(json.dumps({'game1': {'root': str(root), 'version': '1', 'title': 'Game'}}), encoding='utf-8')
        return self.record_path.read_bytes()

    def updater(self, root=None, entries=None, guard=None):
        root = root or self.base / 'update-game'
        root.mkdir(parents=True, exist_ok=True)
        plan = UpdatePlan.parse(root, DOMAIN, entries or [entry('game.exe')], '2')
        return VerifiedUpdater(plan, 'http://127.0.0.1:10808', SIGN,
                               before_commit=guard or (lambda: None))

    def write_journal(self, updater, items):
        updater.work.mkdir(parents=True, exist_ok=True)
        updater.journal.write_text(json.dumps({'state': 'applying', 'items': items}), encoding='utf-8')

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest('This test host does not permit symbolic links')

    def test_move_refuses_both_kinds_of_overlapping_directory(self):
        layouts = [('child', self.base / 'child' / 'game1', self.base / 'child' / 'game1' / 'nested'),
                   ('parent', self.base / 'parent' / 'game1' / 'nested', self.base / 'parent')]
        for label, source, target_base in layouts:
            with self.subTest(layout=label):
                before = self.register(source)
                with patch('dmmclient.backend.games.game_running', return_value=False), \
                        self.assertRaisesRegex(DmmError, '不能互相包含'):
                    move_game('game1', target_base)
                self.assertEqual((source / 'game.exe').read_bytes(), b'original')
                self.assertEqual(self.record_path.read_bytes(), before)

    def test_move_verifies_copy_before_deleting_source(self):
        source = self.base / 'source'
        before = self.register(source)

        def corrupt_copy(original, destination):
            destination.write_bytes(b'corrupt!')

        with patch('dmmclient.backend.games.game_running', return_value=False), \
                patch('dmmclient.backend.games.shutil.copy2', side_effect=corrupt_copy), \
                self.assertRaisesRegex(DmmError, '校验失败'):
            move_game('game1', self.base / 'target')
        self.assertEqual((source / 'game.exe').read_bytes(), b'original')
        self.assertEqual(self.record_path.read_bytes(), before)

    def test_failed_move_record_write_preserves_original(self):
        source = self.base / 'source'
        before = self.register(source)
        with patch('dmmclient.backend.games.game_running', return_value=False), \
                patch('dmmclient.backend.games.atomic_write', side_effect=OSError('disk full')), \
                self.assertRaises(OSError):
            move_game('game1', self.base / 'target')
        self.assertEqual((source / 'game.exe').read_bytes(), b'original')
        self.assertEqual(self.record_path.read_bytes(), before)

    def test_new_file_during_move_keeps_source_and_record(self):
        source = self.base / 'source'
        before = self.register(source)
        copy = shutil.copy2

        def copy_and_change(original, destination):
            result = copy(original, destination)
            (source / 'new-save.dat').write_bytes(b'new save')
            return result

        with patch('dmmclient.backend.games.game_running', return_value=False), \
                patch('dmmclient.backend.games.shutil.copy2', side_effect=copy_and_change), \
                self.assertRaisesRegex(DmmError, '原目录发生变化'):
            move_game('game1', self.base / 'target')
        self.assertEqual((source / 'new-save.dat').read_bytes(), b'new save')
        self.assertEqual(self.record_path.read_bytes(), before)

    def test_failed_old_directory_cleanup_keeps_verified_new_record(self):
        source = self.base / 'source'
        self.register(source)
        messages = []
        remove = shutil.rmtree

        def fail_source(path, *args, **kwargs):
            if Path(path) == source:
                raise PermissionError('busy')
            return remove(path, *args, **kwargs)

        with patch('dmmclient.backend.games.game_running', return_value=False), \
                patch('dmmclient.backend.games.shutil.rmtree', side_effect=fail_source):
            target = move_game('game1', self.base / 'target', message=messages.append)
        self.assertEqual((source / 'game.exe').read_bytes(), b'original')
        self.assertEqual((target / 'game.exe').read_bytes(), b'original')
        record = json.loads(self.record_path.read_text(encoding='utf-8'))['game1']
        self.assertEqual(Path(record['root']).resolve(), target)
        self.assertTrue(any('旧目录' in message for message in messages))

    def test_cancelled_move_can_retry_the_same_destination(self):
        source = self.base / 'source'
        before = self.register(source)
        destination = self.base / 'target'
        cancel = Mock(side_effect=[None, OperationCancelled('已取消')])
        with patch('dmmclient.backend.games.game_running', return_value=False):
            with self.assertRaises(OperationCancelled):
                move_game('game1', destination, cancel_check=cancel)
            self.assertFalse((destination / 'game1').exists())
            self.assertEqual(self.record_path.read_bytes(), before)
            self.assertEqual((source / 'game.exe').read_bytes(), b'original')
            moved = move_game('game1', destination)
        self.assertEqual((moved / 'game.exe').read_bytes(), b'original')
        self.assertFalse(source.exists())

    def test_uninstall_failure_keeps_registration_for_retry(self):
        root = self.base / 'game'
        before = self.register(root)
        unlink = Path.unlink

        def fail_game(path, *args, **kwargs):
            if path == root / 'game.exe':
                raise PermissionError('running')
            return unlink(path, *args, **kwargs)

        with patch('dmmclient.backend.games.game_running', return_value=False), \
                patch.object(Path, 'unlink', fail_game), \
                self.assertRaisesRegex(DmmError, '安装记录已保留'):
            uninstall_game('game1')
        self.assertEqual(self.record_path.read_bytes(), before)
        self.assertEqual((root / 'game.exe').read_bytes(), b'original')

    def test_move_and_uninstall_refuse_links_before_touching_files(self):
        root = self.base / 'game'
        before = self.register(root)
        external = self.base / 'outside'
        external.mkdir()
        (external / 'keep').write_bytes(b'do not touch')
        self.symlink(root / 'linked', external, directory=True)
        with patch('dmmclient.backend.games.game_running', return_value=False):
            for operation in (lambda: move_game('game1', self.base / 'target'),
                              lambda: uninstall_game('game1')):
                with self.assertRaisesRegex(DmmError, '链接|联接'):
                    operation()
        self.assertEqual((external / 'keep').read_bytes(), b'do not touch')
        self.assertEqual((root / 'game.exe').read_bytes(), b'original')
        self.assertEqual(self.record_path.read_bytes(), before)

    def test_nonadjacent_manifest_file_parent_conflict_is_rejected(self):
        with self.assertRaisesRegex(DmmError, '文件和目录冲突'):
            UpdatePlan.parse(self.base, DOMAIN, [entry('a'), entry('a.txt'), entry('a/b')])

    def test_manifest_file_cannot_replace_or_delete_an_existing_directory(self):
        for delete in (False, True):
            with self.subTest(delete=delete):
                root = self.base / ('delete-conflict' if delete else 'replace-conflict')
                root.mkdir()
                (root / 'game.exe').write_bytes(b'old')
                (root / 'saves').mkdir()
                (root / 'saves' / 'user.dat').write_bytes(b'keep save')
                conflict = dict(entry('saves'), force_delete_flg=delete)
                updater = self.updater(root, [entry('game.exe'), conflict])
                updater.backup.mkdir(parents=True)
                (updater.backup / 'previous.dat').write_bytes(b'previous backup')
                updater.journal.write_text('{"state":"committed","items":[]}', encoding='utf-8')
                journal = updater.journal.read_bytes()
                updater.http = SimpleNamespace(proxy_attempts=1,
                    get=Mock(side_effect=lambda *args, **kwargs: FakeResponse([b'new'])))
                with self.assertRaisesRegex(DmmError, '本地目录冲突'):
                    updater.run()
                self.assertEqual((root / 'game.exe').read_bytes(), b'old')
                self.assertEqual((root / 'saves' / 'user.dat').read_bytes(), b'keep save')
                self.assertEqual((updater.backup / 'previous.dat').read_bytes(), b'previous backup')
                self.assertEqual(updater.journal.read_bytes(), journal)

    def test_recovery_checks_all_journal_paths_before_mutating(self):
        updater = self.updater()
        keep = updater.plan.root / 'keep.dat'
        keep.write_bytes(b'keep')
        self.write_journal(updater, [{'local': '../outside', 'existed': False},
                                     {'local': 'keep.dat', 'existed': False}])
        before = updater.journal.read_bytes()
        with self.assertRaises(DmmError):
            updater.recover()
        self.assertEqual(keep.read_bytes(), b'keep')
        self.assertEqual(updater.journal.read_bytes(), before)

    def test_recovery_rejects_non_directory_parent_before_restoring_any_file(self):
        updater = self.updater()
        root = updater.plan.root
        (root / 'blocked').write_bytes(b'keep blocking file')
        (root / 'good.dat').write_bytes(b'new good')
        (updater.backup / 'blocked').mkdir(parents=True)
        (updater.backup / 'blocked' / 'child.dat').write_bytes(b'old child')
        (updater.backup / 'good.dat').write_bytes(b'old good')
        self.write_journal(updater, [{'local': 'blocked/child.dat', 'existed': True},
                                     {'local': 'good.dat', 'existed': True}])
        paths = (root / 'blocked', root / 'good.dat', updater.backup / 'blocked' / 'child.dat',
                 updater.backup / 'good.dat', updater.journal)
        before = {path: path.read_bytes() for path in paths}
        with self.assertRaises(DmmError):
            updater.recover()
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_commit_rejects_non_directory_parent_before_first_replacement(self):
        root = self.base / 'commit-parent-conflict'
        root.mkdir()
        (root / 'good.dat').write_bytes(b'old good')
        updater = self.updater(root, [entry('good.dat'), entry('blocked/child.dat')],
            guard=lambda: (root / 'blocked').write_bytes(b'keep blocking file'))
        updater.backup.mkdir(parents=True)
        (updater.backup / 'previous.dat').write_bytes(b'previous backup')
        updater.journal.write_text('{"state":"committed","items":[]}', encoding='utf-8')
        paths = (root / 'good.dat', updater.backup / 'previous.dat', updater.journal)
        before = {path: path.read_bytes() for path in paths}
        updater.http = SimpleNamespace(proxy_attempts=1,
            get=Mock(side_effect=lambda *args, **kwargs: FakeResponse([b'new'])))
        with self.assertRaises(DmmError):
            updater.run()
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual((root / 'blocked').read_bytes(), b'keep blocking file')
        self.assertFalse((updater.backup / 'good.dat').exists())

    def test_journal_parent_conflict_is_rejected_without_committing(self):
        updater = self.updater()
        self.write_journal(updater, [{'local': name, 'existed': False}
                                     for name in ('a', 'a.txt', 'a/b')])
        before = updater.journal.read_bytes()
        with self.assertRaises(DmmError):
            updater.recover()
        self.assertEqual(updater.journal.read_bytes(), before)

    def test_recovery_respects_running_game_guard(self):
        guard = Mock(side_effect=DmmError('游戏正在运行'))
        updater = self.updater(guard=guard)
        target = updater.plan.root / 'new.dat'
        target.write_bytes(b'new')
        self.write_journal(updater, [{'local': 'new.dat', 'existed': False}])
        with self.assertRaisesRegex(DmmError, '游戏正在运行'):
            updater.recover()
        guard.assert_called_once()
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(json.loads(updater.journal.read_text())['state'], 'applying')

    def test_missing_original_and_backup_is_not_reported_recovered(self):
        updater = self.updater()
        self.write_journal(updater, [{'local': 'lost.dat', 'existed': True}])
        with self.assertRaisesRegex(DmmError, '恢复失败'):
            updater.recover()
        self.assertEqual(json.loads(updater.journal.read_text())['state'], 'applying')

    def test_partial_file_replacement_rolls_back_originals(self):
        root = self.base / 'update'
        root.mkdir()
        for name in ('a.dat', 'b.dat'):
            (root / name).write_bytes(b'old')
        updater = self.updater(root, [entry('a.dat'), entry('b.dat')])

        def staged(file):
            path = updater.staging / file.local
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'new')
            return path

        replace = os.replace

        def fail_second(source, destination):
            if Path(source) == updater.staging / 'b.dat':
                raise OSError('simulated interrupted replacement')
            return replace(source, destination)

        with patch.object(updater, 'download', side_effect=staged), \
                patch('os.replace', side_effect=fail_second), self.assertRaises(OSError):
            updater.run()
        self.assertEqual((root / 'a.dat').read_bytes(), b'old')
        self.assertEqual((root / 'b.dat').read_bytes(), b'old')
        self.assertTrue(json.loads(updater.journal.read_text())['rolled_back'])

    def test_resume_validates_range_and_keeps_verified_partial(self):
        updater = self.updater(entries=[entry('game.exe', b'abcdef')])
        updater.staging.mkdir(parents=True)
        target = updater.staging / 'game.exe'
        target.write_bytes(b'abc')
        get = Mock(side_effect=[FakeResponse([b'def'], 206, {'Content-Range': 'bytes 2-5/6'})])
        updater.http = SimpleNamespace(proxy_attempts=1, get=get)
        with self.assertRaisesRegex(DmmError, '断点下载响应范围'):
            updater.download(updater.plan.files[0])
        self.assertEqual(target.read_bytes(), b'abc')
        get.side_effect = [FakeResponse([b'def'], 206, {'Content-Range': 'bytes 3-5/6'})]
        self.assertEqual(updater.download(updater.plan.files[0]).read_bytes(), b'abcdef')
        self.assertEqual(get.call_args.kwargs['headers'], {'Range': 'bytes=3-'})

    def test_no_hash_staging_cache_is_not_reused_by_size(self):
        updater = self.updater(entries=[entry('game.exe', b'new', digest=False)])
        updater.staging.mkdir(parents=True)
        (updater.staging / 'game.exe').write_bytes(b'old')
        get = Mock(return_value=FakeResponse([b'new']))
        updater.http = SimpleNamespace(proxy_attempts=1, get=get)
        self.assertEqual(updater.download(updater.plan.files[0]).read_bytes(), b'new')
        self.assertEqual(get.call_args.kwargs['headers'], {})

    def test_interrupted_download_preserves_partial_without_changing_game(self):
        updater = self.updater(entries=[entry('game.exe', b'abcdef')])
        installed = updater.plan.root / 'game.exe'
        installed.write_bytes(b'old game')
        get = Mock(return_value=FakeResponse([b'abc', requests.ConnectionError('interrupted')]))
        updater.http = SimpleNamespace(proxy_attempts=1, get=get)
        with self.assertRaisesRegex(DmmError, '已保存断点'):
            updater.download(updater.plan.files[0])
        self.assertEqual((updater.staging / 'game.exe').read_bytes(), b'abc')
        self.assertEqual(installed.read_bytes(), b'old game')

    def test_module_paths_cannot_escape_or_overwrite_update_metadata(self):
        service = GameService(Mock(), 'priconner')
        module = {'domain': DOMAIN, 'dir_name': '..', 'exec_file_name': 'run.exe',
                  'module_version': '1', 'module_file_url': '/run.exe'}
        for folder in ('..', '.', '.ok-gm-update'):
            with self.subTest(folder=folder), patch.object(service, '_download_module') as download, \
                    self.assertRaises(DmmError):
                service.install_modules({'modules': [dict(module, dir_name=folder)]}, self.base)
            download.assert_not_called()

    def test_module_partial_symlink_is_rejected_before_download(self):
        service = GameService(Mock(), 'priconner')
        outside = self.base / 'outside'
        outside.write_bytes(b'keep')
        self.symlink(self.base / 'run.exe.part', outside)
        with self.assertRaisesRegex(DmmError, '链接|联接'):
            service._download_module(DOMAIN + '/run.exe', self.base / 'run.exe', '')
        self.assertEqual(outside.read_bytes(), b'keep')
        service.session.http.get.assert_not_called()

    def test_failed_runtime_is_not_remembered_as_installed(self):
        service = GameService(Mock(), 'priconner')
        module = {'domain': DOMAIN, 'dir_name': 'vc', 'exec_file_name': 'run.exe',
                  'module_version': '1', 'module_file_url': '/run.exe'}
        previous = {}

        def downloaded(url, target, sign):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'MZ')

        with patch.object(service, '_download_module', side_effect=downloaded), \
                patch('dmmclient.backend.games.run_installer', return_value=1603), \
                self.assertRaisesRegex(DmmError, '退出码 1603'):
            service.install_modules({'modules': [module]}, self.base, previous)
        self.assertEqual(previous, {})
        with patch.object(service, '_download_module') as download, \
                patch('dmmclient.backend.games.run_installer', return_value=3010):
            done = service.install_modules({'modules': [module]}, self.base, previous)
        download.assert_called_once()
        self.assertEqual(done, {'vc/run.exe': '1'})

    def test_malformed_module_list_is_not_silently_skipped(self):
        service = GameService(Mock(), 'priconner')
        for modules in ({}, 'bad', [None], [{}]):
            with self.subTest(modules=modules), self.assertRaises(DmmError):
                service.install_modules({'modules': modules}, self.base)

    def test_cached_detail_and_shot_paths_reject_invalid_product(self):
        for operation in (lambda: store_detail(Mock(), '../outside'),
                          lambda: fetch_detail_shots(Mock(), '../outside', []),
                          lambda: load_shot_pairs('../outside')):
            with self.assertRaisesRegex(DmmError, '游戏编号无效'):
                operation()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_invalid_action_never_starts_an_install(self):
        session = Mock()
        service = GameService(session, 'priconner')
        with self.assertRaisesRegex(DmmError, '未知操作'):
            service.execute('unknown', self.base)
        session.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
