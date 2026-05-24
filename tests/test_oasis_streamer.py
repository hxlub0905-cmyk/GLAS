"""Unit tests for tools/oasis_streamer.py byte-level decoders.

Verifies each variable-length / typed decoder against hand-crafted byte
sequences. No file I/O — these run in any environment that can import
the streamer module."""
from __future__ import annotations

import io
import math
import struct
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import oasis_streamer as oas  # noqa: E402


# ── decode_unsigned_int ──────────────────────────────────────────────────────


def _u(bs: bytes) -> int:
    return oas.decode_unsigned_int(io.BytesIO(bs))


class TestUnsignedInt:
    def test_zero(self):
        assert _u(b"\x00") == 0

    def test_small_one_byte(self):
        # 0x7F = 127 is the largest single-byte value (high bit clear).
        assert _u(b"\x7f") == 127

    def test_two_bytes(self):
        # 128 = 0x80 -> two bytes: 0x80 (cont, low 7 = 0), 0x01 (high 7 = 1)
        assert _u(b"\x80\x01") == 128

    def test_three_bytes(self):
        # 16384 = 0x4000 -> 0x80, 0x80, 0x01
        assert _u(b"\x80\x80\x01") == 16384

    def test_truncated(self):
        # Continuation bit set but no follow-up byte.
        with pytest.raises(oas.OasisFormatError):
            _u(b"\x80")


# ── decode_signed_int ────────────────────────────────────────────────────────


def _s(bs: bytes) -> int:
    return oas.decode_signed_int(io.BytesIO(bs))


class TestSignedInt:
    def test_zero(self):
        assert _s(b"\x00") == 0

    def test_positive_one(self):
        # +1 -> sign bit 0, magnitude 1 -> raw 0b10 = 2
        assert _s(b"\x02") == 1

    def test_negative_one(self):
        # -1 -> sign bit 1, magnitude 1 -> raw 0b11 = 3
        assert _s(b"\x03") == -1

    def test_positive_63(self):
        # +63 -> raw 0b1111110 = 126
        assert _s(b"\x7e") == 63

    def test_negative_64(self):
        # -64 -> sign 1, magnitude 64 -> raw (64 << 1) | 1 = 129
        # 129 = 0x81 -> two bytes: 0x81, 0x01
        assert _s(b"\x81\x01") == -64


# ── decode_real ──────────────────────────────────────────────────────────────


def _r(bs: bytes) -> float:
    return oas.decode_real(io.BytesIO(bs))


class TestReal:
    def test_type0_positive_int(self):
        # type 0, value 42 -> bytes [0, 42]
        assert _r(b"\x00\x2a") == 42.0

    def test_type1_negative_int(self):
        assert _r(b"\x01\x2a") == -42.0

    def test_type2_reciprocal(self):
        assert _r(b"\x02\x02") == 0.5

    def test_type3_negative_reciprocal(self):
        assert _r(b"\x03\x04") == -0.25

    def test_type4_ratio(self):
        # 3/8
        assert _r(b"\x04\x03\x08") == pytest.approx(0.375)

    def test_type5_negative_ratio(self):
        assert _r(b"\x05\x03\x04") == pytest.approx(-0.75)

    def test_type6_float32(self):
        val = 1.25
        payload = b"\x06" + struct.pack("<f", val)
        assert _r(payload) == pytest.approx(val)

    def test_type7_float64(self):
        val = math.pi
        payload = b"\x07" + struct.pack("<d", val)
        assert _r(payload) == pytest.approx(val)

    def test_zero_reciprocal_rejected(self):
        with pytest.raises(oas.OasisFormatError):
            _r(b"\x02\x00")

    def test_unknown_type_rejected(self):
        with pytest.raises(oas.OasisFormatError):
            _r(b"\x08")


# ── decode_string ────────────────────────────────────────────────────────────


def _str(bs: bytes) -> bytes:
    return oas.decode_string(io.BytesIO(bs))


class TestString:
    def test_empty(self):
        assert _str(b"\x00") == b""

    def test_simple(self):
        # length 5, "hello"
        assert _str(b"\x05hello") == b"hello"

    def test_truncated(self):
        with pytest.raises(oas.OasisFormatError):
            _str(b"\x05hi")   # claims 5 but only 2 bytes


# ── decode_interval ──────────────────────────────────────────────────────────


def _iv(bs: bytes) -> tuple:
    return oas.decode_interval(io.BytesIO(bs))


class TestInterval:
    """Spec mapping per SEMI P39 §29.3 (and klayout's reader):
        kind 0: <empty>     -> (0, INF=-1)
        kind 1: <single n>  -> (n, n),  1 operand
        kind 2: <0..n>      -> (0, n),  1 operand
        kind 3: <n..INF>    -> (n, INF=-1),  1 operand
        kind 4: <n..m>      -> (n, m),  2 operands
    """

    def test_form_0_empty(self):
        assert _iv(b"\x00") == (0, -1)

    def test_form_1_single(self):
        # kind=1, n=20 -> (20, 20)
        assert _iv(b"\x01\x14") == (20, 20)

    def test_form_2_zero_to_n(self):
        # kind=2, n=7 -> (0, 7)
        assert _iv(b"\x02\x07") == (0, 7)

    def test_form_3_n_to_inf(self):
        # kind=3, n=108 -> (108, INF)
        assert _iv(b"\x03\x6c") == (108, -1)

    def test_form_3_with_multibyte_operand(self):
        # kind=3, n=250 (varint 0xfa 0x01) -> (250, INF). This is the
        # exact byte sequence that desynced the original D2DB load.
        assert _iv(b"\x03\xfa\x01") == (250, -1)

    def test_form_4_n_to_m(self):
        # kind=4, n=2, m=7 -> (2, 7)
        assert _iv(b"\x04\x02\x07") == (2, 7)

    def test_unknown_kind_rejected(self):
        with pytest.raises(oas.OasisFormatError):
            _iv(b"\x05")


# ── PROPERTY skip-value ──────────────────────────────────────────────────────


