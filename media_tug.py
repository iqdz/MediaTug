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
import re
import urllib.parse
import queue
import ctypes

try:
    import winsound
except ImportError:
    winsound = None


def is_screen_reader_active():
    """True if Windows currently reports a screen reader running system-wide
    (JAWS, NVDA, Narrator, etc.), via the official SystemParametersInfo
    SPI_GETSCREENREADER flag -- every major Windows screen reader sets this
    system-wide when it starts. Used to gate spoken announcements that must
    stay silent for sighted users with no screen reader running, while still
    using the same reliable SAPI5 TTS path as every other spoken message in
    this app (rather than MSAA/focus tricks, which proved unreliable with
    JAWS in practice)."""
    try:
        SPI_GETSCREENREADER = 0x0046
        result = ctypes.c_bool()
        ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETSCREENREADER, 0, ctypes.byref(result), 0)
        return bool(ok) and bool(result.value)
    except Exception:
        return False


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
    try:
        os.makedirs(_dir, exist_ok=True)
    except Exception:
        # Never crash at startup over an unwritable data folder (e.g. an
        # install that landed under another account's profile). The app
        # still opens and announces the problem clearly instead.
        pass


def get_sounds_dir():
    r"""Resolves the folder holding this app's bundled WAV sound
    effects. In dev mode they're the 'sounds' folder that ships next to
    this script. In a frozen PyInstaller build the WAVs are collected as
    data files (see media_tug.spec), but exactly where they land next to
    the exe depends on the PyInstaller version: 5.x and older put data
    files directly beside the exe (app\sounds), while 6.x+ puts them in
    the 'contents directory' (app\_internal\sounds) by default -- and
    onefile builds extract to a temp folder exposed as sys._MEIPASS.
    This function checks every one of those locations and returns the
    first that actually exists, so a packaged build plays its sounds no
    matter which PyInstaller layout produced it. Deliberately not based
    on get_base_dir(): that function returns the portable-install *root*
    (parent of 'app'), but the sounds are bundled inside 'app' itself,
    alongside the exe."""
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))

    candidates = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(os.path.join(meipass, 'sounds'))
    candidates.append(os.path.join(exe_dir, 'sounds'))
    candidates.append(os.path.join(exe_dir, '_internal', 'sounds'))

    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    # None found (e.g. sounds failed to bundle): fall back to the
    # conventional exe-adjacent path so play_sound() still behaves the
    # same as before -- it simply stays silent for a missing file.
    return candidates[0]


SOUNDS_DIR = get_sounds_dir()
SOUND_SYSTEM_READY = os.path.join(SOUNDS_DIR, 'system-ready.wav')
SOUND_SEARCH_COMPLETE = os.path.join(SOUNDS_DIR, 'complete.wav')


def play_sound(path):
    """Plays a WAV file asynchronously (non-blocking, never overlaps
    itself -- SND_ASYNC replaces the flag as needed). Uses the
    stdlib's winsound rather than adding VLC or another dependency for
    two short UI cue sounds; silently does nothing if winsound isn't
    available (non-Windows) or the file is missing, so a missing sound
    file never crashes the app."""
    if winsound is None or not path or not os.path.isfile(path):
        return
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass

# Dev-mode (non-frozen) portability: dependencies installed by run.bat land
# in a local 'libs' folder next to this script instead of the system's
# global site-packages, via 'pip install --target'. Adding it to sys.path
# here means the whole folder -- script plus its dependencies -- can be
# copied to another machine with a compatible Python already installed and
# just work, with no re-installation. This has no effect in a PyInstaller
# build, where everything is already bundled into the exe.
#
# yt-dlp itself lives in a separate 'dependencies\yt-dlp' folder rather
# than in 'libs' -- it's fetched/version-checked by update_ytdlp.py the
# same way for both run.bat (dev mode) and build.bat (release builds),
# so both use one shared, persistent copy instead of two separate ones
# that could drift out of sync with each other.
if not getattr(sys, 'frozen', False):
    _base_dir = os.path.dirname(os.path.abspath(__file__))
    _libs_dir = os.path.join(_base_dir, 'libs')
    if os.path.isdir(_libs_dir) and _libs_dir not in sys.path:
        sys.path.insert(0, _libs_dir)
    _ytdlp_dir = os.path.join(_base_dir, 'dependencies', 'yt-dlp')
    if os.path.isdir(_ytdlp_dir) and _ytdlp_dir not in sys.path:
        sys.path.insert(0, _ytdlp_dir)

from yt_dlp import YoutubeDL
from yt_dlp.cookies import load_cookies as _load_browser_cookies


class _PlaybackCancelled(Exception):
    """Raised to abort an in-flight playback download when the user cancels
    (Escape) before it has finished."""
    pass


class _Speaker:
    """Speaks text aloud with Windows SAPI5 (System.Speech). Messages are
    queued and spoken one at a time on a daemon thread, so announcements
    never overlap and never block the UI. SAPI5 ships with .NET Framework
    and is available on Windows 10/11 out of the box."""
    def __init__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def speak(self, text):
        self._queue.put(text)

    def _run(self):
        while True:
            text = self._queue.get()
            if text is None:
                break
            self._sapi_speak(text)

    @staticmethod
    def _sapi_speak(text):
        escaped = text.replace("'", "''")
        command = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Speak('{escaped}')"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                timeout=60,
            )
        except Exception:
            pass


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
    _vlc_dir = os.path.dirname(os.path.abspath(sys.executable))
else:
    # Dev mode: prefer a local 'vlc' folder next to this script if one
    # exists, otherwise fall back directly to dependencies\vlc\ -- the
    # single shared folder run.bat/build.bat both fetch into (see
    # sync_vlc.py) -- so a plain dev checkout works even without a
    # separate copy at the root.
    _local_vlc_dir = os.path.join(BASE_DIR, 'vlc')
    _deps_vlc_dir = os.path.join(BASE_DIR, 'dependencies', 'vlc')
    if os.path.isfile(os.path.join(_local_vlc_dir, 'libvlc.dll')):
        _vlc_dir = _local_vlc_dir
    else:
        _vlc_dir = _deps_vlc_dir
if os.path.isfile(os.path.join(_vlc_dir, 'libvlc.dll')):
    os.environ['PATH'] = _vlc_dir + os.pathsep + os.environ.get('PATH', '')
    os.environ.setdefault('PYTHON_VLC_LIB_PATH', os.path.join(_vlc_dir, 'libvlc.dll'))
    os.environ.setdefault('VLC_PLUGIN_PATH', os.path.join(_vlc_dir, 'plugins'))
# If _vlc_dir has no libvlc.dll (e.g. run.bat's fetch failed and no local
# VLC install exists to fall back to), the env vars above are simply left
# unset and python-vlc falls back to its own default search (PATH,
# registry) -- same behavior as before this dev-mode bundling existed.
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
        if self.media_player.get_state() == vlc.State.Ended:
            self.replay()
            return
        if self.media_player.is_playing():
            self.media_player.pause()
            self.log("Player: Paused")
        else:
            self.media_player.play()
            self.log("Player: Playing")

    def replay(self):
        if not self.current_url:
            self.log("Player: nothing loaded to replay.")
            return
        media = self.instance.media_new(self.current_url)
        self.media_player.set_media(media)
        self.media_player.play()
        self.log("Player: Replaying from the beginning")

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
        had_media = self.current_url is not None
        self.media_player.stop()
        self.current_url = None
        if had_media:
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


def get_default_download_dir():
    """Returns a sane default download folder for this machine: the
    user's real Downloads folder if it exists, else Documents, else the
    profile root. All three live in the current user's own profile, so
    the app can always create whichever one it falls back to."""
    candidates = (
        os.path.expanduser('~/Downloads'),
        os.path.expanduser('~/Documents'),
        os.path.expanduser('~'),
    )
    for candidate in candidates:
        try:
            if os.path.isdir(candidate):
                return candidate
        except Exception:
            continue
    return os.path.expanduser('~')


def _probe_download_dir(path):
    """Creates `path` if needed and verifies the current user can write
    a file into it. Returns (True, "") on success or (False, reason).
    Used everywhere a download folder is loaded or chosen, so an
    unusable folder is caught up front with one clear message instead
    of failing inside yt-dlp after the download has already started."""
    if not isinstance(path, str) or not path.strip():
        return False, "the folder path is empty"
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, '.media_tug_write_test')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('')
        try:
            os.remove(probe)
        except Exception:
            pass  # a leftover probe file is harmless; write access is proven
        return True, ""
    except Exception as e:
        return False, str(e)


def _is_dir_writable(path):
    """True only if `path` already exists as a directory and the current
    user can create and delete a file inside it (no creation attempt --
    for the app's own data/log folders, which should already exist)."""
    try:
        if not isinstance(path, str) or not os.path.isdir(path):
            return False
        probe = os.path.join(path, '.media_tug_write_test')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('')
        try:
            os.remove(probe)
        except Exception:
            pass
        return True
    except Exception:
        return False


