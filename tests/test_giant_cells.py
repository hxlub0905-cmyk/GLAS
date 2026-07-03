"""F27 M7j: the export pre-decodes the giant merge cell once (so pool workers
load it from cache instead of all cold-decoding it and thrashing). It finds that
cell by its ENCODED BYTE SPAN in the offset table — the gap to the next cell
offset — preferring the cell's name so the cache key matches the ROI walk.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import oasis_random as orx           # noqa: E402


class _FakeF:
    def __init__(self, buflen):
        self._buf = bytearray(buflen)


class _FakeReader:
    def __init__(self, buflen):
        self._f = _FakeF(buflen)


def _reader(by_refnum, by_name, buflen):
    r = object.__new__(orx.RandomAccessReader)
    r._by_refnum = by_refnum
    r._by_name = by_name
    r._reader = _FakeReader(buflen)
    return r


def test_finds_biggest_span_prefers_name():
    # offsets 0,10,500 in a 1000-byte file -> spans 10, 490, 500(->EOF).
    r = _reader({1: 0, 2: 10, 3: 500}, {"top": 500}, 1000)
    giants = r.find_giant_cells(min_bytes=100, max_return=4)
    assert giants[0] == "top"          # largest span (500), name-preferred
    assert 2 in giants                 # span 490 >= 100
    assert 1 not in giants             # span 10 < 100


def test_threshold_and_cap():
    r = _reader({1: 0, 2: 100, 3: 200, 4: 300}, {}, 100_000_000)
    # last cell (offset 300) spans to EOF -> ~100 MB; others are 100 bytes.
    giants = r.find_giant_cells(min_bytes=20_000_000, max_return=4)
    assert giants == [4]               # only the huge trailing cell qualifies


def test_no_offsets_returns_empty():
    r = _reader({}, {}, 0)
    assert r.find_giant_cells() == []


def test_cache_key_is_canonical_by_offset():
    # F27 M7k: the SAME cell reached by refnum (44995) and by name ('iMerge_Top')
    # must map to ONE cache key (its offset) so it isn't decoded + cached twice.
    r = _reader({44995: 12345}, {"iMerge_Top": 12345}, 100)
    assert r.cache_key_for(44995) == 12345
    assert r.cache_key_for("iMerge_Top") == 12345
    assert r.cache_key_for(44995) == r.cache_key_for("iMerge_Top")
    assert r.cache_key_for(999) == 999          # unknown cell → falls back to id
