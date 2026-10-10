"""Independent Qt client: no ok-script imports or automation tasks."""
from __future__ import annotations

import faulthandler
import ctypes
import json
import math
import os
import sys
import threading
import time
import traceback
import winreg
from collections import deque
from pathlib import Path

import requests
from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QPropertyAnimation,
    QRectF, QSize, Qt, QThread, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QMouseEvent, QPainter,
    QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScroller, QScrollerProperties, QScrollArea,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget)
from .backend.accounts import AccountStore
from .backend.browser_proxy import restore_browser_proxy
from .backend.games import (SUPPORTED, GameService, catalog, cover_path, game_root,
    installed_record, move_game, official_record, official_records, record_version, records,
    fetch_shot, refresh_catalog, save_cover, store_detail, uninstall_game,
    verify_version)
from .backend.helper import DmmHelper, helper_lock
from .backend.launcher import browser_login, import_login, login_candidate
from .backend.session import (DmmError, DmmSession, LoginRequired, OperationCancelled,
    atomic_write, data_dir, dmm_url, valid_proxy)
from .backend.settings import (load_settings, remember_install_base, game_install_base,
    game_install_root, install_bases)
from .backend.xray import NODE_ENABLED_KEY, HUB, NodeStore, operation_proxy, attach_pool
from .theme import (DUR_MID, DUR_SLOW, EASE_OUT, EASE_SPRING, build_style, easing,
    palette, system_theme)


def rounded_pixmap(pixmap, radius=16):
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


class TouchTapForwarder(QObject):
    """QScroller 抓走触摸后轻点不再产生鼠标事件，这里把轻点补成一次真实点击。

    注意：QScroller 一旦抓住触点，后续 TouchUpdate/TouchEnd 会直接投递给 QScroller
    对象本身，装在 viewport 上的事件过滤器只还能看到 TouchBegin。因此判定「轻点」
    靠 QScroller 自己的状态机：走完 Pressed -> Inactive 且中途没进过 Dragging 即为轻点。
    """

    def __init__(self, target, scroller):
        super().__init__(target)
        self.press_global = None
        self.dragged = False
        scroller.stateChanged.connect(self._state_changed)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.TouchBegin:
            points = event.points()
            self.press_global = points[0].globalPosition() if len(points) == 1 else None
            self.dragged = False
        return False

    def _state_changed(self, state):
        if state in (QScroller.State.Dragging, QScroller.State.Scrolling):
            self.dragged = True
        elif state == QScroller.State.Inactive:
            if self.press_global is not None and not self.dragged:
                self._click(self.press_global)
            self.press_global = None
            self.dragged = False

    @staticmethod
    def _click(global_pos):
        widget = QApplication.widgetAt(global_pos.toPoint())
        if widget is None:
            return
        local = widget.mapFromGlobal(global_pos.toPoint())
        QApplication.sendEvent(widget, QMouseEvent(
            QEvent.Type.MouseButtonPress, local, global_pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        QApplication.sendEvent(widget, QMouseEvent(
            QEvent.Type.MouseButtonRelease, local, global_pos, Qt.LeftButton, Qt.NoButton, Qt.NoModifier))


def enable_touch_scrolling(widget):
    """开启触摸拖拽滚动（含惯性），Qt 默认只认滚轮与滚动条，触摸屏上拖不动。

    - 在控件上抓取 QScroller 触摸手势；列表类控件要抓在 viewport 上。
    - 卡片等子控件默认不接受触摸事件，事件会冒泡到被抓住手势的父控件。
    - 关掉回弹、加大减速，得到接近手机列表的手感。
    - 再补一个轻点转发器：QScroller 会吞掉触摸，导致卡片上的按钮点不动。
    """
    target = widget.viewport() if hasattr(widget, 'viewport') else widget
    target.setAttribute(Qt.WA_AcceptTouchEvents, True)
    QScroller.ungrabGesture(target)
    QScroller.grabGesture(target, QScroller.ScrollerGestureType.TouchGesture)
    scroller = QScroller.scroller(target)
    properties = scroller.scrollerProperties()
    for metric, value in (
            (QScrollerProperties.ScrollMetric.VerticalOvershootPolicy,
             QScrollerProperties.OvershootPolicy.OvershootAlwaysOff),
            (QScrollerProperties.ScrollMetric.HorizontalOvershootPolicy,
             QScrollerProperties.OvershootPolicy.OvershootAlwaysOff),
            (QScrollerProperties.ScrollMetric.DragVelocitySmoothingFactor, 0.06),
            (QScrollerProperties.ScrollMetric.DecelerationFactor, 0.125),
            (QScrollerProperties.ScrollMetric.MaximumVelocity, 1.2)):
        properties.setScrollMetric(metric, value)
    scroller.setScrollerProperties(properties)
    target.installEventFilter(TouchTapForwarder(target, scroller))
    return scroller


def scrollable_page(page):
    """把整页放进可滚动容器：小窗口 / 触摸屏上内容放不下时可以拖动查看。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setWidget(page)
    enable_touch_scrolling(area)
    return area


def game_grid():
    grid = QListWidget()
    grid.setViewMode(QListWidget.IconMode); grid.setMovement(QListWidget.Static)
    grid.setResizeMode(QListWidget.Adjust); grid.setWrapping(True)
    grid.setGridSize(QSize(198, 358)); grid.setSpacing(0)
    grid.setVerticalScrollMode(QListWidget.ScrollPerPixel)
    grid.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    enable_touch_scrolling(grid)
    return grid


def log_error(text):
    try:
        with (data_dir() / 'client.log').open('a', encoding='utf-8', errors='replace') as stream:
            stream.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {text}\n')
    except OSError: pass


_PROXY_KEY_PATH = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
_PROXY_BACKUP = {}
_PROXY_LOCK = threading.Lock()


def _read_system_proxy():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PROXY_KEY_PATH, 0, winreg.KEY_READ) as key:
            def get(name, default):
                try: return winreg.QueryValueEx(key, name)[0]
                except OSError: return default
            return {'enable': get('ProxyEnable', 0), 'server': get('ProxyServer', '')}
    except OSError:
        return {'enable': 0, 'server': ''}


def _apply_system_proxy(enable, server=''):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PROXY_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'ProxyEnable', 0, winreg.REG_DWORD, 1 if enable else 0)
        if enable and server:
            winreg.SetValueEx(key, 'ProxyServer', 0, winreg.REG_SZ, server)
    wininet = ctypes.WinDLL('wininet')
    wininet.InternetSetOptionW(None, 39, None, 0)   # INTERNET_OPTION_SETTINGS_CHANGED
    wininet.InternetSetOptionW(None, 37, None, 0)   # INTERNET_OPTION_REFRESH


def enable_system_proxy(server):
    """临时开启 Windows 系统代理指向本客户端的常驻节点（备份原设置，可还原）。"""
    with _PROXY_LOCK:
        if 'enable' not in _PROXY_BACKUP:
            _PROXY_BACKUP.update(_read_system_proxy())
        _apply_system_proxy(True, server)


def restore_system_proxy():
    with _PROXY_LOCK:
        if _PROXY_BACKUP:
            _apply_system_proxy(bool(_PROXY_BACKUP.get('enable')), _PROXY_BACKUP.get('server', ''))
            _PROXY_BACKUP.clear()


def system_proxy_active():
    return bool(_PROXY_BACKUP)


class Worker(QThread):
    message = Signal(str)
    download = Signal(int, str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, action, settings, number='', name='', node_text='', product='', extra='', parent=None):
        super().__init__(parent)
        self.action, self.settings = action, dict(settings)
        self.number, self.name, self.node_text = number, name, node_text
        self.product, self.extra = product, extra
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
                if self.action == 'game-move':
                    # 纯本地文件搬运，不需要代理
                    target = move_game(self.product, self.extra, self.progress, self.check_cancel, self.message.emit)
                    self.finished_ok.emit(f'游戏目录已移动到 {target}')
                    return
                if self.action == 'game-uninstall':
                    keep = self.extra == 'keep'
                    result = uninstall_game(self.product, keep, self.progress, self.check_cancel, self.message.emit)
                    self.finished_ok.emit('已移除安装记录，游戏文件保留' if keep else
                                          f"已卸载，删除 {result['files']} 个文件 · "
                                          f"{result['bytes'] / 1048576:.1f} MiB")
                    return
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
                        result = self.auto_save_account(accounts, helper) or '现有 DMM 登录态已导入'
                    elif self.action == 'login':
                        browser_login(helper, self.message.emit)
                        result = self.auto_save_account(accounts, helper) or '登录完成'
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

    def auto_save_account(self, accounts, helper):
        """登录/导入成功后一个账号都没存时，自动建一条 —— 否则账号页永远是空的。"""
        if accounts.read()['accounts']: return ''
        try:
            profile = helper.session.user_profile()
        except Exception:
            return ''
        number = profile['number']; name = profile['name'] or f'账号 {number}'
        accounts.save_current(number, name, new=True)
        return f'已自动保存账号：{name}（{number}）；想改名就填同样编号再点「保存当前账号」'

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
            return service.execute(action, self.extra or None)
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


class VersionWorker(QThread):
    """后台核对已安装游戏的真实版本：本地文件与最新清单一致就以服务器版本为准。

    游戏自己更新（或第三方启动器更新）时本地记录会过期，这里把它纠正回来。
    清单校验大部分文件只比大小，很快；失败就静默跳过，继续用本地记录。
    """
    verified = Signal(str, dict)   # product, {'version','latest','pending'}

    def __init__(self, settings, targets, parent=None):
        super().__init__(parent)
        self.settings, self.targets = dict(settings), list(targets)

    def run(self):
        try:
            with operation_proxy(self.settings, lambda message: None, lambda: None, data_dir()) as proxy:
                session = DmmSession(proxy)
                session.ensure_login()
                for product, root in self.targets:
                    try:
                        result = verify_version(session, product, root)
                    except Exception:
                        continue
                    if result['pending'] == 0 and result['version']:
                        record_version(product, result['version'], root)
                    self.verified.emit(product, {'version': result['version'] or '',
                                                 'latest': result['latest'] or '',
                                                 'pending': result['pending']})
        except Exception:
            pass


def soft_backdrop(pixmap, width, height):
    """把封面缩到极小再放大——廉价的高斯感模糊，用作「继续游戏」大图的背景。"""
    if pixmap.isNull() or width <= 1 or height <= 1:
        return QPixmap()
    filled = pixmap.scaled(width, height, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    tiny = filled.scaled(max(8, width // 26), max(8, height // 26),
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return tiny.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def square_cover(pixmap, size, radius=16):
    """裁成正方形并切圆角：DMM 封面本身就是 1:1，这里只是兜底。"""
    scaled = pixmap.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    left = max(0, (scaled.width() - size) // 2)
    top = max(0, (scaled.height() - size) // 2)
    return rounded_pixmap(scaled.copy(left, top, size, size), radius)


class VeilSweep(QWidget):
    """详情展开时从左扫过一道光——对齐 dash 站的 veilSweep。"""

    def __init__(self, parent, colors, radius=24):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.progress = -1.0; self.radius = radius
        self.tint = QColor(colors['accent'])
        self.setGeometry(parent.rect()); self.raise_(); self.show()
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(760)
        self.animation.setStartValue(0.0); self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(easing(EASE_OUT))
        self.animation.valueChanged.connect(self._step)
        self.animation.finished.connect(self.deleteLater)
        self.animation.start(QVariantAnimation.DeleteWhenStopped)

    def _step(self, value):
        self.progress = float(value); self.update()

    def paintEvent(self, event):
        if self.progress < 0: return
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath(); path.addRoundedRect(0, 0, self.width(), self.height(), self.radius, self.radius)
        painter.setClipPath(path)
        fade = math.sin(math.pi * min(max(self.progress, 0.0), 1.0))   # 两端淡，中间亮
        band = max(self.width() * 0.45, 1)
        centre = -0.25 * self.width() + self.progress * 1.5 * self.width()
        gradient = QLinearGradient(centre - band, 0, centre + band, self.height() * 0.4)
        edge = QColor(self.tint); edge.setAlpha(0)
        core = QColor(self.tint); core.setAlpha(int(86 * fade))
        gradient.setColorAt(0.0, edge); gradient.setColorAt(0.5, core); gradient.setColorAt(1.0, edge)
        painter.fillRect(self.rect(), gradient)
        painter.end()


class HeroBanner(QFrame):
    """车库页顶部的「继续游戏」大图：模糊封面当背景 + 清晰缩略图 + 主按钮。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('hero'); self.setFixedHeight(176)
        self._cover = QPixmap(); self._backdrop = QPixmap()
        self._veil = QColor('#0d0f13'); self._edge = QColor('#1f242d')
        row = QHBoxLayout(self); row.setContentsMargins(26, 0, 26, 0); row.setSpacing(20)
        self.thumb = QLabel(); self.thumb.setObjectName('cover'); self.thumb.setFixedSize(112, 112)
        self.thumb.setAlignment(Qt.AlignCenter); self.thumb.setText('暂无封面')
        row.addWidget(self.thumb, 0, Qt.AlignVCenter)
        column = QVBoxLayout(); column.setSpacing(3)
        self.tag = QLabel(''); self.tag.setObjectName('heroTag')
        self.title = QLabel(''); self.title.setObjectName('heroTitle')
        self.meta = QLabel(''); self.meta.setObjectName('muted')
        self.acts = QHBoxLayout(); self.acts.setSpacing(9); self.acts.addStretch()
        column.addWidget(self.tag); column.addWidget(self.title); column.addWidget(self.meta)
        column.addSpacing(9); column.addLayout(self.acts); column.addStretch()
        row.addLayout(column, 1)

    def set_theme(self, colors):
        self._veil = QColor(colors['surface']); self._edge = QColor(colors['border'])
        self.update()

    def set_game(self, tag, title, meta, pixmap=None):
        self.tag.setText(tag); self.title.setText(title); self.meta.setText(meta)
        self._cover = pixmap if pixmap is not None else QPixmap()
        if self._cover.isNull():
            self.thumb.setPixmap(QPixmap()); self.thumb.setText('暂无封面'); self._backdrop = QPixmap()
        else:
            self.thumb.setText(''); self.thumb.setPixmap(square_cover(self._cover, 112))
        self._rebuild()

    def _rebuild(self):
        self._backdrop = soft_backdrop(self._cover, max(self.width(), 720), self.height())
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event); self._rebuild()

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.SmoothPixmapTransform)
        path = QPainterPath(); path.addRoundedRect(0, 0, self.width(), self.height(), 20, 20)
        painter.setClipPath(path); painter.fillRect(self.rect(), self._veil)
        if not self._backdrop.isNull():
            painter.drawPixmap(0, 0, self._backdrop)
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0.0, self._veil)
        gradient.setColorAt(0.55, QColor(self._veil.red(), self._veil.green(), self._veil.blue(), 208))
        gradient.setColorAt(1.0, QColor(self._veil.red(), self._veil.green(), self._veil.blue(), 96))
        painter.fillRect(self.rect(), gradient)
        painter.setPen(QColor(self._edge)); painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, 20, 20)
        painter.end()


