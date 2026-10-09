"""File-backed account slots; callers hold the shared helper lock."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import win32crypt

from dmmclient.backend.session import DmmError, atomic_write


class AccountStore:
    def __init__(self, root: Path):
        self.root = root
        self.index = root / 'accounts.json'

    def read(self):
        if not self.index.exists():
            return {'active': None, 'accounts': []}
        try:
            value = json.loads(self.index.read_text(encoding='utf-8'))
            if not isinstance(value['accounts'], list):
                raise ValueError()
            numbers, keys = set(), set()
            for entry in value['accounts']:
                self.validate(entry['number'], entry['name'])
                key = entry['key']
                if not re.fullmatch(r'[a-f0-9]{32}', key) or key in keys or entry['number'] in numbers:
                    raise ValueError()
                keys.add(key)
                numbers.add(entry['number'])
            if value['active'] is not None and value['active'] not in keys:
                raise ValueError()
            return value
        except Exception:
            raise DmmError('账号目录损坏，请保留 accounts.json 和 accounts 文件夹') from None

    @staticmethod
    def validate(number, name):
        if not isinstance(number, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', number):
            raise DmmError('账号编号使用 1～40 位字母、数字、下划线或横线')
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise DmmError('请填写 1～80 字的账号名称')

    def active(self):
        value = self.read()
        return next((entry for entry in value['accounts'] if entry['key'] == value['active']), None)

    def path(self, key):
        if not re.fullmatch(r'[a-f0-9]{32}', key):
            raise DmmError('账号文件路径非法')
        path = self.root / 'accounts' / key / 'session.dpapi'
        for parent in (path.parent.parent, path.parent, path):
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise DmmError('账号目录不能经过符号链接或目录联接')
        return path

    def current_path(self):
        active = self.active()
        return self.path(active['key']) if active else self.root / 'session.dpapi'

    @staticmethod
    def checked_blob(path):
        try:
            blob = path.read_bytes()
            value = json.loads(win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1])
            if not value['auth'].get('accessToken') or not isinstance(value['device'], dict):
                raise ValueError()
            return blob
        except Exception:
            raise DmmError('没有可用的加密登录文件，请先登录或导入 DMM 登录态') from None

    def save_current(self, number, name, source=None, *, new=False):
        self.validate(number, name)
        value = self.read()
        existing = next((e for e in value['accounts'] if e['number'] == number), None)
        if new and existing:
            raise DmmError('该编号已有账号，请使用新的编号；已有账号可使用「保存当前登录态」更新存档')
        blob = self.checked_blob(source or self.current_path())
        # Write a new snapshot first. A crash before the index replacement leaves
        # the old slot intact, even when explicitly replacing an existing number.
        entry = {'number': number, 'name': name.strip(), 'key': uuid.uuid4().hex}
        atomic_write(self.path(entry['key']), blob)
        value['accounts'] = [e for e in value['accounts'] if e['number'] != number] + [entry]
        value['active'] = entry['key']
        atomic_write(self.index, json.dumps(value, ensure_ascii=False).encode())
        atomic_write(self.root / 'session.dpapi', blob)
        return entry

    def switch(self, number):
        value = self.read()
        entry = next((e for e in value['accounts'] if e['number'] == number), None)
        if entry is None:
            raise DmmError('请选择已保存的账号')
        blob = self.checked_blob(self.path(entry['key']))
        value['active'] = entry['key']
        atomic_write(self.index, json.dumps(value, ensure_ascii=False).encode())
        atomic_write(self.root / 'session.dpapi', blob)
        return entry

    def persist(self, key, blob):
        active = self.active()
        if (active['key'] if active else None) != key:
            raise DmmError('当前账号已改变，已停止保存旧操作的登录态')
        if key:
            atomic_write(self.path(key), blob)
        atomic_write(self.root / 'session.dpapi', blob)
