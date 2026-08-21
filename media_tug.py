import wx
import threading
import webbrowser
import subprocess
import os
import sys
import time
import datetime
import traceback
import json
import urllib.parse


def get_base_dir():
    """Resolves the portable install root. When packaged, the executable
    lives in an 'app' subfolder alongside sibling 'cookies', 'data', and
    'logs' folders -- this returns the parent of 'app' in that case so all
    those siblings can be found consistently. When running media_tug.py
    directly during development (not inside an 'app' folder), the script's
    own folder is used instead, and cookies/data/logs are created next to
    it."""
    if getattr(sys, 'frozen', False):
        script_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(script_dir).lower() == 'app':
        return os.path.dirname(script_dir)
    return script_dir


BASE_DIR = get_base_dir()
DATA_DIR = os.path.join(BASE_DIR, 'data')
COOKIES_DIR = os.path.join(BASE_DIR, 'cookies')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
for _dir in (DATA_DIR, COOKIES_DIR, LOGS_DIR):
    os.makedirs(_dir, exist_ok=True)

# Dev-mode (non-frozen) portability: dependencies installed by run.bat land
# in a local 'libs' folder next to this script instead of the system's
# global site-packages, via 'pip install --target'. Adding it to sys.path
# here means the whole folder -- script plus its dependencies -- can be
# copied to another machine with a compatible Python already installed and
# just work, with no re-installation. This has no effect in a PyInstaller
# build, where everything is already bundled into the exe.
if not getattr(sys, 'frozen', False):
    _libs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'libs')
    if os.path.isdir(_libs_dir) and _libs_dir not in sys.path:
        sys.path.insert(0, _libs_dir)

from yt_dlp import YoutubeDL


def create_desktop_shortcut():
    """Creates a Windows desktop shortcut to Media Tug via PowerShell, so
    no extra Python package is needed just for this. Shared by the
    Settings dialog's button and the first-run prompt. Returns
    (success: bool, message: str)."""
    try:
        desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
        shortcut_path = os.path.join(desktop, 'Media Tug.lnk')

        if getattr(sys, 'frozen', False):
            target = sys.executable
            workdir = os.path.dirname(target)
        else:
            # Dev mode: launch via run.bat sitting next to this script,
            # since double-clicking a .py file directly won't reliably
            # use the right Python or working directory.
            script_dir = os.path.dirname(os.path.abspath(__file__))
            target = os.path.join(script_dir, 'run.bat')
            workdir = script_dir

        ps_script = (
            "$WshShell = New-Object -ComObject WScript.Shell;"
            f"$Shortcut = $WshShell.CreateShortcut('{shortcut_path}');"
            f"$Shortcut.TargetPath = '{target}';"
            f"$Shortcut.WorkingDirectory = '{workdir}';"
            "$Shortcut.Save()"
        )
        subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_script],
            check=True, capture_output=True, text=True
        )
        return True, "Desktop shortcut created."
    except Exception as e:
        return False, f"Could not create shortcut: {str(e)}"

# ---------------------------------------------------------------------------
# PlayerController
#
# Backed by libvlc via python-vlc. Renders into a raw win_id handle with no
# visible player chrome, so there is no overlay UI that can steal keyboard
# focus. yt-dlp resolves the direct stream URL; libvlc just plays that URL
# directly rather than embedding YouTube's own player markup.
#
# Requires: pip install python-vlc
# Requires: libvlc runtime present (VLC install, or bundle libvlc.dll and
# the plugins folder alongside the exe for a portable build).
# ---------------------------------------------------------------------------
if getattr(sys, 'frozen', False):
    # In a portable build, libvlc.dll and the plugins folder are bundled
    # directly next to the exe (see build.bat). python-vlc's loader needs
    # to be pointed at them explicitly here, before "import vlc" runs,
    # since a frozen exe's working directory isn't reliably the exe's own
    # folder and libvlc won't otherwise be found on PATH.
    _app_dir = os.path.dirname(os.path.abspath(sys.executable))
    os.environ['PATH'] = _app_dir + os.pathsep + os.environ.get('PATH', '')
    os.environ.setdefault('PYTHON_VLC_LIB_PATH', os.path.join(_app_dir, 'libvlc.dll'))
    os.environ.setdefault('VLC_PLUGIN_PATH', os.path.join(_app_dir, 'plugins'))
import vlc


class PlayerController:
    def __init__(self, log_callback, video_panel_handle=None, debug_callback=None,
                 native_log_path=None, debug_logging_enabled=False):
        self.log = log_callback
        self.debug_log = debug_callback or (lambda msg: None)

        # libvlc's own internal log, written to file. This shows the real
        # HTTP status code and access-module detail behind a generic
        # "Playback error encountered" event, which the event manager alone
        # does not expose. Only enabled when debug logging is actually
        # turned on in Settings -- otherwise this was writing
        # vlc_native.log on every run regardless of the setting.
        instance_args = ['--quiet']
        if native_log_path and debug_logging_enabled:
            instance_args += ['--verbose=2', '--file-logging', f'--logfile={native_log_path}']
        self.instance = vlc.Instance(instance_args)
        self.media_player = self.instance.media_player_new()

        if video_panel_handle:
            self.media_player.set_hwnd(video_panel_handle)

        self.is_muted = False
        self.volume = 70
        self.media_player.audio_set_volume(self.volume)
        self.current_url = None

        self._attach_event_handlers()

    def _attach_event_handlers(self):
        """Surfaces libvlc's own state transitions to the log. This is the
        most useful diagnostic when playback appears to do something other
        than expected, since it shows exactly what libvlc thinks is
        happening (opening, buffering, playing, error) rather than relying
        on guesswork from the app side."""
        events = self.media_player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerOpening,
                             lambda e: wx.CallAfter(self.debug_log, "VLC event: Opening media"))
        events.event_attach(vlc.EventType.MediaPlayerBuffering,
                             lambda e: wx.CallAfter(self.debug_log, f"VLC event: Buffering {e.u.new_cache:.0f}%"))
        events.event_attach(vlc.EventType.MediaPlayerPlaying,
                             lambda e: wx.CallAfter(self.debug_log, "VLC event: Playing started"))
        events.event_attach(vlc.EventType.MediaPlayerPaused,
                             lambda e: wx.CallAfter(self.debug_log, "VLC event: Paused"))
        events.event_attach(vlc.EventType.MediaPlayerEndReached,
                             lambda e: wx.CallAfter(self.debug_log, "VLC event: End of media reached"))
        events.event_attach(vlc.EventType.MediaPlayerEncounteredError,
                             lambda e: wx.CallAfter(self.log, "VLC event: Playback error encountered"))

    def load(self, local_path):
        self.current_url = local_path
        media = self.instance.media_new(local_path)
        self.media_player.set_media(media)
        result = self.media_player.play()
        self.debug_log(f"Player: play() returned {result} for local file {local_path}")
        self.log(f"Player: loading {local_path}")

    def toggle_play(self):
        if not self.current_url:
            self.log("Player: nothing loaded to play or pause.")
            return
        if self.media_player.is_playing():
            self.media_player.pause()
            self.log("Player: Paused")
        else:
            self.media_player.play()
            self.log("Player: Playing")

    def toggle_mute(self):
        self.is_muted = not self.is_muted
        self.media_player.audio_set_mute(self.is_muted)
        self.log("Player: Muted" if self.is_muted else "Player: Unmuted")

    def change_volume(self, delta):
        self.volume = max(0, min(100, self.volume + delta))
        self.media_player.audio_set_volume(self.volume)
        self.log(f"Player: Volume {self.volume}%")

    def seek(self, seconds):
        if not self.current_url:
            self.log("Player: nothing loaded to seek.")
            return
        current_ms = self.media_player.get_time()
        new_ms = max(0, current_ms + seconds * 1000)
        self.media_player.set_time(new_ms)
        direction = "forward" if seconds > 0 else "back"
        self.log(f"Player: Skipped {direction} {abs(seconds)} seconds")

    def stop(self):
        self.media_player.stop()
        self.current_url = None
        self.log("Player: Stopped")

    def shutdown(self):
        self.media_player.stop()
        self.media_player.release()
        self.instance.release()


