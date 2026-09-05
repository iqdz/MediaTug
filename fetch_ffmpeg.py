"""Ensures a bundled ffmpeg.exe and ffprobe.exe (Gyan.dev "full" static
Windows build) are present, current, and copied into a target folder --
without depending on ffmpeg already being installed, or on PATH, on
whatever machine is doing the building or running.

This closes the actual bug that first surfaced this feature: a portable
build's downloads all failed with "Download failed" for a user, because
the release's tools\\ folder never actually got ffmpeg.exe -- build.bat
used to only look for ffmpeg on the build machine's own PATH, which is
fine for the developer but is never guaranteed for anyone else, and was
never guaranteed to travel into the MSI installer at all (see
build_installer.bat / installer.wxs). Playback needs no ffmpeg at all,
but the real "save to disk" download always post-processes with it --
extracting MP3, embedding thumbnails, writing metadata -- so a missing
ffmpeg only ever showed up as downloads failing, never as playback
failing, which is exactly what got reported.

Shared by run.bat (dev mode, so a developer's own machine exercises the
exact same bundled-ffmpeg code path a shipped build uses) and build.bat
(build time, populating release\\MediaTug\\tools\\).

Every run appends a timestamped line to fetch.log inside --cache,
in addition to printing to the console -- so a failure is never lost to
a scrolled-past console window, and there's a persistent record of every
attempt (version seen, what was downloaded, what failed and why) to look
back at or share for debugging. Delete fetch.log any time; it's diagnostic
only and never read back in by this script.

Behavior:
  - Reads Gyan.dev's published version string for the "release-full"
    channel (stable ffmpeg releases -- the "full" build was chosen over
    "essentials" for its extra codecs/filters, at the cost of a larger
    download).
  - Compares that to whatever's recorded in the cache.
  - Skips the (large, ~100+ MB) download entirely when the cache is
    already current, and just copies the two exes out of the cache.
  - Otherwise downloads the 7z archive, verifies it against Gyan.dev's
    published sha256 when available, extracts just ffmpeg.exe and
    ffprobe.exe into the cache, and records the new version.
  - If Gyan.dev is unreachable, falls back to whatever's already in the
    cache -- a slightly stale ffmpeg still works fine offline.
  - The "full" build is only published as a .7z, not a .zip, so this
    needs py7zr to open it. That's a build-time-only dependency (this
    script pip-installs it into the current Python environment itself
    if missing) -- nothing about py7zr ships inside the frozen app.

Exit codes:
  0 -- ffmpeg.exe/ffprobe.exe are ready in --target
  1 -- not available and could not be downloaded either
"""
import argparse
import datetime
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

# urllib.request.urlretrieve has no timeout parameter of its own -- if a
# connection stalls (not refused, just stuck, e.g. behind certain
# firewalls/proxies) it can hang forever with zero output. Setting the
# process-wide default timeout is what actually bounds it, since
# urlretrieve/urlopen fall back to this when no per-call timeout is given.
socket.setdefaulttimeout(30)

VERSION_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
ARCHIVE_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z"
SHA256_URL = ARCHIVE_URL + ".sha256"
EXE_NAMES = ("ffmpeg.exe", "ffprobe.exe")
VERSION_FILENAME = "ffmpeg-version.txt"
LOG_FILENAME = "fetch.log"
PIP_TIMEOUT_SECONDS = 600


def log(cache_dir, message):
    print(message)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, LOG_FILENAME), "a", encoding="utf-8") as f:
            timestamp = datetime.datetime.now().isoformat(timespec="seconds")
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass  # logging is best-effort; never let it break the actual fetch


def ensure_py7zr(cache_dir):
    try:
        import py7zr  # noqa: F401
    except ImportError:
        log(cache_dir, "py7zr not found -- installing it now (build-time only; it is never bundled into the app)...")
        try:
            result = subprocess.run([sys.executable, "-m", "pip", "install", "py7zr"],
                                     capture_output=True, text=True, timeout=PIP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"pip install py7zr did not finish within {PIP_TIMEOUT_SECONDS}s -- "
                                f"giving up rather than hanging indefinitely")
        if result.returncode != 0:
            log(cache_dir, f"pip install py7zr FAILED (exit {result.returncode}).")
            log(cache_dir, f"pip stdout: {result.stdout.strip()}")
            log(cache_dir, f"pip stderr: {result.stderr.strip()}")
            raise RuntimeError("could not install py7zr -- see fetch.log for pip's exact output")
        log(cache_dir, "py7zr installed successfully.")


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_latest_version():
    with urllib.request.urlopen(VERSION_URL, timeout=15) as resp:
        return resp.read().decode("ascii").strip()


