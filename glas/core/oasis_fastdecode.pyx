# cython: language_level=3, boundscheck=False, wraparound=False
"""Optional native OASIS decode helpers (F26).

This is the compiled fast path for the pure-Python hot loop in
``oasis_streamer`` — the profiler (``tools/oas_profile.py``) showed 82–85% of
decode time is the byte-at-a-time varint loop + per-record dispatch, which is
exactly what native code removes.

It is **optional**: ``oasis_streamer`` imports it inside a ``try`` and falls
back to the pure-Python readers when it is absent, so the project still runs
with no build step / no compiler (tests, dev, cross-project reuse — CLAUDE.md
§6). The build is produced by GitHub Actions (no local compiler needed); the
resulting ``oasis_fastdecode.cp39-win_amd64.pyd`` is dropped into ``glas/core``.

M0 (this file) is the proof-of-mechanism: a correct, round-trip-tested varint
decoder + a ``selftest()`` the user can run to confirm a CI-built ``.pyd``
imports and runs on a locked-down machine. The full record-loop port lands on
top of this once the delivery path is confirmed.
"""

import numpy as np
from libc.math cimport floor, ceil

# Native version tag so callers / the selftest can confirm which build loaded.
# 1 = varint helpers only (M0); 2 = + decode_rect_run (M2a-core); 3 = rect-run
# rewinds on a repetition *before* mutating modal state; 4 = decode_rect_run
# gained the ``started`` flag (continue a known-layer run, stopping on any
# change); 5 = + F27 M1 walk helpers (transform_rects_d4 / roi_overlap_mask);
# 6 = + F27 M3 native subtree walk (walk_rects_native) wired into walk_roi.
VERSION = 6


def decode_uvarint(const unsigned char[::1] buf, Py_ssize_t pos):
    """Unsigned varint (SEMI P39 §7.2): 7 payload bits/byte, LSB-first, bit 7 =
    continuation. Returns ``(value, new_pos)``. Byte-for-byte identical to
    ``OasisStream.read_uvarint`` for every value that fits in 64 bits (all real
    layout coordinates / counts); a value needing >64 bits raises like the
    Python overflow guard rather than silently wrapping."""
    cdef unsigned long long result = 0
    cdef int shift = 0
    cdef Py_ssize_t n = buf.shape[0]
    cdef unsigned int byte
    while True:
        if pos >= n:
            raise ValueError("unexpected EOF inside unsigned-int")
        byte = buf[pos]
        pos += 1
        if shift <= 63:
            result |= (<unsigned long long>(byte & 0x7F)) << shift
        elif (byte & 0x7F):
            # Bits beyond 64 — outside the range real OASIS coords ever use.
            # Defer to the pure-Python big-int reader instead of wrapping.
            raise OverflowError("unsigned-int exceeds 64 bits")
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            raise OverflowError("unsigned-int overflow")


def decode_svarint(const unsigned char[::1] buf, Py_ssize_t pos):
    """Signed varint (SEMI P39 §7.3): sign in the low bit of the unsigned
    representation. Returns ``(value, new_pos)``."""
    cdef unsigned long long raw
    cdef Py_ssize_t new_pos
    raw, new_pos = decode_uvarint(buf, pos)
    if raw & 1:
        return -(<long long>(raw >> 1)), new_pos
    return <long long>(raw >> 1), new_pos


