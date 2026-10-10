import json
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

    def test_touch_scrolling_grabs_viewport(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QListWidget, QScroller, QScrollerProperties
        from dmmclient.app import enable_touch_scrolling
        app = QApplication.instance() or QApplication([])
        grid = QListWidget()
        scroller = enable_touch_scrolling(grid)
        self.assertTrue(grid.viewport().testAttribute(Qt.WA_AcceptTouchEvents))
        self.assertEqual(
            scroller.scrollerProperties().scrollMetric(
                QScrollerProperties.ScrollMetric.VerticalOvershootPolicy),
            QScrollerProperties.OvershootPolicy.OvershootAlwaysOff)
        grid.deleteLater(); app.processEvents()

    def test_touch_tap_is_forwarded_but_drag_is_not(self):
        """轻点补成点击、拖动不补 —— 这是 QScroller 吞掉触摸后按钮仍可点的关键。"""
        from PySide6.QtCore import QPointF
        from PySide6.QtWidgets import QListWidget, QScroller
        from dmmclient.app import TouchTapForwarder
        app = QApplication.instance() or QApplication([])
        grid = QListWidget()
        scroller = QScroller.scroller(grid.viewport())
        clicks = []
        with patch.object(TouchTapForwarder, '_click', staticmethod(lambda pos: clicks.append(pos))):
            forwarder = TouchTapForwarder(grid.viewport(), scroller)
            forwarder.press_global = QPointF(5, 5)
            scroller.stateChanged.emit(QScroller.State.Pressed)
            scroller.stateChanged.emit(QScroller.State.Inactive)
            self.assertEqual(len(clicks), 1, '轻点应补成一次点击')
            clicks.clear(); forwarder.press_global = QPointF(5, 5)
            scroller.stateChanged.emit(QScroller.State.Pressed)
            scroller.stateChanged.emit(QScroller.State.Dragging)
            scroller.stateChanged.emit(QScroller.State.Scrolling)
            scroller.stateChanged.emit(QScroller.State.Inactive)
            self.assertEqual(clicks, [], '拖动结束后不应补点击')
        grid.deleteLater(); app.processEvents()


    def test_actions_queue_instead_of_being_rejected(self):
        """有操作在跑时，新的操作应排队而不是被拒绝。"""
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch.object(DmmHelper, 'installed_game', return_value=(Path(folder), '1')), \
                patch.object(Window, 'kick_covers', lambda self: None):
            window = Window()
            busy = Mock(); busy.isRunning.return_value = True
            window.worker = busy
            window.run_action('game-check', 'priconne')
            self.assertEqual(len(window.pending_actions), 1)
            self.assertIn('已排入队列', window.status.text())
            window.pending_actions.clear(); window.worker = None
            window.close(); window.deleteLater(); app.processEvents()

    def test_non_conflicting_actions_never_queue(self):
        """保存代理设置不与其他操作冲突，应立刻执行而不是排队。"""
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch.object(DmmHelper, 'installed_game', return_value=(Path(folder), '1')), \
                patch.object(Window, 'kick_covers', lambda self: None):
            window = Window()
            busy = Mock(); busy.isRunning.return_value = True
            window.worker = busy
            window.run_action('settings')
            self.assertEqual(len(window.pending_actions), 0)
            self.assertIn('已保存', window.status.text())
            window.worker = None
            window.close(); window.deleteLater(); app.processEvents()

    def test_switch_account_does_not_require_game_closed(self):
        """游戏在运行时也应允许切换账号（存档只是文件替换，不影响已启动的游戏）。"""
        from dmmclient.backend.accounts import AccountStore
        running = Mock(); running.info = {'name': 'game.exe', 'exe': r'C:\games\x\game.exe'}
        with tempfile.TemporaryDirectory() as folder, \
                patch('dmmclient.backend.games.records', return_value={'x': {'root': r'C:\games\x'}}), \
                patch('psutil.process_iter', return_value=[running]):
            store = AccountStore(Path(folder))
            with self.assertRaises(DmmError) as caught:
                store.switch('1')
        self.assertIn('已保存的账号', str(caught.exception))
        self.assertNotIn('游戏正在运行', str(caught.exception))

    def test_queued_actions_chain_after_the_current_one(self):
        """当前操作结束后，队列里的下一个自动开始（FIFO）。"""
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch.object(DmmHelper, 'installed_game', return_value=(Path(folder), '1')), \
                patch.object(Window, 'kick_covers', lambda self: None):
            window = Window()
            started = []
            window.start_operation = lambda request: started.append(request[0])
            window.pending_actions.extend([('game-check', {}, '', '', '', 'a'),
                                           ('game-update', {}, '', '', '', 'b')])
            window.worker = Mock()
            window.operation_finished()
            self.assertEqual(started, ['game-check'])
            self.assertEqual([request[0] for request in window.pending_actions], ['game-update'])
            window.pending_actions.clear(); window.worker = None
            window.close(); window.deleteLater(); app.processEvents()

    def test_every_game_can_pick_its_install_directory(self):
        """所有游戏（不只学园偶像大师）都能指定安装目录。"""
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as target, \
                patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch.object(DmmHelper, 'installed_game', return_value=(Path(folder), '1')), \
                patch.object(Window, 'kick_covers', lambda self: None):
            data_dir().mkdir(parents=True, exist_ok=True)
            (data_dir() / 'catalog.json').write_text(json.dumps([
                {'product': 'umamusume', 'title': 'Uma', 'adult': False, 'link': ''}]), encoding='utf-8')
            window = Window()
            self.assertIn('目录', [button.text() for button in window.store_buttons])
            with patch('dmmclient.app.QFileDialog.getExistingDirectory', return_value=target):
                location = window.ensure_install_location('umamusume')
            self.assertEqual(location, Path(target).resolve() / 'umamusume')
            # 选过一次就记住，不再追问
            self.assertEqual(window.ensure_install_location('umamusume'), Path(target).resolve() / 'umamusume')
            window.close(); window.deleteLater(); app.processEvents()

    def test_install_location_defaults_to_client_games_dir(self):
        from dmmclient.backend.settings import game_install_root
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            self.assertEqual(game_install_root('priconner'),
                             Path(folder) / 'DmmLightClient' / 'games' / 'priconner')

    def test_refresh_keeps_card_wall_scroll_position(self):
        """安装完成后的刷新不应把卡片墙弹回顶部（clear() 会把滚动条归零）。"""
        import json
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch.object(DmmHelper, 'installed_game', return_value=(Path(folder), '1')), \
                patch.object(Window, 'kick_covers', lambda self: None):
            games = [{'product': f'game{i:02d}', 'title': f'Game {i}', 'adult': False,
                      'image_url': '', 'genre': '', 'maker': '', 'link': ''} for i in range(60)]
            data_dir().mkdir(parents=True, exist_ok=True)
            (data_dir() / 'catalog.json').write_text(json.dumps(games), encoding='utf-8')
            window = Window()
            window.show(); window.tabs.setCurrentIndex(1)
            for _ in range(10): app.processEvents()
            bar = window.store_grid.verticalScrollBar()
            self.assertGreater(bar.maximum(), 500)
            bar.setValue(500)
            for _ in range(3): app.processEvents()
            window.refresh()
            for _ in range(8): app.processEvents()
            self.assertEqual(bar.value(), 500, '刷新后应停在原位置')
            window.search.setText('Game 1')
            for _ in range(8): app.processEvents()
            self.assertEqual(bar.value(), 0, '搜索应回到顶部')
            window.close(); window.deleteLater(); app.processEvents()


if __name__ == '__main__': unittest.main()
