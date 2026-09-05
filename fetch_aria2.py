"""Ensures a bundled aria2c.exe (the Windows build of the aria2 download
accelerator, straight from the official GitHub releases) is present,
current, and copied into a target folder -- without depending on aria2
already being installed, or on PATH, on whatever machine is doing the
building or running.

Why this exists: YouTube's CDN throttles downloads per TCP connection,
which is why Media Tug's log shows big transfers crawling along at
~500 KiB/s no matter how fast the connection is. aria2c splits each
download into up to 16 parallel connections, which routinely multiplies
throughput 3-8x on such throttled links, and each segment retries on its
own, so a transient DNS/connection hiccup (like the "[Errno 11001]
getaddrinfo failed. Retrying (1/3)..." lines in media_tug_debug.log) no
longer stalls the whole file. media_tug.py uses it for save-to-disk
downloads, channel/playlist downloads, and the Ctrl+Shift+V video
fetch -- not for the play-now cache download, which keeps yt-dlp's
native downloader so Escape-to-cancel keeps working.

Shared by run.bat (dev mode, so a developer's own machine exercises the
exact same bundled-aria2c code path a shipped build uses) and build.bat
(build time, populating release\\MediaTug\\tools\\, from which the MSI
installer is harvested).

Every run appends a timestamped line to fetch.log inside --cache, in
addition to printing to the console -- so a failure is never lost to a
scrolled-past console window. Delete fetch.log any time; it's diagnostic
only and never read back in by this script.

Behavior:
  - Reads the latest aria2 release metadata from GitHub's API.
  - Compares that to whatever's recorded in the cache.
  - Skips the (small, ~1-2 MB) download entirely when the cache is
    already current, and just copies aria2c.exe out of the cache.
  - Otherwise downloads the win-64 zip, extracts just aria2c.exe into
    the cache, and records the new version. The zip is extracted with
    Python's standard library -- no py7zr or other extra dependency.
  - If GitHub is unreachable, falls back to whatever's already in the
    cache -- a slightly stale aria2c still works fine offline.

Exit codes:
  0 -- aria2c.exe is ready in --target
  1 -- not available and could not be downloaded either
"""
import argparse
import datetime
import json
import os
import shutil
import socket
import sys
import tempfile
import urllib.request
import zipfile

# Same reasoning as fetch_ffmpeg.py: urlretrieve has no timeout of its
# own, so bound the process-wide default to keep a stalled connection
# from hanging the build forever.
socket.setdefaulttimeout(30)

API_URL = "https://api.github.com/repos/aria2/aria2/releases/latest"
EXE_NAME = "aria2c.exe"
VERSION_FILENAME = "aria2-version.txt"
LOG_FILENAME = "fetch.log"


def log(cache_dir, message):
    print(message)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, LOG_FILENAME), "a", encoding="utf-8") as f:
            timestamp = datetime.datetime.now().isoformat(timespec="seconds")
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass  # logging is best-effort; never let it break the actual fetch


