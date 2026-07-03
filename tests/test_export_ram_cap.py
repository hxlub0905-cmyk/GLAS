"""F27 M7m: RAM-aware export worker cap. A giant merge cell is np.load'd in full
by every export pool worker; on a tight-RAM tool N copies at once thrash and the
first wave runs ~15x slower. The worker count is capped to what the machine's
free RAM (auto-detected per tool) actually holds, so it self-tunes across
different tools with no hard-coded size. These guard the cap math, the RAM probe,
the sidecar-size proxy and the find_giant_cells refactor.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app", "tests"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cellcache                      # noqa: E402
import fine_align                     # noqa: E402
import oasis_random as orx            # noqa: E402
import test_oasis_random as T         # noqa: E402


# ── ram_capped_worker_count ──────────────────────────────────────────────────

def test_cap_reduces_when_ram_tight():
    # 300 MB giant, 2x factor -> 600 MB/worker; 4 GB * 0.7 = 2.8 GB budget -> 4.
    assert fine_align.ram_capped_worker_count(
        8, 300_000_000, 4_000_000_000, factor=2.0, fraction=0.7) == 4


def test_no_cap_when_ram_roomy():
    # 32 GB free easily holds 8 x 600 MB -> keep all 8.
    assert fine_align.ram_capped_worker_count(
        8, 300_000_000, 32_000_000_000, factor=2.0, fraction=0.7) == 8


def test_passthrough_when_no_giant():
    assert fine_align.ram_capped_worker_count(8, 0, 4_000_000_000) == 8


def test_passthrough_when_ram_unknown():
    assert fine_align.ram_capped_worker_count(8, 300_000_000, None) == 8


def test_never_below_one():
    assert fine_align.ram_capped_worker_count(
        8, 10_000_000_000, 1_000_000_000, factor=2.0, fraction=0.7) == 1


def test_single_worker_stays_one():
    assert fine_align.ram_capped_worker_count(1, 300_000_000, 100_000_000) == 1


def test_env_overrides_factor_and_fraction(monkeypatch):
    monkeypatch.setenv("GLAS_EXPORT_WORKER_MEM_FACTOR", "2.0")
    monkeypatch.setenv("GLAS_EXPORT_RAM_FRACTION", "0.7")
    assert fine_align.ram_capped_worker_count(8, 300_000_000, 4_000_000_000) == 4


# ── available_ram_bytes ──────────────────────────────────────────────────────

def test_available_ram_is_positive_int_or_none():
    v = fine_align.available_ram_bytes()
    assert v is None or (isinstance(v, int) and v > 0)


# ── cellcache.cached_size (RAM proxy) ────────────────────────────────────────

def test_cached_size_zero_when_missing(tmp_path):
    p = tmp_path / "x.oas"
    p.write_bytes(b"OASIS")
    assert cellcache.cached_size(p, 12345, {(17, 0)}) == 0


def test_cached_size_positive_after_save(tmp_path, monkeypatch):
    monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
    p = tmp_path / "h.oas"
    p.write_bytes(T._build_hierarchy([(0, 0)]))
    r = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    content = r.load_cell(0)
    assert cellcache.save(p, 0, {(17, 0)}, content)
    assert cellcache.cached_size(p, 0, {(17, 0)}) > 0


# ── find_giant_cells refactor + giant_cell_estimate_bytes ────────────────────

def test_find_giant_cells_matches_spans_helper(tmp_path):
    p = tmp_path / "h.oas"
    p.write_bytes(T._build_hierarchy([(x * 40, y * 40)
                                      for y in range(2) for x in range(2)]))
    r = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    ids = r.find_giant_cells(min_bytes=0)            # low bar: every cell qualifies
    pairs = r._giant_cells_with_spans(min_bytes=0)
    assert ids == [cid for cid, _span in pairs]
    assert pairs and all(isinstance(s, int) and s >= 0 for _cid, s in pairs)
    # largest-first
    assert [s for _cid, s in pairs] == sorted((s for _cid, s in pairs), reverse=True)


def test_giant_estimate_zero_when_no_giant(tmp_path):
    p = tmp_path / "h.oas"
    p.write_bytes(T._build_hierarchy([(0, 0)]))
    r = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    # a tiny fixture has no 20 MB+ span -> no giant -> 0 (no cap).
    assert r.giant_cell_estimate_bytes() == 0
