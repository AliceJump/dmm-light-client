"""Temporarily route the normal browser through the helper's explicit proxy."""
from __future__ import annotations

import ctypes
import fnmatch
import json
import os
import threading
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from dmmclient.backend.session import DmmError, atomic_write, data_dir, valid_proxy

_LOCK = threading.RLock()
_ACTIVE_LOGINS = set()
_TEMPORARY_JOURNAL = 'temporary-browser-proxy.json'
_SYSTEM_JOURNAL = 'system-browser-proxy.json'


@contextmanager
def _settings_errors():
    try:
        yield
    except DmmError:
        raise
    except Exception:
        raise DmmError('无法读取或保存浏览器代理设置；请检查客户端数据目录权限后重试') from None


@dataclass(frozen=True)
class ProxySettings:
    flags: int
    server: str
    bypass: str
    auto_config_url: str = ''


class _Value(ctypes.Union):
    _fields_ = [("number", wintypes.DWORD), ("string", ctypes.c_void_p), ("time", wintypes.FILETIME)]


class _Option(ctypes.Structure):
    _fields_ = [("option", wintypes.DWORD), ("value", _Value)]


class _Options(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("connection", ctypes.c_wchar_p),
                ("count", wintypes.DWORD), ("error", wintypes.DWORD),
                ("options", ctypes.POINTER(_Option))]


class WindowsProxy:
    """WinINet connection API; avoids directly editing registry implementation details."""

    def __init__(self):
        self.api = ctypes.WinDLL("wininet", use_last_error=True)
        self.api.InternetQueryOptionW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        self.api.InternetQueryOptionW.restype = wintypes.BOOL
        self.api.InternetSetOptionW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.api.InternetSetOptionW.restype = wintypes.BOOL
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GlobalFree.argtypes = [ctypes.c_void_p]
        self.kernel.GlobalFree.restype = ctypes.c_void_p

    @staticmethod
    def _list(options):
        return _Options(ctypes.sizeof(_Options), None, len(options), 0, options)

    def get(self):
        # FLAGS_UI (10) is the Windows 7+ query interface; FLAGS (1) is the setter.
        options = (_Option * 4)()
        for option, identifier in zip(options, (10, 2, 3, 4)):
            option.option = identifier
        settings = self._list(options)
        size = wintypes.DWORD(ctypes.sizeof(settings))
        if not self.api.InternetQueryOptionW(None, 75, ctypes.byref(settings), ctypes.byref(size)):
            raise DmmError("无法读取 Windows 浏览器代理设置")
        try:
            return ProxySettings(options[0].value.number,
                                 ctypes.wstring_at(options[1].value.string) if options[1].value.string else "",
                                 ctypes.wstring_at(options[2].value.string) if options[2].value.string else "",
                                 ctypes.wstring_at(options[3].value.string) if options[3].value.string else "")
        finally:
            for option in options[1:]:
                if option.value.string:
                    self.kernel.GlobalFree(option.value.string)

    def set(self, value):
        options = (_Option * 4)()
        options[0].option = 1
        options[0].value.number = value.flags
        # Keep string buffers alive until InternetSetOptionW returns.
        server = ctypes.create_unicode_buffer(value.server)
        bypass = ctypes.create_unicode_buffer(value.bypass)
        auto_config_url = ctypes.create_unicode_buffer(value.auto_config_url)
        for option, identifier, buffer in zip(options[1:], (2, 3, 4), (server, bypass, auto_config_url)):
            option.option = identifier
            option.value.string = ctypes.cast(buffer, ctypes.c_void_p).value
        settings = self._list(options)
        if not self.api.InternetSetOptionW(None, 75, ctypes.byref(settings), ctypes.sizeof(settings)):
            raise DmmError("无法应用 Windows 临时浏览器代理")
        # Inform existing applications, then refresh WinINet's cached settings.
        for option in (39, 37):
            if not self.api.InternetSetOptionW(None, option, None, 0):
                raise DmmError("浏览器代理变更通知失败")


def dmm_bypass_removed(bypass: str) -> str:
    hosts = ("dmm.com", "www.dmm.com", "accounts.dmm.com", "games.dmm.com",
             "apidgp-gameplayer.games.dmm.com", "webdgp-gameplayer.games.dmm.com",
             "dmm.co.jp", "www.dmm.co.jp", "player.games.dmm.co.jp")
    keep = []
    for original in bypass.split(";"):
        pattern = original.strip().lower()
        if pattern and any(fnmatch.fnmatchcase(host, pattern) for host in hosts):
            continue
        keep.append(original)
    return ";".join(keep)


def _read_journal(journal):
    saved = json.loads(journal.read_text(encoding='utf-8'))
    return saved, ProxySettings(**saved['original']), ProxySettings(**saved['applied'])


def _other_owner_alive(saved):
    owner = saved.get('owner')
    if not owner or owner['pid'] == os.getpid():
        return False
    try:
        return psutil.Process(owner['pid']).create_time() == owner['created']
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _save_journal(journal, original, applied):
    atomic_write(journal, json.dumps({'original': asdict(original), 'applied': asdict(applied),
        'owner': {'pid': os.getpid(), 'created': psutil.Process().create_time()}}).encode())


