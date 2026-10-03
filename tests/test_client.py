import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from PySide6.QtWidgets import QApplication
from dmmclient.app import Window, Worker
from dmmclient.backend.session import valid_proxy, DmmError, data_dir
from dmmclient.backend.helper import DmmHelper
from dmmclient.backend.xray import NodeStore, parse_node


class ClientTests(unittest.TestCase):
    def test_data_is_independent(self):
        self.assertEqual(data_dir().name, 'DmmLightClient')

    def test_supported_proxies(self):
        for proxy in ['http://127.0.0.1:10808', 'socks5h://localhost:1080']:
            self.assertEqual(valid_proxy(proxy), proxy)
        for proxy in ['file:///tmp/x', 'http://user:password@localhost:1080', 'http://localhost']:
            with self.assertRaises(DmmError): valid_proxy(proxy)

    def test_launch_updates_first(self):
        helper = Mock(spec=DmmHelper)
        events = []
        helper.prepare.side_effect = lambda flag: (events.append(('prepare',flag)) or Mock(exe=Path('gakumas.exe'),arguments=[],version='1'))
        helper.progress = lambda _: None
        with patch('dmmclient.backend.helper.ensure_game_stopped'), patch('dmmclient.backend.helper.subprocess.Popen',side_effect=lambda *a,**kw: (events.append(('process',None)) or Mock(pid=1))):
            DmmHelper.launch(helper, True)
        self.assertEqual(events,[('prepare',True),('process',None)])

    def test_encrypted_node_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            store=NodeStore(Path(folder))
            node=parse_node('vless://11111111-1111-1111-1111-111111111111@example.com:443?security=tls#Test')
            store.save(node)
            self.assertNotIn(b'11111111-1111',store.path.read_bytes())
            self.assertEqual(store.read()['nodes'][0]['name'],'Test')

    def test_independent_window_and_settings(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'LOCALAPPDATA':folder}), patch.object(DmmHelper,'installed_game',return_value=(Path(folder),'1')):
            window=Window()
            self.assertEqual(window.tabs.count(),4)
            window.mode.setCurrentIndex(1)
            window.proxy.setText('socks5h://127.0.0.1:10808')
            window.run_action('settings')
            self.assertTrue((data_dir()/'settings.json').exists())
            self.assertIn('已保存',window.status.text())
            window.close(); window.deleteLater(); app.processEvents()

    def test_pause_is_recoverable(self):
        worker=Worker('update',{'proxy':'http://127.0.0.1:10808'})
        worker.cancelled.set()
        from dmmclient.backend.session import OperationCancelled
        with self.assertRaises(OperationCancelled): worker.check_cancel()


if __name__ == '__main__': unittest.main()
