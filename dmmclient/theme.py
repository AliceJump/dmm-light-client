"""界面主题：跟随系统明暗，按调色板生成 QSS。

Qt 6.5+ 的 QStyleHints.colorScheme() 会报出系统当前是浅色还是深色；
取不到时（旧系统 / 非 Windows）按深色处理。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

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


def system_theme() -> str:
    """跟随系统：浅色系统就用浅色，其余（含取不到）用深色。"""
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
    except Exception:
        return 'dark'
    return 'light' if scheme == Qt.ColorScheme.Light else 'dark'


def palette(theme: str) -> dict:
    return LIGHT if theme == 'light' else DARK


def build_style(theme: str) -> str:
    p = palette(theme)
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
    QLabel#cover {{ background:{p['surface3']}; border-radius:16px; color:{p['faint']};
        font-size:12px; font-weight:600; }}
    QLabel#mark {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #4d7dff,stop:1 #7a5cff);
        border-radius:10px; color:#ffffff; font-size:17px; font-weight:700; }}
    QLabel#chip {{ background:{p['surface3']}; border-radius:12px; padding:9px 11px;
        font-size:12px; color:{p['dim']}; }}
    QLabel#heroTag {{ color:{p['ok']}; font-size:11px; font-weight:600; }}
    QLabel#heroTitle {{ font-size:22px; font-weight:600; color:{p['text']}; }}
    QLabel#badge {{ border-radius:10px; padding:3px 9px; font-size:10.5px; font-weight:600;
        background:rgba(12,14,18,0.62); color:#ffffff; }}
    QLabel#badge[kind="ok"] {{ background:{p['ok']}; }}
    QLabel#badge[kind="up"] {{ background:{p['warn']}; }}
    QLabel#badge[kind="r18"] {{ background:#d83c5a; }}

    /* 骨架 */
    QWidget#sidebar {{ background:{p['surface']}; border-right:1px solid {p['border']}; }}
    QWidget#topbar {{ background:{p['bg']}; border-bottom:1px solid {p['border']}; }}
    QWidget#statusbar {{ background:{p['surface']}; border-top:1px solid {p['border']}; }}
    QFrame#hero {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:20px; }}
    QWidget#chip {{ background:{p['surface3']}; border-radius:8px; }}
    QFrame#divider {{ border:none; background:{p['border']}; max-height:1px; }}

    /* 侧边栏导航 */
    QPushButton#nav {{ background:transparent; border:none; border-radius:12px; padding:9px 12px;
        text-align:left; font-size:13.5px; font-weight:400; color:{p['dim']}; }}
    QPushButton#nav:hover {{ background:{p['hover']}; color:{p['text']}; }}
    QPushButton#nav:checked {{ background:{p['accent_soft']}; color:{p['accent']}; font-weight:600; }}

    /* 卡片 */
    QWidget#card {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:20px; }}
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
    QComboBox {{ padding-right:26px; }}
    QComboBox::drop-down {{ border:none; width:24px; }}
    QComboBox::down-arrow {{ image:none; border-left:4px solid transparent; border-right:4px solid transparent;
        border-top:5px solid {p['dim']}; margin-right:10px; }}
    QComboBox QAbstractItemView {{ background:{p['surface']}; border:1px solid {p['border_strong']};
        border-radius:14px; padding:4px; selection-background-color:{p['accent_soft']};
        selection-color:{p['accent']}; outline:none; }}

    /* 进度条 */
    QProgressBar {{ background:{p['surface3']}; border:none; border-radius:3px; text-align:center;
        color:transparent; max-height:6px; min-height:6px; }}
    QProgressBar::chunk {{ border-radius:3px; background:{p['accent']}; }}

    /* 日志 */
    QTextEdit {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:16px;
        padding:8px; color:{p['dim']}; font-family:"Consolas","Microsoft YaHei UI",monospace; font-size:12px; }}

    /* 滚动条 */
    QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
    QScrollBar::handle:vertical {{ background:{p['border_strong']}; border-radius:5px; min-height:30px; }}
    QScrollBar::handle:vertical:hover {{ background:{p['faint']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}

    QWidget#busy {{ background:transparent; }}
    QLabel#busyload {{ color:{p['dim']}; font-size:13px; }}
    QListWidget {{ background:transparent; border:none; }}
    QListWidget::item {{ border:none; background:transparent; }}
    '''
