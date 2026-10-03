from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from urllib.parse import urljoin, urlsplit

import requests

from dmmclient.backend.session import DmmError, atomic_write, dmm_url


def safe_path(root: Path, remote: str) -> Path:
    """DMM uses a single leading slash for paths relative to the install directory."""
    normalized = remote.replace("\\", "/")
    if normalized.startswith("//"):
        raise DmmError("下载清单包含非法路径")
    relative = normalized.removeprefix("/")
    parts = relative.split("/")
    is_reserved = getattr(os.path, "isreserved", lambda part: PureWindowsPath(part).is_reserved())
    if (not relative or any(p in {"", ".", ".."} or any(c in p for c in ':<>"|?*')
                            or any(ord(c) < 32 for c in p) or p.rstrip(". ") != p
                            or is_reserved(p) for p in parts)
            or ".ok-gm-update" in [p.lower() for p in parts]
            or PureWindowsPath(relative).drive):
        raise DmmError("下载清单包含非法路径")
    destination = root.joinpath(*parts).resolve()
    resolved_root = root.resolve()
    if not destination.is_relative_to(resolved_root) or destination == resolved_root:
        raise DmmError("下载目标超出游戏目录")
    # A symlink or junction inside the tree may resolve into a different game file.
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise DmmError("下载路径经过符号链接或目录联接")
    return destination


@dataclass(repr=False)
class GameFile:
    local: str
    remote: str
    size: int
    digest: str
    protected: bool = False
    delete: bool = False

    def matches(self, path: Path) -> bool:
        if self.delete:
            return not path.exists()
        if not path.is_file():
            return False
        if self.protected:
            return True
        if path.stat().st_size != self.size:
            return False
        if self.digest:
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "md5").hexdigest() == self.digest
        return True


@dataclass(repr=False)
class UpdatePlan:
    root: Path
    domain: str = field(repr=False)
    files: list[GameFile] = field(repr=False)
    version: str = ""

    @classmethod
    def parse(cls, root, domain, entries, version=""):
        dmm_url(domain)
        if not isinstance(entries, list) or not entries:
            raise DmmError("下载清单为空或格式不受支持")
        files, seen = [], set()
        for entry in entries:
            try:
                local = entry["local_path"]
                destination = safe_path(root, local)
                unique = str(destination).casefold()
                if unique in seen:
                    raise DmmError("下载清单包含重复目标")
                seen.add(unique)
                remote = entry["path"]
                if urlsplit(remote).scheme or remote.startswith("//"):
                    raise DmmError("下载清单包含非法下载地址")
                dmm_url(urljoin(domain.rstrip("/") + "/", remote.lstrip("/")))
                size = entry["size"]
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise DmmError("下载清单包含非法文件大小")
                digest = entry.get("hash", "").lower()
                if digest and not re.fullmatch("[0-9a-f]{32}", digest):
                    raise DmmError("下载清单的哈希格式不受支持")
                if entry.get("check_hash_flg") and not digest and not entry.get("force_delete_flg"):
                    raise DmmError("下载清单缺少校验哈希")
                files.append(GameFile(local, remote, size, digest,
                                      bool(entry.get("protected_flg")), bool(entry.get("force_delete_flg"))))
            except (KeyError, TypeError, AttributeError, ValueError):
                raise DmmError("下载清单格式不受支持") from None
        paths = sorted(seen)
        for index, path in enumerate(paths[:-1]):
            if paths[index + 1].startswith(path + os.sep):
                raise DmmError("下载清单的文件和目录冲突")
        return cls(root.resolve(), domain, files, version)

    def pending(self):
        return [file for file in self.files if not file.matches(safe_path(self.root, file.local))]


