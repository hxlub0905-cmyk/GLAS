"""Minimal OASIS (.oas) writer — reverse of ``oasis_streamer`` (F9 M1).

GLAS so far only *reads* layout (OASIS) and emits alignment offsets. F9
adds the missing direction: write selected raw layers and the Boolean
engine's synthesized layer back out as an OASIS file that KLayout (and
the company's .oas-based downstream tooling) can open.

Scope — deliberately a *minimal conformant* subset of SEMI P39:

* **no CBLOCK** — geometry is written uncompressed.
* **no modal-variable optimization** — every geometry record carries its
  own layer / datatype / coordinates, so record order never depends on
  modal carry. (We still emit one ``XYABSOLUTE`` per cell so coordinates
  are unambiguously absolute.)
* **validation scheme 0** — the END record carries no CRC / checksum, so
  there is nothing to compute. This matches what ``oasis_streamer``'s
  ``_read_end`` accepts and what the test-suite's hand-built fixtures use.
* **offset_flag = 0** — the (empty) name-table offset list lives in the
  START record as six ``(strict=0, offset=0)`` pairs ("scan the file"),
  so the END record is just ``record-id 2 + uint 0``. (offset_flag = 1
  would put a 6-pair table in END whose leading ``0`` the reader's peek
  heuristic mistakes for the validation scheme.)

Geometry records (F9 Q4): axis-aligned rectangles are written as
RECTANGLE records (info byte ``0x7b`` = S0/W/H/X/Y/D/L), everything else
as POLYGON records with a type-4 (g-delta) point-list. Both are exactly
the shapes ``oasis_streamer._read_rectangle`` / ``_read_polygon`` decode,
so the writer round-trips through GLAS's own reader (the M4 oracle).

Coordinates are integers in the file's database unit; GLAS treats 1 DBU =
1 nm. ``unit`` is written verbatim from the source file's START.unit when
available, so KLayout shows the same scale as the original.

Public surface::

    from oasis_writer import write_oasis
    write_oasis("out.oas",
                layers=[(17, 0, [ring0, ring1]), (25, 0, [ring2])],
                unit=1000.0)

``layers`` is an iterable of ``(layer, datatype, polygons)`` where each
*polygon* is an ``(N, 2)`` sequence of vertices (a single ring; holes are
the caller's responsibility — see M2 ``shapely_to_rings``). A repeated
closing vertex is tolerated and dropped.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterable, Sequence, Union

# ── Record ids (mirror oasis_streamer) ───────────────────────────────────────

MAGIC = b"%SEMI-OASIS\r\n"
_START = 1
_END = 2
_CELLNAME_IMP = 3
_CELL_REFNUM = 13
_XYABSOLUTE = 15
_RECTANGLE = 20
_POLYGON = 21

# RECTANGLE info byte: S=0, W(0x40), H(0x20), X(0x10), Y(0x08), R=0, D(0x02), L(0x01)
_RECT_INFO = 0x7B
# POLYGON info byte: P(0x20), X(0x10), Y(0x08), D(0x02), L(0x01)
_POLY_INFO = 0x3B

# SEMI P39 §14: the END record is padded to a fixed total length (256 bytes
# including the record-id byte). Lenient readers (our oasis_streamer) ignore
# the trailing pad, but KLayout *requires* it and otherwise rejects the file
# with "too few bytes after END record". Pad with 0x00 (PAD records) after the
# validation scheme; iter_records returns at END so the pad is never decoded.
_END_RECORD_LEN = 256


# ── Encode primitives (inverse of oasis_streamer decoders) ───────────────────


def encode_unsigned_int(n: int) -> bytes:
    """Variable-length unsigned integer (§7.2): 7 payload bits/byte, LSB
    first, bit 7 set on every byte except the last. Inverse of
    ``decode_unsigned_int``."""
    if n < 0:
        raise ValueError(f"encode_unsigned_int got negative {n}")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def encode_signed_int(n: int) -> bytes:
    """Variable-length signed integer (§7.3): magnitude << 1 | sign-bit."""
    raw = (abs(n) << 1) | (1 if n < 0 else 0)
    return encode_unsigned_int(raw)


def encode_real(x: float) -> bytes:
    """Real (§7.4). Integral values use the compact integer forms (type 0
    positive / type 1 negative); anything else falls back to an 8-byte
    IEEE-754 double (type 7)."""
    if float(x).is_integer():
        iv = int(x)
        if iv >= 0:
            return bytes([0]) + encode_unsigned_int(iv)
        return bytes([1]) + encode_unsigned_int(-iv)
    return bytes([7]) + struct.pack("<d", float(x))


def encode_string(s: Union[str, bytes]) -> bytes:
    """Length-prefixed byte string (§7.5)."""
    body = s.encode("ascii") if isinstance(s, str) else bytes(s)
    return encode_unsigned_int(len(body)) + body


def encode_g_delta(dx: int, dy: int) -> bytes:
    """Generic 2D displacement (§7.7.13), always the arbitrary form
    (bit0 = 1): ``(|dx| << 2) | (sign_x << 1) | 1`` then a signed-int dy.
    Handles any direction including pure-axis deltas."""
    raw = (abs(dx) << 2) | ((1 if dx < 0 else 0) << 1) | 1
    return encode_unsigned_int(raw) + encode_signed_int(dy)


# ── Geometry helpers ─────────────────────────────────────────────────────────


def _norm_ring(ring: Sequence) -> list[tuple[int, int]]:
    """Round to int vertices and drop a repeated closing vertex."""
    verts = [(int(round(p[0])), int(round(p[1]))) for p in ring]
    if len(verts) >= 2 and verts[0] == verts[-1]:
        verts = verts[:-1]
    return verts


def _axis_rect(verts: list[tuple[int, int]]):
    """If ``verts`` is exactly an axis-aligned rectangle, return
    ``(x, y, w, h)`` (lower-left + positive size); else ``None``."""
    if len(verts) != 4:
        return None
    xs = {v[0] for v in verts}
    ys = {v[1] for v in verts}
    if len(xs) != 2 or len(ys) != 2:
        return None
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    corners = {(x0, y0), (x0, y1), (x1, y0), (x1, y1)}
    if set(verts) != corners:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _emit_rectangle(layer: int, datatype: int, rect) -> bytes:
    x, y, w, h = rect
    return (bytes([_RECTANGLE, _RECT_INFO])
            + encode_unsigned_int(layer)
            + encode_unsigned_int(datatype)
            + encode_unsigned_int(w)
            + encode_unsigned_int(h)
            + encode_signed_int(x)
            + encode_signed_int(y))


def _emit_polygon(layer: int, datatype: int,
                  verts: list[tuple[int, int]]) -> bytes:
    # Anchor at V0; point-list type 4 carries successive vertex deltas.
    x0, y0 = verts[0]
    n = len(verts) - 1
    point_list = encode_unsigned_int(4) + encode_unsigned_int(n)
    px, py = x0, y0
    for vx, vy in verts[1:]:
        point_list += encode_g_delta(vx - px, vy - py)
        px, py = vx, vy
    return (bytes([_POLYGON, _POLY_INFO])
            + encode_unsigned_int(layer)
            + encode_unsigned_int(datatype)
            + point_list
            + encode_signed_int(x0)
            + encode_signed_int(y0))


def _emit_geometry(layer: int, datatype: int,
                   polygons: Iterable) -> bytes:
    out = bytearray()
    for ring in polygons:
        verts = _norm_ring(ring)
        if len(verts) < 3:
            continue  # degenerate
        rect = _axis_rect(verts)
        if rect is not None:
            out += _emit_rectangle(layer, datatype, rect)
        else:
            out += _emit_polygon(layer, datatype, verts)
    return bytes(out)


# ── Public writer ─────────────────────────────────────────────────────────────


def serialize_oasis(layers: Iterable,
                    *, unit: float = 1000.0,
                    cellname: str = "TOP") -> bytes:
    """Serialize ``layers`` into a minimal-conformant OASIS byte string.

    ``layers``: iterable of ``(layer:int, datatype:int, polygons)`` where
    ``polygons`` is an iterable of ``(N, 2)`` vertex sequences. Empty
    layers contribute nothing. See module docstring for the format subset.
    """
    start = (bytes([_START])
             + encode_string("1.0")
             + encode_real(unit)
             + encode_unsigned_int(0)        # offset_flag = 0
             + b"\x00" * 12)                 # 6 (strict=0, offset=0) pairs
    cellname_rec = bytes([_CELLNAME_IMP]) + encode_string(cellname)
    cell = bytes([_CELL_REFNUM]) + encode_unsigned_int(0) + bytes([_XYABSOLUTE])

    body = bytearray()
    for layer, datatype, polygons in layers:
        body += _emit_geometry(int(layer), int(datatype), polygons)

    end = bytes([_END]) + encode_unsigned_int(0)    # validation scheme 0
    end += b"\x00" * (_END_RECORD_LEN - len(end))   # pad to fixed 256 bytes
    return MAGIC + start + cellname_rec + cell + bytes(body) + end


def write_oasis(path: Union[str, Path], layers: Iterable,
                *, unit: float = 1000.0, cellname: str = "TOP") -> None:
    """Write ``layers`` to ``path`` as OASIS. See :func:`serialize_oasis`."""
    data = serialize_oasis(layers, unit=unit, cellname=cellname)
    Path(path).write_bytes(data)
