"""Shared game launch service used by the framework and the launcher page."""
from __future__ import annotations

import os
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

import psutil

from dmmclient.backend.browser_proxy import restore_browser_proxy
from dmmclient.backend.helper import DmmHelper, ensure_game_stopped, helper_lock, windows_args
from dmmclient.backend.session import DmmError, LoginRequired, DmmSession, atomic_write, data_dir


def resolve_game_exe(_detected_path=None):
    root, _ = DmmHelper.installed_game()
    return str(root / 'gakumas.exe')


def _browser_login(helper, progress, browser='system'):
    try:
        helper.session.login(browser=browser)
    except LoginRequired as error:
        if '授权码签发失败' not in str(error):
            raise
        # The remembered browser protocol handler may have already delivered the
        # code to DMM Player. Only retain/import a server-verified credential.
        if not helper.session.authenticated():
            helper.session.import_player()
            if not helper.session.authenticated():
                raise error
        progress('有效登录态已保存')


@contextmanager
def login_candidate(helper, fresh=False):
    original = helper.session
    base = original.root
    pending = base / 'pending' / uuid.uuid4().hex
    pending.mkdir(parents=True)
    if not fresh and original.accounts.current_path().exists():
        atomic_write(pending / 'session.dpapi', original.accounts.current_path().read_bytes())
    candidate = DmmSession(original.proxy, pending)
    candidate.proxy_root = original.proxy_root
    candidate.progress = original.progress
    candidate.cancel_check = original.cancel_check
    helper.session = candidate
    try:
        yield candidate
    finally:
        helper.session = original
        # Delete only this operation's known staging file, never account backups.
        (pending / 'session.dpapi').unlink(missing_ok=True)
        try:
            pending.rmdir()
        except OSError:
            pass


def _adopt(original, candidate):
    original.auth = candidate.auth
    original.device = candidate.device
    original.identity = candidate.identity
    original.http.cookies.clear()
    original.http.cookies.update(candidate.http.cookies)
    original.save()


def browser_login(helper, progress=lambda message: None, browser='system'):
    original = helper.session
    if not original.profile_key:
        _browser_login(helper, progress, browser)
        return
    if not original.identity:
        try:
            original.verify_game_identity()
        except DmmError:
            raise LoginRequired('该快照尚未确认账号身份，请先使用原登录态启动一次，或重新登录并保存为新账号') from None
    progress('请确认浏览器登录的是当前所选账号；身份不符时不会覆盖存档')
    with login_candidate(helper) as candidate:
        _browser_login(helper, progress, browser)
        candidate.verify_game_identity()
        _adopt(original, candidate)


def import_login(helper):
    original = helper.session
    if not original.profile_key:
        return original.import_player()
    if not original.identity:
        original.verify_game_identity()
    with login_candidate(helper) as candidate:
        copied = candidate.import_player()
        candidate.verify_game_identity()
        _adopt(original, candidate)
        return copied


