# PyInstaller spec for Media Tug.
#
# Produces a --onedir build (a folder, not a single exe) named "app" --
# matching what get_base_dir() in media_tug.py already expects: the exe
# lives in an "app" subfolder, with "cookies", "data", "logs", and "tools"
# as siblings of "app", all under one root folder.
#
# Build with:
#   pyinstaller media_tug.spec
#
# See build.bat for the full process, including the two manual steps
# PyInstaller cannot do for you (copying VLC's DLLs and ffmpeg.exe).

import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
hiddenimports = []

# yt-dlp is installed by update_ytdlp.py into dependencies\yt-dlp (a
# pip --target folder, not the active environment's site-packages) so
# run.bat and build.bat both fetch/cache it in exactly one shared place.
# That means it isn't importable by default in the Python environment
# running this spec file -- add it to sys.path before collect_all()
# below tries to import it, and to pathex so Analysis() resolves
# media_tug.py's own "from yt_dlp import ..." the same way.
_ytdlp_dir = os.path.join(SPECPATH, 'dependencies', 'yt-dlp')
if os.path.isdir(_ytdlp_dir) and _ytdlp_dir not in sys.path:
    sys.path.insert(0, _ytdlp_dir)

# run.bat installs accessible_output2 (and a few small helpers) into
# libs\, so the build looks there too, the same way as for yt-dlp above.
_libs_dir = os.path.join(SPECPATH, 'libs')
if os.path.isdir(_libs_dir) and _libs_dir not in sys.path:
    sys.path.insert(0, _libs_dir)

# Screen reader client libraries, as in ZBox's zbox.spec.
#
# Media Tug has no voice of its own. An announcement is handed to
# whichever screen reader is running through that reader's own client
# library, and those are DLLs inside the accessible_output2 package, in a
# lib folder beside its Python files. PyInstaller does not collect a
# package's data folder on its own: from source everything works, and a
# build without them starts fine and says nothing, on exactly the
# machines that matter. collect_data_files keeps the package-relative
# path, so they land at _internal\accessible_output2\lib, which is where
# the library looks when frozen. A build without them stops here.
_AO2_CLIENT_LIBS = collect_data_files('accessible_output2', includes=['lib/*.dll'])
if not _AO2_CLIENT_LIBS:
    raise SystemExit(
        'No screen reader client libraries were found inside the '
        'accessible_output2 package. A build without them cannot reach '
        'any screen reader. Run run.bat once, which installs it into '
        'libs, then build again.'
    )

# Bundle the sounds/ folder's WAV files (system-ready.wav, complete.wav)
# as data files for the frozen build. NOTE: PyInstaller 6.x+ places data
# files in the onedir 'contents directory' (_internal\sounds) rather than
# directly beside the exe -- get_sounds_dir() in media_tug.py checks both
# locations (plus sys._MEIPASS for onefile), so either layout works.
# SPECPATH is the folder this .spec file lives in (PyInstaller provides
# it; plain __file__ isn't reliable inside a spec).
_sounds_src_dir = os.path.join(SPECPATH, 'sounds')
_sound_files = glob.glob(os.path.join(_sounds_src_dir, '*.wav'))
datas += [(f, 'sounds') for f in _sound_files]

# yt-dlp loads most of its site extractors dynamically; collect_all pulls
# in every extractor module plus any data files so none go missing at
# runtime in a frozen build (this is yt-dlp's own recommended approach
# for PyInstaller).
_ytdlp_datas, _ytdlp_binaries, _ytdlp_hidden = collect_all('yt_dlp')
datas += _ytdlp_datas
binaries += _ytdlp_binaries
hiddenimports += _ytdlp_hidden

# Screen reader output: the client DLLs collected above, and the modules
# reached only through an import inside a function in screen_reader.py.
datas += _AO2_CLIENT_LIBS
hiddenimports += ['accessible_output2.outputs.auto', 'win32com.client']

a = Analysis(
    ['media_tug.py'],
    pathex=[p for p in (_ytdlp_dir, _libs_dir) if os.path.isdir(p)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='media_tug',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,       # no console window -- the wx UI and the
                          # screen reader are the whole interface
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='app',          # output folder name -- matches get_base_dir()'s
                          # expectation that the exe lives in "app"
)
