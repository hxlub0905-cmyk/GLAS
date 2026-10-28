"""Which build of GLAS is this (F32)?

The company machine gets GLAS by unpacking ``bundle/GLAS_bundle.py``; it has
no ``.git``, so "which version am I running" cannot be answered from git. The
bundle's header carries ``BUILD <sha12> <date>`` where ``sha12`` is the git
blob SHA of ``tools/FILELIST.txt`` -- the list of every file's content hash, so
it names the exact set of file contents. :func:`build_id` recomputes that same
number from the unpacked tree, and ``python main.py --version`` / the About
dialog print it, so an operator can read it off and a developer can match it
to a commit (``git log -S<sha12> -- tools/FILELIST.txt`` or the bundle header
in history).

Pure stdlib, no Qt (core rule).
"""
from __future__ import annotations

import hashlib
import os

#: Human version (also shown in the About dialog / status bar brand chip).
GLAS_VERSION = "1.0.0"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def build_id(root: str = "") -> str:
    """``tools/FILELIST.txt``'s git blob SHA, first 12 hex digits -- the same
    number the bundle header's ``BUILD`` line carries. ``"unknown"`` when the
    listing is not there (a checkout that never ran ``release.py``, or a tree
    unpacked without it)."""
    path = os.path.join(root or _repo_root(), "tools", "FILELIST.txt")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return "unknown"
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:12]


def version_line(root: str = "") -> str:
    """The one line ``python main.py --version`` prints."""
    return "GLAS %s (build %s)" % (GLAS_VERSION, build_id(root))
