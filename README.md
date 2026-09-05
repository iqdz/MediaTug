# Media Tug

An accessible, keyboard-first media player and downloader for Windows, built
from the ground up for screen reader users (NVDA, JAWS, Narrator).

- **Version:** 1.0.0 (see `VERSION.txt`)
- **Author:** Harith Alhamdani
- **License:** GNU General Public License v2 (GPL-2.0)
- **Platform:** Windows 10/11 (64-bit)
- **UI toolkit:** wxPython (native Windows controls)
- **Playback:** VLC engine via `python-vlc`
- **Downloads:** yt-dlp + ffmpeg + aria2

## What it is

Media Tug searches, plays, and downloads media from YouTube and the many
other sites yt-dlp supports, through a flat, keyboard-driven wxPython
interface with no custom graphics-heavy UI to confuse a screen reader. A
live status bar acts as a live region, SAPI5 speech announces key events on
a background thread, and short sound cues mark startup and search
completion.

## Key features

- **Search & play** — search YouTube (and other yt-dlp-supported sites) or
  paste a direct link; Enter plays, the context menu offers downloads.
  Playlist/channel links are detected automatically.
- **Dedicated player window** — Space/Ctrl+P play-pause, Ctrl+M mute,
  arrow keys for volume/seek, Ctrl+Shift+V to pop out video for a sighted
  viewer, Ctrl+D/Ctrl+L to download/copy the currently playing track.
- **Downloads** — save as MP3 (configurable bitrate) or video (configurable
  quality); channel/playlist batch downloads with resume support.
- **Fast downloads** — bundled aria2c parallelizes save-to-disk,
  channel/playlist, and video-fetch downloads (up to 16 connections) to get
  around YouTube's per-connection throttling; the play-now cache
  intentionally keeps yt-dlp's own downloader so Escape-to-cancel keeps
  working.
- **Cookie support** — browser cookie auto-detection (Firefox, Edge,
  Chrome, Brave, Vivaldi, Opera) or a manual `cookies.txt`, with automatic
  retry without cookies on failure.
- **Playback cache** — play-now items are cached locally and pruned by age
  (1/2/7/30 days), with a manual clear hotkey (Shift+Alt+C).
- **Settings (Alt+S)** — download path/format/quality, skip seconds, volume
  step, cache retention, cookie source, debug logging, auto-paste.
- **Accessibility extras** — a visual hint panel for sighted onlookers that
  is deliberately hidden from the accessibility tree, an Activity Log,
  and screen-reader detection that gates some spoken alerts.
- **Help & About** — Alt+H for a hotkey reference; an About page with
  version, licensing of bundled components, and links.

See `docs/MediaTug_documentation.txt` for the full internal documentation,
including hotkeys, folder layout, build steps, and versioning workflow.

## Getting started (development)

1. Install Python 3.
2. Run `run.bat`. It will:
   - Install `wxPython`, `curl_cffi`, and `python-vlc` into `libs\`
   - Fetch/version-check yt-dlp (nightly), ffmpeg, aria2c, and the VLC
     engine into `dependencies\`
   - Install Deno via winget if missing (yt-dlp needs it to solve
     YouTube's playback challenge)
   - Launch `media_tug.py`

## Building

- `build.bat` — builds the portable `release\MediaTug\` folder (PyInstaller
  onedir build, plus bundled VLC/ffmpeg/aria2c). Copy the folder anywhere
  and run `app\media_tug.exe`.
- `build_installer.bat` — builds `MediaTugSetup.msi` (requires WiX Toolset
  v3). Run this after `build.bat`. Produces a per-user installer with no
  admin rights required.

## Bundled components and licenses

| Component  | License |
|---|---|
| yt-dlp | Public domain (Unlicense) |
| VLC / libVLC | GNU LGPL v2.1+ |
| FFmpeg (Gyan.dev build) | GPL-3.0 |
| aria2 | GPL v2+ |
| wxPython | wxWindows Library Licence |
| Python | Python Software Foundation License |
| PyInstaller | GPL-2+ with the PyInstaller bootloader exception |
| Media Tug itself | GPL-2.0 |

## Support & contribution

- **Issues:** open a GitHub issue with a description, error message, and
  debug log output.
- **Email:** harith@gvoice.org
- **Support development:** [Ko-fi](https://ko-fi.com/happs#)

Media Tug is free software; see the GPL-2.0 license for full terms.
