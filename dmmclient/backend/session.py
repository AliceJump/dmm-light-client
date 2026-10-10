from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
import win32crypt
from Crypto.Cipher import AES

API = "https://apidgp-gameplayer.games.dmm.com"
DEFAULT_PROXY = "http://127.0.0.1:10808"


class DmmError(RuntimeError):
    """Only sanitized messages may be shown to the user."""


class LoginRequired(DmmError):
    pass


class OperationCancelled(DmmError):
    pass


class DmmApiError(DmmError):
    HINTS = {203: '登录态已失效', 211: '可能需要年龄认证', 314: '游戏未加入 DMM 游戏库',
             801: '地区限制，需要日本节点'}

    def __init__(self, code):
        self.code = code
        hint = self.HINTS.get(code) if isinstance(code, int) else None
        text = f'DMM 接口拒绝请求（result_code={code if isinstance(code, int) else "unknown"}）'
        if hint: text += f'：{hint}'
        super().__init__(text)


def data_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "DmmLightClient"


def dmm_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "dmmgameplayer5"


def atomic_write(path: Path, value: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def valid_proxy(proxy: str) -> str:
    try:
        parsed = urlsplit(proxy)
        if (parsed.scheme not in {'http', 'socks5', 'socks5h'} or not parsed.hostname
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
            raise ValueError()
    except (ValueError, TypeError):
        raise DmmError('填写 HTTP 或 SOCKS5 代理地址，例如 http://127.0.0.1:10808') from None
    return proxy


def dmm_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in {None, 443}
            or not any(host == suffix or host.endswith("." + suffix) for suffix in ("dmm.com", "dmm.co.jp"))):
        raise DmmError("服务器返回了不受支持的 DMM 地址")
    return url


class DmmSession:
    def __init__(self, proxy=DEFAULT_PROXY, root: Path | None = None):
        self.root = root or data_dir()
        from dmmclient.backend.accounts import AccountStore
        self.accounts = AccountStore(self.root)
        active = self.accounts.active()
        self.profile_key = active['key'] if active else None
        self.identity = None
        self.proxy_root = self.root
        self.path = self.root / "session.dpapi"
        self.proxy = valid_proxy(proxy)
        self.cancel_check = lambda: None
        self.progress = lambda message: print(message, flush=True)
        self.http = requests.Session()
        from dmmclient.backend.xray import attach_pool
        attach_pool(self.http, self.proxy)
        self.http.trust_env = False
        self.http.proxies = {"http": self.proxy, "https": self.proxy}
        self.http.headers.update({
            "User-Agent": "DMMGamePlayer5-Win/5.5.21 Electron/44.3.0",
            "Client-App": "DMMGamePlayer5", "Client-Version": "5.5.21",
            "Accept": "application/json", "Accept-Language": "ja",
        })
        self.auth = {}
        self.device = {}
        # 官方 Player 请求商店/API 时始终携带该 cookie（抓包确认，缺失会导致 910）
        self.http.cookies.set("age_check_done", "0", domain=".dmm.com", path="/")
        source = self.accounts.current_path()
        if self.profile_key and not source.exists():
            raise LoginRequired('当前账号的登录文件缺失，请重新登录并保存该账号')
        if source.exists():
            try:
                raw = win32crypt.CryptUnprotectData(source.read_bytes(), None, None, None, 0)[1]
                saved = json.loads(raw)
                self.auth = saved["auth"]
                self.device = saved["device"]
                self.identity = saved.get('identity')
                for cookie in saved.get("cookies", []):
                    self.http.cookies.set_cookie(Cookie(**cookie))
            except Exception:
                raise DmmError("助手会话无法解密，请重新导入或登录") from None
        if not self.device:
            address = secrets.token_hex(6)
            self.device = {"mac_address": ":".join(address[i:i+2] for i in range(0, 12, 2)),
                           "hdd_serial": secrets.token_hex(32), "motherboard": secrets.token_hex(32),
                           "user_os": "win"}

    def save(self):
        cookies = [{name: getattr(c, name) for name in (
            "version", "name", "value", "port", "port_specified", "domain", "domain_specified",
            "domain_initial_dot", "path", "path_specified", "secure", "expires", "discard",
            "comment", "comment_url", "rfc2109")} | {"rest": c._rest} for c in self.http.cookies]
        raw = json.dumps({"auth": self.auth, "device": self.device, "cookies": cookies, 'identity': self.identity}).encode()
        encrypted = win32crypt.CryptProtectData(raw, "ok-gm DMM session", None, None, None, 1)
        self.accounts.persist(self.profile_key, encrypted)

    def accept_identity(self, viewer_id):
        import hashlib
        if self.identity and self.identity.startswith('dmm:'):
            self.verify_game_identity()
            return
        if not isinstance(viewer_id, str) or not viewer_id:
            raise DmmError('服务器未返回有效账号身份')
        identity = hashlib.sha256(viewer_id.encode()).hexdigest()
        if self.identity and self.identity != identity:
            raise DmmError('实际登录账号与所选存档不一致；请使用「新账号登录并保存」记录另一个账号')
        self.identity = identity
        self.save()

    def verify_game_identity(self):
        import hashlib
        legacy = bool(self.identity and not self.identity.startswith('dmm:'))
        if legacy:
            # Existing snapshots were bound to a game viewer. Verify that binding
            # once before migrating to platform identity; never silently rebind.
            from dmmclient.backend.helper import windows_args
            data = self.request('/v5/r2/launch/cl', {'product_id': 'gakumas', 'game_type': 'GCL',
                                'game_os': 'win', 'launch_type': 'LIB', **self.device})
            try:
                viewer = next(a.removeprefix('/viewer_id=') for a in windows_args(data['execute_args'])
                              if a.startswith('/viewer_id='))
            except (KeyError, TypeError, StopIteration):
                raise DmmError('无法验证旧账号存档，请重新登录并保存为新账号') from None
            self.accept_identity(viewer)
        data = self.request('/v5/userinfo', {'user_os':'win'})
        identifier = data.get('open_id') if isinstance(data,dict) else None
        if not isinstance(identifier,str) or not identifier:
            raise DmmError('服务器未返回有效 DMM 平台账号身份')
        identity = 'dmm:' + hashlib.sha256(identifier.encode()).hexdigest()
        if self.identity and not legacy and self.identity != identity:
            raise DmmError('实际登录账号与所选存档不一致，请保存为新账号')
        self.identity=identity
        self.save()

    def import_player(self, source: Path | None = None):
        source = source or dmm_dir()
        try:
            state = json.loads((source / "Local State").read_text(encoding="utf-8"))
            encrypted_key = base64.b64decode(state["os_crypt"]["encrypted_key"])
            if encrypted_key[:5] != b"DPAPI":
                raise ValueError()
            key = win32crypt.CryptUnprotectData(encrypted_key[5:], None, None, None, 0)[1]
            blob = (source / "authAccessTokenData.enc").read_bytes()
            auth = json.loads(self._decrypt(blob, key))
            if not auth.get("accessToken"):
                raise ValueError()
        except Exception:
            raise LoginRequired("读取 DMM 登录态失败，请在助手中登录") from None
        cookie_imported = True
        try:
            cookies = self._player_cookies(source, key)
        except sqlite3.OperationalError:
            # A running DMM Player may exclusively lock its cookie database. Its
            # independent auth file can still be imported; never retain another account's cookies.
            cookies = []
            cookie_imported = False
        self.auth = auth
        self.http.cookies.clear()
        for cookie in cookies:
            self.http.cookies.set_cookie(cookie)
        self.save()
        return cookie_imported

    @staticmethod
    def _decrypt(blob: bytes, key: bytes) -> bytes:
        if blob[:3] != b"v10":
            raise ValueError("Unsupported Electron encryption")
        return AES.new(key, AES.MODE_GCM, blob[3:15]).decrypt_and_verify(blob[15:-16], blob[-16:])

    def _player_cookies(self, source, key):
        database = source / "Network" / "Cookies"
        if not database.exists():
            return []
        cookies = []
        # Read SQLite/WAL through a read-only connection; plaintext stays in memory.
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            version_row = connection.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            version = int(version_row[0]) if version_row else 0
            rows = connection.execute("SELECT host_key,name,path,is_secure,is_httponly,expires_utc,value,encrypted_value FROM cookies")
            for host, name, path, secure, http_only, expires, value, encrypted in rows:
                if not any(host.lstrip('.') == suffix or host.endswith('.' + suffix) for suffix in ('dmm.com', 'dmm.co.jp')):
                    continue
                expiration = int(expires / 1_000_000 - 11644473600) if expires else None
                if expiration and expiration <= time.time():
                    continue
                if encrypted:
                    plaintext = self._decrypt(encrypted, key)
                    if version >= 24:
                        import hashlib
                        if plaintext[:32] != hashlib.sha256(host.encode()).digest():
                            raise ValueError("Cookie domain mismatch")
                        plaintext = plaintext[32:]
                    value = plaintext.decode()
                cookie = requests.cookies.create_cookie(name, value, domain=host, path=path,
                                                        secure=bool(secure), expires=expiration,
                                                        rest={"HttpOnly": bool(http_only)})
                cookies.append(cookie)
        return cookies

    def request(self, path: str, payload=None, *, method="POST"):
        self.cancel_check()
        url = API + path if path.startswith("/") else path
        if urlsplit(dmm_url(url)).hostname != urlsplit(API).hostname:
            raise DmmError("API 地址不属于 DMM Game Player")
        headers = {"actauth": self.auth["accessToken"]} if self.auth.get("accessToken") else {}
        while True:
            try:
                response = self.http.request(method, url, json=payload if method == "POST" else None,
                                             headers=headers, timeout=(10, 45), allow_redirects=False)
                break
            except requests.RequestException as error:
                failover = getattr(self.http, 'proxy_failover', None)
                if failover is not None:
                    # Adapter handles handshake failures; this also handles a
                    # response body interrupted after the headers were received.
                    failover()
                    continue
                if isinstance(error, requests.exceptions.SSLError):
                    raise DmmError("DMM TLS 连接失败，请检查日本节点与代理分流") from None
                raise DmmError("DMM 网络请求失败，请检查本地代理和日本节点") from None
        try:
            # Set-Cookie updates survive both successful and unsuccessful API responses.
            self.save()
            if response.status_code != 200:
                raise DmmError(f"DMM 请求失败：HTTP {response.status_code}")
            result = response.json()
        except ValueError:
            raise DmmError("DMM 返回了无法识别的响应") from None
        code = result.get("result_code")
        if code != 100 and result.get("result") != "success":
            if code == 203:  # 登录态在操作中途失效：走浏览器登录重试，而不是报"拒绝请求"
                raise LoginRequired("登录态已失效，请在浏览器完成登录")
            raise DmmApiError(code)
        data = result.get("data")
        if isinstance(data, dict) and data.get("access_token_info") is not None:
            self.accept_auth(data["access_token_info"])
        self.cancel_check()
        return data

    def user_profile(self):
        """当前登录账号的可读信息，用来给账号起名（DMM GAMES ID + 昵称）。"""
        data = self.request("/v5/userinfo", {"user_os": "win"})
        if not isinstance(data, dict):
            raise DmmError("DMM 没有返回账号信息")
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        number = str(profile.get("dmm_games_id") or data.get("open_id") or "").strip()
        name = str(profile.get("nickname") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", number):
            raise DmmError("DMM 没有返回可用的账号编号")
        return {"number": number, "name": name[:80]}

    def accept_auth(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise DmmError("DMM 没有返回有效的新登录凭证")
        auth = {"accessToken": data["access_token"]}
        if "expires_in_seconds" in data:
            expiration = data["expires_in_seconds"]
            if not isinstance(expiration, int) or isinstance(expiration, bool) or expiration < 0:
                raise DmmError("新登录凭证有效期格式不受支持")
            auth["expiresInSeconds"] = expiration
        self.auth = auth
        self.save()

    def authenticated(self) -> bool:
        if not self.auth.get("accessToken"):
            return False
        payload = {"access_token": self.auth["accessToken"]}
        if "expiresInSeconds" in self.auth:
            payload["expires_in_seconds"] = self.auth["expiresInSeconds"]
        try:
            result = self.request("/v5/auth/accesstoken/check", payload)
        except LoginRequired:
            return False  # token 已失效：交给 ensure_login 的续期流程
        except DmmApiError:
            return False
        return isinstance(result, dict) and result.get("result") is True

    def ensure_login(self):
        if not self.accounts.current_path().exists():
            raise LoginRequired("请先登录或导入现有 DMM 登录态")
        if self.authenticated():
            return
        # Cookie-based OAuth can often renew authorization without manual interaction.
        if self.profile_key:
            if not self.identity:
                raise LoginRequired('该快照尚未确认账号身份，请重新登录并保存为新账号')
            from types import SimpleNamespace
            from dmmclient.backend.launcher import login_candidate, _adopt
            with login_candidate(SimpleNamespace(session=self)) as candidate:
                candidate.login(interactive=False)
                candidate.verify_game_identity()
                _adopt(self, candidate)
        else:
            self.login(interactive=False)

    def _exchange(self, code):
        try:
            data = self.request("/v5/auth/accesstoken/issue", {"code": code})
        except DmmApiError as error:
            if error.code == 348:
                raise LoginRequired("授权码签发失败，可能已过期或被 DMM Player 使用；请重新登录或运行 import 导入当前 DMM 登录态") from None
            raise
        self.accept_auth(data)

    @staticmethod
    def _code(url):
        parsed = urlsplit(url)
        if parsed.hostname == "webdgp-gameplayer.games.dmm.com" and parsed.path == "/login/success":
            return parse_qs(parsed.query).get("code", [None])[0]
        return None

    def login(self, interactive=True, browser="system"):
        data = self.request("/v5/auth/login/url", {"prompt": ""})
        if not isinstance(data, dict) or not data.get("url"):
            raise DmmError("无法获取 DMM 登录页面")
        url = dmm_url(data["url"])
        if not interactive:
            for _ in range(12):
                code = self._code(url)
                if code:
                    self._exchange(code)
                    return
                try:
                    response = self.http.get(dmm_url(url), timeout=(10, 30), allow_redirects=False)
                    self.save()
                except requests.RequestException:
                    raise DmmError("续期登录态时网络连接失败") from None
                if response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get("Location"):
                    break
                from urllib.parse import urljoin
                url = urljoin(url, response.headers["Location"])
            raise LoginRequired("登录已过期，请在「游戏启动器」点击「浏览器登录」")
        if browser == "system":
            self._system_login(url)
        else:
            self._browser_login(url)

    def _system_login(self, url):
        from dmmclient.backend.browser_proxy import temporary_browser_proxy
        with temporary_browser_proxy(self.proxy, self.proxy_root, progress=self.progress):
            self._wait_for_system_login(url)

    def _wait_for_system_login(self, url):
        import ctypes
        import win32clipboard
        # The user requested the normal browser profile, including its saved passwords.
        # Passwords and browser databases are never accessed by this helper.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint32
        last_sequence = user32.GetClipboardSequenceNumber()
        try:
            os.startfile(url)
        except OSError:
            raise DmmError("无法打开默认浏览器，请检查 Windows 默认浏览器设置") from None
        self.progress("请在浏览器完成登录；成功后按 Ctrl+L、Ctrl+C 复制成功页地址。")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            self.cancel_check()
            sequence = user32.GetClipboardSequenceNumber()
            if sequence == last_sequence:
                time.sleep(0.25)
                continue
            copied = ""
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                        copied = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.25)
                continue
            last_sequence = sequence
            # Only a specific DMM OAuth success URL is accepted. Other clipboard data
            # is never logged, saved, transmitted or acted on.
            if isinstance(copied, str) and copied.startswith("https://webdgp-gameplayer.games.dmm.com/login/success?"):
                code = self._code(copied.strip())
                if code:
                    self._exchange(code)
                    return
            time.sleep(0.25)
        raise LoginRequired("等待成功页地址超时，请重新运行 login")

    def _browser_login(self, url):
        from playwright.sync_api import sync_playwright
        code = None
        try:
            with sync_playwright() as playwright:
                # An ephemeral browser; only DPAPI-encrypted cookies are persisted.
                browser = playwright.chromium.launch(channel="msedge", headless=False,
                                                      proxy={"server": self.proxy})
                context = browser.new_context()
                saved_cookies = []
                for c in self.http.cookies:
                    if c.expires and c.expires <= time.time():
                        continue
                    saved_cookies.append({"name": c.name, "value": c.value, "domain": c.domain,
                                          "path": c.path, "secure": c.secure,
                                          "httpOnly": bool(c._rest.get("HttpOnly")),
                                          "expires": c.expires or -1})
                context.add_cookies(saved_cookies)
                page = context.new_page()

                def intercept(route):
                    nonlocal code
                    captured = self._code(route.request.url)
                    if captured:
                        code = captured
                        route.fulfill(status=200, content_type="text/html", body="<h2>ok-gm: Login received. You may close this window.</h2>")
                    else:
                        route.continue_()

                context.route("https://webdgp-gameplayer.games.dmm.com/login/success*", intercept)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                deadline = time.monotonic() + 300
                while not code and time.monotonic() < deadline and not page.is_closed():
                    self.cancel_check()
                    page.wait_for_timeout(200)
                if not code:
                    raise LoginRequired("登录未完成；可重新运行 login")
                self.http.cookies.clear()
                for c in context.cookies():
                    host = c["domain"].lstrip(".")
                    if any(host == suffix or host.endswith("." + suffix) for suffix in ("dmm.com", "dmm.co.jp")):
                        self.http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"],
                                              secure=c["secure"], expires=int(c["expires"]) if c["expires"] > 0 else None,
                                              rest={"HttpOnly": c["httpOnly"]})
                browser.close()
        except DmmError:
            raise
        except Exception:
            raise DmmError("浏览器登录失败，请检查 Edge、代理及登录页面") from None
        self._exchange(code)
