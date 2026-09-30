@echo off
REM Script to update MediaTug repository description
REM Requires: GitHub CLI (gh) to be installed
REM Usage: Run this script from any directory

echo Updating MediaTug repository description...

gh repo edit iqdz/MediaTug --description "Accessible media player and downloader for Windows — keyboard-first, screen-reader friendly, with fast multi-connection downloads."

if %errorlevel% equ 0 (
    echo.
    echo Success! Repository description has been updated.
) else (
    echo.
    echo Error: Failed to update repository description.
    echo Please ensure GitHub CLI (gh) is installed and you are authenticated.
    echo.
    echo Install GitHub CLI from: https://cli.github.com/
    pause
    exit /b 1
)

pause
