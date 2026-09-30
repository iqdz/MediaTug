"""Hands announcements to the running screen reader, the way ZBox does.

accessible_output2 passes the text straight to whichever screen reader is
running (NVDA, JAWS, Dolphin, System Access, PC Talker, ZDSR) through that
reader's own client library, so the reader speaks it in the user's own
voice, rate and settings. No window, no focus change, nothing on screen.

The library also has Microsoft SAPI as its last resort. That is never used
here: when no screen reader is running, speak() returns False and the
caller shows the message visually instead. Media Tug has no voice of its
own.

Main thread only, like every call into the readers: their COM objects
belong to the thread that made them. Callers on worker threads go through
wx.CallAfter.
"""

# False = not built yet, None = unavailable (library missing or broken).
_auto = False

# Outputs that are a speech voice rather than a screen reader.
_NOT_READERS = ('sapi5', 'sapi4')


def _get_auto():
    global _auto
    if _auto is not False:
        return _auto
    _auto = None
    try:
        from accessible_output2.outputs.auto import Auto
        _auto = Auto()
    except Exception:
        # Not installed, or no output it can use. Messages are then
        # shown visually instead.
        _auto = None
    return _auto


def warm_up():
    """Builds the speech output now instead of on the first announcement,
    which could otherwise freeze the window for a moment mid-action."""
    _get_auto()


def active_reader():
    """The running screen reader's output, or None. Checked on every call,
    so a reader started or stopped after Media Tug opened is picked up."""
    auto = _get_auto()
    if auto is None:
        return None
    try:
        output = auto.get_first_available_output()
    except Exception:
        return None
    if output is None or type(output).__name__.lower() in _NOT_READERS:
        return None
    return output


def is_running():
    return active_reader() is not None


def speak(text, interrupt=True):
    """Says text through the running screen reader. True when a reader
    took it; False when there is none, so the caller shows it instead."""
    if not text:
        return False
    output = active_reader()
    if output is None:
        return False
    try:
        output.speak(str(text), interrupt=interrupt)
        return True
    except Exception:
        return False