class VerifiedUpdater:
    def __init__(self, plan: UpdatePlan, proxy: str, sign: str, progress=lambda message: None, before_commit=lambda: None,
                 cancel_check=lambda: None, download_progress=lambda done, total: None):
        self.plan = plan
        self.progress = progress
        self.before_commit = before_commit
        self.cancel_check = cancel_check
        self.download_progress = download_progress
        self.completed_bytes = 0
        self.total_bytes = 0
        self.work = plan.root / ".ok-gm-update"
        self.staging = self.work / "staging"
        self.backup = self.work / "backup"
        self.journal = self.work / "journal.json"
        if self.work.is_symlink() or (hasattr(self.work, "is_junction") and self.work.is_junction()):
            raise DmmError("更新工作目录不能是符号链接或目录联接")
        for directory in (self.staging, self.backup):
            if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
                raise DmmError("更新暂存和备份目录不能是符号链接或目录联接")
        self.http = requests.Session()
        from dmmclient.backend.xray import attach_pool
        attach_pool(self.http, proxy)
        self.http.trust_env = False
        self.http.proxies = {"http": proxy, "https": proxy}
        self.http.headers.update({"Accept-Encoding": "identity"})
        # Download cookies never share the API's actauth header.
        host = urlsplit(plan.domain).hostname
        signed = {}
        for pair in sign.split(";"):
            key, separator, value = pair.strip().partition("=")
            if separator and key in {"CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id"}:
                signed[key] = value
                self.http.cookies.set(key, value, domain=host, path="/", secure=True)
        if len(signed) != 3:
            raise DmmError("下载授权缺少 CloudFront 签名")

    def _journal_write(self, data):
        atomic_write(self.journal, json.dumps(data, ensure_ascii=False).encode())

    def recover(self):
        if not self.journal.exists():
            return
        try:
            data = json.loads(self.journal.read_text(encoding="utf-8"))
            if data["state"] == "committed":
                return
            if data["state"] != "applying":
                raise ValueError()
            for item in reversed(data["items"]):
                destination = safe_path(self.plan.root, item["local"])
                backup = safe_path(self.backup, item["local"])
                if backup.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, destination)
                elif not item["existed"]:
                    destination.unlink(missing_ok=True)
            self._journal_write({"state": "committed", "rolled_back": True, "items": []})
            self.progress("已恢复上次中断的文件替换")
        except Exception:
            raise DmmError("上次更新恢复失败，请保留 .ok-gm-update 目录并检查文件占用") from None

    def download(self, file: GameFile):
        path = safe_path(self.staging, file.local)
        path.parent.mkdir(parents=True, exist_ok=True)
        verification = GameFile(file.local, file.remote, file.size, file.digest)
        if verification.matches(path):
            return path
        url = urljoin(self.plan.domain.rstrip("/") + "/", file.remote.lstrip("/"))
        attempts = getattr(self.http, 'proxy_attempts', 3)
        for attempt in range(attempts):
            self.cancel_check()
            offset = path.stat().st_size if path.exists() else 0
            if offset >= file.size:
                path.unlink(missing_ok=True)
                offset = 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with self.http.get(dmm_url(url), headers=headers, stream=True, timeout=(10, 60),
                                   allow_redirects=False) as response:
                    if response.status_code not in {200, 206}:
                        raise DmmError(f"下载失败：HTTP {response.status_code}")
                    if response.status_code == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if (not match or int(match[1]) != offset or int(match[3]) != file.size
                                or int(match[2]) != file.size - 1):
                            raise DmmError("断点下载响应范围不正确")
                    else:
                        offset = 0
                    with path.open("ab" if offset else "wb") as output:
                        written = offset
                        for block in response.iter_content(256 * 1024):
                            self.cancel_check()
                            if block:
                                written += len(block)
                                if written > file.size:
                                    raise DmmError("下载文件超出声明大小")
                                output.write(block)
                                self.download_progress(self.completed_bytes + written, self.total_bytes)
                        output.flush()
                        os.fsync(output.fileno())
                # Protected files still need full verification before first installation.
                verification = GameFile(file.local, file.remote, file.size, file.digest)
                if verification.matches(path):
                    return path
                path.unlink(missing_ok=True)
                if attempt == attempts - 1:
                    raise DmmError("下载文件未通过大小或 MD5 校验")
            except requests.RequestException:
                failover = getattr(self.http, 'proxy_failover', None)
                if failover is not None:
                    failover()
                if attempt == attempts - 1:
                    raise DmmError("游戏下载连接中断，已保存断点；重试 update 可继续") from None
        raise DmmError("游戏下载失败")

    def run(self):
        self.recover()
        pending = self.plan.pending()
        if not pending:
            self.progress("游戏文件校验通过，无需下载")
            return 0
        self.work.mkdir(parents=True, exist_ok=True)
        required = sum(file.size for file in pending if not file.delete)
        self.total_bytes = required
        if shutil.disk_usage(self.plan.root).free < required + 64 * 1024 * 1024:
            raise DmmError("游戏盘空间不足，无法暂存更新")
        downloads = {}
        for index, file in enumerate(pending, 1):
            self.cancel_check()
            self.progress(f"校验并暂存更新文件 {index}/{len(pending)}")
            if not file.delete:
                downloads[file.local] = self.download(file)
                self.completed_bytes += file.size
                self.download_progress(self.completed_bytes, self.total_bytes)
        # All payloads are verified before changing any installed file.
        self.cancel_check()
        self.before_commit()
        if self.backup.exists():
            # Work is confined to this verified installation's private update directory.
            if not self.backup.resolve().is_relative_to(self.work.resolve()):
                raise DmmError("备份目录超出更新工作目录")
            shutil.rmtree(self.backup)
        data = {"state": "applying", "version": self.plan.version,
                "items": [{"local": f.local, "existed": safe_path(self.plan.root, f.local).exists()} for f in pending]}
        self._journal_write(data)
        try:
            for file in pending:
                destination = safe_path(self.plan.root, file.local)
                if destination.exists():
                    backup = safe_path(self.backup, file.local)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, backup)
                if not file.delete:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(downloads[file.local], destination)
            if self.plan.pending():
                raise DmmError("替换后的文件没有通过最终校验")
            data["state"] = "committed"
            self._journal_write(data)
        except BaseException:
            self.recover()
            raise
        self.progress(f"更新完成，已校验 {len(self.plan.files)} 个文件")
        return len(pending)
