"""Tests for the decoded-cell sidecar cache (F16-B M3/M4).

Round-trip equivalence of the columnar (de)serialisation and the on-disk
load/save, including walk_roi bit-identity between a freshly decoded cell and
one rebuilt from the cache, plus invalidation on file change.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import oasis_random as orx        # noqa: E402
import cellcache                  # noqa: E402

# Reuse the byte-builders from the random-access tests.
import test_oasis_random as tr    # noqa: E402


def _roundtrip(content):
    return orx.CellContent.from_cache_arrays(content.to_cache_arrays())


class TestColumnarRoundTrip:

    def _specs_equal(self, a, b):
        # rect spec tuples
        for key in set(list(a.rect_keys()) + list(b.rect_keys())):
            assert a.rect_count(key) == b.rect_count(key)
            for i in range(a.rect_count(key)):
                assert a.rect_spec_at(key, i) == b.rect_spec_at(key, i)
        for key in set(list(a.poly_keys()) + list(b.poly_keys())):
            assert a.poly_count(key) == b.poly_count(key)
            for i in range(a.poly_count(key)):
                pa, ra, rra = a.poly_spec_at(key, i)
                pb, rb, rrb = b.poly_spec_at(key, i)
                assert np.array_equal(pa, pb)
                assert ra == rb and rra == rrb
        assert a.placements == b.placements
        assert a.bbox == b.bbox

    def test_two_cell_fixture(self, tmp_path):
        data, _, _ = tr._build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        for cid in (0, 1):
            c = rar.load_cell(cid)
            self._specs_equal(c, _roundtrip(c))

    def test_all_repetition_types(self, tmp_path):
        # A cell whose rects/polys/placements exercise every repetition type
        # plus None, name-target placement, and a multi-point polygon.
        c = orx.CellContent()
        c.rect_specs[(17, 0)] = [
            (0, 0, 10, 10, None, None),
            (0, 0, 10, 10, 1, (3, 4, 100, 200)),
            (0, 0, 10, 10, 2, (5, 50)),
            (0, 0, 10, 10, 3, (5, 50)),
            (0, 0, 10, 10, 8, (3, 3, (40, 0), (0, 40))),
            (0, 0, 10, 10, 10, ([(1, 2), (3, 4)],)),
            (0, 0, 10, 10, 11, (2, [(1, 1), (2, 2)])),
        ]
        c.rect_specs[(20, 5)] = [(1, 2, 3, 4, None, None)]
        c.poly_specs[(6, 0)] = [
            (np.array([[0, 0], [10, 0], [10, 10], [0, 10]], dtype=np.int64),
             None, None),
            (np.array([[0, 0], [5, 0], [5, 5]], dtype=np.int64),
             2, (4, 25)),
        ]
        c.placements = [
            orx.Placement(1, "refnum", 100, 200, 90.0, 1.0, True, None, [], None),
            orx.Placement("CHILD", "name", -5, 7, 0.0, 2.0, False, 2, [], (3, 10)),
        ]
        c.bbox = (0, 0, 999, 999)
        self._specs_equal(c, _roundtrip(c))

    def test_placements_lazy_from_cache(self):
        # F18: a cache-loaded cell keeps placements as SoA; the Placement list is
        # not rebuilt until something iterates it. placement_count/placement_at
        # work from SoA, and a name-target round-trips via the sparse side-table.
        c = orx.CellContent()
        c.placements = [
            orx.Placement(7, "refnum", 1, 2, 0.0, 1.0, False, None, [], None),
            orx.Placement("CH", "name", 3, 4, 90.0, 2.0, True, 2, [], (3, 10)),
        ]
        rt = _roundtrip(c)
        assert rt._placements is None and rt._pl_soa is not None   # lazy/SoA
        assert rt.placement_count() == 2
        assert rt.placement_at(0) == c.placements[0]
        assert rt.placement_at(1) == c.placements[1]               # name-target
        assert rt._placements is None              # placement_at didn't build list
        assert rt.placements == c.placements       # property builds + correct
        assert rt._placements is not None          # now materialised

    def test_empty_cell(self, tmp_path):
        c = orx.CellContent()
        rt = _roundtrip(c)
        assert rt.is_empty()
        assert rt.bbox is None

    def test_arrays_match_columnar_vs_tuple(self):
        # rect_arrays / poly_arrays must give identical (base, extent) whether
        # built from the tuple specs or the cache-loaded columnar form (the
        # latter uses the vectorized base + reduceat path). Polygons have
        # varying point counts to exercise the CSR reduceat.
        c = orx.CellContent()
        c.rect_specs[(17, 0)] = [
            (0, 0, 10, 10, None, None),
            (5, 5, 9, 8, 1, (4, 3, 100, 200)),     # repeated
            (-3, -2, 7, 1, None, None),
        ]
        c.poly_specs[(6, 0)] = [
            (np.array([[0, 0], [10, 0], [10, 10], [0, 10]], dtype=np.int64),
             None, None),
            (np.array([[1, 1], [4, 1], [4, 4]], dtype=np.int64), 2, (5, 50)),
            (np.array([[-2, -2], [2, -2], [2, 2], [-2, 2], [0, 4]],
                      dtype=np.int64), None, None),
        ]
        rt = _roundtrip(c)
        for key in [(17, 0)]:
            b0, e0 = c.rect_arrays(key)
            b1, e1 = rt.rect_arrays(key)
            assert np.array_equal(b0, b1) and np.array_equal(e0, e1)
        for key in [(6, 0)]:
            b0, e0 = c.poly_arrays(key)
            b1, e1 = rt.poly_arrays(key)
            assert np.array_equal(b0, b1) and np.array_equal(e0, e1)


class TestWalkEquivalence:
    """walk_roi over a cache-rebuilt cell must be bit-identical to the decode."""

    def test_hierarchy_walk_identical(self, tmp_path):
        # A rect with a big repetition + nested placements; walk a tiny ROI on
        # both the decoded reader and one whose root cell was round-tripped.
        p = tmp_path / "h.oas"
        p.write_bytes(tr._build_big_grid(1000, 1000, 1000))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        roi = (4990, 4990, 5010, 5010)
        ref = orx.walk_roi(rar, 0, roi, 17, 0)

        # Swap the memoised root cell for a cache round-tripped copy.
        rar2 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        for cid in (0, 1):
            rar2._memo[cid] = _roundtrip(rar2.load_cell(cid))
        got = orx.walk_roi(rar2, 0, roi, 17, 0)
        assert ref["rects"].tolist() == got["rects"].tolist()
        assert [x.tolist() for x in ref["polys"]] == \
               [x.tolist() for x in got["polys"]]


class TestSidecarIO:

    def test_save_load_and_invalidate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
        data, _, _ = tr._build_two_cell()
        src = tmp_path / "two.oas"
        src.write_bytes(data)
        rar = orx.RandomAccessReader(src, wanted_layers={(17, 0)})
        c = rar.load_cell(0)

        assert cellcache.load(src, 0, {(17, 0)}) is None        # cold miss
        assert cellcache.save(src, 0, {(17, 0)}, c) is True
        hit = cellcache.load(src, 0, {(17, 0)})
        assert hit is not None
        assert hit.rect_count((17, 0)) == c.rect_count((17, 0))
        assert hit.rect_spec_at((17, 0), 0) == c.rect_spec_at((17, 0), 0)

        # Different wanted-layers tag -> different entry (miss).
        assert cellcache.load(src, 0, {(99, 0)}) is None
        # Touch the source (size change) -> stale -> miss. Release the reader's
        # mmap first: on Windows a live mapping locks the file and blocks the
        # rewrite (OSError 22); POSIX would allow it, so this only bit Windows.
        rar.close()
        src.write_bytes(data + b"\x00")
        assert cellcache.load(src, 0, {(17, 0)}) is None

    def test_load_cell_round_trips_through_cache(self, tmp_path, monkeypatch):
        # End-to-end: first reader decodes + writes the sidecar; a fresh reader's
        # load_cell must serve the cell from the cache (columnar) with identical
        # contents and an identical walk_roi result.
        monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("GLAS_CELLCACHE_MIN_RECORDS", "1")
        p = tmp_path / "big.oas"
        p.write_bytes(tr._build_big_grid(1000, 1000, 1000))
        roi = (4990, 4990, 5010, 5010)

        r1 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        ref = orx.walk_roi(r1, 0, roi, 17, 0)            # decodes + caches

        r2 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        c1 = r2.load_cell(1)                             # the rect-bearing cell
        assert c1._rcol                                  # served columnar = cache hit
        assert c1.rect_spec_at((17, 0), 0) == \
            r1.load_cell(1).rect_spec_at((17, 0), 0)
        got = orx.walk_roi(r2, 0, roi, 17, 0)
        assert ref["rects"].tolist() == got["rects"].tolist()

    def test_prep_cache_round_trips(self, tmp_path, monkeypatch):
        # F16-B M7: the placement-prune precompute is persisted and a fresh
        # reader serves it from disk (so a batch worker skips the gather).
        monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("GLAS_CELLCACHE_MIN_RECORDS", "1")
        p = tmp_path / "h.oas"
        p.write_bytes(tr._build_hierarchy([(0, 0), (1000, 0), (2000, 0)]))
        roi = (900, -50, 1100, 50)

        r1 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        ref = orx.walk_roi(r1, 0, roi, 17, 0)
        # F27 M7k: prep is keyed by the cell's canonical (offset) cache key so a
        # refnum walk and a name walk share it.
        assert cellcache.load_prep(p, r1.cache_key_for(0)) is not None  # persisted

        r2 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        got = orx.walk_roi(r2, 0, roi, 17, 0)
        assert r2.load_cell(0)._place_prep is not None    # served from disk
        assert ref["rects"].tolist() == got["rects"].tolist()

    def test_evict_lru_and_clear(self, tmp_path, monkeypatch):
        import os as _os
        monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
        d = tmp_path / "cache"
        d.mkdir()
        paths = []
        for i in range(3):
            f = d / f"e{i}.npz"
            f.write_bytes(b"x" * 1000)
            _os.utime(f, (1000 + i, 1000 + i))      # ascending mtime
            paths.append(f)
        # Cap below 3 files' total -> evict the oldest (e0) only.
        monkeypatch.setattr(cellcache, "_max_bytes", lambda: 2500)
        cellcache._evict()
        assert not paths[0].exists()                # oldest gone
        assert paths[1].exists() and paths[2].exists()
        # clear() wipes the rest.
        assert cellcache.clear() == 2
        assert list(d.glob("*.npz")) == []

    def test_disabled_via_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("GLAS_CELLCACHE", "0")
        data, _, _ = tr._build_two_cell()
        src = tmp_path / "two.oas"
        src.write_bytes(data)
        rar = orx.RandomAccessReader(src, wanted_layers={(17, 0)})
        c = rar.load_cell(0)
        assert cellcache.save(src, 0, {(17, 0)}, c) is False
        assert cellcache.load(src, 0, {(17, 0)}) is None
