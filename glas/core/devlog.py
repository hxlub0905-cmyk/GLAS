"""Tiny ANSI-colour helper for GLAS's dev-mode diagnostic prints (Qt-free).

Dev mode streams several tagged lines to the console — ``[roi]`` reader/load
telemetry, ``[fa-timing]`` per-stage batch timing, ``[jump]`` coordinate maths,
``[gds-align]`` mode banners. Colouring the tags by category makes that stream
scannable instead of a wall of grey text.

Degrades safely: emits escape codes only when the target stream is a
colour-capable console. Detection covers
  * ``NO_COLOR`` / ``GLAS_NO_COLOR`` env → always plain (user opt-out),
  * PyCharm's run console (``PYCHARM_HOSTED=1``) → colour (it renders ANSI even
    though the stream isn't a TTY),
  * a real TTY (Windows Terminal, PowerShell, *nix) → colour, enabling Windows
    virtual-terminal processing once so the codes render rather than print raw,
  * anything else (piped to a file, dumb terminal) → plain, so no ``\\033[..``
    ever leaks into a redirected log.

Kept dependency-free (stdlib only) and Qt-free so the spawn-pool workers — which
emit the ``[fa-timing]`` lines — can import it just like any other core module.
"""
from __future__ import annotations

import os
import sys

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

# Per-category SGR colour for the bracketed tag. Unknown categories fall back to
# bold so they still stand out.
_COLORS = {
    "roi": "\033[36m",        # cyan  — ROI reader / load telemetry
    "fa-timing": "\033[35m",  # magenta — batch per-stage timing
    "jump": "\033[33m",       # yellow — coordinate maths on image select
    "gds-align": "\033[32m",  # green  — debug / trace mode banners
}


def _enable_windows_vt() -> None:
    """Turn on ENABLE_VIRTUAL_TERMINAL_PROCESSING for the std handles so ANSI
    codes render on Windows 10+ consoles instead of printing literally. No-op
    off Windows or when the console rejects it."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for std in (-11, -12):                 # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:                          # pragma: no cover - platform dep
        pass


_enable_windows_vt()


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("GLAS_NO_COLOR"):
        return False
    if os.environ.get("PYCHARM_HOSTED"):       # PyCharm console renders ANSI
        return True
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def paint(text: str, color: str, *, stream=None, bold: bool = False) -> str:
    """Wrap ``text`` in an SGR ``color`` (an escape string or a key of
    ``_COLORS``) when ``stream`` supports colour, else return it unchanged."""
    stream = stream if stream is not None else sys.stdout
    if not _supports_color(stream):
        return text
    code = _COLORS.get(color, color)
    if bold:
        code = _BOLD + code
    return f"{code}{text}{_RESET}"


def tag(category: str, *, stream=None) -> str:
    """A ``[category]`` label coloured by category (plain when unsupported).

    Use as the prefix of a dev-mode print, e.g.
    ``print(f"{devlog.tag('roi')} loaded in 1.2s")``."""
    label = f"[{category}]"
    stream = stream if stream is not None else sys.stdout
    if not _supports_color(stream):
        return label
    return f"{_COLORS.get(category, _BOLD)}{label}{_RESET}"


def dim(text: str, *, stream=None) -> str:
    """Faint ``text`` (for low-salience detail like pid / counts)."""
    stream = stream if stream is not None else sys.stdout
    return f"{_DIM}{text}{_RESET}" if _supports_color(stream) else text
