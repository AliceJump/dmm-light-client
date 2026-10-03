"""Explicitly supported downloadable games, with independent install records."""
from __future__ import annotations

import ctypes
import json
import re
import subprocess
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

import psutil
import requests

from .helper import windows_args, ensure_update_idle
from .session import DmmError, atomic_write, data_dir
from .updater import UpdatePlan, VerifiedUpdater, safe_path

SUPPORTED = {'priconner': '公主连结 Re:Dive（日服）'}


def checked_product(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',value):
        raise DmmError('游戏编号无效')
    return value


def catalog():
    path=data_dir()/'catalog.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def refresh_catalog(session):
    session.ensure_login()
    result={}
    for adult in (False, True):
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
                    result[product]={'product':product,'title':title[:240],'adult':adult,
                                     'image_url':image if isinstance(image,str) and image.startswith('http') else ''}
                if page*120>=total: break
                if not items or page>=100: raise DmmError('游戏列表分页不完整')
                page+=1
        except DmmError as error:
            if not adult: raise
            session.progress('Fanza 游戏列表获取失败，已跳过：'+str(error))
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

    def request_info(self, installing=False):
        self.session.ensure_login()
        kind=self.session.request('/v5/gametype',{'product_id':self.product,'domain':'app-gameplayer.dmm.com','floor_type':'free',**self.session.device})
        if kind.get('game_type')!='GCL': raise DmmError('该游戏不是受支持的普通 PC 下载型游戏')
        payload={'product_id':self.product,'game_type':'GCL','game_os':'win',**self.session.device}
        if not installing: payload['launch_type']='LIB'
        data=self.session.request('/v5/r2/install/cl' if installing else '/v5/r2/launch/cl',payload)
        if data.get('product_id') != self.product: raise DmmError('服务器返回了其他游戏的安装信息')
        if data.get('has_installer'): raise DmmError('该游戏使用专用安装程序，暂不支持自动安装')
        if data.get('has_modules'): raise DmmError('该游戏需要额外运行模块，暂不支持自动安装')
        if data.get('drm_auth_token'): raise DmmError('该游戏需要额外 DRM 授权，暂不支持启动')
        if not data.get('file_list_url') or not data.get('sign'): raise DmmError('服务器未提供可用的标准下载清单')
        return data

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
        saved=records().get(self.product)
        installing=not saved or action=='install'
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
