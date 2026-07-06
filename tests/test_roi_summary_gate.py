"""F28 (review fix): the M7l per-layer walk summary is level-1 only when the
layer was actually slow OR produced a NEW decode error this walk. A batch worker
reuses one RandomAccessReader across images and ``rar.errors`` is append-only, so
gating on the cumulative total would force every later fast layer back to level 1
after a single bad cell — flooding --debug for the rest of a long export. Guard
that the gate uses the per-walk delta.
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "tests"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import oasis_random as orx           # noqa: E402
import test_oasis_random as T        # noqa: E402


def _fast_reader(tmp_path):
    p = tmp_path / "h.oas"
    p.write_bytes(T._build_hierarchy([(x * 40, y * 40)
                                      for y in range(2) for x in range(2)]))
    return orx.RandomAccessReader(p, wanted_layers={(17, 0)})


def _summary_lines(stderr_text: str) -> list:
    return [ln for ln in stderr_text.splitlines() if "17/0 in" in ln]


def test_fast_layer_demoted_despite_cumulative_errors(tmp_path):
    orx.set_debug(True, 1)
    try:
        r = _fast_reader(tmp_path)
        # simulate a prior image in this worker having hit a bad cell
        r.errors.append(("prior_cell", 0, "pre-existing decode error"))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            orx.walk_roi(r, 0, (-50, -50, 130, 130), 17, 0)
        # fast walk + NO new error this walk → summary stays at level 2 (absent).
        assert _summary_lines(buf.getvalue()) == []
    finally:
        orx.set_debug(False)


def test_fast_layer_with_no_errors_still_demoted(tmp_path):
    # baseline: a clean fast walk is demoted (the M7l behaviour we must preserve).
    orx.set_debug(True, 1)
    try:
        r = _fast_reader(tmp_path)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            orx.walk_roi(r, 0, (-50, -50, 130, 130), 17, 0)
        assert _summary_lines(buf.getvalue()) == []
    finally:
        orx.set_debug(False)
