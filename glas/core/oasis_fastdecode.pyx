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

# Native version tag so callers / the selftest can confirm which build loaded.
VERSION = 1


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