class TestSkipPropValue:
    """``_read_prop_value`` consumes exactly the right number of bytes
    for each of the 16 value-type codes (and returns the decoded value,
    used by M3.5a to read S_CELL_OFFSET). We can't call it as a free
    function (it's a method) so build a minimal OasisReader-like shim."""

    def _skip(self, bs: bytes) -> int:
        """Return number of bytes consumed by reading one value."""
        return self._read(bs)[0]

    def _read(self, bs: bytes):
        """Return (bytes_consumed, decoded_value)."""
        stream = io.BytesIO(bs)

        class _Shim:
            _f = stream

        val = oas.OasisReader._read_prop_value(_Shim())
        return stream.tell(), val

    def test_uint_returns_value(self):
        # type 8 + value 100 -> int 100 (the S_CELL_OFFSET path)
        consumed, val = self._read(b"\x08\x64")
        assert consumed == 2 and val == 100

    def test_real_type0(self):
        # type 0 + value 42
        assert self._skip(b"\x00\x2a") == 2

    def test_real_type4_ratio(self):
        # type 4 + m=3 + n=8
        assert self._skip(b"\x04\x03\x08") == 3

    def test_real_type6_float32(self):
        # type 6 + 4 raw bytes
        assert self._skip(b"\x06" + b"\x00\x00\x80\x3f") == 5

    def test_real_type7_float64(self):
        assert self._skip(b"\x07" + b"\x00" * 8) == 9

    def test_uint(self):
        # type 8 + value 100
        assert self._skip(b"\x08\x64") == 2

    def test_sint(self):
        # type 9 + +1 (encoded 0b10 = 2)
        assert self._skip(b"\x09\x02") == 2

    def test_string(self):
        # type 10 + length 3 + "abc"
        assert self._skip(b"\x0a\x03abc") == 5

    def test_propstring_refnum(self):
        # type 13 + refnum=2
        assert self._skip(b"\x0d\x02") == 2

    def test_unknown(self):
        with pytest.raises(oas.OasisFormatError):
            self._skip(b"\x10")  # type 16, invalid


class TestCellOffsetIndex:
    """M3.5a: scan_cell_offsets reads S_CELL_OFFSET byte offsets and
    verify_cell_offsets confirms they land on CELL records."""

    @staticmethod
    def _uint_fixed(n: int, width: int) -> bytes:
        # Force a fixed-width uint so the property's byte length doesn't
        # depend on the offset value (breaks the circular dependency
        # between the offset and where the CELL record ends up).
        out = []
        for i in range(width):
            b = n & 0x7F
            n >>= 7
            out.append(b | 0x80 if i < width - 1 else b)
        return bytes(out)

    def _build(self):
        start = (bytes([oas.START]) + _make_uint(3) + b"1.0"
                 + bytes([0]) + _make_uint(1) + _make_uint(0) + bytes([0] * 12))
        pn = bytes([oas.PROPNAME_IMP]) + _make_uint(13) + b"S_CELL_OFFSET"
        cn = bytes([oas.CELLNAME_IMP]) + _make_uint(1) + b"A"
        # PROPERTY: C=1 N=1 V=0 U=1 -> info 0x16; propname refnum 0; 1 value
        # of type 8 (uint) = the offset, fixed to 4 bytes.
        def prop(off):
            return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _make_uint(0)
                    + _make_uint(8) + self._uint_fixed(off, 4))
        cell = bytes([oas.CELL_REFNUM]) + _make_uint(0)
        rect = bytes([oas.RECTANGLE, 0x7b, 17, 0, 10, 10, 0, 0])
        end = bytes([oas.END]) + _make_uint(0)
        prefix = oas.MAGIC + start + pn + cn
        offset = len(prefix) + len(prop(0))   # where CELL record begins
        data = prefix + prop(offset) + cell + rect + end
        return data, offset

    def test_scan_and_verify(self, tmp_path: Path):
        data, offset = self._build()
        p = tmp_path / "idx.oas"
        p.write_bytes(data)
        idx = oas.scan_cell_offsets(p)
        assert idx["by_refnum"] == {0: offset}
        assert idx["by_name"] == {"A": offset}
        assert idx["found"] == 1 and idx["cellnames"] == 1
        ver = oas.verify_cell_offsets(p, idx["by_refnum"].values())
        assert ver["ok"] == 1 and ver["bad"] == []

    def test_no_offset_property(self, tmp_path: Path):
        # CELLNAME without any S_CELL_OFFSET -> empty index.
        start = (bytes([oas.START]) + _make_uint(3) + b"1.0"
                 + bytes([0]) + _make_uint(1) + _make_uint(0) + bytes([0] * 12))
        cn = bytes([oas.CELLNAME_IMP]) + _make_uint(1) + b"A"
        cell = bytes([oas.CELL_REFNUM]) + _make_uint(0)
        end = bytes([oas.END]) + _make_uint(0)
        p = tmp_path / "noidx.oas"
        p.write_bytes(oas.MAGIC + start + cn + cell + end)
        idx = oas.scan_cell_offsets(p)
        assert idx["found"] == 0 and idx["cellnames"] == 1


# ── Modal-state cell-boundary reset ──────────────────────────────────────────


class TestModalReset:
    def test_reset_clears_state(self):
        m = oas.ModalState()
        m.xy_relative = True
        m.layer = 7
        m.datatype = 3
        m.geometry_x = 100
        m.placement_cell = 42
        m.repetition = (1, [(0, 0), (10, 0)])
        m.reset_on_cell_boundary()
        assert m.xy_relative is False
        assert m.layer == 0
        assert m.datatype == 0
        assert m.geometry_x == 0
        assert m.placement_cell is None
        assert m.repetition is None


# ── M1.10: delta decoders (§7.7) ─────────────────────────────────────────────


class TestGDelta:
    """Generic 2D delta — two encoding forms keyed by bit 0 of the leading uint."""

    def _g(self, bs: bytes) -> tuple:
        return oas.decode_g_delta(io.BytesIO(bs))

    # Form 1 (bit0=0): octangular. direction at bits 1-3, magnitude at bits 4+.
    def test_form1_east(self):
        # dir=0 (E), mag=5 -> (5<<4) | (0<<1) | 0 = 0x50
        assert self._g(b"\x50") == (5, 0)

    def test_form1_north(self):
        # dir=1 (N), mag=3 -> (3<<4) | (1<<1) = 0x32
        assert self._g(b"\x32") == (0, 3)

    def test_form1_west(self):
        # dir=2 (W), mag=2 -> (2<<4) | (2<<1) = 0x24
        assert self._g(b"\x24") == (-2, 0)

    def test_form1_south(self):
        # dir=3 (S), mag=4 -> (4<<4) | (3<<1) = 0x46
        assert self._g(b"\x46") == (0, -4)

    def test_form1_ne(self):
        # dir=4 (NE), mag=1 -> (1<<4) | (4<<1) = 0x18
        assert self._g(b"\x18") == (1, 1)

    def test_form1_sw(self):
        # dir=6 (SW), mag=2 -> (2<<4) | (6<<1) = 0x2c
        assert self._g(b"\x2c") == (-2, -2)

    # Form 2 (bit0=1): bit1 = x sign, bits 2+ = |x|, then signed-int y.
    def test_form2_positive_xy(self):
        # x_sign=0, x_mag=5 -> uint = (5<<2) | (0<<1) | 1 = 0x15; y=+3 (signed raw 6)
        assert self._g(b"\x15\x06") == (5, 3)

    def test_form2_negative_xy(self):
        # x_sign=1, x_mag=2 -> uint = (2<<2) | 0b11 = 0x0b; y=-1 (signed raw 3)
        assert self._g(b"\x0b\x03") == (-2, -1)


