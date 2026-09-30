@echo off
setlocal enabledelayedexpansion

if "%~1"=="__child__" goto :main

rem Every run writes build.log next to this script, with a full copy of
rem everything printed below (including update_ytdlp.py/fetch_ffmpeg.py/fetch_aria2.py/
rem sync_vlc.py's own output) -- so if a step fails, the exact error text
rem is saved and can be reread or shared even after this window closes.
rem Shown live via PowerShell's Tee-Object (practically always available
rem on Windows 7+) so a stall shows exactly which step it's stuck on
rem instead of a blank terminal; falls back to buffer-then-dump if
rem PowerShell isn't found.
set PYTHONUNBUFFERED=1
set "LOGFILE=%~dp0build.log"
where powershell >nul 2>nul
if not errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Continue'; & '%~f0' __child__ 2>&1 | Tee-Object -FilePath '%LOGFILE%'"
    set "RESULT=%ERRORLEVEL%"
) else (
    call "%~f0" __child__ > "%LOGFILE%" 2>&1
    set "RESULT=%ERRORLEVEL%"
    type "%LOGFILE%"
)

echo.
echo Full output was also saved to: %LOGFILE%
if not "%RESULT%"=="0" echo Exit code: %RESULT%
echo.
echo Press any key to close this window...
pause >nul
endlocal
exit /b %RESULT%

:main