class Spinner(QWidget):
    """转圈：底色圆环 + 一段高亮弧，定时旋转。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.angle = 0
        self.track = QColor('#2b323d'); self.accent = QColor('#5b7cfa')
        self.timer = QTimer(self); self.timer.timeout.connect(self._tick)

    def set_colors(self, track, accent):
        self.track = QColor(track); self.accent = QColor(accent); self.update()

    def start(self):
        if not self.timer.isActive(): self.timer.start(16)

    def stop(self):
        self.timer.stop()

    def hideEvent(self, event):
        super().hideEvent(event); self.stop()

    def showEvent(self, event):
        super().showEvent(event); self.start()

    def _tick(self):
        self.angle = (self.angle + 7) % 360; self.update()

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        pen = QPen(self.track); pen.setWidth(3); pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen); painter.drawArc(rect, 0, 360 * 16)
        pen.setColor(self.accent); painter.setPen(pen)
        painter.drawArc(rect, -self.angle * 16, 110 * 16)
        painter.end()


class BusyOverlay(QWidget):
    """盖在内容上的实时遮罩：半透明底 + 转圈 + 文案，切换/刷新时用。"""

    def __init__(self, parent, colors):
        super().__init__(parent)
        self.setObjectName('busy')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.scrim = QColor(colors['bg']); self.scrim.setAlpha(214)
        box = QVBoxLayout(self); box.setAlignment(Qt.AlignCenter); box.setSpacing(12)
        self.spinner = Spinner(self)
        self.spinner.set_colors(colors['surface3'], colors['accent'])
        self.label = QLabel(''); self.label.setObjectName('busyload')
        self.label.setAlignment(Qt.AlignCenter)
        box.addWidget(self.spinner, 0, Qt.AlignHCenter)
        box.addWidget(self.label)
        self.hide()

    def set_theme(self, colors):
        self.scrim = QColor(colors['bg']); self.scrim.setAlpha(214)
        self.spinner.set_colors(colors['surface3'], colors['accent'])
        self.update()

    def set_text(self, text):
        self.label.setText(text)

    def paintEvent(self, event):
        painter = QPainter(self); painter.fillRect(self.rect(), self.scrim); painter.end()


class CrossFade(QWidget):
    """把切换前的画面盖在新界面上淡出——换肤、换页都靠它做过场。

    直接用 QPainter.setOpacity 画，比 QGraphicsOpacityEffect 快得多，
    淡出期间还会挡住鼠标，避免动画没结束就点到东西。
    """

    def __init__(self, parent, pixmap, duration=220):
        super().__init__(parent)
        self.pixmap = pixmap; self.alpha = 1.0
        self.setGeometry(parent.rect()); self.raise_(); self.show()
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(duration)
        self.animation.setStartValue(1.0); self.animation.setEndValue(0.0)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self._step)
        self.animation.finished.connect(self.deleteLater)
        self.animation.start(QVariantAnimation.DeleteWhenStopped)

    def _step(self, value):
        self.alpha = float(value); self.update()

    def paintEvent(self, event):
        painter = QPainter(self); painter.setOpacity(self.alpha)
        painter.drawPixmap(0, 0, self.pixmap); painter.end()


class AccountWorker(QThread):
    """后台确认当前登录并自动建档。

    登录本身只写"当前会话"，账号列表要手动「保存当前账号」才会有条目 ——
    结果就是登录过的人打开账号页一片空白。这里发现「有登录态但一个账号都没存」
    时，用 /v5/userinfo 的 DMM GAMES ID 与昵称自动建一条。
    """
    ready = Signal(dict)   # {'profile': {...}, 'registered': bool}

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = dict(settings)

    def run(self):
        result = {'profile': None, 'registered': False}
        try:
            store = AccountStore(data_dir())
            if store.read()['accounts'] or not (data_dir() / 'session.dpapi').exists():
                self.ready.emit(result); return
            with operation_proxy(self.settings, lambda message: None, lambda: None, data_dir()) as proxy:
                session = DmmSession(proxy)
                session.ensure_login()
                profile = session.user_profile()
            number = profile['number']
            name = profile['name'] or f'账号 {number}'
            with helper_lock(data_dir()):
                store.save_current(number, name, new=True)
            result.update({'profile': {'number': number, 'name': name}, 'registered': True})
        except Exception:
            pass          # 建档只是补救，失败不影响其他功能
        self.ready.emit(result)


def crop_pixmap(pixmap, width, height, radius=12):
    """按目标比例居中裁切并切圆角，图片不变形也不留黑边。"""
    scaled = pixmap.scaled(width, height, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    left = max(0, (scaled.width() - width) // 2); top = max(0, (scaled.height() - height) // 2)
    return rounded_pixmap(scaled.copy(left, top, width, height), radius)


class DetailWorker(QThread):
    """后台抓商店商品详情（简介 / 截图 / 运行环境）并把图片缓存到本地。

    这是增强：拿不到就只显示本地信息，不阻塞、不报错。
    """
    ready = Signal(str, dict)   # product, {'detail': {...}, 'shots': [路径]}

    def __init__(self, settings, product, adult, parent=None):
        super().__init__(parent)
        self.settings, self.product, self.adult = dict(settings), product, bool(adult)

    def run(self):
        result = {'detail': None, 'shots': []}
        try:
            with operation_proxy(self.settings, lambda message: None, lambda: None, data_dir()) as proxy:
                session = DmmSession(proxy)
                session.ensure_login()
                detail = store_detail(session, self.product, self.adult)
                if detail:
                    folder = data_dir() / 'shots'
                    for index, image in enumerate((detail.get('images') or [])[:8]):
                        target = folder / f"{self.product}-{index}.jpg"
                        try: fetch_shot(session.http, image.get('thumb') or image.get('full'), target)
                        except Exception: continue
                        result['shots'].append(str(target))
                    result['detail'] = detail
        except Exception:
            pass
        self.ready.emit(self.product, result)


class DetailPane(QScrollArea):
    """右侧游戏详情面板：内容由外面填，展开时从 0 宽度长出来并扫一道光。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('detailpane')
        self.setWidgetResizable(True); self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget(); self.body.setObjectName('detail'); self.setWidget(self.body)
        self.box = QVBoxLayout(self.body)
        self.box.setContentsMargins(26, 24, 26, 26); self.box.setSpacing(14)
        enable_touch_scrolling(self)
        self.reveal_animation = None

    def clear(self):
        """清空内容：先 setParent(None) 立刻摘掉，再 deleteLater。

        只 deleteLater 的话旧控件要等事件循环才消失，新内容会叠在旧文字上面。
        """
        while self.box.count():
            item = self.box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None); widget.deleteLater()
            elif item.layout() is not None: self._drop_layout(item.layout())

    @staticmethod
    def _drop_layout(layout):
        while layout.count():
            child = layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None); widget.deleteLater()
            elif child.layout() is not None: DetailPane._drop_layout(child.layout())

    def reveal(self, colors):
        """展开动画：最大宽度 0 → 撑满，同时从左扫过一道光。"""
        self.setMaximumWidth(0)
        animation = QPropertyAnimation(self, b'maximumWidth', self)
        animation.setDuration(DUR_SLOW)
        animation.setStartValue(0); animation.setEndValue(4096)
        animation.setEasingCurve(easing(EASE_OUT))
        animation.finished.connect(lambda: self.setMaximumWidth(16777215))
        self.reveal_animation = animation
        animation.start(QPropertyAnimation.DeleteWhenStopped)
        VeilSweep(self, colors, 24)


