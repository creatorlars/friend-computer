# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Friend Computer.
# Produces a single-file executable that bundles soundboard.json.
#
# Build:
#   pip install pyinstaller
#   pyinstaller friend_computer.spec

a = Analysis(
    ["insane_computer.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("soundboard.json", "."),
    ],
    hiddenimports=[
        # pyttsx3 driver selection is platform-specific and not always
        # auto-detected by PyInstaller -- include all three backends so the
        # same spec works on Windows, macOS and Linux.
        "pyttsx3.drivers",
        "pyttsx3.drivers.sapi5",
        "pyttsx3.drivers.nsss",
        "pyttsx3.drivers.espeak",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="friend-computer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Console window is shown so the user can see stderr diagnostics.
    # The interactive input is handled via pygame events in the CONTROL window.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
)
