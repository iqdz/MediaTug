"""Favorite Channels: a saved list of YouTube channels, each showing how
many new videos it has had since it was last opened in Media Tug.

The list is kept in data\\favorite_channels.json, beside the app's
settings. The network work (looking up a channel, checking it for new
videos) is done by the fetch_page callable the main window passes in, so
this module has no yt-dlp or cookie code of its own and always uses the
same cookie setup as search and playback.

Keys in the dialog: Enter opens the selected channel, F5 checks every
channel for new videos, Delete removes one, Escape closes. Focus starts in
the list, and one Tab reaches Add a channel URL; when the list is empty,
focus starts on that button.
"""
import json
import os
import re
import threading
import time

import wx

# How many of a channel's newest videos are fetched when it is opened,
# checked or added, and how many more each Load more step adds.
CHECK_COUNT = 30

_CHANNEL_RE = re.compile(
    r'^https?://(?:www\.|m\.)?youtube\.com/'
    r'(?P<path>@[^/?#\s]+|channel/UC[\w-]+|c/[^/?#\s]+|user/[^/?#\s]+)',
    re.IGNORECASE)


def channel_base_url(text):
    """The channel's base URL, without a tab or query, when text is a
    YouTube channel link; otherwise None."""
    if not text:
        return None
    match = _CHANNEL_RE.match(text.strip())
    if not match:
        return None
    return 'https://www.youtube.com/' + match.group('path')


def videos_url(base_url):
    """The channel's Videos tab, which lists the newest video first."""
    return base_url.rstrip('/') + '/videos'


def entry_ids(entries):
    return [e.get('id') for e in entries if e and e.get('id')]


def count_new(seen_ids, ids):
    """(count, more): how many videos sit above the newest one that was
    there when the channel was last opened. more is True when none of the
    checked videos had been seen, so the real number may be higher."""
    seen = set(seen_ids or [])
    if not seen:
        return 0, False
    count = 0
    for video_id in ids:
        if video_id in seen:
            return count, False
        count += 1
    return count, True


def channel_name(info):
    name = info.get('channel') or info.get('uploader') or info.get('title') or ''
    name = re.sub(r'\s+-\s+Videos$', '', name).strip()
    return name or 'Unnamed channel'


def row_label(channel):
    count = channel.get('new_count', 0)
    if channel.get('new_more'):
        news = f'{count} or more new videos'
    elif count == 1:
        news = '1 new video'
    elif count:
        news = f'{count} new videos'
    else:
        news = 'no new videos'
    return f"{channel.get('name', 'Unnamed channel')}, {news}"


class ChannelStore:
    """The saved channel list. Main thread only."""

    def __init__(self, path):
        self.path = path
        self.channels = []
        self.load()

    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                self.channels = [c for c in data if isinstance(c, dict) and c.get('url')]
        except FileNotFoundError:
            self.channels = []
        except Exception:
            # Unreadable file: park it out of the way and start empty,
            # rather than failing on every launch.
            try:
                os.replace(self.path, self.path + '.unusable')
            except Exception:
                pass
            self.channels = []

    def save(self):
        try:
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.channels, f, indent=2)
            os.replace(tmp, self.path)
            return True
        except Exception:
            return False

    def find(self, key):
        for channel in self.channels:
            if key and (channel.get('id') == key or channel.get('url') == key):
                return channel
        return None

    def add_from_info(self, base_url, info):
        """Adds the channel described by a fetched Videos page. Returns the
        new entry, or None when it is already in the list."""
        entries = [e for e in (info.get('entries') or []) if e]
        channel_id = info.get('channel_id') or info.get('uploader_id') or base_url
        if str(channel_id).startswith('UC'):
            url = f'https://www.youtube.com/channel/{channel_id}'
        else:
            url = base_url
        if self.find(channel_id) or self.find(url):
            return None
        now = time.time()
        channel = {
            'id': channel_id,
            'name': channel_name(info),
            'url': url,
            'seen_ids': entry_ids(entries)[:CHECK_COUNT],
            'new_count': 0,
            'new_more': False,
            'last_opened': now,
            'last_checked': now,
        }
        self.channels.append(channel)
        self.save()
        return channel

    def update_count(self, channel, entries):
        channel['new_count'], channel['new_more'] = count_new(channel.get('seen_ids'), entry_ids(entries))
        channel['last_checked'] = time.time()

    def mark_opened(self, channel, entries):
        ids = entry_ids(entries)[:CHECK_COUNT]
        if ids:
            channel['seen_ids'] = ids
        channel['new_count'] = 0
        channel['new_more'] = False
        channel['last_opened'] = time.time()
        self.save()

    def remove(self, channel):
        if channel in self.channels:
            self.channels.remove(channel)
            self.save()