class Window(QMainWindow):
    PAGE_TITLES = (('我的车库', '已安装的游戏'), ('发现游戏', '可安装的游戏'),
                   ('账号', '登录态与账号快照'), ('网络', '代理与节点'))

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.worker = None
        self.pending_actions = deque()
        self.versions = {}          # product -> 后台核对出来的版本信息
        self.version_worker = None
        self.version_checked_at = 0.0
        self.account_worker = None
        self.detail_worker = None
        self.detail_loading = None
        self.detail_pending = None
        self.detail_data = {}
        self.detail_shots = {}
        self.cover_worker = None
        self.store_proxy_url = None
        self.buttons = []          # 页面级按钮（账号/网络）
        self.library_buttons = []  # 车库卡片按钮（随网格重建）
        self.store_buttons = []    # 商店卡片按钮（随网格重建）
        self.library_covers = {}   # product -> [QLabel]，随网格重建
        self.store_covers = {}
        self.cover_tried = set()
        self.library_grid = None     # 建页时才有，resizeEvent 可能先到
        self.store_grid = None
        self.busy = None            # 内容区遮罩，按需创建
        self.theme = system_theme()
        self.setWindowTitle('DMM Light · 独立客户端')
        self.setWindowIcon(app_icon())
        self.resize(1240, 840)

        root = QWidget(); self.setCentralWidget(root)
        shell = QHBoxLayout(root); shell.setContentsMargins(0, 0, 0, 0); shell.setSpacing(0)
        shell.addWidget(self.build_sidebar())

        main = QWidget(); column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0); column.setSpacing(0)
        column.addWidget(self.build_topbar())
        self.tabs = QStackedWidget()
        self.tabs.addWidget(self.build_library_page())
        self.tabs.addWidget(self.build_store_page())
        self.tabs.addWidget(self.build_account_page())
        self.tabs.addWidget(self.build_network_page())
        column.addWidget(self.tabs, 1)
        column.addWidget(self.build_statusbar())
        shell.addWidget(main, 1)

        self.apply_theme(self.theme)
        self.show_page(0)
        self.refresh()

    # ---------- 主题 ----------

    def apply_theme(self, theme, animate=True):
        """跟随系统切换明暗；切换时把旧画面盖住淡出，不秒切。"""
        snapshot = self.grab() if (animate and self.isVisible()) else None
        self.theme = theme
        application = QApplication.instance()
        if application is not None: application.setStyleSheet(build_style(theme))
        self.hero.set_theme(palette(theme))
        if self.busy is not None: self.busy.set_theme(palette(theme))
        if snapshot is not None and not snapshot.isNull(): CrossFade(self, snapshot, 260)

    def show_busy(self, text):
        """内容区盖上遮罩（半透明底 + 转圈 + 文案），用于刷新目录这类慢操作。"""
        if self.busy is None: self.busy = BusyOverlay(self.tabs, palette(self.theme))
        self.busy.set_text(text)
        self.busy.setGeometry(self.tabs.rect())
        self.busy.show(); self.busy.raise_()

    def hide_busy(self):
        if self.busy is not None: self.busy.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.busy is not None and self.busy.isVisible():
            self.busy.setGeometry(self.tabs.rect())
        QTimer.singleShot(0, self.fit_grids)

    # ---------- 骨架 ----------

    def build_sidebar(self):
        side = QWidget(); side.setObjectName('sidebar'); side.setFixedWidth(236)
        box = QVBoxLayout(side); box.setContentsMargins(14, 18, 14, 16); box.setSpacing(3)
        brand = QHBoxLayout(); brand.setSpacing(10); brand.setContentsMargins(6, 0, 0, 16)
        mark = QLabel('D'); mark.setObjectName('mark'); mark.setFixedSize(34, 34); mark.setAlignment(Qt.AlignCenter)
        column = QVBoxLayout(); column.setSpacing(0)
        heading = QLabel('DMM Light'); heading.setObjectName('heading'); column.addWidget(heading)
        tagline = QLabel('独立客户端'); tagline.setObjectName('tagline'); column.addWidget(tagline)
        brand.addWidget(mark); brand.addLayout(column); brand.addStretch()
        box.addLayout(brand)
        self.nav_buttons = []
        for index, (title, _) in enumerate(self.PAGE_TITLES):
            button = QPushButton(title); button.setObjectName('nav'); button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _, i=index: self.show_page(i))
            box.addWidget(button); self.nav_buttons.append(button)
        box.addStretch()
        self.account_chip = QLabel('未登录'); self.account_chip.setObjectName('chip')
        self.node_chip = QLabel('节点未就绪'); self.node_chip.setObjectName('chip')
        box.addWidget(self.account_chip); box.addWidget(self.node_chip)
        return side

    def build_topbar(self):
        bar = QWidget(); bar.setObjectName('topbar'); bar.setFixedHeight(64)
        row = QHBoxLayout(bar); row.setContentsMargins(24, 0, 24, 0); row.setSpacing(12)
        self.page_title = QLabel('我的车库'); self.page_title.setObjectName('pagetitle')
        self.page_sub = QLabel(''); self.page_sub.setObjectName('muted')
        row.addWidget(self.page_title); row.addWidget(self.page_sub); row.addStretch()
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索游戏名…')
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(lambda _: self.repopulate_store())
        row.addWidget(self.search)
        self.top_button = QPushButton('刷新'); self.top_button.setCursor(Qt.PointingHandCursor)
        self.top_button.clicked.connect(self.top_action); row.addWidget(self.top_button)
        return bar

    def build_statusbar(self):
        wrap = QWidget(); wrap.setObjectName('statusbar')
        column = QVBoxLayout(wrap); column.setContentsMargins(24, 10, 24, 12); column.setSpacing(8)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setFixedHeight(120)
        self.log.hide(); enable_touch_scrolling(self.log); column.addWidget(self.log)
        row = QHBoxLayout(); row.setSpacing(14)
        self.bar = QProgressBar(); self.bar.setValue(0); self.bar.setTextVisible(False); self.bar.setFixedWidth(300)
        self.bar.hide()   # 空闲时不显示进度条
        self.status = QLabel('就绪'); self.status.setObjectName('status')
        self.stats = QLabel(''); self.stats.setObjectName('muted')
        self.pause = QPushButton('暂停'); self.pause.setObjectName('pause'); self.pause.setEnabled(False)
        self.pause.clicked.connect(self.cancel)
        self.log_button = QPushButton('日志'); self.log_button.setObjectName('pause'); self.log_button.setCheckable(True)
        self.log_button.toggled.connect(lambda on: self.log.setVisible(on))
        row.addWidget(self.bar); row.addWidget(self.status, 1); row.addWidget(self.stats)
        row.addWidget(self.pause); row.addWidget(self.log_button)
        column.addLayout(row)
        return wrap

    @staticmethod
    def section(title, content):
        """把一块内容装进卡片：页面用两列排开，别让控件孤零零挤在左上角。"""
        card = QWidget(); card.setObjectName('card')
        box = QVBoxLayout(card); box.setContentsMargins(18, 16, 18, 18); box.setSpacing(10)
        label = QLabel(title); label.setObjectName('section'); box.addWidget(label)
        box.addWidget(content, 1)
        return card

    def fit_grids(self):
        """按视口宽度平均分列，让卡片墙铺满，右边不留一条空白。"""
        for grid in (self.library_grid, self.store_grid):
            if grid is None: continue
            width = grid.viewport().width()
            if width <= 0: continue
            columns = max(1, width // (self.CARD_WIDTH + 16))
            size = QSize(width // columns, self.CARD_HEIGHT + 12)
            if grid.gridSize() != size: grid.setGridSize(size)   # 相同就别重排，免得来回抖

    def watch_grid(self, grid):
        """盯住视口尺寸：隐藏页的视口宽度不准，等它显示出来要重算一次列数。"""
        grid.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            self.fit_grids()
        elif event.type() == QEvent.Type.MouseButtonRelease and obj.property('product'):
            if obj.rect().contains(event.position().toPoint()):
                self.open_game_detail(obj.property('product')); return True
        return False

    def show_page(self, index, animate=True):
        changed = self.tabs.currentIndex() != index
        snapshot = self.tabs.grab() if (changed and animate and self.isVisible()) else None
        self.tabs.setCurrentIndex(index)
        for position, button in enumerate(self.nav_buttons):
            button.setChecked(position == index)
        title, subtitle = self.PAGE_TITLES[index]
        self.page_title.setText(title); self.page_sub.setText(subtitle)
        self.search.setVisible(index == 1)
        self.top_button.setText('刷新游戏列表' if index == 1 else '刷新')
        QTimer.singleShot(0, self.fit_grids)
        if snapshot is not None and not snapshot.isNull(): CrossFade(self.tabs, snapshot, 180)

    def top_action(self):
        if self.tabs.currentIndex() == 1:
            self.run_action('refresh-catalog'); return
        self.version_checked_at = 0.0; self.kick_version_check(); self.refresh()
        self.report('已刷新')

    # ---------- 页面构建 ----------

    def build_library_page(self):
        page = QWidget(); box = QVBoxLayout(page); box.setContentsMargins(0, 0, 0, 0)
        self.library_stack = QStackedWidget()
        self.library_stack.addWidget(self.build_browse_view())    # 0 浏览：大图 + 卡片墙
        self.library_stack.addWidget(self.build_detail_view())    # 1 详情：左侧列表 + 右侧详情
        box.addWidget(self.library_stack)
        return page

    def build_browse_view(self):
        page = QWidget(); box = QVBoxLayout(page); box.setContentsMargins(24, 20, 24, 20); box.setSpacing(16)
        self.hero = HeroBanner(); box.addWidget(self.hero)
        head = QHBoxLayout(); head.setSpacing(10)
        title = QLabel('全部游戏'); title.setObjectName('section'); head.addWidget(title)
        self.library_count = QLabel(''); self.library_count.setObjectName('muted'); head.addWidget(self.library_count)
        head.addStretch()
        hint = QLabel('点卡片看详情'); hint.setObjectName('muted'); head.addWidget(hint)
        box.addLayout(head)
        self.library_grid = game_grid(); self.watch_grid(self.library_grid)
        box.addWidget(self.library_grid, 1)
        return page

    def build_detail_view(self):
        page = QWidget(); row = QHBoxLayout(page)
        row.setContentsMargins(24, 20, 24, 20); row.setSpacing(18)
        left = QWidget(); left.setObjectName('card'); left.setFixedWidth(286)
        lbox = QVBoxLayout(left); lbox.setContentsMargins(14, 14, 14, 14); lbox.setSpacing(10)
        back = QPushButton('← 返回车库'); back.setObjectName('link')
        back.setCursor(Qt.PointingHandCursor); back.clicked.connect(self.close_detail)
        lbox.addWidget(back, 0, Qt.AlignLeft)
        self.game_list = QListWidget(); self.game_list.setObjectName('gamelist')
        self.game_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.game_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.game_list.setFrameShape(QFrame.NoFrame)
        self.game_list.currentItemChanged.connect(self.row_selected)
        enable_touch_scrolling(self.game_list)
        lbox.addWidget(self.game_list, 1)
        row.addWidget(left)
        self.detail = DetailPane(); row.addWidget(self.detail, 1)
        return page

    def build_store_page(self):
        page = QWidget(); box = QVBoxLayout(page); box.setContentsMargins(24, 20, 24, 20); box.setSpacing(12)
        row = QHBoxLayout(); row.setSpacing(10)
        self.filter_box = QComboBox()
        self.filter_box.addItems(['全部游戏', '一般游戏', 'Fanza R18', '已安装'])
        self.filter_box.setFixedWidth(160); row.addWidget(self.filter_box); row.addStretch()
        self.store_count = QLabel(''); self.store_count.setObjectName('muted'); row.addWidget(self.store_count)
        box.addLayout(row)
        hint = QLabel('免官方 Player 全新安装；仅支持标准下载型游戏，需要专用安装程序或 DRM 的会明确拒绝。个别游戏要先装 VC++ / DirectX 运行库，客户端会自动下载并运行（可能弹 UAC）。Fanza 游戏需年龄认证账号，与一般版版本各自独立。')
        hint.setObjectName('muted'); hint.setWordWrap(True); box.addWidget(hint)
        self.store_grid = game_grid(); self.watch_grid(self.store_grid)
        box.addWidget(self.store_grid, 1)
        self.filter_box.currentIndexChanged.connect(lambda _: self.repopulate_store())
        return page

    def build_account_page(self):
        page = QWidget(); grid = QGridLayout(page)
        grid.setContentsMargins(24, 20, 24, 20); grid.setHorizontalSpacing(18); grid.setVerticalSpacing(18)

        left = QWidget(); a = QVBoxLayout(left); a.setContentsMargins(0, 0, 0, 0); a.setSpacing(10)
        self.account_state = QLabel(''); self.account_state.setObjectName('muted')
        self.account_state.setWordWrap(True); a.addWidget(self.account_state)
        hint = QLabel('密码与验证码只在 DMM 官方网页输入，登录成功后回到客户端即可。')
        hint.setObjectName('muted'); hint.setWordWrap(True); a.addWidget(hint)
        buttons = QHBoxLayout(); buttons.setSpacing(9); a.addLayout(buttons)
        self.button(buttons, '浏览器登录 / 续期', 'login'); self.button(buttons, '导入官方登录态', 'import-login')
        buttons.addStretch()
        buttons = QHBoxLayout(); buttons.setSpacing(9); a.addLayout(buttons)
        self.button(buttons, '登录新账号', 'new-account'); buttons.addStretch()
        a.addStretch()
        grid.addWidget(self.section('当前登录', left), 0, 0, Qt.AlignTop)

        right = QWidget(); b = QVBoxLayout(right); b.setContentsMargins(0, 0, 0, 0); b.setSpacing(10)
        self.accounts = QComboBox(); b.addWidget(self.accounts)
        form = QFormLayout(); form.setSpacing(10); self.number = QLineEdit(); self.name = QLineEdit()
        form.addRow('账号编号', self.number); form.addRow('显示名称', self.name); b.addLayout(form)
        buttons = QHBoxLayout(); buttons.setSpacing(9); b.addLayout(buttons)
        self.button(buttons, '保存当前账号', 'save-account'); self.button(buttons, '切换所选账号', 'switch-account')
        buttons.addStretch()
        hint = QLabel('同一编号再保存一次即为改名；切换只替换登录态文件，正在运行的游戏不受影响。')
        hint.setObjectName('muted'); hint.setWordWrap(True); b.addWidget(hint)
        b.addStretch()
        grid.addWidget(self.section('账号列表', right), 0, 1, Qt.AlignTop)

        self.storage_state = QLabel(''); self.storage_state.setObjectName('muted')
        self.storage_state.setWordWrap(True)
        grid.addWidget(self.section('本地存储', self.storage_state), 1, 0, 1, 2, Qt.AlignTop)
        grid.setRowStretch(2, 1)

        return scrollable_page(page)

    def build_network_page(self):
        page = QWidget(); grid = QGridLayout(page)
        grid.setContentsMargins(24, 20, 24, 20); grid.setHorizontalSpacing(18); grid.setVerticalSpacing(18)

        left = QWidget(); n = QVBoxLayout(left); n.setContentsMargins(0, 0, 0, 0); n.setSpacing(10)
        self.mode = QComboBox(); self.mode.addItems(['内置 Xray 节点池', '外部代理地址'])
        self.mode.setCurrentIndex(0 if self.settings[NODE_ENABLED_KEY] else 1); n.addWidget(self.mode)
        self.proxy = QLineEdit(self.settings['proxy'])
        self.proxy.setPlaceholderText('http://127.0.0.1:10808 或 socks5h://127.0.0.1:10808'); n.addWidget(self.proxy)
        hint = QLabel('外部代理也用于首次下载 Xray 内核；已有内核时，内置模式不依赖 v2rayN。')
        hint.setObjectName('muted'); hint.setWordWrap(True); n.addWidget(hint)
        buttons = QHBoxLayout(); buttons.setSpacing(9); n.addLayout(buttons)
        self.button(buttons, '保存代理设置', 'settings'); self.button(buttons, '测试 DMM 连接', 'test')
        buttons.addStretch()
        self.sysproxy = QPushButton('开启系统代理（给本机浏览器临时用）'); self.sysproxy.setCheckable(True)
        self.sysproxy.toggled.connect(self.toggle_system_proxy); n.addWidget(self.sysproxy)
        hint = QLabel('开启后本机浏览器经本客户端节点访问 DMM（用于商店加库）；关闭软件或再点一次即还原。')
        hint.setObjectName('muted'); hint.setWordWrap(True); n.addWidget(hint)
        n.addStretch()
        grid.addWidget(self.section('代理', left), 0, 0, Qt.AlignTop)

        right = QWidget(); m = QVBoxLayout(right); m.setContentsMargins(0, 0, 0, 0); m.setSpacing(10)
        self.nodes = QComboBox(); m.addWidget(self.nodes)
        self.node_text = QLineEdit(); self.node_text.setEchoMode(QLineEdit.EchoMode.Password)
        self.node_text.setPlaceholderText('粘贴节点分享链接或单节点 Xray JSON，内容隐藏'); m.addWidget(self.node_text)
        buttons = QHBoxLayout(); buttons.setSpacing(9); m.addLayout(buttons)
        self.button(buttons, '添加节点', 'import-node'); self.button(buttons, '导入 JSON 文件', 'node-file')
        buttons.addStretch()
        hint = QLabel('最多 32 个节点，轮换使用，连接失败自动尝试下一节点。\n'
                      '浏览器登录使用 HTTP 代理或内置节点池；SOCKS5 支持 API、更新和下载。')
        hint.setObjectName('muted'); hint.setWordWrap(True); m.addWidget(hint)
        m.addStretch()
        grid.addWidget(self.section('节点', right), 0, 1, Qt.AlignTop)

        self.network_state = QLabel(''); self.network_state.setObjectName('muted')
        self.network_state.setWordWrap(True)
        grid.addWidget(self.section('当前状态', self.network_state), 1, 0, 1, 2, Qt.AlignTop)
        grid.setRowStretch(2, 1)

        return scrollable_page(page)

    # ---------- 卡片 ----------

    CARD_WIDTH = 170
    CARD_HEIGHT = 330
    COVER_SIZE = 150

    def make_card(self, grid, buttons, covers, product, title, state, card_buttons, extra=()):
        card = QWidget(); card.setObjectName('card')
        card.setFixedSize(self.CARD_WIDTH, self.CARD_HEIGHT)
        card.setProperty('product', product)
        card.setCursor(Qt.PointingHandCursor)
        card.installEventFilter(self)
        box = QVBoxLayout(card); box.setContentsMargins(10, 10, 10, 11); box.setSpacing(8)
        cover = QLabel('暂无封面'); cover.setObjectName('cover'); cover.setAlignment(Qt.AlignCenter)
        cover.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        for text, kind, right in self.card_badges(state):
            tag = QLabel(text, cover); tag.setObjectName('badge'); tag.setProperty('kind', kind)
            tag.adjustSize()
            tag.move(cover.width() - tag.width() - 8 if right else 8, 8)
        box.addWidget(cover, 0, Qt.AlignHCenter)
        name = QLabel(title); name.setObjectName('cardtitle'); name.setWordWrap(True)
        name.setFixedHeight(38); box.addWidget(name)
        info = QLabel(state); info.setObjectName('cardmeta'); info.setWordWrap(True)
        info.setFixedHeight(32); box.addWidget(info)
        box.addStretch()
        spec = list(card_buttons) + list(extra)
        primary = next((item for item in spec if item[2]), None)
        if primary is not None:
            text, action, _ = primary
            button = QPushButton(text); button.setProperty('accent', True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _, a=action, p=product: self.run_action(a, p))
            box.addWidget(button); buttons.append(button)
        secondary = [item for item in spec if item is not primary]
        if secondary:
            row = QHBoxLayout(); row.setSpacing(2)
            for text, action, _ in secondary:
                button = QPushButton(text); button.setObjectName('link')
                button.setCursor(Qt.PointingHandCursor)
                if action == 'game-uninstall': button.setProperty('danger', True)
                button.clicked.connect(lambda _, a=action, p=product: self.run_action(a, p))
                row.addWidget(button); buttons.append(button)
            box.addLayout(row)
        covers.setdefault(product, []).append(cover)
        if cover_path(data_dir(), product).is_file(): self.paint_cover(cover, product)
        return card

    def add_card(self, grid, card):
        item = QListWidgetItem()
        item.setSizeHint(QSize(self.CARD_WIDTH + 10, self.CARD_HEIGHT + 10))
        grid.addItem(item); grid.setItemWidget(item, card)

    @staticmethod
    def card_badges(state):
        """卡片状态角标：左上角状态，右上角 R18。"""
        if '未安装' in state: badges = [('未安装', 'new', False)]
        elif '可更新' in state: badges = [('可更新', 'up', False)]
        else: badges = [('已安装', 'ok', False)]
        if 'R18' in state: badges.append(('R18', 'r18', True))
        return badges

    def cover_pixmap(self, product):
        path = cover_path(data_dir(), product) if product else None
        return QPixmap(str(path)) if path and path.is_file() else QPixmap()

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
        titles['gakumas'] = '学园偶像大师'   # 官方记录里的标题就是 product id，别把中文名覆盖掉
        return titles

    @staticmethod
    def installed_version(product, fallback):
        """学园偶像大师的版本以本客户端的 installation.json 为准（官方 dmmgame.cnf 会过期）。"""
        if product == 'gakumas':
            try: return DmmHelper.installed_game()[1]
            except DmmError: pass
        return fallback

    @staticmethod
    def _restore_scroll(grid, offset):
        """重建卡片墙后恢复滚动位置：QListWidget.clear() 会把滚动条归零。

        必须先把新条目的布局算出来（doItemsLayout），否则滚动范围仍是 0，
        setValue 会被夹到顶部。
        """
        if not offset:
            return
        grid.doItemsLayout()
        grid.verticalScrollBar().setValue(min(offset, grid.verticalScrollBar().maximum()))

    def repopulate_library(self, keep_scroll=True):
        offset = self.library_grid.verticalScrollBar().value() if keep_scroll else 0
        self.library_grid.setUpdatesEnabled(False)   # 重建期间别重绘，否则会先闪一下空白
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
            managed = product in records()
            origin = '' if managed else '官方 Player 安装 · '
            buttons = [('启动', 'game-launch', True), ('检查', 'game-check', False), ('更新', 'game-update', False)]
            if managed: buttons += [('目录', 'game-dir', False), ('卸载', 'game-uninstall', False)]
            self.add_card(self.library_grid, self.make_card(self.library_grid, self.library_buttons,
                self.library_covers, product, title,
                f"{origin}已安装 · 版本 {value.get('version', '未知')}{self.version_note(product)}", buttons))
        self._restore_scroll(self.library_grid, offset)
        self.library_grid.setUpdatesEnabled(True)
        self.fit_grids()

    def repopulate_store(self, keep_scroll=False):
        offset = self.store_grid.verticalScrollBar().value() if keep_scroll else 0
        self.store_grid.setUpdatesEnabled(False)
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
                buttons = [('启动', 'game-launch', True), ('更新', 'game-update', False)]
                if product in records(): buttons += [('目录', 'game-dir', False), ('卸载', 'game-uninstall', False)]
                buttons.append(('详情', 'detail-open', False))
                state = badge + f"已安装 · 版本 {record.get('version', '未知')}{self.version_note(product)}"
            else:
                state = badge + '未安装 · 可全新安装'
                buttons = [('全新安装', 'game-update', True), ('目录', 'game-dir', False), ('详情', 'detail-open', False)]
            self.add_card(self.store_grid, self.make_card(self.store_grid, self.store_buttons,
                self.store_covers, product, title, state, buttons))
        self._restore_scroll(self.store_grid, offset)
        self.store_grid.setUpdatesEnabled(True)
        self.store_count.setText(f'{self.store_grid.count()} 款')

    # ---------- 刷新与封面 ----------

    def refresh(self):
        try:
            self.repopulate_library(True); self.repopulate_store(True)
            value = AccountStore(data_dir()).read()
            self.accounts.clear()
            active = None
            for account in value['accounts']:
                self.accounts.addItem(f"{account['number']} · {account['name']}", account['number'])
                if account['key'] == value['active']:
                    self.accounts.setCurrentIndex(self.accounts.count()-1); active = account
            self.account_chip.setText(f"账号 {active['number']} · {active['name']}" if active else '未登录')
            if active:
                self.account_state.setText(f"当前登录：{active['name']}（{active['number']}）")
            elif (data_dir() / 'session.dpapi').exists():
                self.account_state.setText('检测到已有登录态但还没保存成账号，正在自动建档…')
            else:
                self.account_state.setText('当前未登录：请用「浏览器登录 / 续期」或「导入官方客户端登录态」')
            self.nodes.clear()
            store = NodeStore()
            enabled = 0
            if store.path.exists():
                for node in store.read()['nodes']:
                    if node['enabled']: enabled += 1
                    self.nodes.addItem(node['name'] + (' · 已启用' if node['enabled'] else ' · 已停用'), node['id'])
            self.node_chip.setText(f'{enabled} 个节点可用' if enabled else '没有可用节点')
            self.storage_state.setText(
                f'数据目录：{data_dir()}\n'
                f'登录态文件：{"已存在" if (data_dir() / "session.dpapi").exists() else "不存在"}\n'
                f'已保存账号：{len(value["accounts"])} 个')
            self.network_state.setText(
                f'生效代理：{self.settings.get("proxy", "")}\n'
                f'可用节点：{enabled} 个')
        except DmmError as error:
            self.status.setText(str(error))
        except Exception:
            self.report('界面数据加载失败，详情见 client.log'); raise
        try:
            self.sysproxy.blockSignals(True); self.sysproxy.setChecked(system_proxy_active()); self.sysproxy.blockSignals(False)
        except Exception: pass
        self.kick_covers()
        self.kick_version_check()
        self.kick_account_check()
        self.library_count.setText(f'{self.library_grid.count()} 款')
        self.update_hero()

    def update_hero(self):
        """车库页顶部的「继续游戏」：优先上次启动的，否则第一款已安装游戏。"""
        installed = {**official_records(), **records()}
        product = self.settings.get('last_launched')
        if product not in installed:
            product = 'gakumas' if 'gakumas' in installed else (sorted(installed)[0] if installed else None)
        while self.hero.acts.count():
            widget = self.hero.acts.takeAt(0).widget()
            if widget is not None: widget.deleteLater()
        if product is None:
            self.hero.set_game('还没有游戏', '去「发现游戏」里挑一款装上吧', '装好后这里会显示最近启动的游戏')
            self.hero.acts.addStretch(); return
        value = installed[product]
        version = self.installed_version(product, value.get('version', '未知'))
        self.hero.set_game('继续游戏', self.known_titles().get(product, product),
                           f"已安装 · 版本 {version}{self.version_note(product)}",
                           self.cover_pixmap(product))
        spec = ([('启动游戏', 'launch', True), ('检查更新', 'check', False), ('选择目录', 'game-folder', False)]
                if product == 'gakumas' else
                [('启动游戏', 'game-launch', True), ('检查更新', 'game-check', False), ('打开目录', 'game-dir', False)])
        for text, action, accent in spec:
            button = QPushButton(text); button.setCursor(Qt.PointingHandCursor)
            if accent: button.setProperty('accent', True)
            button.clicked.connect(lambda _, a=action, p=product: self.run_action(a, p))
            self.hero.acts.addWidget(button)
        self.hero.acts.addStretch()

    def remember_launch(self, product):
        """记下最近启动的游戏，车库页大图用它。"""
        if not product or self.settings.get('last_launched') == product: return
        self.settings['last_launched'] = product
        try: atomic_write(data_dir() / 'settings.json', json.dumps(self.settings, ensure_ascii=False).encode())
        except OSError: pass

    def version_note(self, product):
        """版本提示：后台核对出有待更新文件时，说明能更新到哪个版本。"""
        info = self.versions.get(product)
        if info and info.get('pending'):
            return f" · 可更新到 {info['latest']}"
        return ''

    def kick_version_check(self):
        """后台核对已安装游戏的真实版本；不打断操作，60 秒内不重复跑。"""
        if self.version_worker and self.version_worker.isRunning(): return
        if self.worker and self.worker.isRunning(): return
        if not self.isVisible(): return
        if time.monotonic() - self.version_checked_at < 60: return
        targets = [(product, value['root']) for product, value in
                   {**official_records(), **records()}.items() if value.get('root')]
        if not targets: return
        self.version_checked_at = time.monotonic()
        self.version_worker = VersionWorker(self.settings, targets)
        self.version_worker.verified.connect(self.version_arrived)
        self.version_worker.start()

    def kick_account_check(self):
        """登录过但一个账号都没存时，后台自动建一条，别让账号页空着。"""
        if self.account_worker and self.account_worker.isRunning(): return
        if not self.isVisible(): return
        self.account_worker = AccountWorker(self.settings)
        self.account_worker.ready.connect(self.account_ready)
        self.account_worker.start()

    def account_ready(self, payload):
        profile = payload.get('profile') or {}
        if not payload.get('registered'): return
        self.report(f"已自动保存账号：{profile.get('name')}（{profile.get('number')}）")
        if not self.number.text(): self.number.setText(str(profile.get('number') or ''))
        if not self.name.text(): self.name.setText(str(profile.get('name') or ''))
        self.refresh()

    def version_arrived(self, product, info):
        self.versions[product] = info
        self.repopulate_library(True); self.repopulate_store(True); self.update_hero()

    def paint_cover(self, label, product):
        pixmap = self.cover_pixmap(product)
        if pixmap.isNull():
            label.setPixmap(QPixmap()); label.setText('暂无封面'); return
        label.setText(''); label.setPixmap(square_cover(pixmap, label.width()))

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

    # ---------- 游戏详情（主从布局） ----------

    def detail_products(self):
        """左侧列表：已安装的游戏，外加正在看的那个（可能还没装）。"""
        products = sorted({**official_records(), **records()})
        current = getattr(self, 'detail_product', None)
        if current and current not in products: products.insert(0, current)
        return products

    def row_meta(self, product):
        record = records().get(product) or official_records().get(product)
        if not record: return '未安装'
        return f"已安装 · 版本 {self.installed_version(product, record.get('version', '未知'))}"

    def make_row(self, product):
        row = QWidget(); row.setObjectName('listrow')
        box = QHBoxLayout(row); box.setContentsMargins(8, 8, 10, 8); box.setSpacing(10)
        thumb = QLabel(); thumb.setObjectName('cover'); thumb.setFixedSize(46, 46)
        thumb.setAlignment(Qt.AlignCenter)
        pixmap = self.cover_pixmap(product)
        if pixmap.isNull(): thumb.setText('—')
        else: thumb.setPixmap(square_cover(pixmap, 46, 12))
        box.addWidget(thumb)
        column = QVBoxLayout(); column.setSpacing(2)
        title = QLabel(self.known_titles().get(product, product)); title.setObjectName('listtitle')
        meta = QLabel(self.row_meta(product)); meta.setObjectName('listmeta')
        column.addWidget(title); column.addWidget(meta)
        box.addLayout(column, 1)
        return row

    def rebuild_game_list(self):
        self.game_list.blockSignals(True)
        self.game_list.clear()
        for product in self.detail_products():
            item = QListWidgetItem(); item.setSizeHint(QSize(250, 62))
            item.setData(Qt.UserRole, product)
            self.game_list.addItem(item)
            self.game_list.setItemWidget(item, self.make_row(product))
        self.game_list.blockSignals(False)

    def mark_active_row(self, product):
        for index in range(self.game_list.count()):
            item = self.game_list.item(index); widget = self.game_list.itemWidget(item)
            if widget is None: continue
            active = item.data(Qt.UserRole) == product
            if widget.property('active') == active: continue
            widget.setProperty('active', active)
            widget.style().unpolish(widget); widget.style().polish(widget)

    def row_selected(self, current, previous):
        if current is None: return
        product = current.data(Qt.UserRole)
        if product and product != getattr(self, 'detail_product', None): self.select_game(product)

    def open_game_detail(self, product):
        """点卡片进详情：左侧游戏变成列表，右侧展开详情。"""
        if not product: return
        if self.tabs.currentIndex() != 0: self.show_page(0, animate=False)
        self.detail_product = product
        self.rebuild_game_list()
        self.fill_detail(product)
        self.mark_active_row(product)
        self.kick_detail(product)
        if self.library_stack.currentIndex() != 1:
            snapshot = self.library_stack.grab()
            self.library_stack.setCurrentIndex(1)
            CrossFade(self.library_stack, snapshot, 420)
            self.detail.reveal(palette(self.theme))       # 展开 + 扫光
        else:
            self.snap_detail()
        self.report(f'已打开详情：{self.known_titles().get(product, product)}')

    def select_game(self, product, animate=True):
        if product == getattr(self, 'detail_product', None): return
        snapshot = self.detail.grab() if (animate and self.detail.isVisible()) else None
        self.detail_product = product
        self.fill_detail(product)
        self.mark_active_row(product)
        self.kick_detail(product)
        if snapshot is not None and not snapshot.isNull(): CrossFade(self.detail, snapshot, 300)
        self.report(f'已打开详情：{self.known_titles().get(product, product)}')

    def snap_detail(self):
        snapshot = self.detail.grab()
        if not snapshot.isNull(): CrossFade(self.detail, snapshot, 300)

    def close_detail(self):
        if self.library_stack.currentIndex() != 1: return
        snapshot = self.library_stack.grab()
        self.library_stack.setCurrentIndex(0)
        CrossFade(self.library_stack, snapshot, 380)

    @staticmethod
    def detail_line(label, value):
        row = QWidget(); box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0); box.setSpacing(10)
        key = QLabel(label); key.setObjectName('muted'); key.setFixedWidth(62)
        text = QLabel(str(value)); text.setObjectName('field'); text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(key, 0, Qt.AlignTop); box.addWidget(text, 1)
        return row

    @staticmethod
    def detail_block(title):
        card = QWidget(); card.setObjectName('card')
        box = QVBoxLayout(card); box.setContentsMargins(18, 16, 18, 18); box.setSpacing(10)
        label = QLabel(title); label.setObjectName('section'); box.addWidget(label)
        return card, box

    def kick_detail(self, product):
        """商店详情是增强：先看本地缓存，没有就后台抓一次。"""
        if product in self.detail_data: return
        cached = data_dir() / 'details' / f'{product}.json'
        if cached.is_file():
            try:
                self.detail_data[product] = json.loads(cached.read_text(encoding='utf-8'))
                folder = data_dir() / 'shots'
                self.detail_shots[product] = [str(path) for path in sorted(folder.glob(f'{product}-*.jpg'))]
                if product == getattr(self, 'detail_product', None): self.snap_fill(product)
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

    def detail_ready(self, product, payload):
        self.detail_loading = None; self.detail_worker = None
        if payload.get('detail'): self.detail_data[product] = payload['detail']
        if payload.get('shots'): self.detail_shots[product] = payload['shots']
        if product == getattr(self, 'detail_product', None): self.snap_fill(product)
        pending = self.detail_pending; self.detail_pending = None
        if pending and pending not in self.detail_data: self.kick_detail(pending)

    def snap_fill(self, product):
        """重画详情内容，并把旧画面盖住淡出，切换游戏时不闪。"""
        snapshot = self.detail.grab()
        self.fill_detail(product)
        if not snapshot.isNull(): CrossFade(self.detail, snapshot, 320)

    def fill_detail(self, product):
        """详情内容全部来自本地目录 + 安装记录，不加载原版网页。"""
        self.detail.clear(); box = self.detail.box
        title = self.known_titles().get(product, product)
        entry = next((item for item in catalog() if item['product'] == product), {})
        record = records().get(product) or official_records().get(product)
        version = self.installed_version(product, (record or {}).get('version', '未知')) if record else ''

        head = QHBoxLayout(); head.setSpacing(20)
        cover = QLabel(); cover.setObjectName('cover'); cover.setFixedSize(176, 176)
        cover.setAlignment(Qt.AlignCenter)
        pixmap = self.cover_pixmap(product)
        if pixmap.isNull(): cover.setText('暂无封面')
        else: cover.setPixmap(square_cover(pixmap, 176, 20))
        head.addWidget(cover, 0, Qt.AlignTop)

        column = QVBoxLayout(); column.setSpacing(9)
        name = QLabel(title); name.setObjectName('detailname'); name.setWordWrap(True)
        column.addWidget(name)
        chips = QHBoxLayout(); chips.setSpacing(6)
        for text in (entry.get('genre'), entry.get('maker'), 'FANZA R18' if entry.get('adult') else None):
            if text:
                chip = QLabel(str(text)); chip.setObjectName('chip'); chips.addWidget(chip)
        chips.addStretch(); column.addLayout(chips)
        column.addWidget(self.detail_line('状态', f'已安装 · 版本 {version}{self.version_note(product)}'
                                          if record else '未安装'))
        if record: column.addWidget(self.detail_line('目录', str(record.get('root', ''))))
        column.addWidget(self.detail_line('编号', product))
        column.addStretch()
        head.addLayout(column, 1)
        box.addLayout(head)

        actions = QHBoxLayout(); actions.setSpacing(9)
        spec = ([('启动', 'game-launch', True), ('检查更新', 'game-check', False), ('更新', 'game-update', False),
                 ('目录', 'game-dir', False), ('卸载', 'game-uninstall', False)] if record else
                [('全新安装', 'game-update', True), ('选择目录', 'game-dir', False)])
        for text, action, accent in spec:
            button = QPushButton(text); button.setCursor(Qt.PointingHandCursor)
            if action == 'game-uninstall':
                button.setObjectName('link'); button.setProperty('danger', True)
            elif accent: button.setProperty('accent', True)
            button.clicked.connect(lambda _, a=action, p=product: self.run_action(a, p))
            actions.addWidget(button)
        actions.addStretch(); box.addLayout(actions)

        data = self.detail_data.get(product) or {}
        shots = self.detail_shots.get(product) or []
        if shots:
            card, inner = self.detail_block('截图')
            row = QHBoxLayout(); row.setSpacing(10)
            for path in shots[:6]:
                shot = QLabel(); shot.setObjectName('cover'); shot.setFixedSize(186, 105)
                shot.setAlignment(Qt.AlignCenter)
                pixmap = QPixmap(path)
                if pixmap.isNull(): shot.setText('—')
                else: shot.setPixmap(crop_pixmap(pixmap, 186, 105, 14))
                row.addWidget(shot)
            row.addStretch(); inner.addLayout(row); box.addWidget(card)
        if data.get('description'):
            card, inner = self.detail_block('简介')
            text = QLabel(data['description']); text.setObjectName('detaildesc')
            text.setWordWrap(True); text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            inner.addWidget(text); box.addWidget(card)
        if data.get('info'):
            card, inner = self.detail_block('游戏信息')
            grid = QGridLayout(); grid.setHorizontalSpacing(22); grid.setVerticalSpacing(7)
            for index, item in enumerate(data['info']):
                key = QLabel(item['key']); key.setObjectName('muted')
                value = QLabel(item['value']); value.setObjectName('field'); value.setWordWrap(True)
                grid.addWidget(key, index // 2, (index % 2) * 2)
                grid.addWidget(value, index // 2, (index % 2) * 2 + 1)
            grid.setColumnStretch(1, 1); grid.setColumnStretch(3, 1)
            inner.addLayout(grid); box.addWidget(card)
        for spec in data.get('specs') or []:
            if not spec.get('html'): continue
            card, inner = self.detail_block(f"运行环境（{spec.get('os') or 'Windows'}）")
            text = QLabel(spec['html']); text.setObjectName('field'); text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            inner.addWidget(text); box.addWidget(card)
        if data.get('captions'):
            card, inner = self.detail_block('注意事项')
            for line in data['captions']:
                item = QLabel('· ' + str(line)); item.setObjectName('muted'); item.setWordWrap(True)
                inner.addWidget(item)
            box.addWidget(card)
        if data.get('links'):
            card, inner = self.detail_block('相关链接')
            row = QHBoxLayout(); row.setSpacing(9)
            for link in data['links'][:4]:
                button = QPushButton(str(link.get('text') or '打开')); button.setCursor(Qt.PointingHandCursor)
                url = str(link.get('url') or '')
                button.clicked.connect(lambda _, u=url: self.open_store_page(u))
                row.addWidget(button)
            row.addStretch(); inner.addLayout(row); box.addWidget(card)
        if not data and self.detail_loading == product:
            loading = QLabel('正在获取商店信息（简介 / 截图 / 运行环境）…')
            loading.setObjectName('muted'); box.addWidget(loading)
        note = QLabel('「全新安装」直接从 DMM CDN 下载到本客户端目录，不需要官方 Player。'
                      if not record else
                      '版本由后台清单校验自动纠正；游戏自己更新过也会显示正确版本。')
        note.setObjectName('muted'); note.setWordWrap(True); box.addWidget(note)
        box.addStretch()

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
            flags = (flags + f' --proxy-server={proxy}').strip()
        if 'touch-events' not in flags:
            flags = (flags + ' --touch-events=enabled').strip()   # 触摸屏上可直接拖动商店页
        os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = flags
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
        if getattr(self, '_store_login_handler', None) is not None:
            try: view.urlChanged.disconnect(self._store_login_handler)
            except (RuntimeError, TypeError): pass
        self._store_login_handler = on_url_changed
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
        dropped = len(self.pending_actions)
        self.pending_actions.clear()
        if self.worker:
            self.worker.cancelled.set()
            tail = f'；队列中 {dropped} 个操作也已取消' if dropped else ''
            self.status.setText('正在暂停并保存下载断点…' + tail)

    def toggle_system_proxy(self, checked):
        if checked:
            try:
                with operation_proxy(self.settings, lambda m: self.report(m), lambda: None, data_dir()) as proxy:
                    enable_system_proxy(str(proxy).replace('http://', ''))
                self.report('系统代理已开启：本机浏览器现在可访问 DMM 商店；关闭软件或再点一次即还原')
            except DmmError as error:
                self.sysproxy.setChecked(False)
                self.report(f'系统代理开启失败：{error}')
        else:
            restore_system_proxy(); self.report('系统代理已恢复原设置')

    def run_action(self, action, product=''):
        try:
            # 打开详情与保存代理设置不与其他操作冲突，任何时候都立即执行
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
            if action == 'game-dir':
                self.choose_game_directory(product); return
            if action == 'game-uninstall':
                choice = self.confirm_uninstall(product)
                if choice is None: return
                self.dispatch('game-uninstall', product, choice); return
            if action in {'launch', 'game-launch'}: self.remember_launch(product)
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
            extra = ''
            if action == 'game-update' and product and not installed_record(product):
                # 全新安装：先确定装到哪里；用户没选过就问一次
                location = self.ensure_install_location(product)
                if location is None: return
                extra = str(location)
            self.enqueue_or_start((action, dict(self.settings), number,
                                   self.name.text().strip(), node_text, product, extra))
        except (DmmError, OSError, UnicodeError) as error:
            self.report(str(error) if isinstance(error,DmmError) else '配置文件无法读取')
        except Exception:
            self.report('操作未能开始，详情见 client.log'); raise

    def enqueue_or_start(self, request):
        """把操作交给 Worker；已有操作在跑就排队，不拒绝。"""
        if self.worker and self.worker.isRunning():
            self.pending_actions.append(request)
            self.report(f'已排入队列（{len(self.pending_actions)} 个待执行），当前操作结束后自动开始')
            return
        self.start_operation(request)

    def confirm_uninstall(self, product):
        """卸载前二次确认：默认「只移除记录」，彻底删除要单独点。"""
        record = records().get(product)
        if not record:
            self.report('该游戏不是由本客户端安装的，无法卸载'); return None
        root = Path(record['root'])
        box = QMessageBox(self)
        box.setWindowTitle('卸载游戏')
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"要卸载「{self.known_titles().get(product, product)}」吗？")
        box.setInformativeText(f'安装目录：{root}\n\n'
                               '「只移除记录」会保留全部游戏文件，下次启动会重新识别；\n'
                               '「彻底删除」会删除上面这个目录里的所有文件，无法恢复。')
        purge = box.addButton('彻底删除（含文件）', QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton('只移除记录，保留文件', QMessageBox.ButtonRole.ActionRole)
        box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is purge: return 'purge'
        if clicked is keep: return 'keep'
        return None

    def pick_install_base(self, product):
        """弹目录选择框，返回用户选的基准目录（游戏会装到 <基准>/<product>）。"""
        title = self.known_titles().get(product, product)
        start = str(game_install_base(product))
        Path(start).mkdir(parents=True, exist_ok=True)
        chosen = QFileDialog.getExistingDirectory(
            self, f'选择「{title}」的安装位置（会在其中创建 {product} 子目录）', start)
        return Path(chosen).resolve() if chosen else None

    def ensure_install_location(self, product):
        """全新安装的目标目录：选过就直接用，没选过问一次。"""
        if install_bases().get(product):
            return game_install_root(product)
        base = self.pick_install_base(product)
        if base is None: return None
        remember_install_base(product, base)
        return base / product

    def choose_game_directory(self, product):
        """卡片上的「目录」：未安装=预设安装位置，本客户端装的=移动过去。"""
        title = self.known_titles().get(product, product)
        if records().get(product):
            base = self.pick_install_base(product)
            if base is None: return
            self.dispatch('game-move', product, str(base))
            return
        if installed_record(product):
            self.report('该游戏由官方 Player 安装，本客户端不移动它的目录')
            return
        base = self.pick_install_base(product)
        if base is None: return
        remember_install_base(product, base)
        self.report(f'「{title}」的安装位置已设为 {base / product}')
        self.refresh()

    def dispatch(self, action, product='', extra=''):
        self.enqueue_or_start((action, dict(self.settings), self.number.text().strip(),
                               self.name.text().strip(), self.node_text.text(), product, extra))

    def start_operation(self, request):
        action, settings, number, name, node_text, product, extra = request
        if action == 'refresh-catalog': self.show_busy('正在刷新游戏列表…')
        self.worker = Worker(action, settings, number, name, node_text, product, extra, self)
        self.worker.message.connect(self.report)
        self.worker.download.connect(lambda percent, text: (self.bar.setRange(0,100), self.bar.setValue(percent), self.stats.setText(text)))
        self.worker.finished_ok.connect(self.report); self.worker.failed.connect(self.report)
        self.worker.finished.connect(self.operation_finished)
        self.pause.setEnabled(True); self.bar.setRange(0,0); self.bar.show()
        self.status.setText('正在处理…'); self.worker.start()

    def report(self, message):
        self.status.setText(message); self.log.append(message)

    def set_busy(self, busy):
        """兼容保留：操作期间不再禁用全局控件，仅由 run_action 提示占用。"""
        self.pause.setEnabled(not busy)

    def operation_finished(self):
        finished, self.worker = self.worker, None
        if finished is not None: finished.deleteLater()
        if self.pending_actions:
            self.start_operation(self.pending_actions.popleft())
            return
        self.pause.setEnabled(False); self.bar.setRange(0,100); self.bar.hide()
        self.hide_busy(); self.node_text.clear(); self.refresh()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel(); self.status.setText('请等待当前操作停止后再关闭客户端'); event.ignore()
        else:
            if self.cover_worker and self.cover_worker.isRunning(): self.cover_worker.wait(3000)
            restore_system_proxy()
            HUB.shutdown()
            event.accept()


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
        window = Window(); window.show()
        try:
            # 跟随系统明暗：系统切换时立刻换肤，不用重启
            app.styleHints().colorSchemeChanged.connect(lambda _: window.apply_theme(system_theme()))
        except AttributeError:
            pass
        return app.exec()
    except BaseException:
        log.write(traceback.format_exc())
        raise


if __name__ == '__main__':
    raise SystemExit(main())
