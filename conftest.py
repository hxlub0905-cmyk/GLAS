"""Pytest bootstrap for GLAS.

The engine modules live in ``glas/core`` and the app modules in ``glas/app``.
They are imported flat (``import oasis_streamer``, ``import gds_fov``,
``import sem_loader`` ...), mirroring how they were laid out under MMH's
``tools/``. Putting both dirs on ``sys.path`` here lets the moved test files
import them unchanged — each test's own (now-stale) ``tools/`` path insert is
harmless because the modules already resolve via these entries.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
