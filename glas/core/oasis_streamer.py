"""Streaming OASIS reader (M1.9 + M1.10).

Why this exists
---------------
Production OASIS files in the 300 MB+ range crash gdstk on Windows
(STATUS_ACCESS_VIOLATION on user's D2DB sample) and stall klayout's
``Layout.read()`` for 10+ minutes without surface-level progress. Neither
backend gives us a viable path for the SEM-alignment use case where the
user expects to scan tens of GB layouts in the future. This module is
a self-contained streaming OASIS reader that holds bounded memory
regardless of file size and surfaces progress per record.

Scope after M1.10
-----------------
* Byte-level decoders per SEMI P39 §7:
    - decode_unsigned_int / decode_signed_int   (§7.2 / §7.3)
    - decode_real                                (§7.4 — eight type codes)
    - decode_string                              (§7.5 — length-prefixed)
    - decode_g_delta / decode_3_delta / decode_2_delta   (§7.7)
* Magic-byte check, START / END decoders, table-name records:
  CELLNAME (3/4), TEXTSTRING (5/6), PROPNAME (7/8), PROPSTRING (9/10),
  LAYERNAME (11/12), XNAME (30/31).
* CELL header (13/14) — modal state resets, ``current_cell`` is tracked
  so PLACEMENT records carry their parent cell.
* XYABSOLUTE / XYRELATIVE (15/16), PAD (0), PROPERTY (28/29).
* **M1.10 additions:**
    - PLACEMENT 17/18 (refnum-or-name + modal x/y + angle/mag/flip + repetition)
    - REPETITION (12 types, all returning an offset list)
    - CBLOCK 34 (raw deflate, transparently pushed onto an inner-stream stack)
    - XELEMENT 32 (skip body, keep sync)
    - ``OasisStream`` wrapper: a file-like that the rest of the reader uses
      so CBLOCK substreams disappear from the caller's perspective.

Anything else (TEXT, RECTANGLE, POLYGON, PATH, TRAPEZOID, CTRAPEZOID,
CIRCLE, XGEOMETRY) still raises ``OasisNotImplemented`` — that's the
next phase's (M1.11) territory.

Public surface
--------------
::

    reader = OasisReader(path)
    for record_id, payload in reader.iter_records():
        ...   # payload is a dict whose keys depend on record_id

When run as a script the module dumps a human-readable trace, which is
useful for smoke-testing the parser against a real OASIS file.

Reference: SEMI P39 OASIS specification, sections 7–10, 22, 27, 35.
"""
from __future__ import annotations

import argparse
import io
import mmap
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional


# ── Constants ────────────────────────────────────────────────────────────────


MAGIC = b"%SEMI-OASIS\r\n"

# Record IDs (SEMI P39 §9). Suffixes _IMP / _EXP / _NAME / _REFNUM follow
# the spec wording (implicit vs explicit reference number, by-name vs
# by-refnum cell header, etc.).
PAD              = 0
START            = 1
END              = 2
CELLNAME_IMP     = 3
CELLNAME_EXP     = 4
TEXTSTRING_IMP   = 5
TEXTSTRING_EXP   = 6
PROPNAME_IMP     = 7
PROPNAME_EXP     = 8
PROPSTRING_IMP   = 9
PROPSTRING_EXP   = 10
LAYERNAME_GEOM   = 11
LAYERNAME_TEXT   = 12
CELL_REFNUM      = 13
CELL_NAME        = 14
XYABSOLUTE       = 15
XYRELATIVE       = 16
PLACEMENT_NOMAG  = 17
PLACEMENT_MAG    = 18
TEXT             = 19
RECTANGLE        = 20
POLYGON          = 21
PATH             = 22
TRAPEZOID        = 23
TRAPEZOID_VR     = 24
TRAPEZOID_VL     = 25
CTRAPEZOID       = 26
CIRCLE           = 27
PROPERTY_NORMAL  = 28
PROPERTY_LAST    = 29
XNAME_IMP        = 30
XNAME_EXP        = 31
XELEMENT         = 32
XGEOMETRY        = 33
CBLOCK           = 34


RECORD_NAMES = {
    PAD:             "PAD",
    START:           "START",
    END:             "END",
    CELLNAME_IMP:    "CELLNAME(implicit)",
    CELLNAME_EXP:    "CELLNAME(explicit)",
    TEXTSTRING_IMP:  "TEXTSTRING(implicit)",
    TEXTSTRING_EXP:  "TEXTSTRING(explicit)",
    PROPNAME_IMP:    "PROPNAME(implicit)",
    PROPNAME_EXP:    "PROPNAME(explicit)",
    PROPSTRING_IMP:  "PROPSTRING(implicit)",
    PROPSTRING_EXP:  "PROPSTRING(explicit)",
    LAYERNAME_GEOM:  "LAYERNAME(geometry)",
    LAYERNAME_TEXT:  "LAYERNAME(text)",
    CELL_REFNUM:     "CELL(by refnum)",
    CELL_NAME:       "CELL(by name)",
    XYABSOLUTE:      "XYABSOLUTE",
    XYRELATIVE:      "XYRELATIVE",
    PLACEMENT_NOMAG: "PLACEMENT(no mag)",
    PLACEMENT_MAG:   "PLACEMENT(with mag)",
    TEXT:            "TEXT",
    RECTANGLE:       "RECTANGLE",
    POLYGON:         "POLYGON",
    PATH:            "PATH",
    TRAPEZOID:       "TRAPEZOID",
    TRAPEZOID_VR:    "TRAPEZOID(vr)",
    TRAPEZOID_VL:    "TRAPEZOID(vl)",
    CTRAPEZOID:      "CTRAPEZOID",
    CIRCLE:          "CIRCLE",
    PROPERTY_NORMAL: "PROPERTY",
    PROPERTY_LAST:   "PROPERTY(last)",
    XNAME_IMP:       "XNAME(implicit)",
    XNAME_EXP:       "XNAME(explicit)",
    XELEMENT:        "XELEMENT",
    XGEOMETRY:       "XGEOMETRY",
    CBLOCK:          "CBLOCK",
}


# ── Errors ───────────────────────────────────────────────────────────────────


class OasisFormatError(Exception):
    """Raised when bytes deviate from SEMI P39 or the stream is truncated."""


class OasisNotImplemented(Exception):
    """Raised when M1.9 hits a record whose decoder is M1.10 / M1.11 work.

    Carries ``record_id`` so the caller can decide whether to surface a
    progress checkpoint or just stop here."""

    def __init__(self, record_id: int, position: int) -> None:
        name = RECORD_NAMES.get(record_id, f"<id {record_id}>")
        super().__init__(
            f"record {name} (id {record_id}) at byte {position} "
            "not yet implemented (M1.11+)"
        )
        self.record_id = record_id
        self.position = position


# ── CBLOCK-aware stream wrapper (M1.10) ──────────────────────────────────────


