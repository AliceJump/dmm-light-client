from __future__ import annotations

import ctypes
import json
import msvcrt
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from dmmclient.backend.session import DmmError, DmmSession, atomic_write, dmm_dir, dmm_url
from dmmclient.backend.updater import UpdatePlan, VerifiedUpdater, safe_path


@contextmanager
def helper_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "helper.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise DmmError("已有助手正在登录、更新或启动，请等待该操作结束") from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def windows_args(arguments: str) -> list[str]:
    count = ctypes.c_int()
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    pointer = shell.CommandLineToArgvW('game.exe ' + arguments, ctypes.byref(count))
    if not pointer:
        raise DmmError("无法解析游戏启动参数")
    try:
        return [pointer[index] for index in range(1, count.value)]
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(pointer)


def ensure_game_stopped():
    from dmmclient.backend.games import records
    roots=[Path(value['root']).resolve() for value in records().values()]
    for process in psutil.process_iter(["name", "exe"]):
        if (process.info["name"] or "").casefold() == "gakumas.exe":
            raise DmmError("游戏正在运行，请先关闭游戏再更新或重新启动")
        try:
            if process.info.get('exe') and any(Path(process.info['exe']).resolve().is_relative_to(root) for root in roots):
                raise DmmError('游戏正在运行，请先关闭游戏再切换账号')
        except (psutil.Error,OSError): continue


def close_game(progress=lambda message: None, cancel_check=lambda: None):
    """Request a graceful close only for this installation, never force-kill it."""
    import time
    import win32gui
    import win32process
    root, _ = DmmHelper.installed_game()
    expected = (root / 'gakumas.exe').resolve()
    targets = []
    for process in psutil.process_iter(['name', 'exe']):
        if (process.info['name'] or '').casefold() != 'gakumas.exe':
            continue
        if not process.info['exe'] or Path(process.info['exe']).resolve() != expected:
            raise DmmError('检测到无法确认安装路径的游戏，请先手动关闭')
        targets.append(process)
    pids = {p.pid for p in targets}
    if not pids:
        return
    progress('切换任务账号前，正在请求关闭游戏')

    def request_close(hwnd, _):
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] in pids:
                win32gui.PostMessage(hwnd, 0x0010, 0, 0)
        except win32gui.error:
            pass

    win32gui.EnumWindows(request_close, None)
    deadline = time.monotonic() + 20
    while any(p.is_running() for p in targets):
        cancel_check()
        if time.monotonic() >= deadline:
            raise DmmError('游戏未退出，请手动关闭后重试；账号文件尚未切换')
        time.sleep(0.1)


def ensure_update_idle():
    ensure_game_stopped()
    for process in psutil.process_iter(["name"]):
        if (process.info["name"] or "").casefold() in {"dmmgameplayer.exe", "celestite.desktop.exe"}:
            raise DmmError("更新前请退出 DMM Player 和 Celestite，避免同时修改游戏文件")


@dataclass(repr=False)
class LaunchSpec:
    exe: Path
    arguments: list[str] = field(repr=False)
    version: str
    plan: UpdatePlan = field(repr=False)
    sign: str = field(repr=False)