class SettingsDialog(wx.Dialog):
    """Accessible Settings Dialog for managing download preferences, quality targets, and hotkey behavior.

    Layout notes: the settings list is taller than most screens, so it now
    lives in a scrollable content area with Save/Cancel pinned in a fixed
    footer below it -- those two buttons stay reachable and visible no
    matter how small the window is, instead of being pushed off the
    bottom of a fixed 900px-tall dialog. Everything below is still built
    from the same native StaticBox/CheckBox/Choice controls, so tab order
    and screen-reader labels are unchanged from before -- the scrolling
    and spacing changes are visual/layout only.
    """

    OUTER_PAD = 12
    FIELD_PAD = 6
    SECTION_GAP = 12

    def __init__(self, parent, config):
        super().__init__(
            parent, title="Application Settings",
            size=(560, 700),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.SetMinSize((460, 380))
        self.config = config

        outer_sizer = wx.BoxSizer(wx.VERTICAL)

        # Scrollable content area holding every settings group.
        scroll = wx.ScrolledWindow(self, style=wx.VSCROLL)
        scroll.SetScrollRate(0, 20)
        panel = scroll
        sizer = wx.BoxSizer(wx.VERTICAL)

        section_font = panel.GetFont()
        section_font.SetWeight(wx.FONTWEIGHT_BOLD)

        def add_section(box_sizer):
            sizer.Add(box_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.OUTER_PAD)

        # Search Behavior
        search_behavior_box = wx.StaticBox(panel, label="Search Behavior")
        search_behavior_box.SetFont(section_font)
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
        search_behavior_sizer.Add(self.auto_paste_check, 0, wx.ALL, self.FIELD_PAD)
        add_section(search_behavior_sizer)

        # Download Folder Choice
        folder_box = wx.StaticBox(panel, label="Default Download Directory")
        folder_box.SetFont(section_font)
        folder_sizer = wx.StaticBoxSizer(folder_box, wx.HORIZONTAL)
        self.path_ctrl = wx.TextCtrl(folder_box, value=self.config.get('download_path', os.getcwd()))
        folder_sizer.Add(self.path_ctrl, 1, wx.EXPAND | wx.ALL, self.FIELD_PAD)
        browse_btn = wx.Button(folder_box, label="Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, self.on_browse)
        folder_sizer.Add(browse_btn, 0, wx.ALL, self.FIELD_PAD)
        add_section(folder_sizer)

        # Download Format: Video or Audio (mutually exclusive)
        format_box = wx.StaticBox(panel, label="Download Format")
        format_box.SetFont(section_font)
        format_sizer = wx.StaticBoxSizer(format_box, wx.VERTICAL)
        self.format_radio = wx.RadioBox(
            format_box, label="Download as", choices=["MP3 Audio", "MP4 Video"],
            style=wx.RA_SPECIFY_ROWS
        )
        self.format_radio.SetStringSelection(self.config.get('download_format', 'MP3 Audio'))
        self.format_radio.Bind(wx.EVT_RADIOBOX, self.on_format_change)
        format_sizer.Add(self.format_radio, 0, wx.EXPAND | wx.ALL, self.FIELD_PAD)

        video_quality_label = wx.StaticText(format_box, label="Video quality (used when MP4 Video is selected):")
        format_sizer.Add(video_quality_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, self.FIELD_PAD)
        self.video_quality_choice = wx.Choice(
            format_box,
            choices=["Best available", "1080p", "720p", "480p", "360p"]
        )
        self.video_quality_choice.SetName("Video quality")
        self.video_quality_choice.SetStringSelection(self.config.get('video_quality', 'Best available'))
        format_sizer.Add(self.video_quality_choice, 0, wx.EXPAND | wx.ALL, self.FIELD_PAD)
        add_section(format_sizer)

        # Audio Quality Selection
        audio_box = wx.StaticBox(panel, label="MP3 Audio Quality Preference")
        audio_box.SetFont(section_font)
        audio_sizer = wx.StaticBoxSizer(audio_box, wx.VERTICAL)
        self.audio_choice = wx.Choice(audio_box, choices=["128 kbps (Standard)", "192 kbps (High)", "320 kbps (Maximum)"])
        self.audio_choice.SetStringSelection(self.config.get('audio_quality', '192 kbps (High)'))
        audio_sizer.Add(self.audio_choice, 0, wx.EXPAND | wx.ALL, self.FIELD_PAD)
        add_section(audio_sizer)

        self.on_format_change(None)  # sync initial enabled/disabled state

        # Playback Hotkey Behavior
        hotkey_box = wx.StaticBox(panel, label="Playback Control Settings")
        hotkey_box.SetFont(section_font)
        hotkey_sizer = wx.StaticBoxSizer(hotkey_box, wx.VERTICAL)

        skip_row = wx.BoxSizer(wx.HORIZONTAL)
        skip_label = wx.StaticText(hotkey_box, label="Skip length in seconds (Left/Right arrow in the player window):")
        skip_label.Wrap(360)
        skip_row.Add(skip_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FIELD_PAD)
        self.skip_spin = wx.SpinCtrl(hotkey_box, min=1, max=120, initial=self.config.get('skip_seconds', 10), size=(70, -1))
        self.skip_spin.SetName("Skip length in seconds")
        skip_row.Add(self.skip_spin, 0, wx.ALIGN_CENTER_VERTICAL)
        hotkey_sizer.Add(skip_row, 0, wx.EXPAND | wx.ALL, self.FIELD_PAD)

        vol_row = wx.BoxSizer(wx.HORIZONTAL)
        vol_label = wx.StaticText(hotkey_box, label="Volume step per key press (Up/Down arrow in the player window):")
        vol_label.Wrap(360)
        vol_row.Add(vol_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FIELD_PAD)
        self.vol_spin = wx.SpinCtrl(hotkey_box, min=1, max=50, initial=self.config.get('volume_step', 5), size=(70, -1))
        self.vol_spin.SetName("Volume step percent")
        vol_row.Add(self.vol_spin, 0, wx.ALIGN_CENTER_VERTICAL)
        hotkey_sizer.Add(vol_row, 0, wx.EXPAND | wx.ALL, self.FIELD_PAD)

        note = wx.StaticText(
            hotkey_box,
            label=("Note: opening a track shows a dedicated player window. Space or Control+P "
                   "play/pause, Control+M mutes, arrows seek and adjust volume, Escape or Control+W "
                   "closes it. Control+Shift+V shows the video for the currently playing audio in "
                   "a separate window, for sharing with a sighted person. These only apply while "
                   "that window is open.")
        )
        note_font = note.GetFont()
        note_font.MakeItalic()
        note.SetFont(note_font)
        note.SetForegroundColour(wx.Colour(90, 90, 90))
        note.Wrap(480)
        hotkey_sizer.Add(note, 0, wx.EXPAND | wx.ALL, self.FIELD_PAD)

        add_section(hotkey_sizer)

        # Debug Logging
        debug_box = wx.StaticBox(panel, label="Diagnostics")
        debug_box.SetFont(section_font)
        debug_sizer = wx.StaticBoxSizer(debug_box, wx.VERTICAL)
        self.debug_check = wx.CheckBox(debug_box, label="Enable debug logging")
        self.debug_check.SetValue(self.config.get('debug_logging', False))
        self.debug_check.SetToolTip(
            "Logs detailed player state, stream resolution, and error detail "
            "to the Activity Log and to media_tug_debug.log in the logs folder. "
            "Also enables VLC's own vlc_native.log for deep playback errors. "
            "Changing this takes effect after restarting the app."
        )
        debug_sizer.Add(self.debug_check, 0, wx.ALL, self.FIELD_PAD)

        self.cookies_check = wx.CheckBox(
            debug_box,
            label="Use browser cookies for YouTube requests (helps with 403 errors)"
        )
        self.cookies_check.SetValue(self.config.get('use_browser_cookies', False))
        self.cookies_check.Bind(wx.EVT_CHECKBOX, self.on_cookies_toggle)
        debug_sizer.Add(self.cookies_check, 0, wx.ALL, self.FIELD_PAD)

        # The cookie source and cookies-file controls only matter once the
        # checkbox above is on, so they're enabled/disabled together with
        # it (same "greyed out until relevant" pattern already used for
        # the video/audio quality choices above) and given extra left
        # padding so they visually read as belonging to that checkbox.
        # These are added directly to debug_box, the same StaticBox every
        # other control in this section already uses as its parent --
        # deliberately not wrapped in an extra child Panel.
        indent = self.FIELD_PAD + 14

        source_label = wx.StaticText(debug_box, label="Browser cookie source:")
        debug_sizer.Add(source_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, indent)
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
        debug_sizer.Add(self.cookie_source_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, indent)

        cookies_file_label = wx.StaticText(
            debug_box,
            label=("Cookies file path (optional). A cookies.txt dropped in the app's "
                   "'cookies' folder is used automatically without setting anything here. "
                   "Export with a browser extension like 'Get cookies.txt LOCALLY':")
        )
        cookies_file_label.Wrap(420)
        debug_sizer.Add(cookies_file_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, indent)

        cookies_file_row = wx.BoxSizer(wx.HORIZONTAL)
        self.cookies_file_ctrl = wx.TextCtrl(debug_box, value=self.config.get('cookies_file_path', ''))
        self.cookies_file_ctrl.SetName("Cookies file path")
        cookies_file_row.Add(self.cookies_file_ctrl, 1, wx.EXPAND | wx.RIGHT, self.FIELD_PAD)
        self.cookies_browse_btn = wx.Button(debug_box, label="Browse...")
        self.cookies_browse_btn.Bind(wx.EVT_BUTTON, self.on_browse_cookies_file)
        cookies_file_row.Add(self.cookies_browse_btn, 0)
        debug_sizer.Add(cookies_file_row, 0, wx.EXPAND | wx.ALL, indent)

        add_section(debug_sizer)
        self.on_cookies_toggle(None)  # sync initial enabled/disabled state

        # Playback cache retention
        retention_box = wx.StaticBox(panel, label="Delete Played Video/Audio After")
        retention_box.SetFont(section_font)
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
        retention_sizer.Add(self.retention_choice, 0, wx.EXPAND | wx.ALL, self.FIELD_PAD)

        clear_cache_btn = wx.Button(retention_box, label="Clear App Cache Now (Shift+Alt+C)")
        clear_cache_btn.SetToolTip("Deletes all cached playback files now.")
        clear_cache_btn.Bind(wx.EVT_BUTTON, self.on_clear_cache_click)
        retention_sizer.Add(clear_cache_btn, 0, wx.ALL, self.FIELD_PAD)
        add_section(retention_sizer)

        # Desktop shortcut
        shortcut_box = wx.StaticBox(panel, label="Desktop Shortcut")
        shortcut_box.SetFont(section_font)
        shortcut_sizer = wx.StaticBoxSizer(shortcut_box, wx.VERTICAL)
        shortcut_btn = wx.Button(shortcut_box, label="Create a shortcut on the desktop")
        shortcut_btn.Bind(wx.EVT_BUTTON, self.on_create_shortcut)
        shortcut_sizer.Add(shortcut_btn, 0, wx.ALL, self.FIELD_PAD)
        add_section(shortcut_sizer)

        sizer.AddSpacer(self.OUTER_PAD)
        panel.SetSizer(sizer)
        panel.FitInside()

        outer_sizer.Add(scroll, 1, wx.EXPAND)

        # Fixed footer: Save/Cancel stay on screen and reachable regardless
        # of scroll position or window height.
        outer_sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.OUTER_PAD)

        footer = wx.Panel(self)
        footer_sizer = wx.BoxSizer(wx.HORIZONTAL)
        footer_sizer.AddStretchSpacer(1)
        btn_sizer = wx.StdDialogButtonSizer()
        save_btn = wx.Button(footer, wx.ID_SAVE)
        cancel_btn = wx.Button(footer, wx.ID_CANCEL)
        save_btn.Bind(wx.EVT_BUTTON, self.on_save)
        save_btn.SetDefault()
        btn_sizer.AddButton(save_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        footer_sizer.Add(btn_sizer, 0)
        footer.SetSizer(footer_sizer)
        outer_sizer.Add(footer, 0, wx.EXPAND | wx.ALL, self.OUTER_PAD)

        self.SetSizer(outer_sizer)

        # Clamp to the real screen's work area (excludes the taskbar).
        # Without this, a display shorter than the dialog's constructor
        # size (560, 700) -- common on smaller laptop screens, or when
        # the parent window sits near a screen edge -- can leave the
        # fixed footer's Save/Cancel buttons positioned below the
        # visible display. A keyboard/screen-reader user can still Tab
        # to and activate them even off-screen, since the controls exist
        # regardless of where they're drawn, but a sighted mouse user
        # has no way to see or click something rendered outside the
        # actual screen. Falls back to the constructor's size/position
        # untouched if display info isn't available for any reason.
        try:
            display_index = wx.Display.GetFromWindow(parent) if parent else wx.NOT_FOUND
            if display_index == wx.NOT_FOUND:
                display_index = 0
            work_area = wx.Display(display_index).GetClientArea()
            margin = 40
            min_w, min_h = self.GetMinSize()
            max_w = max(min_w, work_area.width - margin)
            max_h = max(min_h, work_area.height - margin)
            cur_w, cur_h = self.GetSize()
            new_size = (min(cur_w, max_w), min(cur_h, max_h))
            if new_size != (cur_w, cur_h):
                self.SetSize(new_size)
        except Exception:
            pass

        self.CentreOnParent()
        # CentreOnParent can still leave part of the dialog off-screen if
        # the parent itself sits near a screen edge -- nudge it fully
        # back onto the display as a final safety net.
        try:
            self._keep_fully_on_screen()
        except Exception:
            pass

        # Shift+Alt+C clears the playback cache from inside Settings too.
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key_down)

    def _keep_fully_on_screen(self):
        """Repositions the dialog so its entire rectangle -- footer and
        Save/Cancel buttons included -- sits inside the current display's
        visible work area, in case CentreOnParent left part of it off-screen."""
        display_index = wx.Display.GetFromWindow(self)
        if display_index == wx.NOT_FOUND:
            return
        work_area = wx.Display(display_index).GetClientArea()
        x, y = self.GetPosition()
        w, h = self.GetSize()
        new_x = min(max(x, work_area.x), work_area.x + work_area.width - w)
        new_y = min(max(y, work_area.y), work_area.y + work_area.height - h)
        if (new_x, new_y) != (x, y):
            self.SetPosition((new_x, new_y))

    def on_format_change(self, event):
        is_video = self.format_radio.GetStringSelection() == 'MP4 Video'
        self.video_quality_choice.Enable(is_video)
        self.audio_choice.Enable(not is_video)

    def on_cookies_toggle(self, event):
        use_cookies = self.cookies_check.GetValue()
        self.cookie_source_choice.Enable(use_cookies)
        self.cookies_file_ctrl.Enable(use_cookies)
        self.cookies_browse_btn.Enable(use_cookies)

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

    def on_clear_cache_click(self, event):
        parent = self.GetParent()
        if hasattr(parent, 'clear_app_cache'):
            success, msg = parent.clear_app_cache()
            wx.MessageBox(msg, "Cache Status", wx.OK | (wx.ICON_INFORMATION if success else wx.ICON_ERROR))

    def on_key_down(self, event):
        if event.GetKeyCode() == ord('C') and event.AltDown() and event.ShiftDown():
            self.on_clear_cache_click(event)
        else:
            event.Skip()

    def on_save(self, event):
        # Validate the chosen download folder right here, so a folder that
        # cannot be created or written to is caught immediately with a
        # clear message instead of surfacing as a download failure later.
        new_download_path = self.path_ctrl.GetValue().strip()
        ok, _ = _probe_download_dir(new_download_path)
        if not ok:
            wx.MessageBox(
                "That folder cannot be used for downloads: it does not exist "
                "and cannot be created, or you do not have permission to write "
                "to it. Please choose another folder.",
                "Download Folder Unavailable", wx.OK | wx.ICON_ERROR
            )
            return
        self.config['auto_paste_clipboard'] = self.auto_paste_check.GetValue()
        self.config['download_path'] = new_download_path
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


class _VisualHintPanel(wx.Panel):
    """Draws a one-line hint with plain GDI text in its paint handler.
    Because the string is painted directly on the panel rather than held
    by a real control, it never enters the MSAA/UIA accessibility tree,
    so NVDA and JAWS cannot read it. The line exists purely for the eyes
    of a sighted person sharing the screen."""
    def __init__(self, parent, text):
        super().__init__(parent, size=(-1, 26))
        self._text = text
        self.SetCanFocus(False)
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def _on_paint(self, event):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        if not self._text:
            return
        font = self.GetFont()
        font.SetPointSize(font.GetPointSize() - 1)
        font.MakeItalic()
        dc.SetFont(font)
        dc.SetTextForeground(wx.Colour(85, 85, 85))
        dc.DrawText(self._text, 12, 6)


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

    def __init__(self, parent, player, title, app_config, page_url=None, frame=None, is_audio=True):
        super().__init__(
            parent,
            title=f"Now Playing: {title}",
            style=wx.DEFAULT_DIALOG_STYLE
        )
        self.player = player
        self.app_config = app_config
        self.page_url = page_url
        self.frame = frame
        self.is_audio = is_audio

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.status_ctrl = wx.TextCtrl(panel, value=f"Loading: {title}", style=wx.TE_READONLY)
        self.status_ctrl.SetName("Player status")
        sizer.Add(self.status_ctrl, 1, wx.EXPAND | wx.ALL, 10)

        # Visual-only hint, drawn with plain GDI text so it never enters
        # the accessibility tree: screen readers cannot read it. It exists
        # so a blind user playing audio-only can show the video to a
        # sighted person, who reads this line with their eyes.
        self.hint_panel = _VisualHintPanel(
            panel, "press CTRL+Shift+V to toggle the video watching."
        )
        sizer.Add(self.hint_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        if not is_audio:
            self.hint_panel.Hide()
        panel.SetSizer(sizer)

        # EVT_CHAR_HOOK fires before normal key dispatch to child controls,
        # so these shortcuts work no matter which control inside the
        # dialog currently has focus, without needing a separate
        # AcceleratorTable entry per key.
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key_down)
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.SetSize((420, 165) if is_audio else (420, 130))
        self.Centre()

    def on_key_down(self, event):
        keycode = event.GetKeyCode()
        ctrl = event.ControlDown()

        if keycode == wx.WXK_ESCAPE:
            self.Close()
        elif ctrl and keycode == ord('W'):
            self.Close()
        elif ctrl and keycode == ord('D'):
            self._download_current()
        elif ctrl and keycode == ord('L'):
            self._copy_current_url()
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
        elif ctrl and event.ShiftDown() and keycode == ord('V'):
            self.toggle_video()
        else:
            event.Skip()

    def _download_current(self):
        if self.frame and self.page_url:
            self.frame.download_url_in_background(self.page_url)

    def _copy_current_url(self):
        if self.frame and self.page_url:
            self.frame.copy_text_to_clipboard(self.page_url)

    def update_status(self, text):
        wx.CallAfter(self.status_ctrl.SetValue, text)

    def toggle_video(self):
        """Ctrl+Shift+V while an audio-only track is playing: fetch and
        show the video for the same source in a separate window, so a
        blind user can share the picture with a sighted person. Pressing
        it again (or Escape in the video window) closes it and resumes
        the paused audio."""
        if not self.is_audio:
            self.update_status("This track is already playing with video.")
            return
        if not self.frame or not self.page_url:
            self.update_status("No source URL available to show video.")
            return
        self.frame.toggle_now_playing_video(self.page_url, self)

    def on_close(self, event):
        if self.frame:
            self.frame.close_video_window()
        self.player.stop()
        self.EndModal(wx.ID_CLOSE)


class VideoWindow(wx.Dialog):
    """Visible video window opened by Ctrl+Shift+V while audio-only
    playback is running. Uses its own PlayerController bound to a real
    video surface, independent of the audio player. Supports the same
    transport hotkeys as the audio player window (Space/Control+P,
    arrows, Control+M) and closes with Escape, Ctrl+W, or Ctrl+Shift+V;
    closing resumes the audio playback that was paused while the video
    was on screen."""
    def __init__(self, parent, local_path, title, app_config, frame):
        super().__init__(parent, title=f"Video: {title}",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.frame = frame
        self.local_path = local_path
        self.app_config = app_config

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.video_surface = wx.Panel(panel, size=(640, 360))
        self.video_surface.SetBackgroundColour(wx.BLACK)
        sizer.Add(self.video_surface, 1, wx.EXPAND | wx.ALL, 5)

        self.status_ctrl = wx.TextCtrl(panel, value=f"Playing video: {title}",
                                       style=wx.TE_READONLY)
        self.status_ctrl.SetName("Video status")
        sizer.Add(self.status_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)

        panel.SetSizer(sizer)

        # Transport feedback is sent to the main app's status/log and to
        # this window's own status line, so a screen reader user hears
        # play/pause/volume changes while the video is on screen.
        self.player = PlayerController(
            self._log_bridge,
            self.video_surface.GetHandle(),
            frame.debug_message,
            getattr(frame, 'vlc_native_log_path', None),
            frame.app_config.get('debug_logging', False)
        )
        self.player.load(local_path)

        self.Bind(wx.EVT_CHAR_HOOK, self.on_key_down)
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.SetSize((680, 460))
        self.Centre()

    def on_key_down(self, event):
        keycode = event.GetKeyCode()
        ctrl = event.ControlDown()
        if keycode == wx.WXK_ESCAPE:
            self.Close()
        elif ctrl and keycode == ord('W'):
            self.Close()
        elif ctrl and event.ShiftDown() and keycode == ord('V'):
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

    def _log_bridge(self, message):
        """Routes player log messages to the main app and to this video
        window's own status line."""
        self.frame.log_message(message)
        wx.CallAfter(self.status_ctrl.SetValue, message)

    def on_close(self, event):
        self.player.stop()
        self.player.shutdown()
        if self.frame:
            self.frame.video_window_closed()
        self.Destroy()


# ---------------------------------------------------------------------------
# App version
# ---------------------------------------------------------------------------
def _read_app_version():
    """Reads the current app version from VERSION.txt. In dev mode the
    file sits next to this script; in a frozen build, build.bat copies it
    beside the exe (and into _internal for PyInstaller data layouts), so
    the About page can state the same version the build was made with.
    Falls back to "unknown" if the file cannot be found -- the About page
    must never crash over a missing version file."""
    candidates = []
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, 'VERSION.txt'))
        candidates.append(os.path.join(exe_dir, '_internal', 'VERSION.txt'))
    candidates.append(os.path.join(BASE_DIR, 'VERSION.txt'))
    for candidate in candidates:
        try:
            with open(candidate, 'r', encoding='utf-8') as f:
                text = f.read().strip()
            if text:
                return text
        except Exception:
            continue
    return "unknown"


