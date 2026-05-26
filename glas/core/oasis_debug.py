"""OASIS diagnostics for debug mode (F10).

Produces a plain-text report you can paste back for debugging — covering
both directions:

* **load / parse** — point :func:`report_file` at a production ``.oas``.
  It walks the file through :mod:`oasis_streamer`, tallies a record-type
  histogram + per-layer geometry counts + START unit + cell names, and on
  any decode error captures the streamer's rich context (the hex window +
  cursor pointer baked into ``OasisFormatError``) plus the traceback.

* **export verify** — pass ``sent_layers`` (the ``(layer, datatype,
  polygons)`` actually written). The report re-reads the file and
  cross-checks the per-layer rectangle/polygon counts against what was
  sent, so a writer bug shows up as a mismatch line.

Qt-free so it stays unit-testable; the app wraps the returned string in a
copyable dialog and a ``.debug.txt`` sidecar.
"""
from __future__ import annotations

import traceback
from pathlib import Path
from typing import Iterable, Optional

import oasis_streamer as oas


def _name(b) -> str:
    if isinstance(b, bytes):
        return b.decode("ascii", errors="replace")
    return str(b)


def report_file(path, *, sent_layers: Optional[Iterable] = None,
                max_records: int = 500_000) -> str:
    """Walk ``path`` and return a diagnostic report. Never raises — any
    decode error is captured into the report text."""
    p = Path(path)
    lines: list[str] = ["=== OASIS debug report ==="]
    try:
        size = p.stat().st_size
    except OSError as exc:
        return "\n".join(lines + [f"file: {p}", f"(cannot stat: {exc})"])
    lines += [f"file: {p}", f"size: {size} bytes"]

    hist: dict[int, int] = {}
    geom: dict[tuple, list] = {}        # (layer, dt) -> [rect_count, poly_count]
    unit = None
    offset_flag = None
    cellnames: list[str] = []
    err_text: Optional[str] = None
    n = 0
    truncated = False

    try:
        for rid, payload in oas.OasisReader(p).iter_records():
            hist[rid] = hist.get(rid, 0) + 1
            if rid == oas.START:
                unit = payload.get("unit")
                offset_flag = payload.get("offset_flag")
            elif rid in (oas.CELLNAME_IMP, oas.CELLNAME_EXP):
                cellnames.append(_name(payload.get("name")))
            elif rid == oas.RECTANGLE:
                geom.setdefault((payload["layer"], payload["datatype"]), [0, 0])[0] += 1
            elif rid == oas.POLYGON:
                geom.setdefault((payload["layer"], payload["datatype"]), [0, 0])[1] += 1
            n += 1
            if n >= max_records:
                truncated = True
                break
    except Exception as exc:  # noqa: BLE001 — diagnostics must never throw
        err_text = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"

    lines.append(f"records read: {n}" + (" (truncated)" if truncated else ""))
    lines.append(f"START unit: {unit}    offset_flag: {offset_flag}")
    lines.append(f"cell names ({len(cellnames)}): "
                 + (", ".join(cellnames[:20]) + (" …" if len(cellnames) > 20 else "")
                    if cellnames else "(none)"))

    lines.append("")
    lines.append("record histogram:")
    for rid in sorted(hist):
        lines.append(f"  {oas.RECORD_NAMES.get(rid, rid):24} x {hist[rid]}")

    lines.append("")
    lines.append("geometry per layer (layer/datatype: rect + polygon):")
    if geom:
        for (layer, dt) in sorted(geom):
            rc, pc = geom[(layer, dt)]
            lines.append(f"  L{layer}/D{dt}: {rc} rect, {pc} poly")
    else:
        lines.append("  (none)")

    if sent_layers is not None:
        lines.append("")
        lines.append("round-trip check (sent vs read-back shape counts):")
        for layer, dt, polygons in sent_layers:
            sent_n = sum(1 for _ in polygons)
            rc, pc = geom.get((int(layer), int(dt)), [0, 0])
            read_n = rc + pc
            status = "OK" if read_n == sent_n else "MISMATCH"
            lines.append(f"  L{int(layer)}/D{int(dt)}: sent {sent_n}, "
                         f"read {read_n}  [{status}]")

    if err_text is not None:
        lines.append("")
        lines.append("*** DECODE ERROR ***")
        lines.append(err_text.rstrip())
    else:
        lines.append("")
        lines.append("parse: OK (reached end / record cap without error)")

    return "\n".join(lines)