class OasisStream:
    """Bytes-backed reader that transparently descends into CBLOCK substreams.

    CBLOCK records (SEMI P39 §35) wrap a slab of compressed (raw deflate)
    record bytes inside the outer file. Once decoded, the inner bytes are
    just more OASIS records — they need the same decoder loop the outer
    stream uses. Rather than thread a "current stream" parameter through
    every decoder, we replace the reader's file handle with this wrapper.
    When the iter_records loop hits a CBLOCK it calls ``push_cblock`` with
    the decompressed payload; subsequent reads come from that payload. As
    soon as it's exhausted, ``maybe_pop_exhausted`` returns control to the
    outer file.

    The previous implementation wrapped each substream in ``io.BytesIO``
    and routed every read through ``_top_io().read(n)``. Profiling
    (M1.13.3 sandbox, 200K synthetic RECTANGLE) showed two layers of
    Python method dispatch + the BufferedReader hop accounted for ~29% of
    decode time, with ``decode_unsigned_int`` adding another ~18% on top
    via its per-byte ``stream.read(1)`` calls. M1.13.3b drops both
    overheads by:

      1. Slurping the whole outer file into a ``bytes`` buffer at open
         time and walking it via integer ``self._pos`` (CBLOCK payloads
         are already in-memory bytes after zlib.decompress, so the same
         pattern extends naturally to nested substreams).
      2. Exposing ``read_byte`` / ``read_uvarint`` / ``read_svarint``
         hot-path methods so ``decode_unsigned_int`` (and any caller
         that takes a stream) walk the buffer directly instead of
         calling ``stream.read(1)`` in a loop.

    The legacy ``read`` / ``tell`` / ``seek`` / ``close`` API is kept
    for cold paths (string decoders, _error_context's hex window) and
    for any external caller that still treats the stream as a file.
    """

    def __init__(self, base: Optional[BinaryIO] = None, *,
                 use_mmap: bool = False, shared_buf: object = None) -> None:
        # ``shared_buf`` (F6 M2): wrap an existing, externally-owned buffer
        # (an mmap or bytes opened once by RandomAccessReader) with our own
        # ``_pos`` cursor. We do NOT own it, so close() only drops our
        # reference — the owner closes the real mapping. This lets the
        # offset-scan pass and the persistent ROI reader share a single map
        # of the file instead of mapping it twice.
        if shared_buf is not None:
            self._mmap = None
            self._base = None
            self._buf = shared_buf
            self._pos = 0
            self._closed = False
            self._stack: list[tuple[object, int]] = []
            return
        # Two backing strategies, selected by ``use_mmap``:
        #
        #  • slurp (default): read the whole file into a ``bytes`` buffer.
        #    Outer OASIS files are bounded by user RAM (300-500 MB on real
        #    D2DB masks); the in-memory cost trades for ~5× fewer Python
        #    method calls in the hot decoder. Best for the bulk full-decode
        #    path (oasis_store) which reads every byte anyway.
        #  • mmap (F6 M1): map the file read-only and walk it via ``_pos``.
        #    Best for the random-access ROI path, which only touches a few
        #    cells — the OS pages in just those, so a 345 MB file no longer
        #    costs 345 MB of RAM. ``buf[pos]`` (int) / ``buf[a:b]`` (bytes) /
        #    ``len(buf)`` are identical to bytes, so decoders are unchanged.
        #
        # mmap falls back to slurp transparently when ``base`` has no real
        # fileno (e.g. io.BytesIO in tests), the platform refuses, or the
        # file is empty — so behaviour is identical on those paths.
        self._mmap: Optional[mmap.mmap] = None
        self._base: Optional[BinaryIO] = None
        self._buf = b""
        if use_mmap:
            try:
                fd = base.fileno()
                self._mmap = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
                self._buf = self._mmap          # type: ignore[assignment]
                # Keep ``base`` open for the mapping's lifetime; closed in
                # close(). (CPython keeps the mapping valid past an fd close
                # on most platforms, but holding the handle is portable.)
                self._base = base
            except (ValueError, OSError, io.UnsupportedOperation, AttributeError):
                self._mmap = None
                self._base = None
                self._buf = b""
        if self._mmap is None:
            self._buf = base.read()
            # Close the OS file descriptor immediately so we never hold it
            # across long parser runs.
            try:
                base.close()
            except Exception:
                pass
        self._pos: int = 0
        self._closed: bool = False
        # Stack of (saved_buf, saved_pos) snapshots, taken when a CBLOCK
        # is pushed. The top of the stack holds the outer substream's
        # state we'll restore when the current inner substream drains.
        # (saved_buf is the mmap or a bytes payload; both index the same.)
        self._stack: list[tuple[object, int]] = []

    # ── File-like API (back-compat for cold paths) ─────────────────────────
    def read(self, n: int = -1) -> bytes:
        if self._closed:
            raise ValueError("read from closed OasisStream")
        buf = self._buf
        pos = self._pos
        if n < 0:
            result = buf[pos:]
            self._pos = len(buf)
        else:
            end = pos + n
            result = buf[pos:end]
            self._pos = end
        return result

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence == 0:
            new = pos
        elif whence == 1:
            new = self._pos + pos
        elif whence == 2:
            new = len(self._buf) + pos
        else:
            raise ValueError(f"invalid whence {whence}")
        if new < 0:
            raise OasisFormatError(f"seek to negative position {new}")
        self._pos = new
        return new

    def clear_substreams(self) -> None:
        """Drop any pending CBLOCK substream frames. Used before a random
        seek (M3.5b) so a previous partial decode that stopped inside a
        CBLOCK doesn't leave the substream stack dangling."""
        self._stack.clear()

    def close(self) -> None:
        self._stack.clear()
        # Drop the buffer reference so RAM is freed when the reader
        # closes. Subsequent reads will fail with ValueError, matching
        # the BinaryIO contract.
        self._buf = b""
        if self._mmap is not None:
            try:
                self._mmap.close()
            except Exception:
                pass
            self._mmap = None
        if self._base is not None:
            try:
                self._base.close()
            except Exception:
                pass
            self._base = None
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # ── Hot-path byte readers (M1.13.3b) ───────────────────────────────────
    def read_byte(self) -> int:
        """Read a single byte and return it as ``int`` in [0, 255].

        Replaces the common ``self._f.read(1)[0]`` pattern in hot
        decoders. Saves the per-byte ``bytes`` object allocation."""
        pos = self._pos
        if pos >= len(self._buf):
            raise OasisFormatError("unexpected EOF inside single-byte read")
        b = self._buf[pos]
        self._pos = pos + 1
        return b

    def read_uvarint(self) -> int:
        """Variable-length unsigned int (SEMI P39 §7.2), walking ``_buf``
        directly. Hot path for ``decode_unsigned_int``."""
        buf = self._buf
        pos = self._pos
        n = len(buf)
        result = 0
        shift = 0
        while True:
            if pos >= n:
                raise OasisFormatError("unexpected EOF inside unsigned-int")
            byte = buf[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                self._pos = pos
                return result
            shift += 7
            if shift > 70:
                raise OasisFormatError(
                    f"unsigned-int overflow at shift={shift}"
                )

    def read_svarint(self) -> int:
        """Variable-length signed int (SEMI P39 §7.3) — sign in low bit
        of the unsigned representation."""
        raw = self.read_uvarint()
        sign = raw & 1
        magnitude = raw >> 1
        return -magnitude if sign else magnitude

    # ── CBLOCK substream management ────────────────────────────────────────
    def push_cblock(self, data: bytes) -> None:
        """Switch active substream to ``data`` (the just-decompressed
        CBLOCK payload). The current ``(buf, pos)`` snapshot is pushed
        onto the stack and restored when the substream drains."""
        self._stack.append((self._buf, self._pos))
        self._buf = data
        self._pos = 0

    def maybe_pop_exhausted(self) -> int:
        """Pop every substream whose cursor has reached its end.

        Called between records by ``iter_records`` / ``consume`` so a
        CBLOCK's last record cleanly drains the inner stream and the
        next record comes from the outer file. Returns the pop count
        (zero in the common case)."""
        popped = 0
        while self._stack and self._pos >= len(self._buf):
            self._buf, self._pos = self._stack.pop()
            popped += 1
        return popped

    @property
    def cblock_depth(self) -> int:
        return len(self._stack)

    @property
    def outer_position(self) -> int:
        """Position in the outermost file, regardless of CBLOCK depth.

        While inside a CBLOCK, the outer file's "resume point" was
        captured in the bottom of the stack at push time. Outside any
        CBLOCK, the active buffer is the outer file and pos is the
        outer position directly."""
        if not self._stack:
            return self._pos
        return self._stack[0][1]


# ── Byte-level decoders (SEMI P39 §7) ────────────────────────────────────────


def decode_unsigned_int(stream) -> int:
    """Variable-length unsigned integer (§7.2).

    Each byte carries 7 payload bits in positions 0–6 (LSB first across
    bytes). Bit 7 is set on every byte except the last.

    Since M1.13.3b ``OasisStream`` walks an in-memory ``bytes`` buffer
    directly via ``read_uvarint`` — saves ~3-5× per-call overhead vs
    the old ``stream.read(1)`` byte-by-byte loop. For non-OasisStream
    callers (e.g. raw ``io.BytesIO`` in unit tests) we keep the slow
    path as a fallback so the signature stays backward compatible."""
    read_uvarint = getattr(stream, "read_uvarint", None)
    if read_uvarint is not None:
        return read_uvarint()
    # Fallback: byte-by-byte via stream.read(1). Only hit by unit tests
    # that hand us a raw BytesIO.
    result = 0
    shift = 0
    while True:
        b = stream.read(1)
        if not b:
            raise OasisFormatError("unexpected EOF inside unsigned-int")
        byte = b[0]
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result
        shift += 7
        if shift > 70:
            raise OasisFormatError(
                f"unsigned-int overflow at shift={shift}"
            )


def decode_signed_int(stream) -> int:
    """Variable-length signed integer (§7.3).

    Same continuation encoding as unsigned, but the low bit of the
    overall value is the sign (0 = positive, 1 = negative)."""
    read_svarint = getattr(stream, "read_svarint", None)
    if read_svarint is not None:
        return read_svarint()
    raw = decode_unsigned_int(stream)
    sign = raw & 1
    magnitude = raw >> 1
    return -magnitude if sign else magnitude


def decode_real(stream: BinaryIO) -> float:
    """Real number (§7.4). Type-code dispatches one of six encodings.

    Codes:
        0  positive integer            value = uint
        1  negative integer            value = -uint
        2  positive reciprocal         value = 1/uint
        3  negative reciprocal         value = -1/uint
        4  positive rational m/n       value = uint / uint
        5  negative rational -m/n      value = -uint / uint
        6  single-precision IEEE 754   little-endian, 4 bytes
        7  double-precision IEEE 754   little-endian, 8 bytes
    """
    code = decode_unsigned_int(stream)
    if code == 0:
        return float(decode_unsigned_int(stream))
    if code == 1:
        return float(-decode_unsigned_int(stream))
    if code == 2:
        n = decode_unsigned_int(stream)
        if n == 0:
            raise OasisFormatError("real type 2 (reciprocal) with zero denom")
        return 1.0 / n
    if code == 3:
        n = decode_unsigned_int(stream)
        if n == 0:
            raise OasisFormatError("real type 3 (-reciprocal) with zero denom")
        return -1.0 / n
    if code == 4:
        m = decode_unsigned_int(stream)
        n = decode_unsigned_int(stream)
        if n == 0:
            raise OasisFormatError("real type 4 (ratio) with zero denom")
        return m / n
    if code == 5:
        m = decode_unsigned_int(stream)
        n = decode_unsigned_int(stream)
        if n == 0:
            raise OasisFormatError("real type 5 (-ratio) with zero denom")
        return -m / n
    if code == 6:
        raw = stream.read(4)
        if len(raw) != 4:
            raise OasisFormatError("truncated IEEE 754 float (type 6)")
        return float(struct.unpack("<f", raw)[0])
    if code == 7:
        raw = stream.read(8)
        if len(raw) != 8:
            raise OasisFormatError("truncated IEEE 754 double (type 7)")
        return float(struct.unpack("<d", raw)[0])
    raise OasisFormatError(f"unknown real type code {code}")


def decode_string(stream: BinaryIO) -> bytes:
    """Length-prefixed byte string (§7.5).

    Returns the raw bytes (callers decode to ASCII / N-string as
    appropriate)."""
    n = decode_unsigned_int(stream)
    body = stream.read(n)
    if len(body) != n:
        raise OasisFormatError(
            f"truncated string (wanted {n}, got {len(body)})"
        )
    return body


# ── Interval decoder (used by LAYERNAME etc., §29.3) ─────────────────────────


def decode_interval(stream: BinaryIO) -> tuple[int, int]:
    """Decode an unsigned-interval — a ``(min, max)`` pair (SEMI P39 §29.3).

    Five forms. The earlier version of this decoder had the kinds wrong
    (most files use kind 3 and we expected 2 operands when the spec only
    has 1), which made every LAYERNAME on a real D2DB file desync.

        kind 0   (empty)    -> (0, INF)
        kind 1   (single)   -> (n, n)              read 1 uint
        kind 2   (0..n)     -> (0, n)              read 1 uint
        kind 3   (n..INF)   -> (n, INF)            read 1 uint
        kind 4   (n..m)     -> (n, m)              read 2 uints

    ``INF`` is returned as ``-1`` so callers can spot it without importing
    a sentinel.
    """
    INF = -1
    kind = decode_unsigned_int(stream)
    if kind == 0:
        return (0, INF)
    if kind == 1:
        n = decode_unsigned_int(stream)
        return (n, n)
    if kind == 2:
        n = decode_unsigned_int(stream)
        return (0, n)
    if kind == 3:
        n = decode_unsigned_int(stream)
        return (n, INF)
    if kind == 4:
        n = decode_unsigned_int(stream)
        m = decode_unsigned_int(stream)
        return (n, m)
    raise OasisFormatError(f"unknown interval kind {kind}")


# ── Geometric deltas (SEMI P39 §7.7) ─────────────────────────────────────────
#
# 8-direction unit vectors shared by all delta encodings. Octants are
# numbered as the spec does: 0=E, 1=N, 2=W, 3=S, 4=NE, 5=NW, 6=SW, 7=SE.
_OCT_DX = (1, 0, -1, 0, 1, -1, -1, 1)
_OCT_DY = (0, 1, 0, -1, 1, 1, -1, -1)


def decode_g_delta(stream: BinaryIO) -> tuple[int, int]:
    """Generic 2D signed displacement (§7.7.13).

    Two forms keyed by bit 0 of the leading uint:

    * **Form 1 (octangular)**: ``bit0 = 0``. Bits 1-3 = direction (8
      octants), bits 4+ = magnitude. Used when the delta is axis-aligned
      or on a 45° diagonal — the common case for cell-array vectors.
    * **Form 2 (arbitrary)**: ``bit0 = 1``. Bit 1 = x sign, bits 2+ =
      |x|. A separate signed-int follows for y. Handles any direction.
    """
    raw = decode_unsigned_int(stream)
    if (raw & 1) == 0:
        direction = (raw >> 1) & 0x07
        magnitude = raw >> 4
        return (_OCT_DX[direction] * magnitude,
                _OCT_DY[direction] * magnitude)
    x_sign = (raw >> 1) & 1
    x_mag = raw >> 2
    y = decode_signed_int(stream)
    return (-x_mag if x_sign else x_mag, y)


def decode_3_delta(stream: BinaryIO) -> tuple[int, int]:
    """Octangular delta (§7.7.12). One uint: bits 0-2 direction, rest magnitude."""
    raw = decode_unsigned_int(stream)
    direction = raw & 0x07
    magnitude = raw >> 3
    return (_OCT_DX[direction] * magnitude, _OCT_DY[direction] * magnitude)


def decode_2_delta(stream: BinaryIO) -> tuple[int, int]:
    """Manhattan delta (§7.7.11). 4 directions only (E/N/W/S)."""
    raw = decode_unsigned_int(stream)
    direction = raw & 0x03
    magnitude = raw >> 2
    return (_OCT_DX[direction] * magnitude, _OCT_DY[direction] * magnitude)


# ── Repetition (SEMI P39 §7.6) ───────────────────────────────────────────────


def read_repetition_raw(stream: BinaryIO) -> tuple[int, Optional[tuple]]:
    """Read one repetition record's bytes WITHOUT materializing offsets.

    Returns ``(rtype, raw)`` where ``raw`` is a compact tuple of the
    record's parameters — enough for :func:`expand_repetition` to rebuild
    the full ``(dx, dy)`` offset list later. Type 0 ("reuse modal
    repetition") returns ``(0, None)``; the caller substitutes from
    modal state.

    This is the byte-read half of the old monolithic ``decode_repetition``.
    Splitting expansion off matters because on a layer-filtered D2DB load
    99%+ of geometry records are discarded, and the consume() fast path
    never reads repetition offsets at all — so materializing the
    ``O(nx*ny)`` grid (the single biggest cost on production loads) was
    pure waste. We still read every byte here so the stream stays in
    sync; expansion happens only when a kept record's payload needs it.

    Element counts on the wire are stored as ``count - 2`` for grid-style
    types (a regular grid is always ≥ 2 elements per axis). Arbitrary-list
    types store ``count - 2`` then ``count - 1`` inter-element gaps.
    """
    ru = getattr(stream, "read_uvarint", None)
    if ru is None:
        def ru() -> int:
            return decode_unsigned_int(stream)

    rtype = ru()
    if rtype == 0:
        return (0, None)
    if rtype == 1:
        nx = ru() + 2
        ny = ru() + 2
        x_space = ru()
        y_space = ru()
        return (1, (nx, ny, x_space, y_space))
    if rtype == 2:
        nx = ru() + 2
        x_space = ru()
        return (2, (nx, x_space))
    if rtype == 3:
        ny = ru() + 2
        y_space = ru()
        return (3, (ny, y_space))
    if rtype == 4:
        nx = ru() + 2
        gaps = [ru() for _ in range(nx - 1)]
        return (4, (gaps,))
    if rtype == 5:
        nx = ru() + 2
        grid = ru()
        gaps = [ru() for _ in range(nx - 1)]
        return (5, (grid, gaps))
    if rtype == 6:
        ny = ru() + 2
        gaps = [ru() for _ in range(ny - 1)]
        return (6, (gaps,))
    if rtype == 7:
        ny = ru() + 2
        grid = ru()
        gaps = [ru() for _ in range(ny - 1)]
        return (7, (grid, gaps))
    if rtype == 8:
        nn = ru() + 2
        mm = ru() + 2
        n_vec = decode_g_delta(stream)
        m_vec = decode_g_delta(stream)
        return (8, (nn, mm, n_vec, m_vec))
    if rtype == 9:
        nd = ru() + 2
        d_vec = decode_g_delta(stream)
        return (9, (nd, d_vec))
    if rtype == 10:
        nd = ru() + 2
        deltas = [decode_g_delta(stream) for _ in range(nd - 1)]
        return (10, (deltas,))
    if rtype == 11:
        nd = ru() + 2
        grid = ru()
        deltas = [decode_g_delta(stream) for _ in range(nd - 1)]
        return (11, (grid, deltas))
    raise OasisFormatError(f"unknown repetition type {rtype}")


def expand_repetition(rtype: int,
                      raw: Optional[tuple]) -> list[tuple[int, int]]:
    """Materialize the full ``(dx, dy)`` offset list (including the origin
    ``(0, 0)``) from the ``raw`` params produced by
    :func:`read_repetition_raw`.

    Type 0 returns ``[]`` — modal reuse is resolved by the caller against
    its stored ``(rtype, raw)`` before reaching here, so a bare type 0
    never carries geometry.
    """
    if rtype == 0:
        return []
    if rtype == 1:
        nx, ny, x_space, y_space = raw
        return [(i * x_space, j * y_space)
                for j in range(ny) for i in range(nx)]
    if rtype == 2:
        nx, x_space = raw
        return [(i * x_space, 0) for i in range(nx)]
    if rtype == 3:
        ny, y_space = raw
        return [(0, j * y_space) for j in range(ny)]
    if rtype == 4:
        (gaps,) = raw
        offsets = [(0, 0)]
        x = 0
        for g in gaps:
            x += g
            offsets.append((x, 0))
        return offsets
    if rtype == 5:
        grid, gaps = raw
        offsets = [(0, 0)]
        x = 0
        for g in gaps:
            x += g * grid
            offsets.append((x, 0))
        return offsets
    if rtype == 6:
        (gaps,) = raw
        offsets = [(0, 0)]
        y = 0
        for g in gaps:
            y += g
            offsets.append((0, y))
        return offsets
    if rtype == 7:
        grid, gaps = raw
        offsets = [(0, 0)]
        y = 0
        for g in gaps:
            y += g * grid
            offsets.append((0, y))
        return offsets
    if rtype == 8:
        nn, mm, n_vec, m_vec = raw
        return [(i * n_vec[0] + j * m_vec[0],
                 i * n_vec[1] + j * m_vec[1])
                for j in range(mm) for i in range(nn)]
    if rtype == 9:
        nd, d_vec = raw
        return [(i * d_vec[0], i * d_vec[1]) for i in range(nd)]
    if rtype == 10:
        (deltas,) = raw
        offsets = [(0, 0)]
        x = y = 0
        for dx, dy in deltas:
            x += dx
            y += dy
            offsets.append((x, y))
        return offsets
    if rtype == 11:
        grid, deltas = raw
        offsets = [(0, 0)]
        x = y = 0
        for dx, dy in deltas:
            x += dx * grid
            y += dy * grid
            offsets.append((x, y))
        return offsets
    raise OasisFormatError(f"unknown repetition type {rtype}")


def decode_repetition(stream: BinaryIO) -> tuple[int, list[tuple[int, int]]]:
    """Backward-compatible read + expand in one call.

    Returns ``(rtype, offsets)`` exactly as the pre-M1.13.3c monolithic
    decoder did. Hot paths should prefer :func:`read_repetition_raw` +
    a deferred :func:`expand_repetition` so filtered-out records skip the
    ``O(N)`` expansion entirely.
    """
    rtype, raw = read_repetition_raw(stream)
    return (rtype, expand_repetition(rtype, raw))


def repetition_extent(rtype: int, raw):
    """Analytic ``(min_dx, min_dy, max_dx, max_dy)`` of a repetition's
    offsets WITHOUT materializing them (M3.5e). ``(0, 0, 0, 0)`` for type
    0 / None. Lets the ROI walker prune a million-instance array by its
    bounding extent instead of building the list."""
    if rtype is None or rtype == 0 or raw is None:
        return (0, 0, 0, 0)
    if rtype == 1:
        nx, ny, xs, ys = raw
        return _box(0, (nx - 1) * xs, 0, (ny - 1) * ys)
    if rtype == 2:
        nx, xs = raw
        return _box(0, (nx - 1) * xs, 0, 0)
    if rtype == 3:
        ny, ys = raw
        return _box(0, 0, 0, (ny - 1) * ys)
    if rtype in (4, 5):
        gaps = raw[0] if rtype == 4 else raw[1]
        grid = 1 if rtype == 4 else raw[0]
        total = sum(gaps) * grid
        return _box(0, total, 0, 0)
    if rtype in (6, 7):
        gaps = raw[0] if rtype == 6 else raw[1]
        grid = 1 if rtype == 6 else raw[0]
        total = sum(gaps) * grid
        return _box(0, 0, 0, total)
    if rtype == 8:
        nn, mm, n_vec, m_vec = raw
        xs = [0, (nn - 1) * n_vec[0], (mm - 1) * m_vec[0],
              (nn - 1) * n_vec[0] + (mm - 1) * m_vec[0]]
        ys = [0, (nn - 1) * n_vec[1], (mm - 1) * m_vec[1],
              (nn - 1) * n_vec[1] + (mm - 1) * m_vec[1]]
        return (min(xs), min(ys), max(xs), max(ys))
    if rtype == 9:
        nd, d_vec = raw
        return _box(0, (nd - 1) * d_vec[0], 0, (nd - 1) * d_vec[1])
    # types 10/11: arbitrary delta list (already materialized, bounded by
    # the explicit list length) — cheap to expand.
    offs = expand_repetition(rtype, raw)
    xs = [o[0] for o in offs]
    ys = [o[1] for o in offs]
    return (min(xs), min(ys), max(xs), max(ys))


def _box(xa: int, xb: int, ya: int, yb: int) -> tuple:
    """Build ``(min_dx, min_dy, max_dx, max_dy)`` from x/y endpoint pairs."""
    return (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))