def decode_rect_run(const unsigned char[::1] buf, Py_ssize_t pos,
                    long long layer, long long datatype,
                    long long w, long long h, long long x, long long y,
                    int xy_relative, int started=0):
    """Decode a *run* of consecutive RECTANGLE records that share one
    ``(layer, datatype)``, in one native call — the M2a amortization that the
    per-varint M1 attempt lacked. Inline-handles XYABSOLUTE / XYRELATIVE / PAD.

    Stops (returning control to Python, cursor rewound to the start of the
    stopping record) at: a RECTANGLE that changes layer/datatype, a RECTANGLE
    with a repetition, or any non-rectangle record. Byte/modal semantics are
    identical to ``oasis_streamer._read_rectangle`` + ``OasisStore`` rect store
    (``x1,y1,x2,y2 = x, y, x+w, y+h``).

    ``started`` selects how the *first* rectangle's layer is treated:

    * ``0`` (default) — the run has no established layer yet, so the first rect
      *adopts* whatever ``(layer, datatype)`` it carries (modal reuse keeps the
      passed-in values). Used when starting a run cold.
    * ``1`` — the passed-in ``(layer, datatype)`` is already the run's layer
      (e.g. the caller just decoded one rect of it in Python and wants the rest):
      a first rect on a *different* layer stops immediately (rewound), so the
      gobbled rects are guaranteed to all share the caller's ``(layer, dt)``.

    Returns ``(new_pos, rects, layer, datatype, w, h, x, y, xy_relative,
    stop_rid)`` where ``rects`` is an ``(N, 4)`` int64 array (x1,y1,x2,y2) and
    ``stop_rid`` is the id of the record that ended the run (-1 at EOF). On a
    >64-bit varint it raises OverflowError so the caller falls back to Python.
    """
    cdef Py_ssize_t n = buf.shape[0]
    cdef Py_ssize_t cap = 256
    arr = np.empty((cap, 4), dtype=np.int64)
    cdef long long[:, ::1] out = arr
    cdef Py_ssize_t count = 0
    cdef Py_ssize_t p = pos
    cdef Py_ssize_t rid_start
    cdef unsigned long long u
    cdef long long sval, nl, nd
    cdef int shift
    cdef unsigned int b, info, rid

    while True:
        rid_start = p
        if p >= n:
            return (p, arr[:count], layer, datatype, w, h, x, y,
                    xy_relative, -1)
        # ── rid (uvarint) ──
        u = 0; shift = 0
        while True:
            b = buf[p]; p += 1
            u |= (<unsigned long long>(b & 0x7F)) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift > 63 or p >= n:
                return (rid_start, arr[:count], layer, datatype, w, h, x, y,
                        xy_relative, -1)
        rid = <unsigned int>u

        if rid == 20:                      # RECTANGLE
            if p >= n:
                return (rid_start, arr[:count], layer, datatype, w, h, x, y,
                        xy_relative, -1)
            info = buf[p]; p += 1
            if info & 0x04:                # repetition → hand back *before*
                # touching any modal field. A repeated rect is decoded by the
                # Python path (it owns repetition); rewinding to rid_start with
                # the modal exactly as the last stored rect left it means the
                # caller can write the returned modal back verbatim and the
                # Python re-decode of this record stays correct (no double-apply
                # of a relative x/y). M2a-integrate relies on this invariant.
                return (rid_start, arr[:count], layer, datatype, w, h, x, y,
                        xy_relative, 20)
            nl = layer; nd = datatype
            if info & 0x01:                # layer
                u = 0; shift = 0
                while True:
                    if p >= n:
                        return (rid_start, arr[:count], layer, datatype, w, h,
                                x, y, xy_relative, -1)
                    b = buf[p]; p += 1
                    u |= (<unsigned long long>(b & 0x7F)) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                    if shift > 63:
                        raise OverflowError("layer > 64-bit")
                nl = <long long>u
            if info & 0x02:                # datatype
                u = 0; shift = 0
                while True:
                    if p >= n:
                        return (rid_start, arr[:count], layer, datatype, w, h,
                                x, y, xy_relative, -1)
                    b = buf[p]; p += 1
                    u |= (<unsigned long long>(b & 0x7F)) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                    if shift > 63:
                        raise OverflowError("datatype > 64-bit")
                nd = <long long>u
            # layer/datatype change after the run started → hand back to Python.
            if started and (nl != layer or nd != datatype):
                return (rid_start, arr[:count], layer, datatype, w, h, x, y,
                        xy_relative, 20)
            layer = nl; datatype = nd; started = 1
            if info & 0x40:                # width
                u = 0; shift = 0
                while True:
                    if p >= n:
                        return (rid_start, arr[:count], layer, datatype, w, h,
                                x, y, xy_relative, -1)
                    b = buf[p]; p += 1
                    u |= (<unsigned long long>(b & 0x7F)) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                    if shift > 63:
                        raise OverflowError("width > 64-bit")
                w = <long long>u
            if info & 0x80:                # square: height = width
                h = w
            elif info & 0x20:              # height
                u = 0; shift = 0
                while True:
                    if p >= n:
                        return (rid_start, arr[:count], layer, datatype, w, h,
                                x, y, xy_relative, -1)
                    b = buf[p]; p += 1
                    u |= (<unsigned long long>(b & 0x7F)) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                    if shift > 63:
                        raise OverflowError("height > 64-bit")
                h = <long long>u
            if info & 0x10:                # x (signed)
                u = 0; shift = 0
                while True:
                    if p >= n:
                        return (rid_start, arr[:count], layer, datatype, w, h,
                                x, y, xy_relative, -1)
                    b = buf[p]; p += 1
                    u |= (<unsigned long long>(b & 0x7F)) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                    if shift > 63:
                        raise OverflowError("x > 64-bit")
                sval = -(<long long>(u >> 1)) if (u & 1) else <long long>(u >> 1)
                x = (x + sval) if xy_relative else sval
            if info & 0x08:                # y (signed)
                u = 0; shift = 0
                while True:
                    if p >= n:
                        return (rid_start, arr[:count], layer, datatype, w, h,
                                x, y, xy_relative, -1)
                    b = buf[p]; p += 1
                    u |= (<unsigned long long>(b & 0x7F)) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                    if shift > 63:
                        raise OverflowError("y > 64-bit")
                sval = -(<long long>(u >> 1)) if (u & 1) else <long long>(u >> 1)
                y = (y + sval) if xy_relative else sval
            # (repetition is handled by the early-out above, before any modal
            # field is touched, so there's no late repetition check here.)
            if count == cap:               # grow the output buffer
                cap = cap * 2
                arr2 = np.empty((cap, 4), dtype=np.int64)
                arr2[:count] = arr[:count]
                arr = arr2
                out = arr
            out[count, 0] = x
            out[count, 1] = y
            out[count, 2] = x + w
            out[count, 3] = y + h
            count += 1
        elif rid == 15:                    # XYABSOLUTE
            xy_relative = 0
        elif rid == 16:                    # XYRELATIVE
            xy_relative = 1
        elif rid == 0:                     # PAD
            pass
        else:                              # any other record → hand back
            return (rid_start, arr[:count], layer, datatype, w, h, x, y,
                    xy_relative, <long long>rid)


