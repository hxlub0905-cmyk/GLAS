"""F26 M0: the optional native decode extension must be byte-for-byte identical
to the pure-Python varint readers it replaces.

Skipped automatically when the extension isn't built (no compiler / no CI
artifact), so the suite still passes everywhere; on a machine where
``oasis_fastdecode`` is present (CI build, or a local ``build_ext``) it pins the
native decoder against ``OasisStream.read_uvarint`` / ``read_svarint``.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

fast = pytest.importorskip("oasis_fastdecode")
import oasis_streamer as oas  # noqa: E402


def _encode_uvarint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | 0x80 if v else b)
        if not v:
            return bytes(out)


def _encode_svarint(v: int) -> bytes:
    return _encode_uvarint((abs(v) << 1) | (1 if v < 0 else 0))


def _py_uvarint(b: bytes) -> int:
    return oas.OasisStream(io.BytesIO(b)).read_uvarint()


def _py_svarint(b: bytes) -> int:
    return oas.OasisStream(io.BytesIO(b)).read_svarint()


_UVALS = [0, 1, 2, 127, 128, 129, 255, 256, 16383, 16384,
          2 ** 16, 2 ** 31, 2 ** 32, 2 ** 53 - 1, 123_456_789, 2 ** 63 - 1]
_SVALS = [0, 1, -1, 2, -2, 5, -5, 1000, -1000, 2 ** 31, -(2 ** 31),
          2 ** 40, -(2 ** 40)]


def test_native_version_and_selftest():
    assert fast.selftest() == fast.VERSION


@pytest.mark.parametrize("v", _UVALS)
def test_uvarint_matches_python(v):
    enc = _encode_uvarint(v)
    val, pos = fast.decode_uvarint(enc, 0)
    assert val == v == _py_uvarint(enc)
    assert pos == len(enc)


@pytest.mark.parametrize("v", _SVALS)
def test_svarint_matches_python(v):
    enc = _encode_svarint(v)
    val, pos = fast.decode_svarint(enc, 0)
    assert val == v == _py_svarint(enc)
    assert pos == len(enc)


def test_uvarint_resumes_at_offset():
    # Decoding mid-buffer returns the right new position (used to walk a record).
    blob = _encode_uvarint(300) + _encode_uvarint(7) + _encode_uvarint(2 ** 20)
    v1, p1 = fast.decode_uvarint(blob, 0)
    v2, p2 = fast.decode_uvarint(blob, p1)
    v3, p3 = fast.decode_uvarint(blob, p2)
    assert (v1, v2, v3) == (300, 7, 2 ** 20)
    assert p3 == len(blob)


def test_uvarint_eof_raises():
    with pytest.raises(Exception):
        fast.decode_uvarint(b"\x80\x80", 0)   # continuation set, then EOF
