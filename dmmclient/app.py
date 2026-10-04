"""Independent Qt client: no ok-script imports or automation tasks."""
from __future__ import annotations

import faulthandler
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import requests
from PySide6.QtCore import QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath,
    QPixmap)
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QProgressBar, QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget)
from .backend.accounts import AccountStore
from .backend.browser_proxy import restore_browser_proxy
from .backend.games import (SUPPORTED, GameService, StorePageNeeded, catalog, cover_path,
    installed_record, official_records, records, refresh_catalog, save_cover, store_detail_url)
from .backend.helper import DmmHelper, helper_lock, ensure_game_stopped
from .backend.launcher import browser_login, import_login, login_candidate
from .backend.session import (DmmError, DmmSession, LoginRequired, OperationCancelled,
    atomic_write, data_dir, dmm_url, valid_proxy)
from .backend.xray import NODE_ENABLED_KEY, HUB, NodeStore, operation_proxy, attach_pool


def load_settings():
    path = data_dir() / 'settings.json'
    defaults = {'proxy': 'http://127.0.0.1:10808', NODE_ENABLED_KEY: True}
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding='utf-8')))
    return defaults


def divider():
    line = QFrame(); line.setObjectName('divider'); line.setFrameShape(QFrame.HLine); line.setFixedHeight(1)
    return line


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


def game_grid():
    grid = QListWidget()
    grid.setViewMode(QListWidget.IconMode); grid.setMovement(QListWidget.Static)
    grid.setResizeMode(QListWidget.Adjust); grid.setWrapping(True)
    grid.setGridSize(QSize(258, 294)); grid.setSpacing(10)
    grid.setVerticalScrollMode(QListWidget.ScrollPerPixel)
    grid.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return grid


def log_error(text):
    try:
        with (data_dir() / 'client.log').open('a', encoding='utf-8', errors='replace') as stream:
            stream.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {text}\n')
    except OSError: pass


class Worker(QThread):
    message = Signal(str)
    download = Signal(int, str)
    finished_ok = Signal(str)
    failed = Signal(str)
    open_store = Signal(str)

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
            log_error(f'操作失败 action={self.action} product={self.product or "-"}: {error}')
            self.failed.emit(str(error) if isinstance(error, DmmError) else '网络连接失败，请测试代理或切换节点')
        except Exception as error:
            log_error(f'操作异常 action={self.action} product={self.product or "-"}: {traceback.format_exc()}')
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
                    save_cover(http, product, data_dir()); updated += 1
                except DmmError:
                    continue  # 缺封面静默跳过，后台 CoverWorker 之后还会补
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
            if action == 'update' and not installed_record(self.product):
                action = 'install'  # 未安装过的游戏直接全新安装
            service = GameService(helper.session, self.product, self.message.emit, self.progress)
            try:
                return service.execute(action)
            except StorePageNeeded as error:
                self.open_store.emit(error.url)  # 应用内商店页完成加库（登录一次后持久）
                raise DmmError(str(error)) from None
        raise DmmError('未知操作')


