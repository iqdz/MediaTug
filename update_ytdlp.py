"""Ensures yt-dlp's nightly build is installed and current.

Shared by run.bat (dev mode, installs into the local libs\\ folder) and
build.bat (build time, installs into whatever Python environment is
about to be frozen by PyInstaller) so both paths use one tested code
path instead of two separate, drifting copies of the same logic in
batch.

Behavior:
  - Looks up the newest version PyPI has published for the "yt-dlp"
    package, including pre-releases -- this is exactly what
    `pip install --pre -U yt-dlp` resolves to, since yt-dlp ships its
    nightly builds as pre-release versions of that same PyPI package.
  - Compares that against whatever is already installed.
  - If they already match, nothing is downloaded -- the existing,
    already-installed copy is used as-is (the "cached" copy).
  - Otherwise, installs/updates to that latest version.
  - If the version check itself fails (offline, PyPI unreachable),
    falls back to using whatever is already installed rather than
    failing outright -- a stale nightly still works fine offline.

Exit codes:
  0 -- yt-dlp is ready to use (cached copy already current, update
       installed cleanly, or falling back to an existing offline copy)
  1 -- yt-dlp is not installed and could not be installed (a real
       failure the caller should surface)
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request

# See fetch_ffmpeg.py's sibling comment: without this, a stalled (not
# refused, just stuck) connection during the PyPI check or the pip
# install below can hang forever with no output at all.
socket.setdefaulttimeout(30)
PIP_TIMEOUT_SECONDS = 600


def get_installed_version(target_dir):
    """Returns yt-dlp's __version__ visible from target_dir (a
    pip --target folder) if given, or from the current environment
    otherwise. None if yt-dlp isn't importable at all, or -- when
    target_dir is given -- if the only yt_dlp Python can find lives
    somewhere else entirely (e.g. a global pip install), since that's
    not the self-contained copy this script is responsible for keeping
    current."""
    if target_dir:
        sys.path.insert(0, target_dir)
    try:
        import yt_dlp
    except Exception:
        return None
    if target_dir:
        module_path = os.path.abspath(getattr(yt_dlp, "__file__", "") or "")
        target_abs = os.path.abspath(target_dir)
        if not module_path.startswith(target_abs + os.sep):
            return None
    return yt_dlp.version.__version__


def get_latest_available_version():
    """Newest version string PyPI has for yt-dlp, pre-releases
    included. yt-dlp's own version strings (YYYY.MM.DD[.REV]) sort
    correctly as plain strings, so a plain max() over all published
    versions gives the same answer pip's --pre -U resolver would."""
    url = "https://pypi.org/pypi/yt-dlp/json"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.load(resp)
    releases = data.get("releases", {})
    usable = [v for v, files in releases.items()
              if files and not all(f.get("yanked", False) for f in files)]
    if not usable:
        return None
    return max(usable)


def install(target_dir):
    # --target is an option of the "install" subcommand, not a global
    # pip flag -- it must come *after* "install" in the argument list.
    # A previous version of this inserted it before "install" instead
    # (python -m pip --target DIR install ...), which pip rejects
    # outright, so the install silently never actually happened; this
    # went unnoticed because get_installed_version() above can still
    # find a *different*, unrelated yt-dlp already on the system
    # (e.g. a globally pip-installed copy), making the whole run look
    # successful while dependencies\yt-dlp\ stayed empty.
    cmd = [sys.executable, "-m", "pip", "install", "--pre", "-U", "yt-dlp"]
    if target_dir:
        cmd[4:4] = ["--target", target_dir]
    try:
        return subprocess.run(cmd, timeout=PIP_TIMEOUT_SECONDS).returncode
    except subprocess.TimeoutExpired:
        print(f"pip install yt-dlp did not finish within {PIP_TIMEOUT_SECONDS}s -- giving up rather than hanging indefinitely.")
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", default=None,
        help="pip --target directory (e.g. the libs\\ folder). "
             "Omit to install into the current Python environment instead.")
    args = parser.parse_args()

    installed = get_installed_version(args.target)

    try:
        latest = get_latest_available_version()
    except Exception as e:
        print(f"Could not reach PyPI to check for the latest yt-dlp nightly ({e}).")
        if installed:
            print(f"Using already-installed yt-dlp {installed} (offline/cached copy).")
            return 0
        print("yt-dlp is not installed and it could not be downloaded either.")
        return 1

    if latest and installed == latest:
        print(f"yt-dlp {installed} is already the latest nightly -- using the cached copy, nothing to download.")
        return 0

    if latest:
        print(f"Installing yt-dlp nightly {latest} (currently: {installed or 'not installed'})...")
    else:
        print("Could not determine the latest available version; installing/upgrading yt-dlp anyway...")

    rc = install(args.target)
    if rc != 0 and not installed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
