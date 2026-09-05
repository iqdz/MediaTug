@echo off
setlocal

rem ============================================================
rem  Media Tug installer build script
rem  Requires: WiX Toolset v3 installed
rem            https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314.exe
rem            -- its installer sets the WIX environment variable
rem            this script relies on (with a folder-search fallback
rem            below in case that variable isn't visible yet).
rem  Requires: build.bat already run, so release\MediaTug\app exists.
rem
rem  Every run also writes output.log next to this script, with a full
rem  copy of everything printed here plus heat.exe/candle.exe/light.exe's
rem  own output -- so if something fails, the exact error text is saved
rem  and can be reread or shared even after this window closes.
rem  Logging works by re-running this same file as a child process with
rem  its output redirected to the log, then printing that log back out
rem  here -- so you still see everything on screen as normal, it's just
rem  also being saved.
rem ============================================================

if "%~1"=="__child__" goto :main

set "LOGFILE=%~dp0output.log"
call "%~f0" __child__ > "%LOGFILE%" 2>&1
set "RESULT=%ERRORLEVEL%"

type "%LOGFILE%"
echo.
echo Full output was also saved to: %LOGFILE%
if not "%RESULT%"=="0" echo Exit code: %RESULT%
echo.
echo Press any key to close this window...
pause >nul
endlocal
exit /b %RESULT%

:main
set "EXITCODE=0"

rem ============================================================
rem  Locate WiX Toolset v3.
rem  The official installer sets a WIX environment variable, but only
rem  for command prompt windows opened AFTER installation -- if this
rem  is the same window you had open while installing, %WIX% will
rem  look undefined even though WiX is present. Rather than just
rem  telling you to reopen the window, this also searches the usual
rem  install folders directly, so it works either way.
rem
rem  "Program Files (x86)" contains literal parentheses in its own
rem  *value* (not just a variable name with parens in it) -- searching
rem  it via a for-loop nested inside other ( ... ) blocks confuses
rem  cmd's paren-matching and produces a cryptic ") was unexpected at
rem  this time." error that looks unrelated to WiX entirely. Using a
rem  plain subroutine call and single-line (non-block) for/if
rem  statements below avoids that: neither needs to scan ahead for a
rem  matching close-paren, so the literal "(x86)" in the path can't
rem  confuse anything.
rem ============================================================
set "WIXBIN="

if defined WIX if exist "%WIX%bin\candle.exe" set "WIXBIN=%WIX%bin\"

if not defined WIXBIN call :find_wix "%ProgramFiles(x86)%"
if not defined WIXBIN call :find_wix "%ProgramFiles%"
goto :after_find_wix

:find_wix
for /d %%D in ("%~1\WiX Toolset v3*") do if exist "%%D\bin\candle.exe" set "WIXBIN=%%D\bin\"
exit /b

:after_find_wix

if not defined WIXBIN (
    echo.
    echo ERROR: Could not find WiX Toolset v3 candle.exe, heat.exe, or light.exe.
    echo.
    echo        If you have not installed it yet:
    echo          1. Download and run the WiX v3.14.1 installer from:
    echo             https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314.exe
    echo          2. Do NOT use wixtoolset.org/releases -- that page now
    echo             redirects to WiX v4-v7, a different tool entirely
    echo             with no candle.exe, heat.exe, or light.exe.
    echo          3. Close this window, open a NEW Command Prompt, then run this script again
    echo.
    echo        If you already installed it and are still seeing this: the WIX
    echo        environment variable is only set for new Command Prompt windows
    echo        opened after installation. Close this window and reopen it.
    set "EXITCODE=1"
    goto :end
)

echo Using WiX Toolset at: %WIXBIN%

set APPDIR=%~dp0release\MediaTug\app
set TOOLSDIR=%~dp0release\MediaTug\tools
if not exist "%APPDIR%\media_tug.exe" (
    echo ERROR: %APPDIR%\media_tug.exe not found.
    echo        Run build.bat first to produce the release folder.
    set "EXITCODE=1"
    goto :end
)
if not exist "%TOOLSDIR%\ffmpeg.exe" (
    echo ERROR: %TOOLSDIR%\ffmpeg.exe not found.
    echo        build.bat's ffmpeg fetch step did not complete successfully --
    echo        re-run build.bat and check its Step 6 output before packaging
    echo        an installer that would otherwise ship with no working
    echo        downloads ^(see fetch_ffmpeg.py^).
    set "EXITCODE=1"
    goto :end
)

echo.
echo === Step 1: Harvesting app\ and tools\ file lists ===
rem heat.exe scans each folder and generates a fragment listing every
rem file as its own Component, so nothing has to be hand-typed or kept
rem in sync as the PyInstaller build's contents -- or ffmpeg's own
rem files -- change from build to build. tools\ is harvested exactly
rem the same way app\ is; this used to be skipped entirely, which meant
rem the MSI never actually included ffmpeg.exe even when build.bat had
rem successfully placed it in release\MediaTug\tools\ -- every MSI
rem install got an empty tools\ folder and downloads silently failed.
"%WIXBIN%heat.exe" dir "%APPDIR%" ^
    -cg AppFilesGroup ^
    -gg -scom -sreg -sfrag -srd ^
    -dr APPFOLDER ^
    -var var.AppSourceDir ^
    -out AppFiles.wxs
if errorlevel 1 (
    echo HEAT FAILED ^(app\^). See errors above.
    set "EXITCODE=1"
    goto :end
)
"%WIXBIN%heat.exe" dir "%TOOLSDIR%" ^
    -cg ToolsFilesGroup ^
    -gg -scom -sreg -sfrag -srd ^
    -dr TOOLSFOLDER ^
    -var var.ToolsSourceDir ^
    -out ToolsFiles.wxs
if errorlevel 1 (
    echo HEAT FAILED ^(tools\^). See errors above.
    set "EXITCODE=1"
    goto :end
)

echo.
echo === Step 2: Compiling ===
"%WIXBIN%candle.exe" -ext WixUIExtension -ext WixUtilExtension ^
    -dAppSourceDir="%APPDIR%" ^
    -dToolsSourceDir="%TOOLSDIR%" ^
    installer.wxs AppFiles.wxs ToolsFiles.wxs
if errorlevel 1 (
    echo CANDLE FAILED. See errors above.
    set "EXITCODE=1"
    goto :end
)

echo.
echo === Step 3: Linking (producing the .msi) ===
rem The -sice suppressions below silence a few validation warnings that
rem are common false positives specifically for per-user installs that
rem target a custom folder outside Program Files -- they do not indicate
rem a real problem with this installer.
"%WIXBIN%light.exe" -ext WixUIExtension -ext WixUtilExtension ^
    -loc CustomStrings.wxl ^
    -sice:ICE80 -sice:ICE64 -sice:ICE91 ^
    -out MediaTugSetup.msi ^
    installer.wixobj AppFiles.wixobj ToolsFiles.wixobj
if errorlevel 1 (
    echo LIGHT FAILED. See errors above.
    set "EXITCODE=1"
    goto :end
)

echo.
echo === Done: MediaTugSetup.msi ===

:end
endlocal & exit /b %EXITCODE%
