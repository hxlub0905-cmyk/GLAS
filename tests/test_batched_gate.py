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
    """Only the attributes the gate reads."""
    def __init__(self, n_cells: int, bbox_layer, *, sbbox: bool = False) -> None:
        self._by_refnum = {i: None for i in range(n_cells)}
        self._bbox_layer = bbox_layer
        self._sbbox_by_refnum = {0: (0, 0, 1, 1)} if sbbox else {}
        self._sbbox_by_name = {}


def test_small_no_sbbox_no_ce_is_affordable():
    # Small chip, no sbbox, no CE bbox_layer: full-decode topo build still cheap.
    assert orx._batched_walk_affordable(_Fake(100, None)) is True


def test_no_sbbox_ce_layer_makes_it_affordable_even_when_large():
    # No sbbox but a CE bbox_layer lets load_cell_bbox early-stop (the E3B case).
    assert orx._batched_walk_affordable(_Fake(50_000, (108, 250))) is True


def test_big_no_sbbox_no_ce_is_not_affordable():
    # Many cells, no sbbox, no CE -> topo build would decode the whole file.
    assert orx._batched_walk_affordable(_Fake(44_997, None)) is False


def test_sbbox_present_is_never_affordable():
    # THE LTV bug: sbbox present + bbox_layer configured but CE absent. walk_roi
    # is already decode-free-pruned; the batched topo build would full-decode
    # thousands of cells. Must fall back to walk_roi regardless of bbox_layer.
    assert orx._batched_walk_affordable(
        _Fake(44_997, (108, 250), sbbox=True)) is False
    assert orx._batched_walk_affordable(
        _Fake(500, (108, 250), sbbox=True)) is False


def test_huge_cell_count_never_affordable():
    # Past the hard cap the whole-graph build is too big even with a CE layer.
    assert orx._batched_walk_affordable(_Fake(200_000, (108, 250))) is False


def test_flatten_prewarm_skipped_on_big_sbbox_chip():
    # The LTV chip: big + sbbox → the export must skip the whole-chip flatten
    # prewarm (it aborts on the giant merge cell after ~90 s).
    assert orx.native_flatten_worthwhile(_Fake(44_997, (108, 250), sbbox=True)) is False


def test_flatten_prewarm_kept_for_no_sbbox_and_small():
    # E3B (no sbbox) and small chips still prewarm as before.
    assert orx.native_flatten_worthwhile(_Fake(13_000, (108, 250))) is True     # no sbbox
    assert orx.native_flatten_worthwhile(_Fake(500, (108, 250), sbbox=True)) is True  # small
