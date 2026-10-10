"""客户端设置的读写。不依赖 Qt，界面与命令行共用。"""
from __future__ import annotations

import json
from pathlib import Path

from .session import DmmError, atomic_write, data_dir
from .xray import NODE_ENABLED_KEY

DEFAULT_PROXY = 'http://127.0.0.1:10808'


def settings_path() -> Path:
    return data_dir() / 'settings.json'


def load_settings() -> dict:
    defaults = {'proxy': DEFAULT_PROXY, NODE_ENABLED_KEY: True}
    path = settings_path()
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding='utf-8')))
    return defaults


def save_settings(settings: dict) -> dict:
    atomic_write(settings_path(), json.dumps(settings, ensure_ascii=False).encode())
    return settings


def default_install_base() -> Path:
    """未指定时游戏装到 data_dir()/games/<游戏编号>。"""
    return data_dir() / 'games'


def install_bases() -> dict:
    """每款游戏单独指定的安装基准目录（安装时选择或移动后记录）。"""
    value = load_settings().get('install_bases')
    return value if isinstance(value, dict) else {}


def game_install_base(product: str) -> Path:
    """该游戏的安装基准目录：用户选过就用它，否则用默认目录。"""
    chosen = install_bases().get(product)
    if isinstance(chosen, str) and chosen.strip():
        return Path(chosen).expanduser()
    return default_install_base()


def game_install_root(product: str) -> Path:
    """该游戏应当安装到的完整目录（基准目录下的同名子目录）。"""
    return game_install_base(product) / product


def remember_install_base(product: str, base) -> Path:
    """记住用户为某款游戏选择的安装基准目录。"""
    resolved = Path(base).expanduser().resolve()
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