def transform_rects_d4(const double[:, ::1] rects,
                       double m00, double m01, double m10, double m11,
                       double tx, double ty):
    """F27 M1: transform ``(N,4)`` rects ``(x1,y1,x2,y2)`` by a D4 affine
    ``v' = M v + t`` → new axis-aligned bboxes ``(N,4)`` float64, byte-identical
    to ``oasis_walker.Transform.apply_to_rects`` (the 2-corner D4 form:
    ``floor(min)`` / ``ceil(max)`` over the two transformed diagonal corners).

    This is the ROI walk's single hottest op (~40% with roi_overlap_mask); a C
    loop removes the per-call numpy dispatch that dominates the many tiny
    ``(1,4)`` / ``(K,4)`` calls. D4 only (the sole kind the walk builds)."""
    cdef Py_ssize_t n = rects.shape[0]
    out_arr = np.empty((n, 4), dtype=np.float64)
    cdef double[:, ::1] out = out_arr
    cdef Py_ssize_t i
    cdef double x1, y1, x2, y2, ax, ay, bx, by, xmin, xmax, ymin, ymax
    for i in range(n):
        x1 = rects[i, 0]; y1 = rects[i, 1]
        x2 = rects[i, 2]; y2 = rects[i, 3]
        # corner @ M.T + t, i.e. (m00*cx + m01*cy + tx, m10*cx + m11*cy + ty)
        ax = m00 * x1 + m01 * y1 + tx
        ay = m10 * x1 + m11 * y1 + ty
        bx = m00 * x2 + m01 * y2 + tx
        by = m10 * x2 + m11 * y2 + ty
        if ax < bx:
            xmin = ax; xmax = bx
        else:
            xmin = bx; xmax = ax
        if ay < by:
            ymin = ay; ymax = by
        else:
            ymin = by; ymax = ay
        out[i, 0] = floor(xmin)
        out[i, 1] = floor(ymin)
        out[i, 2] = ceil(xmax)
        out[i, 3] = ceil(ymax)
    return out_arr


