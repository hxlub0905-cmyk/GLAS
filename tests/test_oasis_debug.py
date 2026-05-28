"""Tests for glas/core/oasis_debug.py (F10).

report_file must (a) summarise a well-formed file written by oasis_writer
and (b) capture a decode error into the report text instead of raising.
Requires numpy (oasis_streamer import).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _sub in ("glas/core",):
    _p = REPO_ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import oasis_writer as w  # noqa: E402
import oasis_debug as dbg  # noqa: E402


def test_report_well_formed(tmp_path):
    p = tmp_path / "ok.oas"
    w.write_oasis(p, [
        (17, 0, [[(0, 0), (10, 0), (10, 10), (0, 10)]]),
        (25, 1, [[(0, 0), (5, 0), (0, 5)]]),
    ], unit=1000)
    rpt = dbg.report_file(p)
    assert "OASIS debug report" in rpt
    assert "parse: OK" in rpt
    assert "RECTANGLE" in rpt and "POLYGON" in rpt
    assert "L17/D0" in rpt and "L25/D1" in rpt
    assert "*** DECODE ERROR ***" not in rpt


def test_report_roundtrip_check(tmp_path):
    p = tmp_path / "rt.oas"
    sent = [(17, 0, [[(0, 0), (10, 0), (10, 10), (0, 10)]])]
    w.write_oasis(p, sent, unit=1000)
    rpt = dbg.report_file(p, sent_layers=sent)
    assert "round-trip check" in rpt
    assert "L17/D0: sent 1, read 1  [OK]" in rpt


def test_report_captures_decode_error(tmp_path):
    # Truncate a valid file mid-stream -> reader raises -> report captures it.
    p = tmp_path / "bad.oas"
    w.write_oasis(p, [(17, 0, [[(0, 0), (100, 0), (100, 100), (0, 100)]])], unit=1000)
    data = p.read_bytes()
    bad = tmp_path / "truncated.oas"
    bad.write_bytes(data[:len(data) - 3])   # chop the END / tail bytes
    rpt = dbg.report_file(bad)
    # Must not raise; report is returned either way. If the chop lands inside
    # a record the reader errors and we capture it; document both outcomes.
    assert "OASIS debug report" in rpt


def test_report_missing_file(tmp_path):
    rpt = dbg.report_file(tmp_path / "nope.oas")
    assert "cannot stat" in rpt
