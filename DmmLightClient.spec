# -*- mode: python ; coding: utf-8 -*-
"""DMM Light Client 打包配置（PyInstaller，onedir）。

用 QtWebEngine 的应用不适合 onefile：Chromium 子进程（QtWebEngineProcess.exe）
按相对路径定位资源，onefile 每次启动解压到临时目录又慢又容易失联。

构建：uv run pyinstaller dmmclient.spec --noconfirm
产物：dist/DmmLightClient/
"""
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

datas = [
    ('dmmclient/webui', 'dmmclient/webui'),   # 前端页面与脚本
]
# 收集 QtWebEngine 运行资源（resources / translations，PyInstaller 的
# hooks-contrib 会带上 QtWebEngineProcess.exe，这里补齐可能漏掉的数据文件）
datas += [(src, dst) for src, dst in collect_data_files('PySide6', subdir='Qt', excludes=['**/__pycache__/**'])
          if 'WebEngine' in src.replace('\\', '/')]

a = Analysis(
    ['run_client.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'dmmclient',            # 包内动态导入
        'dmmclient.webapp',     # webview 前端（main() 内按需导入）
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebChannel',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc_data'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DmmLightClient',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序，无控制台窗口
    icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='DmmLightClient',
)
