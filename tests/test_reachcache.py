"""F27 M6: the reachable-bbox sidecar (reachcache) + reach_prewarm must persist
the per-cell reach map so a fresh reader loads it instead of re-sweeping, and the
loaded map must match a freshly-computed one (so pruning is unchanged).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app", "tests"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import reachcache                    # noqa: E402
import oasis_random as orx           # noqa: E402
import test_oasis_random as T        # noqa: E402


def test_reachcache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "f.oas"
    src.write_bytes(b"dummy-bytes-for-stat")
    memo = {0: (1.0, 2.0, 3.0, 4.0), 1: None, 5: (-10.0, -20.0, 30.0, 40.0)}
    assert reachcache.save(src, 0, memo) is True
    got = reachcache.load(src, 0)
    assert got == memo


def test_reachcache_stale_on_file_change(tmp_path, monkeypatch):
    monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "f.oas"
    src.write_bytes(b"aaaa")
    reachcache.save(src, 0, {0: (1.0, 2.0, 3.0, 4.0)})
    assert reachcache.load(src, 0) is not None
    src.write_bytes(b"bbbbbbbb")            # size changed -> stale
    assert reachcache.load(src, 0) is None


def test_reach_prewarm_computes_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
    p = tmp_path / "h.oas"
    p.write_bytes(T._build_hierarchy([(x * 40, y * 40) for y in range(4)
                                      for x in range(4)]))
    # 1st reader: compute + persist the reach map.
    r1 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    assert orx.reach_prewarm(r1, 0) is True
    computed = dict(r1._reach_memo)
    assert computed                       # non-empty (root + child at least)
    assert reachcache.load(p, 0) is not None

    # 2nd (fresh) reader: load-only must repopulate the SAME map without a sweep.
    r2 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    assert orx.reach_prewarm(r2, 0, compute=False) is True
    assert r2._reach_memo == computed

    # And a walk on the sidecar-loaded reader matches one that swept fresh.
    roi = (-50, -50, 4 * 40 + 50, 4 * 40 + 50)
    r3 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    a = orx.walk_roi(r3, 0, roi, 17, 0)
    b = orx.walk_roi_batched(r2, 0, roi, 17, 0)
    assert (sorted(map(tuple, a["rects"].tolist()))
            == sorted(map(tuple, b["rects"].tolist())))
