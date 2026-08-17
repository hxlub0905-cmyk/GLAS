"""F31 M1: the multi-page TIFF read base.

``tiff_index`` is vendored from ADEPT (which vendored it from KLIP); these
tests pin the parts GLAS depends on rather than re-testing the upstream walker:

* the IFD walk counts pages for classic TIFF and BigTIFF, without decoding;
* :func:`tiff_index.read_sem_gray` with ``page=None`` is **byte-identical** to
  the ``cv2.imread(..., IMREAD_GRAYSCALE)`` call it replaces at the three read
  sites — this is the guard that keeps every existing single-image flow (rSEM
  KLARF, folder mode) unchanged;
* a page read returns *that* page, including after another page was read from
  the same cached handle (the bug the eager ``len(tf.pages)`` in
  ``_tiff_handle_locked`` exists to prevent);
* the two silent-corruption guards are still in place — the pid in the cache
  version key (fork-copied fds share a file offset) and the lock around
  ``read_page`` (threads sharing one handle). Both matter in GLAS specifically:
  export runs on a ProcessPool, the preview on a QThread.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
import cv2                       # noqa: E402
import tiff_index                # noqa: E402


def _page_pixels(i, h=24, w=32):
    """A page whose content is unmistakably page ``i`` (constant plane + a
    marker pixel), so a test can tell *which* page came back."""
    a = np.full((h, w), (i * 20 + 5) % 256, dtype=np.uint8)
    a[0, 0] = (i * 7 + 1) % 256
    return a


def _write_multipage(path, n, bigtiff=False):
    tifffile = pytest.importorskip("tifffile")
    with tifffile.TiffWriter(str(path), bigtiff=bigtiff) as tw:
        for i in range(n):
            tw.write(_page_pixels(i), contiguous=False)
    return path


@pytest.fixture(autouse=True)
def _close_handles():
    """The handle cache is module-global; drop it between tests so one test's
    open file can't serve another's (and so tmp dirs can be removed on Win)."""
    yield
    tiff_index.close_cached_tiffs()


# ── page counting ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [1, 2, 5, 17])
def test_count_pages_classic(tmp_path, n):
    p = _write_multipage(tmp_path / f"c{n}.tif", n)
    assert tiff_index.count_pages(p) == n
    assert tiff_index.n_pages(p) == n


def test_count_pages_bigtiff(tmp_path):
    p = _write_multipage(tmp_path / "big.tif", 4, bigtiff=True)
    assert tiff_index.n_pages(p) == 4


def test_count_pages_rejects_non_tiff(tmp_path):
    p = tmp_path / "not.tif"
    p.write_bytes(b"this is not a tiff at all")
    with pytest.raises(ValueError):
        tiff_index.n_pages(p)


def test_count_pages_does_not_need_tifffile(tmp_path, monkeypatch):
    """The IFD walk is the pure-stdlib half — it must work with tifffile absent
    (it is an optional requirement), because the loader calls it to validate the
    KLARF page mapping before any pixel is decoded."""
    p = _write_multipage(tmp_path / "m.tif", 3)
    monkeypatch.setitem(sys.modules, "tifffile", None)
    assert tiff_index.n_pages(p) == 3


# ── read_sem_gray: the page=None contract ────────────────────────────────────

@pytest.mark.parametrize("name,writer", [
    ("g.png", lambda p: cv2.imwrite(str(p), _page_pixels(3, 40, 50))),
    ("c.png", lambda p: cv2.imwrite(
        str(p), np.dstack([_page_pixels(1, 40, 50), _page_pixels(2, 40, 50),
                           _page_pixels(3, 40, 50)]))),
])
def test_page_none_is_byte_identical_to_imread(tmp_path, name, writer):
    """§7 guard: the pre-F31 read path is ``cv2.imread(path, IMREAD_GRAYSCALE)``
    and must stay exactly that, for grayscale and colour sources alike."""
    p = tmp_path / name
    writer(p)
    got = tiff_index.read_sem_gray(p)
    want = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    assert got is not None
    assert np.array_equal(got, want)
    assert got.dtype == want.dtype == np.uint8


def test_page_none_missing_file_returns_none(tmp_path):
    """``cv2.imread`` returns None for an unreadable file and callers already
    map that to status "missing-file"; the wrapper keeps that contract."""
    assert tiff_index.read_sem_gray(tmp_path / "nope.png") is None


def test_page_none_on_multipage_reads_first_page(tmp_path):
    """Documents the pre-F31 behaviour this feature exists to fix: without a
    page, a multi-page TIFF still yields page 0 — that is exactly why every
    defect used to align against the same frame."""
    p = _write_multipage(tmp_path / "m.tif", 4)
    assert np.array_equal(tiff_index.read_sem_gray(p), _page_pixels(0))


# ── read_sem_gray: the page path ─────────────────────────────────────────────

def test_reads_the_requested_page(tmp_path):
    p = _write_multipage(tmp_path / "m.tif", 5)
    for i in range(5):
        assert np.array_equal(tiff_index.read_sem_gray(p, i), _page_pixels(i)), i


def test_pages_differ_across_defects(tmp_path):
    """The whole point of F31: two defects pointing at the same batch TIFF must
    not get the same frame."""
    p = _write_multipage(tmp_path / "batch.tif", 6)
    test_a, ref_a = tiff_index.read_sem_gray(p, 0), tiff_index.read_sem_gray(p, 1)
    test_b, ref_b = tiff_index.read_sem_gray(p, 2), tiff_index.read_sem_gray(p, 3)
    assert not np.array_equal(test_a, test_b)
    assert not np.array_equal(ref_a, ref_b)
    assert not np.array_equal(test_a, ref_a)


def test_sequential_page_reads_on_one_handle(tmp_path):
    """Reading page 0's pixels then asking for page 1 used to hand tifffile a
    file position mid-pixel-data (it reports "suspicious number of tags").
    ``_tiff_handle_locked`` builds the page list eagerly to stop that; a second
    read through the *cached* handle is the regression this pins."""
    p = _write_multipage(tmp_path / "m.tif", 3)
    assert np.array_equal(tiff_index.read_sem_gray(p, 0), _page_pixels(0))
    assert np.array_equal(tiff_index.read_sem_gray(p, 1), _page_pixels(1))
    assert np.array_equal(tiff_index.read_sem_gray(p, 2), _page_pixels(2))


def test_read_page_raises_out_of_range(tmp_path):
    pytest.importorskip("tifffile")
    p = _write_multipage(tmp_path / "m.tif", 2)
    with pytest.raises(IndexError):
        tiff_index.read_page(str(p), 5)


def test_read_sem_gray_out_of_range_returns_none(tmp_path):
    """A batch must not die on one bad page: the wrapper degrades an
    out-of-range page to the same None the callers already treat as
    "missing-file"."""
    p = _write_multipage(tmp_path / "m.tif", 2)
    assert tiff_index.read_sem_gray(p, 9) is None


def test_colour_and_16bit_pages_coerced_to_u8_gray(tmp_path):
    tifffile = pytest.importorskip("tifffile")
    p = tmp_path / "mixed.tif"
    rgb = np.dstack([np.full((8, 8), 10, np.uint8), np.full((8, 8), 20, np.uint8),
                     np.full((8, 8), 30, np.uint8)])
    u16 = np.arange(64, dtype=np.uint16).reshape(8, 8) * 100
    with tifffile.TiffWriter(str(p)) as tw:
        tw.write(rgb, contiguous=False)
        tw.write(u16, contiguous=False)
    for i in (0, 1):
        got = tiff_index.read_sem_gray(p, i)
        assert got.ndim == 2 and got.dtype == np.uint8, i
    # min-max normalisation spans the full range (ADEPT load_gray semantics).
    assert tiff_index.read_sem_gray(p, 1).min() == 0
    assert tiff_index.read_sem_gray(p, 1).max() == 255


def test_cv2_fallback_when_tifffile_absent(tmp_path, monkeypatch):
    """tifffile is an optional requirement; without it the page read falls back
    to cv2.imreadmulti rather than failing."""
    p = _write_multipage(tmp_path / "m.tif", 4)
    tiff_index.close_cached_tiffs()
    monkeypatch.setitem(sys.modules, "tifffile", None)
    with pytest.raises(ImportError):
        tiff_index.read_page(str(p), 1)
    for i in range(4):
        assert np.array_equal(tiff_index.read_sem_gray(p, i), _page_pixels(i)), i


# ── the two silent-corruption guards ─────────────────────────────────────────

def test_handle_cache_key_includes_pid(tmp_path):
    """Fork copies the parent's fds and the copies **share one file offset**, so
    a pooled worker inheriting a cached handle would read another page's bytes
    with no error at all. The pid in the version key makes a child rebuild its
    own handle. Guard test: don't drop it."""
    p = _write_multipage(tmp_path / "m.tif", 2)
    assert os.getpid() in tiff_index._stat_key(str(p))


def test_read_page_is_serialised_across_threads(tmp_path):
    """One TiffFile is one fd; two threads seeking in it interleave and return
    each other's bytes. ``read_page`` holds the lock for the whole read."""
    pytest.importorskip("tifffile")
    p = _write_multipage(tmp_path / "m.tif", 8)
    errors, out = [], {}

    def worker(i):
        try:
            for _ in range(12):
                got = tiff_index.read_sem_gray(p, i)
                if not np.array_equal(got, _page_pixels(i)):
                    errors.append(f"page {i} came back as another page")
            out[i] = True
        except Exception as exc:                       # noqa: BLE001
            errors.append(f"page {i}: {exc!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(out) == 8
