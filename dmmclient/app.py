"""Independent Qt client: no ok-script imports or automation tasks."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath,
    QPixmap)
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QProgressBar, QPushButton,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from .backend.accounts import AccountStore
from .backend.browser_proxy import restore_browser_proxy
from .backend.games import SUPPORTED, GameService, catalog, cover_path, records, refresh_catalog, save_cover
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


def divider():
    line = QFrame(); line.setObjectName('divider'); line.setFrameShape(QFrame.HLine); line.setFixedHeight(1)
    return line


def cover_label():
    label = QLabel('暂无封面\n选择后自动下载'); label.setObjectName('cover'); label.setAlignment(Qt.AlignCenter)
    label.setFixedSize(340, 150)
    return label


def rounded_pixmap(pixmap, radius=10):
    result = QPixmap(pixmap.size()); result.fill(Qt.transparent)
    painter = QPainter(result); painter.setRenderHint(QPainter.SmoothPixmapTransform)
    path = QPainterPath(); path.addRoundedRect(0, 0, result.width(), result.height(), radius, radius)
    painter.setClipPath(path); painter.drawPixmap(0, 0, pixmap); painter.end()
    return result


def app_icon():
    pixmap = QPixmap(256, 256); pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap); painter.setRenderHint(QPainter.Antialiasing)
    gradient = QLinearGradient(0, 0, 256, 256)
    gradient.setColorAt(0, QColor('#4d7dff')); gradient.setColorAt(1, QColor('#7a5cff'))
    painter.setBrush(gradient); painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(16, 16, 224, 224, 56, 56)
    painter.setPen(Qt.white); font = QFont('Segoe UI', 110); font.setBold(True); painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, 'D'); painter.end()
    return QIcon(pixmap)


class Worker(QThread):
    message = Signal(str)
    download = Signal(int, str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, action, settings, number='', name='', node_text='', product='', parent=None):
        super().__init__(parent)
        self.action, self.settings = action, dict(settings)
        self.number, self.name, self.node_text = number, name, node_text
        self.product = product
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
                    if self.action == 'cover':
                        with requests.Session() as http:
                            http.trust_env = False
                            http.proxies = {'http': proxy, 'https': proxy}
                            attach_pool(http, proxy)
                            save_cover(http, self.product, root, self.message.emit)
                        self.finished_ok.emit('封面已更新'); return
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

    def fetch_covers(self, helper):
        products = {'gakumas'} | set(SUPPORTED) | set(records())
        updated = 0
        with requests.Session() as http:
            http.trust_env = False
            http.proxies = {'http': helper.session.proxy, 'https': helper.session.proxy}
            attach_pool(http, helper.session.proxy)
            for product in sorted(products):
                try:
                    save_cover(http, product, data_dir(), self.message.emit); updated += 1
                except DmmError as error:
                    self.message.emit(f'封面获取失败（{product}）：{error}')
        return updated, len(products)

    def game_action(self, helper):
        if self.action == 'refresh-catalog':
            items = refresh_catalog(helper.session)
            updated, total = self.fetch_covers(helper)
            return f'游戏列表已刷新，共 {len(items)} 款免费游戏；封面已更新 {updated}/{total} 张'
        if self.action == 'check':
            result = helper.check()
            return f"最新版本 {result['latest_version']} · 待更新 {result['pending_files']} 个文件 · {result['download_bytes'] / 1048576:.1f} MiB"
        if self.action == 'update':
            spec = helper.prepare(True)
            return f'版本 {spec.version} 已更新并校验'
        if self.action == 'launch':
            helper.launch(True)
            return '游戏进程已启动；请确认游戏内登录完成'
        if self.action in {'game-check', 'game-update', 'game-launch'}:
            action = self.action.removeprefix('game-')
            if action == 'update' and self.product not in records():
                action = 'install'  # 未安装过的游戏直接全新安装
            service = GameService(helper.session, self.product, self.message.emit, self.progress)
            return service.execute(action)
        raise DmmError('未知操作')


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.worker = None
        self.buttons = []
        self.setWindowTitle('DMM Light · 独立客户端预览版')
        self.setWindowIcon(app_icon())
        self.cover_tried = set()
        self.resize(1040, 800)
        outer = QWidget(); self.setCentralWidget(outer)
        layout = QVBoxLayout(outer); layout.setContentsMargins(32, 26, 32, 22); layout.setSpacing(14)
        brand = QVBoxLayout(); brand.setSpacing(2)
        heading = QLabel('DMM Light'); heading.setObjectName('heading'); brand.addWidget(heading)
        tagline = QLabel('游戏、账号与下载，在一个独立客户端中管理'); tagline.setObjectName('tagline'); brand.addWidget(tagline)
        layout.addLayout(brand)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        game = QWidget(); g = QVBoxLayout(game); g.setContentsMargins(28, 24, 28, 24); g.setSpacing(12)
        title = QLabel('学园偶像大师'); title.setObjectName('section'); g.addWidget(title)
        self.gakumas_cover = cover_label(); g.addWidget(self.gakumas_cover)
        self.install_label = QLabel(); self.install_label.setObjectName('muted'); self.install_label.setWordWrap(True); g.addWidget(self.install_label)
        hint = QLabel('启动前自动检查、下载并校验更新。更新过程不计入游戏启动倒计时。')
        hint.setObjectName('muted'); hint.setWordWrap(True); g.addWidget(hint)
        row = QHBoxLayout(); row.setSpacing(10); g.addLayout(row)
        self.button(row, '启动游戏', 'launch', primary=True)
        self.button(row, '检查更新', 'check'); self.button(row, '更新 / 继续下载', 'update')
        self.button(row, '选择游戏目录', 'game-folder')
        g.addWidget(divider())
        title = QLabel('更多游戏'); title.setObjectName('section'); g.addWidget(title)
        hint = QLabel('无需官方 Player，可直接全新安装到本客户端数据目录。仅支持标准下载型游戏；需要专用安装程序、额外模块或 DRM 授权的游戏会明确提示暂不支持。')
        hint.setObjectName('muted'); hint.setWordWrap(True); g.addWidget(hint)
        self.games = QComboBox(); g.addWidget(self.games)
        self.games_cover = cover_label(); g.addWidget(self.games_cover)
        row = QHBoxLayout(); row.setSpacing(10); g.addLayout(row)
        self.button(row, '刷新游戏列表', 'refresh-catalog')
        self.button(row, '检查更新', 'game-check'); self.button(row, '安装 / 更新', 'game-update')
        self.button(row, '启动', 'game-launch')
        hint = QLabel('Fanza（R18）游戏需要已完成年龄认证的 DMM 账号；同一游戏的一般版和 Fanza 版是不同产品，版本各自独立。')
        hint.setObjectName('muted'); hint.setWordWrap(True); g.addWidget(hint)
        g.addStretch(); self.tabs.addTab(game, '游戏库')

        account = QWidget(); a = QVBoxLayout(account); a.setContentsMargins(28, 24, 28, 24); a.setSpacing(12)
        title = QLabel('账号管理'); title.setObjectName('section'); a.addWidget(title)
        hint = QLabel('登录状态加密保存在本客户端的数据目录；切换前需要关闭游戏。')
        hint.setObjectName('muted'); hint.setWordWrap(True); a.addWidget(hint)
        self.accounts = QComboBox(); a.addWidget(self.accounts)
        form = QFormLayout(); form.setSpacing(10); self.number = QLineEdit(); self.name = QLineEdit()
        form.addRow('账号编号', self.number); form.addRow('显示名称', self.name); a.addLayout(form)
        row = QHBoxLayout(); row.setSpacing(10); a.addLayout(row)
        self.button(row, '登录新账号', 'new-account'); self.button(row, '保存当前账号', 'save-account')
        self.button(row, '切换所选账号', 'switch-account')
        a.addWidget(divider())
        row = QHBoxLayout(); row.setSpacing(10); a.addLayout(row)
        self.button(row, '浏览器登录 / 续期', 'login'); self.button(row, '导入官方客户端登录态', 'import-login')
        hint = QLabel('密码与验证码只在 DMM 官方网页输入。登录成功后复制成功页地址即可返回客户端。')
        hint.setObjectName('muted'); hint.setWordWrap(True); a.addWidget(hint)
        a.addStretch(); self.tabs.addTab(account, '账号')

        network = QWidget(); n = QVBoxLayout(network); n.setContentsMargins(28, 24, 28, 24); n.setSpacing(12)
        title = QLabel('代理与节点'); title.setObjectName('section'); n.addWidget(title)
        self.mode = QComboBox(); self.mode.addItems(['内置 Xray 节点池', '外部代理地址'])
        self.mode.setCurrentIndex(0 if self.settings[NODE_ENABLED_KEY] else 1); n.addWidget(self.mode)
        self.proxy = QLineEdit(self.settings['proxy']); self.proxy.setPlaceholderText('http://127.0.0.1:10808 或 socks5h://127.0.0.1:10808'); n.addWidget(self.proxy)
        hint = QLabel('外部代理也用于首次下载 Xray 内核；已有内核时，内置模式不依赖 v2rayN。')
        hint.setObjectName('muted'); hint.setWordWrap(True); n.addWidget(hint)
        row = QHBoxLayout(); row.setSpacing(10); n.addLayout(row)
        self.button(row, '保存代理设置', 'settings'); self.button(row, '测试 DMM 连接', 'test')
        self.nodes = QComboBox(); n.addWidget(self.nodes)
        self.node_text = QLineEdit(); self.node_text.setEchoMode(QLineEdit.EchoMode.Password)
        self.node_text.setPlaceholderText('粘贴节点分享链接或单节点 Xray JSON，内容隐藏'); n.addWidget(self.node_text)
        row = QHBoxLayout(); row.setSpacing(10); n.addLayout(row)
        self.button(row, '添加节点', 'import-node'); self.button(row, '导入 JSON 文件', 'node-file')
        hint = QLabel('最多 32 个节点，轮换使用，连接失败自动尝试下一节点。\n浏览器登录使用 HTTP 代理或内置节点池；SOCKS5 支持 API、更新和下载。')
        hint.setObjectName('muted'); hint.setWordWrap(True); n.addWidget(hint)
        n.addStretch(); self.tabs.addTab(network, '网络')

        self.status = QLabel('就绪'); self.status.setObjectName('status'); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.bar = QProgressBar(); self.bar.setValue(0); layout.addWidget(self.bar)
        self.stats = QLabel(''); self.stats.setObjectName('muted'); layout.addWidget(self.stats)
        self.pause = QPushButton('暂停当前操作'); self.pause.setObjectName('pause'); self.pause.setEnabled(False)
        self.pause.clicked.connect(self.cancel); layout.addWidget(self.pause)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(120); layout.addWidget(self.log)
        self.games.currentIndexChanged.connect(self.on_game_selected)
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
            self.games.clear()
            installed = records()
            known = dict(SUPPORTED)
            known.update({item['product']: item['title'] for item in catalog()})
            known.update({product: value['title'] for product, value in installed.items()})
            adult = {item['product'] for item in catalog() if item.get('adult')}
            entries = sorted(known.items(), key=lambda item: item[0] not in installed)
            for product, title in entries:
                record = installed.get(product)
                badge = ' · Fanza R18' if product in adult else ''
                label = f"{title}{badge} · 已安装（版本 {record['version']}）" if record else f'{title}{badge} · 未安装'
                self.games.addItem(label, product)
            self.paint_cover(self.gakumas_cover, 'gakumas')
        except DmmError as error:
            self.status.setText(str(error))

    def paint_cover(self, label, product):
        path = cover_path(data_dir(), product) if product else None
        pixmap = QPixmap(str(path)) if path and path.is_file() else QPixmap()
        if not pixmap.isNull():
            scaled = pixmap.scaled(label.width() - 4, label.height() - 4,
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
            label.setText(''); label.setPixmap(rounded_pixmap(scaled))
        else:
            label.setPixmap(QPixmap()); label.setText('暂无封面\n选择后自动下载')

    def on_game_selected(self):
        product = self.games.currentData()
        if not product: return
        self.paint_cover(self.games_cover, product)
        if self.isVisible() and not cover_path(data_dir(), product).is_file() \
                and product not in self.cover_tried and not (self.worker and self.worker.isRunning()):
            self.cover_tried.add(product)
            self.run_action('cover', product)

    def cancel(self):
        if self.worker: self.worker.cancelled.set(); self.status.setText('正在暂停并保存下载断点…')

    def run_action(self, action, product=''):
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
            product = product or (self.games.currentData() or '')
            if action.startswith('game-') and not product: raise DmmError('请先选择游戏或刷新游戏列表')
            self.worker = Worker(action, self.settings, number, self.name.text().strip(), node_text, product, self)
            self.worker.message.connect(self.report)
            self.worker.download.connect(lambda percent, text: (self.bar.setRange(0,100), self.bar.setValue(percent), self.stats.setText(text)))
            self.worker.finished_ok.connect(self.report); self.worker.failed.connect(self.report)
            self.worker.finished.connect(self.operation_finished)
            for control in self.buttons + [self.mode,self.proxy,self.accounts,self.number,self.name,self.node_text,self.games]: control.setEnabled(False)
            self.pause.setEnabled(True); self.bar.setRange(0,0)
            self.status.setText('正在处理…'); self.worker.start()
        except (DmmError, OSError, UnicodeError) as error:
            self.report(str(error) if isinstance(error,DmmError) else '配置文件无法读取')

    def report(self, message):
        self.status.setText(message); self.log.append(message)

    def operation_finished(self):
        for control in self.buttons + [self.mode,self.proxy,self.accounts,self.number,self.name,self.node_text,self.games]: control.setEnabled(True)
        self.pause.setEnabled(False); self.bar.setRange(0,100); self.node_text.clear(); self.refresh()
        self.worker.deleteLater(); self.worker = None

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel(); self.status.setText('请等待当前操作停止后再关闭客户端'); event.ignore()
        else: event.accept()


STYLE = '''QWidget { background:#0b101b; color:#dfe7f3; font-family:"Microsoft YaHei UI"; font-size:14px; }
    QLabel { background:transparent; }
    QLabel#heading { font-size:34px; font-weight:800; color:#ffffff; letter-spacing:1px; }
    QLabel#tagline { font-size:14px; color:#8fa1bd; }
    QLabel#section { font-size:16px; font-weight:700; color:#9db8ff; }
    QLabel#muted { color:#8fa1bd; }
    QLabel#status { font-size:15px; font-weight:700; color:#eaf1fb; }
    QLabel#cover { background:#0e1420; border:1px solid #24304a; border-radius:10px; color:#5a6780; font-weight:600; }
    QTabWidget::pane { border:1px solid #1c2740; border-radius:14px; background:#111827; top:-1px; }
    QTabBar { background:transparent; }
    QTabBar::tab { padding:11px 28px; margin-right:6px; background:transparent; color:#8fa1bd; font-weight:600;
        border:1px solid transparent; border-bottom:none; border-top-left-radius:10px; border-top-right-radius:10px; }
    QTabBar::tab:selected { background:#111827; color:#ffffff; border-color:#1c2740; }
    QTabBar::tab:hover:!selected { color:#c4d2ea; }
    QPushButton { background:#1a2334; border:1px solid #26334c; border-radius:10px; padding:10px 18px; color:#dfe7f3; font-weight:600; }
    QPushButton:hover { background:#22304a; border-color:#33456a; }
    QPushButton:pressed { background:#182135; }
    QPushButton:disabled { background:#141c2b; border-color:#1f2a40; color:#5a6780; }
    QPushButton#primary { border:none; color:#ffffff; padding:11px 22px;
        background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #4d7dff, stop:1 #7a5cff); }
    QPushButton#primary:hover { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #6290ff, stop:1 #8f74ff); }
    QPushButton#primary:pressed { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #3f6ae6, stop:1 #6a4de0); }
    QPushButton#primary:disabled { background:#2a3550; color:#7286a8; }
    QLineEdit, QComboBox { background:#0e1420; border:1px solid #24304a; border-radius:9px; padding:9px 12px;
        color:#e7eef9; selection-background-color:#4d7dff; selection-color:#ffffff; }
    QLineEdit:focus, QComboBox:focus { border:1px solid #5b8cff; background:#101827; }
    QLineEdit:disabled, QComboBox:disabled { color:#5a6780; background:#10151f; border-color:#1c2740; }
    QComboBox::drop-down { border:none; width:28px; }
    QComboBox::down-arrow { image:none; border-left:5px solid transparent; border-right:5px solid transparent;
        border-top:6px solid #8fa1bd; margin-right:12px; }
    QComboBox QAbstractItemView { background:#131b2b; border:1px solid #26334c; border-radius:8px;
        selection-background-color:#233355; selection-color:#ffffff; outline:none; }
    QProgressBar { background:#0e1420; border:1px solid #24304a; border-radius:8px; text-align:center;
        color:#dfe7f3; font-weight:600; min-height:16px; }
    QProgressBar::chunk { border-radius:7px;
        background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #4d7dff, stop:1 #7a5cff); }
    QTextEdit { background:#0d1220; border:1px solid #1f2a40; border-radius:10px; padding:6px;
        color:#a8b8d4; font-family:"Consolas","Microsoft YaHei UI"; font-size:12px; }
    QFrame#divider { border:none; background:#1c2740; max-height:1px; }'''


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    window = Window(); window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
