"""Temporarily route the normal browser through the helper's explicit proxy."""
from __future__ import annotations

import ctypes
import fnmatch
import json
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dmmclient.backend.session import DmmError, atomic_write, valid_proxy


@dataclass(frozen=True)
class ProxySettings:
    flags: int
    server: str
    bypass: str


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
        options = (_Option * 3)()
        for option, identifier in zip(options, (10, 2, 3)):
            option.option = identifier
        settings = self._list(options)
        size = wintypes.DWORD(ctypes.sizeof(settings))
        if not self.api.InternetQueryOptionW(None, 75, ctypes.byref(settings), ctypes.byref(size)):
            raise DmmError("无法读取 Windows 浏览器代理设置")
        try:
            return ProxySettings(options[0].value.number,
                                 ctypes.wstring_at(options[1].value.string) if options[1].value.string else "",
                                 ctypes.wstring_at(options[2].value.string) if options[2].value.string else "")
        finally:
            for option in options[1:]:
                if option.value.string:
                    self.kernel.GlobalFree(option.value.string)

    def set(self, value):
        options = (_Option * 3)()
        options[0].option = 1
        options[0].value.number = value.flags
        # Keep string buffers alive until InternetSetOptionW returns.
        server = ctypes.create_unicode_buffer(value.server)
        bypass = ctypes.create_unicode_buffer(value.bypass)
        for option, identifier, buffer in zip(options[1:], (2, 3), (server, bypass)):
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


def restore_browser_proxy(root: Path, backend=None, progress=lambda message: None):
    """Restore only fields we still own, preserving changes made by v2rayN or the user."""
    journal = root / "temporary-browser-proxy.json"
    if not journal.exists():
        return False
    backend = backend or WindowsProxy()
    try:
        saved = json.loads(journal.read_text(encoding="utf-8"))
        original = ProxySettings(**saved["original"])
        applied = ProxySettings(**saved["applied"])
        current = backend.get()
        # Flags, server and bypass form one configuration. Restoring individual fields
        # could disable a new proxy selected by the user during the login.
        restored = original if current == applied else current
        if restored != current:
            backend.set(restored)
            if backend.get() != restored:
                raise DmmError("代理设置恢复后校验失败")
        journal.unlink()
    except Exception:
        raise DmmError("临时代理恢复失败；请运行 python -m dmmclient.backend restore-proxy 重试") from None
    progress("临时浏览器代理已结束，已恢复原设置；期间的外部修改予以保留")
    return True


@contextmanager
def temporary_browser_proxy(proxy: str, root: Path, backend=None, progress=lambda message: None):
    backend = backend or WindowsProxy()
    restore_browser_proxy(root, backend, progress)
    original = backend.get()
    parsed = urlsplit(valid_proxy(proxy))
    if parsed.scheme != 'http':
        raise DmmError('浏览器登录需要 HTTP 代理；可暂用内置节点池，登录后再切回 SOCKS5')
    applied = ProxySettings(3, parsed.netloc, dmm_bypass_removed(original.bypass))
    if applied == original:
        yield
        return
    journal = root / "temporary-browser-proxy.json"
    atomic_write(journal, json.dumps({"original": asdict(original), "applied": asdict(applied)}).encode())
    try:
        backend.set(applied)
        if backend.get() != applied:
            raise DmmError("临时浏览器代理没有生效，请检查 v2rayN 是否重新设置了系统代理")
        progress("登录期间已临时启用指定的本地代理，移除 DMM 直连例外；结束后自动恢复")
        yield
    finally:
        restore_browser_proxy(root, backend, progress)
