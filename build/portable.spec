# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_submodules,
    copy_metadata,
    get_module_file_attribute,
)


spec_location = Path(SPECPATH)
spec_directory = spec_location if spec_location.is_dir() else spec_location.parent
project_root = spec_directory.parent
source_root = project_root / "app"
diagnostic_build = os.getenv("THINKTANK_BUILD_CONSOLE") == "1"
executable_name = "ThinkTankTrackerDebug" if diagnostic_build else "启动智库追踪"

datas = [
    (str(source_root / "thinktank_tracker" / "templates"), "thinktank_tracker/templates"),
    (str(source_root / "thinktank_tracker" / "static"), "thinktank_tracker/static"),
    (str(source_root / "thinktank_tracker" / "assets"), "thinktank_tracker/assets"),
    (str(project_root / "VERSION"), "."),
]
hiddenimports = collect_submodules("keyring.backends") + [
    "thinktank_tracker.web",
    "multipart",
    "python_multipart",
]
binaries = []

for package in ("playwright", "certifi", "anydoc", "scrapling", "patchright", "browserforge", "apify_fingerprint_datapoints", "curl_cffi", "tld"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
anydoc_native = get_module_file_attribute("anydoc._anydoc")
if Path(anydoc_native).name.lower() != "_anydoc.pyd":
    raise RuntimeError(f"Unexpected AnyDoc native module: {anydoc_native}")
binaries.append((anydoc_native, "anydoc"))
datas += copy_metadata("firecrawl-anydoc")
datas += copy_metadata("scrapling")

a = Analysis(
    [str(source_root / "thinktank_tracker" / "launcher.py")],
    pathex=[str(source_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "IPython", "tkinter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=executable_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=diagnostic_build,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="app",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ThinkTankTrackerDebug" if diagnostic_build else "ThinkTankTracker",
)