def _clipboard_text():
    text = ''
    try:
        if wx.TheClipboard.Open():
            try:
                if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)) or \
                        wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_TEXT)):
                    data = wx.TextDataObject()
                    if wx.TheClipboard.GetData(data):
                        text = data.GetText()
            finally:
                wx.TheClipboard.Close()
    except Exception:
        pass
    return text or ''


def _entries(info):
    return [e for e in ((info or {}).get('entries') or []) if e]


class FavoriteChannelsDialog(wx.Dialog):
    """fetch_page(url, count) runs on a worker thread and returns the
    yt-dlp info dict of a channel's Videos page. announce(text) is
    thread-safe: it goes to the screen reader, or on screen when none is
    running. After ShowModal returns wx.ID_OK, selected_channel is the
    channel to open."""

    def __init__(self, parent, store, fetch_page, announce):
        super().__init__(parent, title='Favorite Channels',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.store = store
        self.fetch_page = fetch_page
        self.announce = announce
        self.selected_channel = None
        self._busy = False

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(panel, label='&Channels')
        sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        self.list = wx.ListBox(panel, style=wx.LB_SINGLE)
        self.list.SetName('Favorite channels')
        self.list.SetMinSize((480, 260))
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.add_btn = wx.Button(panel, label='&Add a channel URL')
        close_btn = wx.Button(panel, wx.ID_CLOSE, label='Close')
        buttons.Add(self.add_btn, 0, wx.RIGHT, 8)
        buttons.Add(close_btn, 0)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        hint = wx.StaticText(panel, label='Enter opens a channel. F5 checks for new videos. '
                                          'Delete removes a channel. Escape closes.')
        sizer.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetEscapeId(wx.ID_CLOSE)
        self.CentreOnParent()

        self.add_btn.Bind(wx.EVT_BUTTON, self.on_add)
        close_btn.Bind(wx.EVT_BUTTON, lambda _e: self.EndModal(wx.ID_CANCEL))
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._open_selected())
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

        self._fill(select=0)
        if self.store.channels:
            self.list.SetFocus()
        else:
            self.add_btn.SetFocus()

    # -- list ----------------------------------------------------------------

    def _fill(self, select=None):
        self.list.Set([row_label(c) for c in self.store.channels])
        if self.store.channels and select is not None:
            self.list.SetSelection(max(0, min(select, len(self.store.channels) - 1)))

    def _update_row(self, channel):
        if channel in self.store.channels:
            index = self.store.channels.index(channel)
            if index < self.list.GetCount():
                self.list.SetString(index, row_label(channel))

    def _selected(self):
        index = self.list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self.store.channels):
            return None, index
        return self.store.channels[index], index

    # -- keys ----------------------------------------------------------------

    def _on_key(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_F5:
            self.refresh_all()
            return
        if wx.Window.FindFocus() is self.list:
            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                self._open_selected()
                return
            if key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
                self._remove_selected()
                return
        event.Skip()

    def _open_selected(self):
        channel, _index = self._selected()
        if channel is None:
            return
        self.selected_channel = channel
        self.EndModal(wx.ID_OK)

    def _remove_selected(self):
        channel, index = self._selected()
        if channel is None:
            return
        answer = wx.MessageBox(f"Remove {channel.get('name')} from Favorite Channels?",
                               'Remove channel', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self)
        if answer != wx.YES:
            self.list.SetFocus()
            return
        self.store.remove(channel)
        self._fill(select=index)
        self.announce(f"Removed {channel.get('name')}.")
        if self.store.channels:
            self.list.SetFocus()
        else:
            self.add_btn.SetFocus()

    # -- add -----------------------------------------------------------------

    def on_add(self, _event):
        if self._busy:
            self.announce('Please wait, a channel lookup or check is still running.')
            return
        clip = _clipboard_text().strip()
        prefill = clip if channel_base_url(clip) else ''
        dlg = wx.TextEntryDialog(self, 'Paste the channel URL:', 'Add a channel URL', value=prefill)
        ok = dlg.ShowModal() == wx.ID_OK
        text = dlg.GetValue()
        dlg.Destroy()
        if not ok:
            self.add_btn.SetFocus()
            return
        base = channel_base_url(text)
        if not base:
            wx.MessageBox('That is not a YouTube channel link. Channel links look like '
                          'youtube.com/@name or youtube.com/channel/ followed by the channel ID.',
                          'Add a channel URL', wx.OK | wx.ICON_INFORMATION, self)
            self.add_btn.SetFocus()
            return
        self._busy = True
        self.announce('Looking up the channel.')
        threading.Thread(target=self._add_worker, args=(base,), daemon=True).start()

    def _add_worker(self, base):
        store, announce, fetch = self.store, self.announce, self.fetch_page
        try:
            info, error = fetch(videos_url(base), CHECK_COUNT), None
        except Exception as e:
            info, error = None, e
        wx.CallAfter(_finish_add, self, store, announce, base, info, error)

    # -- refresh -------------------------------------------------------------

    def refresh_all(self):
        if self._busy:
            self.announce('A channel lookup or check is already running.')
            return
        channels = list(self.store.channels)
        if not channels:
            self.announce('There are no channels to check yet.')
            return
        self._busy = True
        noun = 'channel' if len(channels) == 1 else 'channels'
        self.announce(f'Checking {len(channels)} {noun} for new videos.')
        threading.Thread(target=self._refresh_worker, args=(channels,), daemon=True).start()

    def _refresh_worker(self, channels):
        store, announce, fetch = self.store, self.announce, self.fetch_page
        failed = 0
        for channel in channels:
            try:
                entries = _entries(fetch(videos_url(channel['url']), CHECK_COUNT))
                wx.CallAfter(_apply_count, self, store, channel, entries)
            except Exception:
                failed += 1
        wx.CallAfter(_finish_refresh, self, store, announce, channels, failed)


# These run on the main thread through wx.CallAfter. They take the store
# and announce separately because the dialog may already be closed; a
# closed wx dialog tests False, and then only the saved list is updated.

def _finish_add(dialog, store, announce, base, info, error):
    channel = None
    if error is not None or not info:
        announce(f"Could not add the channel: {error if error is not None else 'nothing was found'}")
    else:
        channel = store.add_from_info(base, info)
        if channel is None:
            announce('That channel is already in Favorite Channels.')
        else:
            announce(f"Added {channel['name']}.")
    if dialog:
        dialog._busy = False
        if channel is not None:
            dialog._fill(select=store.channels.index(channel))
            dialog.list.SetFocus()
        else:
            dialog.add_btn.SetFocus()


def _apply_count(dialog, store, channel, entries):
    store.update_count(channel, entries)
    if dialog:
        dialog._update_row(channel)


def _finish_refresh(dialog, store, announce, channels, failed):
    store.save()
    total = sum(c.get('new_count', 0) for c in channels)
    with_new = sum(1 for c in channels if c.get('new_count', 0))
    if total:
        noun = 'video' if total == 1 else 'videos'
        where = 'channel' if with_new == 1 else 'channels'
        message = f'Done. {total} new {noun} in {with_new} {where}.'
    else:
        message = 'Done. No new videos.'
    if failed:
        message += f" {failed} {'channel' if failed == 1 else 'channels'} could not be checked."
    announce(message)
    if dialog:
        dialog._busy = False