def repetition_count(rtype: int, raw) -> int:
    """Number of instances a repetition produces, without materializing."""
    if rtype is None or rtype == 0 or raw is None:
        return 1
    if rtype == 1:
        return raw[0] * raw[1]
    if rtype == 2:
        return raw[0]
    if rtype == 3:
        return raw[0]
    if rtype in (4, 6):
        return len(raw[0]) + 1
    if rtype in (5, 7):
        return len(raw[1]) + 1
    if rtype == 8:
        return raw[0] * raw[1]
    if rtype == 9:
        return raw[0]
    if rtype in (10, 11):
        return len(raw[0] if rtype == 10 else raw[1]) + 1
    return 1


def repetition_offsets_np(rtype: int, raw) -> "np.ndarray":
    """Materialize repetition offsets as a numpy ``(M, 2)`` float64 array,
    using vectorized construction for the regular-grid types (1/2/3/8/9)
    that can explode to millions of instances. Falls back to
    :func:`expand_repetition` for the bounded arbitrary-list types."""
    import numpy as _np
    if rtype is None or rtype == 0 or raw is None:
        return _np.zeros((1, 2), dtype=_np.float64)
    if rtype == 1:
        nx, ny, xs, ys = raw
        i = _np.arange(nx, dtype=_np.float64) * xs
        j = _np.arange(ny, dtype=_np.float64) * ys
        gx, gy = _np.meshgrid(i, j)            # (ny, nx)
        return _np.column_stack((gx.ravel(), gy.ravel()))
    if rtype == 2:
        nx, xs = raw
        out = _np.zeros((nx, 2), dtype=_np.float64)
        out[:, 0] = _np.arange(nx) * xs
        return out
    if rtype == 3:
        ny, ys = raw
        out = _np.zeros((ny, 2), dtype=_np.float64)
        out[:, 1] = _np.arange(ny) * ys
        return out
    if rtype == 8:
        nn, mm, n_vec, m_vec = raw
        i = _np.arange(nn, dtype=_np.float64)
        j = _np.arange(mm, dtype=_np.float64)
        gi, gj = _np.meshgrid(i, j)
        x = gi * n_vec[0] + gj * m_vec[0]
        y = gi * n_vec[1] + gj * m_vec[1]
        return _np.column_stack((x.ravel(), y.ravel()))
    if rtype == 9:
        nd, d_vec = raw
        i = _np.arange(nd, dtype=_np.float64)
        return _np.column_stack((i * d_vec[0], i * d_vec[1]))
    return _np.asarray(expand_repetition(rtype, raw), dtype=_np.float64)


# ── Point-list (SEMI P39 §7.7.9, used by POLYGON / PATH) ─────────────────────


def decode_point_list(stream: BinaryIO,
                      for_polygon: bool) -> list[tuple[int, int]]:
    """Decode a point-list, returning the explicit point sequence relative
    to a (0, 0) anchor.

    The wire format is ``type (uint) + count (uint) + count deltas``. The
    type tells the decoder how each delta is encoded:

    * **type 0**: Manhattan zig-zag starting horizontally — each delta is
      a signed-int along the alternating x / y axis. For polygons (closed
      figures) an extra implicit point closes the path back to ``x`` or
      ``y == 0`` depending on the last axis.
    * **type 1**: Same as type 0 but starts vertical.
    * **type 2**: Each delta is a 2-delta (Manhattan, 4 directions).
    * **type 3**: Each delta is a 3-delta (octangular, 8 directions).
    * **type 4**: Each delta is a g-delta (generic 2D).
    * **type 5**: Each g-delta is a *velocity* increment — position
      accumulates the running velocity. Used to express curves where
      successive deltas have similar direction.

    Returns the full point list including the implicit ``(0, 0)`` first
    point (and the implicit closure for type 0/1 polygons).
    """
    ptype = decode_unsigned_int(stream)
    n = decode_unsigned_int(stream)
    if n == 0:
        raise OasisFormatError("point-list with zero count")
    pts: list[tuple[int, int]] = [(0, 0)]

    if ptype == 0 or ptype == 1:
        # Manhattan zig-zag. h=True means the next delta moves along x.
        h = (ptype == 0)
        x = y = 0
        for _ in range(n):
            d = decode_signed_int(stream)
            if h:
                x += d
            else:
                y += d
            h = not h
            pts.append((x, y))
        if for_polygon:
            # Auto-close: a Manhattan-zigzag polygon must have a final
            # implicit point that brings us back onto whichever axis the
            # first edge started on. klayout warns if n is odd; we mirror
            # that semantics here.
            if h:
                pts.append((0, y))
            else:
                pts.append((x, 0))
        return pts

    if ptype == 2:
        x = y = 0
        for _ in range(n):
            dx, dy = decode_2_delta(stream)
            x += dx
            y += dy
            pts.append((x, y))
        return pts

    if ptype == 3:
        x = y = 0
        for _ in range(n):
            dx, dy = decode_3_delta(stream)
            x += dx
            y += dy
            pts.append((x, y))
        return pts

    if ptype == 4:
        x = y = 0
        for _ in range(n):
            dx, dy = decode_g_delta(stream)
            x += dx
            y += dy
            pts.append((x, y))
        return pts

    if ptype == 5:
        # "Velocity" form: g-deltas accumulate into a vector, the vector
        # accumulates into the position. Two levels of integration.
        x = y = 0
        vx = vy = 0
        for _ in range(n):
            dx, dy = decode_g_delta(stream)
            vx += dx
            vy += dy
            x += vx
            y += vy
            pts.append((x, y))
        return pts

    raise OasisFormatError(f"unknown point-list type {ptype}")


# ── Modal state (SEMI P39 §10) ───────────────────────────────────────────────


@dataclass
class ModalState:
    """OASIS modal variables. M1.9 only writes ``xy_relative``; the rest
    are placeholders the geometry-record decoders in M1.11 will fill."""
    xy_relative: bool = False
    layer: int = 0
    datatype: int = 0
    geometry_x: int = 0
    geometry_y: int = 0
    geometry_w: int = 0
    geometry_h: int = 0
    placement_x: int = 0
    placement_y: int = 0
    placement_cell: Optional[int] = None
    text_x: int = 0
    text_y: int = 0
    polygon_point_list: list = field(default_factory=list)
    path_half_width: int = 0
    path_point_list: list = field(default_factory=list)
    # M1.10: repetition is shared across PLACEMENT / RECTANGLE / POLYGON / ...;
    # a record whose R bit is set with repetition-type 0 reuses this.
    repetition: Optional[tuple] = None
    # M1.11 additions: per-record modals so a follow-up record can omit a
    # bit and reuse the last value seen in this cell. Klayout calls these
    # ``mm_*`` modals; we mirror the names so a side-by-side review stays
    # easy.
    ctrapezoid_type: int = 0
    circle_radius: int = 0
    path_start_extension: int = 0
    path_end_extension: int = 0
    text_string: Optional[object] = None  # bytes (a-string) or int (refnum)
    text_layer: int = 0
    text_type: int = 0

    def reset_on_cell_boundary(self) -> None:
        """§10.2 - modals reset between cells."""
        self.xy_relative = False
        self.layer = 0
        self.datatype = 0
        self.geometry_x = 0
        self.geometry_y = 0
        self.geometry_w = 0
        self.geometry_h = 0
        self.placement_x = 0
        self.placement_y = 0
        self.placement_cell = None
        self.text_x = 0
        self.text_y = 0
        self.polygon_point_list = []
        self.path_half_width = 0
        self.path_point_list = []
        self.repetition = None
        self.ctrapezoid_type = 0
        self.circle_radius = 0
        self.path_start_extension = 0
        self.path_end_extension = 0
        self.text_string = None
        self.text_layer = 0
        self.text_type = 0


# ── Reader ───────────────────────────────────────────────────────────────────


