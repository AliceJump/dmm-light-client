"""客户端设置的读写。不依赖 Qt，界面与命令行共用。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .session import DmmError, atomic_write, data_dir
from .xray import NODE_ENABLED_KEY

DEFAULT_PROXY = 'http://127.0.0.1:10808'
_SETTINGS_LOCK = threading.RLock()


def settings_path() -> Path:
    return data_dir() / 'settings.json'


def load_settings() -> dict:
    with _SETTINGS_LOCK:
        defaults = {'proxy': DEFAULT_PROXY, NODE_ENABLED_KEY: True}
        path = settings_path()
        try:
            saved = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        except (OSError, UnicodeError, ValueError):
            raise DmmError('客户端设置无法读取，请检查 settings.json；原文件已保留') from None
        if not isinstance(saved, dict):
            raise DmmError('客户端设置格式无效，settings.json 必须是 JSON 对象；原文件已保留')
        defaults.update(saved)
        return defaults


def save_settings(settings: dict) -> dict:
    with _SETTINGS_LOCK:
        atomic_write(settings_path(), json.dumps(settings, ensure_ascii=False).encode())
        return settings


def update_settings(changes: dict, remove=()) -> dict:
    """Merge edited keys into the latest file, preserving worker/other-page settings."""
    with _SETTINGS_LOCK:
        settings = load_settings()
        settings.update(changes)
        for key in remove:
            settings.pop(key, None)
        return save_settings(settings)


def default_install_base() -> Path:
    """未指定时游戏装到 data_dir()/games/<游戏编号>。"""
    return data_dir() / 'games'


def global_install_base(settings: dict | None = None) -> Path:
    """默认游戏库：用户在设置里指定过就用它，否则用 data_dir()/games。"""
    value = (settings if settings is not None else load_settings()).get('install_base')
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    return default_install_base()


def install_bases() -> dict:
    """每款游戏单独指定的安装基准目录（安装时选择或移动后记录）。"""
    value = load_settings().get('install_bases')
    return value if isinstance(value, dict) else {}


def game_install_base(product: str) -> Path:
    """该游戏的安装基准目录：用户选过就用它，否则用默认目录。"""
    chosen = install_bases().get(product)
    if isinstance(chosen, str) and chosen.strip():
        return Path(chosen).expanduser()
    return global_install_base()


def game_install_root(product: str) -> Path:
    """该游戏应当安装到的完整目录（基准目录下的同名子目录）。"""
    return game_install_base(product) / product


def remember_install_base(product: str, base) -> Path:
    """记住用户为某款游戏选择的安装基准目录。"""
    resolved = Path(base).expanduser().resolve()
    with _SETTINGS_LOCK:
        settings = load_settings()
        bases = dict(settings.get('install_bases') or {})
        bases[product] = str(resolved)
        settings['install_bases'] = bases
        save_settings(settings)
    return resolved


def checked_base(value) -> Path:
    """校验命令行传进来的目录参数。"""
    if value is None or not str(value).strip():
        raise DmmError('请提供安装目录')
    return Path(str(value)).expanduser().resolve()