def cache_is_valid(cache_dir):
    return all(os.path.isfile(os.path.join(cache_dir, name)) for name in EXE_NAMES)


def read_cached_version(cache_dir):
    path = os.path.join(cache_dir, VERSION_FILENAME)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="ascii") as f:
        return f.read().strip()


def write_cached_version(cache_dir, version):
    with open(os.path.join(cache_dir, VERSION_FILENAME), "w", encoding="ascii") as f:
        f.write(version)


def download_and_extract(cache_dir):
    ensure_py7zr(cache_dir)
    import py7zr

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, "ffmpeg-release-full.7z")
        log(cache_dir, f"Downloading {ARCHIVE_URL} ...")
        last_reported = [0]

        def report_progress(block_num, block_size, total_size):
            downloaded = block_num * block_size
            if total_size > 0:
                pct = min(100, downloaded * 100 // total_size)
                if pct >= last_reported[0] + 10:
                    last_reported[0] = pct
                    log(cache_dir, f"  ...{pct}% ({downloaded:,}/{total_size:,} bytes)")

        try:
            urllib.request.urlretrieve(ARCHIVE_URL, archive_path, reporthook=report_progress)
        except Exception as e:
            raise RuntimeError(f"download from {ARCHIVE_URL} failed: {e}")
        downloaded_size = os.path.getsize(archive_path)
        log(cache_dir, f"Downloaded {downloaded_size:,} bytes.")
        if downloaded_size < 1024 * 1024:
            # A real "full" ffmpeg 7z is well over 100 MB. Anything under
            # 1 MB is almost certainly an HTML error page (e.g. a proxy's
            # block page, or a redirected 404) saved with the .7z name,
            # not an actual archive -- fail loudly here with that file's
            # own content in the log, instead of letting py7zr raise a
            # much more confusing "not a 7z archive" error below.
            with open(archive_path, "r", encoding="utf-8", errors="replace") as f:
                preview = f.read(500)
            log(cache_dir, f"Downloaded file is suspiciously small ({downloaded_size} bytes) -- "
                            f"this is probably not a real archive. First 500 chars: {preview!r}")
            raise RuntimeError("downloaded file is too small to be a real ffmpeg archive "
                                "(likely blocked/redirected by a firewall or proxy -- see fetch.log)")

        try:
            with urllib.request.urlopen(SHA256_URL, timeout=15) as resp:
                expected = resp.read().decode("ascii").split()[0].lower()
            actual = hash_file(archive_path)
            if actual != expected:
                raise RuntimeError(f"checksum mismatch (expected {expected}, got {actual})")
            log(cache_dir, "Checksum verified OK.")
        except Exception as e:
            log(cache_dir, f"Warning: could not verify the ffmpeg download's checksum ({e}). Continuing anyway.")

        extract_dir = os.path.join(tmp, "extracted")
        log(cache_dir, "Extracting archive (this can take a minute)...")
        # Right after urlretrieve finishes, Windows sometimes still has the
        # freshly-written file locked for a moment -- almost always
        # antivirus real-time scanning it, not anything actually wrong
        # with the download (this is a very common Windows quirk, not
        # specific to this archive or this machine). Retrying briefly
        # clears it in practically all cases; only give up if it's still
        # locked after several seconds, since that could mean something
        # else entirely.
        last_error = None
        for attempt in range(10):
            try:
                with py7zr.SevenZipFile(archive_path, mode="r") as archive:
                    archive.extractall(path=extract_dir)
                last_error = None
                break
            except PermissionError as e:
                last_error = e
                log(cache_dir, f"  Archive is still locked (attempt {attempt + 1}/10: {e}) -- "
                                f"probably antivirus scanning it briefly. Retrying in 2s...")
                time.sleep(2)
        if last_error is not None:
            raise RuntimeError(f"py7zr could not open the downloaded archive after repeated retries "
                                f"(still locked): {last_error}")

        # Archive layout is ffmpeg-<version>-full_build/bin/{ffmpeg,ffprobe}.exe
        found = {}
        all_exes_seen = []
        for root, _dirs, files in os.walk(extract_dir):
            for name in files:
                if name.lower() in EXE_NAMES and name.lower() not in found:
                    found[name.lower()] = os.path.join(root, name)
                if name.lower().endswith(".exe"):
                    all_exes_seen.append(os.path.relpath(os.path.join(root, name), extract_dir))
        missing = [n for n in EXE_NAMES if n not in found]
        if missing:
            log(cache_dir, f"Extracted archive's .exe files: {all_exes_seen or '(none found at all)'}")
            raise RuntimeError(f"could not find {', '.join(missing)} inside the downloaded archive "
                                f"(see fetch.log for every .exe the archive actually contained)")

        os.makedirs(cache_dir, exist_ok=True)
        for name, path in found.items():
            shutil.copy2(path, os.path.join(cache_dir, name))
        log(cache_dir, f"Copied {', '.join(found)} into the cache.")


def copy_from_cache(cache_dir, target_dir):
    if os.path.abspath(cache_dir) == os.path.abspath(target_dir):
        return  # already in place -- run.bat points --cache and --target at the same folder
    os.makedirs(target_dir, exist_ok=True)
    for name in EXE_NAMES:
        shutil.copy2(os.path.join(cache_dir, name), os.path.join(target_dir, name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True,
                         help="Persistent dependencies folder for ffmpeg (e.g. dependencies\\ffmpeg). "
                              "You can drop ffmpeg.exe/ffprobe.exe in here yourself at any time -- "
                              "if valid files are already present, this script only ever replaces them "
                              "when Gyan.dev has published a version newer than what's recorded here; "
                              "it never re-downloads files that are already present and current.")
    parser.add_argument("--target", required=True,
                         help="Destination dir for ffmpeg.exe/ffprobe.exe (e.g. release\\MediaTug\\tools)")
    args = parser.parse_args()

    os.makedirs(args.cache, exist_ok=True)
    log(args.cache, "--- fetch_ffmpeg.py run starting ---")
    cache_valid = cache_is_valid(args.cache)
    cached_version = read_cached_version(args.cache)

    if cache_valid and cached_version is None:
        # Valid ffmpeg.exe/ffprobe.exe are already sitting here, but with
        # no version recorded -- almost always because they were placed
        # here by hand rather than by this script. Trust them: don't
        # force a redownload just because we don't know their exact
        # version. We still try to record *a* version below (so future
        # runs can tell whether Gyan.dev has moved on), but a failed/
        # skipped version check here is not a reason to overwrite files
        # that already work.
        log(args.cache, "ffmpeg.exe/ffprobe.exe already present with no recorded version "
                         "(likely placed here manually) -- using them as-is.")
        try:
            write_cached_version(args.cache, get_latest_version())
        except Exception:
            pass  # fine to not know the version yet; try again next run
        copy_from_cache(args.cache, args.target)
        log(args.cache, f"Done -- ffmpeg copied into {args.target}")
        return 0

    try:
        latest = get_latest_version()
    except Exception as e:
        log(args.cache, f"Could not reach Gyan.dev to check the latest ffmpeg version ({e}).")
        if cache_valid:
            log(args.cache, f"Using cached ffmpeg {cached_version or '(unknown version)'} (offline copy).")
            copy_from_cache(args.cache, args.target)
            log(args.cache, f"Done -- ffmpeg copied into {args.target}")
            return 0
        log(args.cache, "ffmpeg is not cached and could not be downloaded either. FAILING.")
        return 1

    if latest == cached_version and cache_valid:
        log(args.cache, f"ffmpeg {latest} is already cached -- using it, nothing to download.")
        copy_from_cache(args.cache, args.target)
        log(args.cache, f"Done -- ffmpeg {latest} copied into {args.target}")
        return 0

    log(args.cache, f"Fetching ffmpeg {latest} (currently cached: {cached_version or 'none'})...")
    try:
        download_and_extract(args.cache)
        write_cached_version(args.cache, latest)
    except Exception as e:
        log(args.cache, f"Could not download/extract ffmpeg: {e}")
        if cache_is_valid(args.cache):
            log(args.cache, "Falling back to the existing cached copy.")
            copy_from_cache(args.cache, args.target)
            return 0
        log(args.cache, "No usable cache to fall back to. FAILING -- tools\\ will not have ffmpeg.")
        return 1

    copy_from_cache(args.cache, args.target)
    log(args.cache, f"Done -- ffmpeg {latest} copied into {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