class TestThreeDelta:
    def test_east(self):
        # dir=0, mag=5 -> (5<<3) | 0 = 0x28
        assert oas.decode_3_delta(io.BytesIO(b"\x28")) == (5, 0)

    def test_se(self):
        # dir=7 (SE), mag=2 -> (2<<3) | 7 = 0x17
        assert oas.decode_3_delta(io.BytesIO(b"\x17")) == (2, -2)


class TestTwoDelta:
    def test_west(self):
        # dir=2 (W), mag=3 -> (3<<2) | 2 = 0x0e
        assert oas.decode_2_delta(io.BytesIO(b"\x0e")) == (-3, 0)

    def test_north(self):
        # dir=1 (N), mag=4 -> (4<<2) | 1 = 0x11
        assert oas.decode_2_delta(io.BytesIO(b"\x11")) == (0, 4)


# ── M1.10: repetition (§7.6) ─────────────────────────────────────────────────


class TestRepetition:
    def _rep(self, bs: bytes) -> tuple:
        return oas.decode_repetition(io.BytesIO(bs))

    def test_type0_reuse_modal(self):
        rtype, offsets = self._rep(b"\x00")
        assert rtype == 0
        assert offsets == []

    def test_type1_regular_grid(self):
        # type=1, nx-2=0 (nx=2), ny-2=1 (ny=3), x_space=10, y_space=5
        rtype, offs = self._rep(b"\x01\x00\x01\x0a\x05")
        assert rtype == 1
        # 6 anchors, j-outer (rows of x):
        # j=0: (0,0), (10,0); j=1: (0,5), (10,5); j=2: (0,10), (10,10)
        assert offs == [(0, 0), (10, 0), (0, 5), (10, 5), (0, 10), (10, 10)]

    def test_type2_x_only(self):
        # type=2, nx-2=2 (nx=4), x_space=3
        rtype, offs = self._rep(b"\x02\x02\x03")
        assert rtype == 2
        assert offs == [(0, 0), (3, 0), (6, 0), (9, 0)]

    def test_type3_y_only(self):
        rtype, offs = self._rep(b"\x03\x01\x07")
        assert rtype == 3
        assert offs == [(0, 0), (0, 7), (0, 14)]

    def test_type4_arbitrary_x(self):
        # nx-2=1 (nx=3); gaps = [5, 10]; positions cumulate from 0
        rtype, offs = self._rep(b"\x04\x01\x05\x0a")
        assert rtype == 4
        assert offs == [(0, 0), (5, 0), (15, 0)]

    def test_type5_x_grid(self):
        # nx-2=0 (nx=2), grid=4, gaps=[3] -> positions 0, 12
        rtype, offs = self._rep(b"\x05\x00\x04\x03")
        assert rtype == 5
        assert offs == [(0, 0), (12, 0)]

    def test_type6_arbitrary_y(self):
        rtype, offs = self._rep(b"\x06\x01\x03\x04")
        assert rtype == 6
        assert offs == [(0, 0), (0, 3), (0, 7)]

    def test_type7_y_grid(self):
        rtype, offs = self._rep(b"\x07\x00\x10\x02")
        assert rtype == 7
        assert offs == [(0, 0), (0, 32)]

    def test_type8_two_d_regular(self):
        # nn-2=0 (nn=2), mm-2=0 (mm=2); n_vec = g-delta E mag 5 (0x50); m_vec = N mag 3 (0x32)
        rtype, offs = self._rep(b"\x08\x00\x00\x50\x32")
        assert rtype == 8
        # j-outer: j=0: (0,0),(5,0); j=1: (0,3),(5,3)
        assert offs == [(0, 0), (5, 0), (0, 3), (5, 3)]

    def test_type9_diagonal(self):
        # nd-2=1 (nd=3); d_vec = g-delta NE mag 2 (dir=4, mag=2 → (2<<4)|(4<<1) = 0x28)
        rtype, offs = self._rep(b"\x09\x01\x28")
        assert rtype == 9
        assert offs == [(0, 0), (2, 2), (4, 4)]

    def test_type10_two_d_list(self):
        # nd-2=1 (nd=3); two g-deltas (East mag 5 = 0x50, North mag 4 = 0x42)
        rtype, offs = self._rep(b"\x0a\x01\x50\x42")
        assert rtype == 10
        # cumulative: (0,0), (5,0), (5,4)
        assert offs == [(0, 0), (5, 0), (5, 4)]

    def test_type11_two_d_list_grid(self):
        # nd-2=0 (nd=2), grid=3; one g-delta East mag 5 (0x50) -> (15, 0) after grid scale
        rtype, offs = self._rep(b"\x0b\x00\x03\x50")
        assert rtype == 11
        assert offs == [(0, 0), (15, 0)]

    def test_unknown_type(self):
        with pytest.raises(oas.OasisFormatError):
            self._rep(b"\x0c")


# ── M1.10: OasisStream cblock substream ──────────────────────────────────────


class TestOasisStream:
    def test_reads_from_base_when_no_cblock(self):
        s = oas.OasisStream(io.BytesIO(b"\x01\x02\x03"))
        assert s.read(2) == b"\x01\x02"
        assert s.tell() == 2
        assert s.cblock_depth == 0

    def test_push_cblock_diverts_reads(self):
        s = oas.OasisStream(io.BytesIO(b"OUTER"))
        s.push_cblock(b"INNER")
        assert s.cblock_depth == 1
        assert s.read(5) == b"INNER"
        # Drained: maybe_pop_exhausted should pop and we return to outer.
        popped = s.maybe_pop_exhausted()
        assert popped == 1
        assert s.cblock_depth == 0
        assert s.read(5) == b"OUTER"

    def test_partial_read_then_more(self):
        s = oas.OasisStream(io.BytesIO(b""))
        s.push_cblock(b"ABCDEF")
        assert s.read(3) == b"ABC"
        # Not exhausted yet — pop should be a no-op.
        assert s.maybe_pop_exhausted() == 0
        assert s.cblock_depth == 1
        assert s.read(3) == b"DEF"
        assert s.maybe_pop_exhausted() == 1

    def test_seek_within_cblock(self):
        s = oas.OasisStream(io.BytesIO(b""))
        s.push_cblock(b"ABCDEF")
        s.read(3)
        s.seek(0)
        assert s.read(2) == b"AB"


# ── M1.10: PLACEMENT decoder ─────────────────────────────────────────────────


