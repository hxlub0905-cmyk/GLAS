"""Tests for F12 layer enumeration on OASIS files with no LAYERNAME table.

``oasis_random.enumerate_layers`` powers the UI's "Scan layers". For files
that carry a LAYERNAME table it returns named layers via the historical fast
path. For files without one (common in non-Calibre production OASIS) it falls
back to a *bounded* cell sample over the S_CELL_OFFSET index — never a full
scan, since these files are multi-GB. These tests pin both paths and, crucially,
that the sample stays bounded (cap + early-stop) rather than walking the file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1] / "glas" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import oasis_streamer as oas      # noqa: E402
import oasis_random as orx        # noqa: E402
import oasis_writer as osw        # noqa: E402
import layerscan_cache            # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the layer-scan sidecar cache at a per-test temp dir so tests stay
    hermetic and never touch the real user cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "_cache"))


# ── byte builders (mirror tests/test_oasis_random.py) ────────────────────────


def _uint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _sint(v: int) -> bytes:
    return _uint((abs(v) << 1) | (1 if v < 0 else 0))


def _ufix(n: int, width: int) -> bytes:
    out = []
    for i in range(width):
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if i < width - 1 else b)
    return bytes(out)


def _astr(s: str) -> bytes:
    b = s.encode()
    return _uint(len(b)) + b


def _rect(layer: int, datatype: int, w: int, h: int, x: int, y: int) -> bytes:
    # info 0x7b: W H X Y D L present (S, R absent).
    return (bytes([oas.RECTANGLE, 0x7b]) + _uint(layer) + _uint(datatype)
            + _uint(w) + _uint(h) + _sint(x) + _sint(y))


def _text(layer: int, ttype: int, x: int, y: int, s: str = "lbl") -> bytes:
    # info 0x5b: text-string + X + Y + TEXTTYPE + TEXTLAYER present.
    return (bytes([oas.TEXT, 0x5b]) + _astr(s) + _uint(layer) + _uint(ttype)
            + _sint(x) + _sint(y))


def _layername(name: str, layer: int, datatype: int) -> bytes:
    # LAYERNAME_GEOM: name, layer-interval (kind 1 = single), datatype-interval.
    return (bytes([oas.LAYERNAME_GEOM]) + _astr(name)
            + _uint(1) + _uint(layer) + _uint(1) + _uint(datatype))


def _start() -> bytes:
    return (bytes([oas.START]) + _astr("1.0") + bytes([0])
            + _uint(1000) + _uint(0) + bytes([0] * 12))


def _build_file(cells, *, layernames=None, with_offsets=True) -> bytes:
    """Build an OASIS file.

    ``cells``: list of ``(name, shape_bytes)`` — each cell becomes a
    CELLNAME + (optional) S_CELL_OFFSET property + a CELL record carrying
    ``shape_bytes``. ``layernames``: optional list of ``(name, L, D)`` emitted
    as a LAYERNAME table in the header. ``with_offsets=False`` omits the
    S_CELL_OFFSET properties (the "no index" case)."""
    start = _start()
    ln = b"".join(_layername(n, L, D) for n, L, D in (layernames or []))
    pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")

    def prop(off):    # PROPERTY: C=1 N=1 V=0 U=1; ref 0; value type 8 (uint)
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    # Header = MAGIC + START + LAYERNAME table + (S_CELL_OFFSET propname) +
    # per cell: CELLNAME [+ PROPERTY]. Fixed-width offsets keep the header
    # length identical whether offsets are placeholders or final values.
    def header(offs):
        h = oas.MAGIC + start + ln
        if with_offsets:
            h += pn
        for (name, _), off in zip(cells, offs):
            h += bytes([oas.CELLNAME_IMP]) + _astr(name)
            if with_offsets:
                h += prop(off)
        return h

    bodies = [bytes([oas.CELL_REFNUM]) + _uint(i) + shp
              for i, (_, shp) in enumerate(cells)]

    placeholder = header([0] * len(cells))
    offs, pos = [], len(placeholder)
    for b in bodies:
        offs.append(pos)
        pos += len(b)

    return header(offs) + b"".join(bodies) + bytes([oas.END]) + _uint(0)


def _build_strict_end_tables(cells, *, layernames=None, offsets_in_end=False) -> bytes:
    """Build a *strict-mode* OASIS where the name tables (PROPNAME +
    CELLNAME-with-S_CELL_OFFSET [+ LAYERNAME]) sit AFTER the cells, with the
    table byte-offsets recorded in START (``offsets_in_end=False``) or in the
    fixed 256-byte END record (``offsets_in_end=True``). This mirrors how
    KLayout writes 'Save As ▸ OASIS (strict)' — the case F12 must handle.

    ``cells``: list of ``(name, shape_bytes)``. ``layernames``: optional
    ``[(name, L, D)]`` (omit to model a numeric-only file)."""
    end_len = 256

    cell_bodies = [bytes([oas.CELL_REFNUM]) + _uint(i) + shp
                   for i, (_, shp) in enumerate(cells)]
    pn_tab = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
    ln_tab = b"".join(_layername(n, L, D) for n, L, D in (layernames or []))

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    def start(off_cn, off_pn, off_ln):
        s = bytes([oas.START]) + _astr("1.0") + bytes([0]) + _uint(1000)
        if offsets_in_end:
            s += _uint(1)            # offset_flag = 1 -> offsets in END
            return s
        s += _uint(0)                # offset_flag = 0 -> 6 pairs follow
        # CELLNAME, TEXTSTRING, PROPNAME, PROPSTRING, LAYERNAME, XNAME.
        s += _uint(1) + _ufix(off_cn, 4)
        s += _uint(0) + _uint(0)
        s += _uint(1) + _ufix(off_pn, 4)
        s += _uint(0) + _uint(0)
        s += (_uint(1) + _ufix(off_ln, 4)) if ln_tab else (_uint(0) + _uint(0))
        s += _uint(0) + _uint(0)
        return s

    start_len = len(start(0, 0, 0))
    base = len(oas.MAGIC) + start_len
    cell_offs, pos = [], base
    for b in cell_bodies:
        cell_offs.append(pos)
        pos += len(b)
    off_pn = pos
    off_cn = off_pn + len(pn_tab)
    cn_tab = b"".join(
        bytes([oas.CELLNAME_IMP]) + _astr(name) + prop(cell_offs[i])
        for i, (name, _) in enumerate(cells))
    off_ln = off_cn + len(cn_tab)

    def end_rec():
        if not offsets_in_end:
            return (bytes([oas.END]) + _uint(0)).ljust(end_len, b"\x00")
        body = bytes([oas.END])
        body += _uint(1) + _ufix(off_cn, 4)
        body += _uint(0) + _uint(0)
        body += _uint(1) + _ufix(off_pn, 4)
        body += _uint(0) + _uint(0)
        body += (_uint(1) + _ufix(off_ln, 4)) if ln_tab else (_uint(0) + _uint(0))
        body += _uint(0) + _uint(0)
        body += _uint(0)             # validation scheme 0
        return body.ljust(end_len, b"\x00")

    return (oas.MAGIC + start(off_cn, off_pn, off_ln)
            + b"".join(cell_bodies) + pn_tab + cn_tab + ln_tab + end_rec())


# ── tests ────────────────────────────────────────────────────────────────────


class TestSampledEnumeration:
    def test_enumerates_numeric_layers_no_names(self, tmp_path):
        # Layers spread across cells: A=17/0, B=6/0, C=42/5.
        data = _build_file([
            ("A", _rect(17, 0, 10, 10, 0, 0)),
            ("B", _rect(6, 0, 20, 20, 100, 100)),
            ("C", _rect(42, 5, 5, 5, 200, 200)),
        ])
        p = tmp_path / "noln.oas"
        p.write_bytes(data)

        res = orx.enumerate_layers(p)
        assert res["source"] == "sampled"
        pairs = {(d["layer"], d["datatype"]) for d in res["layers"]}
        assert pairs == {(17, 0), (6, 0), (42, 5)}
        assert all(d["name"] == "" for d in res["layers"])

    def test_text_layer_included_and_toggle(self, tmp_path):
        data = _build_file([
            ("A", _rect(17, 0, 10, 10, 0, 0) + _text(88, 0, 5, 5)),
        ])
        p = tmp_path / "txt.oas"
        p.write_bytes(data)

        on = orx.enumerate_layers(p, include_text=True)
        assert (88, 0) in {(d["layer"], d["datatype"]) for d in on["layers"]}

        off = orx.enumerate_layers(p, include_text=False)
        assert (88, 0) not in {(d["layer"], d["datatype"]) for d in off["layers"]}
        # geometry layer always present regardless of the text toggle
        assert (17, 0) in {(d["layer"], d["datatype"]) for d in off["layers"]}

    def test_mixed_shapes_one_cell(self, tmp_path):
        data = _build_file([
            ("A", _rect(1, 2, 10, 10, 0, 0) + _rect(3, 4, 10, 10, 50, 0)),
        ])
        p = tmp_path / "multi.oas"
        p.write_bytes(data)
        res = orx.enumerate_layers(p)
        assert {(d["layer"], d["datatype"]) for d in res["layers"]} == {(1, 2), (3, 4)}


class TestFastPath:
    def test_layername_table_used_and_named(self, tmp_path):
        data = _build_file(
            [("A", _rect(17, 0, 10, 10, 0, 0))],
            layernames=[("metal1", 17, 0), ("poly", 6, 0)],
        )
        p = tmp_path / "named.oas"
        p.write_bytes(data)
        res = orx.enumerate_layers(p)
        assert res["source"] == "layername"
        by_pair = {(d["layer"], d["datatype"]): d["name"] for d in res["layers"]}
        assert by_pair == {(17, 0): "metal1", (6, 0): "poly"}

    def test_fast_path_skips_sampling(self, tmp_path, monkeypatch):
        data = _build_file(
            [("A", _rect(17, 0, 10, 10, 0, 0))],
            layernames=[("metal1", 17, 0)],
        )
        p = tmp_path / "named2.oas"
        p.write_bytes(data)

        def _boom(*a, **k):
            raise AssertionError("sample_layers must not run on a LAYERNAME file")

        monkeypatch.setattr(orx, "sample_layers", _boom)
        res = orx.enumerate_layers(p)
        assert res["source"] == "layername"


class TestStrictEndTables:
    """KLayout 'Save As ▸ OASIS (strict)' puts the cellname/S_CELL_OFFSET and
    LAYERNAME tables AFTER the cells; the byte offsets live in START or END.
    Before F12's fix these files looked index-less (no S_CELL_OFFSET / no
    LAYERNAME) and scanned as 'no-index'."""

    def test_offsets_in_start_with_layernames(self, tmp_path):
        data = _build_strict_end_tables(
            [("A", _rect(17, 0, 10, 10, 0, 0)),
             ("B", _rect(6, 0, 20, 20, 100, 100))],
            layernames=[("metal1", 17, 0), ("poly", 6, 0)])
        p = tmp_path / "strict_start.oas"
        p.write_bytes(data)

        idx = oas.scan_cell_offsets(p)
        assert set(idx["by_refnum"]) == {0, 1}        # offsets recovered
        assert idx["offsets_via"] == "tables"
        assert idx["offset_flag"] == 0

        res = orx.enumerate_layers(p)
        assert res["source"] == "layername"
        assert {(d["layer"], d["datatype"]): d["name"] for d in res["layers"]} == {
            (17, 0): "metal1", (6, 0): "poly"}

    def test_offsets_in_start_numeric_only_samples(self, tmp_path):
        # No LAYERNAME table -> falls through to bounded sampling, which now
        # works because the cellname offsets are recovered from the tables.
        data = _build_strict_end_tables([
            ("A", _rect(17, 101, 10, 10, 0, 0)),
            ("B", _rect(6, 0, 20, 20, 100, 100)),
        ])
        p = tmp_path / "strict_numeric.oas"
        p.write_bytes(data)

        idx = oas.scan_cell_offsets(p)
        assert set(idx["by_refnum"]) == {0, 1}

        res = orx.enumerate_layers(p)
        assert res["source"] == "sampled"
        assert {(d["layer"], d["datatype"]) for d in res["layers"]} == {(17, 101), (6, 0)}

    def test_offsets_in_end_record(self, tmp_path):
        data = _build_strict_end_tables(
            [("A", _rect(42, 5, 10, 10, 0, 0)),
             ("B", _rect(7, 0, 10, 10, 50, 0))],
            offsets_in_end=True)
        p = tmp_path / "strict_end.oas"
        p.write_bytes(data)

        idx = oas.scan_cell_offsets(p)
        assert set(idx["by_refnum"]) == {0, 1}
        assert idx["offset_flag"] == 1
        assert idx["offsets_via"] == "tables"

        res = orx.enumerate_layers(p)
        assert res["source"] == "sampled"
        assert {(d["layer"], d["datatype"]) for d in res["layers"]} == {(42, 5), (7, 0)}


class TestNoIndex:
    def test_no_offsets_no_layername_is_no_index(self, tmp_path):
        # oasis_writer emits geometry with neither LAYERNAME nor S_CELL_OFFSET.
        p = tmp_path / "writer.oas"
        osw.write_oasis(p, [(17, 0, [[(0, 0), (0, 10), (10, 10), (10, 0)]])])
        res = orx.enumerate_layers(p)
        assert res["source"] == "no-index"
        assert res["layers"] == []

    def test_no_index_does_not_hang_or_full_scan(self, tmp_path):
        # Same as above but assert it returns promptly (no full-file walk).
        p = tmp_path / "writer2.oas"
        osw.write_oasis(p, [(5, 0, [[(0, 0), (0, 5), (5, 5), (5, 0)]])])
        import time
        t0 = time.monotonic()
        res = orx.enumerate_layers(p)
        assert res["source"] == "no-index"
        assert time.monotonic() - t0 < 2.0


class TestBounded:
    def _many_cells(self, n, *, distinct_layers):
        cells = []
        for i in range(n):
            L = (i + 1) if distinct_layers else 7
            cells.append((f"C{i}", _rect(L, 0, 10, 10, i * 100, 0)))
        return cells

    def test_caps_at_max_cells(self, tmp_path):
        # 50 cells, each a distinct layer (no early-stop), cap at 8.
        data = _build_file(self._many_cells(50, distinct_layers=True))
        p = tmp_path / "big.oas"
        p.write_bytes(data)

        calls = []
        res = orx.enumerate_layers(
            p, max_cells=8, stop_after_no_new=999,
            progress_cb=lambda c, layers: calls.append(c))
        assert res["source"] == "sampled"
        # Sampled at most 8 cells -> at most 8 distinct layers, 8 progress ticks.
        assert len(calls) <= 8
        assert len(res["layers"]) <= 8

    def test_early_stop_on_convergence(self, tmp_path):
        # 60 cells all on the same layer: should stop long before the cap.
        data = _build_file(self._many_cells(60, distinct_layers=False))
        p = tmp_path / "same.oas"
        p.write_bytes(data)

        calls = []
        res = orx.enumerate_layers(
            p, max_cells=64, stop_after_no_new=3,
            progress_cb=lambda c, layers: calls.append(c))
        assert {(d["layer"], d["datatype"]) for d in res["layers"]} == {(7, 0)}
        # 1 cell finds the layer, then 3 with nothing new -> ~4 ticks, not 60.
        assert len(calls) <= 8


class TestCache:
    def test_hit_skips_resampling(self, tmp_path, monkeypatch):
        data = _build_file([
            ("A", _rect(17, 0, 10, 10, 0, 0)),
            ("B", _rect(6, 0, 20, 20, 100, 100)),
        ])
        p = tmp_path / "cached.oas"
        p.write_bytes(data)

        first = orx.enumerate_layers(p)
        assert first["source"] == "sampled"

        # Second call must hit the sidecar: sampling never runs, no progress.
        monkeypatch.setattr(orx, "sample_layers", lambda *a, **k:
                            (_ for _ in ()).throw(AssertionError("re-sampled")))
        calls = []
        second = orx.enumerate_layers(p, progress_cb=lambda c, l: calls.append(c))
        assert second["source"] == first["source"]
        assert second["layers"] == first["layers"]
        assert calls == []

    def test_invalidated_when_file_changes(self, tmp_path):
        p = tmp_path / "changing.oas"
        p.write_bytes(_build_file([("A", _rect(17, 0, 10, 10, 0, 0))]))
        first = orx.enumerate_layers(p)
        assert {(d["layer"], d["datatype"]) for d in first["layers"]} == {(17, 0)}

        # Rewrite with a different layer (and different size) -> cache miss.
        p.write_bytes(_build_file([
            ("A", _rect(99, 0, 10, 10, 0, 0)),
            ("B", _rect(42, 0, 10, 10, 50, 0)),
        ]))
        second = orx.enumerate_layers(p)
        assert {(d["layer"], d["datatype"]) for d in second["layers"]} == {(99, 0), (42, 0)}

    def test_use_cache_false_bypasses(self, tmp_path):
        p = tmp_path / "nocache.oas"
        p.write_bytes(_build_file([("A", _rect(17, 0, 10, 10, 0, 0))]))
        orx.enumerate_layers(p, use_cache=False)
        # Nothing was written, so a load returns None.
        assert layerscan_cache.load(p) is None


class TestScanParams:
    def test_defaults_are_generous(self):
        assert orx._SCAN_DEFAULTS["max_cells"] >= 256
        assert orx._SCAN_DEFAULTS["stop_after_no_new"] >= 64

    def test_explicit_beats_env_beats_default(self, monkeypatch):
        assert orx._scan_param("max_cells", 5, int) == 5          # explicit wins
        monkeypatch.setenv("GLAS_SCAN_MAX_CELLS", "999")
        assert orx._scan_param("max_cells", None, int) == 999     # env next
        monkeypatch.delenv("GLAS_SCAN_MAX_CELLS")
        assert orx._scan_param("max_cells", None, int) == \
            orx._SCAN_DEFAULTS["max_cells"]                       # default last

    def test_env_widens_coverage_via_enumerate(self, tmp_path, monkeypatch):
        # 30 cells, each a distinct layer. A tiny budget misses most; a wide
        # one (via env) catches them all.
        cells = [(f"C{i}", _rect(i + 1, 0, 10, 10, i * 100, 0)) for i in range(30)]
        p = tmp_path / "many.oas"
        p.write_bytes(_build_file(cells))

        narrow = orx.enumerate_layers(p, max_cells=3, stop_after_no_new=999)
        assert len(narrow["layers"]) <= 3

        monkeypatch.setenv("GLAS_SCAN_MAX_CELLS", "64")
        monkeypatch.setenv("GLAS_SCAN_STOP_AFTER_NO_NEW", "999")
        wide = orx.enumerate_layers(p)        # env-driven, no explicit args
        assert len(wide["layers"]) == 30


class TestSampleOffsets:
    def test_spreads_and_dedups(self):
        offs = list(range(0, 1000, 10))   # 100 offsets
        picked = orx._sample_offsets(offs, 8)
        assert len(picked) == 8
        assert picked[0] == 0
        assert picked == sorted(picked)
        assert len(set(picked)) == len(picked)

    def test_returns_all_when_fewer_than_cap(self):
        offs = [50, 10, 30]
        assert orx._sample_offsets(offs, 8) == [10, 30, 50]
