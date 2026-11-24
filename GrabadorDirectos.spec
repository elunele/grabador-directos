# -*- mode: python ; coding: utf-8 -*-
# Compilación:  pyinstaller --noconfirm GrabadorDirectos.spec
# Espera los binarios externos en ./bin  (ffmpeg.exe, libmpv-2.dll, deno.exe)
import glob, os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("customtkinter", "streamlink", "yt_dlp", "yt_dlp_ejs"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

for f in glob.glob(os.path.join("bin", "*")):
    binaries.append((f, "bin"))

icon = "icono.ico" if os.path.exists("icono.ico") else None
if icon:
    datas.append((icon, "."))   # para el icono de la ventana y la barra de tareas

a = Analysis(["run.py"], pathex=["."], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports + ["mpv", "grabador.app", "grabador.core"],
             excludes=["matplotlib", "numpy", "pandas"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GrabadorDirectos",
          console=False, icon=icon, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="GrabadorDirectos", upx=False)
