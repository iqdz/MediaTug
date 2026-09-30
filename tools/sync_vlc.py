"""Ensures libvlc.dll, libvlccore.dll, and the plugins\\ folder are
present, current, and copied into a destination app folder -- without
requiring VLC to be installed on the machine doing the building or
running.

Primary source: VideoLAN's official win64 "last" build folder, which
always holds the current *stable* release as a plain .zip with no
installer needed -- https://download.videolan.org/pub/videolan/vlc/last/win64/.
Cached by version, the same pattern update_ytdlp.py and fetch_ffmpeg.py
use, so a build doesn't re-download VLC every single time.

Fallback, in order, if VideoLAN can't be reached:
  1. A real VLC install found on this machine (--source, e.g. under
     Program Files) -- compared against the cache by hashing
     libvlc.dll+libvlccore.dll together (unlike a download from
     VideoLAN's site, a local install has no version string handy to
     compare against), and used to refresh the cache if different.
  2. Whatever's already sitting in the cache from a previous run,
     regardless of how current it is.

Every run appends a timestamped line to sync.log inside --cache, in
addition to printing to the console -- so a failure is never lost to a
scrolled-past console window. Delete sync.log any time; it's diagnostic
only and never read back in by this script.

Exit codes:
  0 -- destination now has libvlc.dll, libvlccore.dll, and plugins\\
  1 -- no usable download, no usable local install, and no usable
       cache; destination was not updated
"""
import argparse
import datetime
import hashlib
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.request
import zipfile

# See fetch_ffmpeg.py for why this matters: urlretrieve has no timeout
# parameter of its own, so without a process-wide default a stalled
# connection can hang forever with zero output.
socket.setdefaulttimeout(30)

LAST_WIN64_URL = "https://download.videolan.org/pub/videolan/vlc/last/win64/"
DLL_NAMES = ("libvlc.dll", "libvlccore.dll")
VERSION_FILENAME = "vlc-version.txt"
HASH_FILENAME = "vlc.hash"
LOG_FILENAME = "sync.log"


def log(cache_dir, message):
    print(message)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, LOG_FILENAME), "a", encoding="utf-8") as f:
            timestamp = datetime.datetime.now().isoformat(timespec="seconds")
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass  # logging is best-effort; never let it break the actual sync


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def combined_hash(dll_dir):
    """Hash of libvlc.dll + libvlccore.dll together, in a fixed name
    order so the result is stable regardless of directory listing
    order. Raises if either DLL is missing."""
    h = hashlib.sha256()
    for name in DLL_NAMES:
        h.update(hash_file(os.path.join(dll_dir, name)).encode("ascii"))
    return h.hexdigest()


def cache_is_valid(cache_dir):
    return (os.path.isfile(os.path.join(cache_dir, DLL_NAMES[0]))
            and os.path.isfile(os.path.join(cache_dir, DLL_NAMES[1]))
            and os.path.isdir(os.path.join(cache_dir, "plugins")))


def read_text(cache_dir, filename):
    path = os.path.join(cache_dir, filename)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="ascii") as f:
        return f.read().strip()


def write_text(cache_dir, filename, value):
    with open(os.path.join(cache_dir, filename), "w", encoding="ascii") as f:
        f.write(value)


def copy_vlc_files(src_dir, dest_dir):
    if os.path.abspath(src_dir) == os.path.abspath(dest_dir):
        return  # already in place -- run.bat points --cache and --dest at the same folder
    os.makedirs(dest_dir, exist_ok=True)
    for name in DLL_NAMES:
        shutil.copy2(os.path.join(src_dir, name), os.path.join(dest_dir, name))
    dest_plugins = os.path.join(dest_dir, "plugins")
    if os.path.isdir(dest_plugins):
        shutil.rmtree(dest_plugins)
    shutil.copytree(os.path.join(src_dir, "plugins"), dest_plugins)


