"""Sidecar cache for decoded large OASIS cells (F16-B).

Some production OASIS files contain a single chip-spanning *flat* merge cell
(e.g. a Calibre D2DB output) holding tens of millions of records. Decoding it to
view any ROI costs minutes and must happen on the first ROI load of every app
session, even though the result is deterministic. This module persists the
decoded :class:`oasis_random.CellContent` to a per-user sidecar so that the
second-and-later session loads it from disk in seconds instead of re-decoding.

Design (mirrors ``layerscan_cache``):
* one ``.npz`` per ``(source file, cell id, wanted layers)``, in a per-user
  cache dir (NOT next to the .oas, which is often a read-only network share);
* keyed/validated by the source file's ``(mtime, size)`` + a schema version, so
  a changed file or a format bump misses and re-decodes;
* corrupt / unreadable / version-mismatched entries are treated as a miss, so
  the cache can never break a load — worst case it re-decodes.

Only "big" cells are worth caching (small cells decode in microseconds); the
caller decides via a record-count threshold. The columnar (de)serialisation
itself lives on ``CellContent.to_cache_arrays`` / ``from_cache_arrays``; this
module only does keying, atomic file I/O and validation.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

SCHEMA_VERSION = 1


def enabled() -> bool:
    """The cache is on unless ``GLAS_CELLCACHE=0`` (or false/no/off)."""
    return os.environ.get("GLAS_CELLCACHE", "1").lower() not in (
        "0", "false", "no", "off")


def min_records() -> int:
    """Cache only cells with at least this many records (placements + rects +
    polys). Override via ``GLAS_CELLCACHE_MIN_RECORDS``; default 100k."""
    try:
        return int(os.environ.get("GLAS_CELLCACHE_MIN_RECORDS", "100000"))
    except ValueError:
        return 100000


def _cache_dir() -> Path:
    base = os.environ.get("GLAS_CELLCACHE_DIR")
    if base:
        return Path(base)
    base = os.environ.get("XDG_CACHE_HOME")
    if not base and sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "glas" / "celldecode"
    home = Path.home()
    if str(home) not in ("", os.sep):
        return home / ".cache" / "glas" / "celldecode"
    return Path(tempfile.gettempdir()) / "glas" / "celldecode"


def _layers_tag(wanted_layers) -> str:
    if not wanted_layers:
        return "all"
    items = sorted((int(l), int(d)) for (l, d) in wanted_layers)
    return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()[:12]


def _key_path(src: Path, cell_id, wanted_layers) -> Path:
    digest = hashlib.sha1(str(src.resolve()).encode("utf-8")).hexdigest()[:16]
    cell = hashlib.sha1(repr(cell_id).encode("utf-8")).hexdigest()[:12]
    return _cache_dir() / f"{digest}__{cell}__{_layers_tag(wanted_layers)}.npz"


def _stat(src: Path) -> tuple[float, int]:
    st = src.stat()
    return (st.st_mtime, st.st_size)


def load(src, cell_id, wanted_layers):
    """Return a cached ``CellContent`` for ``(src, cell_id, wanted_layers)`` if a
    fresh, valid entry exists, else ``None``. Never raises — any problem is a
    miss."""
    if not enabled():
        return None
    try:
        src = Path(src)
        p = _key_path(src, cell_id, wanted_layers)
        if not p.exists():
            return None
        mtime, size = _stat(src)
        with np.load(p, allow_pickle=True) as z:
            if int(z["__schema__"]) != SCHEMA_VERSION:
                return None
            if int(z["__size__"]) != size or float(z["__mtime__"]) != mtime:
                return None
            arrays = {k: z[k] for k in z.files}
    except Exception:
        return None
    try:
        import oasis_random as orx
        return orx.CellContent.from_cache_arrays(arrays)
    except Exception:
        return None


def _fresh_entry(p: Path, mtime: float, size: int) -> bool:
    """True if ``p`` is an existing, current cache entry (schema + source
    mtime/size match). Reads only the tiny meta arrays, not the geometry."""
    try:
        if not p.exists():
            return False
        with np.load(p, allow_pickle=True) as z:
            return (int(z["__schema__"]) == SCHEMA_VERSION
                    and int(z["__size__"]) == size
                    and float(z["__mtime__"]) == mtime)
    except Exception:
        return False


def save(src, cell_id, wanted_layers, content) -> bool:
    """Serialise ``content`` to the sidecar. Returns True on success; never
    raises (a cache write must not break a decode)."""
    if not enabled():
        return False
    tmp = None
    try:
        src = Path(src)
        mtime, size = _stat(src)
        p = _key_path(src, cell_id, wanted_layers)
        # A concurrent batch worker (each process decodes independently) may
        # already have written this entry — skip the redundant ~hundreds-MB
        # serialise+write rather than racing to rewrite it.
        if _fresh_entry(p, mtime, size):
            return True
        arrays = content.to_cache_arrays()
        arrays["__schema__"] = np.int64(SCHEMA_VERSION)
        arrays["__size__"] = np.int64(size)
        arrays["__mtime__"] = np.float64(mtime)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".npz", dir=str(p.parent))
        os.close(fd)
        np.savez(tmp, **arrays)
        # np.savez appends .npz to the path if missing
        tmp_npz = tmp if os.path.exists(tmp) else tmp + ".npz"
        os.replace(tmp_npz, p)
        return True
    except Exception:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False
