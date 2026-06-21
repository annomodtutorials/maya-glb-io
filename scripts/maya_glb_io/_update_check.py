"""Lightweight GitHub-release update check for Maya GLB I/O.

Pings the GitHub "latest release" API, compares the tag to the installed
version, and (only if a newer one exists) offers a download. Pure stdlib for
the network bit; all Maya UI runs on the main thread. Fails silently when
offline so it never disrupts a Maya session.

- Manual:  GLB I/O > Check for Updates...  -> always reports a result.
- Auto:    runs once on plug-in load, throttled to once per day, and stays
           silent unless a newer version is actually available.
"""
import json
import re
import threading
import time
import urllib.request

API_URL      = "https://api.github.com/repos/annomodtutorials/maya-glb-io/releases/latest"
RELEASES_URL = "https://github.com/annomodtutorials/maya-glb-io/releases/latest"

_OPTVAR_LAST   = "glbIO_lastUpdateCheck"   # epoch seconds of last successful check
_CHECK_INTERVAL = 24 * 3600                # auto-check at most once per day


def _current_version():
    try:
        from . import __version__
        return __version__
    except Exception:
        return "0.0.0"


def _parse_version(s):
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). Missing parts pad with 0."""
    nums = [int(n) for n in re.findall(r"\d+", s or "")][:3]
    return tuple(nums) + (0,) * (3 - len(nums))


def is_newer(latest, current):
    try:
        return _parse_version(latest) > _parse_version(current)
    except Exception:
        return False


def fetch_latest(timeout=5):
    """Return (tag, html_url) of the latest release, or None on any failure."""
    try:
        req = urllib.request.Request(API_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "maya-glb-io-update-check",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("tag_name") or "", data.get("html_url") or RELEASES_URL)
    except Exception:
        return None


def _open_url(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _on_main(tag, url, current, force, ok):
    """Runs on Maya's main thread (via executeDeferred). Safe to call cmds here."""
    from maya import cmds

    if ok:
        try:
            cmds.optionVar(fv=(_OPTVAR_LAST, time.time()))
        except Exception:
            pass

    if tag and is_newer(tag, current):
        msg = ("A new version of Maya GLB I/O is available.\n\n"
               "Installed:  v%s\nLatest:     %s\n\nDownload the update now?"
               % (current, tag))
        ans = cmds.confirmDialog(
            title="Maya GLB I/O - update available", message=msg,
            button=["Download", "Later"], defaultButton="Download",
            cancelButton="Later", dismissString="Later")
        if ans == "Download":
            _open_url(url or RELEASES_URL)
    elif force:
        if tag:
            cmds.confirmDialog(title="Maya GLB I/O",
                               message="You're up to date (v%s)." % current,
                               button=["OK"])
        else:
            cmds.confirmDialog(
                title="Maya GLB I/O",
                message=("Couldn't check for updates (no connection?).\n\n"
                         "Latest releases:\n" + RELEASES_URL),
                button=["OK"])


def _run(force):
    current = _current_version()
    res = fetch_latest()
    ok = res is not None
    tag, url = res if ok else ("", RELEASES_URL)
    try:
        import maya.utils
        maya.utils.executeDeferred(lambda: _on_main(tag, url, current, force, ok))
    except Exception:
        pass


def check(force=True):
    """Manual check (menu item). Network runs off the main thread."""
    threading.Thread(target=_run, args=(force,), daemon=True).start()


def auto_check():
    """Throttled silent check on plug-in load. Call on the main thread."""
    try:
        from maya import cmds
        last = cmds.optionVar(q=_OPTVAR_LAST) if cmds.optionVar(exists=_OPTVAR_LAST) else 0
        if last and (time.time() - float(last)) < _CHECK_INTERVAL:
            return
    except Exception:
        pass
    threading.Thread(target=_run, args=(False,), daemon=True).start()