def _placement_reader(bs: bytes) -> oas.OasisReader:
    """Build an OasisReader pointed at hand-crafted bytes without going
    through __init__ (which would demand a real file + magic check)."""
    reader = oas.OasisReader.__new__(oas.OasisReader)
    reader._f = oas.OasisStream(io.BytesIO(bs))
    reader._modal = oas.ModalState()
    reader._current_cell = None
    reader._last_record_start = 0
    return reader


class TestPlacement:
    # PLACEMENT info-byte N-bit convention (matches SEMI P39 §22.6 + the
    # PROPERTY decoder in this module): N == 1 -> cell ref is a refnum
    # (compact uint), N == 0 -> cell ref is an inline a-string. The
    # earlier version of this test file had N swapped, which paired with
    # a matching bug in _read_placement and only blew up on production
    # files where most PLACEMENTs use the compact (N=1, refnum) form.
    def test_refnum_with_absolute_xy(self):
        # info = C=1 N=1 X=1 Y=1 R=0 A=0 F=0 -> 0b11110000 = 0xf0
        # refnum=2, x=+10 (signed raw 20), y=+20 (signed raw 40)
        reader = _placement_reader(bytes([0xf0, 2, 20, 40]))
        result = reader._read_placement(with_mag=False)
        assert result["cell_ref"] == 2
        assert result["cell_ref_kind"] == "refnum"
        assert result["x"] == 10
        assert result["y"] == 20
        assert result["angle"] == 0.0
        assert result["magnification"] == 1.0
        assert result["flip"] is False
        assert result["repetition_type"] is None

    def test_angle_quarter_turns(self):
        # info bits 2,1 = AA: 00=0deg, 01=90, 10=180, 11=270.
        # Use the refnum form (N=1) so we can put a single byte after.
        for aa, expected in [(0, 0.0), (1, 90.0), (2, 180.0), (3, 270.0)]:
            info = 0xc0 | (aa << 1)   # C=1, N=1, AA=aa
            reader = _placement_reader(bytes([info, 0]))   # refnum 0
            result = reader._read_placement(with_mag=False)
            assert result["angle"] == expected, f"AA={aa}"

    def test_inline_name_string(self):
        # info = C=1, N=0 (name follows) -> 0b10000000 = 0x80
        # name a-string: length=4 + "CELL"
        reader = _placement_reader(bytes([0x80, 4]) + b"CELL")
        result = reader._read_placement(with_mag=False)
        assert result["cell_ref"] == "CELL"
        assert result["cell_ref_kind"] == "name"

    def test_modal_cell_reuse(self):
        # First placement sets modal cell (C=1, N=1, refnum=7).
        reader = _placement_reader(bytes([0xc0, 7]))
        first = reader._read_placement(with_mag=False)
        assert first["cell_ref"] == 7
        # Re-point at a new byte stream but keep modal state (info=0 -> C=0).
        reader._f = oas.OasisStream(io.BytesIO(bytes([0x00])))
        second = reader._read_placement(with_mag=False)
        assert second["cell_ref"] == 7
        assert second["cell_ref_kind"] == "modal"

    def test_relative_xy_accumulates(self):
        # First (absolute) placement at (10, 20) — info = C=1, N=1, X=1, Y=1
        # -> 0xf0, then refnum=1, x raw 20, y raw 40.
        reader = _placement_reader(bytes([0xf0, 1, 20, 40]))
        reader._read_placement(with_mag=False)
        reader._modal.xy_relative = True
        # Second placement adds (+5, -3): x raw 10, y raw 7.
        reader._f = oas.OasisStream(io.BytesIO(bytes([0xf0, 1, 10, 7])))
        result = reader._read_placement(with_mag=False)
        assert result["x"] == 15
        assert result["y"] == 17

    def test_with_mag_and_arbitrary_angle(self):
        # Record 18: info bit 2 (M) and bit 1 (A); C=0 so no cell ref
        info = 0b00000110   # M=1, A=1
        # mag real type 0 + uint 2 -> 2.0; angle real type 0 + uint 45 -> 45.0
        reader = _placement_reader(bytes([info, 0, 2, 0, 45]))
        result = reader._read_placement(with_mag=True)
        assert result["magnification"] == 2.0
        assert result["angle"] == 45.0
        assert result["flip"] is False

    def test_flip_bit(self):
        # C=1, N=1, F=1 -> 0xc1, then refnum=3
        info = 0xc0 | 0x01
        reader = _placement_reader(bytes([info, 3]))
        result = reader._read_placement(with_mag=False)
        assert result["flip"] is True

    def test_with_repetition(self):
        # info = C=1, N=1, R=1 -> 0xc8, refnum=1, repetition type=2 (x-grid):
        # nx-2=1 (nx=3), x_space=5
        reader = _placement_reader(bytes([0xc8, 1, 0x02, 0x01, 0x05]))
        result = reader._read_placement(with_mag=False)
        assert result["repetition_type"] == 2
        assert result["repetition_offsets"] == [(0, 0), (5, 0), (10, 0)]

    def test_modal_repetition_reuse(self):
        # First placement sets modal repetition via type 3 (y-list).
        reader = _placement_reader(bytes([0xc8, 1, 0x03, 0x00, 0x04]))
        first = reader._read_placement(with_mag=False)
        assert first["repetition_offsets"] == [(0, 0), (0, 4)]
        # Second placement uses R=1 but repetition-type 0 (reuse modal).
        reader._f = oas.OasisStream(io.BytesIO(bytes([0xc8, 1, 0x00])))
        second = reader._read_placement(with_mag=False)
        assert second["repetition_type"] == 3
        assert second["repetition_offsets"] == [(0, 0), (0, 4)]

    def test_modal_repetition_without_prior_raises(self):
        # R=1 with type 0 but no prior repetition is set -> OasisFormatError
        reader = _placement_reader(bytes([0xc8, 1, 0x00]))
        with pytest.raises(oas.OasisFormatError):
            reader._read_placement(with_mag=False)


# ── M1.10: CBLOCK record + transparent substream ─────────────────────────────