class YtdlpLoggerBridge:
    """Routes yt-dlp's own internal debug/warning/error messages into the
    app's debug log. quiet=True normally suppresses these, but they often
    contain the exact PO-Token or JS-runtime guidance yt-dlp emits when it
    hits YouTube's current anti-bot checks, so they are captured here
    instead of thrown away."""
    def __init__(self, debug_callback):
        self.debug_callback = debug_callback

    def debug(self, msg):
        if msg.startswith('[debug] '):
            return  # too noisy to be useful
        wx.CallAfter(self.debug_callback, f"yt-dlp: {msg}")

    def warning(self, msg):
        wx.CallAfter(self.debug_callback, f"yt-dlp warning: {msg}")

    def error(self, msg):
        wx.CallAfter(self.debug_callback, f"yt-dlp error: {msg}")


class SettingsDialog(wx.Dialog):
    """Accessible Settings Dialog for managing download preferences, quality targets, and hotkey behavior."""
    def __init__(self, parent, config):
        super().__init__(parent, title="Application Settings", size=(480, 900))
        self.config = config

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Search Behavior
        search_behavior_box = wx.StaticBox(panel, label="Search Behavior")
        search_behavior_sizer = wx.StaticBoxSizer(search_behavior_box, wx.VERTICAL)
        self.auto_paste_check = wx.CheckBox(
            search_behavior_box,
            label="Automatically detect and paste media URL from clipboard"
        )
        self.auto_paste_check.SetValue(self.config.get('auto_paste_clipboard', False))
        self.auto_paste_check.SetToolTip(
            "When enabled, switching back to this app checks the clipboard; if it "
            "holds a web link, it's pasted into the search box automatically."
        )
        search_behavior_sizer.Add(self.auto_paste_check, 0, wx.ALL, 5)
        sizer.Add(search_behavior_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # Download Folder Choice
        folder_box = wx.StaticBox(panel, label="Default Download Directory")
        folder_sizer = wx.StaticBoxSizer(folder_box, wx.HORIZONTAL)
        self.path_ctrl = wx.TextCtrl(folder_box, value=self.config.get('download_path', os.getcwd()))
        folder_sizer.Add(self.path_ctrl, 1, wx.EXPAND | wx.ALL, 5)
        browse_btn = wx.Button(folder_box, label="Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, self.on_browse)
        folder_sizer.Add(browse_btn, 0, wx.ALL, 5)
        sizer.Add(folder_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # Download Format: Video or Audio (mutually exclusive)
        format_box = wx.StaticBox(panel, label="Download Format")
        format_sizer = wx.StaticBoxSizer(format_box, wx.VERTICAL)
        self.format_radio = wx.RadioBox(
            format_box, label="Download as", choices=["MP3 Audio", "MP4 Video"],
            style=wx.RA_SPECIFY_ROWS
        )
        self.format_radio.SetStringSelection(self.config.get('download_format', 'MP3 Audio'))
        self.format_radio.Bind(wx.EVT_RADIOBOX, self.on_format_change)
        format_sizer.Add(self.format_radio, 0, wx.EXPAND | wx.ALL, 5)

        self.video_quality_choice = wx.Choice(
            format_box,
            choices=["Best available", "1080p", "720p", "480p", "360p"]
        )
        self.video_quality_choice.SetStringSelection(self.config.get('video_quality', 'Best available'))
        format_sizer.Add(self.video_quality_choice, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(format_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # Audio Quality Selection
        audio_box = wx.StaticBox(panel, label="MP3 Audio Quality Preference")
        audio_sizer = wx.StaticBoxSizer(audio_box, wx.VERTICAL)
        self.audio_choice = wx.Choice(audio_box, choices=["128 kbps (Standard)", "192 kbps (High)", "320 kbps (Maximum)"])
        self.audio_choice.SetStringSelection(self.config.get('audio_quality', '192 kbps (High)'))
        audio_sizer.Add(self.audio_choice, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(audio_sizer, 0, wx.EXPAND | wx.ALL, 10)

        self.on_format_change(None)  # sync initial enabled/disabled state

        # Playback Hotkey Behavior
        hotkey_box = wx.StaticBox(panel, label="Playback Control Settings")
        hotkey_sizer = wx.StaticBoxSizer(hotkey_box, wx.VERTICAL)

        skip_label = wx.StaticText(hotkey_box, label="Skip length in seconds (Left/Right arrow in the player window):")
        hotkey_sizer.Add(skip_label, 0, wx.ALL, 5)
        self.skip_spin = wx.SpinCtrl(hotkey_box, min=1, max=120, initial=self.config.get('skip_seconds', 10))
        self.skip_spin.SetName("Skip length in seconds")
        hotkey_sizer.Add(self.skip_spin, 0, wx.ALL, 5)

        vol_label = wx.StaticText(hotkey_box, label="Volume step per key press (Up/Down arrow in the player window):")
        hotkey_sizer.Add(vol_label, 0, wx.ALL, 5)
        self.vol_spin = wx.SpinCtrl(hotkey_box, min=1, max=50, initial=self.config.get('volume_step', 5))
        self.vol_spin.SetName("Volume step percent")
        hotkey_sizer.Add(self.vol_spin, 0, wx.ALL, 5)

        note = wx.StaticText(
            hotkey_box,
            label=("Note: opening a track shows a dedicated player window. Space or Ctrl+P "
                   "play/pause, Ctrl+M mutes, arrows seek and adjust volume, Escape or Ctrl+W "
                   "closes it. These only apply while that window is open.")
        )
        note.Wrap(400)
        hotkey_sizer.Add(note, 0, wx.EXPAND | wx.ALL, 5)

        sizer.Add(hotkey_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # Debug Logging
        debug_box = wx.StaticBox(panel, label="Diagnostics")
        debug_sizer = wx.StaticBoxSizer(debug_box, wx.VERTICAL)
        self.debug_check = wx.CheckBox(debug_box, label="Enable debug logging")
        self.debug_check.SetValue(self.config.get('debug_logging', False))
        self.debug_check.SetToolTip(
            "Logs detailed player state, stream resolution, and error detail "
            "to the Activity Log and to media_tug_debug.log in the logs folder. "
            "Also enables VLC's own vlc_native.log for deep playback errors. "
            "Changing this takes effect after restarting the app."
        )
        debug_sizer.Add(self.debug_check, 0, wx.ALL, 5)

        self.cookies_check = wx.CheckBox(
            debug_box,
            label="Use browser cookies for YouTube requests (helps with 403 errors)"
        )
        self.cookies_check.SetValue(self.config.get('use_browser_cookies', False))
        debug_sizer.Add(self.cookies_check, 0, wx.ALL, 5)

        source_label = wx.StaticText(debug_box, label="Browser cookie source:")
        debug_sizer.Add(source_label, 0, wx.LEFT | wx.TOP, 5)
        self.cookie_source_choice = wx.Choice(
            debug_box,
            choices=["Auto - try all installed browsers", "Firefox", "Edge", "Chrome", "Brave", "Vivaldi", "Opera"]
        )
        self.cookie_source_choice.SetStringSelection(self.config.get('cookie_source', 'Auto - try all installed browsers'))
        self.cookie_source_choice.SetToolTip(
            "Auto tries each browser in order and uses the first one that works. "
            "Firefox does not have the Windows DPAPI decryption issue that some "
            "Chromium browsers currently have; if a specific browser fails, try "
            "Auto or pick Firefox directly."
        )
        debug_sizer.Add(self.cookie_source_choice, 0, wx.EXPAND | wx.ALL, 5)

        cookies_file_label = wx.StaticText(
            debug_box,
            label=("Cookies file path (optional). A cookies.txt dropped in the app's "
                   "'cookies' folder is used automatically without setting anything here. "
                   "Export with a browser extension like 'Get cookies.txt LOCALLY':")
        )
        cookies_file_label.Wrap(400)
        debug_sizer.Add(cookies_file_label, 0, wx.ALL, 5)

        cookies_file_row = wx.BoxSizer(wx.HORIZONTAL)
        self.cookies_file_ctrl = wx.TextCtrl(debug_box, value=self.config.get('cookies_file_path', ''))
        self.cookies_file_ctrl.SetName("Cookies file path")
        cookies_file_row.Add(self.cookies_file_ctrl, 1, wx.EXPAND | wx.ALL, 5)
        cookies_browse_btn = wx.Button(debug_box, label="Browse...")
        cookies_browse_btn.Bind(wx.EVT_BUTTON, self.on_browse_cookies_file)
        cookies_file_row.Add(cookies_browse_btn, 0, wx.ALL, 5)
        debug_sizer.Add(cookies_file_row, 0, wx.EXPAND | wx.ALL, 5)

        sizer.Add(debug_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # Playback cache retention
        retention_box = wx.StaticBox(panel, label="Delete Played Video/Audio After")
        retention_sizer = wx.StaticBoxSizer(retention_box, wx.VERTICAL)
        self.retention_choice = wx.Choice(
            retention_box,
            choices=["1 day", "2 days", "7 days", "30 days"]
        )
        self.retention_choice.SetStringSelection(self.config.get('cache_retention', '7 days'))
        self.retention_choice.SetToolTip(
            "Files played from the app are kept in a local cache for offline "
            "re-play and are automatically deleted once older than this."
        )
        retention_sizer.Add(self.retention_choice, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(retention_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # Desktop shortcut
        shortcut_box = wx.StaticBox(panel, label="Desktop Shortcut")
        shortcut_sizer = wx.StaticBoxSizer(shortcut_box, wx.VERTICAL)
        shortcut_btn = wx.Button(shortcut_box, label="Create a shortcut on the desktop")
        shortcut_btn.Bind(wx.EVT_BUTTON, self.on_create_shortcut)
        shortcut_sizer.Add(shortcut_btn, 0, wx.ALL, 5)
        sizer.Add(shortcut_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # Save / Cancel Buttons
        btn_sizer = wx.StdDialogButtonSizer()
        save_btn = wx.Button(panel, wx.ID_SAVE)
        cancel_btn = wx.Button(panel, wx.ID_CANCEL)
        save_btn.Bind(wx.EVT_BUTTON, self.on_save)
        btn_sizer.AddButton(save_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        panel.SetSizer(sizer)

    def on_format_change(self, event):
        is_video = self.format_radio.GetStringSelection() == 'MP4 Video'
        self.video_quality_choice.Enable(is_video)
        self.audio_choice.Enable(not is_video)

    def on_browse(self, event):
        dlg = wx.DirDialog(self, "Choose Default Download Directory", style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            self.path_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def on_browse_cookies_file(self, event):
        dlg = wx.FileDialog(self, "Choose cookies.txt file", defaultDir=COOKIES_DIR,
                             wildcard="Text files (*.txt)|*.txt|All files (*.*)|*.*",
                             style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            self.cookies_file_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def on_create_shortcut(self, event):
        success, message = create_desktop_shortcut()
        if success:
            wx.MessageBox(message, "Success", wx.OK | wx.ICON_INFORMATION)
        else:
            wx.MessageBox(message, "Error", wx.OK | wx.ICON_ERROR)

    def on_save(self, event):
        self.config['auto_paste_clipboard'] = self.auto_paste_check.GetValue()
        self.config['download_path'] = self.path_ctrl.GetValue()
        self.config['download_format'] = self.format_radio.GetStringSelection()
        self.config['video_quality'] = self.video_quality_choice.GetStringSelection()
        self.config['audio_quality'] = self.audio_choice.GetStringSelection()
        self.config['skip_seconds'] = self.skip_spin.GetValue()
        self.config['volume_step'] = self.vol_spin.GetValue()
        self.config['debug_logging'] = self.debug_check.GetValue()
        self.config['use_browser_cookies'] = self.cookies_check.GetValue()
        self.config['cookie_source'] = self.cookie_source_choice.GetStringSelection()
        self.config['cookies_file_path'] = self.cookies_file_ctrl.GetValue().strip()
        self.config['cache_retention'] = self.retention_choice.GetStringSelection()
        self.EndModal(wx.ID_OK)


class PlayerWindow(wx.Dialog):
    """Dedicated modal player window. Opens on play, captures keyboard
    focus completely, and stays open until the user explicitly closes it
    with Escape or Ctrl+W. This solves the earlier problem of arrow-key
    and hotkey transport controls being hard to reach because focus stayed
    on the results list. Deliberately minimal content -- one status
    control, no nested group boxes -- to cut down on announced verbiage
    each time it's focused; full instructions live in the window title
    (announced once on open) and the tooltip, not repeated on every
    focus."""

    def __init__(self, parent, player, title, app_config):
        super().__init__(
            parent,
            title=f"Now Playing: {title} - Space or Ctrl+P Play/Pause, "
                  f"Ctrl+M Mute, Arrows Seek/Volume, Escape or Ctrl+W Close",
            style=wx.DEFAULT_DIALOG_STYLE
        )
        self.player = player
        self.app_config = app_config

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.status_ctrl = wx.TextCtrl(panel, value=f"Loading: {title}", style=wx.TE_READONLY)
        self.status_ctrl.SetName("Player status")
        self.status_ctrl.SetToolTip(
            "Space or Ctrl+P: play or pause. Ctrl+M: mute. "
            "Up/Down: volume. Left/Right: seek. Escape or Ctrl+W: close."
        )
        sizer.Add(self.status_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        panel.SetSizer(sizer)

        # EVT_CHAR_HOOK fires before normal key dispatch to child controls,
        # so these shortcuts work no matter which control inside the
        # dialog currently has focus, without needing a separate
        # AcceleratorTable entry per key.
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key_down)
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.SetSize((420, 130))
        self.Centre()

    def on_key_down(self, event):
        keycode = event.GetKeyCode()
        ctrl = event.ControlDown()

        if keycode == wx.WXK_ESCAPE:
            self.Close()
        elif ctrl and keycode == ord('W'):
            self.Close()
        elif keycode == wx.WXK_SPACE or (ctrl and keycode == ord('P')):
            self.player.toggle_play()
        elif ctrl and keycode == ord('M'):
            self.player.toggle_mute()
        elif keycode == wx.WXK_UP:
            self.player.change_volume(self.app_config.get('volume_step', 5))
        elif keycode == wx.WXK_DOWN:
            self.player.change_volume(-self.app_config.get('volume_step', 5))
        elif keycode == wx.WXK_RIGHT:
            self.player.seek(self.app_config.get('skip_seconds', 10))
        elif keycode == wx.WXK_LEFT:
            self.player.seek(-self.app_config.get('skip_seconds', 10))
        else:
            event.Skip()

    def update_status(self, text):
        wx.CallAfter(self.status_ctrl.SetValue, text)

    def on_close(self, event):
        self.player.stop()
        self.EndModal(wx.ID_CLOSE)


class AboutDialog(wx.Dialog):
    ABOUT_TEXT = """Media Tug
By Harith

An accessible media player and downloader for Windows, built with screen
reader users as the primary audience from the start.

License: GNU General Public License version 2 (GPL-2.0)
This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation, version 2 of the License. This program is
distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. Full license text: https://www.gnu.org/licenses/gpl-2.0.html

How to use:
Type a search term, or paste a link, into the search box and press Enter.
Arrow through results and press Enter to play, or open the context menu
for download options. Pasting a channel or playlist link and pressing
Enter opens a download confirmation for the whole thing automatically.
The player opens in its own window and stays open until you close it.

Hotkeys:
Ctrl+F: focus the search box.
Alt+S: open Settings.
Enter on a result: play it.
Escape on the results list: close the results panel.
In the player window: Space or Ctrl+P play or pause, Ctrl+M mute,
Up and Down arrows adjust volume, Left and Right arrows seek, Escape
or Ctrl+W close the player.
Escape on this About page: close it.
"""

    SUPPORTED_SITES_URL = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
    GITHUB_REPO_URL = "https://github.com/iqdz/"
    KOFI_URL = "https://ko-fi.com/happs#"
    CONTACT_EMAIL = "harith@gvoice.org"

    def __init__(self, parent):
        super().__init__(parent, title="About Media Tug", size=(500, 660))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        text_ctrl = wx.TextCtrl(
            panel, value=self.ABOUT_TEXT,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_BESTWRAP
        )
        sizer.Add(text_ctrl, 1, wx.EXPAND | wx.ALL, 10)

        sites_btn = wx.Button(panel, label="View Currently Supported Media Websites")
        sites_btn.SetToolTip("Opens the full, live list of sites this app can play and download from, in your web browser.")
        sites_btn.Bind(wx.EVT_BUTTON, self.on_open_supported_sites)
        sizer.Add(sites_btn, 0, wx.ALIGN_CENTER | wx.ALL, 5)

        github_btn = wx.Button(panel, label="GitHub Repository")
        github_btn.Bind(wx.EVT_BUTTON, self.on_open_github)
        sizer.Add(github_btn, 0, wx.ALIGN_CENTER | wx.ALL, 5)

        contact_btn = wx.Button(panel, label="Contact Harith")
        contact_btn.SetToolTip(f"Opens your email app addressed to {self.CONTACT_EMAIL}")
        contact_btn.Bind(wx.EVT_BUTTON, self.on_contact_harith)
        sizer.Add(contact_btn, 0, wx.ALIGN_CENTER | wx.ALL, 5)

        support_btn = wx.Button(panel, label="If you like using Media Tug, consider supporting the developer")
        support_btn.Bind(wx.EVT_BUTTON, self.on_support_developer)
        sizer.Add(support_btn, 0, wx.ALIGN_CENTER | wx.ALL, 5)

        close_btn = wx.Button(panel, wx.ID_CLOSE, label="Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE))
        sizer.Add(close_btn, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        panel.SetSizer(sizer)

        # EVT_CHAR_HOOK catches Escape regardless of which control inside
        # the dialog currently has focus, same pattern as the player and
        # results-list Escape handling elsewhere in the app.
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key_down)

    def on_key_down(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
        else:
            event.Skip()

    def on_open_supported_sites(self, event):
        webbrowser.open(self.SUPPORTED_SITES_URL)

    def on_open_github(self, event):
        webbrowser.open(self.GITHUB_REPO_URL)

    def on_contact_harith(self, event):
        webbrowser.open(f"mailto:{self.CONTACT_EMAIL}")

    def on_support_developer(self, event):
        webbrowser.open(self.KOFI_URL)


class PlaylistDownloadDialog(wx.Dialog):
    """Shown automatically when Enter is pressed on a channel or playlist
    URL in the main search box, instead of running a normal search. This
    replaces the old standalone 'URL downloading' button -- the same
    action is now reached by just pressing Enter on the right kind of
    link, detected from the URL shape."""

    def __init__(self, parent, url, format_label):
        super().__init__(parent, title="Start Download: Playlist or Channel", size=(460, 220))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        info = wx.StaticText(
            panel,
            label=(f"This looks like a playlist or channel link:\n{url}\n\n"
                   f"Download format: {format_label} (change in Settings)\n"
                   "Saved into a 'Playlists' subfolder of your download folder.\n\n"
                   "Start downloading every video in it?")
        )
        info.Wrap(420)
        sizer.Add(info, 1, wx.EXPAND | wx.ALL, 10)

        btn_sizer = wx.StdDialogButtonSizer()
        start_btn = wx.Button(panel, wx.ID_OK, label="Start Download")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label="Cancel")
        btn_sizer.AddButton(start_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        panel.SetSizer(sizer)


class AccessibleDownloaderFrame(wx.Frame):

    ID_HOTKEY_SEARCH = 1001
    ID_HOTKEY_MUTE = 1002
    ID_HOTKEY_PLAYPAUSE = 1003
    ID_HOTKEY_SETTINGS = 1004

    def __init__(self):
        super().__init__(parent=None, title="Media Tug", size=(800, 700))

        is_first_run = not os.path.exists(os.path.join(DATA_DIR, 'media_tug_settings.json'))

        default_download_dir = os.path.join(os.path.expanduser('~'), 'Downloads', 'MediaTug')
        self.app_config = {
            'download_path': default_download_dir,
            'download_format': 'MP3 Audio',
            'video_quality': 'Best available',
            'audio_quality': '192 kbps (High)',
            'skip_seconds': 10,
            'volume_step': 5,
            'debug_logging': False,
            'use_browser_cookies': False,
            'cookie_source': 'Auto - try all installed browsers',
            'cookies_file_path': '',
            'cache_retention': '7 days',
            'auto_paste_clipboard': False,
        }
        self.config_file_path = os.path.join(DATA_DIR, 'media_tug_settings.json')
        self.load_config()
        os.makedirs(self.app_config['download_path'], exist_ok=True)
        self.is_first_run = is_first_run
        self.debug_log_path = os.path.join(LOGS_DIR, 'media_tug_debug.log')
        self.vlc_native_log_path = os.path.join(LOGS_DIR, 'vlc_native.log')

        # Direct googlevideo URLs get rejected by YouTube's server-side
        # anti-bot checks (HTTP 403) even with matching headers, since the
        # rejection is based on signals a raw HTTP request can't replicate.
        # yt-dlp handles that properly on download; the file is saved here
        # temporarily and played locally instead, which has no anti-bot
        # surface at all. Unlike before, this cache is now persistent
        # (not wiped on close) and lives under data/, so recently played
        # tracks are still there for offline re-play; it is pruned by age
        # instead, per the retention setting in Settings.
        self.playback_cache_dir = os.path.join(DATA_DIR, 'playback_cache')
        os.makedirs(self.playback_cache_dir, exist_ok=True)
        self.prune_playback_cache()

        self.search_results_data = {}
        self.current_search_query = ''
        self.search_is_extensible = False
        self.current_result_count = 10
        self.loading_more = False
        self.last_clipboard_url = None

        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # App name / credit heading
        app_name_ctrl = wx.StaticText(panel, label="Media Tug")
        app_name_font = app_name_ctrl.GetFont()
        app_name_font.SetPointSize(app_name_font.GetPointSize() + 4)
        app_name_font.MakeBold()
        app_name_ctrl.SetFont(app_name_font)
        main_sizer.Add(app_name_ctrl, 0, wx.LEFT | wx.TOP, 10)

        credit_ctrl = wx.StaticText(panel, label="By Harith")
        main_sizer.Add(credit_ctrl, 0, wx.LEFT | wx.BOTTOM, 10)

        # Top Menu / Action Bar (Settings)
        top_bar = wx.BoxSizer(wx.HORIZONTAL)
        settings_btn = wx.Button(panel, label="Settings & Preferences")
        settings_btn.SetToolTip("Configure storage folders, quality, and playback key behavior. (Alt+S)")
        settings_btn.Bind(wx.EVT_BUTTON, self.open_settings)
        top_bar.Add(settings_btn, 0, wx.ALL, 5)
        about_btn = wx.Button(panel, label="About")
        about_btn.SetToolTip("License, instructions, and hotkey list.")
        about_btn.Bind(wx.EVT_BUTTON, self.open_about)
        top_bar.Add(about_btn, 0, wx.ALL, 5)
        main_sizer.Add(top_bar, 0, wx.EXPAND | wx.ALL, 5)

        # 1. Search Section
        search_box = wx.StaticBox(panel, label="Search or Paste a URL (Ctrl+F)")
        search_sizer = wx.StaticBoxSizer(search_box, wx.HORIZONTAL)

        self.search_input = wx.TextCtrl(search_box, style=wx.TE_PROCESS_ENTER)
        self.search_input.SetName("Search or paste a URL")
        self.search_input.SetToolTip(
            "Type keywords, or paste a video, channel, or playlist link, and press Enter. "
            "Channel and playlist links open a download confirmation instead of a search."
        )
        self.search_input.Bind(wx.EVT_TEXT_ENTER, self.on_perform_search)
        search_sizer.Add(self.search_input, 1, wx.EXPAND | wx.ALL, 5)

        search_btn = wx.Button(search_box, label="Search")
        search_btn.Bind(wx.EVT_BUTTON, self.on_perform_search)
        search_sizer.Add(search_btn, 0, wx.ALL, 5)

        main_sizer.Add(search_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # 2. Results List View Section
        self.results_box = wx.StaticBox(panel, label="Search Results (Arrow Keys, Enter to Play, Escape to Close)")
        results_sizer = wx.StaticBoxSizer(self.results_box, wx.VERTICAL)

        self.results_list = wx.ListCtrl(self.results_box, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.results_list.InsertColumn(0, "Title", width=450)
        self.results_list.InsertColumn(1, "Duration", width=100)
        self.results_list.InsertColumn(2, "URL / ID", width=180)

        self.results_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_item_activated)
        self.results_list.Bind(wx.EVT_CONTEXT_MENU, self.on_show_context_menu)

        results_sizer.Add(self.results_list, 1, wx.EXPAND | wx.ALL, 5)
        self.results_sizer = results_sizer
        main_sizer.Add(results_sizer, 1, wx.EXPAND | wx.ALL, 10)

        # Hidden render surface for libvlc. Kept off-screen since this is
        # primarily an audio use case; give it real size and add it to a
        # sizer if video display is wanted later. It never takes keyboard
        # focus, so it cannot become a focus trap.
        self.video_panel = wx.Panel(panel, size=(1, 1))
        self.video_panel.SetCanFocus(False)
        self.player = PlayerController(self.log_message, self.video_panel.GetHandle(),
                                        self.debug_message, self.vlc_native_log_path,
                                        self.app_config.get('debug_logging', False))
        self.working_cookie_label = None  # caches whichever browser cookie source last succeeded
        self.player_window = None  # set while a PlayerWindow is open

        # 4. Status and Screen Reader Logging Feed
        #
        # Plain AppendText on a background TextCtrl is NOT announced by
        # NVDA or JAWS unless that control currently has focus, and this
        # control is rarely focused during normal use. The status bar
        # below is the one that actually gets picked up as a live
        # announcement; this log stays as a scrollback for sighted
        # debugging and for anyone tabbing in deliberately to review it.
        status_box = wx.StaticBox(panel, label="Activity Log (scrollback, not auto-announced)")
        status_sizer = wx.StaticBoxSizer(status_box, wx.VERTICAL)

        self.log_ctrl = wx.TextCtrl(status_box, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(-1, 100))
        status_sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 5)
        main_sizer.Add(status_sizer, 0, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(main_sizer)
        self.main_sizer = main_sizer

        # Status bar for spoken announcements. Screen readers pick up
        # SetStatusText changes as a live-region-style announcement even
        # when a totally different control has focus.
        self.CreateStatusBar()
        self.SetStatusText("Ready.")

        # Frame-level accelerators. Ctrl+letter combos pass through wx
        # TextCtrl's default key handling untouched, so these are safe to
        # register app-wide without a low-level keyboard hook.
        accel_entries = [
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('F'), self.ID_HOTKEY_SEARCH),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('M'), self.ID_HOTKEY_MUTE),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('P'), self.ID_HOTKEY_PLAYPAUSE),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('S'), self.ID_HOTKEY_SETTINGS),
        ]
        self.SetAcceleratorTable(wx.AcceleratorTable(accel_entries))
        self.Bind(wx.EVT_MENU, self.focus_search_box, id=self.ID_HOTKEY_SEARCH)
        self.Bind(wx.EVT_MENU, self.on_hotkey_mute, id=self.ID_HOTKEY_MUTE)
        self.Bind(wx.EVT_MENU, self.on_hotkey_playpause, id=self.ID_HOTKEY_PLAYPAUSE)
        self.Bind(wx.EVT_MENU, self.open_settings, id=self.ID_HOTKEY_SETTINGS)

        self.results_list.Bind(wx.EVT_CHAR_HOOK, self.on_results_list_key_down)

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_ACTIVATE, self.on_activate)

        self.Centre()
        self.Show()
        # Explicitly hand focus to a real control right after showing.
        # Without this, some wx/Windows builds briefly land keyboard focus
        # on an internal helper window first, which JAWS/NVDA can announce
        # as raw class names like "wxAuiTabCtrl" -- confusing noise that
        # has nothing to do with anything this app actually uses.
        self.search_input.SetFocus()
        self.log_message("Application initialized. Press Ctrl+F to focus search, Alt+S for settings.")

        if self.is_first_run:
            wx.CallAfter(self.prompt_first_run_shortcut)

    def prompt_first_run_shortcut(self):
        answer = wx.MessageBox(
            "Welcome to Media Tug! Would you like to create a shortcut on your desktop now?",
            "Create Desktop Shortcut", wx.YES_NO | wx.ICON_QUESTION
        )
        if answer == wx.YES:
            success, message = create_desktop_shortcut()
            wx.MessageBox(message, "Shortcut",
                           wx.OK | (wx.ICON_INFORMATION if success else wx.ICON_ERROR))
        # Save now regardless of choice, so this prompt never shows again.
        self.save_config()

    def on_close(self, event):
        self.player.shutdown()
        event.Skip()

    def on_activate(self, event):
        """Fires when the window regains focus, e.g. switching back after
        copying a link elsewhere. Only acts if the setting is on."""
        if event.GetActive() and self.app_config.get('auto_paste_clipboard', False):
            self.try_auto_paste_clipboard()
        event.Skip()

    def get_clipboard_text(self):
        text = None
        if wx.TheClipboard.Open():
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_TEXT)):
                data = wx.TextDataObject()
                wx.TheClipboard.GetData(data)
                text = data.GetText()
            wx.TheClipboard.Close()
        return text

    def try_auto_paste_clipboard(self):
        text = self.get_clipboard_text()
        if not text:
            return
        text = text.strip()
        if not (text.startswith('http://') or text.startswith('https://')):
            return
        if text == self.last_clipboard_url:
            return  # already handled this one, don't re-paste on every activate
        self.last_clipboard_url = text
        self.search_input.SetValue(text)
        self.search_input.SetInsertionPointEnd()
        self.log_message(f"Auto-pasted URL from clipboard: {text}")

    def prune_playback_cache(self):
        """Deletes cached playback files older than the configured
        retention window. Checked once at startup rather than on a live
        timer -- simple and sufficient, since the cache only grows one
        file at a time during normal use."""
        days_map = {'1 day': 1, '2 days': 2, '7 days': 7, '30 days': 30}
        days = days_map.get(self.app_config.get('cache_retention', '7 days'), 7)
        cutoff = time.time() - (days * 86400)
        try:
            for fname in os.listdir(self.playback_cache_dir):
                fpath = os.path.join(self.playback_cache_dir, fname)
                try:
                    if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                except Exception:
                    continue
        except Exception:
            pass

    # -- logging / announcements -------------------------------------------------

    def log_message(self, message):
        """Writes to the scrollback log AND pushes a spoken-friendly summary to the status bar."""
        wx.CallAfter(self.log_ctrl.AppendText, message + "\n")
        wx.CallAfter(self.SetStatusText, message)
        self._write_to_debug_file(message)

    def debug_message(self, message):
        """Verbose diagnostic detail, only surfaced when debug logging is
        enabled in Settings. Goes to the Activity Log and to media_tug_debug.log,
        but not to the status bar, so it does not spam spoken announcements
        during normal use."""
        if not self.app_config.get('debug_logging', False):
            return
        tagged = f"[DEBUG] {message}"
        wx.CallAfter(self.log_ctrl.AppendText, tagged + "\n")
        self._write_to_debug_file(tagged)

    def _write_to_debug_file(self, message):
        if not self.app_config.get('debug_logging', False):
            return
        try:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            with open(self.debug_log_path, 'a', encoding='utf-8') as f:
                f.write(f"{timestamp} {message}\n")
        except Exception:
            pass  # never let logging itself crash the app

    def focus_search_box(self, event):
        self.search_input.SetFocus()

    # -- settings -----------------------------------------------------------

    def load_config(self):
        """Loads saved settings from disk, merging into the defaults above
        so any new setting added later still gets a sane default even if
        the saved file predates it."""
        if not os.path.exists(self.config_file_path):
            return
        try:
            with open(self.config_file_path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            self.app_config.update(saved)
        except Exception:
            pass  # corrupt or unreadable settings file -- fall back to defaults

    def save_config(self):
        try:
            with open(self.config_file_path, 'w', encoding='utf-8') as f:
                json.dump(self.app_config, f, indent=2)
        except Exception as e:
            self.log_message(f"Could not save settings: {str(e)}")

    def open_settings(self, event):
        dlg = SettingsDialog(self, self.app_config)
        if dlg.ShowModal() == wx.ID_OK:
            self.save_config()
            self.log_message(f"Settings updated. Download path: {self.app_config['download_path']}")
        dlg.Destroy()

    def open_about(self, event):
        dlg = AboutDialog(self)
        dlg.ShowModal()
        dlg.Destroy()

    # -- search ---------------------------------------------------------------

    def on_perform_search(self, event):
        query = self.search_input.GetValue().strip()
        if not query:
            wx.MessageBox("Please enter a query or URL.", "Notice", wx.OK | wx.ICON_INFORMATION)
            return

        if query.startswith("http") and self.is_channel_or_playlist_url(query):
            self.show_playlist_download_popup(query)
            return

        self.current_search_query = query
        self.search_is_extensible = not query.startswith("http")
        self.current_result_count = 10

        self.log_message(f"Searching for: {query}...")
        threading.Thread(target=self.fetch_search_results, args=(query,), daemon=True).start()

    # A URL containing any of these is treated as a whole channel or
    # playlist rather than a single video, based on the URL shape alone
    # (no network call needed to decide). Covers standard playlist links,
    # "watch?v=X&list=Y" mixed links, and the common channel URL forms:
    # /channel/UC..., /c/name, /user/name, and the newer /@handle style.
    CHANNEL_PLAYLIST_MARKERS = ['list=', '/playlist', '/channel/', '/c/', '/user/', '/@']

    def is_channel_or_playlist_url(self, url):
        lowered = url.lower()
        return any(marker in lowered for marker in self.CHANNEL_PLAYLIST_MARKERS)

    def show_playlist_download_popup(self, url):
        format_label = self.app_config.get('download_format', 'MP3 Audio')
        dlg = PlaylistDownloadDialog(self, url, format_label)
        if dlg.ShowModal() == wx.ID_OK:
            self.log_message(f"Starting whole channel/playlist download: {url}")
            threading.Thread(target=self.run_channel_download_task, args=(url,), daemon=True).start()
        dlg.Destroy()
        self.search_input.SetFocus()

    def fetch_search_results(self, query):
        try:
            ydl_opts = {'extract_flat': True}
            search_target = query if query.startswith("http") else f"ytsearch10:{query}"

            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_target, download=False)
                entries = info.get('entries', [info]) if 'entries' in info else [info]

            wx.CallAfter(self.populate_list_ui, entries)
        except Exception as e:
            self.log_message(f"Search failed: {str(e)}")

    def populate_list_ui(self, entries):
        self.main_sizer.Show(self.results_sizer)
        self.main_sizer.Layout()
        self.results_list.DeleteAllItems()
        self.search_results_data.clear()

        for idx, entry in enumerate(entries):
            title = entry.get('title', 'Unknown Title')
            duration_sec = entry.get('duration', 0)
            duration_str = f"{duration_sec // 60}:{duration_sec % 60:02d}" if duration_sec else "N/A"
            url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"

            self.results_list.InsertItem(idx, title)
            self.results_list.SetItem(idx, 1, duration_str)
            self.results_list.SetItem(idx, 2, url)

            self.search_results_data[idx] = {'title': title, 'url': url}

        self.log_message(f"Loaded {len(entries)} results. Use arrow keys to explore.")
        if len(entries) > 0:
            self.results_list.SetFocus()
            self.results_list.SetItemState(0, wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
                                            wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED)

    def on_item_activated(self, event):
        """Fires on Enter or double-click. wx.ListCtrl already generates this on Enter,
        so there is no separate EVT_KEY_DOWN handler for Enter -- avoids double-triggering."""
        self.debug_message(f"EVT_LIST_ITEM_ACTIVATED fired, index {event.GetIndex()}")
        self.play_media(event.GetIndex())

    def get_selected_url(self):
        selected_index = self.results_list.GetFirstSelected()
        if selected_index == -1:
            return None
        return self.search_results_data.get(selected_index, {}).get('url')

    # -- playback -------------------------------------------------------------

    # Preferred try-order for cookie "Auto" mode: Firefox first since it
    # does not use Windows DPAPI and is unaffected by the Chrome/Edge
    # App-Bound Encryption cookie decryption issue affecting yt-dlp's
    # Chromium cookie reader on some systems.
    AUTO_COOKIE_BROWSER_ORDER = ['firefox', 'edge', 'chrome', 'brave', 'vivaldi', 'opera']

    # android_vr is the client currently returning 403 on YouTube's CDN as
    # of a live, unresolved upstream bug (yt-dlp issue #17456, opened
    # Aug 18 2026). These clients are tried instead, in order, until one
    # produces a working stream. This list may need updating again later
    # since which client works shifts as YouTube and yt-dlp trade fixes.
    CLIENT_FALLBACK_ORDER = ['tv', 'web_safari', 'web', 'mweb']

    def resolve_cookie_config(self, base_opts_template):
        """Determines a single working cookie configuration once per play
        attempt, separate from client selection. A cookie source only needs
        to load successfully once; it does not need to be re-tested for
        every client fallback attempt below."""
        cookies_file = self.app_config.get('cookies_file_path', '').strip()
        if not cookies_file:
            # Zero-config fallback: a cookies.txt placed in the app's
            # cookies folder is picked up automatically, no Settings
            # dialog or file browse needed.
            default_cookie_path = os.path.join(COOKIES_DIR, 'cookies.txt')
            if os.path.exists(default_cookie_path):
                cookies_file = default_cookie_path
        if cookies_file and os.path.exists(cookies_file):
            return ('cookies file', {'cookiefile': cookies_file})

        if not self.app_config.get('use_browser_cookies', False):
            return ('no cookies', {})

        source = self.app_config.get('cookie_source', 'Auto - try all installed browsers')
        if source.startswith('Auto'):
            browsers = list(self.AUTO_COOKIE_BROWSER_ORDER)
            if self.working_cookie_label and self.working_cookie_label in browsers:
                browsers.remove(self.working_cookie_label)
                browsers.insert(0, self.working_cookie_label)
        else:
            browsers = [source.lower()]

        for browser in browsers:
            opts = dict(base_opts_template)
            opts['cookiesfrombrowser'] = (browser,)
            try:
                with YoutubeDL(opts):
                    pass  # construction alone triggers cookie loading
                wx.CallAfter(self.debug_message, f"Cookie source resolved: {browser}")
                self.working_cookie_label = browser
                return (browser, {'cookiesfrombrowser': (browser,)})
            except Exception as e:
                wx.CallAfter(self.debug_message, f"Cookie source '{browser}' unavailable: {str(e)}")
                continue

        wx.CallAfter(self.debug_message, "No browser cookie source available, proceeding without cookies")
        return ('no cookies', {})

    def play_media(self, index):
        entry = self.search_results_data.get(index)
        if not entry:
            self.debug_message(f"play_media called with index {index}, no matching entry in search_results_data")
            return
        url = entry['url']
        title = entry['title']

        self.debug_message(f"play_media: index {index}, title '{title}', source url {url}")
        self.log_message(f"Downloading for playback: {title}")

        self.player_window = PlayerWindow(self, self.player, title, self.app_config)
        threading.Thread(target=self.resolve_and_play, args=(url, title), daemon=True).start()
        self.player_window.ShowModal()
        self.player_window.Destroy()
        self.player_window = None
        self.results_list.SetFocus()

    def resolve_and_play(self, page_url, title):
        """Direct googlevideo stream URLs get rejected by YouTube's
        server-side anti-bot checks (HTTP 403) regardless of headers, since
        the rejection is based on request signing and fingerprinting that a
        raw HTTP client can't replicate. yt-dlp already handles that
        correctly during its own download, so the audio is fetched to a
        local temp file here and played from disk, which sidesteps the
        anti-bot surface entirely."""
        try:
            outtmpl = os.path.join(self.playback_cache_dir, '%(id)s.%(ext)s')
            base_opts = {
                'format': 'bestaudio/best',
                'quiet': True,
                'logger': YtdlpLoggerBridge(self.debug_message),
                'outtmpl': outtmpl,
                'noplaylist': True,
                'retries': 10,
                'fragment_retries': 10,
                'socket_timeout': 20,
                'continuedl': True,
                # NOTE: 'impersonate' was removed. Passing a plain string
                # like 'chrome' crashes YoutubeDL construction with
                # AssertionError on this yt-dlp version (it expects an
                # ImpersonateTarget object, not a string), which silently
                # broke every single download attempt regardless of cookie
                # source or client -- that was the actual root cause of
                # every prior failure, not YouTube's protections.
            }

            cookie_label, cookie_opts = self.resolve_cookie_config(base_opts)
            wx.CallAfter(self.debug_message, f"Using cookie source: {cookie_label}")

            info = None
            local_path = None
            last_error = None

            # Try yt-dlp's own default client selection first, since that
            # is what the known-working reference script relies on. Only
            # fall back to forcing specific clients if the default fails.
            client_attempts = [None] + self.CLIENT_FALLBACK_ORDER

            for client in client_attempts:
                ydl_opts = dict(base_opts)
                ydl_opts.update(cookie_opts)
                if client:
                    ydl_opts['extractor_args'] = {'youtube': {'player_client': [client]}}

                label = client or 'default'
                wx.CallAfter(self.debug_message, f"Attempting download using player client: {label}")
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(page_url, download=True)
                        local_path = ydl.prepare_filename(info)
                    wx.CallAfter(self.debug_message, f"Succeeded using player client: {label}")
                    break
                except Exception as attempt_error:
                    last_error = attempt_error
                    wx.CallAfter(self.debug_message, f"Player client '{label}' failed: {str(attempt_error)}")
                    continue

            if local_path is None:
                raise last_error if last_error else RuntimeError("No download attempt succeeded.")

            wx.CallAfter(self.debug_message, f"Downloaded to local file: {local_path}")
            wx.CallAfter(self.player.load, local_path)
            if self.player_window:
                self.player_window.update_status(f"Now playing: {title}")
            wx.CallAfter(self.log_message, f"Playing: {title}")
        except Exception as e:
            wx.CallAfter(self.log_message, f"Playback failed: {str(e)}")
            if self.player_window:
                self.player_window.update_status(f"Playback failed: {str(e)}")
            wx.CallAfter(self.debug_message, "Traceback:\n" + traceback.format_exc())

    def on_hotkey_mute(self, event):
        self.player.toggle_mute()

    def on_hotkey_playpause(self, event):
        self.player.toggle_play()

    # -- context menu / downloads ---------------------------------------------

    def on_results_list_key_down(self, event):
        """Escape closes the results panel, same pattern as the player
        window. Arrow-Down while on the last item loads 10 more results
        automatically, so the list keeps extending as you scroll rather
        than hard-capping at the first batch."""
        keycode = event.GetKeyCode()
        if keycode == wx.WXK_ESCAPE:
            self.main_sizer.Hide(self.results_sizer)
            self.main_sizer.Layout()
            self.search_input.SetFocus()
        elif keycode == wx.WXK_DOWN:
            count = self.results_list.GetItemCount()
            if count > 0 and self.results_list.GetFocusedItem() == count - 1:
                self.load_more_results()
            event.Skip()
        else:
            event.Skip()

    def load_more_results(self):
        if self.loading_more or not self.search_is_extensible:
            return
        self.loading_more = True
        self.current_result_count += 10
        threading.Thread(target=self._fetch_more_results, daemon=True).start()

    def _fetch_more_results(self):
        try:
            ydl_opts = {'extract_flat': True}
            search_target = f"ytsearch{self.current_result_count}:{self.current_search_query}"
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_target, download=False)
                entries = info.get('entries', [])
            wx.CallAfter(self.append_more_results, entries)
        except Exception as e:
            wx.CallAfter(self.log_message, f"Load more failed: {str(e)}")
        finally:
            self.loading_more = False

    def append_more_results(self, entries):
        existing_count = self.results_list.GetItemCount()
        new_entries = entries[existing_count:]
        for offset, entry in enumerate(new_entries):
            idx = existing_count + offset
            title = entry.get('title', 'Unknown Title')
            duration_sec = entry.get('duration', 0)
            duration_str = f"{duration_sec // 60}:{duration_sec % 60:02d}" if duration_sec else "N/A"
            url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
            self.results_list.InsertItem(idx, title)
            self.results_list.SetItem(idx, 1, duration_str)
            self.results_list.SetItem(idx, 2, url)
            self.search_results_data[idx] = {'title': title, 'url': url}
        if new_entries:
            self.log_message(f"Loaded {len(new_entries)} more results.")
        else:
            self.log_message("No more results.")

    def on_show_context_menu(self, event):
        selected_index = self.results_list.GetFirstSelected()
        if selected_index == -1:
            return

        menu = wx.Menu()
        play_id = wx.NewIdRef()
        dl_vid_id = wx.NewIdRef()
        dl_aud_id = wx.NewIdRef()
        browser_id = wx.NewIdRef()
        copy_id = wx.NewIdRef()
        email_id = wx.NewIdRef()

        menu.Append(play_id, "Play Video\tEnter")
        menu.Append(dl_vid_id, "Download Video (MP4)")
        menu.Append(dl_aud_id, "Download Audio (MP3)")
        menu.AppendSeparator()
        menu.Append(browser_id, "Open in Web Browser")
        menu.Append(copy_id, "Copy URL to Clipboard")
        menu.Append(email_id, "Send URL by Email")

        self.Bind(wx.EVT_MENU, lambda evt: self.play_media(selected_index), play_id)
        self.Bind(wx.EVT_MENU, lambda evt: self.start_download(selected_index, is_audio=False), dl_vid_id)
        self.Bind(wx.EVT_MENU, lambda evt: self.start_download(selected_index, is_audio=True), dl_aud_id)
        self.Bind(wx.EVT_MENU, lambda evt: webbrowser.open(self.search_results_data[selected_index]['url']), browser_id)
        self.Bind(wx.EVT_MENU, lambda evt: self.copy_url_to_clipboard(selected_index), copy_id)
        self.Bind(wx.EVT_MENU, lambda evt: self.send_url_by_email(selected_index), email_id)

        self.PopupMenu(menu)
        menu.Destroy()

    def copy_url_to_clipboard(self, index):
        url = self.search_results_data[index]['url']
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(url))
            wx.TheClipboard.Close()
            self.log_message(f"Copied URL to clipboard: {url}")

    def send_url_by_email(self, index):
        """Equivalent of Windows Explorer's Send To > Mail Recipient: hands
        the link to whatever mail program the user has set as default,
        pre-filled and ready to send, with no email credentials or SMTP
        config needed here. A mailto: link is what webbrowser.open() and
        Windows both resolve through the same registered default mail
        handler, so this matches Send To > Mail Recipient's behavior
        without shelling out to Explorer directly."""
        entry = self.search_results_data.get(index)
        if not entry:
            return
        url = entry['url']
        title = entry['title']

        subject = urllib.parse.quote(title)
        body = urllib.parse.quote(f"{title}\n{url}")
        mailto_link = f"mailto:?subject={subject}&body={body}"

        try:
            webbrowser.open(mailto_link)
            self.log_message(f"Opened default email app to send: {title}")
        except Exception as e:
            self.log_message(f"Could not open email app: {str(e)}")

    def run_channel_download_task(self, url):
        """Downloads every video in a channel or playlist URL. Mirrors the
        retry/archive pattern from a reference script that is proven to
        handle long batch downloads reliably: a download_archive so a
        second run skips anything already fetched, generous retries since
        a channel-length run has more opportunities to hit a transient
        network error, and ignoreerrors so one broken video in a large
        channel doesn't abort the whole batch."""
        try:
            output_dir = self.app_config['download_path']
            # All channel/playlist downloads live under their own dedicated
            # subfolder, kept separate from single-video downloads that
            # save directly into output_dir.
            playlists_dir = os.path.join(output_dir, 'Playlists')
            os.makedirs(playlists_dir, exist_ok=True)
            archive_path = os.path.join(playlists_dir, 'download_archive.txt')
            is_audio = self.app_config.get('download_format', 'MP3 Audio') == 'MP3 Audio'

            ydl_opts = {
                'outtmpl': os.path.join(playlists_dir, '%(playlist_title|channel)s/%(playlist_index)03d - %(title)s.%(ext)s'),
                'download_archive': archive_path,
                'ignoreerrors': True,
                'retries': 30,
                'fragment_retries': 30,
                'socket_timeout': 60,
                'continuedl': True,
                'progress_hooks': [self.ytdl_hook],
                'logger': YtdlpLoggerBridge(self.debug_message),
            }

            cookie_label, cookie_opts = self.resolve_cookie_config(ydl_opts)
            wx.CallAfter(self.debug_message, f"Channel download using cookie source: {cookie_label}")
            ydl_opts.update(cookie_opts)

            if is_audio:
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': self.get_audio_bitrate(),
                    }],
                })
            else:
                ydl_opts.update({'format': self.get_video_format_string()})

            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            self.log_message(f"Channel/playlist download complete: {playlists_dir}")
        except Exception as e:
            self.log_message(f"Channel download error: {str(e)}")
            self.debug_message("Traceback:\n" + traceback.format_exc())

    def get_audio_bitrate(self):
        audio_quality_str = self.app_config.get('audio_quality', '192 kbps (High)')
        if "128" in audio_quality_str:
            return "128"
        if "320" in audio_quality_str:
            return "320"
        return "192"

    def get_video_format_string(self):
        height_caps = {'1080p': 1080, '720p': 720, '480p': 480, '360p': 360}
        cap = height_caps.get(self.app_config.get('video_quality', 'Best available'))
        if cap:
            return f'bestvideo[height<={cap}]+bestaudio/best[height<={cap}]'
        return 'bestvideo+bestaudio/best'

    def start_download(self, index, is_audio):
        url = self.search_results_data[index]['url']
        output_dir = self.app_config['download_path']
        bitrate = self.get_audio_bitrate()
        video_quality_str = self.app_config.get('video_quality', 'Best available')

        self.log_message(f"Queueing background download for: {url}")
        threading.Thread(target=self.run_download_task,
                          args=(url, is_audio, output_dir, bitrate, video_quality_str), daemon=True).start()

    def run_download_task(self, url, is_audio, output_dir, bitrate, video_quality_str='Best available'):
        try:
            outtmpl = os.path.join(output_dir, '%(title)s.%(ext)s')
            ydl_opts = {
                'outtmpl': outtmpl,
                'progress_hooks': [self.ytdl_hook],
            }

            if is_audio:
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': bitrate,
                    }],
                })
            else:
                ydl_opts.update({'format': self.get_video_format_string()})

            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            self.log_message(f"Success: File saved successfully to {output_dir}")
        except Exception as e:
            self.log_message(f"Download Error: {str(e)}")

    def ytdl_hook(self, d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            eta = d.get('_eta_str', 'unknown')
            self.log_message(f"Progress: {percent} | ETA: {eta}")
        elif d['status'] == 'finished':
            self.log_message("Download block complete. Running file conversions...")


if __name__ == '__main__':
    app = wx.App(False)
    frame = AccessibleDownloaderFrame()
    app.MainLoop()
