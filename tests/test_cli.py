import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dmmclient.backend.session import data_dir
from dmmclient.backend.settings import game_install_root
from dmmclient.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_all_commands_are_wired(self):
        parser = build_parser()
        bare = ('list', 'catalog', 'refresh', 'nodes', 'proxy', 'ping', 'accounts')
        for command in bare + ('check', 'install', 'update', 'launch', 'dir'):
            args = parser.parse_args([command] if command in bare else [command, 'priconner'])
            self.assertTrue(callable(args.func), command)
        self.assertEqual(parser.parse_args(['move', 'priconner', '--dir', 'D:/g']).dir, 'D:/g')
        self.assertEqual(parser.parse_args(['install', 'priconner', '--dir', 'D:/g']).dir, 'D:/g')

    def test_dir_set_remembers_the_install_location(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as target, \
                patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            self.assertEqual(main(['dir', 'umamusume', '--set', target]), 0)
            self.assertEqual(game_install_root('umamusume'), Path(target).resolve() / 'umamusume')

    def test_invalid_product_is_rejected_with_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            self.assertEqual(main(['dir', '../evil']), 1)
            self.assertEqual(main(['move', '../evil', '--dir', folder]), 1)

    def test_list_prints_installed_games(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch('dmmclient.cli.official_records', return_value={}):
            data_dir().mkdir(parents=True, exist_ok=True)
            (data_dir() / 'games.json').write_text(
                json.dumps({'priconner': {'root': 'D:/games/priconner', 'version': '12.7.2'}}), encoding='utf-8')
            self.assertEqual(main(['list']), 0)
            self.assertEqual(main(['list', '--json']), 0)


    def test_uninstall_previews_before_it_deletes(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as target, \
                patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            data_dir().mkdir(parents=True, exist_ok=True)
            root = Path(target) / 'game1'; root.mkdir(); (root / 'game.exe').write_bytes(b'MZ')
            (data_dir() / 'games.json').write_text(
                json.dumps({'game1': {'root': str(root), 'version': '1'}}), encoding='utf-8')
            self.assertEqual(main(['uninstall', 'game1']), 0)
            self.assertTrue(root.exists(), '不加 --yes 不能删文件')
            self.assertEqual(main(['uninstall', 'game1', '--yes']), 0)
            self.assertFalse(root.exists())

    def test_uninstall_keep_leaves_the_files(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as target, \
                patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            data_dir().mkdir(parents=True, exist_ok=True)
            root = Path(target) / 'game1'; root.mkdir(); (root / 'game.exe').write_bytes(b'MZ')
            (data_dir() / 'games.json').write_text(
                json.dumps({'game1': {'root': str(root), 'version': '1'}}), encoding='utf-8')
            self.assertEqual(main(['uninstall', 'game1', '--keep']), 0)
            self.assertTrue(root.exists())
            self.assertEqual(json.loads((data_dir() / 'games.json').read_text(encoding='utf-8')), {})


if __name__ == '__main__': unittest.main()
