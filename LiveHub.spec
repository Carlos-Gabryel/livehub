# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("livehub/static", "livehub/static"),
    ("livehub/chat/static", "livehub/chat/static"),
    ("livehub/resgates/index.html", "livehub/resgates"),
]
binaries = []
hiddenimports = ["pystray._win32", "engineio.async_drivers.aiohttp"]
for pkg in ("TikTokLive", "betterproto", "socketio", "engineio", "sounddevice",
            "_sounddevice_data", "soundfile", "_soundfile_data", "pystray", "PIL"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d; binaries += b; hiddenimports += h
hiddenimports += collect_submodules("livehub")
hiddenimports += collect_submodules("numpy")

a = Analysis(
    ["run_livehub.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="LiveHub",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/livehub.ico",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="LiveHub")
