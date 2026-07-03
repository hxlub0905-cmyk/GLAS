"""F27 M7: the intra-cell decode heartbeat (so a minutes-long single-cell decode
shows live progress instead of a frozen "Loading GDS ROI…"). It must be
non-semantic — decoded geometry is unchanged — and must never leave a stale
"decoding cell" state behind after load_cell returns.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app", "tests"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import oasis_random as orx           # noqa: E402
import test_oasis_random as T        # noqa: E402


def _reader(tmp_path):
    p = tmp_path / "h.oas"
    p.write_bytes(T._build_hierarchy([(x * 40, y * 40)
                                      for y in range(3) for x in range(3)]))
    return orx.RandomAccessReader(p, wanted_layers={(17, 0)})


def test_decode_cell_cleared_after_load(tmp_path):
    r = _reader(tmp_path)
    assert r._decode_cell is None            # nothing in flight at rest
    r.load_cell(0)
    assert r._decode_cell is None            # cleared in the finally, no leak


def test_decode_tick_records_progress(tmp_path):
    # _decode_tick just publishes the counter the UI poller reads; calling it
    # updates _decode_records and never raises regardless of debug state.
    r = _reader(tmp_path)
    r._decode_cell = 0
    r._decode_t0 = r._decode_hb_t = 0.0
    r._decode_tick(50_000)
    assert r._decode_records == 50_000


def test_load_still_correct_with_heartbeat(tmp_path):
    # The instrumented decode must produce the same geometry the walk expects.
    r = _reader(tmp_path)
    roi = (-50, -50, 3 * 40 + 50, 3 * 40 + 50)
    res = orx.walk_roi(r, 0, roi, 17, 0)
    assert res["rects"].shape[0] > 0
    assert r._decode_cell is None


def test_records_decoded_total_counts_fresh_only(tmp_path):
    # F27 M7i: the export warm loop watches this to know when it decoded the giant
    # cell. It must grow on a FRESH decode and NOT on a memo re-hit.
    r = _reader(tmp_path)
    assert r._records_decoded_total == 0
    r.load_cell(0)                      # fresh decode of the root
    after_first = r._records_decoded_total
    assert after_first > 0
    r.load_cell(0)                      # memo hit → no change
    assert r._records_decoded_total == after_first
