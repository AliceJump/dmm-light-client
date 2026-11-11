"""学园偶像大师的安装状态；界面、命令行和专用更新器共用。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .session import data_dir, dmm_dir


GAKUMAS_TITLE = '学园偶像大师'
GAKUMAS_INSTALL_HINT = ('学园偶像大师的首次安装需要专用安装程序；'
                        '请先完成安装，再选择含 gakumas.exe 的游戏目录')


@dataclass(frozen=True)
class GakumasInstallation:
    root: Path | None = None
    version: str = '未知'
    source: str = ''
    error: str = ''
    status: str = 'missing'
    recoverable: bool = False

    @property
    def installed(self) -> bool:
        return self.status == 'installed'

    def as_record(self) -> dict | None:
        if not self.installed or self.root is None:
            return None
        return {'root': str(self.root), 'version': self.version, 'title': GAKUMAS_TITLE}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError):
        return None


def _candidate(record, source: str) -> GakumasInstallation:
    if not isinstance(record, dict) or not isinstance(record.get('root'), str) or not record['root'].strip():
        return GakumasInstallation(source=source, status='invalid',
                                  error='游戏安装记录无效，请重新选择含 gakumas.exe 的目录')
    version = str(record.get('version') or '未知')
    try:
        root = Path(record['root']).resolve()
        if not root.is_dir():
            return GakumasInstallation(root, version, source,
                                      f'游戏安装目录已失效：{root}；请重新选择游戏目录', 'invalid')
        if not (root / 'gakumas.exe').is_file():
            recoverable = (root / '.ok-gm-update' / 'journal.json').is_file()
            return GakumasInstallation(root, version, source,
                                      f'游戏目录中没有 gakumas.exe：{root}；请恢复更新或重新选择游戏目录'
                                      if recoverable else
                                      f'游戏目录中没有 gakumas.exe：{root}；请重新选择游戏目录',
                                      'invalid', recoverable)
        return GakumasInstallation(root, version, source, status='installed')
    except (OSError, ValueError, RuntimeError):
        return GakumasInstallation(version=version, source=source, status='invalid',
                                  error='游戏安装目录无法读取，请重新选择含 gakumas.exe 的目录')


def gakumas_installation() -> GakumasInstallation:
    """只读验证专用、旧通用和官方记录；坏记录不能遮住后面的有效安装。"""
    candidates = []
    own_path = data_dir() / 'installation.json'
    if own_path.exists():
        candidates.append((_read_json(own_path), 'local'))

    managed = _read_json(data_dir() / 'games.json')
    if isinstance(managed, dict) and 'gakumas' in managed:
        candidates.append((managed['gakumas'], 'managed'))

    official = _read_json(dmm_dir() / 'dmmgame.cnf')
    entries = official.get('contents') if isinstance(official, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('productId') != 'gakumas' or entry.get('gameType') != 'GCL':
                continue
            detail = entry.get('detail')
            if not isinstance(detail, dict) or not detail.get('installed'):
                continue
            candidates.append(({'root': detail.get('path'), 'version': detail.get('version')}, 'official'))

    invalid = recoverable = None
    for record, source in candidates:
        result = _candidate(record, source)
        if result.installed:
            return result
        if result.recoverable and recoverable is None:
            recoverable = result
        if invalid is None:
            invalid = result
    return recoverable or invalid or GakumasInstallation(error=GAKUMAS_INSTALL_HINT)