def get_latest_release():
    """Returns (tag, asset_name, asset_url) for the newest Windows zip
    in the latest aria2 GitHub release."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": "MediaTug-fetch-aria2"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = str(data.get("tag_name", "")).lstrip("v")
    assets = data.get("assets", []) or []
    win_zips = [
        a for a in assets
        if "win-64bit" in a.get("name", "") and a.get("name", "").lower().endswith(".zip")
    ]
    if not win_zips:
        names = ", ".join(a.get("name", "?") for a in assets) or "(none)"
        raise RuntimeError(f"no win-64bit zip asset found in latest aria2 release "
                           f"(tag {tag}); assets: {names}")
    asset = win_zips[0]
    return tag, asset["name"], asset["browser_download_url"]


def cache_is_valid(cache_dir):
    return os.path.isfile(os.path.join(cache_dir, EXE_NAME))


def read_cached_version(cache_dir):
    path = os.path.join(cache_dir, VERSION_FILENAME)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="ascii") as f:
        return f.read().strip()


def write_cached_version(cache_dir, version):
    with open(os.path.join(cache_dir, VERSION_FILENAME), "w", encoding="ascii") as f:
        f.write(version)


def download_and_extract(cache_dir, asset_name, asset_url):
    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, asset_name)
        log(cache_dir, f"Downloading {asset_url} ...")
        try:
            urllib.request.urlretrieve(asset_url, archive_path)
        except Exception as e:
            raise RuntimeError(f"download from {asset_url} failed: {e}")
        downloaded_size = os.path.getsize(archive_path)
        log(cache_dir, f"Downloaded {downloaded_size:,} bytes.")
        if downloaded_size < 100 * 1024:
            # aria2's win64 zip is ~1-2 MB; anything under 100 KB is
            # almost certainly an HTML error page, not a real archive.
            with open(archive_path, "r", encoding="utf-8", errors="replace") as f:
                preview = f.read(500)
            log(cache_dir, f"Downloaded file is suspiciously small ({downloaded_size} bytes) -- "
                            f"probably not a real archive. First 500 chars: {preview!r}")
            raise RuntimeError("downloaded file is too small to be a real aria2 archive "
                                "(likely blocked/redirected by a firewall or proxy -- see fetch.log)")

        with zipfile.ZipFile(archive_path) as zf:
            found = None
            for name in zf.namelist():
                if os.path.basename(name).lower() == EXE_NAME.lower():
                    found = name
                    break
            if found is None:
                raise RuntimeError(f"could not find {EXE_NAME} inside the downloaded archive "
                                   f"(see fetch.log for what it contained: {zf.namelist()})")
            # Extract the single exe directly, no need to unpack everything.
            os.makedirs(cache_dir, exist_ok=True)
            with zf.open(found) as src, open(os.path.join(cache_dir, EXE_NAME), "wb") as dst:
                shutil.copyfileobj(src, dst)
        log(cache_dir, f"Copied {EXE_NAME} into the cache.")


def copy_from_cache(cache_dir, target_dir):
    if os.path.abspath(cache_dir) == os.path.abspath(target_dir):
        return  # already in place -- run.bat points --cache and --target at the same folder
    os.makedirs(target_dir, exist_ok=True)
    shutil.copy2(os.path.join(cache_dir, EXE_NAME), os.path.join(target_dir, EXE_NAME))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True,
                         help="Persistent dependencies folder for aria2 (e.g. dependencies\\aria2). "
                              "You can drop aria2c.exe in here yourself at any time -- if a valid "
                              "file is already present, this script only ever replaces it when "
                              "GitHub has published a version newer than what's recorded here.")
    parser.add_argument("--target", required=True,
                         help="Destination dir for aria2c.exe (e.g. release\\MediaTug\\tools)")
    args = parser.parse_args()

    os.makedirs(args.cache, exist_ok=True)
    log(args.cache, "--- fetch_aria2.py run starting ---")
    cache_valid = cache_is_valid(args.cache)
    cached_version = read_cached_version(args.cache)

    if cache_valid and cached_version is None:
        # aria2c.exe is already sitting here with no version recorded --
        # almost always placed here by hand. Trust it: don't force a
        # redownload just because we don't know its exact version.
        log(args.cache, "aria2c.exe already present with no recorded version "
                         "(likely placed here manually) -- using it as-is.")
        try:
            tag, _name, _url = get_latest_release()
            write_cached_version(args.cache, tag)
        except Exception:
            pass  # fine to not know the version yet; try again next run
        copy_from_cache(args.cache, args.target)
        log(args.cache, f"Done -- aria2c copied into {args.target}")
        return 0

    try:
        tag, asset_name, asset_url = get_latest_release()
    except Exception as e:
        log(args.cache, f"Could not reach GitHub to check the latest aria2 release ({e}).")
        if cache_valid:
            log(args.cache, f"Using cached aria2c {cached_version or '(unknown version)'} (offline copy).")
            copy_from_cache(args.cache, args.target)
            log(args.cache, f"Done -- aria2c copied into {args.target}")
            return 0
        log(args.cache, "aria2c is not cached and could not be downloaded either. FAILING.")
        return 1

    if tag == cached_version and cache_valid:
        log(args.cache, f"aria2 {tag} is already cached -- using it, nothing to download.")
        copy_from_cache(args.cache, args.target)
        log(args.cache, f"Done -- aria2c {tag} copied into {args.target}")
        return 0

    log(args.cache, f"Fetching aria2 {tag} (currently cached: {cached_version or 'none'})...")
    try:
        download_and_extract(args.cache, asset_name, asset_url)
        write_cached_version(args.cache, tag)
    except Exception as e:
        log(args.cache, f"Could not download/extract aria2c: {e}")
        if cache_is_valid(args.cache):
            log(args.cache, "Falling back to the existing cached copy.")
            copy_from_cache(args.cache, args.target)
            return 0
        log(args.cache, "No usable cache to fall back to. FAILING -- tools\\ will not have aria2c.")
        return 1

    copy_from_cache(args.cache, args.target)
    log(args.cache, f"Done -- aria2c {tag} copied into {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
