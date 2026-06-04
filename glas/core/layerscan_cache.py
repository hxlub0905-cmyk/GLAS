"""Sidecar cache for F12 layer enumeration (``oasis_random.enumerate_layers``).

Enumerating layers on a no-LAYERNAME OASIS means a bounded cell sample, which
on a multi-GB file still costs a few seconds. Users have *many* such files, so
re-sampling on every app launch / "Scan layers" click is wasteful. This module
memoises the enumeration result in a tiny JSON sidecar keyed by the source
file's ``(absolute path, mtime, size)`` — a hit returns instantly; a changed
file (mtime or size differs) misses and re-scans.

The cache lives in a per-user cache directory (NOT next to the .oas, which is
often a read-only network share). It stores only the small layer list, never
geometry. Corrupt / unreadable cache entries are ignored (treated as a miss),
so the cache can never break a scan — worst case it re-samples.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1


def _cache_dir() -> Path:
    """Per-user cache dir for layer-scan sidecars. Honours XDG_CACHE_HOME /
    LOCALAPPDATA, falling back to ~/.cache then the system temp dir."""
    base = os.environ.get("XDG_CACHE_HOME")
    if not base and sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "glas" / "layerscan"
    home = Path.home()
    if str(home) not in ("", os.sep):
        return home / ".cache" / "glas" / "layerscan"
    return Path(tempfile.gettempdir()) / "glas" / "layerscan"


def _key_path(src: Path) -> Path:
    digest = hashlib.sha1(str(src.resolve()).encode("utf-8")).hexdigest()[:16]
    return _cache_dir() / f"{digest}.json"


def _stat(src: Path) -> tuple[float, int]:
    st = src.stat()
    return (st.st_mtime, st.st_size)


def _params_key(params: Optional[dict]) -> str:
    """Stable string fingerprint of the result-affecting scan params, so a scan
    with different settings (e.g. include_text toggled) doesn't return another
    setting's cached result."""
    return json.dumps(params or {}, sort_keys=True)


def load(path: str | Path, params: Optional[dict] = None) -> Optional[dict]:
    """Return the cached enumeration result for ``path`` if a fresh entry
    exists (same mtime + size + scan params), else ``None``. Never raises —
    any problem (missing, corrupt, stale, schema bump) is a miss."""
    src = Path(path)
    try:
        mtime, size = _stat(src)
        entry = json.loads(_key_path(src).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if entry.get("schema_version") != SCHEMA_VERSION:
        return None
    if entry.get("size") != size or abs(entry.get("mtime", -1) - mtime) > 1e-6:
        return None
    if entry.get("params") != _params_key(params):
        return None
    result = entry.get("result")
    if not isinstance(result, dict) or "layers" not in result:
        return None
    return result


def save(path: str | Path, result: dict, params: Optional[dict] = None) -> bool:
    """Write ``result`` (an ``enumerate_layers`` dict) to the sidecar for
    ``path``. Returns True on success. Best-effort: a write failure (e.g. an
    unwritable cache dir) is swallowed and returns False, so caching never
    blocks a scan."""
    src = Path(path)
    try:
        mtime, size = _stat(src)
        kp = _key_path(src)
        kp.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "schema_version": SCHEMA_VERSION,
            "source": str(src.resolve()),
            "mtime": mtime,
            "size": size,
            "params": _params_key(params),
            "result": result,
        }
        # Atomic-ish write: temp file in the same dir, then replace.
        fd, tmp = tempfile.mkstemp(dir=str(kp.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh)
            os.replace(tmp, kp)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError:
        return False