def roi_overlap_mask(const double[:, ::1] boxes,
                     double r0, double r1, double r2, double r3):
    """F27 M1: boolean mask over ``(N,4)`` boxes overlapping ROI
    ``(r0,r1,r2,r3)`` — byte-identical to ``oasis_random._roi_overlap_mask``
    (normalizes each box's corners, then the 4 interval-overlap tests). Returns
    a numpy bool array. The C loop removes the per-call numpy dispatch of the
    5 vectorized ops the walk pays on every visit."""
    cdef Py_ssize_t n = boxes.shape[0]
    out_arr = np.empty(n, dtype=np.bool_)
    cdef unsigned char[::1] out = out_arr
    cdef Py_ssize_t i
    cdef double a, b, c, d, bx1, bx2, by1, by2
    for i in range(n):
        a = boxes[i, 0]; b = boxes[i, 2]
        if a < b:
            bx1 = a; bx2 = b
        else:
            bx1 = b; bx2 = a
        c = boxes[i, 1]; d = boxes[i, 3]
        if c < d:
            by1 = c; by2 = d
        else:
            by1 = d; by2 = c
        out[i] = (1 if (bx1 <= r2 and bx2 >= r0 and by1 <= r3 and by2 >= r1)
                  else 0)
    return out_arr


def walk_rects_native(const double[:, ::1] rect_coords,
                      const long long[::1] rect_off,
                      const long long[::1] pl_target,
                      const double[:, ::1] pl_M,
                      const double[:, ::1] pl_t,
                      const long long[::1] pl_off,
                      const double[:, ::1] reach_bbox,
                      long long root_index,
                      double r0, double r1, double r2, double r3,
                      int max_depth):
    """F27 M3a spike: native explicit-stack DFS over a *flattened* cell graph,
    emitting D4-transformed rects that hit the ROI. Supports only the common
    case (rect geometry, K=1 no-repetition placements, single wanted layer, D4);
    the Python walk owns everything else. Byte-identical emitted-rect SET to
    ``walk_roi`` on that case (order differs — LIFO stack vs recursion — so
    compare sorted). Proves the ceiling before the full M3 port."""
    cdef Py_ssize_t out_cap = 1024, out_n = 0
    out_arr = np.empty((out_cap, 4), dtype=np.float64)
    cdef double[:, ::1] out = out_arr
    cdef Py_ssize_t st_cap = 1024, st_n = 0
    # stack row: ci, m00,m01,m10,m11, t0,t1, depth  (ci/depth carried as double)
    st_arr = np.empty((st_cap, 8), dtype=np.float64)
    cdef double[:, ::1] st = st_arr
    st[0, 0] = root_index
    st[0, 1] = 1; st[0, 2] = 0; st[0, 3] = 0; st[0, 4] = 1
    st[0, 5] = 0; st[0, 6] = 0; st[0, 7] = 0
    st_n = 1
    cdef Py_ssize_t ci, p, r, child
    cdef double m00, m01, m10, m11, t0, t1
    cdef int depth
    cdef double x1, y1, x2, y2, ax, ay, bx, by, xmin, xmax, ymin, ymax
    cdef double bm00, bm01, bm10, bm11, bt0, bt1
    cdef double cm00, cm01, cm10, cm11, ct0, ct1
    while st_n > 0:
        st_n -= 1
        ci = <Py_ssize_t>st[st_n, 0]
        m00 = st[st_n, 1]; m01 = st[st_n, 2]; m10 = st[st_n, 3]; m11 = st[st_n, 4]
        t0 = st[st_n, 5]; t1 = st[st_n, 6]; depth = <int>st[st_n, 7]
        # ── emit this cell's rects (D4 2-corner bbox + exact ROI mask) ──
        for r in range(rect_off[ci], rect_off[ci + 1]):
            x1 = rect_coords[r, 0]; y1 = rect_coords[r, 1]
            x2 = rect_coords[r, 2]; y2 = rect_coords[r, 3]
            ax = m00 * x1 + m01 * y1 + t0; ay = m10 * x1 + m11 * y1 + t1
            bx = m00 * x2 + m01 * y2 + t0; by = m10 * x2 + m11 * y2 + t1
            if ax < bx:
                xmin = ax; xmax = bx
            else:
                xmin = bx; xmax = ax
            if ay < by:
                ymin = ay; ymax = by
            else:
                ymin = by; ymax = ay
            xmin = floor(xmin); ymin = floor(ymin)
            xmax = ceil(xmax); ymax = ceil(ymax)
            if xmin <= r2 and xmax >= r0 and ymin <= r3 and ymax >= r1:
                if out_n == out_cap:
                    out_cap = out_cap * 2
                    out2 = np.empty((out_cap, 4), dtype=np.float64)
                    out2[:out_n] = out_arr[:out_n]
                    out_arr = out2; out = out_arr
                out[out_n, 0] = xmin; out[out_n, 1] = ymin
                out[out_n, 2] = xmax; out[out_n, 3] = ymax
                out_n += 1
        if depth >= max_depth:
            continue
        # ── descend placements (compose transform, prune child by reach bbox) ──
        for p in range(pl_off[ci], pl_off[ci + 1]):
            child = <Py_ssize_t>pl_target[p]
            bm00 = pl_M[p, 0]; bm01 = pl_M[p, 1]
            bm10 = pl_M[p, 2]; bm11 = pl_M[p, 3]
            bt0 = pl_t[p, 0]; bt1 = pl_t[p, 1]
            cm00 = m00 * bm00 + m01 * bm10; cm01 = m00 * bm01 + m01 * bm11
            cm10 = m10 * bm00 + m11 * bm10; cm11 = m10 * bm01 + m11 * bm11
            ct0 = m00 * bt0 + m01 * bt1 + t0
            ct1 = m10 * bt0 + m11 * bt1 + t1
            x1 = reach_bbox[child, 0]; y1 = reach_bbox[child, 1]
            x2 = reach_bbox[child, 2]; y2 = reach_bbox[child, 3]
            ax = cm00 * x1 + cm01 * y1 + ct0; ay = cm10 * x1 + cm11 * y1 + ct1
            bx = cm00 * x2 + cm01 * y2 + ct0; by = cm10 * x2 + cm11 * y2 + ct1
            if ax < bx:
                xmin = ax; xmax = bx
            else:
                xmin = bx; xmax = ax
            if ay < by:
                ymin = ay; ymax = by
            else:
                ymin = by; ymax = ay
            xmin = floor(xmin); ymin = floor(ymin)
            xmax = ceil(xmax); ymax = ceil(ymax)
            if xmin <= r2 and xmax >= r0 and ymin <= r3 and ymax >= r1:
                if st_n == st_cap:
                    st_cap = st_cap * 2
                    st2 = np.empty((st_cap, 8), dtype=np.float64)
                    st2[:st_n] = st_arr[:st_n]
                    st_arr = st2; st = st_arr
                st[st_n, 0] = child
                st[st_n, 1] = cm00; st[st_n, 2] = cm01
                st[st_n, 3] = cm10; st[st_n, 4] = cm11
                st[st_n, 5] = ct0; st[st_n, 6] = ct1
                st[st_n, 7] = depth + 1
                st_n += 1
    return out_arr[:out_n]


