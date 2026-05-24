"""GLAS entry point — launch the GDS-Layout Alignment for SEM app.

Run:  python main.py

Puts glas/core (no-Qt engine) and glas/app (PyQt6 app) on sys.path, then hands
off to the app's main(). gds_align_tool also performs the same path setup on
import, so spawned subprocesses (multiprocessing) resolve the engine modules
without depending on this launcher.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def main() -> int:
    import gds_align_tool
    return gds_align_tool.main()


if __name__ == "__main__":
    # multiprocessing guard (the ROI loader spawns workers; required on Windows).
    import multiprocessing as _mp
    _mp.freeze_support()
    raise SystemExit(main())
