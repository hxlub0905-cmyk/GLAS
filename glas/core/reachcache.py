"""F27 M6: persistent sidecar for the per-cell reachable-bbox map.

The ROI walk prunes subtrees by ``reachable_bbox(cid)`` — the bbox (cid-local
frame) of all geometry reachable from a cell. On a file with no name-table
S_BOUNDING_BOX (E3B), the FIRST walk of a session computes this for EVERY
reachable cell (a ~13k-cell ``load_cell_bbox`` sweep, ~20-30 s), memoized on the
reader. Every export worker repeats that sweep, and every re-run of the same
file repeats it again.

This sidecar persists the whole ``reach_memo`` once (keyed on file identity +
root) so a later worker / session loads it in ms instead of re-sweeping — the
same win ``cellcache`` gives for decoded geometry, for the reachable-bbox map.

Same discipline as ``cellcache`` / ``walkflatten_cache``: mtime+size
validation, atomic write, corruption/schema-mismatch = miss, never raises. The
cache dir is shared (``GLAS_CELLCACHE_DIR`` / XDG / LOCALAPPDATA).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import numpy as np

import cellcache   # reuse the cache-dir resolution

_SCHEMA = 1


def _key_path(src: Path, root: object) -> Path:
    digest = hashlib.sha1(str(Path(src).resolve()).encode("utf-8")).hexdigest()[:16]
    r = hashlib.sha1(repr(root).encode("utf-8")).hexdigest()[:8]
    return cellcache._cache_dir() / f"reach{_SCHEMA}__{digest}__{r}.npz"


def _stat(src: Path):
    st = os.stat(src)
    return st.st_mtime_ns, st.st_size


def load(src, root) -> "dict | None":
    """Return ``{cell_id: (x0,y0,x1,y1) | None}`` or ``None`` (miss/stale)."""
    src = Path(src)
    p = _key_path(src, root)
    if not p.exists():
        return None
    try:
        mt, sz = _stat(src)
        with np.load(p, allow_pickle=False) as z:
            if int(z["mtime"]) != mt or int(z["size"]) != sz:
                return None
            ids = z["ids"]
            bb = z["bbox"]                       # (N,4) f64, NaN row => None
            out: dict = {}
            for i in range(ids.shape[0]):
                row = bb[i]
                out[int(ids[i])] = (None if np.isnan(row[0])
                                    else (float(row[0]), float(row[1]),
                                          float(row[2]), float(row[3])))
            return out
    except Exception:            # noqa: BLE001 — a bad cache is just a miss
        return None


def save(src, root, reach_memo: dict) -> bool:
    """Persist ``reach_memo`` (``{int cell_id: bbox | None}``). Atomic; never
    raises (returns False). Only integer cell keys are stored."""
    src = Path(src)
    p = _key_path(src, root)
    try:
        items = [(int(k), v) for k, v in reach_memo.items()
                 if isinstance(k, (int, np.integer))]
        ids = np.array([k for k, _ in items], dtype=np.int64)
        bb = np.full((len(items), 4), np.nan, dtype=np.float64)
        for i, (_k, v) in enumerate(items):
            if v is not None:
                bb[i, 0] = v[0]; bb[i, 1] = v[1]; bb[i, 2] = v[2]; bb[i, 3] = v[3]
        mt, sz = _stat(src)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".npz", dir=str(p.parent))
        os.close(fd)
        tmp_npz = tmp if tmp.endswith(".npz") else tmp + ".npz"
        np.savez(tmp if os.path.exists(tmp) else tmp_npz[:-4],
                 mtime=np.int64(mt), size=np.int64(sz), ids=ids, bbox=bb)
        os.replace(tmp_npz if os.path.exists(tmp_npz) else tmp, p)
        return True
    except Exception:            # noqa: BLE001
        return False
