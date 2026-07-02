"""F27 M7: the batched walk's topo build reads every reachable cell via
load_cell_bbox, which only early-stops with a CE bbox_layer. On a big file with
no CE layer (the 1750 MB LTV chip) that would full-decode the whole file just to
order the cells, so walk_roi_fast must fall back to the ROI-pruned walk_roi
there. _batched_walk_affordable is that gate.
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


class _Fake:
    """Only the two attributes the gate reads."""
    def __init__(self, n_cells: int, bbox_layer) -> None:
        self._by_refnum = {i: None for i in range(n_cells)}
        self._bbox_layer = bbox_layer


def test_small_no_ce_is_affordable():
    # Small chip, no CE bbox_layer: a full-decode topo build is still cheap.
    assert orx._batched_walk_affordable(_Fake(100, None)) is True


def test_ce_layer_makes_it_affordable_even_when_large():
    # A CE bbox_layer lets load_cell_bbox early-stop, so a large chip is fine.
    assert orx._batched_walk_affordable(_Fake(50_000, (108, 250))) is True


def test_big_no_ce_is_not_affordable():
    # The LTV case: many cells, no CE -> topo build would decode the whole file.
    assert orx._batched_walk_affordable(_Fake(44_997, None)) is False


def test_huge_cell_count_never_affordable():
    # Past the hard cap the whole-graph build is too big even with a CE layer.
    assert orx._batched_walk_affordable(_Fake(200_000, (108, 250))) is False