def _make_uint(n: int) -> bytes:
    """Encode a non-negative integer in the variable-length OASIS uint format."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _raw_deflate(data: bytes) -> bytes:
    """Compress with raw deflate (no zlib header/checksum), matching SEMI P39 §35."""
    co = zlib.compressobj(level=9, wbits=-15)
    return co.compress(data) + co.flush()


class TestCBlock:
    def _build_min_oasis(self, cblock_inner: bytes) -> bytes:
        """Assemble a minimal valid OASIS file containing one CBLOCK
        between START and END.

        Layout: MAGIC + START(offset_flag=0, all-zero offsets) + CBLOCK + END.
        """
        magic = oas.MAGIC

        # START: id=1, version a-string "1.0", unit real (type 0 + uint 1),
        # offset_flag=0, then 6 (strict, offset) pairs of zeros.
        start = bytes([oas.START])
        start += _make_uint(3) + b"1.0"
        start += bytes([0]) + _make_uint(1)   # unit = 1
        start += _make_uint(0)                # offset_flag = 0
        start += bytes([0] * 12)              # 6 pairs of zeros

        comp = _raw_deflate(cblock_inner)
        cblock = bytes([oas.CBLOCK])
        cblock += _make_uint(0)                 # comp_type = 0 (deflate)
        cblock += _make_uint(len(cblock_inner)) # uncompressed size
        cblock += _make_uint(len(comp))         # compressed size
        cblock += comp

        end = bytes([oas.END]) + _make_uint(0)  # validation_scheme = 0

        return magic + start + cblock + end

    def test_records_inside_cblock_surface_to_caller(self, tmp_path: Path):
        # Inner content: one CELLNAME(implicit) -> id=3 + a-string "CELL"
        inner = bytes([oas.CELLNAME_IMP]) + _make_uint(4) + b"CELL"
        path = tmp_path / "cblock.oas"
        path.write_bytes(self._build_min_oasis(inner))

        seen = []
        with oas.OasisReader(path) as reader:
            for rid, payload in reader.iter_records():
                seen.append((rid, payload))

        # Expect: START, CBLOCK, CELLNAME_IMP (from inside CBLOCK), END.
        rids = [r for r, _ in seen]
        assert rids == [oas.START, oas.CBLOCK, oas.CELLNAME_IMP, oas.END]
        # CELLNAME from inside the cblock should decode to "CELL".
        cn_payload = seen[2][1]
        assert cn_payload["name"] == b"CELL"
        # CBLOCK header reports a depth of 1 at the moment it was decoded.
        assert seen[1][1]["cblock_depth"] == 1

    def test_cblock_auto_pop_returns_to_outer_stream(self, tmp_path: Path):
        # Two records in the CBLOCK + an OUTER (top-level) CELLNAME between
        # CBLOCK and END would not be valid (END must come right after
        # table records), but we can put two CELLNAMEs inside the CBLOCK
        # and confirm both are yielded.
        inner = (
            bytes([oas.CELLNAME_IMP]) + _make_uint(1) + b"A"
            + bytes([oas.CELLNAME_IMP]) + _make_uint(1) + b"B"
        )
        path = tmp_path / "cblock_multi.oas"
        path.write_bytes(self._build_min_oasis(inner))

        names = []
        with oas.OasisReader(path) as reader:
            for rid, payload in reader.iter_records():
                if rid == oas.CELLNAME_IMP:
                    names.append(payload["name"])
        assert names == [b"A", b"B"]

    def test_unsupported_comp_type_raises(self, tmp_path: Path):
        # Build a CBLOCK with comp_type=1 (undefined) and check we raise.
        magic = oas.MAGIC
        start = bytes([oas.START]) + _make_uint(3) + b"1.0"
        start += bytes([0]) + _make_uint(1) + _make_uint(0) + bytes([0] * 12)
        cblock = bytes([oas.CBLOCK]) + _make_uint(1) + _make_uint(0) + _make_uint(0)
        end = bytes([oas.END]) + _make_uint(0)
        path = tmp_path / "bad_cblock.oas"
        path.write_bytes(magic + start + cblock + end)
        with oas.OasisReader(path) as reader:
            with pytest.raises(oas.OasisFormatError):
                for _ in reader.iter_records():
                    pass


# ── M1.10: XELEMENT skip ─────────────────────────────────────────────────────


class TestXElement:
    def test_decode_attribute_and_data_len(self):
        reader = _placement_reader(b"")   # we re-point _f below
        reader._f = oas.OasisStream(io.BytesIO(_make_uint(42) + _make_uint(3) + b"abc"))
        result = reader._read_xelement()
        assert result["attribute"] == 42
        assert result["data_len"] == 3


# ── M1.11: point-list decoder (§7.7.9) ───────────────────────────────────────


def _pl(bs: bytes, *, for_polygon: bool = False) -> list:
    return oas.decode_point_list(io.BytesIO(bs), for_polygon=for_polygon)


class TestPointList:
    """Six point-list types — bytes lay out as ``type, n, n deltas``."""

    def test_type0_horizontal_zigzag_path(self):
        # type=0 (h-first), n=3, deltas = +5 (x), +3 (y), +5 (x)
        # signed-ints: +5 -> 10, +3 -> 6, +5 -> 10
        # Expected anchor + 3 zig-zag steps: (0,0), (5,0), (5,3), (10,3)
        pts = _pl(b"\x00\x03\x0a\x06\x0a", for_polygon=False)
        assert pts == [(0, 0), (5, 0), (5, 3), (10, 3)]

    def test_type0_polygon_auto_closes(self):
        # type=0, n=2 (legal: even count zig-zag closes cleanly into a
        # Manhattan rectangle). Two deltas: +5 along x, +3 along y.
        # After 2 iters h ends back at True (axis "x next"); closure
        # therefore pushes (0, y) to make the final segment vertical.
        pts = _pl(b"\x00\x02\x0a\x06", for_polygon=True)
        # Sequence: (0,0) -> (5,0) -> (5,3) -> closure (0, 3)
        assert pts == [(0, 0), (5, 0), (5, 3), (0, 3)]

    def test_type1_vertical_zigzag_path(self):
        # type=1 (v-first), n=2, deltas = +4 (y), +6 (x)
        pts = _pl(b"\x01\x02\x08\x0c", for_polygon=False)
        assert pts == [(0, 0), (0, 4), (6, 4)]

    def test_type2_two_delta(self):
        # type=2, n=2, two 2-deltas: E mag 5 (0x14), N mag 3 (0x0d)
        # 2-delta: dir at bits 1-0, mag at bits 2+.
        # E (dir 0) mag 5 -> (5<<2)|0 = 0x14
        # N (dir 1) mag 3 -> (3<<2)|1 = 0x0d
        pts = _pl(b"\x02\x02\x14\x0d", for_polygon=False)
        assert pts == [(0, 0), (5, 0), (5, 3)]

    def test_type3_three_delta(self):
        # type=3, n=2, 3-deltas: NE mag 2 (dir 4) = (2<<3)|4 = 0x14
        # then SE mag 1 (dir 7) = (1<<3)|7 = 0x0f
        pts = _pl(b"\x03\x02\x14\x0f", for_polygon=False)
        assert pts == [(0, 0), (2, 2), (3, 1)]

    def test_type4_g_delta(self):
        # type=4, n=2, g-deltas: form-1 East mag 5 (0x50) then North mag 3 (0x32)
        pts = _pl(b"\x04\x02\x50\x32", for_polygon=False)
        assert pts == [(0, 0), (5, 0), (5, 3)]

    def test_type5_velocity_g_delta(self):
        # type=5, n=2, deltas accumulate as velocity then position.
        # first g-delta: E mag 5 (0x50)  -> velocity (5, 0), pos += (5, 0) -> (5, 0)
        # second g-delta: N mag 1 (0x12) -> velocity (5, 1), pos += (5, 1) -> (10, 1)
        pts = _pl(b"\x05\x02\x50\x12", for_polygon=False)
        assert pts == [(0, 0), (5, 0), (10, 1)]

    def test_unknown_type(self):
        with pytest.raises(oas.OasisFormatError):
            _pl(b"\x06\x01\x00")

    def test_zero_count_rejected(self):
        with pytest.raises(oas.OasisFormatError):
            _pl(b"\x02\x00")


# ── M1.11: geometry-record decoders (§23-28) ─────────────────────────────────


def _geom_reader(bs: bytes, *,
                 wanted_layers=None,
                 prime_modal: dict | None = None):
    """Bare reader for testing geometry record decoders in isolation."""
    reader = oas.OasisReader.__new__(oas.OasisReader)
    reader._f = oas.OasisStream(io.BytesIO(bs))
    reader._modal = oas.ModalState()
    reader._current_cell = None
    reader._last_record_start = 0
    reader._wanted_layers = wanted_layers
    if prime_modal:
        for k, v in prime_modal.items():
            setattr(reader._modal, k, v)
    return reader


class TestRectangle:
    def test_all_fields_present_non_square(self):
        # info = S=0 W=1 H=1 X=1 Y=1 R=0 D=1 L=1 -> 0b01111011 = 0x7b
        # bytes: layer=5, datatype=2, width=100, height=50, x=+10 (raw 20),
        #        y=+20 (raw 40)
        reader = _geom_reader(bytes([0x7b, 5, 2, 100, 50, 20, 40]))
        r = reader._read_rectangle()
        assert r["layer"] == 5
        assert r["datatype"] == 2
        assert r["width"] == 100
        assert r["height"] == 50
        assert r["x"] == 10
        assert r["y"] == 20
        assert r["repetition_type"] is None
        assert r["filtered_out"] is False

    def test_square_skips_height_read(self):
        # info = S=1 W=1 H=0 X=1 Y=1 R=0 D=0 L=0 -> 0b11011000 = 0xd8
        # bytes: width=42, x=+5 (raw 10), y=+6 (raw 12). Height should
        # mirror width without consuming any extra byte.
        reader = _geom_reader(bytes([0xd8, 42, 10, 12]))
        r = reader._read_rectangle()
        assert r["width"] == 42
        assert r["height"] == 42
        assert r["x"] == 5
        assert r["y"] == 6

    def test_modal_reuse(self):
        # First rect sets layer/datatype/width/height/x/y. Second has all
        # bits clear -> reuses every modal.
        reader = _geom_reader(bytes([0x7b, 5, 2, 100, 50, 20, 40]))
        reader._read_rectangle()
        reader._f = oas.OasisStream(io.BytesIO(bytes([0x00])))
        r = reader._read_rectangle()
        assert r["layer"] == 5
        assert r["datatype"] == 2
        assert r["width"] == 100
        assert r["height"] == 50

    def test_layer_filter_marks_filtered_out(self):
        # wanted_layers = {(7, 0)}; this rect is on (5, 2) -> filtered
        reader = _geom_reader(
            bytes([0x7b, 5, 2, 100, 50, 20, 40]),
            wanted_layers={(7, 0)},
        )
        r = reader._read_rectangle()
        assert r["filtered_out"] is True
        assert "repetition_offsets" not in r   # heavy data dropped

    def test_layer_filter_passes_wanted_layer(self):
        reader = _geom_reader(
            bytes([0x7b, 5, 2, 100, 50, 20, 40]),
            wanted_layers={(5, 2)},
        )
        r = reader._read_rectangle()
        assert r["filtered_out"] is False
        assert "repetition_offsets" in r


class TestPolygon:
    def test_with_point_list(self):
        # info = P=1 X=1 Y=1 D=1 L=1 -> 0b00111011 = 0x3b
        # bytes: layer=1, datatype=0, point-list type=2 n=2 (0x14, 0x0d),
        # x=+0 (raw 0), y=+0 (raw 0)
        bs = bytes([0x3b, 1, 0, 2, 2, 0x14, 0x0d, 0, 0])
        reader = _geom_reader(bs)
        p = reader._read_polygon()
        assert p["layer"] == 1
        assert p["datatype"] == 0
        assert p["point_count"] == 3   # (0,0) + 2 deltas
        assert p["points"] == [(0, 0), (5, 0), (5, 3)]

    def test_filtered_drops_points(self):
        bs = bytes([0x3b, 1, 0, 2, 2, 0x14, 0x0d, 0, 0])
        reader = _geom_reader(bs, wanted_layers={(99, 99)})
        p = reader._read_polygon()
        assert p["filtered_out"] is True
        assert "points" not in p
        assert p["point_count"] == 3   # still counted


class TestPath:
    def test_halfwidth_and_points(self):
        # info = E=0 W=1 P=1 X=1 Y=1 R=0 D=1 L=1 -> 0b01111011 = 0x7b
        # bytes: layer=1, datatype=0, halfwidth=10,
        # point-list type=2 n=1 (E mag 5 -> 0x14), x=0, y=0
        bs = bytes([0x7b, 1, 0, 10, 2, 1, 0x14, 0, 0])
        reader = _geom_reader(bs)
        p = reader._read_path()
        assert p["half_width"] == 10
        assert p["point_count"] == 2
        assert p["points"] == [(0, 0), (5, 0)]
        assert p["start_extension"] == 0
        assert p["end_extension"] == 0

    def test_extension_explicit(self):
        # info = E=1 W=1 P=0 X=0 Y=0 R=0 D=0 L=0 -> 0xc0
        # halfwidth=10, e = 0b1111 = 0x0f (start=11 explicit, end=11 explicit)
        # then start_ext signed +3 (raw 6), end_ext signed -1 (raw 3)
        bs = bytes([0xc0, 10, 0x0f, 6, 3])
        reader = _geom_reader(bs)
        p = reader._read_path()
        assert p["half_width"] == 10
        assert p["start_extension"] == 3
        assert p["end_extension"] == -1

    def test_extension_halfwidth_mode(self):
        # e = 0b1010 = 0x0a (start mode 10 = halfwidth, end mode 10 = halfwidth)
        bs = bytes([0xc0, 7, 0x0a])
        reader = _geom_reader(bs)
        p = reader._read_path()
        assert p["start_extension"] == 7
        assert p["end_extension"] == 7

    def test_extension_zero_mode(self):
        # e = 0b0101 = 0x05 (start mode 01 = zero, end mode 01 = zero)
        bs = bytes([0xc0, 7, 0x05])
        reader = _geom_reader(bs)
        p = reader._read_path()
        assert p["start_extension"] == 0
        assert p["end_extension"] == 0


class TestTrapezoid:
    def test_record_23_full(self):
        # info = W=1 H=1 X=1 Y=1 R=0 D=0 L=0 -> 0x78
        # bytes: width=100, height=80, delta_a=+5 (raw 10), delta_b=-3 (raw 7),
        # x=+10 (raw 20), y=+20 (raw 40)
        bs = bytes([0x78, 100, 80, 10, 7, 20, 40])
        reader = _geom_reader(bs)
        t = reader._read_trapezoid(23)
        assert t["width"] == 100
        assert t["height"] == 80
        assert t["delta_a"] == 5
        assert t["delta_b"] == -3
        assert t["x"] == 10
        assert t["y"] == 20

    def test_record_24_only_delta_a(self):
        bs = bytes([0x78, 100, 80, 10, 20, 40])
        reader = _geom_reader(bs)
        t = reader._read_trapezoid(24)
        assert t["delta_a"] == 5
        assert t["delta_b"] == 0

    def test_record_25_only_delta_b(self):
        bs = bytes([0x78, 100, 80, 7, 20, 40])
        reader = _geom_reader(bs)
        t = reader._read_trapezoid(25)
        assert t["delta_a"] == 0
        assert t["delta_b"] == -3


class TestCTrapezoid:
    def test_full(self):
        # info = T=1 W=1 H=1 X=1 Y=1 R=0 D=0 L=0 -> 0xf8
        # bytes: ctype=4, width=100, height=80, x=+10 (raw 20), y=+20 (raw 40)
        bs = bytes([0xf8, 4, 100, 80, 20, 40])
        reader = _geom_reader(bs)
        c = reader._read_ctrapezoid()
        assert c["ctrapezoid_type"] == 4
        assert c["width"] == 100
        assert c["height"] == 80
        assert c["x"] == 10
        assert c["y"] == 20


class TestCircle:
    def test_full(self):
        # info = r=1 X=1 Y=1 R=0 D=1 L=1 -> 0b00111011 = 0x3b
        # bytes: layer=1, datatype=0, radius=50, x=+5 (raw 10), y=+5 (raw 10)
        bs = bytes([0x3b, 1, 0, 50, 10, 10])
        reader = _geom_reader(bs)
        c = reader._read_circle()
        assert c["radius"] == 50
        assert c["x"] == 5
        assert c["y"] == 5


class TestRepetitionAnalytics:
    """M3.5e: analytic extent / count + numpy expansion of repetitions,
    so a million-instance array is never materialized just to bbox it."""

    def test_extent_type1_grid(self):
        # 1000x1000 grid, 1000nm pitch -> 0..999000 each axis.
        assert oas.repetition_extent(1, (1000, 1000, 1000, 1000)) == \
            (0, 0, 999000, 999000)

    def test_extent_type2_xrow(self):
        assert oas.repetition_extent(2, (17, 100)) == (0, 0, 1600, 0)

    def test_extent_type3_ycol(self):
        assert oas.repetition_extent(3, (5, 50)) == (0, 0, 0, 200)

    def test_extent_none(self):
        assert oas.repetition_extent(None, None) == (0, 0, 0, 0)

    def test_count(self):
        assert oas.repetition_count(1, (1000, 1000, 1000, 1000)) == 1_000_000
        assert oas.repetition_count(2, (17, 100)) == 17
        assert oas.repetition_count(None, None) == 1

    def test_offsets_np_matches_expand(self):
        # Vectorized numpy expansion must equal the reference list expansion.
        for rtype, raw in [(1, (3, 2, 10, 20)), (2, (4, 7)), (3, (5, 9)),
                           (9, (3, (4, 6)))]:
            ref = sorted(oas.expand_repetition(rtype, raw))
            got = sorted(map(tuple, oas.repetition_offsets_np(rtype, raw)
                             .astype(int).tolist()))
            assert got == ref, (rtype, got, ref)


class TestEndRecord:
    """The END record may be padded (e.g. with 0x80 bytes) after the
    validation scheme. _read_end must not overflow on that padding, and
    iter_records must still reach END with the preceding geometry intact
    (regression: real Calibre D2DB END crashed the ROI decoder)."""

    def _file(self, end_tail: bytes) -> bytes:
        start = (bytes([oas.START]) + _make_uint(3) + b"1.0"
                 + bytes([0]) + _make_uint(1) + _make_uint(0) + bytes([0] * 12))
        cell = bytes([oas.CELL_REFNUM]) + _make_uint(0)
        rect = bytes([oas.RECTANGLE, 0x7b, 17, 0, 10, 10, 0, 0])
        return oas.MAGIC + start + cell + rect + bytes([oas.END]) + end_tail

    def test_padded_end_does_not_overflow(self, tmp_path):
        p = tmp_path / "padded.oas"
        p.write_bytes(self._file(bytes([0x80] * 16)))   # 0x80 padding tail
        rids = []
        with oas.OasisReader(p) as r:
            for rid, _ in r.iter_records():
                rids.append(rid)
        assert oas.RECTANGLE in rids        # geometry kept
        assert rids[-1] == oas.END          # reached END gracefully

    def test_clean_scheme0_end_still_works(self, tmp_path):
        p = tmp_path / "clean.oas"
        p.write_bytes(self._file(_make_uint(0)))   # validation scheme 0
        with oas.OasisReader(p) as r:
            rids = [rid for rid, _ in r.iter_records()]
        assert rids[-1] == oas.END


class TestText:
    def test_inline_string(self):
        # info = C=1 N=0 X=1 Y=1 R=0 T=1 L=1 -> 0b01011011 = 0x5b
        # bytes: string a-string len=3 "abc", textlayer=2, texttype=0,
        # x=+10 (raw 20), y=+20 (raw 40)
        bs = bytes([0x5b, 3]) + b"abc" + bytes([2, 0, 20, 40])
        reader = _geom_reader(bs)
        t = reader._read_text()
        assert t["text"] == "abc"
        assert t["text_layer"] == 2
        assert t["text_type"] == 0
        assert t["x"] == 10
        assert t["y"] == 20

    def test_refnum_string(self):
        # info = C=1 N=1 (refnum) X=0 Y=0 R=0 T=0 L=0 -> 0x60
        # bytes: refnum=5
        bs = bytes([0x60, 5])
        reader = _geom_reader(bs)
        t = reader._read_text()
        assert t["text"] == 5   # refnum reported as int

    def test_text_filter_uses_textlayer_texttype(self):
        # text on (textlayer=2, texttype=0), filter set wants only (1, 0)
        bs = bytes([0x5b, 3]) + b"abc" + bytes([2, 0, 20, 40])
        reader = _geom_reader(bs, wanted_layers={(1, 0)})
        t = reader._read_text()
        assert t["filtered_out"] is True


# ── M1.11: layer filter at iter_records level ────────────────────────────────


class TestLayerFilterRoundtrip:
    """End-to-end: build a tiny OASIS file containing 2 RECTANGLEs on
    different layers, parse with wanted_layers, confirm only one comes
    through unfiltered while both records are yielded (so counts stay
    accurate)."""

    def _build(self, rects: list[tuple[int, int]]) -> bytes:
        """rects = list of (layer, datatype). Each rect uses
        W=1, H=1, X=1, Y=1, D=1, L=1 with fixed sizes."""
        magic = oas.MAGIC
        start = bytes([oas.START]) + _make_uint(3) + b"1.0"
        start += bytes([0]) + _make_uint(1) + _make_uint(0)
        start += bytes([0] * 12)
        cell = bytes([oas.CELL_REFNUM]) + _make_uint(0)
        body = cell
        for L, D in rects:
            # record-id RECTANGLE (20), then info byte 0x7b
            # (W=1 H=1 X=1 Y=1 D=1 L=1, no S, no R), then fields
            body += bytes([oas.RECTANGLE, 0x7b, L, D, 10, 10, 0, 0])
        end = bytes([oas.END]) + _make_uint(0)
        return magic + start + body + end

    def test_no_filter_yields_full_payload(self, tmp_path: Path):
        path = tmp_path / "two_rects.oas"
        path.write_bytes(self._build([(1, 0), (2, 0)]))

        rect_payloads = []
        with oas.OasisReader(path) as reader:
            for rid, payload in reader.iter_records():
                if rid == oas.RECTANGLE:
                    rect_payloads.append(payload)
        assert len(rect_payloads) == 2
        assert all(p["filtered_out"] is False for p in rect_payloads)
        assert all("repetition_offsets" in p for p in rect_payloads)

    def test_filter_marks_unwanted_but_still_yields(self, tmp_path: Path):
        path = tmp_path / "two_rects.oas"
        path.write_bytes(self._build([(1, 0), (2, 0)]))

        with oas.OasisReader(path, wanted_layers={(1, 0)}) as reader:
            rect_payloads = [
                payload for rid, payload in reader.iter_records()
                if rid == oas.RECTANGLE
            ]
        assert len(rect_payloads) == 2
        kept = [p for p in rect_payloads if not p["filtered_out"]]
        dropped = [p for p in rect_payloads if p["filtered_out"]]
        assert len(kept) == 1 and kept[0]["layer"] == 1
        assert len(dropped) == 1 and dropped[0]["layer"] == 2
        assert "repetition_offsets" not in dropped[0]


# ── M1.13.3b: Fast-path byte readers ─────────────────────────────────────────


class TestFastByteReaders:
    """OasisStream.read_byte / read_uvarint / read_svarint walk the
    buffer directly without going through stream.read(1). Verify the
    output matches the slow path (module-level decode_unsigned_int with
    a raw BytesIO) for the cases hot decoders exercise on D2DB."""

    def test_read_byte_advances_pos(self):
        s = oas.OasisStream(io.BytesIO(b"\x00\xff\x42"))
        assert s.read_byte() == 0x00
        assert s.tell() == 1
        assert s.read_byte() == 0xFF
        assert s.read_byte() == 0x42
        assert s.tell() == 3

    def test_read_byte_raises_on_eof(self):
        s = oas.OasisStream(io.BytesIO(b"\x01"))
        s.read_byte()
        with pytest.raises(oas.OasisFormatError):
            s.read_byte()

    @pytest.mark.parametrize("value,expected_bytes", [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (16383, b"\xff\x7f"),
        (16384, b"\x80\x80\x01"),
        (0xDEAD_BEEF, None),
    ])
    def test_read_uvarint_matches_slow_path(self, value, expected_bytes):
        # Re-encode and decode both ways; outputs must match.
        bs = _make_uint(value)
        if expected_bytes is not None:
            assert bs == expected_bytes
        # Slow path via decode_unsigned_int on BytesIO.
        slow = oas.decode_unsigned_int(io.BytesIO(bs))
        # Fast path via OasisStream.read_uvarint.
        s = oas.OasisStream(io.BytesIO(bs))
        fast = s.read_uvarint()
        assert slow == fast == value
        # Cursor at end.
        assert s.tell() == len(bs)

    def test_read_svarint_matches_slow_path(self):
        # signed-int = (magnitude << 1) | sign
        for value in (0, 1, -1, 42, -42, 127, -128, 16383, -16384):
            magnitude = abs(value)
            sign = 1 if value < 0 else 0
            bs = _make_uint((magnitude << 1) | sign)
            slow = oas.decode_signed_int(io.BytesIO(bs))
            fast = oas.OasisStream(io.BytesIO(bs)).read_svarint()
            assert slow == fast == value

    def test_decode_unsigned_int_uses_fast_path_when_available(self):
        # When given an OasisStream, decode_unsigned_int yields the
        # same result as direct fast-path call.
        bs = _make_uint(123456)
        s = oas.OasisStream(io.BytesIO(bs))
        assert oas.decode_unsigned_int(s) == 123456
        assert s.tell() == len(bs)

    def test_uvarint_inside_cblock(self):
        s = oas.OasisStream(io.BytesIO(b""))
        s.push_cblock(_make_uint(256))
        assert s.read_uvarint() == 256
        s.maybe_pop_exhausted()
        assert s.cblock_depth == 0

    def test_close_releases_buffer(self):
        s = oas.OasisStream(io.BytesIO(b"data"))
        s.close()
        assert s.closed
        with pytest.raises(ValueError):
            s.read(1)

    def test_outer_position_during_cblock(self):
        # Outside CBLOCK: outer_position == tell().
        s = oas.OasisStream(io.BytesIO(b"OUTER1234"))
        s.read(5)
        assert s.outer_position == 5
        # Push CBLOCK: outer_position keeps the outer's pos.
        s.push_cblock(b"INNER")
        assert s.outer_position == 5
        s.read_byte()
        # Reading inside CBLOCK does NOT change outer_position.
        assert s.outer_position == 5
        s.read(4)
        # Drain inner -> pop -> back to outer with the saved pos.
        assert s.maybe_pop_exhausted() == 1
        assert s.tell() == 5
