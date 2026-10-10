"""Explicitly supported downloadable games, with independent install records."""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urljoin

import psutil
import requests

from .helper import windows_args
from .session import DmmApiError, DmmError, atomic_write, data_dir, dmm_dir, dmm_url
from .settings import game_install_root, remember_install_base
from .updater import UpdatePlan, VerifiedUpdater, safe_path

SUPPORTED = {'priconner': '公主连结 Re:Dive（日服）'}


class StorePageNeeded(DmmError):
    """314：该游戏还不在账号的 DMM 游戏库，需要先在商店页点一次「ゲーム開始」。"""
    def __init__(self, url, title):
        self.url = url
        super().__init__('DMM 要求先将该游戏加入游戏库：已在本客户端打开 DMM 游戏商店，'
                         f'请在商店内搜索「{title}」进入详情页点击「ゲームを始める」'
                         '（可能需要年龄认证），完成后重新点「全新安装」')


def checked_product(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',value):
        raise DmmError('游戏编号无效')
    return value


def catalog():
    path=data_dir()/'catalog.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def set_age_cookie(session, verified):
    """age_check_done=1 表示已完成年龄认证；Fanza 目录与 ACL 游戏的全部调用都要求它。"""
    session.http.cookies.set('age_check_done', '1' if verified else '0', domain='.dmm.com', path='/')


def refresh_catalog(session):
    session.ensure_login()
    result={}
    for adult in (False, True):
        set_age_cookie(session, adult)
        try:
            page=1
            while True:
                session.cancel_check()
                query=urlencode({'floor':'free','article':'download:1','limit':120,'sort':'date','os':'win','page':page,'isAdult':'true' if adult else 'false'})
                data=session.request('/v5/view/search?'+query,method='GET')
                items=data.get('list')
                total=data.get('total_hit_count')
                if not isinstance(items,list) or not isinstance(total,int) or not 0<=total<=12000:
                    raise DmmError('游戏列表格式不受支持')
                for item in items:
                    product=checked_product(item['product_id'])
                    title=item.get('title')
                    if not isinstance(title,str) or not title: raise DmmError('游戏名称无效')
                    image=item.get('image_url')
                    genre=item.get('genre'); maker=item.get('maker')
                    link=item.get('link') or {}
                    link_url=link.get('url') if isinstance(link,dict) else None
                    result[product]={'product':product,'title':title[:240],'adult':adult,
                                     'image_url':image if isinstance(image,str) and image.startswith('http') else '',
                                     'genre':genre[:40] if isinstance(genre,str) else '',
                                     'maker':maker[:60] if isinstance(maker,str) else '',
                                     'link':link_url[:200] if isinstance(link_url,str) and link_url.startswith('/') else ''}
                if page*120>=total: break
                if not items or page>=100: raise DmmError('游戏列表分页不完整')
                page+=1
        except DmmError as error:
            if not adult: raise
            session.progress('Fanza 游戏列表获取失败，已跳过：'+str(error))
    set_age_cookie(session, False)
    value=list(result.values())
    atomic_write(data_dir()/'catalog.json',json.dumps(value,ensure_ascii=False).encode())
    return value


def cover_path(root, product):
    return root / 'covers' / (checked_product(product) + '.jpg')


def catalog_image_url(product):
    """Cover URL provided by the search API; far more reliable than the detail page."""
    try:
        for item in catalog():
            if item['product'] == product: return item.get('image_url') or None
    except (DmmError, ValueError): pass
    return None


def official_record(product):
    """官方 Player（dmmgame.cnf）里已安装的同款游戏记录，可被本客户端接管更新与启动。"""
    try:
        config=json.loads((dmm_dir()/'dmmgame.cnf').read_text(encoding='utf-8'))
        entry=next(item for item in config['contents'] if item.get('productId')==product)
        detail=entry.get('detail') or {}
        if not detail.get('installed') or entry.get('gameType')!='GCL': return None
        root=detail.get('path')
        if not root: return None
        return {'root':str(root),'version':str(detail.get('version','未知')),'title':str(detail.get('title') or product)}
    except Exception:
        return None


def installed_record(product):
    """本客户端或官方 Player 安装过的游戏记录（gakumas 走专用流程，不在此列）。"""
    return records().get(product) or official_record(product)


def official_records():
    """官方 Player 里全部已安装的 GCL 游戏。"""
    result={}
    try:
        config=json.loads((dmm_dir()/'dmmgame.cnf').read_text(encoding='utf-8'))
        for entry in config.get('contents',[]):
            product=entry.get('productId')
            detail=entry.get('detail') or {}
            if not product or not detail.get('installed') or entry.get('gameType')!='GCL': continue
            if not detail.get('path'): continue
            try: checked_product(product)
            except DmmError: continue
            result[product]={'root':str(detail['path']),'version':str(detail.get('version','未知')),
                             'title':str(detail.get('title') or product)}
    except Exception:
        return {}
    return result


def store_detail_url(product):
    """游戏商店详情页：DMM GAMES 已迁移到 games.dmm.co.jp（旧 .com 域名子路径 404）。

    系统浏览器里用户已登录 DMM，可直接在该页点「ゲーム開始」加库。
    """
    return f'https://games.dmm.co.jp/detail/{product}'


def save_cover(http, product, root, progress=lambda message: None):
    """Fetch the game key visual: prefer the API image_url, fall back to detail pages."""
    checked_product(product)
    headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'}

    def grab(url):
        image=http.get(url,headers=headers,timeout=(10,60))
        head=image.content[:12]
        if image.status_code!=200 or len(image.content)<512 or not (
                head[:3]==b'\xff\xd8\xff' or head[:8]==b'\x89PNG\r\n\x1a\n'
                or head[:4]==b'GIF8' or head[:4]==b'RIFF'):
            raise DmmError('封面图片格式无效')
        atomic_write(cover_path(root,product),image.content)
        return cover_path(root,product)

    direct=catalog_image_url(product)
    if direct:
        try:
            progress('下载封面图片')
            return grab(direct)
        except (DmmError,requests.RequestException): pass
    for base in ('https://games.dmm.com/detail/', 'https://games.dmm.co.jp/detail/'):
        try:
            progress('获取游戏封面图')
            page=http.get(base+product,headers=headers,timeout=(10,30))
            if page.status_code!=200: raise DmmError(f'详情页 HTTP {page.status_code}')
            match=re.search(r'<meta property="og:image" content="([^"]+)"',page.text)
            if not match: raise DmmError('详情页没有封面信息')
            progress('下载封面图片')
            return grab(unescape(match.group(1)))
        except (DmmError,requests.RequestException):
            continue
    raise DmmError('封面获取失败，请刷新游戏列表后重试')


def records():
    path = data_dir() / 'games.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def game_root(product):
    """已安装游戏的当前目录（本客户端记录优先，其次官方 Player 记录）。"""
    record = records().get(product) or official_record(product)
    root = (record or {}).get('root')
    return Path(root).resolve() if root else None


def game_running(root):
    """该目录下是否有进程正在运行。"""
    target = Path(root).resolve()
    for process in psutil.process_iter(['exe']):
        try:
            exe = process.info.get('exe')
            if exe and Path(exe).resolve().is_relative_to(target):
                return True
        except (psutil.Error, OSError):
            continue
    return False


def record_version(product, version, root=None):
    """把核对出来的真实版本写回安装记录（游戏自己更新过时记录会过期）。"""
    value = records()
    if product in value:
        value[product] = {**value[product], 'version': str(version)}
        atomic_write(data_dir() / 'games.json', json.dumps(value, ensure_ascii=False).encode())
        return
    if product == 'gakumas':
        path = data_dir() / 'installation.json'
        try:
            current = json.loads(path.read_text(encoding='utf-8'))
            root = root or current.get('root')
        except Exception:
            pass
        if root:
            atomic_write(path, json.dumps({'root': str(root), 'version': str(version)}).encode())


def verify_version(session, product, root, progress=lambda message: None):
    """核对已安装游戏的真实版本：本地文件与最新清单一致就以服务器版本为准。

    游戏自己更新（或第三方启动器更新）时本地记录会过期，这里能把它纠正回来。
    返回 {'latest': 服务器最新版本, 'pending': 待更新文件数, 'version': 真实版本或 None}。
    """
    service = GameService(session, product, progress, lambda done, total: None)
    info = service.request_info(installing=False)
    pending = service.plan(info, Path(root)).pending()
    latest = info.get('latest_version')
    return {'latest': latest, 'pending': len(pending),
            'version': latest if not pending else None}


def _safe_to_remove(root):
    """只允许删游戏自己的目录：不碰盘符根、用户主目录，也不碰数据目录所在的位置。"""
    try:
        root = Path(root).resolve()
    except OSError:
        return False
    if not root.is_dir() or root.parent == root:
        return False
    try:
        if root == Path.home().resolve(): return False
        if data_dir().resolve().is_relative_to(root): return False
    except OSError:
        return False
    return True


def _plain(value):
    return re.sub(r'<br\s*/?>', '\n', str(value or '')).strip()


def _normalize_detail(data):
    """把 /v5/detail/product 的响应整理成详情页好用的形状。"""
    info = []
    for table in data.get('link_tables') or []:
        if not isinstance(table, dict): continue
        key = str(table.get('disp_key') or '').strip()
        values = [str(item.get('disp_value') or '').strip()
                  for item in (table.get('disp_items') or []) if isinstance(item, dict)]
        values = [value for value in values if value]
        if key and values: info.append({'key': key, 'value': ' / '.join(values)})
    return {
        'title': str(data.get('title') or '').strip(),
        'description': _plain(data.get('content_text')),
        'captions': [str(item) for item in (data.get('captions') or []) if item],
        'images': [{'thumb': item.get('thumbnail_url'), 'full': item.get('image_url')}
                   for item in (data.get('gallery_images') or [])
                   if isinstance(item, dict) and item.get('image_url')],
        'movies': [{'thumb': item.get('thumbnail'), 'mp4': item.get('mp4_url')}
                   for item in (data.get('gallery_movies') or []) if isinstance(item, dict)],
        'info': info,
        'specs': [{'os': item.get('target_os'), 'html': _plain(item.get('spec_html'))}
                  for item in (data.get('specs') or []) if isinstance(item, dict)],
        'price': data.get('price'), 'new': bool(data.get('is_new')),
        'premium': bool(data.get('is_premium')),
    }


def store_detail(session, product, adult=False):
    """商店商品详情：官方 /v5/detail/product 直接给简介、截图、宣传片、提供元、运行环境。

    不用抓网页。结果按商品缓存在 details/ 下；R18 要带年龄认证 cookie。
    """
    cache = data_dir() / 'details' / f'{product}.json'
    if cache.is_file():
        try:
            value = json.loads(cache.read_text(encoding='utf-8'))
            if isinstance(value, dict) and value.get('title'): return value
        except Exception:
            pass
    payload = {'product_id': product, 'game_os': 'win', **session.device}
    for game_type, verified in ((('ACL' if adult else 'GCL'), adult),
                                (('GCL' if adult else 'ACL'), not adult)):
        try:
            set_age_cookie(session, verified)
            data = session.request('/v5/detail/product', {**payload, 'game_type': game_type})
        except DmmError:
            continue
        if isinstance(data, dict) and data.get('title'):
            value = _normalize_detail(data); value['game_type'] = game_type
            try:
                links = session.request('/v5/detail/links', {**payload, 'game_type': game_type})
                if isinstance(links, dict):
                    value['links'] = [{'url': item.get('url'), 'text': item.get('text')}
                                      for item in (links.get('link_list') or [])
                                      if isinstance(item, dict) and item.get('url')]
            except DmmError:
                pass
            atomic_write(cache, json.dumps(value, ensure_ascii=False).encode())
            return value
    return None


def fetch_shot(http, url, target):
    """下载详情页的截图/宣传图到本地缓存（图片走 https，宣传片是 http 就不下）。"""
    if target.is_file(): return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with http.get(dmm_url(url), stream=True, timeout=(10, 60)) as response:
        if response.status_code != 200:
            raise DmmError(f'图片下载失败：HTTP {response.status_code}')
        temporary = target.with_name(target.name + '.part')
        with temporary.open('wb') as output:
            for block in response.iter_content(128 * 1024):
                if block: output.write(block)
        os.replace(temporary, target)
    return target


def uninstall_game(product, keep_files=False, progress=lambda done, total: None,
                   cancel_check=lambda: None, message=lambda text: None):
    """卸载本客户端安装的游戏；keep_files=True 时只移除安装记录，文件留着。

    只删记录里那一个目录，先删文件再删空目录；每一步都检查取消。
    返回 {'files': 删除的文件数, 'bytes': 释放的字节数}。
    """
    value = records()
    record = value.get(product)
    if not record or not record.get('root'):
        raise DmmError('该游戏不是由本客户端安装的，无法卸载')
    root = Path(record['root']).resolve()
    if game_running(root):
        raise DmmError('该游戏正在运行，请先关闭游戏再卸载')
    removed = freed = 0
    if not keep_files:
        if not _safe_to_remove(root):
            raise DmmError(f'安装目录不安全，已拒绝删除：{root}')
        targets = [path for path in root.rglob('*') if path.is_file()]
        total = sum(path.stat().st_size for path in targets)
        message(f'删除 {len(targets)} 个文件 · {total / 1048576:.1f} MiB')
        done = 0
        for path in targets:
            cancel_check()
            try:
                size = path.stat().st_size
                path.unlink(); removed += 1; freed += size
            except OSError:
                continue
            done += size
            progress(done, total or 1)
        cancel_check()
        for folder in sorted((p for p in root.rglob('*') if p.is_dir()),
                             key=lambda p: len(p.parts), reverse=True):
            try: folder.rmdir()
            except OSError: pass
        try: root.rmdir()
        except OSError: pass
    value.pop(product, None)
    atomic_write(data_dir() / 'games.json', json.dumps(value, ensure_ascii=False).encode())
    return {'files': removed, 'bytes': freed}


def move_game(product, base, progress=lambda done, total: None,
              cancel_check=lambda: None, message=lambda text: None):
    """把已安装的游戏整体搬到新的基准目录（装到 <base>/<product>），跨盘可用。

    先完整复制、最后才删源目录：中途失败或取消时源目录依然完整。
    """
    record = records().get(product)
    if not record or not record.get('root'):
        raise DmmError('该游戏不是由本客户端安装的，无法移动目录')
    source = Path(record['root']).resolve()
    target = Path(base).expanduser().resolve() / product
    if source == target:
        message('游戏已经在该目录下')
        return target
    if target.exists() and any(target.iterdir()):
        raise DmmError('目标目录不为空，请换一个位置')
    if game_running(source):
        raise DmmError('该游戏正在运行，请先关闭游戏再移动目录')
    files = [path for path in source.rglob('*') if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    message(f'移动 {len(files)} 个文件 · {total / 1048576:.1f} MiB 到 {target}')
    done = 0
    for path in files:
        cancel_check()
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        done += path.stat().st_size
        progress(done, total)
    cancel_check()
    shutil.rmtree(source)
    value = records()
    value[product] = {**value[product], 'root': str(target)}
    atomic_write(data_dir() / 'games.json', json.dumps(value, ensure_ascii=False).encode())
    remember_install_base(product, base)
    return target


class GameService:
    def __init__(self, session, product, progress=lambda _:None, download=lambda *_:None):
        checked_product(product)
        known={item['product']:item['title'] for item in catalog()} | SUPPORTED
        known.update({key:value['title'] for key,value in records().items()})
        if product not in known: raise DmmError('请先刷新官方游戏列表并选择游戏')
        self.title=known[product]
        self.session, self.product, self.progress, self.download = session, product, progress, download
        # Fanza（R18）游戏走 co.jp 域名、game_type=ACL；一般游戏走 .com、GCL。以目录 link 字段为准。
        entry = next((item for item in catalog() if item['product'] == product), None)
        link = (entry or {}).get('link') or ''
        self.adult = bool((entry or {}).get('adult')) or '.co.jp' in link
        self.domain = 'app-gameplayer.dmm.co.jp' if '.co.jp' in link else 'app-gameplayer.dmm.com'

    def request_info(self, installing=False):
        self.session.ensure_login()
        if self.adult: set_age_cookie(self.session, True)
        try:
            game_type = self._detect_game_type()
            payload={'product_id':self.product,'game_type':game_type,'game_os':'win',**self.session.device}
            if not installing: payload['launch_type']='LIB'
            endpoint='/v5/r2/install/cl' if installing else '/v5/r2/launch/cl'
            data=self._request_info(endpoint,payload,installing)
        finally:
            if self.adult: set_age_cookie(self.session, False)
        if data.get('product_id') != self.product: raise DmmError('服务器返回了其他游戏的安装信息')
        if data.get('has_installer'): raise DmmError('该游戏使用专用安装程序，暂不支持自动安装')
        if data.get('drm_auth_token'): raise DmmError('该游戏需要额外 DRM 授权，暂不支持启动')
        if data.get('has_modules') and not data.get('modules'):
            # 少数游戏（如 アサルトリリィ / DOAXVV）需要先装运行库：VC++ 或 DirectX。
            # 安装信息里没带上时，用 /v5/modules 单独取一次。
            extra=self.session.request('/v5/modules', {
                'product_id':self.product,'game_type':payload['game_type'],'game_os':'win',**self.session.device})
            data['modules']=extra.get('modules') or []
        if not data.get('file_list_url') or not data.get('sign'): raise DmmError('服务器未提供可用的标准下载清单')
        return data

    def _request_info(self, endpoint, payload, installing):
        """安装/启动信息获取，自动修复 314（未加库）与 308（未同意规约）后重试。"""
        try:
            return self.session.request(endpoint,payload)
        except DmmApiError as error:
            if error.code==314:
                # 314 = 游戏不在账号的 DMM 游戏库。官方抓包确认加库接口：
                # POST /v5/detail/mygame/add {"product_id","game_type","game_os"}
                # 自动加库后重试原请求，无需用户去商店页操作。
                self.progress('该游戏未加入 DMM 游戏库，正在自动加入…')
                added=self.session.request('/v5/detail/mygame/add',
                    {'product_id':self.product,'game_type':payload['game_type'],'game_os':'win'})
                if not (isinstance(added,dict) and added.get('result')):
                    raise DmmError('自动加入 DMM 游戏库失败，请稍后重试') from None
                self.progress('已加入游戏库，继续获取安装信息')
                return self._retry_after_consent(endpoint,payload,installing)
            if error.code==308:
                return self._retry_after_consent(endpoint,payload,installing)
            raise

    def _retry_after_consent(self, endpoint, payload, installing):
        """308 = NOT_CONSENT_AGREEMENT（官方错误码表）：首次使用该游戏必须先同意其利用规约。

        官方 Player 是弹窗让用户点「同意」，对应 POST /v5/agreement/confirm/client
        {"product_id","is_notification":false}。这里在用户主动点安装/启动时自动代为同意，
        同意后 install/cl 与 launch/cl 都会恢复 100。
        """
        self.progress('首次使用该游戏：正在提交利用规约同意…')
        try:
            agreed=self.session.request('/v5/agreement/confirm/client',
                {'product_id':self.product,'is_notification':False})
        except DmmError:
            agreed=None
        if not (isinstance(agreed,dict) and agreed.get('result')):
            if installing:
                return self._first_download_info(payload)
            raise DmmError('该游戏需要先在 DMM 同意其利用规约，且自动同意未成功；请稍后重试')
        try:
            return self.session.request(endpoint,payload)
        except DmmApiError as retry_error:
            if retry_error.code==308 and installing:
                return self._first_download_info(payload)
            raise

    def _first_download_info(self, payload):
        """兜底：仍被 308 拒绝时，改用下载清单接口直接取 file_list_url 与 CloudFront 签名。

        官方 cex.dll 下载时走的正是这条路径（GetCLDownloadFileResourceInfo），因此
        即使 install/cl 不可用也能完成首次安装。
        """
        self.progress('改用标准下载清单接口获取安装信息')
        return self.session.request('/v5/r2/filelist/cl', payload)

    def install_modules(self, info, root, previous=None):
        """下载并运行游戏声明的运行库模块（VC++ 运行库 / DirectX 等）。

        官方 Player 在下载流程里同样会下载这些模块并以管理员身份执行；同一版本的
        模块已经装过就跳过，避免每次更新都弹安装器。返回 {模块路径: 版本} 记录。
        """
        modules=info.get('modules') or []
        done=dict(previous or {})
        for module in modules:
            if not isinstance(module,dict): continue
            domain=module.get('domain'); remote=module.get('module_file_url')
            folder=module.get('dir_name'); name=module.get('exec_file_name')
            version=str(module.get('module_version') or '')
            if not all(isinstance(value,str) and value for value in (domain,remote,folder,name)): continue
            if not re.fullmatch(r'[A-Za-z0-9_.\-]{1,120}',folder) or not re.fullmatch(r'[A-Za-z0-9_.\-]{1,120}',name):
                raise DmmError('运行库模块路径无效')
            key=f'{folder}/{name}'
            target=root/folder/name
            if done.get(key)==version and target.is_file(): continue
            url=dmm_url(urljoin(domain.rstrip('/')+'/',remote.lstrip('/')))
            self.progress(f'下载运行库 {name}（{folder}）')
            self._download_module(url,target,module.get('sign') or '')
            self._run_module(target)
            done[key]=version
        return done

    def _download_module(self, url, target, sign):
        target.parent.mkdir(parents=True,exist_ok=True)
        headers={'Cookie':sign} if sign else {}
        attempts=getattr(self.session.http,'proxy_attempts',3)
        for attempt in range(attempts):
            self.session.cancel_check()
            try:
                with self.session.http.get(url,headers=headers,stream=True,timeout=(10,180),
                                           allow_redirects=False) as response:
                    if response.status_code!=200:
                        raise DmmError(f'运行库下载失败：HTTP {response.status_code}')
                    temporary=target.with_name(target.name+'.part')
                    with temporary.open('wb') as output:
                        for block in response.iter_content(256*1024):
                            self.session.cancel_check()
                            if block: output.write(block)
                        output.flush(); os.fsync(output.fileno())
                    os.replace(temporary,target)
                    return
            except requests.RequestException:
                failover=getattr(self.session.http,'proxy_failover',None)
                if failover is not None: failover()
                if attempt==attempts-1:
                    raise DmmError('运行库下载连接中断，请重新点安装继续') from None

    def _run_module(self, target):
        """按官方做法以管理员身份运行模块；需要提权时交给系统弹 UAC。"""
        if target.suffix.lower()!='.exe' or not target.is_file():
            raise DmmError('运行库模块不是可执行文件，已跳过')
        self.progress(f'安装运行库 {target.name}（可能弹出 UAC，请允许）')
        try:
            subprocess.run([str(target)],cwd=str(target.parent),check=False)
        except OSError:
            # 安装包要求管理员权限时 CreateProcess 会失败，改由系统弹 UAC 启动。
            ctypes.windll.shell32.ShellExecuteW(None,'runas',str(target),None,str(target.parent),1)

    def remote_info(self):
        """gameinfo 查询：最新版本与大小，未安装的游戏也可用（不触发 308 下载限制）。"""
        self.session.ensure_login()
        if self.adult: set_age_cookie(self.session, True)
        try:
            game_type = self._detect_game_type()
            return self.session.request('/v5/gameinfo', {
                'product_id': self.product, 'game_type': game_type, 'game_os': 'win', **self.session.device})
        finally:
            if self.adult: set_age_cookie(self.session, False)

    def _detect_game_type(self):
        """gametype 探测：Fanza 游戏返回 ACL（co.jp 域名），一般游戏 GCL（.com）。"""
        domains = (self.domain, 'app-gameplayer.dmm.co.jp' if self.domain == 'app-gameplayer.dmm.com'
                   else 'app-gameplayer.dmm.com')
        last_error = None
        for domain in domains:
            try:
                kind = self.session.request('/v5/gametype', {
                    'product_id': self.product, 'domain': domain, 'floor_type': 'free', **self.session.device})
            except DmmApiError as error:
                last_error = error
                continue
            game_type = kind.get('game_type')
            if game_type in ('GCL', 'ACL'): return game_type
        if last_error is not None: raise last_error
        raise DmmError('无法识别该游戏类型（GCL/ACL 均不匹配）')

    def plan(self, info, root):
        url=info['file_list_url']
        manifest=self.session.request(url,method='GET')
        entries=list(manifest['file_list'])
        try: pages=self.session.request(url.rstrip('/')+'/totalpages',method='GET')['total_pages']
        except DmmError as error:
            if '404' not in str(error): raise
            pages=1
        if not isinstance(pages,int) or not 1<=pages<=10000: raise DmmError('下载清单分页无效')
        for page in range(2,pages+1):
            chunk=self.session.request(url+f'?page={page}',method='GET')
            if chunk['domain'] != manifest['domain']: raise DmmError('下载清单服务器不一致')
            entries.extend(chunk['file_list'])
        return UpdatePlan.parse(root,manifest['domain'],entries,info['latest_version'])

    def stopped(self, root):
        """只拦「要操作的这个游戏正在运行」；别的游戏在跑不互相阻碍。"""
        for process in psutil.process_iter(['exe']):
            try:
                if process.info['exe'] and Path(process.info['exe']).resolve().is_relative_to(root.resolve()):
                    raise DmmError('该游戏正在运行，请先关闭游戏')
            except (psutil.Error,OSError): continue

    def updater(self, info, plan):
        return VerifiedUpdater(plan,self.session.proxy,info['sign'],self.progress,
            lambda:self.stopped(plan.root),self.session.cancel_check,self.download)

    def execute(self, action, install_root=None):
        saved=records().get(self.product) or official_record(self.product)
        installing=not saved or action=='install'
        if action=='check' and not saved:
            # 未安装的游戏：用 gameinfo 查最新版本（不触发 308 下载限制）
            info=self.remote_info()
            size=info.get('file_size') or 0
            return (f"最新版本 {info.get('latest_version','?')} · {size/1048576:.1f} MiB · 尚未安装："
                    '点「全新安装」开始下载')
        if installing:
            if install_root is None and action in {'check','install','update'}:
                install_root=game_install_root(self.product)
            if install_root is None: raise DmmError('请先安装游戏')
            root=Path(install_root).resolve()
        else: root=Path(saved['root']).resolve()
        self.progress('获取安装清单' if installing else '检查最新版本和启动参数')
        info=self.request_info(installing)
        plan=self.plan(info,root)
        updater=self.updater(info,plan)
        updater.recover()
        pending=plan.pending()
        size=sum(f.size for f in pending if not f.delete)
        self.progress(f"版本 {info['latest_version']} · {len(pending)} 个文件 · {size/1048576:.1f} MiB")
        if action=='check':
            if not pending:
                # 本地文件与最新清单完全一致：说明游戏已经是这个版本了（可能是游戏自己更新的）
                record_version(self.product, info['latest_version'], root)
            return f"版本 {info['latest_version']} · 待更新 {len(pending)} 个文件 · {size/1048576:.1f} MiB"
        self.stopped(root)
        updater.run()
        if plan.pending(): raise DmmError('文件校验未通过')
        modules=self.install_modules(info,root,(saved or {}).get('modules'))
        value=records(); value[self.product]={'root':str(root),'version':info['latest_version'],
                                              'title':self.title,'modules':modules}
        atomic_write(data_dir()/'games.json',json.dumps(value,ensure_ascii=False).encode())
        if action!='launch': return f"{self.title} {info['latest_version']} 安装 / 更新并校验完成"
        self.session.cancel_check()
        info=self.request_info(False)
        if self.plan(info,root).pending(): raise DmmError('服务器版本已改变，请重新启动以更新')
        if info.get('is_administrator') and not ctypes.windll.shell32.IsUserAnAdmin():
            raise DmmError('文件已准备完成；该游戏要求以管理员身份运行客户端后启动')
        exe=safe_path(root,info['exec_file_name'])
        if exe.suffix.lower()!='.exe' or not exe.is_file(): raise DmmError('游戏启动文件无效')
        args=windows_args(info.get('execute_args',''))
        if any(any(c in arg for c in '\0\n\r') for arg in args): raise DmmError('启动参数无效')
        self.stopped(root)
        process=subprocess.Popen([str(exe),*args],cwd=root,shell=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return f'游戏进程已创建（PID {process.pid}），请确认游戏内登录成功'
