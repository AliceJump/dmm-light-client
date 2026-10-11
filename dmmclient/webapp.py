"""Webview 前端：QWebEngineView 渲染 HTML UI，后端逻辑与原生版完全共用。

数据流：Python 侧把状态打包成 JSON 快照经 QWebChannel 推给页面（evt 信号）；
页面上的按钮调 bridge.action(JSON) 回来，全部复用 app.py 的 Worker 线程体系。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QVBoxLayout)

from PySide6.QtWebChannel import QWebChannel   # noqa: E402
# QWebEngineView 的导入会初始化 Chromium，必须等 bootstrap_proxy 写好
# QTWEBENGINE_CHROMIUM_FLAGS 之后再导入 —— 延迟到 run_webview 里做。

from .app import (AccountWorker, CoverWorker, DetailWorker, ShotWorker, VersionWorker, Worker,
    app_icon, enable_system_proxy, log_error, restore_system_proxy, system_proxy_active)
from .backend.accounts import AccountStore
from .backend.games import (SUPPORTED, catalog, cover_path, installed_record, records,
    official_records, load_shot_pairs)
from .backend.helper import DmmHelper, helper_lock
from .backend.session import DmmError, atomic_write, data_dir, valid_proxy
from .backend.settings import (default_install_base, game_install_base, game_install_root,
    global_install_base, install_bases, load_settings, remember_install_base, save_settings)
from .backend.xray import HUB, NODE_ENABLED_KEY, NodeStore, operation_proxy
from .theme import system_theme

WEBUI_DIR = Path(__file__).parent / 'webui'


def _cover_uri(product, require_file):
    path = cover_path(data_dir(), product) if product else None
    if not path: return ''
    if require_file and not path.is_file(): return ''
    return path.as_uri()


def cover_uri(product):
    return _cover_uri(product, True)


def cover_base(product):
    """封面规范地址：文件还没下载也先给前端（covers 事件到达后按它补图）。"""
    return _cover_uri(product, False)


def uri_of(path):
    return Path(path).as_uri() if path else ''


def _id_stem(product):
    """去掉可执行后缀：'aigisr_exe' -> ('aigisr', '_exe')，'tskx' -> ('tskx', '')。"""
    for suf in ('_exe', 'exe'):
        if product.endswith(suf) and len(product) > len(suf):
            return product[:-len(suf)], suf
    return product, ''


def variant_other(product):
    """普通版/R18 版互查（纯 id 规则）：R18 是普通版 id 加 r/x（或 _r/_x）尾巴。
    返回另一版本的 id，不存在或自身不在目录时返回 None。"""
    entries = {item['product']: item for item in catalog()}
    if product not in entries: return None
    adult = bool(entries[product].get('adult'))
    stem, suf = _id_stem(product)
    candidates = []
    if adult:      # R18 -> 普通：剥掉 r/x 尾
        for tail in ('_r', '_x', '-r', '-x', 'r', 'x'):
            if stem.endswith(tail):
                candidates.append(stem[:-len(tail)] + suf)
    else:          # 普通 -> R18：尝试加尾
        for tail in ('_r', '_x', '-r', '-x', 'r', 'x'):
            candidates.append(stem + tail + suf)
    for cand in candidates:
        if cand in entries and bool(entries[cand].get('adult')) != adult:
            return cand
    return None


def bootstrap_proxy(settings):
    """在 Chromium 初始化之前把代理定下来（QTWEBENGINE_CHROMIUM_FLAGS 只在导入前有效）。"""
    flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    if 'proxy-server' in flags:
        return settings
    proxy = ''
    if settings.get(NODE_ENABLED_KEY, True):
        try:
            with operation_proxy(settings, lambda m: None, lambda: None, data_dir()) as url:
                proxy = str(url)
        except Exception:
            proxy = ''
    else:
        proxy = settings.get('proxy', '')
    if not proxy:
        return settings
    flags = (flags + f' --proxy-server={proxy} --touch-events=enabled').strip()
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = flags
    return settings


class Bridge(QObject):
    """页面唯一入口：槽收 JSON，事件经 evt 信号推回。"""
    evt = Signal(str)

    def __init__(self, window):
        super().__init__(window)
        self.win = window

    def send(self, **payload):
        self.evt.emit(json.dumps(payload, ensure_ascii=False, default=str))

    # ---- 页面主动调用 ----

    @Slot()
    def init(self):
        self.win.push_snapshot()

    @Slot(str)
    def action(self, payload):
        try:
            self.win.handle_action(json.loads(payload))
        except Exception:
            log_error(f'前端操作异常: {traceback.format_exc()}')
            self.send(type='status', message='操作未能开始，详情见 client.log')

    @Slot(str)
    def log(self, message):
        log_error(str(message))


class WebWindow(QMainWindow):
    PAGE_TITLES = (('我的车库', '已安装的游戏'), ('发现游戏', '可安装的游戏'),
                   ('账号', '登录态与账号快照'), ('网络', '代理与节点'))

    def __init__(self):
        from PySide6.QtWebEngineWidgets import QWebEngineView
        super().__init__()
        self.settings = load_settings()
        self.worker = None
        self.pending_actions = deque()
        self.versions = {}
        self.version_worker = None
        self.version_checked_at = 0.0
        self.account_worker = None
        self.detail_worker = None
        self.detail_loading = None
        self.detail_pending = None
        self.shot_worker = None
        self.detail_data = {}
        self.detail_shots = {}
        self.cover_worker = None
        self.cover_tried = set()
        self.store_proxy_url = None
        self.detail_product = None
        self.theme = system_theme()
        self.setWindowTitle('DMM Light · 独立客户端')
        self.setWindowIcon(app_icon())
        self.resize(1280, 860)

        self.view = QWebEngineView(self)
        channel = QWebChannel(self.view)
        self.bridge = Bridge(self)
        channel.registerObject('bridge', self.bridge)
        self.view.page().setWebChannel(channel)
        self.view.load(QUrl.fromLocalFile(str(WEBUI_DIR / 'index.html')))
        self.setCentralWidget(self.view)

    # ---------- 状态快照 ----------

    def send(self, **payload):
        self.bridge.send(**payload)

    def report(self, message):
        self.send(type='status', message=str(message))

    def known_titles(self):
        titles = {'gakumas': '学园偶像大师'}
        titles.update(dict(SUPPORTED))
        titles.update({item['product']: item['title'] for item in catalog()})
        titles.update({product: value.get('title', product) for product, value in official_records().items()})
        titles.update({product: value.get('title', product) for product, value in records().items()})
        titles['gakumas'] = '学园偶像大师'
        return titles

    @staticmethod
    def installed_version(product, fallback):
        if product == 'gakumas':
            try: return DmmHelper.installed_game()[1]
            except DmmError: pass
        return fallback

    def version_note(self, product):
        info = self.versions.get(product)
        if info and info.get('pending'):
            return f" · 可更新到 {info['latest']}"
        return ''

    def card_badges(self, state):
        if '未安装' in state: badges = [{'t': '未安装', 'k': 'new'}]
        elif '可更新' in state: badges = [{'t': '可更新', 'k': 'up'}]
        else: badges = [{'t': '已安装', 'k': 'ok'}]
        if 'R18' in state: badges.append({'t': 'R18', 'k': 'r18'})
        return badges

    def library_cards(self):
        installed = {**official_records(), **records()}
        helper_root = None
        try:
            helper_root, version = DmmHelper.installed_game()
            state = f'已安装 · 版本 {version}'
        except DmmError:
            state = '未识别官方安装记录'
        if not helper_root:
            try: helper_root = json.loads((data_dir() / 'installation.json').read_text(encoding='utf-8')).get('root')
            except Exception: helper_root = None
        gakumas_buttons = [{'t': '启动', 'a': 'launch', 'pri': True},
                           {'t': '检查', 'a': 'check'}, {'t': '更新', 'a': 'update'}]
        if helper_root and Path(helper_root).exists():
            gakumas_buttons.append({'t': '打开目录', 'a': 'open-dir'})
        else:
            gakumas_buttons.append({'t': '选择游戏目录', 'a': 'game-folder'})
        cards = [{'product': 'gakumas', 'title': '学园偶像大师', 'state': state,
                  'badges': self.card_badges(state), 'cover': cover_uri('gakumas'),
                  'coverBase': cover_base('gakumas'), 'buttons': gakumas_buttons}]
        titles = self.known_titles()
        for product, value in sorted(installed.items()):
            if product == 'gakumas': continue
            managed = product in records()
            origin = '' if managed else '官方 Player 安装 · '
            buttons = [{'t': '启动', 'a': 'game-launch', 'pri': True},
                       {'t': '检查', 'a': 'game-check'}, {'t': '更新', 'a': 'game-update'},
                       {'t': '打开目录', 'a': 'open-dir'}]
            if managed:
                buttons += [{'t': '移动', 'a': 'game-dir'}, {'t': '卸载', 'a': 'game-uninstall', 'danger': True}]
            state = f"{origin}已安装 · 版本 {value.get('version', '未知')}{self.version_note(product)}"
            cards.append({'product': product, 'title': titles.get(product, product), 'state': state,
                          'badges': self.card_badges(state), 'cover': cover_uri(product),
                          'coverBase': cover_base(product), 'buttons': buttons})
        return cards

    def store_cards(self):
        installed = {**official_records(), **records()}
        titles = self.known_titles()
        adult = {item['product'] for item in catalog() if item.get('adult')}
        cards = []
        for product, title in sorted(titles.items()):
            if product == 'gakumas': continue
            record = installed.get(product)
            badge = 'Fanza R18 · ' if product in adult else ''
            buttons = []
            if record:
                buttons = [{'t': '启动', 'a': 'game-launch', 'pri': True}, {'t': '更新', 'a': 'game-update'},
                           {'t': '打开目录', 'a': 'open-dir'}]
                if product in records():
                    buttons += [{'t': '移动', 'a': 'game-dir'}, {'t': '卸载', 'a': 'game-uninstall', 'danger': True}]
                buttons.append({'t': '详情', 'a': 'detail-open'})
                state = badge + f"已安装 · 版本 {record.get('version', '未知')}{self.version_note(product)}"
            else:
                state = badge + '未安装 · 可全新安装'
                buttons = [{'t': '全新安装', 'a': 'game-update', 'pri': True}, {'t': '详情', 'a': 'detail-open'}]
            cards.append({'product': product, 'title': title, 'state': state, 'adult': product in adult,
                          'badges': self.card_badges(state), 'cover': cover_uri(product),
                          'coverBase': cover_base(product), 'buttons': buttons})
        return cards

    def hero_data(self):
        installed = {**official_records(), **records()}
        product = self.settings.get('last_launched')
        if product not in installed:
            product = 'gakumas' if 'gakumas' in installed else (sorted(installed)[0] if installed else None)
        if product is None:
            return None
        value = installed[product]
        version = self.installed_version(product, value.get('version', '未知'))
        buttons = ([{'t': '启动游戏', 'a': 'launch', 'pri': True}, {'t': '检查更新', 'a': 'check'},
                    {'t': '选择目录', 'a': 'game-folder'}] if product == 'gakumas' else
                   [{'t': '启动游戏', 'a': 'game-launch', 'pri': True}, {'t': '检查更新', 'a': 'game-check'},
                    {'t': '打开目录', 'a': 'open-dir'}])
        return {'product': product, 'title': self.known_titles().get(product, product),
                'meta': f"已安装 · 版本 {version}{self.version_note(product)}",
                'cover': cover_uri(product), 'coverBase': cover_base(product), 'buttons': buttons}

    def account_data(self):
        value = AccountStore(data_dir()).read()
        active = next((a for a in value['accounts'] if a['key'] == value['active']), None)
        if active:
            state = f"当前登录：{active['name']}（{active['number']}）"
        elif (data_dir() / 'session.dpapi').exists():
            state = '检测到已有登录态但还没保存成账号，正在自动建档…'
        else:
            state = '当前未登录：请用「浏览器登录 / 续期」或「导入官方客户端登录态」'
        storage = (f'数据目录：{data_dir()}\n'
                   f'登录态文件：{"已存在" if (data_dir() / "session.dpapi").exists() else "不存在"}\n'
                   f'已保存账号：{len(value["accounts"])} 个')
        return {'list': [{'number': a['number'], 'name': a['name'], 'active': a['key'] == value['active']}
                         for a in value['accounts']],
                'number': active['number'] if active else '',
                'name': active['name'] if active else '',
                'chip': f"账号 {active['number']} · {active['name']}" if active else '未登录',
                'loggedIn': bool(active),
                'state': state, 'storage': storage}

    def node_data(self):
        store = NodeStore()
        nodes = store.read()['nodes'] if store.path.exists() else []
        enabled = sum(1 for node in nodes if node['enabled'])
        return {'list': [{'id': node['id'], 'name': node['name'], 'enabled': node['enabled']} for node in nodes],
                'enabled': enabled,
                'state': f'生效代理：{self.settings.get("proxy", "")}\n可用节点：{enabled} 个',
                'mode': 0 if self.settings.get(NODE_ENABLED_KEY, True) else 1,
                'proxy': self.settings.get('proxy', ''),
                'sysproxy': system_proxy_active(),
                'installBase': str(self.settings.get('install_base', '') or ''),
                'defaultBase': str(default_install_base()),
                'askInstall': bool(self.settings.get('ask_install_dir', True))}

    def push_snapshot(self):
        try:
            account = self.account_data()
            network = self.node_data()
            self.send(type='snapshot', theme=self.theme,
                      hero=self.hero_data(), library=self.library_cards(), store=self.store_cards(),
                      installedProducts=list({**official_records(), **records()}),
                      accountChip=account['chip'], loggedIn=account['loggedIn'],
                      nodeChip=f"{network['enabled']} 个节点可用" if network['enabled'] else '没有可用节点',
                      nodeReady=bool(network['enabled']),
                      account=account, network=network, busy=bool(self.worker and self.worker.isRunning()))
        except Exception:
            log_error(f'快照生成失败: {traceback.format_exc()}')
            self.report('界面数据加载失败，详情见 client.log')

    # ---------- 详情 ----------

    def detail_row(self, product):
        record = records().get(product) or official_records().get(product)
        meta = '未安装'
        if record:
            meta = f"已安装 · 版本 {self.installed_version(product, record.get('version', '未知'))}"
        return {'product': product, 'title': self.known_titles().get(product, product),
                'meta': meta, 'cover': cover_uri(product)}

    def detail_products(self):
        products = sorted(self.known_titles())
        if self.detail_product and self.detail_product not in products:
            products.insert(0, self.detail_product)
        return products

    def push_detail(self, product, open_view):
        entry = next((item for item in catalog() if item['product'] == product), {})
        record = records().get(product) or official_records().get(product)
        data = self.detail_data.get(product) or {}
        if record:
            state_line = f"已安装 · 版本 {self.installed_version(product, record.get('version', '未知'))}{self.version_note(product)}"
            buttons = [{'t': '启动', 'a': 'game-launch', 'pri': True}, {'t': '检查更新', 'a': 'game-check'},
                       {'t': '更新', 'a': 'game-update'}, {'t': '打开目录', 'a': 'open-dir'}]
            if product in records():
                buttons += [{'t': '移动', 'a': 'game-dir'}, {'t': '卸载', 'a': 'game-uninstall', 'danger': True}]
            note = '版本由后台清单校验自动纠正；游戏自己更新过也会显示正确版本。'
        else:
            state_line = '未安装'
            buttons = [{'t': '全新安装', 'a': 'game-update', 'pri': True}]
            note = '「全新安装」直接从 DMM CDN 下载到本客户端目录，不需要官方 Player。'
        other = variant_other(product)
        if other:
            other_adult = bool(next((item for item in catalog() if item['product'] == other), {}).get('adult'))
            buttons.append({'t': '前往 R18 版' if other_adult else '前往普通版', 'a': 'open-variant'})
        self.send(type='detail', product=product, open=open_view,
                  title=self.known_titles().get(product, product), cover=cover_uri(product),
                  coverBase=cover_base(product),
                  chips=[str(c) for c in (entry.get('genre'), entry.get('maker')) if c]
                        + (['FANZA R18'] if entry.get('adult') else []) if (entry.get('genre') or entry.get('maker') or entry.get('adult')) else [],
                  stateLine=state_line, root=str(record.get('root', '')) if record else '',
                  buttons=buttons,
                  list=[self.detail_row(p) for p in self.detail_products()],
                  shots=[{'t': uri_of(s.get('t')), 'f': uri_of(s.get('f') or s.get('t'))}
                         for s in (self.detail_shots.get(product) or [])
                         if isinstance(s, dict) and s.get('t')],
                  description=data.get('description', ''), info=data.get('info') or [],
                  specs=data.get('specs') or [], captions=data.get('captions') or [],
                  links=data.get('links') or [], loading=self.detail_loading == product and not data,
                  note=note)

    def open_detail(self, product):
        if not product: return
        self.detail_product = product
        self.push_detail(product, True)
        self.kick_detail(product)
        self.report(f'已打开详情：{self.known_titles().get(product, product)}')

    def select_game(self, product):
        if product == self.detail_product: return
        self.detail_product = product
        self.push_detail(product, True)
        self.kick_detail(product)
        self.report(f'已打开详情：{self.known_titles().get(product, product)}')

    def close_detail(self):
        self.detail_product = None
        self.push_snapshot()

    def kick_detail(self, product):
        if product in self.detail_data: return
        cached = data_dir() / 'details' / f'{product}.json'
        if cached.is_file():
            try:
                self.detail_data[product] = json.loads(cached.read_text(encoding='utf-8'))
                self.detail_shots[product] = load_shot_pairs(product)
                if any(pair['f'] == pair['t'] for pair in self.detail_shots[product]):
                    self.kick_shot_upgrade(product)   # 老缓存没有大图，后台补抓
                self.push_detail(product, True)
                return
            except Exception:
                pass
        if self.detail_worker and self.detail_worker.isRunning():
            self.detail_pending = product; return
        entry = next((item for item in catalog() if item['product'] == product), {})
        self.detail_loading = product
        self.detail_worker = DetailWorker(self.settings, product, bool(entry.get('adult')))
        self.detail_worker.ready.connect(self.detail_ready)
        self.detail_worker.start()
        self.push_detail(product, True)

    def detail_ready(self, product, payload):
        # 不能清掉 self.detail_worker：信号队列投递期间丢引用会让 QThread 被 GC（Qt abort）
        self.detail_loading = None
        if payload.get('detail'): self.detail_data[product] = payload['detail']
        if payload.get('shots'): self.detail_shots[product] = payload['shots']
        if product == self.detail_product: self.push_detail(product, True)
        pending = self.detail_pending; self.detail_pending = None
        if pending and pending not in self.detail_data: self.kick_detail(pending)

    def kick_shot_upgrade(self, product):
        """老缓存只有缩略图：后台补抓大图，完成后刷新详情。"""
        if self.shot_worker and self.shot_worker.isRunning(): return
        images = (self.detail_data.get(product) or {}).get('images') or []
        if not any(image.get('full') for image in images): return
        self.shot_worker = ShotWorker(self.settings, product, images)
        self.shot_worker.ready.connect(self.shots_ready)
        self.shot_worker.start()

    def shots_ready(self, product, shots):
        if shots: self.detail_shots[product] = shots
        if product == self.detail_product: self.push_detail(product, True)

    # ---------- 后台补齐 ----------

    def kick_covers(self):
        if self.cover_worker and self.cover_worker.isRunning(): return
        products = sorted(set(self.known_titles()))
        missing = [product for product in products
                   if product not in self.cover_tried and not cover_path(data_dir(), product).is_file()]
        if not missing: return
        for product in missing: self.cover_tried.add(product)
        self.cover_worker = CoverWorker(missing, self.settings)
        self.cover_worker.cover_ready.connect(self.cover_arrived)
        self.cover_worker.start()

    def cover_arrived(self, product):
        self.send(type='covers', products=[product])

    def kick_version_check(self):
        if self.version_worker and self.version_worker.isRunning(): return
        if self.worker and self.worker.isRunning(): return
        if time.monotonic() - self.version_checked_at < 60: return
        targets = [(product, value['root']) for product, value in
                   {**official_records(), **records()}.items() if value.get('root')]
        if not targets: return
        self.version_checked_at = time.monotonic()
        self.version_worker = VersionWorker(self.settings, targets)
        self.version_worker.verified.connect(self.version_arrived)
        self.version_worker.start()

    def version_arrived(self, product, info):
        self.versions[product] = info
        self.push_snapshot()

    def kick_account_check(self):
        if self.account_worker and self.account_worker.isRunning(): return
        self.account_worker = AccountWorker(self.settings)
        self.account_worker.ready.connect(self.account_ready)
        self.account_worker.start()

    def account_ready(self, payload):
        profile = payload.get('profile') or {}
        if not payload.get('registered'): return
        self.report(f"已自动保存账号：{profile.get('name')}（{profile.get('number')}）")
        self.push_snapshot()

    def worker_settings(self):
        settings = dict(self.settings)          # 保留 install_base 等非代理键
        settings.update({'proxy': valid_proxy(self.settings.get('proxy', '')),
                         NODE_ENABLED_KEY: bool(self.settings.get(NODE_ENABLED_KEY, True))})
        self.settings = settings
        return self.settings

    # ---------- 应用内商店页 ----------

    def ensure_store_proxy(self):
        if self.store_proxy_url is not None: return self.store_proxy_url
        with operation_proxy(self.settings, lambda m: self.report(m), lambda: None, data_dir()) as proxy:
            self.store_proxy_url = proxy
        return self.store_proxy_url

    def open_store_page(self, url):
        try:
            self.ensure_store_proxy()
        except DmmError as error:
            self.report(f'商店页代理启动失败：{error}'); return
        dialog = QDialog(self)
        dialog.setWindowTitle('DMM 商店页 · 应用内浏览（走软件代理）')
        dialog.resize(1080, 760); dialog.setAttribute(QDialog.Attribute.WA_DeleteOnClose)
        box = QVBoxLayout(dialog)
        view = QWebEngineView(dialog)
        box.addWidget(view, 1)
        dialog.show()
        view.load(QUrl(url))
        self.report('已在应用内打开商店页')

    # ---------- 操作分发 ----------

    def handle_action(self, payload):
        action = payload.get('action', '')
        product = str(payload.get('product', '') or '')
        if action == 'refresh': self.version_checked_at = 0.0; self.kick_version_check(); self.push_snapshot(); self.report('已刷新'); return
        if action == 'refresh-catalog': self.run_action('refresh-catalog'); return
        if action == 'open-detail': self.open_detail(payload.get('product', '')); return
        if action == 'open-variant':
            other = variant_other(product)
            if other: self.open_detail(other)
            return
        if action == 'select-game': self.select_game(payload.get('product', '')); return
        if action == 'detail-open': self.open_detail(product); return
        if action == '__close_detail' or action == 'close-detail': self.close_detail(); return
        if action == 'cancel': self.cancel(); return
        if action == 'open-url': self.open_store_page(payload.get('url', '')); return
        if action == 'system-proxy': self.toggle_system_proxy(payload.get('on')); return
        if action == 'settings':
            self.settings.update({'proxy': valid_proxy(str(payload.get('proxy', '')).strip()),
                                  NODE_ENABLED_KEY: bool(payload.get('mode', True))})
            base = str(payload.get('install_base', '') or '').strip()
            if base:
                self.settings['install_base'] = str(Path(base).expanduser().resolve())
            else:
                self.settings.pop('install_base', None)
            self.settings['ask_install_dir'] = bool(payload.get('ask', True))
            atomic_write(data_dir() / 'settings.json', json.dumps(self.settings, ensure_ascii=False).encode())
            self.report('设置已保存'); return
        if action == 'browse-base':
            chosen = QFileDialog.getExistingDirectory(self, '选择默认游戏库（游戏会装到其下的子目录）',
                                                      str(global_install_base(self.settings)))
            if chosen:
                path = str(Path(chosen).resolve()).replace('\\', '\\\\')
                self.view.page().runJavaScript(
                    f"var el=document.getElementById('defaultbase'); if(el) el.value='{path}'; 1")
            return
        if action == 'save-account':
            self.run_action('save-account', number=str(payload.get('number', '')).strip(),
                            name=str(payload.get('name', '')).strip()); return
        if action == 'new-account':
            self.run_action('new-account', number=str(payload.get('number', '')).strip(),
                            name=str(payload.get('name', '')).strip()); return
        if action == 'switch-account':
            number = str(payload.get('number', '')).strip()
            if not number: raise DmmError('请先保存并选择账号')
            self.run_action('switch-account', number=number); return
        if action == 'import-node':
            self.run_action('import-node', node_text=str(payload.get('text', ''))); return
        if action == 'node-file':
            path, _ = QFileDialog.getOpenFileName(self, '导入节点 JSON', '', 'JSON (*.json)')
            if not path: return
            file = Path(path)
            if file.stat().st_size > 1048576: raise DmmError('节点文件超过 1 MiB')
            self.run_action('import-node', node_text=file.read_text(encoding='utf-8-sig')); return
        if action == 'game-folder':
            folder = QFileDialog.getExistingDirectory(self, '选择含 gakumas.exe 的游戏目录')
            if not folder: return
            root = Path(folder).resolve()
            if not (root / 'gakumas.exe').is_file(): raise DmmError('所选目录中没有 gakumas.exe')
            with helper_lock(data_dir()):
                atomic_write(data_dir() / 'installation.json', json.dumps({'root': str(root), 'version': '未知'}).encode())
            self.push_snapshot(); self.report('游戏目录已保存'); return
        if action == 'open-dir':
            self.open_game_folder(product); return
        if action == 'game-dir':
            self.choose_game_directory(product); return
        if action == 'game-uninstall':
            choice = self.confirm_uninstall(product)
            if choice is None: return
            self.dispatch('game-uninstall', product, choice); return
        if action in {'launch', 'game-launch'}: self.remember_launch(product)
        if action.startswith('game-') and not product: raise DmmError('请先选择游戏')
        extra = ''
        if action == 'game-update' and product and not installed_record(product):
            location = self.ensure_install_location(product)
            if location is None: return
            extra = str(location)
        self.dispatch(action, product, extra)

    def run_action(self, action, number='', name='', node_text=''):
        self.enqueue_or_start((action, dict(self.worker_settings()), number, name, node_text, '', ''))

    def dispatch(self, action, product='', extra=''):
        self.enqueue_or_start((action, dict(self.worker_settings()), '', '', '', product, extra))

    def enqueue_or_start(self, request):
        if self.worker and self.worker.isRunning():
            self.pending_actions.append(request)
            self.report(f'已排入队列（{len(self.pending_actions)} 个待执行），当前操作结束后自动开始')
            return
        self.start_operation(request)

    def start_operation(self, request):
        action, settings, number, name, node_text, product, extra = request
        if action == 'refresh-catalog': self.send(type='busy', show=True, text='正在刷新游戏列表…')
        self.worker = Worker(action, settings, number, name, node_text, product, extra, self)
        self.worker.message.connect(self.report)
        self.worker.download.connect(lambda percent, text: self.send(type='progress', active=True, percent=percent, text=text))
        self.worker.finished_ok.connect(self.report)
        self.worker.failed.connect(self.report)
        self.worker.finished.connect(self.operation_finished)
        self.send(type='progress', active=True, percent=-1, text='正在处理…')
        self.send(type='busy', show=False)
        self.worker.start()

    def cancel(self):
        dropped = len(self.pending_actions)
        self.pending_actions.clear()
        if self.worker:
            self.worker.cancelled.set()
            self.report('正在暂停并保存下载断点…' + (f'；队列中 {dropped} 个操作也已取消' if dropped else ''))

    def operation_finished(self):
        finished, self.worker = self.worker, None
        if finished is not None: finished.deleteLater()
        if self.pending_actions:
            self.start_operation(self.pending_actions.popleft())
            return
        self.send(type='progress', active=False, percent=0, text='')
        self.send(type='busy', show=False)
        self.push_snapshot()
        self.kick_covers(); self.kick_version_check()

    # ---------- 目录 / 卸载 / 安装位置 ----------

    def pick_install_base(self, product):
        title = self.known_titles().get(product, product)
        start = str(game_install_base(product))
        Path(start).mkdir(parents=True, exist_ok=True)
        chosen = QFileDialog.getExistingDirectory(
            self, f'选择「{title}」的安装位置（会在其中创建 {product} 子目录）', start)
        return Path(chosen).resolve() if chosen else None

    def pick_install_base_with_option(self, product):
        """安装时的位置选择：可勾「不再提示」把这里设为默认游戏库（之后可在设置改）。"""
        title = self.known_titles().get(product, product)
        start = str(global_install_base(self.settings))
        Path(start).mkdir(parents=True, exist_ok=True)
        dialog = QDialog(self)
        dialog.setWindowTitle(f'选择「{title}」的安装位置')
        box = QVBoxLayout(dialog)
        edit = QLineEdit(start); box.addWidget(edit)
        browse = QPushButton('浏览…')
        browse.clicked.connect(lambda: edit.setText(
            QFileDialog.getExistingDirectory(dialog, '选择安装位置', edit.text() or start) or edit.text()))
        box.addWidget(browse)
        hint = QLabel(f'游戏会安装到所选目录下的 {product} 子目录。')
        box.addWidget(hint)
        remember = QCheckBox('不再提示：以后都装到这个目录（可在网络页的「游戏安装」里修改）')
        box.addWidget(remember)
        buttons = QHBoxLayout(); buttons.addStretch()
        ok = QPushButton('开始安装'); ok.setDefault(True)
        cancel = QPushButton('取消')
        buttons.addWidget(cancel); buttons.addWidget(ok); box.addLayout(buttons)
        ok.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        text = edit.text().strip()
        if not text: return None
        base = Path(text).expanduser().resolve()
        if remember.isChecked():
            self.settings['install_base'] = str(base)
            self.settings['ask_install_dir'] = False
            save_settings(self.settings)
            self.report(f'默认游戏库已设为 {base}，之后安装不再询问；可在网络页的「游戏安装」里修改')
        return base

    def ensure_install_location(self, product):
        """全新安装的目标目录：该游戏选过就用它；否则按默认库设置（不再提示模式直接用）。"""
        if install_bases().get(product):
            return game_install_root(product)
        if self.settings.get('install_base') and not self.settings.get('ask_install_dir', True):
            base = global_install_base(self.settings)
            remember_install_base(product, base)
            return base / product
        base = self.pick_install_base_with_option(product)
        if base is None: return None
        remember_install_base(product, base)
        return base / product

    def open_game_folder(self, product):
        """用资源管理器打开游戏安装目录（gakumas 走专用记录）。"""
        root = None
        if product == 'gakumas':
            try: root = DmmHelper.installed_game()[0]
            except DmmError: root = None
            if not root:
                try: root = json.loads((data_dir() / 'installation.json').read_text(encoding='utf-8')).get('root')
                except Exception: root = None
        else:
            root = (installed_record(product) or {}).get('root')
        if not root or not Path(root).exists():
            self.report('没有找到该游戏的安装目录'); return
        os.startfile(str(Path(root)))
        self.report(f'已打开目录：{root}')

    def choose_game_directory(self, product):
        title = self.known_titles().get(product, product)
        if records().get(product):
            base = self.pick_install_base(product)
            if base is None: return
            self.dispatch('game-move', product, str(base)); return
        if installed_record(product):
            self.report('该游戏由官方 Player 安装，本客户端不移动它的目录'); return
        base = self.pick_install_base(product)
        if base is None: return
        remember_install_base(product, base)
        self.report(f'「{title}」的安装位置已设为 {base / product}')
        self.push_snapshot()

    def confirm_uninstall(self, product):
        record = records().get(product)
        if not record:
            self.report('该游戏不是由本客户端安装的，无法卸载'); return None
        root = Path(record['root'])
        box = QMessageBox(self)
        box.setWindowTitle('卸载游戏'); box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"要卸载「{self.known_titles().get(product, product)}」吗？")
        box.setInformativeText(f'安装目录：{root}\n\n'
                               '「只移除记录」会保留全部游戏文件，下次启动会重新识别；\n'
                               '「彻底删除」会删除上面这个目录里的所有文件，无法恢复。')
        purge = box.addButton('彻底删除（含文件）', QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton('只移除记录，保留文件', QMessageBox.ButtonRole.ActionRole)
        box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is purge: return 'purge'
        if box.clickedButton() is keep: return 'keep'
        return None

    def remember_launch(self, product):
        if not product or self.settings.get('last_launched') == product: return
        self.settings['last_launched'] = product
        try: atomic_write(data_dir() / 'settings.json', json.dumps(self.settings, ensure_ascii=False).encode())
        except OSError: pass

    def toggle_system_proxy(self, on):
        if on:
            try:
                with operation_proxy(self.settings, lambda m: self.report(m), lambda: None, data_dir()) as proxy:
                    enable_system_proxy(str(proxy).replace('http://', ''))
                self.report('系统代理已开启：本机浏览器现在可访问 DMM 商店；关闭软件或再点一次即还原')
            except DmmError as error:
                self.report(f'系统代理开启失败：{error}')
        else:
            restore_system_proxy(); self.report('系统代理已恢复原设置')
        self.push_snapshot()

    # ---------- 生命周期 ----------

    def apply_theme(self, theme):
        self.theme = theme
        self.send(type='theme', theme=theme)

    def background_workers(self):
        return (self.cover_worker, self.version_worker, self.account_worker, self.detail_worker)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel(); self.report('请等待当前操作停止后再关闭客户端'); event.ignore(); return
        deadline = time.monotonic() + 8
        for worker in self.background_workers():
            if worker is None or not worker.isRunning(): continue
            worker.wait(max(200, int((deadline - time.monotonic()) * 1000)))
        restore_system_proxy()
        HUB.shutdown()
        event.accept()


def run_webview():
    app = QApplication(sys.argv)
    bootstrap_proxy(load_settings())          # 必须在 Chromium 初始化前定好代理
    window = WebWindow()
    window.show()
    window.push_snapshot()
    window.kick_covers(); window.kick_version_check(); window.kick_account_check()
    try:
        app.styleHints().colorSchemeChanged.connect(lambda _: window.apply_theme(system_theme()))
    except AttributeError:
        pass
    return app.exec()
