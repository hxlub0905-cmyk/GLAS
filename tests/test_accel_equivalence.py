"""Golden-output equivalence tests for the F6 acceleration work.

The F6 milestones (mmap-backed OASIS read, single shared map, thread-pool
batch fine-align) are *pure performance* changes: the output must be
byte-for-byte / value-for-value identical to the pre-acceleration path.
These tests pin that invariant so a speed-up can never silently change a
result.

M1 (this file, first batch):
  - OasisStream over a real file with ``use_mmap=True`` vs ``False`` returns
    identical bytes for every read primitive (numpy-free).
  - OasisReader.iter_records is identical mmap vs slurp (numpy-free).
  - RandomAccessReader.load_cell geometry is identical (numpy-gated).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1] / "glas" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import oasis_streamer as oas      # noqa: E402


# ── minimal OASIS builder (numpy-free; mirrors test_oasis_random) ────────────


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


def _rect(layer: int, w: int, h: int, x: int, y: int) -> bytes:
    return (bytes([oas.RECTANGLE, 0x7b]) + _uint(layer) + _uint(0)
            + _uint(w) + _uint(h) + _sint(x) + _sint(y))


def _build_two_cell() -> tuple[bytes, int, int]:
    """A=ref0 (rect + placement of B); B=ref1 (rect). Both carry
    S_CELL_OFFSET. Returns (bytes, offA, offB)."""
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
             + _uint(1000) + _uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
    cna = bytes([oas.CELLNAME_IMP]) + _astr("A")
    cnb = bytes([oas.CELLNAME_IMP]) + _astr("B")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    place_b = bytes([oas.PLACEMENT_NOMAG, 0xC0]) + _uint(1)
    cell_a = bytes([oas.CELL_REFNUM]) + _uint(0)
    cell_b = bytes([oas.CELL_REFNUM]) + _uint(1)
    end = bytes([oas.END]) + _uint(0)

    hdr = (oas.MAGIC + start + pn + cna + prop(0) + cnb + prop(0))
    off_a = len(hdr)
    body_a = cell_a + _rect(17, 10, 10, 0, 0) + place_b
    off_b = len(hdr) + len(body_a)
    data = (oas.MAGIC + start + pn + cna + prop(off_a) + cnb + prop(off_b)
            + body_a + cell_b + _rect(17, 20, 20, 100, 100) + end)
    return data, off_a, off_b


@pytest.fixture()
def oas_file(tmp_path):
    data, off_a, off_b = _build_two_cell()
    p = tmp_path / "two.oas"
    p.write_bytes(data)
    return p, data, off_a, off_b


# ── M1: OasisStream mmap vs slurp ────────────────────────────────────────────


class TestOasisStreamMmapEquivalence:

    def test_read_primitives_identical(self, oas_file):
        p, data, _, _ = oas_file
        # Walk both backings with the same primitive calls and compare.
        s_slurp = oas.OasisStream(open(p, "rb"), use_mmap=False)
        s_mmap = oas.OasisStream(open(p, "rb"), use_mmap=True)
        try:
            # mmap fixture must actually map (real file has a fileno).
            assert s_mmap._mmap is not None
            assert len(s_slurp._buf) == len(s_mmap._buf) == len(data)
            # byte-by-byte via read_byte
            for _ in range(len(data)):
                assert s_slurp.read_byte() == s_mmap.read_byte()
            # seek + bulk read
            for pos, n in [(0, 13), (5, 7), (len(data) - 3, 10)]:
                s_slurp.seek(pos)
                s_mmap.seek(pos)
                assert s_slurp.read(n) == s_mmap.read(n)
                assert s_slurp.tell() == s_mmap.tell()
        finally:
            s_slurp.close()
            s_mmap.close()

    def test_bytesio_falls_back_to_slurp(self, oas_file):
        _, data, _, _ = oas_file
        import io
        s = oas.OasisStream(io.BytesIO(data), use_mmap=True)
        try:
            assert s._mmap is None          # no fileno → slurp fallback
            assert s.read(len(data)) == data
        finally:
            s.close()

    def test_iter_records_identical(self, oas_file):
        p, _, _, _ = oas_file
        r_slurp = oas.OasisReader(p, use_mmap=False)
        r_mmap = oas.OasisReader(p, use_mmap=True)
        try:
            recs_slurp = list(r_slurp.iter_records())
            recs_mmap = list(r_mmap.iter_records())
        finally:
            r_slurp.close()
            r_mmap.close()
        assert recs_slurp == recs_mmap
        assert len(recs_slurp) > 0

    def test_scan_cell_offsets_identical(self, oas_file):
        p, _, off_a, off_b = oas_file
        idx_slurp = oas.scan_cell_offsets(p, use_mmap=False)
        idx_mmap = oas.scan_cell_offsets(p, use_mmap=True)
        assert idx_slurp == idx_mmap
        assert idx_mmap["by_refnum"] == {0: off_a, 1: off_b}


# ── M2: single shared map for scan + persistent reader ──────────────────────


class TestSharedMapEquivalence:

    def test_shared_buf_scan_matches_standalone(self, oas_file):
        p, _, off_a, off_b = oas_file
        own = oas.OasisStream(open(p, "rb"), use_mmap=True)
        try:
            shared = own._buf
            i_shared = oas.scan_cell_offsets(p, shared_buf=shared)
            i_std = oas.scan_cell_offsets(p, use_mmap=False)
            assert i_shared == i_std
            assert i_shared["by_refnum"] == {0: off_a, 1: off_b}
        finally:
            own.close()

    def test_shared_buf_reader_matches_standalone(self, oas_file):
        p, _, _, _ = oas_file
        own = oas.OasisStream(open(p, "rb"), use_mmap=True)
        try:
            shared = own._buf
            r_shared = oas.OasisReader(p, shared_buf=shared)
            recs_shared = list(r_shared.iter_records())
            r_shared.close()           # must NOT close the owner's map
            # owner still usable after a shared wrapper closed
            assert not own.closed
            own.seek(0)
            assert own.read(len(oas.MAGIC)) == oas.MAGIC
            r_std = oas.OasisReader(p, use_mmap=False)
            recs_std = list(r_std.iter_records())
            r_std.close()
            assert recs_shared == recs_std
        finally:
            own.close()

    def test_random_access_reader_close_is_idempotent(self, oas_file):
        pytest.importorskip("numpy")
        import oasis_random as orx
        p, _, _, _ = oas_file
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        rar.load_cell(0)
        rar.close()
        rar.close()        # second close must not raise


# ── M1: RandomAccessReader geometry identical (numpy-gated) ──────────────────


class TestRandomAccessMmapEquivalence:

    def test_load_cell_geometry_identical(self, oas_file):
        pytest.importorskip("numpy")
        import oasis_random as orx
        p, _, _, _ = oas_file
        # RandomAccessReader now maps the file (use_mmap=True internally);
        # geometry must equal the documented golden output.
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert rar.has_offsets()
        a = rar.load_cell(0)
        assert a.rects((17, 0)).tolist() == [[0, 0, 10, 10]]
        assert a.bbox == (0, 0, 10, 10)
        b = rar.load_cell(1)
        assert b.rects((17, 0)).tolist() == [[100, 100, 120, 120]]
        assert b.bbox == (100, 100, 120, 120)
        rar.close()


# ── M3: thread-pool batch == sequential batch (numpy/cv2/PyQt6-gated) ─────────


class TestBatchParallelEquivalence:
    """Per-image fine-align is independent, so running the batch across a
    thread pool must produce byte-identical per-image results to the
    sequential loop. Pins the F6 M3 "no functional change" invariant."""

    def _setup(self, tmp_path):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app_dir = Path(__file__).resolve().parents[1] / "glas" / "app"
        if str(app_dir) not in sys.path:
            sys.path.insert(0, str(app_dir))
        pytest.importorskip("PyQt6", reason="PyQt6 required to import gds_align_tool")
        np = pytest.importorskip("numpy")
        cv2 = pytest.importorskip("cv2")
        import gds_align_tool as gat
        import oasis_random as orx

        # OASIS with geometry on layer (17, 0).
        data, _, _ = _build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)
        base = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        root = next(iter(base._by_name))      # any cell name as root

        # A couple of non-flat fake SEM frames so matchTemplate has signal.
        frames = []
        for i in range(4):
            img = (np.tile(np.arange(64, dtype=np.uint8), (64, 1)) + i * 3)
            fp = tmp_path / f"sem_{i}.png"
            cv2.imwrite(str(fp), img)
            frames.append(fp)

        # jobs: mix of valid anchors, a no-coords, and a missing file.
        jobs = [
            ("img0", (50.0, 50.0), str(frames[0]), True),
            ("img1", (110.0, 110.0), str(frames[1]), True),
            ("img2", None, str(frames[2]), True),
            ("img3", (60.0, 60.0), str(tmp_path / "nope.png"), False),
            ("img4", (5.0, 5.0), str(frames[3]), True),
        ]
        cfg = {
            "fov_w": 200.0, "fov_h": 200.0, "nm_auto": True, "nm_manual": 0.0,
            "bg_glv": 80, "blur_sigma_px": 1.0, "search_radius_nm": 20.0,
            "score_threshold": 0.5,
        }
        specs = [(("raw", 17, 0), 200)]
        return gat, base, root, jobs, cfg, specs

    def test_sequential_matches_threadpool(self, tmp_path):
        gat, base, root, jobs, cfg, specs = self._setup(tmp_path)
        from concurrent.futures import ThreadPoolExecutor

        never = lambda: False

        # sequential (shared reader, like the old code path)
        seq = {}
        for job in jobs:
            res = gat._fine_align_image(job, base, root, specs, cfg, never)
            seq[res[0]] = res

        # thread-pool: each thread clones the reader (private state)
        import threading
        tl = threading.local()
        created = []
        lock = threading.Lock()

        def reader():
            r = getattr(tl, "rar", None)
            if r is None:
                r = base.clone()
                with lock:
                    created.append(r)
                tl.rar = r
            return r

        def task(j):
            # reader() runs INSIDE the pool thread → thread-local clone per
            # worker, exactly like FineAlignAllWorker.run().
            return gat._fine_align_image(j, reader(), root, specs, cfg, never)

        par = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(task, j) for j in jobs]
            for f in futs:
                res = f.result()
                par[res[0]] = res
        for r in created:
            r.close()
        base.close()

        assert seq == par
        # sanity: the deterministic non-anchored / missing cases are present
        assert seq["img2"][5] == "no-coords"
        assert seq["img3"][5] == "missing-file"


# ── F8: ProcessPool batch == sequential batch (numpy/cv2-gated) ──────────────


class TestProcessPoolEquivalence:
    """F8 moved the batch from a thread pool to a process pool. A worker
    process rebuilds its reader from the file *path* (the live reader isn't
    picklable) via ``fine_align._pool_init`` and runs the same per-image work
    via ``fine_align._pool_task``. This pins that the pool entry points
    reproduce the sequential per-image result exactly — the reader rebuilt from
    path + the task wiring must not change a single value (§7).

    The std-lib process transport itself is not re-tested here (spawning real
    workers under pytest is slow/fragile); the entry points are exercised
    in-process, which is where any divergence would come from."""

    def test_pool_entry_matches_sequential(self, tmp_path):
        np = pytest.importorskip("numpy")
        cv2 = pytest.importorskip("cv2")
        import fine_align as fa
        import oasis_random as orx

        data, _, _ = _build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)
        base = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        root = next(iter(base._by_name))

        frames = []
        for i in range(3):
            img = (np.tile(np.arange(64, dtype=np.uint8), (64, 1)) + i * 4)
            fp = tmp_path / f"sem_{i}.png"
            cv2.imwrite(str(fp), img)
            frames.append(fp)
        jobs = [
            ("img0", (50.0, 50.0), str(frames[0]), True),
            ("img1", (110.0, 110.0), str(frames[1]), True),
            ("img2", None, str(frames[2]), True),       # no-coords
            ("img3", (60.0, 60.0), str(tmp_path / "nope.png"), False),  # missing
        ]
        cfg = {
            "fov_w": 200.0, "fov_h": 200.0, "nm_auto": True, "nm_manual": 0.0,
            "bg_glv": 80, "blur_sigma_px": 1.0, "search_radius_nm": 20.0,
        }
        specs = [(("raw", 17, 0), 200)]

        # Sequential baseline on the live base reader.
        never = lambda: False
        seq = {}
        for job in jobs:
            r = fa._fine_align_image(job, base, root, specs, cfg, never)
            seq[r[0]] = r

        # Pool entry points: _pool_init rebuilds the reader from the PATH only
        # (exactly what a spawned worker does), _pool_task runs the per-image
        # work against that rebuilt reader.
        fa._pool_init(str(p), base._init_wanted, base._dtype,
                      base._bbox_layer, root, specs, cfg)
        pool = {}
        for job in jobs:
            r = fa._pool_task(job)
            pool[r[0]] = r
        fa._G["rar"].close()
        base.close()

        assert seq == pool
        assert seq["img2"][5] == "no-coords"
        assert seq["img3"][5] == "missing-file"
