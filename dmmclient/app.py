"""Independent Qt client: no ok-script imports or automation tasks."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import requests
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QProgressBar, QPushButton,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from .backend.accounts import AccountStore
from .backend.browser_proxy import restore_browser_proxy
from .backend.helper import DmmHelper, helper_lock, ensure_game_stopped
from .backend.launcher import browser_login, import_login, login_candidate
from .backend.session import DmmError, LoginRequired, OperationCancelled, atomic_write, data_dir, valid_proxy
from .backend.xray import NODE_ENABLED_KEY, NodeStore, operation_proxy, attach_pool


def load_settings():
    path = data_dir() / 'settings.json'
    defaults = {'proxy': 'http://127.0.0.1:10808', NODE_ENABLED_KEY: True}
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding='utf-8')))
    return defaults


class Worker(QThread):
    message = Signal(str)
    download = Signal(int, str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, action, settings, number='', name='', node_text='', parent=None):
        super().__init__(parent)
        self.action, self.settings = action, dict(settings)
        self.number, self.name, self.node_text = number, name, node_text
        self.cancelled = threading.Event()
        self.start_time = time.monotonic()

    def check_cancel(self):
        if self.cancelled.is_set():
            raise OperationCancelled('已暂停；再次更新或启动时将继续下载')

    def progress(self, done, total):
        elapsed = max(time.monotonic() - self.start_time, 0.1)
        self.download.emit(int(done * 100 / total) if total else 100,
                           f'{done / 1048576:.1f} / {total / 1048576:.1f} MiB · 平均 {done / elapsed / 1048576:.1f} MiB/s')

    def run(self):
        try:
            root = data_dir()
            with helper_lock(root):
                self.check_cancel()
                restore_browser_proxy(root, progress=self.message.emit)
                accounts = AccountStore(root)
                if self.action == 'save-account':
                    accounts.save_current(self.number, self.name)
                    self.finished_ok.emit('账号已保存'); return
                if self.action == 'switch-account':
                    accounts.switch(self.number)
                    self.finished_ok.emit('账号已切换'); return
                if self.action == 'import-node':
                    NodeStore(root).import_node(self.node_text, self.settings['proxy'], self.message.emit)
                    self.finished_ok.emit('节点已加密导入'); return
                with operation_proxy(self.settings, self.message.emit, self.check_cancel, root) as proxy:
                    if self.action == 'test':
                        with requests.Session() as http:
                            http.trust_env = False
                            http.proxies = {'http': proxy, 'https': proxy}
                            attach_pool(http, proxy)
                            with http.get('https://player.games.dmm.co.jp/', timeout=(10, 20),
                                          allow_redirects=False, stream=True) as response:
                                response.raise_for_status()
                                self.finished_ok.emit(f'DMM 连接成功（HTTP {response.status_code}）')
                        return
                    helper = DmmHelper(proxy, self.message.emit, root=root, download_progress=self.progress)
                    helper.session.cancel_check = self.check_cancel
                    if self.action == 'new-account':
                        ensure_game_stopped()
                        accounts.validate(self.number, self.name)
                        if any(a['number'] == self.number for a in accounts.read()['accounts']):
                            raise DmmError('编号已存在，请使用新编号')
                        with login_candidate(helper, fresh=True) as candidate:
                            browser_login(helper, self.message.emit)
                            candidate.verify_game_identity()
                            accounts.save_current(self.number, self.name, candidate.path, new=True)
                        result = '账号登录并保存完成'
                    elif self.action == 'import-login':
                        import_login(helper)
                        result = '现有 DMM 登录态已导入，请填写编号保存账号'
                    elif self.action == 'login':
                        browser_login(helper, self.message.emit)
                        result = '登录完成'
                    else:
                        try:
                            result = self.game_action(helper)
                        except LoginRequired:
                            self.message.emit('登录已过期，请在浏览器完成登录')
                            browser_login(helper, self.message.emit)
                            result = self.game_action(helper)
                    self.finished_ok.emit(result)
        except (DmmError, requests.RequestException) as error:
            self.failed.emit(str(error) if isinstance(error, DmmError) else '网络连接失败，请测试代理或切换节点')
        except Exception as error:
            self.failed.emit(f'操作未完成（{type(error).__name__}）；敏感内容已隐藏')

    def game_action(self, helper):
        if self.action == 'check':
            result = helper.check()
            return f"最新版本 {result['latest_version']} · 待更新 {result['pending_files']} 个文件 · {result['download_bytes'] / 1048576:.1f} MiB"
        if self.action == 'update':
            spec = helper.prepare(True)
            return f'版本 {spec.version} 已更新并校验'
        if self.action == 'launch':
            helper.launch(True)
            return '游戏进程已启动；请确认游戏内登录完成'
        raise DmmError('未知操作')


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.worker = None
        self.buttons = []
        self.setWindowTitle('DMM Light · 独立客户端预览版')
        self.resize(1000, 760)
        outer = QWidget(); self.setCentralWidget(outer)
        layout = QVBoxLayout(outer); layout.setContentsMargins(28, 24, 28, 24); layout.setSpacing(14)
        heading = QLabel('DMM Light'); heading.setObjectName('heading'); layout.addWidget(heading)
        layout.addWidget(QLabel('游戏、账号与下载，在一个独立客户端中管理'))
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        game = QWidget(); g = QVBoxLayout(game); g.setContentsMargins(24, 24, 24, 24)
        title = QLabel('学园偶像大师'); title.setObjectName('subtitle'); g.addWidget(title)
        self.install_label = QLabel(); self.install_label.setWordWrap(True); g.addWidget(self.install_label)
        g.addWidget(QLabel('启动前自动检查、下载并校验更新。更新过程不计入游戏启动倒计时。'))
        row = QHBoxLayout(); g.addLayout(row)
        self.button(row, '启动游戏', 'launch', primary=True)
        self.button(row, '检查更新', 'check'); self.button(row, '更新 / 继续下载', 'update')
        self.button(row, '选择游戏目录', 'game-folder')
        g.addStretch(); g.addWidget(QLabel('预览版目前支持已安装的学园偶像大师。其他游戏与全新安装尚未开放。'))
        self.tabs.addTab(game, '游戏库')

        account = QWidget(); a = QVBoxLayout(account); a.setContentsMargins(24, 24, 24, 24)
        a.addWidget(QLabel('登录状态加密保存在本客户端的数据目录；切换前需要关闭游戏。'))
        self.accounts = QComboBox(); a.addWidget(self.accounts)
        form = QFormLayout(); self.number = QLineEdit(); self.name = QLineEdit()
        form.addRow('账号编号', self.number); form.addRow('显示名称', self.name); a.addLayout(form)
        row = QHBoxLayout(); a.addLayout(row)
        self.button(row, '登录新账号', 'new-account'); self.button(row, '保存当前账号', 'save-account')
        self.button(row, '切换所选账号', 'switch-account')
        row = QHBoxLayout(); a.addLayout(row)
        self.button(row, '浏览器登录 / 续期', 'login'); self.button(row, '导入官方客户端登录态', 'import-login')
        a.addWidget(QLabel('密码与验证码只在 DMM 官方网页输入。登录成功后复制成功页地址即可返回客户端。'))
        a.addStretch(); self.tabs.addTab(account, '账号')

        network = QWidget(); n = QVBoxLayout(network); n.setContentsMargins(24, 24, 24, 24)
        self.mode = QComboBox(); self.mode.addItems(['内置 Xray 节点池', '外部代理地址'])
        self.mode.setCurrentIndex(0 if self.settings[NODE_ENABLED_KEY] else 1); n.addWidget(self.mode)
        self.proxy = QLineEdit(self.settings['proxy']); self.proxy.setPlaceholderText('http://127.0.0.1:10808 或 socks5h://127.0.0.1:10808'); n.addWidget(self.proxy)
        n.addWidget(QLabel('外部代理也用于首次下载 Xray 内核；已有内核时，内置模式不依赖 v2rayN。'))
        row = QHBoxLayout(); n.addLayout(row)
        self.button(row, '保存代理设置', 'settings'); self.button(row, '测试 DMM 连接', 'test')
        self.nodes = QComboBox(); n.addWidget(self.nodes)
        self.node_text = QLineEdit(); self.node_text.setEchoMode(QLineEdit.EchoMode.Password)
        self.node_text.setPlaceholderText('粘贴节点分享链接或单节点 Xray JSON，内容隐藏'); n.addWidget(self.node_text)
        row = QHBoxLayout(); n.addLayout(row)
        self.button(row, '添加节点', 'import-node'); self.button(row, '导入 JSON 文件', 'node-file')
        n.addWidget(QLabel('最多 32 个节点，轮换使用，连接失败自动尝试下一节点。\n浏览器登录使用 HTTP 代理或内置节点池；SOCKS5 支持 API、更新和下载。'))
        n.addStretch(); self.tabs.addTab(network, '网络')

        self.status = QLabel('就绪'); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.bar = QProgressBar(); self.bar.setValue(0); layout.addWidget(self.bar)
        self.stats = QLabel(''); layout.addWidget(self.stats)
        self.pause = QPushButton('暂停当前操作'); self.pause.setEnabled(False)
        self.pause.clicked.connect(self.cancel); layout.addWidget(self.pause)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(110); layout.addWidget(self.log)
        self.refresh()

    def button(self, layout, label, action, primary=False):
        button = QPushButton(label)
        if primary: button.setObjectName('primary')
        button.clicked.connect(lambda: self.run_action(action)); layout.addWidget(button); self.buttons.append(button)

    def refresh(self):
        try:
            root, version = DmmHelper.installed_game()
            self.install_label.setText(f'已安装 · 记录版本 {version}\n{root}')
            value = AccountStore(data_dir()).read()
            self.accounts.clear()
            for account in value['accounts']:
                self.accounts.addItem(f"{account['number']} · {account['name']}", account['number'])
                if account['key'] == value['active']: self.accounts.setCurrentIndex(self.accounts.count()-1)
            self.nodes.clear()
            store = NodeStore()
            if store.path.exists():
                for node in store.read()['nodes']:
                    self.nodes.addItem(node['name'] + (' · 已启用' if node['enabled'] else ' · 已停用'), node['id'])
        except DmmError as error:
            self.status.setText(str(error))

    def cancel(self):
        if self.worker: self.worker.cancelled.set(); self.status.setText('正在暂停并保存下载断点…')

    def run_action(self, action):
        if self.worker and self.worker.isRunning(): return
        try:
            if action == 'game-folder':
                folder = QFileDialog.getExistingDirectory(self, '选择含 gakumas.exe 的游戏目录')
                if not folder: return
                root = Path(folder).resolve()
                if not (root / 'gakumas.exe').is_file(): raise DmmError('所选目录中没有 gakumas.exe')
                with helper_lock(data_dir()):
                    atomic_write(data_dir() / 'installation.json', json.dumps({'root':str(root),'version':'未知'}).encode())
                self.refresh(); self.report('游戏目录已保存'); return
            self.settings = {'proxy': valid_proxy(self.proxy.text().strip()), NODE_ENABLED_KEY: self.mode.currentIndex()==0}
            atomic_write(data_dir()/'settings.json', json.dumps(self.settings, ensure_ascii=False).encode())
            if action == 'settings': self.status.setText('代理设置已保存'); return
            if action == 'node-file':
                path, _ = QFileDialog.getOpenFileName(self, '导入节点 JSON', '', 'JSON (*.json)')
                if not path: return
                file = Path(path)
                if file.stat().st_size > 1048576: raise DmmError('节点文件超过 1 MiB')
                node_text = file.read_text(encoding='utf-8-sig'); action = 'import-node'
            else: node_text = self.node_text.text()
            number = self.accounts.currentData() if action == 'switch-account' else self.number.text().strip()
            if action == 'switch-account' and not number: raise DmmError('请先保存并选择账号')
            self.worker = Worker(action, self.settings, number, self.name.text().strip(), node_text, self)
            self.worker.message.connect(self.report)
            self.worker.download.connect(lambda percent, text: (self.bar.setRange(0,100), self.bar.setValue(percent), self.stats.setText(text)))
            self.worker.finished_ok.connect(self.report); self.worker.failed.connect(self.report)
            self.worker.finished.connect(self.operation_finished)
            for control in self.buttons + [self.mode,self.proxy,self.accounts,self.number,self.name,self.node_text]: control.setEnabled(False)
            self.pause.setEnabled(True); self.bar.setRange(0,0)
            self.status.setText('正在处理…'); self.worker.start()
        except (DmmError, OSError, UnicodeError) as error:
            self.report(str(error) if isinstance(error,DmmError) else '配置文件无法读取')

    def report(self, message):
        self.status.setText(message); self.log.append(message)

    def operation_finished(self):
        for control in self.buttons + [self.mode,self.proxy,self.accounts,self.number,self.name,self.node_text]: control.setEnabled(True)
        self.pause.setEnabled(False); self.bar.setRange(0,100); self.node_text.clear(); self.refresh()
        self.worker.deleteLater(); self.worker = None

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel(); self.status.setText('请等待当前操作停止后再关闭客户端'); event.ignore()
        else: event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet('''QWidget { background:#101827; color:#e4eaf4; font-family:"Microsoft YaHei"; font-size:14px; }
        QLabel#heading { font-size:30px; font-weight:700; } QLabel#subtitle { font-size:24px; font-weight:600; }
        QPushButton { background:#24324a; border:1px solid #344563; border-radius:8px; padding:10px 16px; }
        QPushButton:hover { background:#334868; } QPushButton#primary { background:#466cff; }
        QPushButton:disabled { color:#66748a; } QLineEdit,QComboBox,QTextEdit { background:#182338; border:1px solid #344563; border-radius:6px; padding:8px; }
        QTabWidget::pane { border:1px solid #27374f; border-radius:8px; } QTabBar::tab { padding:12px 24px; background:#182338; }
        QTabBar::tab:selected { background:#304469; } QProgressBar { border:1px solid #344563; border-radius:5px; text-align:center; }
        QProgressBar::chunk { background:#466cff; }''')
    window = Window(); window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