MEDIA_TUG_VERSION = _read_app_version()


class AboutDialog(wx.Dialog):
    ABOUT_TEXT = f"""Media Tug
By Harith Alhamdani
Version: {MEDIA_TUG_VERSION}

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
Control+F: focus the search box.
Control+D: download the selected item(s), or the currently playing track.
Control+L: copy the selected link to the clipboard.
Control+Shift+E: share the selected link by email.
Control+Shift+O: open the selected link in your web browser.
Control+Shift+V: play the selected item with video (slower to start).
Alt+S: open Settings.
Alt+H: open Help.
Shift+Alt+C: clear the playback cache.
Enter on a result: play it.
Escape on the results list: close the results panel.
In the player window: Space or Control+P play or pause, Control+M mute,
Up and Down arrows adjust volume, Left and Right arrows seek, Escape
or Control+W close the player. Control+D downloads the currently
playing track; Control+L copies its link. Control+Shift+V shows the
video for the currently playing audio in a separate window, for sharing
with a sighted person; the usual player controls work there too, and it
closes with Escape, Control+W, or the same hotkey again.
Escape on this About page: close it.

Bundled components and their licenses:
yt-dlp: public domain (Unlicense).
VLC / libVLC: GNU Lesser General Public License version 2.1 or later.
FFmpeg (ffmpeg.exe and ffprobe.exe): the bundled Gyan.dev build is GPL-3.0.
aria2 (aria2c.exe): GNU General Public License version 2 or later.
wxPython: wxWindows Library Licence (LGPL-based with a distribution exception).
Python: Python Software Foundation License.
PyInstaller: GNU General Public License version 2 or later, with the
PyInstaller bootloader exception.
Full license texts are available on each project's website. The Media Tug
application itself is GPL-2.0, as stated above.
"""

    SUPPORTED_SITES_URL = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
    GITHUB_REPO_URL = "https://github.com/iqdz/MediaTug"
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

        contact_btn = wx.Button(panel, label="Contact Harith Alhamdani")
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


