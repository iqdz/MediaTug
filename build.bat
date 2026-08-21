@echo off
setlocal

set OUTPUT_NAME=Media_Tug

echo Installing/updating build dependencies...
python -m pip install --upgrade pyinstaller wxPython yt-dlp python-vlc curl_cffi

echo.
echo Building with PyInstaller (onedir, windowed, single exe inside app\)...
python -m PyInstaller --noconfirm --onedir --windowed --name media_tug media_tug.py

if errorlevel 1 (
    echo Build failed. See output above.
    pause
    exit /b 1
)

echo.
echo Assembling portable folder: %OUTPUT_NAME%\
if exist "%OUTPUT_NAME%" rmdir /s /q "%OUTPUT_NAME%"
mkdir "%OUTPUT_NAME%"
move /Y "dist\media_tug" "%OUTPUT_NAME%\app" >nul
mkdir "%OUTPUT_NAME%\cookies"
mkdir "%OUTPUT_NAME%\data"
mkdir "%OUTPUT_NAME%\logs"

set VLC_DIR=C:\Program Files\VideoLAN\VLC
if exist "%VLC_DIR%\libvlc.dll" (
    echo Bundling VLC runtime into app\ for portability...
    copy /Y "%VLC_DIR%\libvlc.dll" "%OUTPUT_NAME%\app\" >nul
    copy /Y "%VLC_DIR%\libvlccore.dll" "%OUTPUT_NAME%\app\" >nul
    xcopy /E /I /Y "%VLC_DIR%\plugins" "%OUTPUT_NAME%\app\plugins" >nul
) else (
    echo Warning: VLC not found at "%VLC_DIR%".
    echo The built app will need VLC installed on the target machine,
    echo or copy libvlc.dll, libvlccore.dll, and the plugins folder from
    echo a VLC install into %OUTPUT_NAME%\app\ manually for full portability.
)

echo.
echo Done.
echo Portable app folder: %OUTPUT_NAME%\
echo Run:    %OUTPUT_NAME%\app\media_tug.exe
echo Data, settings, and playback cache: %OUTPUT_NAME%\data\
echo Cookies file (optional, drop cookies.txt here): %OUTPUT_NAME%\cookies\
echo Debug logs: %OUTPUT_NAME%\logs\
echo.
echo This app folder is fully self-contained -- it can be moved to any
echo other Windows computer and run directly with no reinstallation,
echo since PyInstaller bundles Python and all dependencies into it.
echo.
echo To update the app later, rebuild and replace only the app\ folder --
echo cookies, data, and logs are untouched since they live outside it.
pause
