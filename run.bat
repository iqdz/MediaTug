@echo off
setlocal

if "%~1"=="__setup__" goto :setup

rem Forces Python's stdout to flush immediately instead of block-buffering
rem when it isn't talking directly to a real console (which is the case
rem once its output is piped through Tee-Object below) -- without this,
rem status/progress messages from update_ytdlp.py/fetch_ffmpeg.py/
rem sync_vlc.py could still lag behind what's actually happening.
set PYTHONUNBUFFERED=1

rem The dependency-checking/fetching steps below are shown live AND
rem captured to logs\run.log (via PowerShell's
rem Tee-Object, if PowerShell is available -- it practically always is
rem on Windows 7+), so a stall shows you exactly which step it's stuck
rem on instead of a blank terminal, and there's still a saved copy to
rem reread or share afterward. Falls back to buffer-then-dump (log
rem first, show after) if PowerShell isn't found. The actual app launch
rem further below always stays live and un-redirected either way, since
rem it's an interactive GUI, not something that benefits from logging.
if not exist "%~dp0logs" mkdir "%~dp0logs"
set "LOGFILE=%~dp0logs\run.log"
where powershell >nul 2>nul
if not errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Continue'; & '%~f0' __setup__ 2>&1 | Tee-Object -FilePath '%LOGFILE%'"
    set "SETUP_RESULT=%ERRORLEVEL%"
) else (
    call "%~f0" __setup__ > "%LOGFILE%" 2>&1
    set "SETUP_RESULT=%ERRORLEVEL%"
    type "%LOGFILE%"
)
echo.
echo Full setup output was also saved to: %LOGFILE%
if not "%SETUP_RESULT%"=="0" (
    echo.
    echo Setup reported a problem above -- Media Tug may not fully work
    echo until it's fixed. Continuing to launch it anyway...
)

echo.
echo Starting Media Tug...
python "%~dp0media_tug.py"
if errorlevel 1 (
    echo Media Tug closed with an error. See messages above.
    pause
)

endlocal
exit /b 0

:setup
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3 and try again.
    exit /b 1
)

set LIBS_DIR=%~dp0libs
set DEPS=%~dp0dependencies

REM Most dependencies install into a local 'libs' folder next to this
REM script, instead of the system's global site-packages. media_tug.py
REM adds this folder to its own sys.path automatically. Once installed
REM here, the whole folder -- script, run.bat, and libs -- can be copied
REM to another machine with a compatible Python already installed and it
REM will just work, with nothing to reinstall.
if not exist "%LIBS_DIR%" mkdir "%LIBS_DIR%"

echo Checking dependencies...
python -c "import sys; sys.path.insert(0, r'%LIBS_DIR%'); import wx" 2>nul
if errorlevel 1 (
    echo Installing wxPython into libs\...
    python -m pip install --target "%LIBS_DIR%" wxPython
)