class HelpDialog(wx.Dialog):
    HELP_TEXT = """Media Tug Help

Hotkeys and what they do:
Control+F: focus the search box.
Control+D: download the selected item(s) from the results list, or the
currently playing track from inside the player window.
Control+L: copy the selected link (or the currently playing link) to the
clipboard.
Control+Shift+E: share the selected link by email.
Control+Shift+O: open the selected link in your web browser.
Control+Shift+V: play the selected item with video (slower to start).
Alt+S: open Settings.
Alt+H: open this Help page.
Shift+Alt+C: clear the playback cache.

In the results list:
Enter plays the selected item. Arrow keys move through the list.
Arrow-Down on the last item loads ten more results.
Escape closes the results panel.

In the player window:
Space or Control+P: play or pause.
Control+M: mute or unmute.
Up and Down arrows: change volume.
Left and Right arrows: skip back or forward.
Escape or Control+W: close the player window.
Control+D: download the currently playing track in the background.
Control+L: copy the currently playing track's link.
Control+Shift+V: show the video for the currently playing audio in a
separate window, for a sighted person to watch. The usual player
controls work there too (Space/Control+P, arrows, Control+M);
Escape, Control+W, or the same hotkey closes it.

Escape closes this Help page.
"""

    def __init__(self, parent):
        super().__init__(parent, title="Help", size=(520, 640))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        text_ctrl = wx.TextCtrl(
            panel, value=self.HELP_TEXT,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_BESTWRAP
        )
        sizer.Add(text_ctrl, 1, wx.EXPAND | wx.ALL, 10)

        contact_btn = wx.Button(panel, label="Contact Harith")
        contact_btn.SetToolTip(f"Opens your email app addressed to {AboutDialog.CONTACT_EMAIL}")
        contact_btn.Bind(wx.EVT_BUTTON, self.on_contact_harith)
        sizer.Add(contact_btn, 0, wx.ALIGN_CENTER | wx.ALL, 5)

        close_btn = wx.Button(panel, wx.ID_CLOSE, label="Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE))
        sizer.Add(close_btn, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key_down)

    def on_key_down(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
        else:
            event.Skip()

    def on_contact_harith(self, event):
        webbrowser.open(f"mailto:{AboutDialog.CONTACT_EMAIL}")


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
    ID_HOTKEY_CLEAR_CACHE = 1005
    ID_HOTKEY_DOWNLOAD = 1006
    ID_HOTKEY_COPY_URL = 1007
    ID_HOTKEY_EMAIL = 1008
    ID_HOTKEY_OPEN_BROWSER = 1009
    ID_HOTKEY_PLAY_VIDEO = 1010
    ID_HOTKEY_HELP = 1011

    def __init__(self):
        super().__init__(parent=None, title="Media Tug", size=(800, 700))

        self.speaker = _Speaker()

        # Use Downloads folder; fallback to Documents, then home directory
        default_download_dir = get_default_download_dir()

        self.app_config = {
            'download_path': default_download_dir,
            'download_format': 'MP3 Audio',
            'video_quality': 'Best available',
            'audio_quality': '192 kbps (High)',
            'skip_seconds': 10,
            'volume_step': 5,
            'debug_logging': False,
            'use_browser_cookies': False,
            'cookie_source': 'Auto - try all installed browsers',  # Fallback-friendly default
            'cookies_file_path': '',
            'cache_retention': '7 days',
            'auto_paste_clipboard': True,
        }
        self.config_file_path = os.path.join(DATA_DIR, 'media_tug_settings.json')
        self._download_path_reset_message = None
        self.load_config()
        # Make sure the configured download folder actually exists and is
        # writable. If even the corrected default can't be used, fall back
        # to the profile root and let the user pick another folder in
        # Settings -- never crash the whole app over a folder choice.
        if not _probe_download_dir(self.app_config['download_path'])[0]:
            self.app_config['download_path'] = os.path.expanduser('~')
            _probe_download_dir(self.app_config['download_path'])
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
        try:
            os.makedirs(self.playback_cache_dir, exist_ok=True)
        except Exception:
            # Unwritable data folder (broken permissions / wrong profile):
            # leave the cache dir unset rather than crashing the whole app;
            # the startup health check below announces the real problem.
            self.playback_cache_dir = None
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
        help_btn = wx.Button(panel, label="Help")
        help_btn.SetToolTip("Hotkey reference and help. (Alt+H)")
        help_btn.Bind(wx.EVT_BUTTON, self.open_help)
        top_bar.Add(help_btn, 0, wx.ALL, 5)
        main_sizer.Add(top_bar, 0, wx.EXPAND | wx.ALL, 5)

        # 1. Search Section
        search_box = wx.StaticBox(panel, label="Search or Paste a URL (Control+F)")
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

        clear_btn = wx.Button(search_box, label="Clear")
        clear_btn.SetToolTip("Clear search text box")
        clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_search)
        search_sizer.Add(clear_btn, 0, wx.ALL, 5)

        main_sizer.Add(search_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # 2. Results List View Section
        self.results_box = wx.StaticBox(panel, label="Search Results (Arrow Keys, Enter to Play, Escape to Close)")
        results_sizer = wx.StaticBoxSizer(self.results_box, wx.VERTICAL)

        self.results_list = wx.ListCtrl(self.results_box, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
        self.results_list.InsertColumn(0, "Title", width=550)
        self.results_list.InsertColumn(1, "Duration", width=100)

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
        self._video_window = None  # set while a VideoWindow is open
        self._video_fetch_in_progress = False  # guards Ctrl+Shift+V re-entry
        self._playback_cancel_event = None  # threading.Event set to abort an in-flight playback download

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

        # If load_config had to reset an unusable saved download folder,
        # say so once at startup so the reset isn't a silent surprise.
        # Deferred with wx.CallAfter so the announcement happens after the
        # window is fully built.
        if getattr(self, '_download_path_reset_message', None):
            # Delayed past the startup focus announcement (see the
            # wx.CallLater further down) so the SAPI5 voice never speaks
            # over the screen reader's native announcement of the search
            # box, and never sounds like another application while the
            # user may be multitasking.
            wx.CallLater(1500, self.log_and_announce, self._download_path_reset_message)

        # Startup health check for the app's own data/log folders. The
        # installer is per-user and installs into the current user's
        # profile, but if the MSI was ever run with "Run as
        # administrator" (or deployed by an admin tool), the files land
        # under ANOTHER account's profile and this user cannot write
        # data\, cookies\, or logs\ -- which would silently break saved
        # settings, downloads, and caching. Detect and announce it.
        if not _is_dir_writable(DATA_DIR) or not _is_dir_writable(LOGS_DIR):
            # Same deferred timing as the settings-reset message above:
            # never speak over the startup focus announcement.
            wx.CallLater(1500, self.log_and_announce,
                "Warning: Media Tug cannot write to its data folder. It may "
                "have been installed under another user's account. Reinstall "
                "Media Tug from your own user account, without using Run as "
                "administrator.")

        # Frame-level accelerators. Ctrl+letter combos pass through wx
        # TextCtrl's default key handling untouched, so these are safe to
        # register app-wide without a low-level keyboard hook.
        accel_entries = [
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('F'), self.ID_HOTKEY_SEARCH),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('M'), self.ID_HOTKEY_MUTE),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('P'), self.ID_HOTKEY_PLAYPAUSE),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('S'), self.ID_HOTKEY_SETTINGS),
            wx.AcceleratorEntry(wx.ACCEL_ALT | wx.ACCEL_SHIFT, ord('C'), self.ID_HOTKEY_CLEAR_CACHE),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('D'), self.ID_HOTKEY_DOWNLOAD),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('L'), self.ID_HOTKEY_COPY_URL),
            wx.AcceleratorEntry(wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('E'), self.ID_HOTKEY_EMAIL),
            wx.AcceleratorEntry(wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('O'), self.ID_HOTKEY_OPEN_BROWSER),
            wx.AcceleratorEntry(wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('V'), self.ID_HOTKEY_PLAY_VIDEO),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('H'), self.ID_HOTKEY_HELP),
        ]
        self.SetAcceleratorTable(wx.AcceleratorTable(accel_entries))
        self.Bind(wx.EVT_MENU, self.focus_search_box, id=self.ID_HOTKEY_SEARCH)
        self.Bind(wx.EVT_MENU, self.on_hotkey_mute, id=self.ID_HOTKEY_MUTE)
        self.Bind(wx.EVT_MENU, self.on_hotkey_playpause, id=self.ID_HOTKEY_PLAYPAUSE)
        self.Bind(wx.EVT_MENU, self.open_settings, id=self.ID_HOTKEY_SETTINGS)
        self.Bind(wx.EVT_MENU, self.on_hotkey_clear_cache, id=self.ID_HOTKEY_CLEAR_CACHE)
        self.Bind(wx.EVT_MENU, self.on_hotkey_download, id=self.ID_HOTKEY_DOWNLOAD)
        self.Bind(wx.EVT_MENU, self.on_hotkey_copy_url, id=self.ID_HOTKEY_COPY_URL)
        self.Bind(wx.EVT_MENU, self.on_hotkey_email, id=self.ID_HOTKEY_EMAIL)
        self.Bind(wx.EVT_MENU, self.on_hotkey_open_browser, id=self.ID_HOTKEY_OPEN_BROWSER)
        self.Bind(wx.EVT_MENU, self.on_hotkey_play_video, id=self.ID_HOTKEY_PLAY_VIDEO)
        self.Bind(wx.EVT_MENU, self.open_help, id=self.ID_HOTKEY_HELP)

        self.results_list.Bind(wx.EVT_CHAR_HOOK, self.on_results_list_key_down)

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_ACTIVATE, self.on_activate)

        self.Centre()
        self.Show()
        # Audible launch cue, on every startup, independent of any spoken
        # announcement -- plays regardless of whether a screen reader is
        # active.
        play_sound(SOUND_SYSTEM_READY)
        # Strictly native focus announcement: keyboard focus is moved to
        # the search box a beat AFTER the window has fully appeared, so
        # the screen reader's own focus cursor announces it natively as
        # "Search or paste a URL, edit". No SAPI5 voice speaks at startup
        # -- a second voice can be mistaken for another application while
        # the user is multitasking, and any key pressed in response would
        # go to whatever window really has focus, not to this search box.
        wx.CallLater(400, self._focus_search_box_native)
        self.log_message("Application initialized. Press Control+F to focus search, Alt+S for settings.")

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
        """Writes to the scrollback log and the status bar. Use
        log_and_announce for messages that must also be spoken aloud."""
        wx.CallAfter(self.log_ctrl.AppendText, message + "\n")
        wx.CallAfter(self.SetStatusText, message)
        self._write_to_debug_file(message)

    def announce(self, message):
        """Speak a message aloud through Windows' built-in speech voice.
        This is always audible even when NVDA/JAWS do not announce status
        bar text. Queued on a background thread and never blocks the UI."""
        speaker = getattr(self, 'speaker', None)
        if speaker is None:
            return
        try:
            speaker.speak(message)
        except Exception:
            pass

    def screen_reader_alert(self, message):
        """Speaks `message` aloud via the same SAPI5 voice used for every
        other announcement in this app (see announce()), but ONLY when
        Windows reports a screen reader is actually running (see the
        module-level is_screen_reader_active()). A sighted user with no
        screen reader active hears nothing. Two earlier approaches -- a
        raw EVENT_SYSTEM_ALERT MSAA event, then a focus-flash onto a
        hidden control -- were both tried first and neither was picked up
        reliably by JAWS in practice, so this reuses the one mechanism
        already confirmed to work for every other spoken message."""
        if not is_screen_reader_active():
            return
        self.announce(message)

    def _focus_search_box_native(self):
        """Moves keyboard focus to the search box a beat after the window
        appears (see the wx.CallLater in __init__), so the screen reader's
        own focus cursor announces it natively as "Search or paste a URL,
        edit". SetFocusFromKbd() is wxWidgets' keyboard-focus entry point,
        which produces the accessibility focus event NVDA/JAWS track and
        announce. This is the ONLY spoken startup cue: no SAPI5 welcome is
        used, because a second voice while the user may be multitasking can
        be mistaken for another application, and any key pressed in
        response would go to whatever window really has focus."""
        try:
            self.search_input.SetFocusFromKbd()
        except Exception:
            self.search_input.SetFocus()

    def log_and_announce(self, message):
        """Record a message in the log and speak it aloud at the same time."""
        self.log_message(message)
        self.announce(message)

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

    def on_clear_search(self, event):
        """Clears search input text and redirects focus."""
        self.search_input.SetValue("")
        self.search_input.SetFocus()

    def clear_app_cache(self):
        """Removes temporary audio/video playback cache files from disk."""
        count = 0
        size_freed = 0
        try:
            for fname in os.listdir(self.playback_cache_dir):
                fpath = os.path.join(self.playback_cache_dir, fname)
                if os.path.isfile(fpath):
                    size_freed += os.path.getsize(fpath)
                    os.remove(fpath)
                    count += 1
            mb_freed = size_freed / (1024 * 1024)
            msg = f"App cache cleared: {count} file(s) removed ({mb_freed:.1f} MB freed)."
            self.log_message(msg)
            return True, msg
        except Exception as e:
            err_msg = f"Failed to clear cache: {str(e)}"
            self.log_message(err_msg)
            return False, err_msg

    def on_hotkey_clear_cache(self, event):
        """Triggered via Shift+Alt+C."""
        success, msg = self.clear_app_cache()
        wx.MessageBox(msg, "Cache Cleared" if success else "Error",
                      wx.OK | (wx.ICON_INFORMATION if success else wx.ICON_ERROR))

    # -- settings -----------------------------------------------------------

    def load_config(self):
        """Loads saved settings from disk, merging into the defaults above
        so any new setting added later still gets a sane default even if
        the saved file predates it.

        The saved download folder is validated against THIS machine, not
        just trusted: a settings file from an older version, a different
        machine, a manual edit, a disconnected drive, or an install that
        ran under another account can carry a download folder that does
        not exist here or cannot be written to. Any unusable folder is
        reset to this machine's default and the corrected value is saved
        back, so downloads can never repeatedly fail with a
        missing/unwritable-directory error the user has to manually
        repair in Settings."""
        if not os.path.exists(self.config_file_path):
            return
        try:
            with open(self.config_file_path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                raise ValueError("settings file is not a JSON object")
            self.app_config.update(saved)
        except Exception:
            # Corrupt or unreadable settings file: keep the in-memory
            # defaults, and park the broken file out of the way so it
            # stops being loaded (and silently breaking every launch)
            # instead of surviving every reinstall untouched.
            self._download_path_reset_message = (
                "Your saved settings could not be read, so Media Tug started "
                "with default settings."
            )
            try:
                os.replace(self.config_file_path, self.config_file_path + '.unusable')
            except Exception:
                pass
            return

        if 'download_path' in saved and not _probe_download_dir(self.app_config.get('download_path'))[0]:
            self.app_config['download_path'] = get_default_download_dir()
            self._download_path_reset_message = (
                "Your saved download folder is not available on this computer, "
                "so downloads will use your default Downloads folder instead. "
                "You can change it anytime in Settings."
            )
            try:
                with open(self.config_file_path, 'w', encoding='utf-8') as f:
                    json.dump(self.app_config, f, indent=2)
            except Exception:
                pass

    def save_config(self):
        try:
            with open(self.config_file_path, 'w', encoding='utf-8') as f:
                json.dump(self.app_config, f, indent=2)
        except Exception as e:
            self.log_message(f"Could not save settings: {str(e)}")

    def open_settings(self, event):
        # Wrapped defensively: a construction error inside the dialog
        # previously failed silently (the button appeared to do nothing),
        # which is much harder to diagnose than a visible error. Any
        # failure now shows a message box and is written to the debug
        # log/traceback so it's actually reportable.
        try:
            dlg = SettingsDialog(self, self.app_config)
        except Exception as e:
            self.debug_message("Traceback:\n" + traceback.format_exc())
            wx.MessageBox(
                f"Settings could not be opened: {str(e)}",
                "Error", wx.OK | wx.ICON_ERROR
            )
            return
        try:
            if dlg.ShowModal() == wx.ID_OK:
                self.save_config()
                self.log_message(f"Settings updated. Download path: {self.app_config['download_path']}")
        finally:
            dlg.Destroy()

    def open_about(self, event):
        dlg = AboutDialog(self)
        dlg.ShowModal()
        dlg.Destroy()

    def open_help(self, event):
        dlg = HelpDialog(self)
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
            self.log_and_announce(f"Starting whole channel/playlist download: {url}")
            threading.Thread(target=self.run_channel_download_task, args=(url,), daemon=True).start()
        dlg.Destroy()
        self.search_input.SetFocus()

    def _perform_yt_search(self, search_target):
        """Runs one search and returns its entry list. Works for keyword
        searches (ytsearch10:query) and plain video links alike. Anonymous
        YouTube searches run in the logged-out context, which applies
        SafeSearch-style filtering and silently drops mature/adult results
        entirely -- that is YouTube-side behaviour, not a content filter in
        this app. Reusing the same verified browser-cookie resolution as
        playback puts the search in the user's signed-in account context,
        where SafeSearch follows the account's own YouTube setting instead.
        If the cookie-backed attempt fails or is itself filtered to nothing,
        the search is retried once anonymously so a cookie problem can never
        take search down."""
        base_opts = {'extract_flat': True}
        cookie_label, cookie_opts = self.resolve_cookie_config(base_opts)
        wx.CallAfter(self.debug_message, f"Search cookie source: {cookie_label}")

        attempts = [dict(base_opts, **cookie_opts)]
        if cookie_opts:
            attempts.append(dict(base_opts))

        last_error = None
        for attempt_index, ydl_opts in enumerate(attempts):
            if not cookie_opts:
                wx.CallAfter(self.debug_message, "Searching anonymously (no cookies)")
            elif attempt_index == 0:
                wx.CallAfter(self.debug_message, "Searching with cookies")
            else:
                wx.CallAfter(self.debug_message, "Search retry without cookies")
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_target, download=False)
                raw_entries = info.get('entries', [info]) if 'entries' in info else [info]
                entries = raw_entries
            except Exception as attempt_error:
                last_error = attempt_error
                wx.CallAfter(self.debug_message, f"Search attempt failed: {str(attempt_error)}")
                continue

            if entries or attempt_index == len(attempts) - 1:
                if not entries and cookie_opts:
                    wx.CallAfter(
                        self.debug_message,
                        "Search still returned no results without cookies. The site may be "
                        "filtering this query (YouTube SafeSearch / Restricted Mode, or the "
                        "term itself may not be allowed there).")
                return entries
            wx.CallAfter(
                self.debug_message,
                "Search with cookies returned no results; retrying anonymously in case "
                "SafeSearch filtering is active.")

        if last_error:
            raise last_error
        return []

    def fetch_search_results(self, query):
        try:
            search_target = query if query.startswith("http") else f"ytsearch10:{query}"
            entries = self._perform_yt_search(search_target)
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

            self.search_results_data[idx] = {'title': title, 'url': url}

        self.log_message(f"Loaded {len(entries)} results. Use arrow keys to explore.")
        if len(entries) > 0:
            self.results_list.SetFocus()
            self.results_list.SetItemState(0, wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
                                            wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED)
        # Audible cue that the search has finished and the list is now
        # populated -- plays even on a zero-result search, since the
        # search itself has still ended.
        play_sound(SOUND_SEARCH_COMPLETE)

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

    # Spoken aloud when a playback attempt fails while no browser cookie
    # source was found (cookie_label == 'no cookies'). Running without a
    # signed-in cookie context is the most common cause of failed YouTube
    # playback, and Firefox is the most reliable browser for yt-dlp to read
    # cookies from on Windows, so the guidance points the user there first.
    COOKIE_SETUP_GUIDANCE = (
        "To play uninterrupted videos, install Firefox browser, sign in to "
        "YouTube with your Gmail account. Check your settings to toggle your "
        "browser cookies configurations, then restart the app."
    )

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
                # Real validation: actually load the browser's cookie jar.
                # Constructing YoutubeDL alone does not trigger cookie
                # loading, so a browser with an unreadable database could
                # otherwise "validate" and then poison every client
                # fallback attempt with a cookie error.
                with YoutubeDL(opts) as ydl:
                    _load_browser_cookies(None, (browser,), ydl)
                wx.CallAfter(self.debug_message, f"Cookie source resolved: {browser}")
                self.working_cookie_label = browser
                return (browser, {'cookiesfrombrowser': (browser,)})
            except Exception as e:
                wx.CallAfter(self.debug_message, f"Cookie source '{browser}' unavailable: {str(e)}")
                continue

        wx.CallAfter(self.debug_message, "No browser cookie source available, proceeding without cookies")
        return ('no cookies', {})

    def _attempt_client_downloads(self, page_url, base_opts, cookie_opts, cancel_event=None):
        """Shared download engine for playback and the Ctrl+Shift+V video
        fetch. Tries yt-dlp's own default client selection first, then the
        fallback clients in CLIENT_FALLBACK_ORDER, each attempt with the
        resolved cookie source. If every client attempt with cookies fails
        and cookies were in play, retries once without them so a stale or
        unreadable cookie database can never block a stream that would
        otherwise download fine anonymously. Returns (info, local_path)."""
        client_attempts = [None] + self.CLIENT_FALLBACK_ORDER
        last_error = None
        info = None
        local_path = None

        for client in client_attempts:
            if cancel_event is not None and cancel_event.is_set():
                raise _PlaybackCancelled()
            ydl_opts = dict(base_opts)
            ydl_opts.update(cookie_opts)
            if client:
                ydl_opts['extractor_args'] = {'youtube': {'player_client': [client]}}

            label = client or 'default'
            wx.CallAfter(self.debug_message, f"Attempting download using player client: {label}")
            # Keep the open player window visibly alive during the
            # client-fallback chain (the debug log showed this taking
            # ~27s when several clients failed in a row) instead of
            # sitting on the same status line looking hung.
            if self.player_window is not None:
                wx.CallAfter(self.player_window.update_status,
                             f"Trying YouTube client '{label}'...")
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(page_url, download=True)
                    local_path = ydl.prepare_filename(info)
                wx.CallAfter(self.debug_message, f"Succeeded using player client: {label}")
                return info, local_path
            except _PlaybackCancelled:
                raise
            except Exception as attempt_error:
                if cancel_event is not None and cancel_event.is_set():
                    raise _PlaybackCancelled()
                last_error = attempt_error
                wx.CallAfter(self.debug_message, f"Player client '{label}' failed: {str(attempt_error)}")
                if self.player_window is not None:
                    wx.CallAfter(self.player_window.update_status,
                                 f"Client '{label}' failed, trying another...")
                continue

        # Every client attempt with cookies failed. If cookies were in
        # play, retry once without them before giving up so a cookie
        # problem can never block a stream that works anonymously.
        if cookie_opts:
            if cancel_event is not None and cancel_event.is_set():
                raise _PlaybackCancelled()
            wx.CallAfter(self.debug_message,
                         "All client attempts with cookies failed; retrying once without cookies.")
            try:
                with YoutubeDL(base_opts) as ydl:
                    info = ydl.extract_info(page_url, download=True)
                    local_path = ydl.prepare_filename(info)
                wx.CallAfter(self.debug_message, "Succeeded without cookies.")
                return info, local_path
            except _PlaybackCancelled:
                raise
            except Exception as attempt_error:
                last_error = attempt_error

        raise last_error if last_error else RuntimeError("No download attempt succeeded.")

    def play_media(self, index, as_video=False):
        entry = self.search_results_data.get(index)
        if not entry:
            self.debug_message(f"play_media called with index {index}, no matching entry in search_results_data")
            return
        url = entry['url']
        title = entry['title']

        # Cancel any previous playback that is still downloading before
        # starting a new one, so two videos never fetch to disk at once
        # and a stale one can never start playing later.
        self.cancel_playback()
        cancel_event = threading.Event()
        self._playback_cancel_event = cancel_event

        mode_label = "video" if as_video else "audio"
        self.debug_message(f"play_media: index {index}, title '{title}', source url {url}, mode {mode_label}")
        self.log_message(f"Downloading for playback: {title}")

        self.player_window = PlayerWindow(self, self.player, title, self.app_config,
                                          page_url=url, frame=self, is_audio=not as_video)
        threading.Thread(target=self.resolve_and_play, args=(url, title, cancel_event, as_video), daemon=True).start()
        self.player_window.ShowModal()
        self.player_window.Destroy()
        self.player_window = None
        # The modal player window has closed (Escape or Ctrl+W), so anything
        # still downloading for this playback is no longer wanted: abort it
        # and stop the player so it cannot start after the user moved on.
        self.cancel_playback()
        self.results_list.SetFocus()

    def resolve_and_play(self, page_url, title, cancel_event, as_video=False):
        """Fetches the selected stream to the local cache and plays it from
        disk. Audio-only (bestaudio) is the fast default because the app's
        video surface is a hidden 1x1 panel; video mode downloads a single
        combined video stream instead, which is slower to start. Playing
        from disk sidesteps YouTube's server-side anti-bot checks that
        reject direct googlevideo stream URLs with HTTP 403."""
        video_id = self._video_id_from_url(page_url)
        cookie_label = None  # set below by resolve_cookie_config; used to decide whether to speak cookie-setup guidance on failure
        try:
            outtmpl = os.path.join(self.playback_cache_dir, '%(id)s.%(ext)s')

            def _cancel_check(d):
                # Runs inside yt-dlp's download loop. Raising here aborts
                # the in-progress transfer immediately on Escape. Also
                # feeds live progress into the player window's status line
                # (throttled to ~1/sec) so a slow download never looks
                # like the app has hung -- it visibly shows it is working.
                if cancel_event.is_set():
                    raise _PlaybackCancelled()
                if d.get('status') == 'downloading':
                    percent = (d.get('_percent_str') or '').strip()
                    now = time.time()
                    if percent and now - getattr(self, '_playback_status_last', 0.0) >= 1.0:
                        self._playback_status_last = now
                        if self.player_window is not None:
                            wx.CallAfter(self.player_window.update_status,
                                         f"Downloading {percent}...")

            if as_video:
                fmt = 'best[height<=480]/best'
            else:
                fmt = 'bestaudio/best'

            base_opts = {
                'format': fmt,
                'quiet': True,
                'logger': YtdlpLoggerBridge(self.debug_message),
                'outtmpl': outtmpl,
                'noplaylist': True,
                # Retries raised from the old 3: the debug log showed
                # "[Errno 11001] getaddrinfo failed. Retrying (1/3)..."
                # during playback -- a transient Windows DNS hiccup that
                # only had 3 chances to clear before the whole play
                # attempt gave up. 10 retries ride through such blips.
                'retries': 10,
                'fragment_retries': 10,
                'socket_timeout': 20,
                'continuedl': True,
                'progress_hooks': [_cancel_check],
                # NOTE: do not pass 'impersonate' as a plain string here; on
                # this yt-dlp version that raises AssertionError at YoutubeDL
                # construction time (it expects an ImpersonateTarget object).
            }

            if cancel_event.is_set():
                raise _PlaybackCancelled()

            cookie_label, cookie_opts = self.resolve_cookie_config(base_opts)
            wx.CallAfter(self.debug_message, f"Using cookie source: {cookie_label}")

            info, local_path = self._attempt_client_downloads(page_url, base_opts, cookie_opts, cancel_event)

            wx.CallAfter(self.debug_message, f"Downloaded to local file: {local_path}")
            # Hand playback over to the main thread, which re-checks that the
            # user has not cancelled or closed the player window in the
            # meantime. This is what stops a slow download from playing after
            # the user has already moved on.
            wx.CallAfter(self._start_playback_if_current, local_path, title, cancel_event, video_id)
        except _PlaybackCancelled:
            self._cleanup_partial_download(video_id)
            wx.CallAfter(self.debug_message, "Playback cancelled before it could start.")
        except Exception as e:
            wx.CallAfter(self.log_message, f"Playback failed: {str(e)}")
            if self.player_window:
                self.player_window.update_status(f"Playback failed: {str(e)}")
            if cookie_label == 'no cookies':
                wx.CallAfter(self.log_and_announce, self.COOKIE_SETUP_GUIDANCE)
            wx.CallAfter(self.debug_message, "Traceback:\n" + traceback.format_exc())

    def cancel_playback(self):
        """Signals any in-flight playback download to abort and stops the
        player. Safe to call even when nothing is playing."""
        if self._playback_cancel_event is not None:
            self._playback_cancel_event.set()
        self.player.stop()

    def _video_id_from_url(self, url):
        match = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url)
        return match.group(1) if match else None

    def _cleanup_partial_download(self, video_id):
        """Removes leftover .part files from a cancelled download so a
        future play of the same video starts clean instead of resuming a
        half-written file."""
        if not video_id:
            return
        try:
            for fname in os.listdir(self.playback_cache_dir):
                if fname.startswith(video_id) and fname.endswith('.part'):
                    try:
                        os.remove(os.path.join(self.playback_cache_dir, fname))
                    except Exception:
                        continue
        except Exception:
            pass

    def _start_playback_if_current(self, local_path, title, cancel_event, video_id):
        """Runs on the main thread after a download completes. If the user
        already pressed Escape (or started a different video), the download
        is discarded instead of being played."""
        if cancel_event.is_set() or self.player_window is None:
            self._cleanup_partial_download(video_id)
            self.debug_message("Discarding download: playback was cancelled.")
            return
        self.player.load(local_path)
        if self.player_window:
            self.player_window.update_status(f"Now playing: {title}")
        self.log_message(f"Playing: {title}")

    def toggle_now_playing_video(self, page_url, player_window):
        """Ctrl+Shift+V from inside the audio player window. Opens a
        visible video window for the same source; pressing the hotkey
        again while it is open closes it."""
        if self._video_fetch_in_progress:
            if player_window:
                player_window.update_status("Video is still being fetched. Please wait.")
            return
        if self._video_window is not None:
            self._video_window.Close()
            return
        # Pause the audio while the video shows so the two never play
        # overlapping sound; audio resumes when the video window closes.
        try:
            self.player.media_player.pause()
        except Exception:
            pass
        self._video_fetch_in_progress = True
        if player_window:
            player_window.update_status("Fetching video to show... this can take a moment.")
        self.log_message("Fetching video for the currently playing track.")
        threading.Thread(target=self._fetch_video_for_display, args=(page_url,),
                         daemon=True).start()

    def _fetch_video_for_display(self, page_url):
        """Downloads a single combined video stream of the same source to
        the playback cache, then hands it to a visible VideoWindow on the
        main thread. Mirrors the client/cookie fallback logic used by
        normal playback."""
        cookie_label = None  # set below by resolve_cookie_config; used to decide whether to speak cookie-setup guidance on failure
        try:
            outtmpl = os.path.join(self.playback_cache_dir, '%(id)s.%(ext)s')
            ydl_opts = {
                'format': 'best[height<=480]/best',
                'quiet': True,
                'logger': YtdlpLoggerBridge(self.debug_message),
                'outtmpl': outtmpl,
                'noplaylist': True,
                'retries': 10,
                'fragment_retries': 10,
                'socket_timeout': 20,
                'continuedl': True,
            }
            # The video fetch has no Escape-cancel (unlike playback), so
            # the bundled aria2c can safely parallelize it into 16
            # connections -- the 66 MiB "format 18" download from the
            # logs gets several times faster on YouTube's per-connection
            # throttled CDN. Harmless no-op when aria2c is not bundled.
            self._apply_external_downloader(ydl_opts)
            cookie_label, cookie_opts = self.resolve_cookie_config(ydl_opts)
            # Cookies are applied per attempt inside _attempt_client_downloads,
            # which also retries once without them if every client attempt
            # with cookies fails, so a stale cookie store can never block the
            # video fetch.
            info, local_path = self._attempt_client_downloads(page_url, ydl_opts, cookie_opts)

            if local_path is None or not os.path.exists(local_path):
                raise RuntimeError("Video fetch failed.")

            title = info.get('title', 'Video')
            wx.CallAfter(self._open_video_window, local_path, title)
        except Exception as e:
            wx.CallAfter(self.log_message, f"Could not fetch video: {str(e)}")
            if cookie_label == 'no cookies':
                wx.CallAfter(self.log_and_announce, self.COOKIE_SETUP_GUIDANCE)
            wx.CallAfter(self.video_fetch_failed)

    def video_fetch_failed(self):
        self._video_fetch_in_progress = False
        self.resume_audio_after_video()
        if self.player_window:
            self.player_window.update_status("Could not fetch the video. Audio continues.")

    def _open_video_window(self, local_path, title):
        self._video_fetch_in_progress = False
        if self.player_window is None:
            # The audio player closed while the video was being fetched;
            # the download is no longer wanted.
            try:
                os.remove(local_path)
            except Exception:
                pass
            return
        self._video_window = VideoWindow(self.player_window, local_path, title,
                                         self.app_config, self)
        self._video_window.Show()
        self._video_window.Raise()
        self._video_window.status_ctrl.SetFocus()
        if self.player_window:
            self.player_window.update_status(
                "Video window open. Press Control+Shift+V or Escape in it to close."
            )

    def close_video_window(self):
        vw = self._video_window
        if vw is not None:
            try:
                vw.Close()
            except Exception:
                pass

    def video_window_closed(self):
        self._video_window = None
        self.resume_audio_after_video()

    def resume_audio_after_video(self):
        """Resumes the audio player after the video window closes, but
        only if the audio player window is still open and actually paused
        (never if it already ended or is mid-stop)."""
        try:
            if (self.player_window is not None and
                    self.player.media_player.get_state() == vlc.State.Paused):
                self.player.media_player.play()
                self.log_message("Audio playback resumed.")
        except Exception:
            pass

    def on_hotkey_mute(self, event):
        self.player.toggle_mute()

    def on_hotkey_playpause(self, event):
        self.player.toggle_play()

    def on_hotkey_download(self, event):
        indices = self.get_selected_indices()
        if not indices:
            self.log_message("No item selected to download.")
            return
        for idx in indices:
            item = self.search_results_data.get(idx)
            if item and item.get('url'):
                self.download_url_in_background(item['url'])

    def on_hotkey_copy_url(self, event):
        selected = self.get_selected_indices()
        if not selected:
            self.log_message("No item selected to copy.")
            return
        self.copy_url_to_clipboard(selected[0])

    def on_hotkey_email(self, event):
        selected = self.get_selected_indices()
        if not selected:
            self.log_message("No item selected to share by email.")
            return
        self.send_url_by_email(selected[0])

    def on_hotkey_open_browser(self, event):
        selected = self.get_selected_indices()
        if not selected:
            self.log_message("No item selected to open in the browser.")
            return
        url = self.search_results_data[selected[0]]['url']
        webbrowser.open(url)
        self.log_message("Opened link in your web browser.")

    def on_hotkey_play_video(self, event):
        selected = self.get_selected_indices()
        if not selected:
            self.log_message("No item selected to play.")
            return
        self.play_media(selected[0], as_video=True)

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
            search_target = f"ytsearch{self.current_result_count}:{self.current_search_query}"
            entries = self._perform_yt_search(search_target)
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
            self.search_results_data[idx] = {'title': title, 'url': url}
        if new_entries:
            self.log_message(f"Loaded {len(new_entries)} more results.")
        else:
            self.log_message("No more results.")

    def get_selected_indices(self):
        """Returns list of all highlighted item indices in the ListCtrl."""
        indices = []
        idx = self.results_list.GetFirstSelected()
        while idx != -1:
            indices.append(idx)
            idx = self.results_list.GetNextSelected(idx)
        return indices

    def on_show_context_menu(self, event):
        selected_indices = self.get_selected_indices()
        if not selected_indices:
            return

        menu = wx.Menu()
        count = len(selected_indices)

        if count == 1:
            selected_index = selected_indices[0]
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
        else:
            m_mp3 = menu.Append(wx.ID_ANY, f"Download Selected as MP3 ({count})")
            m_mp4 = menu.Append(wx.ID_ANY, f"Download Selected as MP4 ({count})")
            self.Bind(wx.EVT_MENU, lambda evt: self.batch_download(selected_indices, is_audio=True), m_mp3)
            self.Bind(wx.EVT_MENU, lambda evt: self.batch_download(selected_indices, is_audio=False), m_mp4)

        self.PopupMenu(menu)
        menu.Destroy()

    def batch_download(self, indices, is_audio):
        dlg = wx.DirDialog(self, "Choose Save Location", style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            out_dir = dlg.GetPath()
            dlg.Destroy()
            for idx in indices:
                item = self.search_results_data.get(idx)
                if not item:
                    continue
                url = item.get('webpage_url') or item.get('url')
                if url:
                    threading.Thread(
                        target=self.run_download_task,
                        args=(url, is_audio, out_dir),
                        daemon=True
                    ).start()
        else:
            dlg.Destroy()

    def copy_text_to_clipboard(self, text):
        """Puts arbitrary text on the system clipboard and confirms it."""
        ok = False
        if wx.TheClipboard.Open():
            ok = wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
        if ok:
            self.log_message("Copied link to clipboard.")
        return ok

    def copy_url_to_clipboard(self, index):
        url = self.search_results_data[index]['url']
        self.copy_text_to_clipboard(url)

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
            ok, probe_err = _probe_download_dir(playlists_dir)
            if not ok:
                self.log_and_announce(
                    "Channel download failed: your download folder is not "
                    f"available ({output_dir}). Open Settings with Alt+S and "
                    "choose a new download folder, then try again."
                )
                self.debug_message(
                    f"Playlist download folder unusable: {playlists_dir} ({probe_err})"
                )
                return
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

            # aria2c parallelizes every video in the batch; same trade-off
            # as single downloads (no per-file % progress spoken while
            # aria2c runs) for much higher throughput on a channel-length
            # run, where speed matters most.
            self._apply_external_downloader(ydl_opts)

            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            self.log_and_announce(f"Channel/playlist download complete: {playlists_dir}")
        except Exception as e:
            self.log_and_announce(f"Channel download error: {str(e)}")
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

    def get_ffmpeg_path(self):
        """Locates bundled FFmpeg binary relative to root or app directory."""
        tools_path = os.path.join(BASE_DIR, 'tools', 'ffmpeg.exe')
        if os.path.exists(tools_path):
            return tools_path
        app_tools_path = os.path.join(BASE_DIR, 'app', 'tools', 'ffmpeg.exe')
        if os.path.exists(app_tools_path):
            return app_tools_path
        if not getattr(sys, 'frozen', False):
            # Dev mode only: fall back to dependencies\ffmpeg\ directly --
            # the single shared folder run.bat/build.bat both fetch into
            # (see fetch_ffmpeg.py) -- so a plain dev checkout works even
            # without a separate tools\ copy at the root.
            deps_ffmpeg_path = os.path.join(BASE_DIR, 'dependencies', 'ffmpeg', 'ffmpeg.exe')
            if os.path.exists(deps_ffmpeg_path):
                return deps_ffmpeg_path
        return None

    def get_aria2c_path(self):
        """Locates bundled aria2c.exe relative to root or app directory."""
        tools_path = os.path.join(BASE_DIR, 'tools', 'aria2c.exe')
        if os.path.exists(tools_path):
            return tools_path
        app_tools_path = os.path.join(BASE_DIR, 'app', 'tools', 'aria2c.exe')
        if os.path.exists(app_tools_path):
            return app_tools_path
        if not getattr(sys, 'frozen', False):
            # Dev mode only: fall back to dependencies/aria2/ directly --
            # the single shared folder run.bat/build.bat both fetch into
            # (see fetch_aria2.py) -- so a plain dev checkout works even
            # without a separate tools/ copy at the root.
            deps_aria2_path = os.path.join(BASE_DIR, 'dependencies', 'aria2', 'aria2c.exe')
            if os.path.exists(deps_aria2_path):
                return deps_aria2_path
        return None

    def _apply_external_downloader(self, ydl_opts):
        """Speeds up non-playback downloads (save-to-disk, channel/
        playlist, and the Ctrl+Shift+V video fetch) with the bundled
        aria2c when it is present: aria2c opens up to 16 parallel
        connections to the same stream, which routinely multiplies
        throughput on links YouTube's CDN throttles per connection (the
        log's 500 KiB/s dips are exactly that cap), and each segment
        retries on its own, so a transient DNS/connection hiccup no
        longer stalls the whole file. Playback's cache download does NOT
        use aria2c on purpose: Escape-to-cancel relies on yt-dlp's
        progress hooks, which external downloaders do not fire mid-
        transfer. If no aria2c is bundled, opts are left untouched and
        yt-dlp's own single-connection downloader is used, so nothing
        breaks either way."""
        aria2c = self.get_aria2c_path()
        if not aria2c:
            return
        aria2_dir = os.path.dirname(aria2c)
        # yt-dlp resolves the external downloader by name on PATH.
        # Prepend the bundled copy's folder once (same technique already
        # used for libvlc) instead of passing an absolute path, so the
        # frozen app never depends on an aria2c install on the user's
        # machine.
        if aria2_dir not in os.environ.get('PATH', ''):
            os.environ['PATH'] = aria2_dir + os.pathsep + os.environ.get('PATH', '')
        ydl_opts['external_downloader'] = 'aria2c'
        ydl_opts['external_downloader_args'] = {
            'aria2c': ['-x', '16', '-s', '16', '-k', '1M',
                       '--file-allocation=none', '--console-log-level=warn']
        }
        wx.CallAfter(self.debug_message,
                     "Using aria2c (16 parallel connections) for a faster download.")

    def _is_audio_download(self):
        return self.app_config.get('download_format', 'MP3 Audio') == 'MP3 Audio'

    def download_url_in_background(self, url):
        """Queues a single URL for download using the configured format,
        straight to the configured download folder with no extra prompts."""
        output_dir = self.app_config['download_path']
        is_audio = self._is_audio_download()
        self.log_message(f"Queueing background download for: {url}")
        threading.Thread(target=self.run_download_task,
                          args=(url, is_audio, output_dir), daemon=True).start()

    def start_download(self, index, is_audio):
        url = self.search_results_data[index]['url']
        output_dir = self.app_config['download_path']

        self.log_message(f"Queueing background download for: {url}")
        threading.Thread(target=self.run_download_task,
                          args=(url, is_audio, output_dir), daemon=True).start()

    def run_download_task(self, url, is_audio, output_dir):
        # Validate the destination up front, before announcing that the
        # download started: an unusable folder (missing, disconnected
        # drive, or permissions) produces one clear, actionable
        # announcement here instead of a generic yt-dlp failure after
        # "Download started."
        ok, probe_err = _probe_download_dir(output_dir)
        if not ok:
            self.log_and_announce(
                "Download failed: your download folder is not available "
                f"({output_dir}). Open Settings with Alt+S and choose a new "
                "download folder, then try again."
            )
            self.debug_message(f"Download folder unusable: {output_dir} ({probe_err})")
            return
        try:
            bitrate = self.get_audio_bitrate()
            ffmpeg_bin = self.get_ffmpeg_path()
            outtmpl = os.path.join(output_dir, '%(title)s.%(ext)s')

            ydl_opts = {
                'outtmpl': outtmpl,
                'progress_hooks': [self.ytdl_hook],
                'ffmpeg_location': ffmpeg_bin,
                'writethumbnail': True,
                'logger': YtdlpLoggerBridge(self.debug_message),
                'retries': 10,
                'fragment_retries': 10,
                'socket_timeout': 30,
                'continuedl': True,
            }

            cookie_label, cookie_opts = self.resolve_cookie_config(ydl_opts)
            ydl_opts.update(cookie_opts)

            postprocessors = [{
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            }]

            if is_audio:
                ydl_opts.update({'format': 'bestaudio/best'})
                postprocessors.extend([
                    {
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': bitrate,
                    },
                    {'key': 'EmbedThumbnail'},
                ])
            else:
                ydl_opts.update({'format': self.get_video_format_string()})
                postprocessors.append({'key': 'EmbedThumbnail'})

            ydl_opts['postprocessors'] = postprocessors

            # Speed up save-to-disk downloads with the bundled aria2c
            # (16 parallel connections) when present; falls back to
            # yt-dlp's own single-connection downloader otherwise.
            # NOTE: with an external downloader yt-dlp does not fire the
            # per-progress hooks, so the "Download progress: x%" spoken
            # updates pause until the transfer finishes -- the trade-off
            # for typically 3-8x faster throughput on throttled links.
            self._apply_external_downloader(ydl_opts)

            self._last_progress_announce = time.time()
            self.log_and_announce("Download started.")

            # If the first attempt fails and cookies were in play, retry the
            # same download once without them, so a cookie issue (for example
            # a browser whose cookie database has gone away) can never block a
            # download that would otherwise work without cookies.
            attempt_opts_list = [ydl_opts]
            if cookie_opts:
                no_cookie_opts = dict(ydl_opts)
                no_cookie_opts.pop('cookiesfrombrowser', None)
                no_cookie_opts.pop('cookiefile', None)
                attempt_opts_list.append(no_cookie_opts)

            for attempt_index, attempt_opts in enumerate(attempt_opts_list):
                try:
                    with YoutubeDL(attempt_opts) as ydl:
                        ydl.download([url])
                    break
                except Exception:
                    if attempt_index == 0 and len(attempt_opts_list) > 1:
                        self.log_message("Download attempt with cookies failed; retrying without cookies.")
                        continue
                    raise

            self.log_and_announce("Download finished successfully.")
            self.log_message(f"Saved to {output_dir}")
        except Exception as e:
            self.log_and_announce(f"Download failed: {str(e)}")
            self.debug_message("Traceback:\n" + traceback.format_exc())

    def ytdl_hook(self, d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            eta = d.get('_eta_str', 'unknown')
            now = time.time()
            last = getattr(self, '_last_progress_announce', 0.0)
            if now - last >= 5:
                self._last_progress_announce = now
                self.log_and_announce(f"Download progress: {percent} | ETA: {eta}")


if __name__ == '__main__':
    app = wx.App(False)
    frame = AccessibleDownloaderFrame()
    app.MainLoop()
