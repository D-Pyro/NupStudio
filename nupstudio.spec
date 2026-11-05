# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.building.datastruct import TOC
from PyInstaller.utils.hooks import collect_all


datas, binaries, hiddenimports = collect_all("zstandard")

a = Analysis(
    ["nupstudio_launcher.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# These optional Qt subsystems are not used by the desktop workflows.
unused_qt_modules = (
    "qt6opengl",
    "qt6pdf",
    "qt6qml",
    "qt6quick",
    "qt6virtualkeyboard",
    "qtopengl",
    "qtpdf",
    "qtqml",
    "qtquick",
    "qtvirtualkeyboard",
    "libqt6opengl",
    "libqt6pdf",
    "libqt6qml",
    "libqt6quick",
    "libqt6virtualkeyboard",
    "libqtopengl",
    "libqtpdf",
    "libqtqml",
    "libqtquick",
    "libqtvirtualkeyboard",
)


def keep_binary(binary: tuple) -> bool:
    destination = binary[0].replace("\\", "/").lower()
    filename = destination.rsplit("/", 1)[-1]
    if filename == "opengl32sw.dll":
        return False
    if filename.startswith(unused_qt_modules):
        return False
    return not (
        "/plugins/imageformats/qpdf." in destination
        or "/plugins/platforminputcontexts/qtvirtualkeyboardplugin." in destination
    )


a.binaries = TOC(binary for binary in a.binaries if keep_binary(binary))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NupStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