class OasisReader:
    """Sequential record-level reader over an OASIS file.

    Construction opens the file and validates the magic. ``iter_records``
    is a generator that yields ``(record_id, payload_dict)`` pairs and
    advances modal state as it goes. The generator stops naturally on
    END, raises ``OasisNotImplemented`` if it encounters a record M1.9
    doesn't decode yet, or raises ``OasisFormatError`` on truncation /
    illegal bytes.
    """

    # Class-level defaults so test shims that bypass __init__ (and any
    # partially-constructed instance) still have these flags defined.
    _capture_prop_values = False
    _defer_rep = False

    def __init__(self, path: str | Path,
                 *,
                 wanted_layers: Optional[set[tuple[int, int]]] = None,
                 capture_prop_values: bool = False,
                 defer_repetition: bool = False,
                 use_mmap: bool = False,
                 shared_buf: object = None) -> None:
        """Open ``path`` and validate the OASIS magic.

        ``shared_buf`` (F6 M2): when given, read from this externally-owned
        buffer (an mmap/bytes the caller opened once) instead of opening the
        file. The reader does not own it; ``close()`` only drops the wrapper.

        ``wanted_layers``: optional filter. When provided, geometry records
        whose ``(layer, datatype)`` (or ``(text_layer, text_type)`` for
        TEXT) is not in the set are still **decoded** from the byte
        stream (so the cursor stays in sync) but the bulky data (point
        lists, etc.) is dropped before the payload is yielded. The
        payload still includes ``layer`` / ``datatype`` and a
        ``filtered_out: True`` flag so callers can keep counts without
        carrying megabytes of polygon data they will not use.

        When ``wanted_layers`` is ``None`` (the default) every geometry
        record is yielded with its full payload.
        """
        self._path = Path(path)
        # All decoders read through OasisStream so CBLOCK substreams stay
        # invisible to them. The wrapper exposes the same read/tell/seek
        # surface a plain file handle does.
        if shared_buf is not None:
            self._f = OasisStream(shared_buf=shared_buf)
        else:
            self._f = OasisStream(
                open(self._path, "rb"), use_mmap=use_mmap)
        self._modal = ModalState()
        self._last_record_start: int = 0
        # When True, PLACEMENT payloads carry the compact (rtype, raw)
        # repetition descriptor as ``repetition_raw`` and SKIP expanding
        # the full offset list — random-access ROI load (M3.5e) computes
        # the array extent analytically and only materializes offsets for
        # placements that survive ROI pruning, avoiding million-element
        # Python lists per cell.
        self._defer_rep = bool(defer_repetition)
        # When True, _read_property returns decoded values in payload
        # ["values"] (M3.5a: needed to read S_CELL_OFFSET byte offsets).
        # Default off keeps the hot path allocation-free.
        self._capture_prop_values = bool(capture_prop_values)
        # Tracks which cell the current PLACEMENT / geometry record belongs
        # to. Set whenever a CELL header is seen; None at top level.
        self._current_cell: Optional[object] = None
        # consume() stashes non-hot record payloads here so callbacks
        # have a uniform `cb(reader)` signature. Stays None for hot
        # records (RECTANGLE/POLYGON) — those callbacks pull from
        # reader._modal directly.
        self.last_payload: Optional[dict] = None
        self._wanted_layers = wanted_layers
        self._validate_magic()

    @property
    def last_record_start(self) -> int:
        return self._last_record_start

    @property
    def current_cell(self) -> Optional[object]:
        return self._current_cell

    @property
    def cblock_depth(self) -> int:
        return self._f.cblock_depth

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def __enter__(self) -> "OasisReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def modal(self) -> ModalState:
        return self._modal

    @property
    def position(self) -> int:
        return self._f.tell()

    # ── Internal: magic + record dispatch ──────────────────────────────────
    def _validate_magic(self) -> None:
        head = self._f.read(len(MAGIC))
        if head != MAGIC:
            raise OasisFormatError(
                f"missing OASIS magic at byte 0: got {head!r}"
            )

    def iter_records(self) -> Iterator[tuple[int, dict]]:
        # Local rebinds: the inner loop runs once per record. Even for the
        # GUI scan path that breaks out early, the load path on a 345 MB
        # D2DB hits this 700M+ times. Trading a global lookup for a local
        # attribute lookup saves ~7-10% of pure-Python parse time. The
        # bound-method local for ``read_uvarint`` skips the
        # ``decode_unsigned_int`` wrapper (which does a getattr probe to
        # find this same method anyway).
        f = self._f
        read_uvarint = f.read_uvarint
        decode_uint = decode_unsigned_int
        record_names = RECORD_NAMES

        while True:
            # Drain any CBLOCK substreams that just ran out — records never
            # straddle a CBLOCK boundary, so doing this between records is
            # the natural place to fall back to the outer file.
            f.maybe_pop_exhausted()

            # The old version did a read(1) + seek(-1) pair purely to
            # produce a friendly "EOF before END record" error. Drop it:
            # if the file is truncated mid-record decode_uint() raises
            # OasisFormatError("unexpected EOF inside unsigned-int") with
            # the same exit code, and we save two file ops per record
            # (worth ~3-4 min on the user's D2DB at 720M records).
            pos_before = f.tell()
            rid = read_uvarint()
            rid_pos = pos_before
            self._last_record_start = rid_pos  # for callers tracking sync

            payload: dict
            try:
                if rid == PAD:
                    payload = {}
                elif rid == START:
                    payload = self._read_start()
                elif rid == END:
                    payload = self._read_end()
                    yield END, payload
                    return
                elif rid in (CELLNAME_IMP, CELLNAME_EXP):
                    payload = self._read_cellname(explicit=(rid == CELLNAME_EXP))
                elif rid in (TEXTSTRING_IMP, TEXTSTRING_EXP):
                    payload = self._read_textstring(explicit=(rid == TEXTSTRING_EXP))
                elif rid in (PROPNAME_IMP, PROPNAME_EXP):
                    payload = self._read_propname(explicit=(rid == PROPNAME_EXP))
                elif rid in (PROPSTRING_IMP, PROPSTRING_EXP):
                    payload = self._read_propstring(explicit=(rid == PROPSTRING_EXP))
                elif rid in (LAYERNAME_GEOM, LAYERNAME_TEXT):
                    payload = self._read_layername()
                elif rid in (XNAME_IMP, XNAME_EXP):
                    payload = self._read_xname(explicit=(rid == XNAME_EXP))
                elif rid in (CELL_REFNUM, CELL_NAME):
                    self._modal.reset_on_cell_boundary()
                    payload = self._read_cell_header(by_name=(rid == CELL_NAME))
                    # Record which cell subsequent PLACEMENT / geometry
                    # records belong to.
                    self._current_cell = (
                        payload.get("name") if rid == CELL_NAME
                        else payload.get("refnum")
                    )
                elif rid == XYABSOLUTE:
                    self._modal.xy_relative = False
                    payload = {}
                elif rid == XYRELATIVE:
                    self._modal.xy_relative = True
                    payload = {}
                elif rid in (PROPERTY_NORMAL, PROPERTY_LAST):
                    payload = self._read_property(last=(rid == PROPERTY_LAST))
                elif rid in (PLACEMENT_NOMAG, PLACEMENT_MAG):
                    payload = self._read_placement(with_mag=(rid == PLACEMENT_MAG))
                    payload["in_cell"] = self._current_cell
                elif rid == CBLOCK:
                    payload = self._read_cblock()
                elif rid == XELEMENT:
                    payload = self._read_xelement()
                elif rid == RECTANGLE:
                    payload = self._read_rectangle()
                    payload["in_cell"] = self._current_cell
                elif rid == POLYGON:
                    payload = self._read_polygon()
                    payload["in_cell"] = self._current_cell
                elif rid == PATH:
                    payload = self._read_path()
                    payload["in_cell"] = self._current_cell
                elif rid in (TRAPEZOID, TRAPEZOID_VR, TRAPEZOID_VL):
                    payload = self._read_trapezoid(rid)
                    payload["in_cell"] = self._current_cell
                elif rid == CTRAPEZOID:
                    payload = self._read_ctrapezoid()
                    payload["in_cell"] = self._current_cell
                elif rid == CIRCLE:
                    payload = self._read_circle()
                    payload["in_cell"] = self._current_cell
                elif rid == TEXT:
                    payload = self._read_text()
                    payload["in_cell"] = self._current_cell
                else:
                    raise OasisNotImplemented(rid, rid_pos)
            except OasisFormatError as exc:
                # Decorate with record + byte-position context plus a hex
                # window of the surrounding bytes — the only practical way
                # to debug an off-by-one inside a record decoder.
                ctx = self._error_context(rid_pos)
                raise OasisFormatError(
                    f"{exc}\n  while decoding {RECORD_NAMES.get(rid, rid)} "
                    f"(id {rid}) starting at byte {rid_pos}\n  {ctx}"
                ) from exc

            yield rid, payload

    def consume(
        self,
        callbacks: dict[int, "Callable[[OasisReader], None]"],
        *,
        on_each: "Optional[Callable[[int, int], bool]]" = None,
    ) -> int:
        """Callback-driven counterpart of :meth:`iter_records`.

        Inner loop matches ``iter_records`` byte-for-byte but never
        builds a payload dict for the two hot geometry records
        (RECTANGLE / POLYGON) and never yields tuples — the two
        overheads payload-dict construction and generator state machine
        impose on a 5M-record D2DB load. Other record types still build
        their payload dict (placement / cell-header / table records are
        cumulatively << 1% of records on real masks, so the dict-alloc
        cost is negligible there) and stash it in ``self.last_payload``.

        ``callbacks`` maps a record id to ``cb(reader)``. The callback
        pulls fields from ``reader._modal`` for hot records or from
        ``reader.last_payload`` for everything else. Records without a
        registered callback are still decoded (needed to keep the
        stream cursor in sync) but the dispatch is a single dict get.

        ``on_each(rid, count)`` is invoked after every record (including
        those with no callback registered). Return ``False`` to stop the
        loop — used by store.run() for ``max_records`` cutoff and
        periodic progress callbacks.

        Returns the number of records processed (including the END
        record if reached).
        """
        f = self._f
        read_uvarint = f.read_uvarint
        decode_uint = decode_unsigned_int
        modal = self._modal
        get_cb = callbacks.get

        count = 0
        while True:
            f.maybe_pop_exhausted()

            rid_pos = f.tell()
            rid = read_uvarint()
            self._last_record_start = rid_pos

            try:
                if rid == PAD:
                    self.last_payload = None
                elif rid == START:
                    self.last_payload = self._read_start()
                elif rid == END:
                    self.last_payload = self._read_end()
                    count += 1
                    cb = get_cb(END)
                    if cb is not None:
                        cb(self)
                    if on_each is not None:
                        on_each(END, count)
                    return count
                elif rid in (CELLNAME_IMP, CELLNAME_EXP):
                    self.last_payload = self._read_cellname(
                        explicit=(rid == CELLNAME_EXP))
                elif rid in (TEXTSTRING_IMP, TEXTSTRING_EXP):
                    self.last_payload = self._read_textstring(
                        explicit=(rid == TEXTSTRING_EXP))
                elif rid in (PROPNAME_IMP, PROPNAME_EXP):
                    self.last_payload = self._read_propname(
                        explicit=(rid == PROPNAME_EXP))
                elif rid in (PROPSTRING_IMP, PROPSTRING_EXP):
                    self.last_payload = self._read_propstring(
                        explicit=(rid == PROPSTRING_EXP))
                elif rid in (LAYERNAME_GEOM, LAYERNAME_TEXT):
                    self.last_payload = self._read_layername()
                elif rid in (XNAME_IMP, XNAME_EXP):
                    self.last_payload = self._read_xname(
                        explicit=(rid == XNAME_EXP))
                elif rid in (CELL_REFNUM, CELL_NAME):
                    modal.reset_on_cell_boundary()
                    p = self._read_cell_header(by_name=(rid == CELL_NAME))
                    self.last_payload = p
                    self._current_cell = (
                        p.get("name") if rid == CELL_NAME
                        else p.get("refnum")
                    )
                elif rid == XYABSOLUTE:
                    modal.xy_relative = False
                    self.last_payload = None
                elif rid == XYRELATIVE:
                    modal.xy_relative = True
                    self.last_payload = None
                elif rid in (PROPERTY_NORMAL, PROPERTY_LAST):
                    self.last_payload = self._read_property(
                        last=(rid == PROPERTY_LAST))
                elif rid in (PLACEMENT_NOMAG, PLACEMENT_MAG):
                    self.last_payload = self._read_placement(
                        with_mag=(rid == PLACEMENT_MAG))
                elif rid == CBLOCK:
                    self.last_payload = self._read_cblock()
                elif rid == XELEMENT:
                    self.last_payload = self._read_xelement()
                elif rid == RECTANGLE:
                    self._read_rectangle(build_payload=False)
                elif rid == POLYGON:
                    self._read_polygon(build_payload=False)
                elif rid == PATH:
                    self.last_payload = self._read_path()
                elif rid in (TRAPEZOID, TRAPEZOID_VR, TRAPEZOID_VL):
                    self.last_payload = self._read_trapezoid(rid)
                elif rid == CTRAPEZOID:
                    self.last_payload = self._read_ctrapezoid()
                elif rid == CIRCLE:
                    self.last_payload = self._read_circle()
                elif rid == TEXT:
                    self.last_payload = self._read_text()
                else:
                    raise OasisNotImplemented(rid, rid_pos)
            except OasisFormatError as exc:
                ctx = self._error_context(rid_pos)
                raise OasisFormatError(
                    f"{exc}\n  while decoding {RECORD_NAMES.get(rid, rid)} "
                    f"(id {rid}) starting at byte {rid_pos}\n  {ctx}"
                ) from exc

            count += 1
            cb = get_cb(rid)
            if cb is not None:
                cb(self)
            if on_each is not None and on_each(rid, count) is False:
                return count

    def _error_context(self, record_start: int) -> str:
        """Return a hex window around the current cursor for diagnostics."""
        cur = self._f.tell()
        window_before = 8
        window_after = 16
        win_start = max(0, cur - window_before)
        save = self._f.tell()
        self._f.seek(win_start)
        chunk = self._f.read(window_before + window_after)
        self._f.seek(save)
        hex_pairs = " ".join(f"{b:02x}" for b in chunk)
        # Bytes line is `  bytes @ {win_start}..{win_end}: {hex_pairs}`.
        # The "^^" must land at the same column as the cursor byte in the
        # hex string, accounting for the variable-width "bytes @ ...: "
        # prefix.
        prefix = f"bytes @ {win_start}..{win_start + len(chunk)}: "
        cursor_col = (cur - win_start) * 3
        pointer = " " * (len(prefix) + cursor_col) + "^^"
        return (
            f"cursor at byte {cur} (record began at {record_start})\n"
            f"  {prefix}{hex_pairs}\n"
            f"  {pointer}"
        )

    # ── Per-record decoders ────────────────────────────────────────────────
    def _read_start(self) -> dict:
        version_b = decode_string(self._f)
        unit = decode_real(self._f)
        offset_flag = decode_unsigned_int(self._f)
        out = {
            "version": version_b,
            "unit": unit,
            "offset_flag": offset_flag,
        }
        if offset_flag == 0:
            # SEMI P39 §13.11: 12 uints arranged as 6 (strict, offset)
            # pairs — one pair per name table (CELLNAME / TEXTSTRING /
            # PROPNAME / PROPSTRING / LAYERNAME / XNAME). Earlier
            # iterations of this decoder mistakenly read 12 pairs (24
            # uints) and lost stream sync on real klayout-written files.
            offsets: list[tuple[int, int]] = []
            for _ in range(6):
                offsets.append((
                    decode_unsigned_int(self._f),   # strict flag
                    decode_unsigned_int(self._f),   # byte offset
                ))
            out["table_offsets"] = offsets
        return out

    def _read_end(self) -> dict:
        # END body: optional 6-pair offset table (only when START had
        # offset_flag == 1), then a validation-scheme uint (0/1/2) and an
        # optional 4-byte signature. We don't carry offset_flag across
        # iter_records, so peek the first uint: 0/1/2 -> scheme directly,
        # >2 -> offset table. Many writers PAD the END record afterwards
        # (e.g. with 0x80 bytes) to a fixed size; that padding is not a
        # valid uint, so guard every decode and stop gracefully when it
        # overflows — nothing follows END anyway.
        save = self._f.tell()
        try:
            peek = decode_unsigned_int(self._f)
        except OasisFormatError:
            self._f.seek(save)
            return {"validation_scheme": None, "padded": True}
        if peek > 2:
            self._f.seek(save)
            offsets = []
            try:
                for _ in range(6):
                    offsets.append((
                        decode_unsigned_int(self._f),
                        decode_unsigned_int(self._f),
                    ))
                scheme = decode_unsigned_int(self._f)
            except OasisFormatError:
                self._f.seek(save)
                return {"validation_scheme": None, "padded": True}
            sig = b""
            if scheme != 0:
                sig = self._f.read(4)
            return {
                "table_offsets": offsets,
                "validation_scheme": scheme,
                "validation_signature": sig,
            }
        scheme = peek
        sig = b""
        if scheme != 0:
            sig = self._f.read(4)
        return {"validation_scheme": scheme, "validation_signature": sig}

    def _read_cellname(self, *, explicit: bool) -> dict:
        name = decode_string(self._f)
        refnum: Optional[int] = None
        if explicit:
            refnum = decode_unsigned_int(self._f)
        return {"name": name, "refnum": refnum, "explicit": explicit}

    def _read_textstring(self, *, explicit: bool) -> dict:
        text = decode_string(self._f)
        refnum: Optional[int] = None
        if explicit:
            refnum = decode_unsigned_int(self._f)
        return {"text": text, "refnum": refnum, "explicit": explicit}

    def _read_propname(self, *, explicit: bool) -> dict:
        name = decode_string(self._f)
        refnum: Optional[int] = None
        if explicit:
            refnum = decode_unsigned_int(self._f)
        return {"name": name, "refnum": refnum, "explicit": explicit}

    def _read_propstring(self, *, explicit: bool) -> dict:
        value = decode_string(self._f)
        refnum: Optional[int] = None
        if explicit:
            refnum = decode_unsigned_int(self._f)
        return {"value": value, "refnum": refnum, "explicit": explicit}

    def _read_layername(self) -> dict:
        name = decode_string(self._f)
        layer_iv = decode_interval(self._f)
        datatype_iv = decode_interval(self._f)
        return {
            "name": name,
            "layer_interval": layer_iv,
            "datatype_interval": datatype_iv,
        }

    def _read_xname(self, *, explicit: bool) -> dict:
        attribute = decode_unsigned_int(self._f)
        name = decode_string(self._f)
        refnum: Optional[int] = None
        if explicit:
            refnum = decode_unsigned_int(self._f)
        return {
            "attribute": attribute,
            "name": name,
            "refnum": refnum,
            "explicit": explicit,
        }

    def _read_cell_header(self, *, by_name: bool) -> dict:
        if by_name:
            name = decode_string(self._f)
            return {"name": name}
        refnum = decode_unsigned_int(self._f)
        return {"refnum": refnum}

    # SEMI P39 §32 PROPERTY records. M1.9 only decodes enough to keep the
    # stream in sync — values are discarded. klayout writes a top-level
    # PROPERTY immediately after START on every file, so without this the
    # parser bails before reaching any name table.
    def _read_property(self, *, last: bool) -> dict:
        info_b = self._f.read(1)
        if len(info_b) != 1:
            raise OasisFormatError("truncated PROPERTY info byte")
        info = info_b[0]
        # Bit layout per §32.4:
        #   bits 7-4 (UUUU) value-count (15 = uint follows)
        #   bit 3     (V)   1 = use modal value list
        #   bit 2     (C)   1 = prop-name follows (else modal)
        #   bit 1     (N)   if C: 1 = name is refnum (uint), 0 = a-string
        #   bit 0           unused for record 28; for 29 marks "last in run"
        U = (info >> 4) & 0x0F
        V = bool(info & 0x08)
        C = bool(info & 0x04)
        N = bool(info & 0x02)

        propname: Optional[object] = None
        if C:
            if N:
                propname = decode_unsigned_int(self._f)   # refnum
            else:
                propname = decode_string(self._f)         # a-string

        n_values = 0
        values: list = []
        if not V:
            n_values = decode_unsigned_int(self._f) if U == 15 else U
            for _ in range(n_values):
                v = self._read_prop_value()
                if self._capture_prop_values:
                    values.append(v)

        return {
            "info_byte": info,
            "propname": propname,
            "value_count": n_values,
            "values": values,
            "last": last,
        }

    def _read_prop_value(self):
        """Decode one PROPERTY value, advancing the cursor and returning
        the decoded scalar (int / float / bytes). Callers that only need
        to keep the stream in sync ignore the return value.

        Value types per §32.6:
            0–7:   real (same type codes as decode_real)
            8:     unsigned int
            9:     signed int
            10/11/12:  a-/b-/n-string
            13/14/15:  propstring refnum (a-/b-/n-)
        """
        t = decode_unsigned_int(self._f)
        if t <= 7:
            # Real type codes (decode_real consumes its own type byte, which
            # we've already read here, so inline the per-code paths).
            if t == 0:
                return float(decode_unsigned_int(self._f))
            if t == 1:
                return -float(decode_unsigned_int(self._f))
            if t == 2:
                return 1.0 / decode_unsigned_int(self._f)
            if t == 3:
                return -1.0 / decode_unsigned_int(self._f)
            if t == 4:
                a = decode_unsigned_int(self._f)
                b = decode_unsigned_int(self._f)
                return a / b
            if t == 5:
                a = decode_unsigned_int(self._f)
                b = decode_unsigned_int(self._f)
                return -a / b
            if t == 6:
                raw = self._f.read(4)
                if len(raw) != 4:
                    raise OasisFormatError("truncated float32 prop value")
                return struct.unpack("<f", raw)[0]
            raw = self._f.read(8)              # t == 7
            if len(raw) != 8:
                raise OasisFormatError("truncated float64 prop value")
            return struct.unpack("<d", raw)[0]
        if t == 8:
            return decode_unsigned_int(self._f)
        if t == 9:
            return decode_signed_int(self._f)
        if t in (10, 11, 12):
            return decode_string(self._f)
        if t in (13, 14, 15):
            return ("propstring_ref", decode_unsigned_int(self._f))
        raise OasisFormatError(f"unknown prop value type {t}")

    # ── PLACEMENT 17/18 (SEMI P39 §22) ─────────────────────────────────────
    def _read_placement(self, *, with_mag: bool) -> dict:
        """Decode one PLACEMENT record.

        Info-byte layout (high → low bit):
            17 (no-mag): C N X Y R A A F
            18 (mag):    C N X Y R M A F

        * **C** (bit 7): if 1, a cell reference follows; if 0, use modal
          ``placement_cell``.
        * **N** (bit 6): only meaningful when C=1. ``0`` -> cell is encoded as
          an a-string (inline name); ``1`` -> cell is encoded as a refnum
          (unsigned-int pointing into the CELLNAME table). Same convention
          as PROPERTY: 1 == compact numeric form, 0 == verbose name form.
          (This is the convention klayout, gdstk, and the SEMI P39 §22
          spec text agree on; an earlier draft of this decoder had the
          two branches swapped, which made every production-sized D2DB
          desync at the first PLACEMENT.)
        * **X / Y** (bits 5,4): if set, a signed-int follows for that
          coordinate. In XYRELATIVE mode it's a delta added to the modal;
          in XYABSOLUTE mode it replaces the modal outright.
        * **R** (bit 3): if 1, a repetition record follows (with type 0
          meaning "reuse modal repetition").
        * Record 17 packs the rotation into bits 2-1 (``AA``) as a
          quarter-turn count: 0/1/2/3 → 0°/90°/180°/270°. Record 18 uses
          bit 2 (``M``) to flag a magnification real and bit 1 (``A``)
          to flag an arbitrary-angle real (degrees CCW).
        * **F** (bit 0): mirror about the x-axis applied *before* rotation.

        The result dict carries an interpreted view (resolved cell ref,
        absolute x/y after modal update, numeric angle/magnification, the
        repetition offset list if any). ``cell_ref_kind`` is included so a
        downstream cell resolver knows whether to look up a refnum or
        consume an inline name.
        """
        info_b = self._f.read(1)
        if len(info_b) != 1:
            raise OasisFormatError("truncated PLACEMENT info byte")
        info = info_b[0]

        C = bool(info & 0x80)
        N = bool(info & 0x40)
        X = bool(info & 0x20)
        Y = bool(info & 0x10)
        R = bool(info & 0x08)
        F = bool(info & 0x01)

        cell_ref: Optional[object]
        cell_ref_kind: str
        if C:
            if N:
                cell_ref = decode_unsigned_int(self._f)
                cell_ref_kind = "refnum"
            else:
                cell_ref = decode_string(self._f).decode("ascii", "backslashreplace")
                cell_ref_kind = "name"
            self._modal.placement_cell = cell_ref
        else:
            cell_ref = self._modal.placement_cell
            cell_ref_kind = "modal"

        if X:
            dx = decode_signed_int(self._f)
            if self._modal.xy_relative:
                self._modal.placement_x += dx
            else:
                self._modal.placement_x = dx
        if Y:
            dy = decode_signed_int(self._f)
            if self._modal.xy_relative:
                self._modal.placement_y += dy
            else:
                self._modal.placement_y = dy

        if with_mag:
            M_bit = bool(info & 0x04)
            A_bit = bool(info & 0x02)
            magnification = decode_real(self._f) if M_bit else 1.0
            angle = decode_real(self._f) if A_bit else 0.0
        else:
            magnification = 1.0
            angle = float(((info >> 1) & 0x03) * 90)

        rep_type: Optional[int] = None
        rep_raw: Optional[tuple] = None
        if R:
            rep_type, rep_raw = read_repetition_raw(self._f)
            if rep_type == 0:
                if self._modal.repetition is None:
                    raise OasisFormatError(
                        "PLACEMENT used modal repetition (type 0) but no "
                        "prior repetition has been set in this cell"
                    )
                rep_type, rep_raw = self._modal.repetition
            else:
                self._modal.repetition = (rep_type, rep_raw)

        payload = {
            "cell_ref": cell_ref,
            "cell_ref_kind": cell_ref_kind,
            "x": self._modal.placement_x,
            "y": self._modal.placement_y,
            "angle": angle,
            "magnification": magnification,
            "flip": F,
            "repetition_type": rep_type,
        }
        if self._defer_rep:
            # Compact descriptor; offsets materialized lazily by the caller.
            payload["repetition_raw"] = rep_raw
        else:
            payload["repetition_offsets"] = (
                [] if rep_type is None
                else expand_repetition(rep_type, rep_raw))
        return payload

    # ── CBLOCK 34 (SEMI P39 §35) ───────────────────────────────────────────
    def _read_cblock(self) -> dict:
        """Decode a CBLOCK header, decompress its payload, and push it as
        the active substream.

        ``comp_type`` 0 is the only defined value (raw deflate). After we
        push the substream, ``iter_records`` reads the next record from
        inside it; when the cursor reaches the substream's end the loop's
        ``maybe_pop_exhausted`` call drops us back into the outer file.

        We validate that ``zlib.decompress(..., wbits=-15)`` produces
        exactly ``uncompressed_byte_count`` bytes — a size mismatch
        almost always means we landed mid-stream rather than at a real
        CBLOCK header.
        """
        comp_type = decode_unsigned_int(self._f)
        if comp_type != 0:
            raise OasisFormatError(
                f"unsupported CBLOCK comp_type {comp_type} "
                "(only 0 = deflate is defined in SEMI P39 §35)"
            )
        uncompressed_count = decode_unsigned_int(self._f)
        compressed_count = decode_unsigned_int(self._f)

        comp_bytes = self._f.read(compressed_count)
        if len(comp_bytes) != compressed_count:
            raise OasisFormatError(
                f"truncated CBLOCK payload "
                f"(header says {compressed_count} bytes, got {len(comp_bytes)})"
            )

        try:
            decomp = zlib.decompress(comp_bytes, wbits=-15)
        except zlib.error as exc:
            raise OasisFormatError(f"CBLOCK deflate failed: {exc}") from exc

        if len(decomp) != uncompressed_count:
            raise OasisFormatError(
                "CBLOCK uncompressed size mismatch "
                f"(header={uncompressed_count}, deflate produced {len(decomp)})"
            )

        self._f.push_cblock(decomp)
        return {
            "comp_type": comp_type,
            "uncompressed_count": uncompressed_count,
            "compressed_count": compressed_count,
            "cblock_depth": self._f.cblock_depth,
        }

    # ── XELEMENT 32 (SEMI P39 §27) ─────────────────────────────────────────
    def _read_xelement(self) -> dict:
        """Skip an XELEMENT (attribute + b-string of opaque data).

        Standard files rarely contain these -- we decode just enough to
        keep the stream in sync."""
        attribute = decode_unsigned_int(self._f)
        data = decode_string(self._f)
        return {"attribute": attribute, "data_len": len(data)}

    # ── Geometry-record helpers (M1.11) ────────────────────────────────────
    #
    # Every geometry record (RECTANGLE / POLYGON / PATH / TRAPEZOID /
    # CTRAPEZOID / CIRCLE / TEXT) follows the same shape:
    #
    #   info-byte:  bit 0 (L), bit 1 (D), bit 2 (R), bit 3 (Y), bit 4 (X)
    #               + record-specific bits 5/6/7
    #   read order: layer, datatype, record-specific fields,
    #               x, y, repetition
    #
    # The shared bit positions (L/D/R/X/Y) match what klayout's
    # dbOASISReader.cc uses, which is the authoritative reference we
    # cross-checked after the M1.10 PLACEMENT N-bit bug.
    #
    # Each decoder updates modals when its bit is set, leaves them alone
    # when not (modal reuse). Coordinate fields obey XYABSOLUTE /
    # XYRELATIVE mode like PLACEMENT does.

    def _decode_xy(self, info: int) -> None:
        """Update modal geometry_x / geometry_y based on info-byte bits.

        Bit 4 (0x10) = X present, bit 3 (0x08) = Y present. Coordinate
        is signed-int; XYRELATIVE mode adds to the modal, XYABSOLUTE
        replaces it. Used by every geometry record that has X/Y.
        """
        if info & 0x10:
            x = decode_signed_int(self._f)
            if self._modal.xy_relative:
                self._modal.geometry_x += x
            else:
                self._modal.geometry_x = x
        if info & 0x08:
            y = decode_signed_int(self._f)
            if self._modal.xy_relative:
                self._modal.geometry_y += y
            else:
                self._modal.geometry_y = y

    def _decode_repetition_if_set(self, info: int) -> tuple[Optional[int],
                                                            Optional[tuple]]:
        """If bit 2 of info is set, read a repetition (raw, *unexpanded*)
        and handle type-0 modal reuse. Returns ``(rtype, raw)`` or
        ``(None, None)``.

        Expansion to the ``(dx, dy)`` offset list is deferred to
        :func:`expand_repetition` at the payload-build site, so the
        consume() fast path and filtered-out records never pay the
        ``O(nx*ny)`` cost. Modal state stores the raw params (not the
        expanded list) so type-0 reuse stays cheap too."""
        if not (info & 0x04):
            return (None, None)
        rtype, raw = read_repetition_raw(self._f)
        if rtype == 0:
            if self._modal.repetition is None:
                raise OasisFormatError(
                    "geometry record used modal repetition (type 0) but no "
                    "prior repetition has been set in this cell"
                )
            rtype, raw = self._modal.repetition
        else:
            self._modal.repetition = (rtype, raw)
        return (rtype, raw)

    def _layer_filtered_out(self, layer: int, datatype: int) -> bool:
        """True iff a ``wanted_layers`` filter is active AND this layer
        pair is NOT in the wanted set. Decoder still runs to keep the
        byte stream in sync; caller drops the heavy payload bits."""
        if self._wanted_layers is None:
            return False
        return (layer, datatype) not in self._wanted_layers

    # ── RECTANGLE 20 (SEMI P39 §23) ────────────────────────────────────────
    def _read_rectangle(self, *, build_payload: bool = True) -> Optional[dict]:
        """Info byte: ``SWHXYRDL`` (bit 7 -> bit 0).

        M1.13.3b: inlined fast-path byte reads (``f.read_byte`` /
        ``f.read_uvarint`` / ``f.read_svarint``) bypass the
        ``decode_unsigned_int`` getattr probe and the per-byte
        ``stream.read(1)`` overhead. 98%+ of D2DB records hit this
        decoder so the savings compound.


        ``S`` (0x80) makes the rectangle a square: H is not read; height
        takes the value of width. Otherwise W (0x40) and H (0x20) are
        independent uints. Cross-checked against klayout dbOASISReader.cc.
        """
        f = self._f
        read_uvarint = f.read_uvarint
        modal = self._modal
        info = f.read_byte()
        if info & 0x01:
            modal.layer = read_uvarint()
        if info & 0x02:
            modal.datatype = read_uvarint()
        if info & 0x40:
            modal.geometry_w = read_uvarint()
        if info & 0x80:
            # Square: height equals width, no extra read.
            modal.geometry_h = modal.geometry_w
        elif info & 0x20:
            modal.geometry_h = read_uvarint()
        if info & 0x10:
            x = f.read_svarint()
            if modal.xy_relative:
                modal.geometry_x += x
            else:
                modal.geometry_x = x
        if info & 0x08:
            y = f.read_svarint()
            if modal.xy_relative:
                modal.geometry_y += y
            else:
                modal.geometry_y = y
        rep_type, rep_raw = self._decode_repetition_if_set(info)

        # consume() callers skip the dict pack entirely — 98% of D2DB
        # records hit this path so removing 5M dict allocations is
        # measurable on full loads.
        if not build_payload:
            return None

        filtered = self._layer_filtered_out(self._modal.layer,
                                            self._modal.datatype)
        payload = {
            "layer": self._modal.layer,
            "datatype": self._modal.datatype,
            "width": self._modal.geometry_w,
            "height": self._modal.geometry_h,
            "x": self._modal.geometry_x,
            "y": self._modal.geometry_y,
            "repetition_type": rep_type,
            "filtered_out": filtered,
        }
        if not filtered:
            if self._defer_rep:
                payload["repetition_raw"] = rep_raw
            else:
                payload["repetition_offsets"] = (
                    [] if rep_type is None
                    else expand_repetition(rep_type, rep_raw))
        return payload

    # ── POLYGON 21 (SEMI P39 §25) ──────────────────────────────────────────
    def _read_polygon(self, *, build_payload: bool = True) -> Optional[dict]:
        """Info byte: ``00PXYRDL``. ``P`` (0x20) = point-list follows.

        M1.13.3b: fast-path byte reads (same approach as RECTANGLE)."""
        f = self._f
        read_uvarint = f.read_uvarint
        modal = self._modal
        info = f.read_byte()
        if info & 0x01:
            modal.layer = read_uvarint()
        if info & 0x02:
            modal.datatype = read_uvarint()
        if info & 0x20:
            modal.polygon_point_list = decode_point_list(
                f, for_polygon=True
            )
        if info & 0x10:
            x = f.read_svarint()
            if modal.xy_relative:
                modal.geometry_x += x
            else:
                modal.geometry_x = x
        if info & 0x08:
            y = f.read_svarint()
            if modal.xy_relative:
                modal.geometry_y += y
            else:
                modal.geometry_y = y
        rep_type, rep_raw = self._decode_repetition_if_set(info)

        if not build_payload:
            return None

        filtered = self._layer_filtered_out(self._modal.layer,
                                            self._modal.datatype)
        payload = {
            "layer": self._modal.layer,
            "datatype": self._modal.datatype,
            "x": self._modal.geometry_x,
            "y": self._modal.geometry_y,
            "repetition_type": rep_type,
            "filtered_out": filtered,
            "point_count": len(self._modal.polygon_point_list),
        }
        if not filtered:
            payload["points"] = list(self._modal.polygon_point_list)
            if self._defer_rep:
                payload["repetition_raw"] = rep_raw
            else:
                payload["repetition_offsets"] = (
                    [] if rep_type is None
                    else expand_repetition(rep_type, rep_raw))
        return payload

    # ── PATH 22 (SEMI P39 §26) ─────────────────────────────────────────────
    def _read_path(self) -> dict:
        """Info byte: ``EWPXYRDL``.

        ``W`` (0x40) = halfwidth uint follows. ``E`` (0x80) = extension
        scheme byte follows. The extension byte itself has two 2-bit
        fields encoding start (bits 3-2) and end (bits 1-0) extensions:
        ``00`` = reuse modal, ``01`` = zero, ``10`` = halfwidth,
        ``11`` = explicit signed-int follows.
        """
        info_b = self._f.read(1)
        if len(info_b) != 1:
            raise OasisFormatError("truncated PATH info byte")
        info = info_b[0]
        if info & 0x01:
            self._modal.layer = decode_unsigned_int(self._f)
        if info & 0x02:
            self._modal.datatype = decode_unsigned_int(self._f)
        if info & 0x40:
            self._modal.path_half_width = decode_unsigned_int(self._f)
        if info & 0x80:
            e = decode_unsigned_int(self._f)
            # Start extension (bits 3-2 of e)
            start_mode = (e & 0x0C) >> 2
            if start_mode == 0b11:
                self._modal.path_start_extension = decode_signed_int(self._f)
            elif start_mode == 0b01:
                self._modal.path_start_extension = 0
            elif start_mode == 0b10:
                self._modal.path_start_extension = self._modal.path_half_width
            # End extension (bits 1-0 of e)
            end_mode = e & 0x03
            if end_mode == 0b11:
                self._modal.path_end_extension = decode_signed_int(self._f)
            elif end_mode == 0b01:
                self._modal.path_end_extension = 0
            elif end_mode == 0b10:
                self._modal.path_end_extension = self._modal.path_half_width
        if info & 0x20:
            self._modal.path_point_list = decode_point_list(
                self._f, for_polygon=False
            )
        self._decode_xy(info)
        rep_type, rep_raw = self._decode_repetition_if_set(info)

        filtered = self._layer_filtered_out(self._modal.layer,
                                            self._modal.datatype)
        payload = {
            "layer": self._modal.layer,
            "datatype": self._modal.datatype,
            "half_width": self._modal.path_half_width,
            "start_extension": self._modal.path_start_extension,
            "end_extension": self._modal.path_end_extension,
            "x": self._modal.geometry_x,
            "y": self._modal.geometry_y,
            "repetition_type": rep_type,
            "filtered_out": filtered,
            "point_count": len(self._modal.path_point_list),
        }
        if not filtered:
            payload["points"] = list(self._modal.path_point_list)
            payload["repetition_offsets"] = (
                [] if rep_type is None
                else expand_repetition(rep_type, rep_raw))
        return payload

    # ── TRAPEZOID 23 / 24 / 25 (SEMI P39 §24) ──────────────────────────────
    def _read_trapezoid(self, record_id: int) -> dict:
        """Info byte: ``0WHXYRDL``. Records 23/24/25 differ only in which
        of the two slope deltas (``delta_a`` / ``delta_b``) follow the
        height field:

        * 23 -> both ``delta_a`` *and* ``delta_b`` (full trapezoid)
        * 24 -> only ``delta_a`` (``delta_b == 0``)
        * 25 -> only ``delta_b`` (``delta_a == 0``)
        """
        info_b = self._f.read(1)
        if len(info_b) != 1:
            raise OasisFormatError("truncated TRAPEZOID info byte")
        info = info_b[0]
        if info & 0x01:
            self._modal.layer = decode_unsigned_int(self._f)
        if info & 0x02:
            self._modal.datatype = decode_unsigned_int(self._f)
        if info & 0x40:
            self._modal.geometry_w = decode_unsigned_int(self._f)
        if info & 0x20:
            self._modal.geometry_h = decode_unsigned_int(self._f)
        delta_a = decode_signed_int(self._f) if record_id in (23, 24) else 0
        delta_b = decode_signed_int(self._f) if record_id in (23, 25) else 0
        self._decode_xy(info)
        rep_type, rep_raw = self._decode_repetition_if_set(info)

        filtered = self._layer_filtered_out(self._modal.layer,
                                            self._modal.datatype)
        payload = {
            "layer": self._modal.layer,
            "datatype": self._modal.datatype,
            "width": self._modal.geometry_w,
            "height": self._modal.geometry_h,
            "delta_a": delta_a,
            "delta_b": delta_b,
            "x": self._modal.geometry_x,
            "y": self._modal.geometry_y,
            "repetition_type": rep_type,
            "filtered_out": filtered,
        }
        if not filtered:
            payload["repetition_offsets"] = (
                [] if rep_type is None
                else expand_repetition(rep_type, rep_raw))
        return payload

    # ── CTRAPEZOID 26 (SEMI P39 §24.2) ─────────────────────────────────────
    def _read_ctrapezoid(self) -> dict:
        """Info byte: ``TWHXYRDL``. ``T`` (0x80) = ctrapezoid-type uint
        follows (one of the 26 canned shapes). Width / height may be
        omitted for shapes that derive them from each other."""
        info_b = self._f.read(1)
        if len(info_b) != 1:
            raise OasisFormatError("truncated CTRAPEZOID info byte")
        info = info_b[0]
        if info & 0x01:
            self._modal.layer = decode_unsigned_int(self._f)
        if info & 0x02:
            self._modal.datatype = decode_unsigned_int(self._f)
        if info & 0x80:
            self._modal.ctrapezoid_type = decode_unsigned_int(self._f)
        if info & 0x40:
            self._modal.geometry_w = decode_unsigned_int(self._f)
        if info & 0x20:
            self._modal.geometry_h = decode_unsigned_int(self._f)
        self._decode_xy(info)
        rep_type, rep_raw = self._decode_repetition_if_set(info)

        filtered = self._layer_filtered_out(self._modal.layer,
                                            self._modal.datatype)
        payload = {
            "layer": self._modal.layer,
            "datatype": self._modal.datatype,
            "ctrapezoid_type": self._modal.ctrapezoid_type,
            "width": self._modal.geometry_w,
            "height": self._modal.geometry_h,
            "x": self._modal.geometry_x,
            "y": self._modal.geometry_y,
            "repetition_type": rep_type,
            "filtered_out": filtered,
        }
        if not filtered:
            payload["repetition_offsets"] = (
                [] if rep_type is None
                else expand_repetition(rep_type, rep_raw))
        return payload

    # ── CIRCLE 27 (SEMI P39 §27.2) ─────────────────────────────────────────
    def _read_circle(self) -> dict:
        """Info byte: ``00rXYRDL``. ``r`` (0x20) = radius uint follows."""
        info_b = self._f.read(1)
        if len(info_b) != 1:
            raise OasisFormatError("truncated CIRCLE info byte")
        info = info_b[0]
        if info & 0x01:
            self._modal.layer = decode_unsigned_int(self._f)
        if info & 0x02:
            self._modal.datatype = decode_unsigned_int(self._f)
        if info & 0x20:
            self._modal.circle_radius = decode_unsigned_int(self._f)
        self._decode_xy(info)
        rep_type, rep_raw = self._decode_repetition_if_set(info)

        filtered = self._layer_filtered_out(self._modal.layer,
                                            self._modal.datatype)
        payload = {
            "layer": self._modal.layer,
            "datatype": self._modal.datatype,
            "radius": self._modal.circle_radius,
            "x": self._modal.geometry_x,
            "y": self._modal.geometry_y,
            "repetition_type": rep_type,
            "filtered_out": filtered,
        }
        if not filtered:
            payload["repetition_offsets"] = (
                [] if rep_type is None
                else expand_repetition(rep_type, rep_raw))
        return payload

    # ── TEXT 19 (SEMI P39 §28) ─────────────────────────────────────────────
    def _read_text(self) -> dict:
        """Info byte: ``0CNXYRTL`` (klayout layout):

        * ``C`` (0x40): text string follows
        * ``N`` (0x20): if C set, 1 = textstring refnum, 0 = inline a-string
        * ``X`` (0x10), ``Y`` (0x08), ``R`` (0x04)
        * ``T`` (0x02): texttype uint follows
        * ``L`` (0x01): textlayer uint follows

        TEXT has its own (textlayer, texttype) pair instead of
        (layer, datatype), so the layer filter is applied to those.
        XYABSOLUTE / XYRELATIVE applies to the *text* x/y modals, which
        are separate from the geometry x/y modals.
        """
        info_b = self._f.read(1)
        if len(info_b) != 1:
            raise OasisFormatError("truncated TEXT info byte")
        info = info_b[0]

        if info & 0x40:
            if info & 0x20:
                self._modal.text_string = decode_unsigned_int(self._f)
            else:
                self._modal.text_string = decode_string(self._f)
        if info & 0x01:
            self._modal.text_layer = decode_unsigned_int(self._f)
        if info & 0x02:
            self._modal.text_type = decode_unsigned_int(self._f)
        # TEXT uses its own modal x/y (text_x, text_y), separate from the
        # geometry modals -- matching klayout's mm_text_x / mm_text_y.
        if info & 0x10:
            tx = decode_signed_int(self._f)
            if self._modal.xy_relative:
                self._modal.text_x += tx
            else:
                self._modal.text_x = tx
        if info & 0x08:
            ty = decode_signed_int(self._f)
            if self._modal.xy_relative:
                self._modal.text_y += ty
            else:
                self._modal.text_y = ty
        rep_type, rep_raw = self._decode_repetition_if_set(info)

        filtered = self._layer_filtered_out(self._modal.text_layer,
                                            self._modal.text_type)
        # Stringify text_string for the payload; bytes survive as-is so
        # downstream code can decode with its own error policy.
        text_repr = self._modal.text_string
        if isinstance(text_repr, bytes):
            text_repr = text_repr.decode("ascii", "backslashreplace")
        payload = {
            "text": text_repr,
            "text_layer": self._modal.text_layer,
            "text_type": self._modal.text_type,
            "x": self._modal.text_x,
            "y": self._modal.text_y,
            "repetition_type": rep_type,
            "filtered_out": filtered,
        }
        if not filtered:
            payload["repetition_offsets"] = (
                [] if rep_type is None
                else expand_repetition(rep_type, rep_raw))
        return payload