rem ============================================================
rem  Media Tug build script
rem  Produces: release\MediaTug\
rem              app\        (the frozen exe + its DLLs)
rem              cookies\
rem              data\
rem              logs\
rem              tools\      (ffmpeg.exe, aria2c.exe go here)
rem
rem  Also maintains, next to this script (outside release\, so Step 1's
rem  clean never wipes it):
rem              dependencies\ffmpeg\  ffmpeg.exe/ffprobe.exe, version-
rem                                    checked against Gyan.dev -- see
rem                                    fetch_ffmpeg.py
rem              dependencies\vlc\     libvlc.dll/libvlccore.dll/plugins,
rem                                    version-checked against VideoLAN --
rem                                    see sync_vlc.py
rem              dependencies\yt-dlp\  the yt-dlp package itself, shared
rem                                    with run.bat so both fetch/cache
rem                                    exactly one copy
rem              dependencies\aria2\   aria2c.exe (multi-connection speed
rem                                    for downloads), version-checked
rem                                    against GitHub -- see fetch_aria2.py
rem
rem  This dependencies\ folder is meant to be looked at, not just treated
rem  as a hidden cache: you can drop files into any of its subfolders
rem  yourself (e.g. a manually downloaded+extracted ffmpeg build) and
rem  they'll be used as-is -- these scripts only ever replace files that
rem  are already there once they can confirm a newer version has been
rem  published upstream; they never overwrite something that's already
rem  present and working just because its exact version isn't recorded.
rem
rem  Building requires internet access the first time (or whenever a
rem  newer version is published) to fetch VLC and ffmpeg -- see Steps
rem  5-6. Each of those also keeps its own fetch.log/sync.log inside its
rem  dependencies\ subfolder with more detail than fits in this console
rem  (e.g. exactly what a failed download's response body looked like).
rem ============================================================

set ROOT=%~dp0
set RELEASE=%ROOT%release\MediaTug
set DEPS=%ROOT%dependencies
set EXITCODE=0

rem Read the current app version from VERSION.txt next to this script.
rem VERSION.txt is the single source of truth for the version shown in the
rem app's About page (and echoed by this build). To bump the version, edit
rem VERSION.txt -- no code changes needed. Falls back to 1.0.0 if missing.
set "APP_VERSION=1.0.0"
if exist "%ROOT%VERSION.txt" set /p APP_VERSION=<"%ROOT%VERSION.txt"

echo.
echo === Media Tug build: version %APP_VERSION% ===
echo.
echo === Step 1: Cleaning previous build ===
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"
if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
if exist "%RELEASE%" rmdir /s /q "%RELEASE%"

echo.
echo === Step 2: Ensuring yt-dlp nightly is up to date ===
rem Checks the latest version PyPI has for yt-dlp's nightly channel
rem against what's in dependencies\yt-dlp\, and only downloads when they
rem differ -- if it's already the latest nightly, it's used as-is with
rem no re-download. Installed into dependencies\yt-dlp\ (a pip --target
rem folder) rather than this environment's site-packages, so run.bat and
rem build.bat share exactly one copy instead of two that could drift out
rem of sync -- media_tug.spec adds this folder to PyInstaller's search
rem path itself, so nothing else here needs to change to find it.
python "%ROOT%tools\update_ytdlp.py" --target "%DEPS%\yt-dlp"
if errorlevel 1 (
    echo WARNING: could not verify/install yt-dlp before building.
    echo          The build will use whatever is currently in
    echo          dependencies\yt-dlp\, if anything.
)

echo.
echo === Step 3: Running PyInstaller ===
pyinstaller media_tug.spec
if errorlevel 1 (
    echo.
    echo BUILD FAILED: PyInstaller reported an error above. Nothing was released.
    set EXITCODE=1
    goto :end
)

echo.
echo === Step 4: Assembling the release folder ===
mkdir "%RELEASE%"
xcopy /e /i /q "%ROOT%dist\app" "%RELEASE%\app" >nul
mkdir "%RELEASE%\cookies"
mkdir "%RELEASE%\data"
mkdir "%RELEASE%\logs"
mkdir "%RELEASE%\tools"

rem Stamp the version into the release folder (a marker at the root and a
rem copy beside the exe), so the built app's About page can read VERSION.txt
rem and state the same version this build was made with.
copy /y "%ROOT%VERSION.txt" "%RELEASE%\VERSION.txt" >nul
copy /y "%ROOT%VERSION.txt" "%RELEASE%\app\VERSION.txt" >nul

echo.
echo === Step 5: Fetching VLC (libvlc.dll, libvlccore.dll, plugins) ===
rem Downloads the current stable VLC build directly from VideoLAN (no
rem installer needed -- it's published as a plain win64 zip), version-
rem checked in dependencies\vlc\ so a build doesn't re-download VLC every
rem single time. A real VLC install under Program Files is only used as
rem a fallback if VideoLAN can't be reached -- see sync_vlc.py.
set VLC_FOUND=0
if exist "%ProgramFiles%\VideoLAN\VLC\libvlc.dll" set VLC_DIR=%ProgramFiles%\VideoLAN\VLC
if exist "%ProgramFiles(x86)%\VideoLAN\VLC\libvlc.dll" set VLC_DIR=%ProgramFiles(x86)%\VideoLAN\VLC

set VLC_CACHE=%DEPS%\vlc
if defined VLC_DIR (
    python "%ROOT%tools\sync_vlc.py" --source "!VLC_DIR!" --cache "%VLC_CACHE%" --dest "%RELEASE%\app"
) else (
    python "%ROOT%tools\sync_vlc.py" --cache "%VLC_CACHE%" --dest "%RELEASE%\app"
)
if errorlevel 1 (
    echo WARNING: Could not fetch VLC from VideoLAN, no VLC install was found
    echo          under Program Files either, and there was no cached copy
    echo          from a previous build to fall back on.
    echo          Copy libvlc.dll, libvlccore.dll, and the plugins folder
    echo          from a VLC installation into: %RELEASE%\app\
    echo          Playback will not work until this is done.
) else (
    set VLC_FOUND=1
)

echo.
echo === Step 6: Fetching ffmpeg (ffmpeg.exe, ffprobe.exe) ===
rem Downloads the current stable "full" ffmpeg build directly from
rem Gyan.dev (a static, no-install-needed build), version-checked in
rem dependencies\ffmpeg\ so a build doesn't re-download it every single
rem time. This used to just grab whatever ffmpeg.exe happened to be on
rem the *build machine's* PATH, which meant a build only worked for
rem downloads on machines that coincidentally already had ffmpeg
rem installed -- everyone else got silent "Download failed" errors.
set FFMPEG_CACHE=%DEPS%\ffmpeg
python "%ROOT%tools\fetch_ffmpeg.py" --cache "%FFMPEG_CACHE%" --target "%RELEASE%\tools"
if errorlevel 1 (
    set FFMPEG_FOUND=0
    echo WARNING: Could not fetch ffmpeg, and there was no cached copy from
    echo          a previous build to fall back on.
    echo          Download a Windows build from https://www.gyan.dev/ffmpeg/builds/
    echo          and place ffmpeg.exe and ffprobe.exe here: %RELEASE%\tools\
    echo          Audio extraction and format conversion will not work until this is done.
) else (
    set FFMPEG_FOUND=1
)

echo.
echo === Step 6.5: Fetching aria2c (fast multi-connection downloads) ===
rem Downloads the current aria2 release (a single small aria2c.exe) from
rem GitHub, version-checked in dependencies\aria2\ so a build doesn't
rem re-download it every single time. aria2c makes save-to-disk, channel,
rem and video-fetch downloads several times faster by splitting each file
rem into up to 16 parallel connections -- YouTube throttles single
rem connections hard. It is optional: without it the app still works,
rem just slower, so a fetch failure only warns.
set ARIA2_CACHE=%DEPS%\aria2
python "%ROOT%tools\fetch_aria2.py" --cache "%ARIA2_CACHE%" --target "%RELEASE%\tools"
if errorlevel 1 (
    set ARIA2_FOUND=0
    echo WARNING: Could not fetch aria2c, and there was no cached copy from
    echo          a previous build to fall back on.
    echo          Downloads will keep working, just with a single
    echo          connection instead of 16 parallel ones.
) else (
    set ARIA2_FOUND=1
)

echo.
echo === Build finished ===
echo Release folder: %RELEASE%
if "!VLC_FOUND!"=="0" echo   - still needs: VLC files in app\
if "!FFMPEG_FOUND!"=="0" echo   - still needs: tools\ffmpeg.exe
if "!ARIA2_FOUND!"=="0" echo   - optional: tools\aria2c.exe (faster downloads)
echo.
echo This "release\MediaTug" folder IS the portable app -- copy it anywhere
echo and run app\media_tug.exe. It is also exactly what the MSI installer
echo (installer.wxs) expects to find and package.

:end
endlocal & exit /b %EXITCODE%