def _restore(journal, backend, progress):
    """Restore a configuration as a unit, only while it is still ours."""
    if not journal.exists():
        return False
    try:
        saved, original, applied = _read_journal(journal)
        if _other_owner_alive(saved):
            return False
        current = backend.get()
        # Older versions never changed or recorded the PAC URL. Preserve that
        # field while recovering their snapshots instead of mistaking it for
        # an external edit and leaving a now-dead temporary proxy selected.
        if 'auto_config_url' not in saved['original']:
            original = replace(original, auto_config_url=current.auto_config_url)
        if 'auto_config_url' not in saved['applied']:
            applied = replace(applied, auto_config_url=current.auto_config_url)
        if current == applied and current != original:
            backend.set(original)
            if backend.get() != original:
                raise DmmError('代理设置恢复后校验失败')
        journal.unlink()
    except Exception:
        raise DmmError('浏览器代理恢复失败；原设置已保留，请关闭当前操作后重启客户端重试') from None
    progress('浏览器代理已恢复原设置' if current == applied else '浏览器代理已被外部修改，保留当前设置')
    return True


def restore_browser_proxy(root: Path, backend=None, progress=lambda message: None):
    """Recover an interrupted login, never an active login or the UI's proxy toggle."""
    with _LOCK, _settings_errors():
        if root.resolve() in _ACTIVE_LOGINS or not (root / _TEMPORARY_JOURNAL).exists():
            return False
        return _restore(root / _TEMPORARY_JOURNAL, backend or WindowsProxy(), progress)


def _browser_settings(proxy, original):
    parsed = urlsplit(valid_proxy(proxy))
    if parsed.scheme != 'http':
        raise DmmError('浏览器系统代理需要 HTTP 地址；请选择内置节点或填写外部代理的 HTTP 端口')
    return ProxySettings(3, parsed.netloc, dmm_bypass_removed(original.bypass), original.auto_config_url)


def _check_login_idle(root):
    if root.resolve() in _ACTIVE_LOGINS:
        raise DmmError('浏览器登录正在使用临时代理，请先完成或取消登录，再切换系统代理')
    journal = root / _TEMPORARY_JOURNAL
    if journal.exists() and _other_owner_alive(_read_journal(journal)[0]):
        raise DmmError('另一个客户端正在登录，请等待登录结束后再切换系统代理')


def enable_system_proxy(proxy, root=None, backend=None):
    """The browser toggle and login share WinINet settings and ownership checks."""
    root = root or data_dir()
    with _LOCK, _settings_errors():
        _check_login_idle(root)
        backend = backend or WindowsProxy()
        restore_browser_proxy(root, backend)
        current = backend.get()
        applied = _browser_settings(proxy, current)
        original = current
        journal = root / _SYSTEM_JOURNAL
        if journal.exists():
            saved, previous, owned = _read_journal(journal)
            if _other_owner_alive(saved):
                raise DmmError('另一个客户端正在使用系统代理，请先在该客户端关闭')
            if current == owned:
                original = previous
        _save_journal(journal, original, applied)
        try:
            backend.set(applied)
            if backend.get() != applied:
                raise DmmError('系统代理没有生效，请检查其他代理软件是否重新设置了系统代理')
        except Exception:
            _restore(journal, backend, lambda message: None)
            raise


def restore_system_proxy(root=None, backend=None, progress=lambda message: None):
    root = root or data_dir()
    with _LOCK, _settings_errors():
        _check_login_idle(root)
        journal = root / _SYSTEM_JOURNAL
        if not journal.exists():
            return False
        return _restore(journal, backend or WindowsProxy(), progress)


def system_proxy_active(root=None, backend=None):
    root = root or data_dir()
    with _LOCK:
        journal = root / _SYSTEM_JOURNAL
        if not journal.exists():
            return False
        try:
            saved, _, applied = _read_journal(journal)
            return not _other_owner_alive(saved) and (backend or WindowsProxy()).get() == applied
        except Exception:
            return False


@contextmanager
def temporary_browser_proxy(proxy: str, root: Path, backend=None, progress=lambda message: None):
    with _LOCK, _settings_errors():
        _check_login_idle(root)
        backend = backend or WindowsProxy()
        restore_browser_proxy(root, backend, progress)
        original = backend.get()
        applied = _browser_settings(proxy, original)
        journal = root / _TEMPORARY_JOURNAL
        # Even a no-op login needs an owner record so another client instance
        # cannot switch off the proxy it is currently using.
        _save_journal(journal, original, applied)
        _ACTIVE_LOGINS.add(root.resolve())

    def check_proxy():
        with _settings_errors():
            if backend.get() != applied:
                raise DmmError('登录期间系统代理被其他软件修改；请暂停其他软件的自动设置系统代理后重试登录')

    try:
        with _settings_errors():
            if applied != original:
                backend.set(applied)
        check_proxy()
        progress("登录期间已临时启用指定的本地代理，移除 DMM 直连例外；结束后自动恢复")
        yield check_proxy
    finally:
        with _LOCK:
            try:
                _restore(journal, backend, progress)
            finally:
                _ACTIVE_LOGINS.discard(root.resolve())