# ── M3.5a: per-cell byte-offset index (S_CELL_OFFSET) ────────────────────────

_CELL_OFFSET_PROP = "S_CELL_OFFSET"

# F13: KLayout's per-cell bounding-box standard property (written when
# "Save As → OASIS, Standard properties = Global + per cell bounding box").
# Attached to CELLNAME records alongside S_CELL_OFFSET; SEMI P39 §31 gives 5
# integer operands (assumed [flag, x, y, w, h]; confirmed per-file in F13 M1).
# Read here purely so callers can later prune walk_roi without a CE layer.
_BBOX_PROP = "S_BOUNDING_BOX"

# SEMI P39 §14: the END record is padded to a fixed 256-byte length, so on an
# offset_flag==1 file (name tables at the tail) END is the last record and
# begins at ``file_size - 256``. Its body opens with the 6 (strict, byte)
# table-offset pairs we need to locate those tail tables.
_END_RECORD_LEN = 256

# Record ids that make up each tail name table (offset_flag==1). A table read
# stops at the first record outside its set — i.e. the next table / a CELL /
# END — so reads never bleed from one table into the next.
_TAIL_PROPNAME_RIDS = (PROPNAME_IMP, PROPNAME_EXP)
_TAIL_CELLNAME_RIDS = (CELLNAME_IMP, CELLNAME_EXP,
                       PROPERTY_NORMAL, PROPERTY_LAST)
