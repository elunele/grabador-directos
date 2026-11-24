@echo off
REM Compilación en tu propio PC (alternativa a GitHub Actions).
REM Requiere Python 3.12 y que pongas en .\bin\ : ffmpeg.exe, libmpv-2.dll y deno.exe
cd /d %~dp0
py -3.12 -m pip install -r requirements.txt pyinstaller || goto :err
py -3.12 -m PyInstaller --noconfirm GrabadorDirectos.spec || goto :err
echo.
echo Listo: dist\GrabadorDirectos\GrabadorDirectos.exe
pause
exit /b 0
:err
echo Ha fallado la compilacion.
pause
exit /b 1
