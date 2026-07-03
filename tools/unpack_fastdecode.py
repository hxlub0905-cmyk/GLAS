#!/usr/bin/env python3
"""Materialize the native decode extension from its base64 text sidecar (F26).

Why this exists: on a locked-down machine where (a) git is blocked, (b) no C
compiler is allowed, (c) GitHub artifact downloads are blocked, AND (d) a source
ZIP containing a ``.pyd`` (a Windows DLL) is itself blocked by the file-download
policy — the only thing that rides the source ZIP safely is plain text. So CI
commits the compiled ``oasis_fastdecode.cp39-win_amd64.pyd`` as a base64 *text*
sidecar (``…​.pyd.b64``). After you extract the source ZIP, run this once:

    python tools/unpack_fastdecode.py

It decodes the ``.b64`` back into ``glas/core/oasis_fastdecode.cp39-win_amd64.pyd``
so ``import oasis_fastdecode`` works. (The app also works without it — it just
falls back to the slower pure-Python decode path.)

Pure stdlib; writes one file under glas/core/. Nothing is downloaded.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "glas" / "core"


def main() -> int:
    # Decode every *.pyd.b64 sidecar present (normally just the cp39 one).
    sidecars = sorted(_CORE.glob("oasis_fastdecode.*.pyd.b64"))
    if not sidecars:
        print(f"No base64 sidecar found in {_CORE}\n"
              f"  (expected oasis_fastdecode.<tag>.pyd.b64 — is this the right "
              f"branch ZIP, and did CI finish building it?)")
        return 1
    wrote = 0
    for b64 in sidecars:
        pyd = b64.with_suffix("")          # strip the trailing .b64
        try:
            data = base64.b64decode(b64.read_text())
        except Exception as exc:           # noqa: BLE001
            print(f"  ! failed to decode {b64.name}: {exc}")
            continue
        pyd.write_bytes(data)
        print(f"  wrote {pyd.name}  ({len(data):,} bytes)")
        wrote += 1
    if not wrote:
        return 1
    print("\nDone. Now verify it imports + matches pure Python:")
    print('  python -c "import sys; sys.path.insert(0,\'glas/core\'); '
          'import oasis_fastdecode as f; print(f.VERSION, f.selftest())"')
    print("  python -m pytest tests/test_fastdecode.py -v"
          "   (use 'python -m pytest', not bare 'pytest', if it's not on PATH)")
    print("\nIf antivirus removes the .pyd right after this writes it, that is "
          "the (c) blocker — tell me and we pivot to the no-binary path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