def selftest():
    """Tiny smoke check so a user can confirm a CI-built .pyd actually loaded
    and runs: ``python -c "import oasis_fastdecode as f; print(f.selftest())"``.
    Returns the native VERSION on success; raises if the math is wrong."""
    # 300 = 0xAC 0x02 as a uvarint (0x2C|0x80, then 0x02).
    v, p = decode_uvarint(b"\xac\x02", 0)
    assert v == 300 and p == 2, (v, p)
    # svarint: sign in low bit. raw 0x09 = (4<<1)|1 -> -4; raw 0x0B -> -5.
    s, p = decode_svarint(b"\x09", 0)
    assert s == -4 and p == 1, (s, p)
    s, p = decode_svarint(b"\x0b", 0)
    assert s == -5 and p == 1, (s, p)
    # F27 M1 walk helpers: 90deg CCW of rect (0,0,10,20) -> bbox (-20,0,0,10).
    r = np.array([[0.0, 0.0, 10.0, 20.0]])
    o = transform_rects_d4(r, 0.0, -1.0, 1.0, 0.0, 0.0, 0.0)
    assert (o[0, 0] == -20 and o[0, 1] == 0
            and o[0, 2] == 0 and o[0, 3] == 10), o
    m = roi_overlap_mask(np.array([[0.0, 0.0, 5.0, 5.0],
                                   [100.0, 100.0, 110.0, 110.0]]),
                         1.0, 1.0, 3.0, 3.0)
    assert bool(m[0]) and not bool(m[1]), m
    return VERSION
