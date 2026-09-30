"""Download formats: which video and audio file formats Media Tug saves,
the quality choices each one offers, and the yt-dlp options that produce
them. Also the Advanced download options dialog that picks them.

Formats are chosen so the common cases need no re-encoding at all, which
is the fastest path: MP4, MOV and AVI take YouTube's H.264 and AAC
streams, WebM takes its VP9 and Opus streams, and for audio M4A and Opus
copy the original stream straight into the file when it already matches.
Everything else is converted by the bundled full ffmpeg build.

Settings keys used, all in the app's saved settings:
  download_format   'Audio' or 'Video'
  video_container   one of VIDEO_FORMATS
  video_quality     one of VIDEO_QUALITIES[video_container]
  audio_codec       one of AUDIO_FORMATS
  audio_quality     one of AUDIO_QUALITIES[audio_codec]
"""
import re

import wx

VIDEO_FORMATS = ['MP4', 'MKV', 'WebM', 'MOV', 'AVI']
_HEIGHTS_FULL = ['Best available', '2160p', '1440p', '1080p', '720p', '480p', '360p']
VIDEO_QUALITIES = {
    'MP4': _HEIGHTS_FULL,
    'MKV': _HEIGHTS_FULL,
    'WebM': _HEIGHTS_FULL,
    'MOV': _HEIGHTS_FULL,
    # AVI is an old container; very large pictures are rarely playable in it.
    'AVI': ['Best available', '1080p', '720p', '480p', '360p'],
}

AUDIO_FORMATS = ['MP3', 'M4A (AAC)', 'Opus', 'OGG Vorbis', 'FLAC', 'WAV', 'ALAC']
AUDIO_QUALITIES = {
    'MP3': ['320 kbps', '256 kbps', '192 kbps', '128 kbps'],
    'M4A (AAC)': ['256 kbps', '192 kbps', '128 kbps'],
    'Opus': ['160 kbps', '128 kbps', '96 kbps'],
    'OGG Vorbis': ['320 kbps', '256 kbps', '192 kbps', '128 kbps'],
    'FLAC': ['Lossless'],
    'WAV': ['Lossless'],
    'ALAC': ['Lossless'],
}
# yt-dlp's FFmpegExtractAudio codec names.
_AUDIO_CODEC = {'MP3': 'mp3', 'M4A (AAC)': 'm4a', 'Opus': 'opus', 'OGG Vorbis': 'vorbis',
                'FLAC': 'flac', 'WAV': 'wav', 'ALAC': 'alac'}
# Source streams to prefer, so a matching one is copied instead of converted.
_AUDIO_SOURCE = {'M4A (AAC)': 'bestaudio[ext=m4a]/bestaudio/best',
                 'ALAC': 'bestaudio[ext=m4a]/bestaudio/best',
                 'Opus': 'bestaudio[acodec=opus]/bestaudio/best'}
# Formats yt-dlp can embed a thumbnail into.
_THUMB_AUDIO = {'MP3', 'M4A (AAC)', 'Opus', 'OGG Vorbis', 'FLAC', 'ALAC'}
_THUMB_VIDEO = {'MP4', 'MKV', 'MOV'}

DEFAULTS = {
    'download_format': 'Audio',
    'video_container': 'MP4',
    'video_quality': 'Best available',
    'audio_codec': 'MP3',
    'audio_quality': '192 kbps',
}


def audio_label(config):
    return config.get('audio_codec', 'MP3')


def video_label(config):
    return config.get('video_container', 'MP4')


def _pick(value, choices, fallback):
    return value if value in choices else fallback


def normalize_config(config):
    """Brings saved settings from older versions in line: 'MP3 Audio' and
    'MP4 Video' become 'Audio' and 'Video', '192 kbps (High)' becomes
    '192 kbps', and any value a format does not offer falls back to that
    format's nearest sensible choice. Changes config in place."""
    for key, value in DEFAULTS.items():
        config.setdefault(key, value)
    fmt = str(config.get('download_format', 'Audio'))
    config['download_format'] = 'Video' if 'video' in fmt.lower() else 'Audio'

    config['video_container'] = _pick(config.get('video_container'), VIDEO_FORMATS, 'MP4')
    qualities = VIDEO_QUALITIES[config['video_container']]
    config['video_quality'] = _pick(config.get('video_quality'), qualities, 'Best available')

    config['audio_codec'] = _pick(config.get('audio_codec'), AUDIO_FORMATS, 'MP3')
    qualities = AUDIO_QUALITIES[config['audio_codec']]
    old = str(config.get('audio_quality', ''))
    match = re.match(r'\s*(\d+)\s*kbps', old)
    if match and f'{match.group(1)} kbps' in qualities:
        config['audio_quality'] = f'{match.group(1)} kbps'
    elif old not in qualities:
        config['audio_quality'] = '192 kbps' if '192 kbps' in qualities else qualities[0]
    return config


