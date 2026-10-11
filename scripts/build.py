# -*- coding: utf-8 -*-
"""打包工作流：清理 → PyInstaller 构建 → 产物校验 → zip。

用法：
    uv run python scripts/build.py            # 构建 + zip
    uv run python scripts/build.py --no-zip   # 只构建
产物：dist/DmmLightClient/ 与 dist/DmmLightClient-<版本>-win64.zip
"""
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = 'DmmLightClient'

# Windows 控制台 / CI 可能是 cp1252 之类的窄编码，中文输出会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError, OSError):
        pass


def version() -> str:
    text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    for line in text.splitlines():
        if line.startswith('version'):
            return line.split('=', 1)[1].strip().strip('"\'')
    return '0.0.0'


def clean():
    for name in ('build', 'dist'):
        path = ROOT / name
        if not path.exists():
            continue
        try:
            print(f'清理 {name}/')
            shutil.rmtree(path)
        except OSError as error:
            # 大目录可能被文件守卫拦截；--noconfirm 会让 PyInstaller 覆盖旧产物
            print(f'清理 {name}/ 失败（{error}），改用覆盖构建')


def build():
    print('== PyInstaller 构建（onedir，含 QtWebEngine）==')
    result = subprocess.run(
        [sys.executable, '-m', 'PyInstaller', f'{APP_NAME}.spec', '--noconfirm',
         '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build')],
        cwd=ROOT)
    if result.returncode != 0:
        sys.exit('构建失败')


REQUIRED = [
    f'{APP_NAME}.exe',
    '_internal/PySide6/QtWebEngineProcess.exe',   # Chromium 子进程
    '_internal/PySide6/resources/icudtl.dat',     # WebEngine 资源
    '_internal/dmmclient/webui/index.html',       # 前端页面
    '_internal/dmmclient/webui/app.js',
    '_internal/dmmclient/webui/style.css',
]


def verify() -> list[str]:
    base = ROOT / 'dist' / APP_NAME
    missing = [name for name in REQUIRED if not (base / name).exists()]
    if missing:
        sys.exit('产物缺少关键文件：\n  ' + '\n  '.join(missing))
    print('产物校验通过')
    return [str(p.relative_to(base)) for p in base.rglob('*') if p.is_file()]


def make_zip(rel_files: list[str]):
    tag = f'{APP_NAME}-{version()}-win64.zip'
    target = ROOT / 'dist' / tag
    base = ROOT / 'dist' / APP_NAME
    print(f'== 打包 {target.name} ==')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in sorted(rel_files):
            z.write(base / rel, f'{APP_NAME}/{rel}')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f'完成：{target}')
    print(f'  大小 {target.stat().st_size / 1048576:.1f} MiB · sha256 {digest[:16]}…')
    return target


def main():
    if '--no-clean' not in sys.argv:
        clean()
    build()
    files = verify()
    if '--no-zip' not in sys.argv:
        make_zip(files)


if __name__ == '__main__':
    main()