def find_latest_zip_name(cache_dir):
    """Parses VideoLAN's win64 'last' (i.e. current stable) directory
    listing for the vlc-<version>-win64.zip filename."""
    log(cache_dir, f"Checking {LAST_WIN64_URL} for the latest stable VLC build...")
    try:
        with urllib.request.urlopen(LAST_WIN64_URL, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"could not reach VideoLAN ({e})")
    matches = re.findall(r'href="(vlc-([\d.]+)-win64\.zip)"', html)
    if not matches:
        log(cache_dir, f"Directory listing had no matching links. First 500 chars: {html[:500]!r}")
        raise RuntimeError("could not find a vlc-*-win64.zip link on VideoLAN's site "
                            "(see sync.log for what the page actually returned)")
    # Normally there's exactly one; if VideoLAN ever lists more than
    # one, the highest version string wins.
    matches.sort(key=lambda pair: [int(p) for p in pair[1].split(".")])
    filename, version = matches[-1]
    return filename, version


def download_and_extract(cache_dir, filename):
    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, filename)
        log(cache_dir, f"Downloading {filename} from VideoLAN...")
        last_reported = [0]

        def report_progress(block_num, block_size, total_size):
            downloaded = block_num * block_size
            if total_size > 0:
                pct = min(100, downloaded * 100 // total_size)
                if pct >= last_reported[0] + 10:
                    last_reported[0] = pct
                    log(cache_dir, f"  ...{pct}% ({downloaded:,}/{total_size:,} bytes)")

        try:
            urllib.request.urlretrieve(LAST_WIN64_URL + filename, archive_path, reporthook=report_progress)
        except Exception as e:
            raise RuntimeError(f"download of {filename} failed: {e}")
        downloaded_size = os.path.getsize(archive_path)
        log(cache_dir, f"Downloaded {downloaded_size:,} bytes.")
        if downloaded_size < 1024 * 1024:
            # A real VLC win64 zip is tens of MB. Anything under 1 MB is
            # almost certainly a block/redirect page saved with the .zip
            # name, not a real archive -- fail loudly with its content
            # logged, instead of a confusing "not a zip file" error below.
            with open(archive_path, "r", encoding="utf-8", errors="replace") as f:
                preview = f.read(500)
            log(cache_dir, f"Downloaded file is suspiciously small ({downloaded_size} bytes). "
                            f"First 500 chars: {preview!r}")
            raise RuntimeError("downloaded file is too small to be a real VLC archive "
                                "(likely blocked/redirected by a firewall or proxy -- see sync.log)")

        extract_dir = os.path.join(tmp, "extracted")
        # See fetch_ffmpeg.py's identical retry loop: Windows sometimes
        # briefly locks a just-downloaded file (almost always antivirus
        # scanning it), not anything wrong with the download itself.
        last_error = None
        for attempt in range(10):
            try:
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(extract_dir)
                last_error = None
                break
            except PermissionError as e:
                last_error = e
                log(cache_dir, f"  Archive is still locked (attempt {attempt + 1}/10: {e}) -- "
                                f"probably antivirus scanning it briefly. Retrying in 2s...")
                time.sleep(2)
        if last_error is not None:
            raise RuntimeError(f"could not open the downloaded zip after repeated retries (still locked): {last_error}")

        # Zip layout is vlc-<version>\{libvlc.dll,libvlccore.dll,plugins\...}
        entries = os.listdir(extract_dir)
        if len(entries) != 1:
            raise RuntimeError(f"unexpected archive layout -- top level had {entries!r}, expected one folder")
        vlc_root = os.path.join(extract_dir, entries[0])
        if not cache_is_valid(vlc_root):
            log(cache_dir, f"Contents of {entries[0]}: {os.listdir(vlc_root)}")
            raise RuntimeError(f"{entries[0]} did not contain the expected DLLs/plugins folder "
                                f"(see sync.log for what it actually contained)")
        copy_vlc_files(vlc_root, cache_dir)
        log(cache_dir, "Copied libvlc.dll, libvlccore.dll, and plugins\\ into the cache.")


def try_download(cache_dir):
    filename, version = find_latest_zip_name(cache_dir)
    cached_version = read_text(cache_dir, VERSION_FILENAME)
    if version == cached_version and cache_is_valid(cache_dir):
        log(cache_dir, f"VLC {version} is already cached -- using it, nothing to download.")
        return
    log(cache_dir, f"Fetching VLC {version} (currently cached: {cached_version or 'none'})...")
    download_and_extract(cache_dir, filename)
    write_text(cache_dir, VERSION_FILENAME, version)
    # A freshly downloaded copy replaces any older hash-fallback marker,
    # so a later offline run doesn't compare against a stale local-install hash.
    hash_path = os.path.join(cache_dir, HASH_FILENAME)
    if os.path.isfile(hash_path):
        os.remove(hash_path)


def try_local_install_fallback(cache_dir, source_dir):
    if not source_dir:
        return False
    try:
        source_hash = combined_hash(source_dir)
    except OSError as e:
        log(cache_dir, f"Could not read VLC files at {source_dir} ({e}).")
        return False
    cached_hash = read_text(cache_dir, HASH_FILENAME) if cache_is_valid(cache_dir) else None
    if source_hash == cached_hash:
        log(cache_dir, "Local VLC install matches the cached copy -- using cache.")
        return True
    log(cache_dir, "Refreshing the cache from the local VLC install instead.")
    copy_vlc_files(source_dir, cache_dir)
    write_text(cache_dir, HASH_FILENAME, source_hash)
    return True


def flatten_if_nested(cache_dir):
    """If the folder isn't directly valid but contains exactly one
    subfolder that is, move that subfolder's contents up a level
    automatically. Handles the most likely mistake when manually
    extracting a VLC zip by hand: it keeps its own top-level
    vlc-<version>\\ folder, so the DLLs/plugins end up one level too
    deep to be recognized as-is."""
    if cache_is_valid(cache_dir):
        return
    try:
        entries = [e for e in os.listdir(cache_dir) if os.path.isdir(os.path.join(cache_dir, e))]
    except OSError:
        return
    for entry in entries:
        candidate = os.path.join(cache_dir, entry)
        if cache_is_valid(candidate):
            log(cache_dir, f"Found VLC files nested inside '{entry}\\' -- flattening them up into this folder directly.")
            for name in DLL_NAMES:
                shutil.move(os.path.join(candidate, name), os.path.join(cache_dir, name))
            shutil.move(os.path.join(candidate, "plugins"), os.path.join(cache_dir, "plugins"))
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=None,
                         help="A local VLC install dir (e.g. Program Files\\VideoLAN\\VLC), "
                              "used only as a fallback if VideoLAN can't be reached.")
    parser.add_argument("--cache", required=True,
                         help="Persistent dependencies folder for VLC (e.g. dependencies\\vlc). "
                              "You can drop libvlc.dll/libvlccore.dll/plugins\\ in here yourself at any "
                              "time -- if valid files are already present, this script never overwrites "
                              "them just because it doesn't know their exact version; it only replaces "
                              "them once it can confirm (from VideoLAN) that a newer stable build exists.")
    parser.add_argument("--dest", required=True, help="Destination dir to copy libvlc.dll/libvlccore.dll/plugins into")
    args = parser.parse_args()

    os.makedirs(args.cache, exist_ok=True)
    log(args.cache, "--- sync_vlc.py run starting ---")
    flatten_if_nested(args.cache)

    if cache_is_valid(args.cache) and read_text(args.cache, VERSION_FILENAME) is None \
            and read_text(args.cache, HASH_FILENAME) is None:
        # Valid VLC files are already here with no version or hash marker
        # recorded -- almost always placed here by hand. Trust them as-is
        # rather than forcing a redownload just because their exact
        # version is unknown to us.
        log(args.cache, "VLC files already present with no recorded version "
                         "(likely placed here manually) -- using them as-is.")
        try:
            _filename, version = find_latest_zip_name(args.cache)
            write_text(args.cache, VERSION_FILENAME, version)
        except Exception:
            pass  # fine to not know the version yet; try again next run
        copy_vlc_files(args.cache, args.dest)
        return 0

    try:
        try_download(args.cache)
    except Exception as e:
        log(args.cache, f"Could not fetch VLC from VideoLAN: {e}")
        if not try_local_install_fallback(args.cache, args.source):
            if not cache_is_valid(args.cache):
                log(args.cache, "No download, no local VLC install, and no cached copy either. FAILING.")
                return 1
            log(args.cache, "Using the last cached copy from a previous build.")

    if not cache_is_valid(args.cache):
        log(args.cache, "Cache is still not valid after all fallbacks. FAILING.")
        return 1

    copy_vlc_files(args.cache, args.dest)
    log(args.cache, f"Done -- VLC files copied into {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
