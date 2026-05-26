"""Generate sample .oas files for testing the F9 OASIS writer / F10 debug mode.

No production layout needed. Writes two files next to where you run it:

  sample_good.oas    — a rectangle + a triangle + a 45-degree polygon on a
                       few layers; open this in KLayout to confirm the writer's
                       output is accepted and renders correctly.
  sample_broken.oas  — sample_good.oas with its tail bytes chopped off, so the
                       reader desyncs; feed this to File > Diagnose OASIS file…
                       (developer mode) to see the error-capture report.

Usage:
    python scripts/make_sample_oas.py            # writes into the current dir
    python scripts/make_sample_oas.py out_dir    # writes into out_dir/
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent / "glas" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import oasis_writer as w


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    layers = [
        # axis-aligned rectangle -> RECTANGLE record (1 um square at origin)
        (17, 0, [[(0, 0), (1000, 0), (1000, 1000), (0, 1000)]]),
        # triangle -> POLYGON record
        (25, 0, [[(0, 0), (2000, 0), (0, 1500)]]),
        # 45-degree / arbitrary polygon -> POLYGON (g-delta) record
        (40, 1, [[(3000, 0), (4000, 500), (3500, 1500), (2500, 1000)]]),
    ]
    good = out_dir / "sample_good.oas"
    w.write_oasis(good, layers, unit=1000, cellname="SAMPLE")
    print(f"wrote {good}  ({good.stat().st_size} bytes)")

    # Make a genuinely broken file: cut into the LAST geometry record (just
    # before the 256-byte END record) so the stream desyncs / hits EOF
    # mid-record. (Chopping the END pad alone wouldn't error — the reader
    # returns at END before reading the pad.)
    data = good.read_bytes()
    cut = max(0, len(data) - w._END_RECORD_LEN - 6)
    broken = out_dir / "sample_broken.oas"
    broken.write_bytes(data[:cut])
    print(f"wrote {broken}  (truncated mid-geometry, for testing Diagnose)")

    print("\nNext:")
    print(f"  - open {good.name} in KLayout (layers 17/0, 25/0, 40/1)")
    print(f"  - File > Diagnose OASIS file... on both (developer mode)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
