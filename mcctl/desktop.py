"""The user's desktop from mcctl: open a URL in their browser, put text on their clipboard (controller only).

WSL: Linux openers fail there (`gio: Operation not supported`, printed by xdg-open on every call), so the
Windows side is used: `wslview` (wslu) if installed, else `explorer.exe`, else `cmd.exe /c start`; the
clipboard is `clip.exe`. Elsewhere: `open`/`pbcopy` on macOS, `xdg-open` and `wl-copy`/`xclip`/`xsel` on
Linux, always with their output captured (an opener's complaints never land in the conversation), and Python's
`webbrowser` as the last resort. Every function reports failure as False: the caller prints the URL anyway.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

Which = Callable[[str], str | None]


def is_wsl(read: Callable[[], str] = lambda: Path("/proc/version").read_text()) -> bool:
    try:
        return "microsoft" in read().lower()
    except OSError:
        return False


def open_browser(
    url: str,
    *,
    wsl: bool | None = None,
    which: Which = shutil.which,
    run: Callable[..., Any] = subprocess.run,
    fallback: Callable[[str], Any] = webbrowser.open,
    platform: str = sys.platform,
) -> bool:
    """Open `url` in the user's browser; False when nothing could be started (the URL is printed anyway)."""
    if wsl if wsl is not None else is_wsl():
        for argv in (["wslview", url], ["explorer.exe", url], ["cmd.exe", "/c", "start", "", url]):
            if which(argv[0]):
                try:
                    run(argv, check=False, capture_output=True, timeout=30)  # explorer.exe exits 1 on success
                    return True
                except (OSError, subprocess.SubprocessError):
                    continue
        return False
    opener = "open" if platform == "darwin" else "xdg-open"
    if which(opener):
        try:
            # xdg-open may wait for the browser: started detached, its output discarded.
            subprocess.Popen(
                [opener, url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except OSError:
            pass
    try:
        return bool(fallback(url))
    except Exception:
        return False


def clipboard_argv(*, wsl: bool, platform: str, which: Which) -> Sequence[str] | None:
    """The command that reads stdin into the clipboard here, or None."""
    candidates: list[list[str]] = []
    if wsl:
        candidates.append(["clip.exe"])
    elif platform == "darwin":
        candidates.append(["pbcopy"])
    else:
        candidates += [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    return next((c for c in candidates if which(c[0])), None)


def copy(
    text: str,
    *,
    wsl: bool | None = None,
    which: Which = shutil.which,
    run: Callable[..., Any] = subprocess.run,
    platform: str = sys.platform,
) -> bool:
    """Put `text` on the clipboard (never a secret: public keys, commands). True if it worked."""
    argv = clipboard_argv(wsl=is_wsl() if wsl is None else wsl, platform=platform, which=which)
    if argv is None:
        return False
    try:
        proc = run(list(argv), input=text, text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(proc, "returncode", 1) == 0