class CoverWorker(QThread):
    """Fetch missing cover images one by one in the background; never blocks actions."""
    cover_ready = Signal(str)

    def __init__(self, products, settings, parent=None):
        super().__init__(parent)
        self.products, self.settings = list(products), dict(settings)

    def run(self):
        try:
            root = data_dir()
            with operation_proxy(self.settings, lambda message: None, lambda: None, root) as proxy:
                with requests.Session() as http:
                    http.trust_env = False
                    http.proxies = {'http': proxy, 'https': proxy}
                    attach_pool(http, proxy)
                    for product in self.products:
                        try:
                            save_cover(http, product, root)
                            self.cover_ready.emit(product)
                        except DmmError:
                            continue
        except Exception:
            pass  # 封面是增强体验；代理不可用时静默跳过，等待下次触发


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.worker = None
        self.cover_worker = None
        self.store_proxy_url = None
        self.buttons = []          # 页面级按钮（账号/网络/商店工具栏）
        self.library_buttons = []  # 车库卡片按钮（随网格重建）
        self.store_buttons = []    # 商店卡片按钮（随网格重建）
        self.library_covers = {}   # product -> [QLabel]，随网格重建
        self.store_covers = {}
        self.cover_tried = set()
        self.setWindowTitle('DMM Light · 独立客户端预览版')
        self.setWindowIcon(app_icon())
        self.resize(1180, 820)
        outer = QWidget(); self.setCentralWidget(outer)
        layout = QVBoxLayout(outer); layout.setContentsMargins(24, 22, 24, 18); layout.setSpacing(12)
        brand = QHBoxLayout()
        column = QVBoxLayout(); column.setSpacing(2)
        heading = QLabel('DMM Light'); heading.setObjectName('heading'); column.addWidget(heading)
        tagline = QLabel('游戏管理中心 · 独立运行，不依赖官方 Player'); tagline.setObjectName('tagline'); column.addWidget(tagline)
        brand.addLayout(column); brand.addStretch()
        layout.addLayout(brand)
        self.tabs = QTabWidget(); self.tabs.setTabPosition(QTabWidget.West); layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self.build_library_page(), '我的车库')
        self.tabs.addTab(self.build_store_page(), '发现游戏')
        self.tabs.addTab(self.build_account_page(), '账号')
        self.tabs.addTab(self.build_network_page(), '网络')

        self.status = QLabel('就绪'); self.status.setObjectName('status'); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.bar = QProgressBar(); self.bar.setValue(0); layout.addWidget(self.bar)
        self.stats = QLabel(''); self.stats.setObjectName('muted'); layout.addWidget(self.stats)
        self.pause = QPushButton('暂停当前操作'); self.pause.setObjectName('pause'); self.pause.setEnabled(False)
        self.pause.clicked.connect(self.cancel); layout.addWidget(self.pause)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(110); layout.addWidget(self.log)
        self.refresh()

    # ---------- 页面构建 ----------

    def build_library_page(self):
        page = QWidget(); box = QVBoxLayout(page); box.setContentsMargins(24, 20, 24, 20); box.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel('我的车库'); title.setObjectName('section'); head.addWidget(title); head.addStretch()
        hint = QLabel('卡片上直接启动或更新；启动前自动检查、下载并校验更新。')
        hint.setObjectName('muted'); head.addWidget(hint)
        box.addLayout(head)
        self.library_grid = game_grid(); box.addWidget(self.library_grid, 1)
        return page

    def build_store_page(self):
        page = QWidget(); box = QVBoxLayout(page); box.setContentsMargins(24, 20, 24, 20); box.setSpacing(10)
        head = QHBoxLayout(); head.setSpacing(10)
        title = QLabel('发现游戏'); title.setObjectName('section'); head.addWidget(title)
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索游戏名…'); self.search.setObjectName('search')
        head.addWidget(self.search, 1)
        self.filter_box = QComboBox()
        self.filter_box.addItems(['全部游戏', '一般游戏', 'Fanza R18', '已安装'])
        head.addWidget(self.filter_box)
        self.button(head, '刷新游戏列表', 'refresh-catalog')
        box.addLayout(head)
        hint = QLabel('免官方 Player 全新安装；仅支持标准下载型游戏，需要专用安装程序 / 模块 / DRM 的会明确拒绝。Fanza 游戏需年龄认证账号，与一般版版本各自独立。')
        hint.setObjectName('muted'); hint.setWordWrap(True); box.addWidget(hint)
        self.store_grid = game_grid(); box.addWidget(self.store_grid, 1)
        self.search.textChanged.connect(lambda _: self.repopulate_store())
        self.filter_box.currentIndexChanged.connect(lambda _: self.repopulate_store())
        return page

    def build_account_page(self):
        page = QWidget(); a = QVBoxLayout(page); a.setContentsMargins(28, 24, 28, 24); a.setSpacing(12)
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
        a.addStretch(); return page

    def build_network_page(self):
        page = QWidget(); n = QVBoxLayout(page); n.setContentsMargins(28, 24, 28, 24); n.setSpacing(12)
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
        n.addStretch(); return page

    # ---------- 卡片 ----------

    def make_card(self, grid, buttons, covers, product, title, state, card_buttons, extra=()):
        card = QWidget(); card.setObjectName('card'); card.setFixedSize(226, 280)
        box = QVBoxLayout(card); box.setContentsMargins(10, 10, 10, 12); box.setSpacing(7)
        cover = QLabel('暂无封面'); cover.setObjectName('cover'); cover.setAlignment(Qt.AlignCenter)
        cover.setFixedSize(204, 96)
        box.addWidget(cover, 0, Qt.AlignHCenter)
        name = QLabel(title); name.setObjectName('cardtitle'); name.setWordWrap(True)
        name.setMaximumHeight(42); box.addWidget(name)
        info = QLabel(state); info.setObjectName('muted'); info.setWordWrap(True); box.addWidget(info)
        box.addStretch()
        row = QHBoxLayout(); row.setSpacing(6); box.addLayout(row)
        for text, action, accent in list(card_buttons) + list(extra):
            button = QPushButton(text); button.setObjectName('mini')
            if accent: button.setProperty('accent', True)
            button.clicked.connect(lambda _, a=action, p=product: self.run_action(a, p))
            row.addWidget(button); buttons.append(button)
        covers.setdefault(product, []).append(cover)
        if cover_path(data_dir(), product).is_file(): self.paint_cover(cover, product)
        return card

    def add_card(self, grid, card):
        item = QListWidgetItem(); item.setSizeHint(QSize(234, 288))
        grid.addItem(item); grid.setItemWidget(item, card)

    def all_covers(self):
        merged = {}
        for source in (self.library_covers, self.store_covers):
            for product, labels in source.items():
                merged.setdefault(product, []).extend(labels)
        return merged

    def known_titles(self):
        titles = {'gakumas': '学园偶像大师'}
        titles.update(dict(SUPPORTED))
        titles.update({item['product']: item['title'] for item in catalog()})
        titles.update({product: value.get('title', product) for product, value in official_records().items()})
        titles.update({product: value.get('title', product) for product, value in records().items()})
        return titles

    def repopulate_library(self):
        self.library_grid.clear(); self.library_buttons = []; self.library_covers = {}
        installed = {**official_records(), **records()}
        try:
            _, version = DmmHelper.installed_game()
            state = f'已安装 · 版本 {version}'
        except DmmError:
            state = '未识别官方安装记录'
        self.add_card(self.library_grid, self.make_card(self.library_grid, self.library_buttons,
            self.library_covers, 'gakumas', '学园偶像大师', state,
            [('启动', 'launch', True), ('检查', 'check', False), ('更新', 'update', False)],
            extra=[('选择游戏目录', 'game-folder', False)]))
        titles = self.known_titles()
        for product, value in sorted(installed.items()):
            if product == 'gakumas': continue  # 学园偶像大师固定为第一张卡片
            title = titles.get(product, product)
            origin = '官方 Player 安装 · ' if product not in records() else ''
            self.add_card(self.library_grid, self.make_card(self.library_grid, self.library_buttons,
                self.library_covers, product, title,
                f"{origin}已安装 · 版本 {value.get('version', '未知')}",
                [('启动', 'game-launch', True), ('检查', 'game-check', False), ('更新', 'game-update', False)]))

    def repopulate_store(self):
        self.store_grid.clear(); self.store_buttons = []; self.store_covers = {}
        installed = {**official_records(), **records()}
        titles = self.known_titles()
        adult = {item['product'] for item in catalog() if item.get('adult')}
        query = self.search.text().strip().casefold()
        mode = self.filter_box.currentIndex()
        for product, title in sorted(titles.items()):
            if product == 'gakumas': continue  # 学园偶像大师固定在车库页
            if query and query not in title.casefold(): continue
            if mode == 1 and product in adult: continue
            if mode == 2 and product not in adult: continue
            if mode == 3 and product not in installed: continue
            record = installed.get(product)
            badge = 'Fanza R18 · ' if product in adult else ''
            if record:
                state, buttons = badge + f"已安装 · 版本 {record.get('version', '未知')}", \
                    [('启动', 'game-launch', True), ('更新', 'game-update', False), ('详情', 'detail-open', False)]
            else:
                state, buttons = badge + '未安装 · 可全新安装', [('全新安装', 'game-update', True), ('详情', 'detail-open', False)]
            self.add_card(self.store_grid, self.make_card(self.store_grid, self.store_buttons,
                self.store_covers, product, title, state, buttons))

    # ---------- 刷新与封面 ----------

    def refresh(self):
        try:
            self.repopulate_library(); self.repopulate_store()
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
        except Exception:
            self.report('界面数据加载失败，详情见 client.log'); raise
        self.kick_covers()

    def paint_cover(self, label, product):
        path = cover_path(data_dir(), product) if product else None
        pixmap = QPixmap(str(path)) if path and path.is_file() else QPixmap()
        if not pixmap.isNull():
            scaled = pixmap.scaled(label.width() - 4, label.height() - 4,
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
            label.setText(''); label.setPixmap(rounded_pixmap(scaled))
        else:
            label.setPixmap(QPixmap()); label.setText('暂无封面\n后台自动下载')

    def kick_covers(self):
        if not self.isVisible() or (self.cover_worker and self.cover_worker.isRunning()): return
        products = self.all_covers()
        missing = sorted(product for product in products
                         if product not in self.cover_tried and not cover_path(data_dir(), product).is_file())
        if not missing: return
        for product in missing: self.cover_tried.add(product)
        self.cover_worker = CoverWorker(missing, self.settings)
        self.cover_worker.cover_ready.connect(self.cover_arrived)
        self.cover_worker.start()

    def cover_arrived(self, product):
        for label in self.all_covers().get(product, []): self.paint_cover(label, product)

    # ---------- 原生详情页 ----------

    def open_game_detail(self, product):
        installed = installed_record(product)
        entry = next((item for item in catalog() if item['product'] == product), {})
        title = self.known_titles().get(product, product)
        dialog = QDialog(self); dialog.setWindowTitle(title)
        dialog.resize(620, 340); dialog.setAttribute(Qt.WA_DeleteOnClose)
        row = QHBoxLayout(dialog); row.setContentsMargins(20, 20, 20, 16); row.setSpacing(16)
        cover = QLabel('暂无封面'); cover.setObjectName('cover'); cover.setAlignment(Qt.AlignCenter)
        cover.setFixedSize(220, 220)
        path = cover_path(data_dir(), product)
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                cover.setText(''); cover.setPixmap(rounded_pixmap(
                    pixmap.scaled(216, 216, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
        row.addWidget(cover)
        column = QVBoxLayout(); column.setSpacing(8); row.addLayout(column, 1)
        name = QLabel(title); name.setObjectName('detailtitle'); name.setWordWrap(True); column.addWidget(name)
        meta = [str(entry.get(key)) for key in ('maker', 'genre') if entry.get(key)]
        if entry.get('adult'): meta.append('Fanza R18')
        info = QLabel(' · '.join(meta) or product); info.setObjectName('muted'); info.setWordWrap(True); column.addWidget(info)
        if installed:
            state = f"已安装 · 版本 {installed.get('version', '未知')}\n{installed.get('root', '')}"
        else:
            state = '未安装 ·「全新安装」会把游戏下载到本客户端数据目录'
        state_label = QLabel(state); state_label.setObjectName('muted'); state_label.setWordWrap(True); column.addWidget(state_label)
        column.addStretch()
        buttons = QHBoxLayout(); buttons.setSpacing(8); column.addLayout(buttons)
        spec = ([('启动', 'game-launch', True), ('更新', 'game-update', False)] if installed
                else [('全新安装', 'game-update', True)])
        for text, action, accent in spec + [('检查', 'game-check', False), ('网页商店', 'store-open', False)]:
            button = QPushButton(text); button.setObjectName('mini')
            if accent: button.setProperty('accent', True)
            button.clicked.connect(lambda _, a=action, p=product: self.run_action(a, p))
            buttons.addWidget(button)
        dialog.show()
        self.report(f'已打开详情：{title}')

    # ---------- 应用内商店页 ----------

    def showEvent(self, event):
        super().showEvent(event)
        self.kick_covers()

    # ---------- 应用内商店页（内嵌浏览器，走软件代理，登录全在应用内） ----------

    def ensure_store_proxy(self):
        """ProxyHub 常驻代理：with 退出不再杀进程，端口跨操作稳定（Chromium 代理只设置一次）。"""
        if self.store_proxy_url is not None: return self.store_proxy_url
        with operation_proxy(self.settings, lambda m: self.report(m), lambda: None, data_dir()) as proxy:
            self.store_proxy_url = proxy
        return self.store_proxy_url

    def open_store_page(self, url):
        # 顺序很重要：先起代理并写 Chromium 启动参数，再导入 QtWebEngine（导入时初始化 Chromium）
        try:
            proxy = self.ensure_store_proxy()
        except DmmError as error:
            self.report(f'商店页代理启动失败：{error}'); return
        flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
        if 'proxy-server' not in flags:
            os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = (flags + f' --proxy-server={proxy}').strip()
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            from PySide6.QtWebEngineCore import QWebEngineProfile
        except ImportError:
            self.report('缺少 QtWebEngine 组件，无法打开应用内商店页'); return
        # cookie 全部持久化：应用内登录一次后，商店页自动保持登录（与官方 Player 同机制）
        profile = QWebEngineProfile.defaultProfile()
        profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        dialog = QDialog(self); dialog.setWindowTitle('DMM 商店页 · 应用内浏览（走软件代理）')
        dialog.resize(1080, 760); dialog.setAttribute(Qt.WA_DeleteOnClose)
        box = QVBoxLayout(dialog); bar = QHBoxLayout(); bar.setSpacing(8)
        back = QPushButton('← 返回'); bar.addWidget(back)
        reload_button = QPushButton('刷新'); bar.addWidget(reload_button)
        login = QPushButton('登录 DMM'); bar.addWidget(login)
        bar.addStretch()
        close = QPushButton('关闭'); bar.addWidget(close)
        box.addLayout(bar)
        view = QWebEngineView(dialog); box.addWidget(view, 1)
        back.clicked.connect(view.back); reload_button.clicked.connect(view.reload)
        # 应用内 OAuth 登录：网页 cookie 留在内嵌浏览器（商店页此后自动登录），
        # 授权码同时换发 API token——一次登录，商店与下载两边都通。
        login.clicked.connect(lambda: self.start_store_login(view))
        close.clicked.connect(dialog.close)
        dialog.show()
        self._current_store_url = url
        view.load(QUrl(url))
        self.report('已打开应用内商店页；首次使用请点「登录 DMM」，登录一次后自动保持')

    def start_store_login(self, view):
        """在应用内浏览器里走 DMM 官方登录：网页 cookie 留在内嵌浏览器（商店页此后自动
        保持登录），登录成功的授权码同时换发 API token（下载与启动同步可用）。"""
        try:
            proxy = self.ensure_store_proxy()
            session = DmmSession(proxy)
            data = session.request('/v5/auth/login/url', {'prompt': ''})
            login_url = dmm_url(data['url'])
        except DmmError as error:
            self.report(f'获取登录页失败：{error}'); return
        state = {'done': False}
        def on_url_changed(qurl):
            code = DmmSession._code(qurl.toString())
            if not code or state['done']: return
            state['done'] = True
            try:
                session._exchange(code)
                self.report('DMM 登录成功：商店页与游戏下载均已验证')
                view.load(QUrl(self._current_store_url or 'https://games.dmm.co.jp/'))
            except DmmError as error:
                self.report(f'登录交换失败：{error}')
        view.urlChanged.connect(on_url_changed)
        view.load(QUrl(login_url))
        self.report('请在页面内完成 DMM 登录；登录一次，商店页与下载都会记住')

    def showEvent(self, event):
        super().showEvent(event)
        self.kick_covers()

    # ---------- 操作 ----------

    def button(self, layout, label, action, primary=False):
        button = QPushButton(label)
        if primary: button.setObjectName('primary')
        button.clicked.connect(lambda: self.run_action(action)); layout.addWidget(button); self.buttons.append(button)

    def cancel(self):
        if self.worker: self.worker.cancelled.set(); self.status.setText('正在暂停并保存下载断点…')

    def run_action(self, action, product=''):
        if self.worker and self.worker.isRunning():
            self.report('已有操作正在进行，请等它结束或点「暂停当前操作」'); return
        try:
            if action == 'store-open':
                self.open_store_page(store_detail_url(product)); return
            if action == 'detail-open':
                self.open_game_detail(product); return
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
            if action.startswith('game-') and not product: raise DmmError('请先选择游戏')
            self.worker = Worker(action, self.settings, number, self.name.text().strip(), node_text, product, self)
            self.worker.message.connect(self.report)
            self.worker.download.connect(lambda percent, text: (self.bar.setRange(0,100), self.bar.setValue(percent), self.stats.setText(text)))
            self.worker.finished_ok.connect(self.report); self.worker.failed.connect(self.report)
            self.worker.open_store.connect(self.open_store_page)
            self.worker.finished.connect(self.operation_finished)
            self.pause.setEnabled(True); self.bar.setRange(0,0)
            self.status.setText('正在处理…'); self.worker.start()
        except (DmmError, OSError, UnicodeError) as error:
            self.report(str(error) if isinstance(error,DmmError) else '配置文件无法读取')
        except Exception:
            self.report('操作未能开始，详情见 client.log'); raise

    def report(self, message):
        self.status.setText(message); self.log.append(message)

    def set_busy(self, busy):
        """兼容保留：操作期间不再禁用全局控件，仅由 run_action 提示占用。"""
        self.pause.setEnabled(not busy)

    def operation_finished(self):
        self.pause.setEnabled(False); self.bar.setRange(0,100); self.node_text.clear(); self.refresh()
        self.worker.deleteLater(); self.worker = None

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel(); self.status.setText('请等待当前操作停止后再关闭客户端'); event.ignore()
        else:
            if self.cover_worker and self.cover_worker.isRunning(): self.cover_worker.wait(3000)
            HUB.shutdown()
            event.accept()


STYLE = '''QWidget { background:#0b101b; color:#dfe7f3; font-family:"Microsoft YaHei UI"; font-size:14px; }
    QLabel { background:transparent; }
    QLabel#heading { font-size:30px; font-weight:800; color:#ffffff; letter-spacing:1px; }
    QLabel#tagline { font-size:13px; color:#8fa1bd; }
    QLabel#section { font-size:17px; font-weight:700; color:#9db8ff; }
    QLabel#muted { color:#8fa1bd; }
    QLabel#status { font-size:15px; font-weight:700; color:#eaf1fb; }
    QLabel#cardtitle { font-size:13px; font-weight:700; color:#eaf1fb; }
    QLabel#detailtitle { font-size:19px; font-weight:800; color:#ffffff; }
    QLabel#cover { background:#0e1420; border:1px solid #24304a; border-radius:10px; color:#5a6780; font-size:12px; font-weight:600; }
    QTabWidget::pane { border:1px solid #1c2740; border-radius:14px; background:#111827; left:-1px; }
    QTabBar { background:transparent; }
    QTabBar::tab { background:transparent; color:#8fa1bd; font-weight:600; padding:16px 14px;
        min-width:78px; border:1px solid transparent; border-radius:10px; margin-bottom:6px; }
    QTabBar::tab:selected { background:#1c2841; color:#ffffff; border-color:#27375a; }
    QTabBar::tab:hover:!selected { color:#c4d2ea; }
    QListWidget { background:transparent; border:none; }
    QListWidget::item { border:none; background:transparent; }
    QWidget#card { background:#0e1420; border:1px solid #1c2740; border-radius:12px; }
    QPushButton { background:#1a2334; border:1px solid #26334c; border-radius:10px; padding:10px 18px; color:#dfe7f3; font-weight:600; }
    QPushButton:hover { background:#22304a; border-color:#33456a; }
    QPushButton:pressed { background:#182135; }
    QPushButton:disabled { background:#141c2b; border-color:#1f2a40; color:#5a6780; }
    QPushButton#mini { padding:7px 6px; border-radius:8px; font-size:12px; }
    QPushButton[accent="true"] { border:none; color:#ffffff;
        background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #4d7dff, stop:1 #7a5cff); }
    QPushButton[accent="true"]:hover { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #6290ff, stop:1 #8f74ff); }
    QPushButton[accent="true"]:pressed { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #3f6ae6, stop:1 #6a4de0); }
    QPushButton[accent="true"]:disabled { background:#2a3550; color:#7286a8; }
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
    QFrame#divider { border:none; background:#1c2740; max-height:1px; }
    QScrollBar:vertical { background:transparent; width:10px; margin:2px; }
    QScrollBar::handle:vertical { background:#24304a; border-radius:5px; min-height:30px; }
    QScrollBar::handle:vertical:hover { background:#33456a; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }'''


def setup_logging():
    """pythonw has no console; any uncaught error must land in client.log."""
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    stream = (root / 'client.log').open('a', buffering=1, encoding='utf-8', errors='replace')
    stream.write(f'\n--- 启动 {time.strftime("%Y-%m-%d %H:%M:%S")} ---\n')
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = stream
    faulthandler.enable(stream)
    sys.excepthook = lambda t, v, tb: stream.write(''.join(traceback.format_exception(t, v, tb)))
    threading.excepthook = lambda args: stream.write(
        ''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    return stream


def main():
    log = setup_logging()
    try:
        app = QApplication(sys.argv)
        app.setStyleSheet(STYLE)
        window = Window(); window.show()
        return app.exec()
    except BaseException:
        log.write(traceback.format_exc())
        raise


if __name__ == '__main__':
    raise SystemExit(main())