def video_format_selector(container, quality):
    match = re.match(r'(\d+)p', quality or '')
    cap = f'[height<={match.group(1)}]' if match else ''
    if container in ('MP4', 'MOV', 'AVI'):
        return f'bestvideo{cap}[ext=mp4]+bestaudio[ext=m4a]/bestvideo{cap}+bestaudio/best{cap}'
    if container == 'WebM':
        return f'bestvideo{cap}[ext=webm]+bestaudio[ext=webm]/bestvideo{cap}+bestaudio/best{cap}'
    return f'bestvideo{cap}+bestaudio/best{cap}'


def apply_download_format(ydl_opts, config, is_audio, embed_metadata=True):
    """Sets format, conversion and thumbnail options on ydl_opts for the
    formats chosen in Settings and Advanced download options."""
    postprocessors = []
    if is_audio:
        name = config.get('audio_codec', 'MP3')
        ydl_opts['format'] = _AUDIO_SOURCE.get(name, 'bestaudio/best')
        extract = {'key': 'FFmpegExtractAudio', 'preferredcodec': _AUDIO_CODEC.get(name, 'mp3')}
        match = re.match(r'(\d+)\s*kbps', config.get('audio_quality', ''))
        if match:
            # MP3 takes a plain number; the others a bitrate such as 192K.
            extract['preferredquality'] = match.group(1) if name == 'MP3' else f'{match.group(1)}K'
        postprocessors.append(extract)
        thumbnail = name in _THUMB_AUDIO
    else:
        name = config.get('video_container', 'MP4')
        ydl_opts['format'] = video_format_selector(name, config.get('video_quality', 'Best available'))
        ydl_opts['merge_output_format'] = name.lower()
        # A single-file stream in another container is repackaged, not
        # re-encoded. yt-dlp's option name really is spelled this way.
        postprocessors.append({'key': 'FFmpegVideoRemuxer', 'preferedformat': name.lower()})
        thumbnail = name in _THUMB_VIDEO
    if embed_metadata:
        postprocessors.append({'key': 'FFmpegMetadata', 'add_metadata': True})
    if thumbnail:
        ydl_opts['writethumbnail'] = True
        postprocessors.append({'key': 'EmbedThumbnail'})
    else:
        ydl_opts.pop('writethumbnail', None)
    ydl_opts['postprocessors'] = postprocessors
    return ydl_opts


class AdvancedDownloadDialog(wx.Dialog):
    """Tab order: video format, its quality list, audio format, its
    quality list, OK, Cancel. Enter anywhere is OK. Changing a format
    refills its quality list with what that format offers."""

    def __init__(self, parent, values):
        super().__init__(parent, title='Advanced download options')
        self.values = dict(values)
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        def labelled(text, choices, name):
            sizer.Add(wx.StaticText(panel, label=text), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
            ctrl = wx.Choice(panel, choices=choices)
            ctrl.SetName(name)
            sizer.Add(ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)
            return ctrl

        self.video_format = labelled('&Video format:', VIDEO_FORMATS, 'Video format')
        self.video_quality = labelled('Video &quality:', [], 'Video quality')
        self.audio_format = labelled('&Audio format:', AUDIO_FORMATS, 'Audio format')
        self.audio_quality = labelled('Audio q&uality:', [], 'Audio quality')

        buttons = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(panel, wx.ID_OK)
        cancel_btn = wx.Button(panel, wx.ID_CANCEL)
        ok_btn.SetDefault()
        buttons.AddButton(ok_btn)
        buttons.AddButton(cancel_btn)
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetMinSize((380, -1))
        self.CentreOnParent()

        self.video_format.SetStringSelection(self.values.get('video_container', 'MP4'))
        self._fill(self.video_quality, VIDEO_QUALITIES[self.video_format.GetStringSelection()],
                   self.values.get('video_quality'))
        self.audio_format.SetStringSelection(self.values.get('audio_codec', 'MP3'))
        self._fill(self.audio_quality, AUDIO_QUALITIES[self.audio_format.GetStringSelection()],
                   self.values.get('audio_quality'))

        self.video_format.Bind(wx.EVT_CHOICE, self._on_video_format)
        self.audio_format.Bind(wx.EVT_CHOICE, self._on_audio_format)
        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.video_format.SetFocus()

    @staticmethod
    def _fill(ctrl, choices, current):
        ctrl.Set(choices)
        ctrl.SetStringSelection(current if current in choices else choices[0])

    def _on_video_format(self, _event):
        self._fill(self.video_quality, VIDEO_QUALITIES[self.video_format.GetStringSelection()],
                   self.video_quality.GetStringSelection())

    def _on_audio_format(self, _event):
        self._fill(self.audio_quality, AUDIO_QUALITIES[self.audio_format.GetStringSelection()],
                   self.audio_quality.GetStringSelection())

    def _on_key(self, event):
        # Enter on any of the four lists means OK, as on the OK button.
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and \
                isinstance(wx.Window.FindFocus(), wx.Choice):
            self._on_ok(None)
            return
        event.Skip()

    def _on_ok(self, _event):
        self.values.update({
            'video_container': self.video_format.GetStringSelection(),
            'video_quality': self.video_quality.GetStringSelection(),
            'audio_codec': self.audio_format.GetStringSelection(),
            'audio_quality': self.audio_quality.GetStringSelection(),
        })
        self.EndModal(wx.ID_OK)
