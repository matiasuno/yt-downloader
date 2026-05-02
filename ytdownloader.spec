# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

yt_datas, yt_binaries, yt_hiddenimports = collect_all('yt_dlp')
img_datas, img_binaries, img_hiddenimports = collect_all('imageio_ffmpeg')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=yt_binaries + img_binaries,
    datas=yt_datas + img_datas + [('templates', 'templates')],
    hiddenimports=(
        yt_hiddenimports + img_hiddenimports +
        collect_submodules('flask') +
        collect_submodules('werkzeug') +
        collect_submodules('jinja2') +
        ['email.mime.text', 'email.mime.multipart']
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'wx', 'matplotlib', 'numpy', 'pandas'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='YTDownloader',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='YTDownloader',
)

# macOS only: wrap in a proper .app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='YTDownloader.app',
        bundle_identifier='com.local.ytdownloader',
        info_plist={
            'CFBundleName': 'YT Downloader',
            'CFBundleDisplayName': 'YT Downloader',
            'CFBundleShortVersionString': '1.0',
            'NSHighResolutionCapable': True,
            'LSBackgroundOnly': False,
        },
    )
