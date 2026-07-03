"""F27 M7: the polygon point-list TYPE histogram (a feasibility probe for a
future native polygon decoder). Counting must be opt-in (zero cost off), count
only POLYGON point-lists (not PATH), attribute to the right type, and never
change the decoded points.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import oasis_streamer as oas          # noqa: E402
import oasis_random as orx            # noqa: E402

# point-list bytes: type, n, n deltas.  type0=Manhattan h-first, type1=v-first.
_TYPE0 = b"\x00\x02\x0a\x06"          # n=2, +5(x) +3(y)
_TYPE1 = b"\x01\x02\x0a\x06"          # n=2, +5(y) +3(x)


def _run(fn):
    saved = oas.PTYPE_COUNT_ON
    try:
        fn()
    finally:
        oas.PTYPE_COUNT_ON = saved


def test_polygon_types_counted_when_enabled():
    def body():
        oas.PTYPE_COUNT_ON = True
        orx.reset_poly_ptype_counts()
        oas.decode_point_list(io.BytesIO(_TYPE0), for_polygon=True)
        oas.decode_point_list(io.BytesIO(_TYPE0), for_polygon=True)
        oas.decode_point_list(io.BytesIO(_TYPE1), for_polygon=True)
        c = orx.poly_ptype_counts()
        assert c[0] == 2 and c[1] == 1
        assert sum(c.values()) == 3
        s = orx.poly_ptype_summary()
        assert "rectilinear 0/1 = 100%" in s
    _run(body)


def test_path_pointlists_not_counted():
    # PATH point-lists share the decoder but must not pollute the polygon probe.
    def body():
        oas.PTYPE_COUNT_ON = True
        orx.reset_poly_ptype_counts()
        oas.decode_point_list(io.BytesIO(_TYPE0), for_polygon=False)
        assert sum(orx.poly_ptype_counts().values()) == 0
    _run(body)


def test_off_by_default_is_zero_cost():
    def body():
        oas.PTYPE_COUNT_ON = False
        orx.reset_poly_ptype_counts()
        pts = oas.decode_point_list(io.BytesIO(_TYPE0), for_polygon=True)
        assert sum(orx.poly_ptype_counts().values()) == 0
        # decoded points are unaffected by the (disabled) counter
        assert pts[0] == (0, 0) and len(pts) >= 3
    _run(body)


def test_summary_when_no_polygons():
    def body():
        orx.reset_poly_ptype_counts()
        assert "no polygons" in orx.poly_ptype_summary()
    _run(body)
