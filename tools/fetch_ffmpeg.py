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
  - The archive is kept in the cache until both exes are out, so a
    failed extraction is retried on the next run without downloading
    it again. py7zr is tried first, Windows' own tar.exe second, and
    each one's real error is written to fetch.log.
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


ARCHIVE_PREFIX = "ffmpeg-release-full-"


def remove_archives(cache_dir, keep=()):
    """Deletes archives kept in the cache, except the paths in `keep`."""
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return
    for name in names:
        full = os.path.join(cache_dir, name)
        if name.startswith(ARCHIVE_PREFIX) and full not in keep:
            try:
                os.remove(full)
                log(cache_dir, f"Removed old archive {name}.")
            except OSError:
                pass


def _published_sha256():
    with urllib.request.urlopen(SHA256_URL, timeout=15) as resp:
        return resp.read().decode("ascii").split()[0].lower()


def _find_exes(extract_dir):
    found = {}
    for root, _dirs, files in os.walk(extract_dir):
        for name in files:
            low = name.lower()
            if low in EXE_NAMES and low not in found:
                found[low] = os.path.join(root, name)
    return found


def _extract_with_py7zr(archive_path, extract_dir, cache_dir):
    """First extractor: py7zr, extracting only ffmpeg.exe and ffprobe.exe.
    Right after a download Windows sometimes keeps the new file locked for
    a moment (antivirus scanning it), so a lock is retried briefly."""
    ensure_py7zr(cache_dir)
    import py7zr
    last_error = None
    for attempt in range(10):
        try:
            with py7zr.SevenZipFile(archive_path, mode="r") as archive:
                wanted = [n for n in archive.getnames()
                          if os.path.basename(n).lower() in EXE_NAMES]
                if not wanted:
                    raise RuntimeError("the archive lists no ffmpeg.exe or ffprobe.exe")
                archive.extract(path=extract_dir, targets=wanted)
            return
        except PermissionError as e:
            last_error = e
            log(cache_dir, f"  Archive is still locked (attempt {attempt + 1}/10: {e}) -- "
                            f"probably antivirus scanning it briefly. Retrying in 2s...")
            time.sleep(2)
    raise RuntimeError(f"archive still locked after repeated retries: {last_error}")


def _extract_with_windows_tar(archive_path, extract_dir, cache_dir):
    """Second extractor: Windows' own tar.exe, which reads .7z as well. The
    System32 copy is used by full path, since another tar.exe on PATH (for
    example Git's) may not read .7z at all."""
    tar = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
    if not os.path.isfile(tar):
        raise RuntimeError("Windows tar.exe not found")
    os.makedirs(extract_dir, exist_ok=True)
    cmd = [tar, "-xf", archive_path, "-C", extract_dir,
           "--include", "*ffmpeg.exe", "--include", "*ffprobe.exe"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise RuntimeError(f"tar exit {result.returncode}: {detail}")


def download_and_extract(cache_dir, version):
    """Gets the archive for `version` into the cache, reusing one kept from
    an earlier run, extracts only ffmpeg.exe and ffprobe.exe into the cache,
    then deletes the archive. On any failure the archive stays in the cache,
    so the next run retries the extraction without downloading it again."""
    os.makedirs(cache_dir, exist_ok=True)
    archive_path = os.path.join(cache_dir, f"{ARCHIVE_PREFIX}{version}.7z")
    part_path = archive_path + ".part"
    remove_archives(cache_dir, keep=(archive_path,))

    try:
        expected = _published_sha256()
    except Exception as e:
        expected = None
        log(cache_dir, f"Warning: could not read the published checksum ({e}).")

    if os.path.isfile(archive_path):
        if expected and hash_file(archive_path) != expected:
            log(cache_dir, "The kept archive does not match the published checksum -- downloading again.")
            os.remove(archive_path)
        else:
            log(cache_dir, f"Reusing the archive kept from an earlier run: {os.path.basename(archive_path)}")

    if not os.path.isfile(archive_path):
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
            urllib.request.urlretrieve(ARCHIVE_URL, part_path, reporthook=report_progress)
        except Exception as e:
            raise RuntimeError(f"download from {ARCHIVE_URL} failed: {e}")
        downloaded_size = os.path.getsize(part_path)
        log(cache_dir, f"Downloaded {downloaded_size:,} bytes.")
        if downloaded_size < 1024 * 1024:
            # A real "full" ffmpeg 7z is well over 100 MB. Anything under
            # 1 MB is almost certainly an HTML error page (a proxy's block
            # page, a redirected 404) saved with the .7z name.
            with open(part_path, "r", encoding="utf-8", errors="replace") as f:
                preview = f.read(500)
            os.remove(part_path)
            log(cache_dir, f"Downloaded file is suspiciously small ({downloaded_size} bytes) -- "
                            f"this is probably not a real archive. First 500 chars: {preview!r}")
            raise RuntimeError("downloaded file is too small to be a real ffmpeg archive "
                                "(likely blocked/redirected by a firewall or proxy -- see fetch.log)")
        if expected:
            actual = hash_file(part_path)
            if actual != expected:
                os.remove(part_path)
                raise RuntimeError(f"checksum mismatch (expected {expected}, got {actual})")
            log(cache_dir, "Checksum verified OK.")
        os.replace(part_path, archive_path)

    tmp = tempfile.mkdtemp(prefix="mt_ffmpeg_")
    try:
        extract_dir = os.path.join(tmp, "extracted")
        log(cache_dir, "Extracting ffmpeg.exe and ffprobe.exe...")
        errors = []
        found = {}
        for extractor in (_extract_with_py7zr, _extract_with_windows_tar):
            try:
                extractor(archive_path, extract_dir, cache_dir)
                found = _find_exes(extract_dir)
                if all(n in found for n in EXE_NAMES):
                    log(cache_dir, f"  Extracted with {extractor.__name__}.")
                    break
                errors.append(f"{extractor.__name__}: finished, but ffmpeg.exe or ffprobe.exe is missing")
            except Exception as e:
                errors.append(f"{extractor.__name__}: {type(e).__name__}: {e}")
            log(cache_dir, f"  {errors[-1]}")
            found = {}
            shutil.rmtree(extract_dir, ignore_errors=True)
        if not found:
            raise RuntimeError("could not extract the archive; it is kept for the next run. "
                               + " | ".join(errors))
        for name, path in found.items():
            shutil.copy2(path, os.path.join(cache_dir, name))
        log(cache_dir, f"Copied {', '.join(sorted(found))} into the cache.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    remove_archives(cache_dir)


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
        remove_archives(args.cache)
        copy_from_cache(args.cache, args.target)
        log(args.cache, f"Done -- ffmpeg {latest} copied into {args.target}")
        return 0

    log(args.cache, f"Fetching ffmpeg {latest} (currently cached: {cached_version or 'none'})...")
    try:
        download_and_extract(args.cache, latest)
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