_TAIL_LAYERNAME_RIDS = (LAYERNAME_GEOM, LAYERNAME_TEXT)


def _name_str(v) -> str:
    if isinstance(v, bytes):
        return v.decode("ascii", "replace")
    return v if isinstance(v, str) else str(v)


def _peek_start(reader: "OasisReader") -> dict:
    """Read the START record (right after MAGIC) without disturbing the
    reader's position, so callers can branch on ``offset_flag`` before the
    main scan. Returns the START payload (``{}`` if the first record isn't a
    START)."""
    f = reader._f
    save = f.tell()
    try:
        f.seek(len(MAGIC))
        if f.read_uvarint() != START:
            return {}
        return reader._read_start()
    except OasisFormatError:
        return {}
    finally:
        f.seek(save)


def _try_parse_end_offsets(f, pos: int, size: int):
    """If a valid offset_flag==1 END record starts at ``pos``, return its 6
    ``(strict, byte_offset)`` table-offset pairs; else None."""
    f.seek(pos)
    try:
        if f.read_uvarint() != END:
            return None
        offs: list[tuple[int, int]] = []
        for _ in range(6):
            strict = f.read_uvarint()
            byte_off = f.read_uvarint()
            offs.append((strict, byte_off))
    except OasisFormatError:
        return None
    # Sanity: present tables (byte_offset != 0) must point inside the file,
    # and at least one must be present — otherwise this 0x02 byte wasn't a
    # real END offset table (guards the tail-scan fallback below).
    present = [(s, o) for s, o in offs if o]
    if not present or any(o >= size for _, o in present):
        return None
    return offs


