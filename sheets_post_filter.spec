# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

import PyInstaller
from PyInstaller.utils.hooks import collect_submodules

hidden = []
for pkg in ("gspread", "google", "flask", "jinja2", "werkzeug", "certifi"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

# 某些 Windows Python 安装能导入 tkinter，却无法在 PyInstaller 的隔离探测
# 中初始化 Tcl。显式收集这些文件，避免打包成功但桌面程序启动即退出。
python_root = Path(sys.base_prefix)
manual_datas = []
manual_binaries = []
tk_runtime_hook = Path(PyInstaller.__file__).resolve().parent / "hooks" / "rthooks" / "pyi_rth__tkinter.py"
for source, target in (
    (python_root / "Lib" / "tkinter", "tkinter"),
    (python_root / "tcl" / "tcl8.6", "_tcl_data"),
    (python_root / "tcl" / "tk8.6", "_tk_data"),
):
    if source.exists():
        manual_datas.append((str(source), target))
for filename in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
    source = python_root / "DLLs" / filename
    if source.exists():
        manual_binaries.append((str(source), "."))

a = Analysis(
    ["desktop_app.py"],
    pathex=[],
    binaries=manual_binaries,
    datas=[("web", "web"), ("logo.ico", ".")] + manual_datas,
    hiddenimports=hidden
    + [
        "google.auth",
        "google.auth.transport.requests",
        "google.oauth2.service_account",
        "gspread",
        "flask",
        "jinja2",
        "werkzeug",
        "click",
        "itsdangerous",
        "markupsafe",
        "certifi",
        "idna",
        "urllib3",
        "requests",
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "video_duration",
        "catalog_merge",
        "post_aggregate",
        "roster_fill",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(tk_runtime_hook)] if tk_runtime_hook.exists() else [],
    excludes=["promo-site"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="数据汇总工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon="logo.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="数据汇总工具",
)
