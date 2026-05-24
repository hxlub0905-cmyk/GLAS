"""Tests for tools/gds_layer_cache.py (F2 M2.1 user-facing layer cache)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import gds_layer_cache as glc  # noqa: E402
from gds_layer_cache import (  # noqa: E402
    LayerCacheMeta,
    SCHEMA_VERSION,
    cache_save,
    cache_load,
    check_source,
    make_meta,
)


def _src(tmp_path, content=b"oasis-bytes"):
    p = tmp_path / "design.oas"
    p.write_bytes(content)
    return p


def _layers():
    l1 = (17, 101,
          [np.array([[0, 0], [10, 0], [10, 5], [0, 5]], dtype=np.float32),
           np.array([[20, 0], [30, 0], [30, 5], [20, 5]], dtype=np.float32)],
          np.array([[0, 0, 10, 5], [20, 0, 30, 5]], dtype=np.float32))
    l2 = (18, 0,
          [np.array([[0, 0], [4, 0], [4, 4], [2, 6], [0, 4]], dtype=np.float32)],
          np.array([[0, 0, 4, 6]], dtype=np.float32))
    return [l1, l2]


def _meta(src):
    return make_meta(src, chip_corner_x=1000, chip_corner_y=2000,
                     fov_w=5000, fov_h=4000, top_cell_name="iMerge_Top",
                     nm_units=1.0)


# ── Round-trip ───────────────────────────────────────────────────────


class TestRoundTrip:

    def test_save_then_load(self, tmp_path):
        src = _src(tmp_path)
        out = tmp_path / "proj.npz"
        cache_save(out, _layers(), _meta(src))
        loaded = cache_load(out)
        assert loaded is not None
        assert loaded.meta.chip_corner_x == 1000
        assert loaded.meta.chip_corner_y == 2000
        assert loaded.meta.fov_w == 5000
        assert loaded.meta.fov_h == 4000
        assert loaded.meta.top_cell_name == "iMerge_Top"
        assert loaded.meta.source_oas == "design.oas"
        assert len(loaded.layers) == 2
        l1 = loaded.layers[0]
        assert l1[0] == 17 and l1[1] == 101
        assert len(l1[2]) == 2
        np.testing.assert_array_equal(l1[3], _layers()[0][3])
        l2 = loaded.layers[1]
        assert l2[2][0].shape == (5, 2)

    def test_m4a_origin_and_scale_roundtrip(self, tmp_path):
        """M4a δ origin + overlay nm_per_px must survive the cache so the
        user's dragged alignment is restored on reload (SCHEMA v3+)."""
        src = _src(tmp_path)
        out = tmp_path / "proj.npz"
        meta = make_meta(src, origin_dx=1234.5, origin_dy=-678.0,
                         nm_per_px=2.75)
        cache_save(out, _layers(), meta)
        loaded = cache_load(out)
        assert loaded is not None
        assert loaded.meta.origin_dx == pytest.approx(1234.5)
        assert loaded.meta.origin_dy == pytest.approx(-678.0)
        assert loaded.meta.nm_per_px == pytest.approx(2.75)

    def test_rfl_chip_offset_um_roundtrip(self, tmp_path):
        """SCHEMA v4 stores the RFL Chip-offset row (µm) as the source of
        truth for the chip-corner derivation; it must round-trip intact."""
        src = _src(tmp_path)
        out = tmp_path / "proj.npz"
        meta = make_meta(src, chip_x_um=12.5, chip_y_um=34.0,
                         chip_w_um=800.0, chip_h_um=600.0,
                         gds_off_x_um=0.125, gds_off_y_um=-0.25)
        cache_save(out, _layers(), meta)
        loaded = cache_load(out)
        assert loaded is not None
        assert loaded.meta.chip_x_um == pytest.approx(12.5)
        assert loaded.meta.chip_y_um == pytest.approx(34.0)
        assert loaded.meta.chip_w_um == pytest.approx(800.0)
        assert loaded.meta.chip_h_um == pytest.approx(600.0)
        assert loaded.meta.gds_off_x_um == pytest.approx(0.125)
        assert loaded.meta.gds_off_y_um == pytest.approx(-0.25)

    def test_only_selected_layers_stored(self, tmp_path):
        # Pass just one of the two layers -> only that one comes back.
        src = _src(tmp_path)
        out = tmp_path / "proj.npz"
        cache_save(out, _layers()[:1], _meta(src))
        loaded = cache_load(out)
        assert len(loaded.layers) == 1
        assert loaded.layers[0][0] == 17

    def test_empty_layer_roundtrip(self, tmp_path):
        src = _src(tmp_path)
        out = tmp_path / "proj.npz"
        empty = (5, 0, [], np.empty((0, 4), dtype=np.float32))
        cache_save(out, [empty], _meta(src))
        loaded = cache_load(out)
        assert len(loaded.layers) == 1
        assert loaded.layers[0][2] == []
        assert loaded.layers[0][3].shape == (0, 4)

    def test_load_missing_returns_none(self, tmp_path):
        assert cache_load(tmp_path / "nope.npz") is None

    def test_load_corrupted_returns_none(self, tmp_path):
        bad = tmp_path / "bad.npz"
        bad.write_bytes(b"not an npz file")
        assert cache_load(bad) is None

    def test_bbox_mismatch_raises(self, tmp_path):
        src = _src(tmp_path)
        bad = (17, 101, [np.zeros((4, 2), dtype=np.float32)],
               np.empty((0, 4), dtype=np.float32))
        with pytest.raises(ValueError, match="bboxes shape"):
            cache_save(tmp_path / "p.npz", [bad], _meta(src))

    def test_atomic_no_tmp_left(self, tmp_path):
        src = _src(tmp_path)
        out = tmp_path / "proj.npz"
        cache_save(out, _layers(), _meta(src))
        assert list(tmp_path.glob("*.tmp")) == []
        assert out.exists()