def _read_end_table_offsets(reader: "OasisReader"):
    """Locate the END record (last record, padded to a fixed 256 bytes) and
    return its 6 ``(strict, byte_offset)`` name-table pairs, or None. Used
    only for offset_flag==1 files; the offset_flag==0 path never calls this."""
    f = reader._f
    f.clear_substreams()
    size = f.seek(0, 2)
    candidates: list[int] = []
    if size >= _END_RECORD_LEN:
        candidates.append(size - _END_RECORD_LEN)
    # Fallback for non-standard tail padding: scan the last few hundred bytes
    # for an END id byte. Cheap (bounded window) and the sanity checks in
    # _try_parse_end_offsets reject false positives.
    tail_start = max(0, size - 512)
    f.seek(tail_start)
    tail = f.read(size - tail_start)
    for i, b in enumerate(tail):
        if b == END:
            cand = tail_start + i
            if cand not in candidates:
                candidates.append(cand)
    for cand in candidates:
        offs = _try_parse_end_offsets(f, cand, size)
        if offs is not None:
            return offs
    return None


def _iter_table_at(reader: "OasisReader", offset: int, allowed):
    """Yield ``(rid, payload)`` for consecutive records of one tail name
    table, starting at absolute byte ``offset`` and stopping at the first
    record whose id isn't in ``allowed`` (next table / CELL / END / CBLOCK we
    don't expand) or at EOF. offset_flag==1 only; the header path is
    untouched."""
    f = reader._f
    f.clear_substreams()
    f.seek(offset)
    while True:
        try:
            rid = f.read_uvarint()
        except OasisFormatError:
            return
        if rid not in allowed:
            return
        if rid in (CELLNAME_IMP, CELLNAME_EXP):
            yield rid, reader._read_cellname(explicit=(rid == CELLNAME_EXP))
        elif rid in (PROPNAME_IMP, PROPNAME_EXP):
            yield rid, reader._read_propname(explicit=(rid == PROPNAME_EXP))
        elif rid in (LAYERNAME_GEOM, LAYERNAME_TEXT):
            yield rid, reader._read_layername()
        elif rid in (PROPERTY_NORMAL, PROPERTY_LAST):
            yield rid, reader._read_property(last=(rid == PROPERTY_LAST))
        else:
            return


def _scan_tail_tables(reader: "OasisReader", start: dict) -> dict:
    """offset_flag==1: the CELLNAME / PROPNAME / LAYERNAME tables live at the
    file tail, located via the END record's offset table. Read them directly
    (PROPNAME first, to resolve the S_CELL_OFFSET property name) and build the
    same ``{by_refnum, by_name, layernames, ...}`` dict the header scan
    returns. Closes the reader before returning (mirrors ``scan_cell_offsets``).
    Empty index on a missing/unreadable offset table — caller then reports
    'no S_CELL_OFFSET' exactly as before."""
    by_refnum: dict[int, int] = {}
    by_name: dict[str, int] = {}
    sbbox_by_refnum: dict[int, list] = {}      # F13: per-cell S_BOUNDING_BOX
    sbbox_by_name: dict[str, list] = {}
    layernames: list[tuple[str, tuple, tuple]] = []
    propname_by_refnum: dict[int, str] = {}
    cellnames = 0
    try:
        offs = _read_end_table_offsets(reader)
        if offs:
            cellname_t, propname_t, layername_t = offs[0], offs[2], offs[4]

            # 1) PROPNAME table → refnum/index → name, so a PROPERTY that
            #    references its propname by refnum resolves to S_CELL_OFFSET.
            pn_implicit = 0
            if propname_t[1]:
                for rid, p in _iter_table_at(reader, propname_t[1],
                                             _TAIL_PROPNAME_RIDS):
                    nm = _name_str(p.get("name", ""))
                    if rid == PROPNAME_EXP and p.get("refnum") is not None:
                        propname_by_refnum[int(p["refnum"])] = nm
                    else:
                        propname_by_refnum[pn_implicit] = nm
                        pn_implicit += 1

            # 2) CELLNAME table: each CELLNAME is followed by its
            #    PROPERTY(S_CELL_OFFSET) → fill by_refnum / by_name. Mirrors the
            #    header-path logic so refnum numbering stays identical.
            if cellname_t[1]:
                cn_implicit = 0
                last_cell_ref: Optional[int] = None
                last_cell_name: Optional[str] = None
                last_propname: Optional[str] = None
                for rid, p in _iter_table_at(reader, cellname_t[1],
                                             _TAIL_CELLNAME_RIDS):
                    if rid in (CELLNAME_IMP, CELLNAME_EXP):
                        cellnames += 1
                        last_cell_name = _name_str(p.get("name", ""))
                        if rid == CELLNAME_EXP and p.get("refnum") is not None:
                            last_cell_ref = int(p["refnum"])
                        else:
                            last_cell_ref = cn_implicit
                            cn_implicit += 1
                    else:  # PROPERTY_NORMAL / PROPERTY_LAST
                        pn = p.get("propname")
                        if pn is None:
                            name = last_propname        # modal: reuse previous
                        elif isinstance(pn, int):
                            name = propname_by_refnum.get(pn)
                        else:
                            name = _name_str(pn)
                        last_propname = name
                        if name == _CELL_OFFSET_PROP and last_cell_ref is not None:
                            vals = p.get("values") or []
                            if vals:
                                off = int(vals[0])
                                by_refnum[last_cell_ref] = off
                                if last_cell_name is not None:
                                    by_name[last_cell_name] = off
                        elif name == _BBOX_PROP and last_cell_ref is not None:
                            vals = p.get("values") or []
                            if len(vals) >= 5:
                                bb = [int(v) for v in vals[:5]]
                                sbbox_by_refnum[last_cell_ref] = bb
                                if last_cell_name is not None:
                                    sbbox_by_name[last_cell_name] = bb

            # 3) LAYERNAME table → layer labels for the UI (F3 M2 parity).
            if layername_t[1]:
                for _rid, p in _iter_table_at(reader, layername_t[1],
                                              _TAIL_LAYERNAME_RIDS):
                    layernames.append((
                        _name_str(p.get("name", "")),
                        tuple(p.get("layer_interval") or (0, -1)),
                        tuple(p.get("datatype_interval") or (0, -1)),
                    ))
    finally:
        reader.close()
    return {
        "by_refnum": by_refnum,
        "by_name": by_name,
        "found": len(by_refnum),
        "cellnames": cellnames,
        "unit": start.get("unit"),
        "layernames": layernames,
        "propnames": sorted(set(propname_by_refnum.values())),
        "sbbox_by_refnum": sbbox_by_refnum,
        "sbbox_by_name": sbbox_by_name,
    }


def scan_cell_offsets(path: str | Path, *, use_mmap: bool = False,
                      shared_buf: object = None) -> dict:
    """Read the name-table section and return the per-cell byte-offset
    index from ``S_CELL_OFFSET`` properties (M3.5a).

    ``shared_buf`` (F6 M2): scan an already-mapped buffer instead of
    re-opening the file, so RandomAccessReader maps the file only once.

    Random-access ROI load (M3.5b/c) seeks straight to a cell's CELL
    record using these offsets, decoding only the cells a SEM image's FOV
    touches instead of the whole multi-hundred-MB file.

    Returns ``{"by_refnum": {refnum: offset}, "by_name": {name: offset},
    "found": int, "cellnames": int}``. Offsets are absolute byte
    positions of each cell's CELL record in the file. Empty index when
    the file carries no ``S_CELL_OFFSET`` (caller falls back to a full
    decode)."""
    reader = OasisReader(path, capture_prop_values=True, use_mmap=use_mmap,
                         shared_buf=shared_buf)
    # offset_flag (SEMI P39 §13.10) says where the name tables live:
    #   0 → in the header, between START and the first CELL (the original fast
    #       path below; verified on Calibre D2DB files — do not touch).
    #   1 → at the file tail, located via the END record's offset table (e.g.
    #       KLayout "Save As → OASIS, strict mode"). The header scan would hit
    #       the first CELL with an empty index and wrongly report "no
    #       S_CELL_OFFSET", so dispatch to a dedicated tail-table reader.
    start = _peek_start(reader)
    if start.get("offset_flag") == 1:
        return _scan_tail_tables(reader, start)
    propname_by_refnum: dict[int, str] = {}
    pn_implicit = 0
    cn_implicit = 0
    last_propname: Optional[str] = None
    last_cell_ref: Optional[int] = None
    last_cell_name: Optional[str] = None
    by_refnum: dict[int, int] = {}
    by_name: dict[str, int] = {}
    # F13: per-cell S_BOUNDING_BOX raw operand lists, keyed the same way as the
    # offset maps. Collected in the same PROPERTY pass; empty when the file has
    # no per-cell bounding boxes (caller falls back to CE / full-decode prune).
    sbbox_by_refnum: dict[int, list] = {}
    sbbox_by_name: dict[str, list] = {}
    cellnames = 0
    # LAYERNAME records (11/12) map a name to a (layer, datatype) interval; they
    # live in the name-table section before any CELL, so this same pass picks
    # them up for free (F3 M2 — layer labels in the UI).
    layernames: list[tuple[str, tuple, tuple]] = []

    unit = None
    for rid, payload in reader.iter_records():
        if rid == START:
            unit = payload.get("unit")
        if rid in (LAYERNAME_GEOM, LAYERNAME_TEXT):
            layernames.append((
                _name_str(payload.get("name", "")),
                tuple(payload.get("layer_interval") or (0, -1)),
                tuple(payload.get("datatype_interval") or (0, -1)),
            ))
        if rid in (PROPNAME_IMP, PROPNAME_EXP):
            nm = _name_str(payload.get("name", ""))
            if rid == PROPNAME_EXP and payload.get("refnum") is not None:
                propname_by_refnum[int(payload["refnum"])] = nm
            else:
                propname_by_refnum[pn_implicit] = nm
                pn_implicit += 1
        elif rid in (CELLNAME_IMP, CELLNAME_EXP):
            cellnames += 1
            last_cell_name = _name_str(payload.get("name", ""))
            if rid == CELLNAME_EXP and payload.get("refnum") is not None:
                last_cell_ref = int(payload["refnum"])
            else:
                last_cell_ref = cn_implicit
                cn_implicit += 1
        elif rid in (PROPERTY_NORMAL, PROPERTY_LAST):
            pn = payload.get("propname")
            if pn is None:
                name = last_propname            # modal: reuse previous
            elif isinstance(pn, int):
                name = propname_by_refnum.get(pn)
            else:
                name = _name_str(pn)
            last_propname = name
            if name == _CELL_OFFSET_PROP and last_cell_ref is not None:
                vals = payload.get("values") or []
                if vals:
                    off = int(vals[0])
                    by_refnum[last_cell_ref] = off
                    if last_cell_name is not None:
                        by_name[last_cell_name] = off
            elif name == _BBOX_PROP and last_cell_ref is not None:
                vals = payload.get("values") or []
                if len(vals) >= 5:
                    bb = [int(v) for v in vals[:5]]
                    sbbox_by_refnum[last_cell_ref] = bb
                    if last_cell_name is not None:
                        sbbox_by_name[last_cell_name] = bb
        elif rid in (CELL_REFNUM, CELL_NAME):
            break    # body reached; name table fully behind us

    reader.close()   # release the mmap / slurp buffer (F6 M1)
    return {
        "by_refnum": by_refnum,
        "by_name": by_name,
        "found": len(by_refnum),
        "cellnames": cellnames,
        "unit": unit,
        "layernames": layernames,
        "propnames": sorted(set(propname_by_refnum.values())),
        "sbbox_by_refnum": sbbox_by_refnum,
        "sbbox_by_name": sbbox_by_name,
    }


def verify_cell_offsets(path: str | Path, offsets,
                        sample: int = 16) -> dict:
    """Spot-check that byte ``offsets`` really point at CELL records.

    Opens the raw file (no streaming) and, for up to ``sample`` offsets,
    seeks there and checks the record-id byte is CELL (13/14). Returns
    ``{"checked": int, "ok": int, "bad": [(offset, byte), ...]}``. This
    de-risks M3.5b: if offsets don't land on CELL records (e.g. they're
    relative to something, or cells live inside CBLOCKs), random access
    won't work and we fall back to option A."""
    items = list(offsets)[:sample]
    ok = 0
    bad: list = []
    with open(path, "rb") as fh:
        for off in items:
            fh.seek(int(off))
            b = fh.read(1)
            rid = b[0] if b else -1
            if rid in (CELL_REFNUM, CELL_NAME):
                ok += 1
            else:
                bad.append((int(off), rid))
    return {"checked": len(items), "ok": ok, "bad": bad}


# ── CLI smoke test ───────────────────────────────────────────────────────────


