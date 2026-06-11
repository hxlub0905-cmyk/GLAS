#!/usr/bin/env python3
"""產一個帶 S_CELL_OFFSET 的合成 OASIS，供 bench harness 在無 production 檔時 smoke-test。

非 production 代表檔 —— 只為了讓 harness 的五個區段都跑得起來、驗證輸出正確。
結構：root cell "TOP" 放置 N 個 leaf cell（grid 散開），每個 leaf 內含若干 rect。
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2] / "glas" / "core"
sys.path.insert(0, str(_CORE))
import oasis_streamer as oas  # noqa: E402


def _uint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _sint(v):
    return _uint((abs(v) << 1) | (1 if v < 0 else 0))


def _ufix(n, width):
    out = []
    for i in range(width):
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if i < width - 1 else b)
    return bytes(out)


def _astr(s):
    b = s.encode()
    return _uint(len(b)) + b


def _rect(layer, w, h, x, y):
    return (bytes([oas.RECTANGLE, 0x7b]) + _uint(layer) + _uint(0)
            + _uint(w) + _uint(h) + _sint(x) + _sint(y))


def _placement(refnum, x, y):
    # PLACEMENT_NOMAG, info-byte bits C=0x80 N=0x40 X=0x20 Y=0x10 → 0xF0:
    # explicit refnum + x + y present (no angle/flip/repetition).
    return bytes([oas.PLACEMENT_NOMAG, 0xF0]) + _uint(refnum) + _sint(x) + _sint(y)


def build(n_leaf=400, rects_per_leaf=50, pitch=20000):
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
             + _uint(1000) + _uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
    # cell names: TOP=ref0, leaf_i = ref(i+1)
    cellnames = [bytes([oas.CELLNAME_IMP]) + _astr("TOP")]
    for i in range(n_leaf):
        cellnames.append(bytes([oas.CELLNAME_IMP]) + _astr(f"LEAF{i}"))

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    # bodies
    cols = int(n_leaf ** 0.5) + 1
    top_body = bytes([oas.CELL_REFNUM]) + _uint(0)
    for i in range(n_leaf):
        x = (i % cols) * pitch
        y = (i // cols) * pitch
        top_body += _placement(i + 1, x, y)

    leaf_bodies = []
    for i in range(n_leaf):
        body = bytes([oas.CELL_REFNUM]) + _uint(i + 1)
        for r in range(rects_per_leaf):
            body += _rect(17, 200, 200, r * 400, (r % 5) * 400)
        leaf_bodies.append(body)

    end = bytes([oas.END]) + _uint(0)

    # 兩遍：先用 off=0 算 header 長度，得到各 cell offset，再回填
    def assemble(offsets):
        props = b""
        for i, cn in enumerate(cellnames):
            props += cn + prop(offsets[i] if offsets else 0)
        hdr = oas.MAGIC + start + pn + props
        bodies = top_body + b"".join(leaf_bodies)
        return hdr, bodies

    hdr0, _ = assemble(None)
    base = len(hdr0)
    offsets = [base]
    cur = base + len(top_body)
    for lb in leaf_bodies:
        offsets.append(cur)
        cur += len(lb)
    hdr, bodies = assemble(offsets)
    return hdr + bodies + end


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/sample_bench.oas")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    data = build(n_leaf=n)
    out.write_bytes(data)
    print(f"wrote {out} ({len(data)/1e6:.2f} MB, {n} leaf cells)")
