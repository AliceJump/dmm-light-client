"""界面主题：跟随系统明暗，按调色板生成 QSS。

Qt 6.5+ 的 QStyleHints.colorScheme() 会报出系统当前是浅色还是深色；
取不到时（旧系统 / 非 Windows）按深色处理。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from .backend.session import data_dir

_ARROWS: dict = {}


def chevron_path(theme: str, colors: dict):
    """生成一张跟随主题色的下拉箭头。

    Qt 的 QSS 不支持用透明边框拼三角（那会画成一个灰方块），
    所以老老实实画一张 PNG 再让样式表引用。
    """
    if theme in _ARROWS:
        return _ARROWS[theme]
    path = None
    try:
        if QApplication.instance() is None:      # 没有 QApplication 时画不了 QPixmap
            raise RuntimeError('no QApplication')
        folder = Path(data_dir()) / 'ui'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f'chevron-{theme}.png'
        pixmap = QPixmap(20, 20); pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap); painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(colors['dim'])); pen.setWidthF(1.9)
        pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(5.5, 8.0), QPointF(10.0, 12.5))
        painter.drawLine(QPointF(10.0, 12.5), QPointF(14.5, 8.0))
        painter.end()
        pixmap.save(str(target), 'PNG')
        path = target
    except Exception:
        path = None
    _ARROWS[theme] = path
    return path

DARK = {
    'bg': '#0d0f13', 'surface': '#14171d', 'surface2': '#1a1e26', 'surface3': '#232833',
    'border': '#1f242d', 'border_strong': '#2b323d',
    'text': '#e9edf4', 'dim': '#98a2b3', 'faint': '#6b7482',
    'accent': '#5b7cfa', 'accent_hover': '#6f8cff', 'accent_soft': '#1e2740',
    'accent_text': '#ffffff', 'ok': '#3fbf8f', 'warn': '#e0a33c',
    'input': '#12151b', 'hover': '#222833',
}

LIGHT = {
    'bg': '#f4f5f8', 'surface': '#ffffff', 'surface2': '#fafbfc', 'surface3': '#eef0f4',
    'border': '#e5e7ec', 'border_strong': '#d5d9e0',
    'text': '#14171d', 'dim': '#5f6875', 'faint': '#8b93a1',
    'accent': '#2f66f6', 'accent_hover': '#4a7cff', 'accent_soft': '#e8efff',
    'accent_text': '#ffffff', 'ok': '#17916a', 'warn': '#b5761a',
    'input': '#f7f8fa', 'hover': '#f0f2f6',
}

FONT = '"Microsoft YaHei UI","Yu Gothic UI","Segoe UI",system-ui,sans-serif'

# 动效参数，对齐 dash 站那套：慢一点、指数收尾、交互用回弹
EASE_OUT = (0.22, 1.0, 0.36, 1.0)      # cubic-bezier(.22,1,.36,1)
EASE_SPRING = (0.34, 1.4, 0.64, 1.0)   # cubic-bezier(.34,1.4,.64,1)
DUR_SLOW = 620      # 结构变化（详情展开、换页）
DUR_MID = 320       # 交互反馈
DUR_FAST = 180


def system_theme() -> str:
    """跟随系统：浅色系统就用浅色，其余（含取不到）用深色。"""
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
    except Exception:
        return 'dark'
    return 'light' if scheme == Qt.ColorScheme.Light else 'dark'


def palette(theme: str) -> dict:
    return LIGHT if theme == 'light' else DARK


def easing(points):
    """把 CSS 的 cubic-bezier(x1,y1,x2,y2) 转成 QEasingCurve（失败退回 OutCubic）。"""
    from PySide6.QtCore import QEasingCurve, QPointF
    try:
        curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
        curve.addCubicBezierSegment(QPointF(points[0], points[1]),
                                    QPointF(points[2], points[3]), QPointF(1.0, 1.0))
        return curve
    except Exception:
        return QEasingCurve(QEasingCurve.Type.OutCubic)


def build_style(theme: str) -> str:
    p = palette(theme)
    arrow = chevron_path(theme, p)
    arrow_rule = (f"image:url({str(arrow).replace(chr(92), '/')}); width:20px; height:20px;"
                  if arrow else '')
    return f'''
    QWidget {{ background:{p['bg']}; color:{p['text']}; font-family:{FONT}; font-size:14px; }}
    QLabel {{ background:transparent; }}
    QLabel#heading {{ font-size:15px; font-weight:600; color:{p['text']}; }}
    QLabel#tagline {{ font-size:11px; color:{p['faint']}; }}
    QLabel#section {{ font-size:14px; font-weight:600; color:{p['text']}; }}
    QLabel#pagetitle {{ font-size:17px; font-weight:600; color:{p['text']}; }}
    QLabel#muted {{ color:{p['faint']}; font-size:12px; }}
    QLabel#status {{ font-size:12px; color:{p['dim']}; }}
    QLabel#cardtitle {{ font-size:13.5px; font-weight:600; color:{p['text']}; }}
    QLabel#cardmeta {{ font-size:11.5px; color:{p['faint']}; }}
    QLabel#detailtitle {{ font-size:19px; font-weight:600; color:{p['text']}; }}
    QLabel#cover {{ background:{p['surface3']}; border-radius:20px; color:{p['faint']};
        font-size:12px; font-weight:600; }}
    QLabel#mark {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #4d7dff,stop:1 #7a5cff);
        border-radius:10px; color:#ffffff; font-size:17px; font-weight:700; }}
    QLabel#chip {{ background:{p['surface3']}; border-radius:14px; padding:9px 11px;
        font-size:12px; color:{p['dim']}; }}
    QLabel#heroTag {{ color:{p['ok']}; font-size:11px; font-weight:600; }}
    QLabel#heroTitle {{ font-size:22px; font-weight:600; color:{p['text']}; }}
    QLabel#badge {{ border-radius:12px; padding:3px 9px; font-size:10.5px; font-weight:600;
        background:rgba(12,14,18,0.62); color:#ffffff; }}
    QLabel#badge[kind="ok"] {{ background:{p['ok']}; }}
    QLabel#badge[kind="up"] {{ background:{p['warn']}; }}
    QLabel#badge[kind="r18"] {{ background:#d83c5a; }}

    /* 骨架 */
    QWidget#sidebar {{ background:{p['surface']}; border-right:1px solid {p['border']}; }}
    QWidget#topbar {{ background:{p['bg']}; border-bottom:1px solid {p['border']}; }}
    QWidget#statusbar {{ background:{p['surface']}; border-top:1px solid {p['border']}; }}
    QFrame#hero {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:24px; }}
    QWidget#chip {{ background:{p['surface3']}; border-radius:8px; }}
    QFrame#divider {{ border:none; background:{p['border']}; max-height:1px; }}

    /* 侧边栏导航 */
    QPushButton#nav {{ background:transparent; border:none; border-radius:14px; padding:9px 12px;
        text-align:left; font-size:13.5px; font-weight:400; color:{p['dim']}; }}
    QPushButton#nav:hover {{ background:{p['hover']}; color:{p['text']}; }}
    QPushButton#nav:checked {{ background:{p['accent_soft']}; color:{p['accent']}; font-weight:600; }}

    /* 卡片 */
    QWidget#card {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:24px; }}
    QWidget#card:hover {{ border-color:{p['border_strong']}; }}

    /* 按钮 */
    QPushButton {{ background:{p['surface']}; border:1px solid {p['border_strong']}; border-radius:999px;
        padding:8px 16px; color:{p['text']}; font-weight:400; outline:none; }}
    QPushButton:focus {{ outline:none; }}
    QPushButton:hover {{ border-color:{p['accent']}; color:{p['accent']}; }}
    QPushButton:pressed {{ background:{p['hover']}; }}
    QPushButton:disabled {{ color:{p['faint']}; border-color:{p['border']}; }}
    QPushButton[accent="true"] {{ background:{p['accent']}; border-color:{p['accent']};
        color:{p['accent_text']}; font-weight:600; }}
    QPushButton[accent="true"]:hover {{ background:{p['accent_hover']}; color:{p['accent_text']}; }}
    QPushButton[accent="true"]:pressed {{ background:{p['accent']}; }}
    QPushButton[accent="true"]:disabled {{ background:{p['surface3']}; border-color:{p['border']};
        color:{p['faint']}; }}
    QPushButton#link {{ background:transparent; border:none; border-radius:6px; padding:5px 4px;
        color:{p['dim']}; font-size:12px; font-weight:400; }}
    QPushButton#link:hover {{ color:{p['accent']}; background:{p['accent_soft']}; }}
    QPushButton#link[danger="true"] {{ color:{p['faint']}; }}
    QPushButton#link[danger="true"]:hover {{ color:#d83c5a; background:rgba(216,60,90,0.12); }}
    QPushButton#pause {{ padding:7px 14px; font-size:12px; }}

    /* 输入 */
    QLineEdit, QComboBox {{ background:{p['input']}; border:1px solid {p['border_strong']};
        border-radius:999px; padding:8px 14px; color:{p['text']};
        selection-background-color:{p['accent']}; selection-color:{p['accent_text']}; }}
    QLineEdit:focus, QComboBox:focus {{ border-color:{p['accent']}; }}
    QLineEdit:disabled, QComboBox:disabled {{ color:{p['faint']}; }}
    QComboBox {{ padding-right:30px; }}
    QComboBox::drop-down {{ border:none; width:30px; }}
    QComboBox::down-arrow {{ {arrow_rule} }}
    QComboBox QAbstractItemView {{ background:{p['surface']}; border:1px solid {p['border_strong']};
        border-radius:18px; padding:6px; outline:none;
        selection-background-color:{p['accent_soft']}; selection-color:{p['accent']}; }}
    QComboBox QAbstractItemView::item {{ padding:7px 10px; border-radius:8px; min-height:20px; }}

    /* 进度条 */
    QProgressBar {{ background:{p['surface3']}; border:none; border-radius:3px; text-align:center;
        color:transparent; max-height:6px; min-height:6px; }}
    QProgressBar::chunk {{ border-radius:3px; background:{p['accent']}; }}

    /* 日志 */
    QTextEdit {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:20px;
        padding:8px; color:{p['dim']}; font-family:"Consolas","Microsoft YaHei UI",monospace; font-size:12px; }}

    /* 滚动条 */
    QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
    QScrollBar::handle:vertical {{ background:{p['border_strong']}; border-radius:5px; min-height:30px; }}
    QScrollBar::handle:vertical:hover {{ background:{p['faint']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}

    QWidget#detail {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:24px; }}
    QWidget#listrow {{ background:transparent; border:1px solid transparent; border-radius:16px; }}
    QWidget#listrow:hover {{ background:{p['hover']}; }}
    QWidget#listrow[active="true"] {{ background:{p['accent_soft']}; border-color:{p['accent']}; }}
    QLabel#listtitle {{ font-size:12.5px; font-weight:600; color:{p['text']}; }}
    QLabel#listmeta {{ font-size:11px; color:{p['faint']}; }}
    QLabel#detailname {{ font-size:24px; font-weight:600; color:{p['text']}; }}
    QLabel#detaildesc {{ font-size:13px; color:{p['dim']}; }}
    QLabel#field {{ font-size:12.5px; color:{p['dim']}; }}
    QWidget#chipbox {{ background:{p['surface3']}; border-radius:14px; }}
    QWidget#busy {{ background:transparent; }}
    QLabel#busyload {{ color:{p['dim']}; font-size:13px; }}
    QListWidget {{ background:transparent; border:none; }}
    QListWidget::item {{ border:none; background:transparent; }}
    '''