def _dump(path: Path, *, summary_only: bool = False,
          heartbeat_every: int = 100_000,
          max_records: int = 0) -> int:
    """Dump an OASIS file's record stream.

    Args:
        path: file to dump.
        summary_only: suppress per-record print; emit final histogram.
        heartbeat_every: stderr progress interval in summary mode.
        max_records: stop after this many records (0 = no limit). Useful
            on production-sized files where you just want a representative
            histogram and don't want to wait for the full multi-hour walk.
            A SIGINT (Ctrl+C) has the same effect — the partial summary
            is printed cleanly.
    """
    import time

    print(f"file: {path}")
    print(f"size: {path.stat().st_size:,} bytes\n")

    cellnames: dict[int, str] = {}
    implicit_idx_cell = 0
    layernames_geom: list[tuple[str, tuple, tuple]] = []
    layernames_text: list[tuple[str, tuple, tuple]] = []
    cells_seen: list[str] = []
    # In summary mode we MUST NOT accumulate every PLACEMENT payload — on
    # a 300 MB D2DB with millions of placements that would OOM. Keep a
    # bounded sample (first 4 per parent cell) plus an integer counter.
    placement_count: dict[object, int] = {}
    placement_samples: dict[object, list[dict]] = {}
    cblock_count = 0
    record_counts: dict[int, int] = {}
    t0 = time.monotonic()
    last_heartbeat = t0
    total_records = 0

    def _emit(msg: str) -> None:
        """Per-record output sink — silenced in summary mode."""
        if not summary_only:
            print(msg)

    with OasisReader(path) as reader:
        print("MAGIC OK")
        if summary_only:
            print(f"(summary mode -- per-record output suppressed; "
                  f"heartbeat every {heartbeat_every:,} records on stderr)")
        else:
            print("(positions are local to the current stream -- '[d=N]' marks "
                  "records read from inside CBLOCK substream depth N)")
        early_stop_reason: Optional[str] = None
        try:
            for rid, payload in reader.iter_records():
                total_records += 1
                record_counts[rid] = record_counts.get(rid, 0) + 1

                # Early termination: caller asked for a representative
                # sample, not a full walk. Stop cleanly so the partial
                # histogram still prints.
                if max_records and total_records >= max_records:
                    early_stop_reason = (
                        f"--max-records {max_records:,} reached"
                    )
                    break

                # Heartbeat keeps a multi-minute parse from looking hung.
                # Goes to stderr so it doesn't pollute the redirected
                # dump file.
                if summary_only and total_records % heartbeat_every == 0:
                    now = time.monotonic()
                    rate = heartbeat_every / max(1e-9, now - last_heartbeat)
                    print(
                        f"  [progress] {total_records:>11,} records  "
                        f"elapsed={now - t0:>6.1f}s  "
                        f"rate={rate:>9,.0f}/s  "
                        f"cblock_depth={reader.cblock_depth}  "
                        f"outer={reader._f.outer_position:,}/{path.stat().st_size:,}",
                        file=sys.stderr,
                    )
                    last_heartbeat = now

                rs = reader.last_record_start
                name = RECORD_NAMES.get(rid, f"<id {rid}>")
                # ``cblock_depth`` is read BEFORE the next auto-pop, so a
                # record's depth reflects which stream it actually came
                # from. Records sitting on the outer file print as plain
                # ``@N``; ones inside a CBLOCK get a ``[d=N]`` prefix.
                depth = reader.cblock_depth
                depth_tag = f"[d={depth}] " if depth > 0 else "       "
                prefix = f"{depth_tag}@{rs:>7d}  {name:22s}"
                if rid == START:
                    _emit(
                        f"{prefix}  version={payload['version']!r} "
                        f"unit={payload['unit']} "
                        f"offset_flag={payload['offset_flag']}"
                    )
                elif rid == END:
                    _emit(f"{prefix}  scheme={payload.get('validation_scheme')}")
                elif rid in (CELLNAME_IMP, CELLNAME_EXP):
                    if payload["explicit"]:
                        refnum = payload["refnum"]
                    else:
                        refnum = implicit_idx_cell
                        implicit_idx_cell += 1
                    cellnames[refnum] = payload["name"].decode("ascii", "backslashreplace")
                    _emit(
                        f"{prefix}  refnum={refnum}  -> {cellnames[refnum]!r}"
                    )
                elif rid in (LAYERNAME_GEOM, LAYERNAME_TEXT):
                    layer_iv = payload["layer_interval"]
                    datatype_iv = payload["datatype_interval"]
                    nm = payload["name"].decode("ascii", "backslashreplace")
                    entry = (nm, layer_iv, datatype_iv)
                    if rid == LAYERNAME_GEOM:
                        layernames_geom.append(entry)
                    else:
                        layernames_text.append(entry)
                    _emit(
                        f"{prefix}  {nm!r:18s}  L={layer_iv}  D={datatype_iv}"
                    )
                elif rid in (CELL_REFNUM, CELL_NAME):
                    if "name" in payload:
                        nm = payload["name"].decode("ascii", "backslashreplace")
                    else:
                        nm = cellnames.get(payload["refnum"], f"#{payload['refnum']}")
                    cells_seen.append(nm)
                    _emit(f"{prefix}  -> {nm!r}")
                elif rid in (XYABSOLUTE, XYRELATIVE, PAD):
                    _emit(prefix)
                elif rid in (PROPERTY_NORMAL, PROPERTY_LAST):
                    propname = payload.get("propname")
                    if isinstance(propname, bytes):
                        propname = propname.decode("ascii", "backslashreplace")
                    _emit(
                        f"{prefix}  info=0x{payload['info_byte']:02x} "
                        f"propname={propname!r} "
                        f"values={payload.get('value_count')}"
                    )
                elif rid in (PROPNAME_IMP, PROPNAME_EXP):
                    name_bytes = payload.get("name", b"")
                    _emit(
                        f"{prefix}  name={name_bytes.decode('ascii', 'replace')!r}  "
                        f"refnum={payload.get('refnum')}"
                    )
                elif rid in (PROPSTRING_IMP, PROPSTRING_EXP):
                    val = payload.get("value", b"")
                    # b-string may contain non-ascii; show length + escape.
                    _emit(
                        f"{prefix}  value_len={len(val)} "
                        f"value={val[:40]!r}{'...' if len(val) > 40 else ''}"
                    )
                elif rid in (TEXTSTRING_IMP, TEXTSTRING_EXP):
                    txt = payload.get("text", b"")
                    _emit(
                        f"{prefix}  text={txt.decode('ascii', 'replace')!r}  "
                        f"refnum={payload.get('refnum')}"
                    )
                elif rid in (PLACEMENT_NOMAG, PLACEMENT_MAG):
                    cell_ref = payload.get("cell_ref")
                    rep_n = len(payload.get("repetition_offsets") or []) or 1
                    _emit(
                        f"{prefix}  cell={cell_ref!r} ({payload.get('cell_ref_kind')})  "
                        f"x={payload['x']}  y={payload['y']}  "
                        f"angle={payload['angle']}deg  mag={payload['magnification']}  "
                        f"flip={payload['flip']}  rep={payload.get('repetition_type')}×{rep_n}"
                    )
                    parent = payload.get("in_cell")
                    placement_count[parent] = placement_count.get(parent, 0) + 1
                    samples = placement_samples.setdefault(parent, [])
                    if len(samples) < 4:
                        samples.append(payload)
                elif rid == CBLOCK:
                    cblock_count += 1
                    _emit(
                        f"{prefix}  comp_type={payload['comp_type']} "
                        f"compressed={payload['compressed_count']} -> "
                        f"uncompressed={payload['uncompressed_count']} "
                        f"(depth now {payload['cblock_depth']})"
                    )
                elif rid == XELEMENT:
                    _emit(
                        f"{prefix}  attribute={payload['attribute']}  "
                        f"data_len={payload['data_len']}"
                    )
                elif rid in (RECTANGLE, CTRAPEZOID,
                             TRAPEZOID, TRAPEZOID_VR, TRAPEZOID_VL):
                    _emit(
                        f"{prefix}  L={payload['layer']}/D={payload['datatype']}  "
                        f"W={payload.get('width')}  H={payload.get('height')}  "
                        f"@({payload['x']}, {payload['y']})  "
                        f"filtered={payload['filtered_out']}"
                    )
                elif rid == POLYGON:
                    _emit(
                        f"{prefix}  L={payload['layer']}/D={payload['datatype']}  "
                        f"pts={payload['point_count']}  "
                        f"@({payload['x']}, {payload['y']})  "
                        f"filtered={payload['filtered_out']}"
                    )
                elif rid == PATH:
                    _emit(
                        f"{prefix}  L={payload['layer']}/D={payload['datatype']}  "
                        f"hw={payload['half_width']}  "
                        f"pts={payload['point_count']}  "
                        f"@({payload['x']}, {payload['y']})  "
                        f"filtered={payload['filtered_out']}"
                    )
                elif rid == CIRCLE:
                    _emit(
                        f"{prefix}  L={payload['layer']}/D={payload['datatype']}  "
                        f"r={payload['radius']}  "
                        f"@({payload['x']}, {payload['y']})  "
                        f"filtered={payload['filtered_out']}"
                    )
                elif rid == TEXT:
                    _emit(
                        f"{prefix}  L={payload['text_layer']}/T={payload['text_type']}  "
                        f"text={payload['text']!r}  "
                        f"@({payload['x']}, {payload['y']})  "
                        f"filtered={payload['filtered_out']}"
                    )
                else:
                    _emit(f"{prefix}  (payload keys: {list(payload)})")
        except OasisNotImplemented as exc:
            print(f"\n[STOP] {exc}")
            print("(parser hit a record id this streamer build does not yet "
                  "decode. With M1.11 in place every geometry record (19-27) "
                  "is supported; the most common remaining stop is XGEOMETRY "
                  "(33), which is extension-only and rarely used.)")
        except KeyboardInterrupt:
            early_stop_reason = "interrupted by user (Ctrl+C)"
            print(f"\n[INTERRUPT] stopping; partial summary follows.")

        if early_stop_reason:
            print(f"\n[EARLY STOP] {early_stop_reason} after "
                  f"{total_records:,} records, "
                  f"outer-file position {reader._f.outer_position:,} / "
                  f"{path.stat().st_size:,} bytes "
                  f"({100 * reader._f.outer_position / path.stat().st_size:.2f}%).")

    elapsed = time.monotonic() - t0
    print()
    print(f"Elapsed                  : {elapsed:.2f} s "
          f"({total_records:,} records, "
          f"{total_records / max(elapsed, 1e-9):,.0f}/s avg)")
    # Record-type histogram lets the user see at a glance what dominated
    # the parse — particularly useful on big files where seeing every
    # record is impractical.
    if record_counts:
        print(f"Record-type histogram    :")
        for rid_ in sorted(record_counts, key=lambda r: -record_counts[r]):
            rname = RECORD_NAMES.get(rid_, f"<id {rid_}>")
            print(f"    {rname:24s} {record_counts[rid_]:>10,}")
    print(f"Discovered cellnames     : {len(cellnames)}")
    for ref, nm in sorted(cellnames.items()):
        print(f"    #{ref}  {nm!r}")
    print(f"Discovered geom layers   : {len(layernames_geom)}")
    for nm, l_iv, d_iv in layernames_geom:
        print(f"    {nm!r:18s}  layer-interval={l_iv}  datatype-interval={d_iv}")
    print(f"Discovered text layers   : {len(layernames_text)}")
    for nm, l_iv, d_iv in layernames_text:
        print(f"    {nm!r:18s}  layer-interval={l_iv}  datatype-interval={d_iv}")
    print(f"CELL records reached     : {len(cells_seen)}  "
          f"(first 10 names: {cells_seen[:10]}"
          f"{' ...' if len(cells_seen) > 10 else ''})")
    print(f"CBLOCK records reached   : {cblock_count}")
    total_placements = sum(placement_count.values())
    print(f"PLACEMENT records reached: {total_placements:,}")
    for parent, n in placement_count.items():
        # Resolve parent if it's a refnum.
        if isinstance(parent, int) and parent in cellnames:
            parent_label = f"{cellnames[parent]!r} (#{parent})"
        else:
            parent_label = repr(parent)
        print(f"    in {parent_label}: {n:,} placement(s)")
        # Show the first few we sampled for spot-checking transforms.
        for p in placement_samples.get(parent, []):
            target = p["cell_ref"]
            if isinstance(target, int) and target in cellnames:
                target_label = f"{cellnames[target]!r}"
            else:
                target_label = repr(target)
            print(
                f"        -> {target_label}  "
                f"@ ({p['x']}, {p['y']})  "
                f"angle={p['angle']}deg  flip={p['flip']}"
            )
        if n > len(placement_samples.get(parent, [])):
            print(f"        ... ({n - len(placement_samples.get(parent, [])):,} more)")
    return 0


def main() -> int:
    # Windows zh-TW PowerShell runs as cp950 by default; any stray
    # non-ASCII byte coming out of an OASIS payload (or a degree
    # symbol in a format string) crashes the dump with
    # UnicodeEncodeError. Force UTF-8 with replace-on-failure so the
    # process can never abort mid-parse on a print.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # Older Pythons / non-text streams: best effort, skip.
            pass

    ap = argparse.ArgumentParser(
        description="Dump an OASIS file via the streaming parser. "
                    "Default mode prints every record (good for small files); "
                    "use --summary on production-sized layouts.",
    )
    ap.add_argument("path", help="OASIS file to dump")
    ap.add_argument(
        "--summary", action="store_true",
        help="Suppress per-record output; print only counts + samples + "
             "final histogram. Recommended for files > 50 MB: stdout shrinks "
             "by ~3 orders of magnitude and the parser runs much faster "
             "(stdout is no longer the bottleneck).",
    )
    ap.add_argument(
        "--heartbeat", type=int, default=100_000, metavar="N",
        help="In --summary mode, print a stderr progress line every N "
             "records (default 100000). Useful for multi-minute parses "
             "to confirm the process is still alive.",
    )
    ap.add_argument(
        "--max-records", type=int, default=0, metavar="N",
        help="Stop after N records and print the partial summary. 0 "
             "(default) means walk the whole file. Use on multi-GB "
             "files when you only need the histogram for design "
             "decisions and can't wait hours for a full walk. Ctrl+C "
             "has the same effect.",
    )
    args = ap.parse_args()
    path = Path(args.path)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    try:
        return _dump(path, summary_only=args.summary,
                     heartbeat_every=args.heartbeat,
                     max_records=args.max_records)
    except OasisFormatError as exc:
        print(f"\n[FORMAT ERROR] {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
