"""CLI regression tests use only temporary installation metadata and files."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dmmclient import cli
from dmmclient.backend.helper import DmmHelper
from dmmclient.backend.session import data_dir


class GakumasCliTests(unittest.TestCase):
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
        official = self.root / 'roaming' / 'dmmgameplayer5'
        official.mkdir(parents=True)
        (official / 'dmmgame.cnf').write_text('{"contents": []}', encoding='utf-8')
        (data_dir() / 'games.json').write_text('{}', encoding='utf-8')

    def game_directory(self, name='chosen-game'):
        root = self.root / name
        root.mkdir()
        (root / 'gakumas.exe').write_bytes(b'MZ-test-fixture')
        return root.resolve()

    def bind(self, root, version='3.5.0'):
        (data_dir() / 'installation.json').write_text(
            json.dumps({'root': str(root), 'version': version}), encoding='utf-8')

    def test_list_includes_an_independently_bound_gakumas(self):
        root = self.game_directory()
        self.bind(root)
        with patch.object(cli, 'say') as say:
            self.assertEqual(cli.main(['list', '--json']), 0)
        output = json.loads(say.call_args.args[0])
        self.assertEqual(Path(output['gakumas']['root']).resolve(), root)
        self.assertEqual(output['gakumas']['version'], '3.5.0')

    def test_dir_set_binds_the_exact_game_directory(self):
        root = self.game_directory()
        settings = {'proxy': 'socks5h://127.0.0.1:1080', 'install_base': 'preserved-base'}
        (data_dir() / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
        with patch.object(cli, 'say'):
            self.assertEqual(cli.main(['dir', 'gakumas', '--set', str(root)]), 0)
        installed, _version = DmmHelper.installed_game()
        self.assertEqual(installed, root)
        self.assertFalse((root / 'gakumas').exists(), 'Binding must not append another product directory')
        saved_settings = json.loads((data_dir() / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved_settings['proxy'], settings['proxy'])
        self.assertEqual(saved_settings['install_base'], settings['install_base'])

    def test_dir_set_rejects_an_empty_directory_without_replacing_the_binding(self):
        root = self.game_directory()
        self.bind(root)
        before = (data_dir() / 'installation.json').read_bytes()
        empty = self.root / 'empty'
        empty.mkdir()
        with patch.object(cli, 'say'):
            self.assertEqual(cli.main(['dir', 'gakumas', '--set', str(empty)]), 1)
        self.assertEqual((data_dir() / 'installation.json').read_bytes(), before)
        self.assertEqual(DmmHelper.installed_game()[0], root)

    def test_dir_reports_the_specialized_installation_path(self):
        root = self.game_directory()
        self.bind(root)
        with patch.object(cli, 'say') as say:
            self.assertEqual(cli.main(['dir', 'gakumas']), 0)
        output = '\n'.join(str(call.args[0]) for call in say.call_args_list)
        self.assertIn(str(root), output)


if __name__ == '__main__':
    unittest.main()
