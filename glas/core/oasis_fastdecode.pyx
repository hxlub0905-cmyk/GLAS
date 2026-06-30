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

# Native version tag so callers / the selftest can confirm which build loaded.
# 1 = varint helpers only (M0); 2 = + decode_rect_run (M2a-core); 3 = rect-run
# rewinds on a repetition *before* mutating modal state; 4 = decode_rect_run
# gained the ``started`` flag (continue a known-layer run, stopping on any
# change) — both required by M2a-integrate's per-cell gobble.
VERSION = 4


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
    return VERSION