REM yt-dlp, ffmpeg, and the VLC engine (unlike the other dependencies
REM above) live under a single 'dependencies\' folder next to this
REM script instead -- shared with build.bat so both fetch/version-check
REM exactly one copy of each instead of two that could drift apart. This
REM folder is meant to be looked at, not just a hidden cache: you can
REM drop files into any of its subfolders yourself (e.g. a manually
REM downloaded+extracted ffmpeg build) and they'll be used as-is --
REM nothing here overwrites a file that's already present and working
REM just because its exact version isn't recorded; it only replaces
REM something once it can confirm a newer version is actually available.
REM Each of these also keeps its own fetch.log/sync.log inside its
REM dependencies\ subfolder with more detail than fits here (e.g. exactly
REM what a failed download's response body looked like).

REM yt-dlp always runs on the nightly channel (YouTube changes its
REM protections often and stable releases lag), but only actually
REM downloads when a newer nightly exists.
echo Checking yt-dlp nightly version ^(uses dependencies\yt-dlp\ as-is if it's already the latest^)...
python "%~dp0tools\update_ytdlp.py" --target "%DEPS%\yt-dlp"
if errorlevel 1 (
    echo Warning: could not verify/install yt-dlp automatically. Continuing with whatever is in dependencies\yt-dlp\, if anything.
)

python -c "import sys; sys.path.insert(0, r'%LIBS_DIR%'); import curl_cffi" 2>nul
if errorlevel 1 (
    echo Installing curl_cffi into libs\ for browser impersonation...
    python -m pip install --target "%LIBS_DIR%" curl_cffi
)

python -c "import sys; sys.path.insert(0, r'%LIBS_DIR%'); import vlc" 2>nul
if errorlevel 1 (
    echo Installing python-vlc into libs\...
    python -m pip install --target "%LIBS_DIR%" python-vlc
)

REM Screen reader announcements go through accessible_output2, the same
REM library ZBox uses. Its JAWS support needs pywin32, which is installed
REM into Python itself rather than libs\: pywin32 does not start up
REM reliably from a pip --target folder, and the PyInstaller build needs
REM it in the same Python that runs build.bat.
python -c "import win32com.client" 2>nul
if errorlevel 1 (
    echo Installing pywin32 for screen reader announcements...
    python -m pip install pywin32
)
python -c "import sys; sys.path.insert(0, r'%LIBS_DIR%'); import accessible_output2.outputs.auto" 2>nul
if errorlevel 1 (
    echo Installing accessible_output2 into libs\ for screen reader announcements...
    python -m pip install --target "%LIBS_DIR%" --no-deps accessible_output2 libloader platform_utils
)

REM ffmpeg and the VLC engine itself (as opposed to the python-vlc
REM bindings just above) are used directly from dependencies\ -- no
REM separate copy is made in dev mode, since media_tug.py already knows
REM to fall back to dependencies\ffmpeg\ and dependencies\vlc\ directly
REM when it's not running as a frozen build. This is different from
REM build.bat, which does copy them into release\MediaTug\tools\ and
REM \app\, since those files need to be physically bundled with the
REM shipped exe -- an end user's machine won't have this project's
REM dependencies\ folder at all.
echo Checking ffmpeg (uses dependencies\ffmpeg\ as-is if it's already current)...
python "%~dp0tools\fetch_ffmpeg.py" --cache "%DEPS%\ffmpeg" --target "%DEPS%\ffmpeg"
if errorlevel 1 (
    echo Warning: could not fetch ffmpeg automatically. Downloads that need
    echo          audio extraction/conversion will fail until dependencies\ffmpeg\ffmpeg.exe
    echo          and dependencies\ffmpeg\ffprobe.exe exist. See dependencies\ffmpeg\fetch.log for details.
)

echo Checking aria2c (optional speed-up for downloads; uses dependencies\aria2\ as-is if already current)...
python "%~dp0tools\fetch_aria2.py" --cache "%DEPS%\aria2" --target "%DEPS%\aria2"
if errorlevel 1 (
    echo Warning: could not fetch aria2c automatically. Downloads will still
    echo          work, just with a single connection instead of 16 parallel
    echo          ones. See dependencies\aria2\fetch.log for details.
)

echo Checking VLC engine (uses dependencies\vlc\ as-is if it's already current)...
set VLC_DIR=
if exist "%ProgramFiles%\VideoLAN\VLC\libvlc.dll" set VLC_DIR=%ProgramFiles%\VideoLAN\VLC
if exist "%ProgramFiles(x86)%\VideoLAN\VLC\libvlc.dll" set VLC_DIR=%ProgramFiles(x86)%\VideoLAN\VLC
if defined VLC_DIR (
    python "%~dp0tools\sync_vlc.py" --source "%VLC_DIR%" --cache "%DEPS%\vlc" --dest "%DEPS%\vlc"
) else (
    python "%~dp0tools\sync_vlc.py" --cache "%DEPS%\vlc" --dest "%DEPS%\vlc"
)
if errorlevel 1 (
    echo Warning: could not fetch the VLC engine automatically, no VLC install
    echo          was found on this machine either, and there was no cached
    echo          copy from a previous run. Playback will fail until
    echo          dependencies\vlc\libvlc.dll exists. See dependencies\vlc\sync.log for details.
)

where deno >nul 2>nul
if errorlevel 1 (
    echo Deno not found. yt-dlp now needs it to solve YouTube's playback
    echo challenge. Attempting automatic install via winget...
    where winget >nul 2>nul
    if errorlevel 1 (
        echo winget not available. Install Deno manually from https://deno.com
    ) else (
        winget install DenoLand.Deno -e --silent --accept-package-agreements --accept-source-agreements
        if errorlevel 1 (
            echo Automatic Deno install failed. Install manually from https://deno.com
        ) else (
            echo Deno installed. You may need to restart this terminal for PATH to update.
        )
    )
)

exit /b 0
