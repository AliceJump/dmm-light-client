"""Explicitly supported downloadable games, with independent install records."""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

import psutil
import requests

from .helper import windows_args, ensure_update_idle
from .session import DmmApiError, DmmError, atomic_write, data_dir, dmm_dir
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
            try:
                data=self.session.request(endpoint,payload)
            except DmmApiError as error:
                if error.code==314:
                    # 314 = 游戏不在账号的 DMM 游戏库。官方抓包确认加库接口：
                    # POST /v5/detail/mygame/add {"product_id","game_type","game_os"}
                    # 自动加库后重试原请求，无需用户去商店页操作。
                    self.progress('该游戏未加入 DMM 游戏库，正在自动加入…')
                    added=self.session.request('/v5/detail/mygame/add',
                        {'product_id':self.product,'game_type':game_type,'game_os':'win'})
                    if not (isinstance(added,dict) and added.get('result')):
                        raise DmmError('自动加入 DMM 游戏库失败，请稍后重试') from None
                    self.progress('已加入游戏库，继续获取安装信息')
                    try:
                        data=self.session.request(endpoint,payload)
                    except DmmApiError as retry_error:
                        if retry_error.code==308 and installing:
                            data=self._first_download_info(payload)
                        else:
                            raise
                elif error.code==308 and installing:
                    # 308 = 该游戏从未由官方下载器下载过，install/cl 被服务端拒绝。
                    # 下载清单与 CloudFront 签名改由 /v5/r2/filelist/cl 直接取得：
                    # 官方 cex.dll 下载时走的正是这条路径（GetCLDownloadFileResourceInfo），
                    # 它不检查「是否曾由官方下载器下载过」，因此无需官方 Player 即可首次安装。
                    data=self._first_download_info(payload)
                else:
                    raise
        finally:
            if self.adult: set_age_cookie(self.session, False)
        if data.get('product_id') != self.product: raise DmmError('服务器返回了其他游戏的安装信息')
        if data.get('has_installer'): raise DmmError('该游戏使用专用安装程序，暂不支持自动安装')
        if data.get('has_modules'): raise DmmError('该游戏需要额外运行模块，暂不支持自动安装')
        if data.get('drm_auth_token'): raise DmmError('该游戏需要额外 DRM 授权，暂不支持启动')
        if not data.get('file_list_url') or not data.get('sign'): raise DmmError('服务器未提供可用的标准下载清单')
        return data

    def _first_download_info(self, payload):
        """308 首次下载限制的绕过：用下载清单接口拿 file_list_url 与 CloudFront 签名。"""
        self.progress('该游戏首次安装：使用标准下载清单接口（无需官方下载器）')
        return self.session.request('/v5/r2/filelist/cl', payload)

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
        ensure_update_idle()
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
            if install_root is None and action in {'check','install'}:
                install_root=data_dir()/'games'/self.product
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
        if action=='check': return f"版本 {info['latest_version']} · 待更新 {len(pending)} 个文件 · {size/1048576:.1f} MiB"
        self.stopped(root)
        updater.run()
        if plan.pending(): raise DmmError('文件校验未通过')
        value=records(); value[self.product]={'root':str(root),'version':info['latest_version'],'title':self.title}
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
