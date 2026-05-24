"""SVG icon helpers.

A handful of Lucide-style stroke icons rendered at 24x24 with a warm
grey-brown stroke (#6b5a4a) so they read clearly against both the
default rightPanel cream background and the active-tab amber tint.

Use ``qicon(name)`` to obtain a QIcon for any SVG in this directory.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PyQt6.QtGui import QIcon

_ICON_DIR = Path(__file__).parent


@lru_cache(maxsize=64)
def qicon(name: str) -> QIcon:
    """Return a QIcon for ``<name>.svg`` in this directory.

    Cached so repeatedly asking for the same icon (e.g. while building
    every rail tab) does not re-read the file from disk.
    """
    path = _ICON_DIR / f"{name}.svg"
    if not path.is_file():
        return QIcon()
    return QIcon(str(path))
