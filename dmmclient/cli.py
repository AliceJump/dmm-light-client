"""命令行入口：与界面共用同一套后端与数据，不需要 Qt。

用法示例：
    dmmcli list                     列出已安装游戏
    dmmcli catalog -q ウマ娘         搜索可安装游戏
    dmmcli install umamusume        全新安装（默认装到 <数据目录>/games/umamusume）
    dmmcli install umamusume --dir D:\\GAMES\\DMM   指定安装基准目录
    dmmcli update umamusume         更新
    dmmcli launch umamusume         启动
    dmmcli dir umamusume --set D:\\GAMES\\DMM       改安装位置
    dmmcli move umamusume --dir E:\\Games           移动已装好的游戏目录
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from .backend.accounts import AccountStore
from .backend.games import (GameService, catalog, checked_product, game_root, move_game,
                            official_records, records, refresh_catalog, uninstall_game)
from .backend.session import (DmmError, DmmSession, LoginRequired, data_dir, valid_proxy)
from .backend.settings import (game_install_root, load_settings, remember_install_base,
                               save_settings)
from .backend.xray import NodeStore, operation_proxy


def say(text=''):
    print(text, flush=True)


class Meter:
    """把下载进度压成一行（百分比变化时才刷新）。"""

    def __init__(self):
        self.percent = -1

    def __call__(self, done, total):
        percent = int(done * 100 / total) if total else 100
        if percent != self.percent:
            self.percent = percent
            print(f'\r  {percent:3d}%  {done / 1048576:.1f} / {total / 1048576:.1f} MiB',
                  end='', flush=True)

    def done(self):
        if self.percent >= 0:
            print()


@contextmanager
def open_session(settings, cancel=lambda: None):
    """起代理 + 登录，命令结束后自动收尾。"""
    with operation_proxy(settings, say, cancel, data_dir()) as proxy:
        session = DmmSession(proxy)
        session.ensure_login()
        yield session


def game_service(session, product, meter=None):
    return GameService(session, product, say, meter or (lambda done, total: None))


# ---------- 命令 ----------

def cmd_list(args):
    installed = {**official_records(), **records()}
    if not installed:
        say('（没有已安装的游戏）')
        return 0
    if args.json:
        say(json.dumps(installed, ensure_ascii=False, indent=2))
        return 0
    for product, value in sorted(installed.items()):
        origin = '本客户端' if product in records() else '官方 Player'
        say(f"{product:24s} {str(value.get('version', '?')):12s} [{origin}] {value.get('root', '')}")
    return 0


def cmd_catalog(args):
    items = catalog()
    if not items:
        say('本地还没有游戏目录，先跑一次：dmmcli refresh')
        return 0
    if args.query:
        needle = args.query.casefold()
        items = [item for item in items if needle in item['title'].casefold()]
    if args.filter == 'adult':
        items = [item for item in items if item.get('adult')]
    elif args.filter == 'normal':
        items = [item for item in items if not item.get('adult')]
    for item in items:
        say(f"{item['product']:26s} {'R18' if item.get('adult') else '   '} {item['title']}")
    say(f'共 {len(items)} 款')
    return 0


def cmd_refresh(args):
    with open_session(load_settings()) as session:
        items = refresh_catalog(session)
    say(f'游戏列表已刷新，共 {len(items)} 款')
    return 0


def cmd_check(args):
    with open_session(load_settings()) as session:
        say(game_service(session, args.product).execute('check'))
    return 0


def cmd_install(args):
    if args.dir:
        remember_install_base(args.product, Path(args.dir))
    root = game_install_root(args.product)
    meter = Meter()
    say(f'安装到 {root}')
    with open_session(load_settings()) as session:
        result = game_service(session, args.product, meter).execute('install', root)
    meter.done()
    say(result)
    return 0


def cmd_update(args):
    meter = Meter()
    with open_session(load_settings()) as session:
        result = game_service(session, args.product, meter).execute('update')
    meter.done()
    say(result)
    return 0


def cmd_launch(args):
    with open_session(load_settings()) as session:
        say(game_service(session, args.product).execute('launch'))
    return 0


def cmd_move(args):
    checked_product(args.product)
    meter = Meter()
    moved = move_game(args.product, Path(args.dir), meter, lambda: None, say)
    meter.done()
    say(f'已移动到 {moved}')
    return 0


def cmd_uninstall(args):
    """默认只预览要删什么；加 --yes 才真的删。"""
    checked_product(args.product)
    record = records().get(args.product)
    if not record or not record.get('root'):
        say('该游戏不是由本客户端安装的，无法卸载')
        return 1
    root = Path(record['root'])
    if args.keep:
        uninstall_game(args.product, keep_files=True)
        say(f'已移除安装记录，游戏文件保留在 {root}')
        return 0
    if not args.yes:
        say(f'将删除：{root}')
        say('这是本客户端安装的目录，里面的文件会全部消失且无法恢复。')
        say(f'确认请加 --yes：dmmcli uninstall {args.product} --yes')
        say(f'只想移除记录、保留文件：dmmcli uninstall {args.product} --keep')
        return 0
    result = uninstall_game(args.product, keep_files=False)
    say(f"已卸载，删除 {result['files']} 个文件 · {result['bytes'] / 1048576:.1f} MiB")
    return 0


def cmd_dir(args):
    checked_product(args.product)
    if args.set:
        base = remember_install_base(args.product, Path(args.set))
        say(f'安装位置已设为 {base / args.product}')
        return 0
    current = game_root(args.product)
    say(f'当前目录：{current if current else "（未安装）"}')
    say(f'安装位置：{game_install_root(args.product)}')
    return 0


def cmd_accounts(args):
    store = AccountStore(data_dir())
    if args.switch:
        entry = store.switch(args.switch)
        say(f"已切换到 {entry['number']} · {entry['name']}")
        return 0
    value = store.read()
    if not value['accounts']:
        say('（没有保存的账号）')
        return 0
    for account in value['accounts']:
        mark = '*' if account['key'] == value['active'] else ' '
        say(f"{mark} {account['number']:>8s}  {account['name']}")
    return 0


def cmd_nodes(args):
    store = NodeStore()
    if not store.path.exists():
        say('（还没有导入节点）')
        return 0
    for node in store.read()['nodes']:
        say(f"{'启用' if node['enabled'] else '停用'}  {node['name']}")
    return 0


def cmd_proxy(args):
    settings = load_settings()
    if args.url:
        settings['proxy'] = valid_proxy(args.url)
        save_settings(settings)
        say(f"代理已设为 {settings['proxy']}")
        return 0
    say(settings['proxy'])
    return 0


def cmd_ping(args):
    with open_session(load_settings()) as session:
        session.request('/v5/userinfo', {'user_os': 'win'})
    say('DMM 连接正常，登录态有效')
    return 0


# ---------- 参数解析 ----------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='dmmcli', description='DMM Light 客户端命令行（与界面共用同一套数据与登录态）')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('list', help='列出已安装的游戏')
    p.add_argument('--json', action='store_true', help='以 JSON 输出')
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('catalog', help='列出本地缓存的游戏目录')
    p.add_argument('-q', '--query', default='', help='按名称过滤')
    p.add_argument('--filter', choices=['all', 'adult', 'normal'], default='all', help='一般 / Fanza R18')
    p.set_defaults(func=cmd_catalog)

    sub.add_parser('refresh', help='联网刷新游戏目录').set_defaults(func=cmd_refresh)

    p = sub.add_parser('check', help='检查更新（不下载）')
    p.add_argument('product')
    p.set_defaults(func=cmd_check)

    p = sub.add_parser('install', help='全新安装')
    p.add_argument('product')
    p.add_argument('--dir', help='安装基准目录，实际装到 <dir>/<product>')
    p.set_defaults(func=cmd_install)

    p = sub.add_parser('update', help='更新到最新版本')
    p.add_argument('product')
    p.set_defaults(func=cmd_update)

    p = sub.add_parser('launch', help='启动游戏（启动前自动更新）')
    p.add_argument('product')
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser('move', help='移动已安装游戏的目录')
    p.add_argument('product')
    p.add_argument('--dir', required=True, help='新的基准目录，游戏会移到 <dir>/<product>')
    p.set_defaults(func=cmd_move)

    p = sub.add_parser('uninstall', help='卸载本客户端安装的游戏')
    p.add_argument('product')
    p.add_argument('--keep', action='store_true', help='只移除安装记录，保留游戏文件')
    p.add_argument('--yes', action='store_true', help='确认真的删除文件（不加只预览）')
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser('dir', help='查看或设置游戏的安装位置')
    p.add_argument('product')
    p.add_argument('--set', help='设置安装基准目录')
    p.set_defaults(func=cmd_dir)

    p = sub.add_parser('accounts', help='列出或切换账号')
    p.add_argument('--switch', help='切换到该编号的账号')
    p.set_defaults(func=cmd_accounts)

    sub.add_parser('nodes', help='列出已导入的节点').set_defaults(func=cmd_nodes)

    p = sub.add_parser('proxy', help='查看或设置代理地址')
    p.add_argument('url', nargs='?', help='例如 http://127.0.0.1:10808')
    p.set_defaults(func=cmd_proxy)

    sub.add_parser('ping', help='测试 DMM 连接与登录态').set_defaults(func=cmd_ping)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LoginRequired as error:
        say(f'登录态不可用：{error}')
        say('请先在图形界面里登录一次（浏览器登录 / 导入官方登录态），再回到命令行。')
        return 2
    except DmmError as error:
        say(f'操作失败：{error}')
        return 1
    except KeyboardInterrupt:
        say('已中断')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
