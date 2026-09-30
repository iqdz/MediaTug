# Media Tug — build & installer pipeline

Two outputs, both from the same source: the existing portable folder,
and a new per-user MSI as a second, optional install path. Nothing about
the portable workflow changes.

## 1. One-time setup

- Install PyInstaller: `pip install pyinstaller`
- Install WiX Toolset v3 (only needed for the MSI, not the portable
  build): get `wix314.exe` from
  https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314.exe —
  **not** from wixtoolset.org/releases, which now redirects to WiX v4-v7
  (a different, incompatible tool with no candle.exe/heat.exe/light.exe).
  After installing, close and reopen your Command Prompt so it picks up
  the `WIX` environment variable the installer sets — `build_installer.bat`
  also searches `Program Files\WiX Toolset v3*` on its own as a fallback,
  in case that variable isn't visible yet.
- **Internet access on the build machine (first run only, usually).**
  VLC, ffmpeg, aria2c, and yt-dlp are no longer sourced from whatever
  happens to be installed locally — `build.bat` downloads and
  version-checks the current stable VLC build straight from VideoLAN,
  the current stable "full" ffmpeg build straight from Gyan.dev, the
  latest aria2 release (aria2c.exe) from GitHub, and yt-dlp's latest
  nightly from PyPI, keeping all four in a `dependencies\` folder next
  to the source (`dependencies\vlc\`, `dependencies\ffmpeg\`,
  `dependencies\aria2\`, `dependencies\yt-dlp\`) shared with `run.bat`. This is what actually
  fixes a real bug: builds used to only work correctly for *downloads*
  on a machine that coincidentally already had ffmpeg on its PATH —
  everyone else's copy silently failed every download while playback
  kept working fine. See `tools\sync_vlc.py`/`tools\fetch_ffmpeg.py`/
  `tools\update_ytdlp.py` for details.
  `dependencies\` is meant to be looked at, not just a hidden cache —
  drop files into any of its subfolders yourself (e.g. a manually
  downloaded/extracted ffmpeg build, if the automatic download ever hits
  a flaky network or gets briefly locked by antivirus scanning) and
  they're used as-is; nothing here overwrites a file that's already
  present and working just because its exact version isn't recorded — it
  only replaces something once it can confirm a newer version is
  actually available. A local VLC install is still used as an offline
  fallback if VideoLAN can't be reached; there's no equivalent ffmpeg
  fallback beyond what's already in `dependencies\ffmpeg\`.
- `tools\fetch_ffmpeg.py` needs `py7zr` to unpack Gyan.dev's "full" build
  (only offered as `.7z`) — it pip-installs this into your Python
  environment itself the first time it's needed. Build-time only;
  nothing about it ships in the frozen app.
- Install WiX Toolset v3 (only needed for the MSI, not the portable
  build): get `wix314.exe` from
  https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314.exe —
  **not** from wixtoolset.org/releases, which now redirects to WiX v4-v7
  (a different, incompatible tool with no candle.exe/heat.exe/light.exe).
  After installing, close and reopen your Command Prompt so it picks up
  the `WIX` environment variable the installer sets — `build_installer.bat`
  also searches `Program Files\WiX Toolset v3*` on its own as a fallback,
  in case that variable isn't visible yet.

## 2. Build the portable folder

```
build.bat
```

Produces `release\MediaTug\` with `app\`, `cookies\`, `data\`, `logs\`,
and `tools\` — this folder already *is* the portable app; copy it
anywhere and run `app\media_tug.exe`. Watch the console output: it
warns clearly if VLC or ffmpeg couldn't be fetched (from the internet,
a local install, or a previous build's cache) and copied in. `tools\`
also gets `aria2c.exe`, which makes save-to-disk, channel, and
video-fetch downloads several times faster by opening up to 16 parallel
connections (YouTube throttles single connections hard). It is optional:
without it everything still works, just slower, and the build only
warns. The play-now cache download intentionally keeps yt-dlp's native
downloader so Escape-to-cancel keeps working.

## 3. Build the MSI (optional second distribution path)

```
build_installer.bat
```

This harvests every file under both `release\MediaTug\app` and
`release\MediaTug\tools` automatically (via `heat.exe`), so you never
have to hand-maintain a file list as PyInstaller's output — or
ffmpeg's — changes between builds. Produces `release\installer\MediaTugSetup.msi`.
(Older versions of this script only harvested `app\`, so every MSI
install shipped with an empty `tools\` folder and no working ffmpeg —
downloads failed for every MSI user regardless of what the portable
build looked like on the developer's own machine. That's fixed now.)

Every run also writes `release\installer\output.log`, containing
everything printed to the window plus `heat.exe`/`candle.exe`/`light.exe`'s
own diagnostic output. You still see all of it on screen as normal — it's
just also saved, so if a build fails you can reread or share the exact
error text afterward instead of having to catch it before the window
closes.

What installing it does:
- No admin prompt — installs entirely under the current user
- Installs to `C:\Users\<name>\MediaTug`, alongside Documents/Downloads/Music
- Two real decisions in the wizard: click Next, then Finish (License
  page is skipped since there isn't a real EULA to review)
- Every dialog is a standard native Windows dialog — reads correctly in
  NVDA/JAWS with no custom UI work needed
- Adds a Start Menu entry and a Desktop shortcut
- "Launch Media Tug now" checkbox on the last screen
- Uninstalling (from Settings > Apps, or the Start Menu shortcut) removes
  `app\` and the shortcuts, but leaves `cookies\`, `data\`, and `logs\`
  alone — same as removing a portable copy wouldn't touch a backed-up
  copy of your settings elsewhere.

- The Welcome page's message is overridden in `installer\CustomStrings.wxl` (a
  small localization file `build_installer.bat` passes to `light.exe`
  via `-loc`). To change that text again later, edit the
  `WelcomeDlgDescription` string in that file — no need to touch
  `installer\installer.wxs` or redefine the dialog itself.

## Before your first real release

- `installer\installer.wxs` has placeholder GUIDs for `UpgradeCode` and every
  `Component`. Keep `UpgradeCode` the same forever (it's how future
  versions recognize "this is an upgrade of the same app"), but replace
  each `Component`'s `Guid` with your own freshly generated one
  (PowerShell: `[guid]::NewGuid()`).
- The app's About page shows a version line read from `VERSION.txt` at
  the repo root (currently `1.0.0`). To bump the version, edit that one
  text file -- `build.bat` reads it, prints it, and copies it into the
  release folder so the built app states it in About. For MSI builds,
  keep the `Version` in `installer\installer.wxs` in sync (WiX wants four parts,
  e.g. `1.0.0.0`).
- Test the MSI itself with NVDA or JAWS running, the same way you'd test
  the app — the wizard should read exactly like any other Windows
  installer.

## What changed in media_tug.py itself

A short spoken line was added right after the main window is shown, so
it's heard on every launch: *"This app created by Harith Alhamdani for the
community, I hope it brings more ease of use."* It goes through the
same `announce()`/SAPI5 path as every other spoken message in the app,
so it plays whether or not a screen reader is running.