# ── Schema gate ──────────────────────────────────────────────────────


class TestSchema:

    def test_old_schema_rejected(self, tmp_path, monkeypatch):
        src = _src(tmp_path)
        out = tmp_path / "proj.npz"
        cache_save(out, _layers(), _meta(src))
        assert cache_load(out) is not None
        monkeypatch.setattr(glc, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
        assert cache_load(out) is None


# ── Staleness check ──────────────────────────────────────────────────


class TestCheckSource:

    def test_ok(self, tmp_path):
        src = _src(tmp_path)
        meta = _meta(src)
        assert check_source(meta, src) == "ok"

    def test_no_source(self, tmp_path):
        meta = _meta(_src(tmp_path))
        assert check_source(meta, None) == "no_source"

    def test_missing(self, tmp_path):
        src = _src(tmp_path)
        meta = _meta(src)
        src.unlink()
        assert check_source(meta, src) == "missing"

    def test_name_mismatch(self, tmp_path):
        src = _src(tmp_path)
        meta = _meta(src)
        other = tmp_path / "other.oas"
        other.write_bytes(b"oasis-bytes")
        assert check_source(meta, other) == "name_mismatch"

    def test_stale_mtime(self, tmp_path):
        src = _src(tmp_path)
        meta = _meta(src)
        # Bump mtime two seconds forward.
        st = src.stat()
        os.utime(src, (st.st_atime, st.st_mtime + 2))
        assert check_source(meta, src) == "stale_mtime"

    def test_stale_size(self, tmp_path):
        src = _src(tmp_path)
        meta = _meta(src)
        # Same name, same mtime, different size.
        st = src.stat()
        src.write_bytes(b"oasis-bytes-but-longer-now")
        os.utime(src, (st.st_atime, st.st_mtime))
        assert check_source(meta, src) == "stale_mtime"