class DmmHelper:
    def __init__(self, proxy, progress=lambda message: None, root=None, download_progress=lambda done, total: None):
        self.session = DmmSession(proxy, root)
        self.progress = progress
        self.session.progress = progress
        self.download_progress = download_progress

    @staticmethod
    def installed_game():
        from dmmclient.backend.session import data_dir
        own = data_dir() / 'installation.json'
        if own.exists():
            try:
                record = json.loads(own.read_text(encoding='utf-8'))
                root = Path(record['root']).resolve()
                if not root.is_dir(): raise ValueError()
                return root, record.get('version', '未知')
            except Exception:
                raise DmmError('游戏安装目录无效，请重新选择目录') from None
        try:
            config = json.loads((dmm_dir() / "dmmgame.cnf").read_text(encoding="utf-8"))
            game = next(item for item in config["contents"] if item["productId"] == "gakumas")
            if not game["detail"]["installed"] or game["gameType"] != "GCL":
                raise ValueError()
            root = Path(game["detail"]["path"]).resolve()
            if not root.is_dir():
                raise ValueError()
            return root, game["detail"]["version"]
        except Exception:
            raise DmmError("DMM 安装配置中未找到已安装的学园偶像大师") from None

    def fetch(self):
        self.progress("检查并续期 DMM 登录态")
        self.session.ensure_login()
        root, installed = self.installed_game()
        self.progress("请求最新版本与启动参数")
        data = self.session.request("/v5/r2/launch/cl", {
            "product_id": "gakumas", "game_type": "GCL", "game_os": "win",
            "launch_type": "LIB", **self.session.device,
        })
        if not isinstance(data, dict):
            raise DmmError("游戏启动响应格式不受支持")
        if data.get("drm_auth_token"):
            raise DmmError("此启动响应要求额外 DRM 流程，请暂用 DMM Player")
        if data.get("is_administrator") and not ctypes.windll.shell32.IsUserAnAdmin():
            raise DmmError("游戏要求管理员权限，请以管理员身份运行助手后重试")
        try:
            exe = safe_path(root, data["exec_file_name"])
            if exe.name.lower() != "gakumas.exe":
                raise DmmError("启动响应不是学园偶像大师")
            arguments = windows_args(data["execute_args"])
            for required in ("/viewer_id=", "/open_id=", "/pf_access_token="):
                if not any(argument.startswith(required) and len(argument) > len(required) for argument in arguments):
                    raise DmmError("服务器没有返回完整游戏启动凭证")
            self.session.accept_identity(next(a.removeprefix('/viewer_id=') for a in arguments
                                              if a.startswith('/viewer_id=')))
            if any("\0" in a or "\n" in a or "\r" in a for a in arguments):
                raise DmmError("启动参数格式不受支持")
            version = data["latest_version"]
            sign = data["sign"]
            file_url = data["file_list_url"]
            if not isinstance(version, str) or not isinstance(sign, str) or not isinstance(file_url, str):
                raise ValueError()
            manifest = self.session.request(file_url, method="GET")
            domain, entries = manifest["domain"], manifest["file_list"]
            # Current client endpoints may paginate; never treat an incomplete page as a full update.
            try:
                pages_data = self.session.request(file_url.rstrip("/") + "/totalpages", method="GET")
            except DmmError as error:
                # Some current manifests are unpaginated and have no totalpages endpoint.
                if "HTTP 404" not in str(error) and "result_code=404" not in str(error):
                    raise
                pages_data = {"total_pages": 1}
            pages = pages_data["total_pages"]
            if not isinstance(pages, int) or not 1 <= pages <= 10000:
                raise DmmError("下载清单分页格式不受支持")
            for page in range(2, pages + 1):
                chunk = self.session.request(file_url + f"?page={page}", method="GET")
                if chunk["domain"] != domain:
                    raise DmmError("下载清单的服务器不一致")
                entries.extend(chunk["file_list"])
            plan = UpdatePlan.parse(root, domain, entries, version)
        except (KeyError, TypeError, ValueError):
            raise DmmError("游戏启动或下载清单格式不受支持") from None
        self.progress(f"DMM 安装记录版本 {installed}；服务器版本 {version}")
        return LaunchSpec(exe, arguments, version, plan, sign)

    def updater(self, spec):
        return VerifiedUpdater(spec.plan, self.session.proxy, spec.sign, self.progress, ensure_update_idle,
                               self.session.cancel_check, self.download_progress)

    def check(self):
        spec = self.fetch()
        # Repair an interrupted commit before inspecting the installed files.
        updater = self.updater(spec)
        if updater.journal.exists():
            ensure_game_stopped()
            updater.recover()
        pending = spec.plan.pending()
        result = {"latest_version": spec.version, "files": len(spec.plan.files),
                  "pending_files": len(pending), "download_bytes": sum(f.size for f in pending if not f.delete),
                  "delete_files": sum(f.delete for f in pending), "exe_present": spec.exe.is_file()}
        if not pending:
            # 本地文件与最新清单一致：游戏已经是这个版本了（可能被游戏自己更新过）
            self._remember_version(spec)
        self.progress(f"版本 {spec.version}：需更新 {len(pending)} 个文件，下载 {result['download_bytes'] / 1024 / 1024:.1f} MiB")
        return result

    def _remember_version(self, spec):
        atomic_write(self.session.root / "installation.json", json.dumps({
            "root": str(spec.plan.root), "version": spec.version,
        }).encode())

    def prepare(self, auto_update=True):
        ensure_game_stopped()
        spec = self.fetch()
        updater = self.updater(spec)
        updater.recover()
        if spec.plan.pending():
            if not auto_update:
                raise DmmError("游戏文件需要更新，请启用自动更新或运行 python -m dmmclient.backend update")
            ensure_update_idle()
            updater.run()
            # Launch tokens obtained before a potentially long update are not reused.
            spec = self.fetch()
            if spec.plan.pending():
                raise DmmError("更新期间服务器版本已改变，请重试")
        # 文件已与最新清单一致，把版本记下来（游戏自己更新过时也能纠正）
        self._remember_version(spec)
        if not spec.exe.is_file():
            raise DmmError("游戏可执行文件不存在")
        return spec

    def launch(self, auto_update=True):
        spec = self.prepare(auto_update)
        ensure_game_stopped()
        try:
            process = subprocess.Popen([str(spec.exe), *spec.arguments], cwd=spec.exe.parent, shell=False,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            raise DmmError("游戏进程启动失败，请检查文件占用和运行权限") from None
        self.progress(f"游戏进程已创建，PID={process.pid}；窗口出现后仍需确认游戏登录成功")
        return process
