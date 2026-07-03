"""F27 M3c: persistent sidecar for the native-walk flattened cell graph.

``oasis_random.flatten_cell_graph`` materializes the whole ROI-independent cell
graph reachable from a root into CSR arrays for ``walk_rects_native``. On a big
production chip that's a multi-second whole-chip decode + array build — far too
slow to redo per ROI, per worker, per session. This sidecar persists the CSR
once (keyed on file identity + root + layer) so:

* every worker in a batch shares ONE build (via the OS page cache when they
  ``np.load`` the same file), instead of each decoding the whole chip;
* a later session skips the build entirely.

Same discipline as ``cellcache`` (F16-B): mtime+size validation, atomic write,
corruption/schema-mismatch treated as a miss, never raises. The cache dir is
shared with ``cellcache`` (``GLAS_CELLCACHE_DIR`` / XDG / LOCALAPPDATA).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import numpy as np

import cellcache   # reuse the cache-dir resolution

# Schema 3 (F27 M3e): the CSR gained grid descriptors (analytic repetition clip)
# + polygon arrays, so its field set changed — bump to invalidate v2 sidecars.
_SCHEMA = 3

# The named arrays of the flat CSR tuple, in order. Kept as a single list so
# load()/save() can't drift apart when the CSR layout changes.
_CSR_FIELDS = ("rect", "rect_off", "poly_pts", "poly_ptoff", "poly_meta",
               "poly_off", "pl_target", "pl_data", "pl_off", "reach_bbox")

# A distinct sentinel from "miss": the graph was flattened and found NOT
# native-able (non-D4 / name-ref / a non-clippable array over the cap) — persist
# that verdict (with the reason) so callers don't re-walk a big chip to bail.
NOT_NATIVE = "not-native"

# The reason string from the most recent load() that returned NOT_NATIVE
# (diagnostic; e.g. "placement repetition (cell 42)").
last_not_native_reason = ""


def _key_path(src: Path, root: object, layer: int, datatype: int) -> Path:
    digest = hashlib.sha1(str(Path(src).resolve()).encode("utf-8")).hexdigest()[:16]
    r = hashlib.sha1(repr(root).encode("utf-8")).hexdigest()[:8]
    return (cellcache._cache_dir()
            / f"wf{_SCHEMA}__{digest}__{r}__{layer}_{datatype}.npz")


def _stat(src: Path):
    st = os.stat(src)
    return st.st_mtime_ns, st.st_size


def load(src, root, layer: int, datatype: int):
    """Return the flat CSR tuple, ``NOT_NATIVE`` (persisted non-native verdict),
    or ``None`` (miss / stale / corrupt — caller should build)."""
    src = Path(src)
    p = _key_path(src, root, layer, datatype)
    if not p.exists():
        return None
    try:
        mt, sz = _stat(src)
        with np.load(p, allow_pickle=False) as z:
            if int(z["mtime"]) != mt or int(z["size"]) != sz:
                return None
            if not bool(z["native_able"]):
                global last_not_native_reason
                last_not_native_reason = (str(z["reason"])
                                          if "reason" in z.files else "")
                return NOT_NATIVE
            return tuple([z[f] for f in _CSR_FIELDS] + [int(z["root_index"])])
    except Exception:            # noqa: BLE001 — a bad cache is just a miss
        return None


def save(src, root, layer: int, datatype: int, flat, reason: str = "") -> bool:
    """Persist ``flat`` (the CSR tuple, or ``None`` for a non-native verdict
    with an optional ``reason`` string). Atomic; never raises (returns False)."""
    src = Path(src)
    p = _key_path(src, root, layer, datatype)
    try:
        mt, sz = _stat(src)
        p.parent.mkdir(parents=True, exist_ok=True)
        fields = {"mtime": np.int64(mt), "size": np.int64(sz)}
        if flat is None:
            fields["native_able"] = np.bool_(False)
            fields["reason"] = np.array(reason or "")
        else:
            fields["native_able"] = np.bool_(True)
            for name, arr in zip(_CSR_FIELDS, flat[:len(_CSR_FIELDS)]):
                fields[name] = arr
            fields["root_index"] = np.int64(flat[-1])
        fd, tmp = tempfile.mkstemp(suffix=".npz", dir=str(p.parent))
        os.close(fd)
        tmp_npz = tmp if tmp.endswith(".npz") else tmp + ".npz"
        np.savez(tmp if os.path.exists(tmp) else tmp_npz[:-4], **fields)
        os.replace(tmp_npz if os.path.exists(tmp_npz) else tmp, p)
        return True
    except Exception:            # noqa: BLE001
        return False
