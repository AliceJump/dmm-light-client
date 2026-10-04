"""Isolated Xray process with one encrypted outbound and a loopback HTTP inlet."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
import socket
import subprocess
import threading
import time
import zipfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import psutil
import requests
import win32api
import win32crypt
import win32job

from dmmclient.backend.session import DmmError, atomic_write, data_dir, valid_proxy

NODE_ENABLED_KEY = '使用独立 Xray 节点'
PROTOCOLS = {'vless', 'vmess', 'trojan', 'shadowsocks'}
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).decode()


def _stream(query):
    network = query.get('type', ['tcp'])[0]
    security = query.get('security', ['none'])[0]
    if network not in {'tcp', 'raw', 'ws', 'grpc', 'httpupgrade', 'xhttp'} or security not in {'none', 'tls', 'reality'}:
        raise DmmError('该分享链接的传输格式暂不支持，请导入 Xray JSON 节点配置')
    result = {'network': network, 'security': security}
    get = lambda key, default='': query.get(key, [default])[0]
    if security == 'tls':
        settings = {'serverName': get('sni'), 'fingerprint': get('fp', 'chrome')}
        if get('alpn'):
            settings['alpn'] = get('alpn').split(',')
        result['tlsSettings'] = settings
    elif security == 'reality':
        result['realitySettings'] = {'serverName': get('sni'), 'fingerprint': get('fp', 'chrome'),
                                     'publicKey': get('pbk'), 'shortId': get('sid')}
    if network in {'ws', 'httpupgrade', 'xhttp'}:
        settings = {'path': get('path', '/')}
        if get('host'):
            if network == 'ws':
                settings['headers'] = {'Host': get('host')}
            else:
                settings['host'] = get('host')
        result[network + 'Settings'] = settings
    elif network == 'grpc':
        result['grpcSettings'] = {'serviceName': get('serviceName')}
    return result


def parse_node(text):
    """Never include submitted text or parser exceptions in errors."""
    try:
        if not isinstance(text, str) or len(text) > 1024 * 1024:
            raise ValueError()
        text = text.strip()
        if text.startswith('{'):
            raw = json.loads(text)
            if 'outbounds' in raw:
                candidates = [o for o in raw['outbounds'] if o.get('protocol') in PROTOCOLS]
                if len(candidates) != 1:
                    raise DmmError('JSON 必须只包含一个可独立使用的代理出站节点')
                raw = candidates[0]
            name = raw.get('remarks') or raw.get('tag') or '导入节点'
            outbound = raw
        elif text.startswith('vmess://'):
            raw = json.loads(_decode(text[8:]))
            name = raw.get('ps') or 'VMess'
            outbound = {'protocol': 'vmess', 'settings': {'vnext': [{'address': raw['add'], 'port': int(raw['port']),
                'users': [{'id': raw['id'], 'alterId': int(raw.get('aid', 0)), 'security': raw.get('scy', 'auto')}]}]},
                'streamSettings': _stream({k: [v] for k, v in {'type': raw.get('net', 'tcp'),
                    'security': 'tls' if raw.get('tls') == 'tls' else 'none', 'sni': raw.get('sni', ''),
                    'host': raw.get('host', ''), 'path': raw.get('path', '/')}.items()})}
        else:
            parsed = urlsplit(text)
            name = unquote(parsed.fragment) or parsed.scheme.upper()
            if parsed.scheme in {'vless', 'trojan'}:
                if not parsed.hostname or not parsed.port or not parsed.username:
                    raise ValueError()
                query = parse_qs(parsed.query)
                if parsed.scheme == 'trojan':
                    query.setdefault('security', ['tls'])
                    settings = {'servers': [{'address': parsed.hostname, 'port': parsed.port,
                                              'password': unquote(parsed.username)}]}
                else:
                    user = {'id': unquote(parsed.username), 'encryption': query.get('encryption', ['none'])[0]}
                    if query.get('flow'):
                        user['flow'] = query['flow'][0]
                    settings = {'vnext': [{'address': parsed.hostname, 'port': parsed.port, 'users': [user]}]}
                outbound = {'protocol': parsed.scheme, 'settings': settings, 'streamSettings': _stream(query)}
            elif parsed.scheme == 'ss':
                if parsed.query:
                    raise DmmError('带插件的 Shadowsocks 链接暂不支持，请导入 Xray JSON 节点配置')
                body = text[5:].split('#', 1)[0]
                if '@' not in body:
                    body = _decode(body)
                credential, address = body.rsplit('@', 1)
                if ':' not in credential:
                    credential = _decode(credential)
                method, password = unquote(credential).split(':', 1)
                server = urlsplit('ss://' + address)
                outbound = {'protocol': 'shadowsocks', 'settings': {'servers': [{'address': server.hostname,
                             'port': server.port, 'method': method, 'password': password}]}}
            else:
                raise DmmError('支持 VLESS、VMess、Trojan、Shadowsocks 分享链接或 Xray JSON')
        if outbound.get('protocol') not in PROTOCOLS or not isinstance(outbound.get('settings'), dict):
            raise ValueError()
        if 'proxySettings' in outbound:
            raise DmmError('节点依赖其他出站，请导入可独立使用的节点')
        outbound = {key: copy.deepcopy(outbound[key]) for key in ('protocol', 'settings', 'streamSettings', 'mux') if key in outbound}
        # Ignore imported listeners, routes, API, DNS and log destinations. Reject
        # local file references and dependencies on another outbound.
        def inspect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {'certificateFile', 'keyFile', 'dialerProxy', 'proxySettings', 'sockopt'}:
                        raise DmmError('节点依赖本地文件或其他出站，请导入可独立使用的节点')
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        inspect(outbound)
        outbound['tag'] = 'fixed-node'
        if not isinstance(name, str):
            name = '导入节点'
        name = ''.join(c for c in name if ord(c) >= 32)[:80] or '导入节点'
        return {'name': name, 'outbound': outbound}
    except DmmError:
        raise
    except Exception:
        raise DmmError('节点配置格式无效；敏感内容不会写入日志') from None


def runtime_config(node, port):
    return {'log': {'loglevel': 'none'}, 'inbounds': [{'tag': 'launcher-only', 'listen': '127.0.0.1',
             'port': port, 'protocol': 'http', 'settings': {}}], 'outbounds': [node['outbound']]}


class NodeStore:
    def __init__(self, root=None):
        self.root = root or data_dir()
        self.path = self.root / 'node.dpapi'
        self.exe = self.root / 'xray-core' / 'xray.exe'

    def read(self):
        try:
            raw = win32crypt.CryptUnprotectData(self.path.read_bytes(), None, None, None, 0)[1]
            saved = json.loads(raw)
            if 'outbound' in saved:
                saved = {'nodes': [dict(saved, id='legacy', enabled=True)], 'next': 'legacy'}
            nodes, ids = [], set()
            for entry in saved['nodes']:
                if not isinstance(entry['id'], str) or entry['id'] in ids:
                    raise ValueError()
                checked = parse_node(json.dumps(entry['outbound']))
                checked.update(name=str(entry['name'])[:80], id=entry['id'], enabled=bool(entry.get('enabled', True)))
                nodes.append(checked)
                ids.add(entry['id'])
            if len(nodes) > 32 or saved.get('next') not in ids | {None}:
                raise ValueError()
            return {'nodes': nodes, 'next': saved.get('next')}
        except Exception:
            raise DmmError('独立节点尚未配置或无法解密，请重新导入节点') from None

    def load(self):
        return self.ordered()[0]

    def ordered(self):
        saved = self.read()
        nodes = [node for node in saved['nodes'] if node['enabled']]
        if not nodes:
            raise DmmError('没有启用的独立节点，请导入或启用节点')
        start = next((i for i, node in enumerate(nodes) if node['id'] == saved['next']), 0)
        return nodes[start:] + nodes[:start]

    def write(self, saved):
        payload = json.dumps(saved).encode()
        atomic_write(self.path, win32crypt.CryptProtectData(payload, 'ok-gm Xray nodes', None, None, None, 1))

    def save(self, node):
        saved = self.read() if self.path.exists() else {'nodes': [], 'next': None}
        existing = next((n for n in saved['nodes'] if n['outbound'] == node['outbound']), None)
        if existing:
            existing.update(name=node['name'], enabled=True)
        else:
            if len(saved['nodes']) >= 32:
                raise DmmError('最多保存 32 个节点，请先移除不再使用的节点')
            saved['nodes'].append(dict(node, id=uuid.uuid4().hex, enabled=True))
        if not saved['next']:
            saved['next'] = saved['nodes'][0]['id']
        self.write(saved)

    def change(self, node_id, action):
        saved = self.read()
        entry = next((n for n in saved['nodes'] if n['id'] == node_id), None)
        if not entry:
            raise DmmError('请选择已保存的节点')
        if action == 'select':
            entry['enabled'] = True
            saved['next'] = node_id
        elif action == 'toggle':
            entry['enabled'] = not entry['enabled']
        elif action == 'remove':
            saved['nodes'].remove(entry)
            if saved['next'] == node_id:
                saved['next'] = next((n['id'] for n in saved['nodes'] if n['enabled']), None)
        else:
            raise DmmError('未知节点操作')
        self.write(saved)

    def advance(self, node_id):
        saved = self.read()
        nodes = [n for n in saved['nodes'] if n['enabled']]
        for index, entry in enumerate(nodes):
            if entry['id'] == node_id:
                saved['next'] = nodes[(index + 1) % len(nodes)]['id']
                self.write(saved)
                return

    def pin(self, node_id):
        """记住最近可用的节点：下次连接（含重启软件后）优先使用它。"""
        saved = self.read()
        if saved.get('next') != node_id:
            saved['next'] = node_id
            self.write(saved)

    def install(self, bootstrap_proxy, progress=lambda message: None):
        if self.exe.is_file():
            return self.exe
        bundled = Path(__file__).resolve().parents[2] / 'vendor/xray/xray.exe'
        local = Path('D:/Programs/v2rayn/bin/xray/xray.exe')
        for source in (bundled, local):
            if source.is_file():
                data = source.read_bytes()
                if not data.startswith(b'MZ'):
                    raise DmmError('本地 Xray 内核不是有效 Windows 程序')
                atomic_write(self.exe, data)
                progress('独立 Xray 内核已复制；后续运行不依赖 v2rayN')
                return self.exe
        progress('下载官方 Xray Windows 内核')
        try:
            http = requests.Session()
            http.trust_env = False
            http.proxies = {'http': valid_proxy(bootstrap_proxy), 'https': valid_proxy(bootstrap_proxy)}
            response = http.get('https://api.github.com/repos/XTLS/Xray-core/releases/latest', timeout=(10, 30))
            response.raise_for_status()
            release = response.json()
            archive = next(a for a in release['assets'] if a['name'] == 'Xray-windows-64.zip')
            url = archive['browser_download_url']
            if not url.startswith('https://github.com/XTLS/Xray-core/releases/download/'):
                raise ValueError()
            response = http.get(url, timeout=(10, 60))
            response.raise_for_status()
            data = response.content
            if len(data) > 128 * 1024 * 1024:
                raise ValueError()
            digest = archive.get('digest') or ''
            if digest.startswith('sha256:'):
                expected = digest[7:]
            else:
                dgst = next(a for a in release['assets'] if a['name'] == archive['name'] + '.dgst')
                digest_url = dgst['browser_download_url']
                if not digest_url.startswith('https://github.com/XTLS/Xray-core/releases/download/'):
                    raise ValueError()
                response = http.get(digest_url, timeout=(10, 30))
                response.raise_for_status()
                expected = re.search(r'(?im)^SHA256\s*=\s*([a-f0-9]{64})\s*$', response.text)[1]
            if not re.fullmatch(r'[a-fA-F0-9]{64}', expected) or hashlib.sha256(data).hexdigest() != expected.lower():
                raise ValueError()
            with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
                item = next(i for i in zip_file.infolist() if i.filename.lower() == 'xray.exe')
                if item.file_size > 128 * 1024 * 1024:
                    raise ValueError()
                executable = zip_file.read(item)
            if not executable.startswith(b'MZ'):
                raise ValueError()
            atomic_write(self.exe, executable)
        except Exception:
            raise DmmError('官方 Xray 下载或 SHA-256 校验失败；可将可信内核放到 xray-core/xray.exe') from None
        return self.exe

    def import_node(self, text, bootstrap_proxy, progress=lambda message: None):
        node = parse_node(text)
        exe = self.install(bootstrap_proxy, progress)
        try:
            result = subprocess.run([str(exe), 'run', '-test', '-c', 'stdin:', '-format', 'json'],
                input=json.dumps(runtime_config(node, 18080)).encode(), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15, creationflags=NO_WINDOW)
            if result.returncode:
                raise ValueError()
        except Exception:
            raise DmmError('节点未通过 Xray 配置校验；请检查节点格式，详细错误因含有凭据而隐藏') from None
        self.save(node)
        progress('固定节点已加密保存')
        return node


@contextmanager
def node_process(node, exe, port, progress, cancel_check):
    process, job, ready = None, None, False
    try:
        cancel_check()
        job = win32job.CreateJobObject(None, '')
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        process = subprocess.Popen([str(exe), 'run', '-c', 'stdin:', '-format', 'json'], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        handle = win32api.OpenProcess(0x0100 | 0x0001, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        process.stdin.write(json.dumps(runtime_config(node, port)).encode())
        process.stdin.close()
        deadline = time.monotonic() + 10
        owner = psutil.Process(process.pid)
        while time.monotonic() < deadline:
            cancel_check()
            if process.poll() is not None:
                raise DmmError('独立 Xray 启动失败，请重新导入节点或检查内核')
            if any(c.status == psutil.CONN_LISTEN and c.laddr.ip == '127.0.0.1' and c.laddr.port == port
                   for c in owner.net_connections(kind='tcp')):
                progress('独立 Xray 已启动')
                ready = True
                yield f'http://127.0.0.1:{port}'
                return
            time.sleep(0.1)
        raise DmmError('独立 Xray 本地入口启动超时')
    except DmmError:
        raise
    except (OSError, psutil.Error, win32api.error):
        if ready:
            raise
        raise DmmError('独立 Xray 进程异常；不会回退到其他代理或直连') from None
    finally:
        try:
            if process is not None:
                try:
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                except OSError:
                    pass
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    # Closing the job below also terminates an unresponsive core.
                    pass
        finally:
            if job is not None:
                job.Close()


class ProxyAddress(str):
    def __new__(cls, value, pool):
        result = super().__new__(cls, value)
        result.pool = pool
        return result


class NodePool:
    """One bounded pass over saved nodes, keeping the browser's proxy port stable."""
    def __init__(self, store, exe, port, progress, cancel_check):
        self.store, self.exe, self.port = store, exe, port
        self.progress, self.cancel_check = progress, cancel_check
        self.nodes = store.ordered()
        self.index, self.context, self.recorded = -1, None, False

    def close(self):
        if self.context is not None:
            context, self.context = self.context, None
            context.__exit__(None, None, None)

    def failover(self):
        self.close()
        while self.index + 1 < len(self.nodes):
            self.cancel_check()
            self.index += 1
            node = self.nodes[self.index]
            self.progress(f"尝试节点 {self.index + 1}/{len(self.nodes)}：{node['name']}")
            context = node_process(node, self.exe, self.port, self.progress, self.cancel_check)
            try:
                context.__enter__()
            except DmmError as error:
                from dmmclient.backend.session import OperationCancelled
                if isinstance(error, OperationCancelled):
                    raise
                continue
            self.context, self.recorded = context, False
            return
        raise DmmError('所有启用节点均已尝试，连接仍失败；已停止操作并保留下载断点')

    def success(self):
        if not self.recorded:
            self.store.pin(self.nodes[self.index]['id'])
            self.recorded = True


class PoolAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, pool):
        super().__init__(max_retries=0)
        self.pool = pool

    def send(self, request, **kwargs):
        while True:
            self.pool.cancel_check()
            try:
                response = super().send(request, **kwargs)
            except requests.RequestException:
                self.close()
                self.pool.failover()
                continue
            if response.status_code in {403, 502, 503, 504}:
                response.close()
                self.close()
                self.pool.failover()
                continue
            self.pool.success()
            return response


def attach_pool(http, proxy):
    pool = getattr(proxy, 'pool', None)
    if pool is not None:
        http.mount('http://', PoolAdapter(pool))
        http.mount('https://', PoolAdapter(pool))
        http.proxy_failover = pool.failover
        http.proxy_attempts = len(pool.nodes) + 2


class ProxyHub:
    """常驻代理管理器：Xray 进程跨操作复用，最近可用节点粘性优先。

    首次操作仍需扫描节点；之后只要进程存活、配置未变，后续操作立即返回
    现成地址（无端口绑定、无节点扫描）。端口在进程生命周期内保持不变，
    内嵌浏览器（Chromium 代理参数只能设置一次）因此可以稳定复用。
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._fingerprint = None
        self._pool = None
        self._address = None
        self._port = None

    @staticmethod
    def _fingerprint_of(settings):
        return json.dumps({key: settings.get(key) for key in (NODE_ENABLED_KEY, 'proxy')},
                          ensure_ascii=False, sort_keys=True)

    def _alive(self):
        if self._pool is None or self._address is None: return False
        parsed = urlsplit(str(self._address))
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
                return True
        except OSError:
            return False

    @contextmanager
    def acquire(self, settings, progress=lambda message: None, cancel_check=lambda: None, root=None):
        if not settings.get(NODE_ENABLED_KEY, False):
            yield valid_proxy(settings['proxy']); return
        root = root or data_dir()
        fingerprint = self._fingerprint_of(settings)
        with self._lock:
            if self._pool is None or fingerprint != self._fingerprint or not self._alive():
                if self._pool is not None:
                    self._pool.close(); self._pool = None
                store = NodeStore(root)
                exe = store.install(settings['proxy'], progress)
                if self._port is None:
                    with socket.socket() as inlet:
                        inlet.bind(('127.0.0.1', 0))
                        self._port = inlet.getsockname()[1]
                pool = NodePool(store, exe, self._port, progress, cancel_check)
                pool.failover()
                self._pool = pool
                self._address = ProxyAddress(f'http://127.0.0.1:{self._port}', pool)
                self._fingerprint = fingerprint
            else:
                # 复用常驻进程：仅同步当前操作的进度与取消回调
                self._pool.progress, self._pool.cancel_check = progress, cancel_check
        yield self._address

    def shutdown(self):
        with self._lock:
            if self._pool is not None: self._pool.close()
            self._pool, self._address, self._fingerprint = None, None, None


HUB = ProxyHub()


def operation_proxy(settings, progress=lambda message: None, cancel_check=lambda: None, root=None):
    """兼容旧签名：现在通过常驻 ProxyHub 获取代理地址（返回上下文管理器）。"""
    return HUB.acquire(settings, progress, cancel_check, root)
