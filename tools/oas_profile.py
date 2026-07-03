#!/usr/bin/env python3
"""GLAS OASIS decode profiler (F26 measurement harness).

Answers the one question that decides how to speed up 解包/輸出: *where does
the time actually go on YOUR files?* Run it on a few real .oas files and read
the SUMMARY at the bottom.

    python tools/oas_profile.py BIG.oas
    python tools/oas_profile.py BIG.oas --layers 17/0,20/0
    python tools/oas_profile.py BIG.oas --decode-limit 5000000   # sample huge files
    python tools/oas_profile.py BIG.oas --roi 120000 340000 25000 --root 0 --layer 17/0

It measures three phases independently:

  1. open + name-table scan      (RandomAccessReader build — the per-file index)
  2. whole-file decode pass      (consume(): the pure decode cost, no storage)
  3. (optional) one ROI walk     (the interactive "click a defect" path)

Then it runs cProfile over a capped sample of the decode and buckets the time
into varint / dispatch+decode / zlib(CBLOCK) / store+numpy / io, so you can see
at a glance whether a native varint/record decoder (Cython) would help, or
whether the cost is elsewhere (zlib / IO / numpy — where Cython would NOT help
and borrowing gdstk/klayout for bulk ops might).

No third-party deps — only the GLAS core + stdlib. Read-only; nothing is
written.
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import time
from pathlib import Path

# Put glas/core on sys.path (same trick as conftest.py / main.py).
_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import oasis_streamer as oas        # noqa: E402
import oasis_random                 # noqa: E402


def _fmt_secs(s: float) -> str:
    if s < 1e-3:
        return f"{s * 1e6:.0f} µs"
    if s < 1.0:
        return f"{s * 1e3:.1f} ms"
    return f"{s:.2f} s"


def _fmt_n(n: float) -> str:
    return f"{n:,.0f}"


def _parse_layers(text):
    if not text:
        return None
    out = set()
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        l, _, d = tok.partition("/")
        out.add((int(l), int(d or 0)))
    return out


# ── Phase 1: name-table scan (per-file index build) ──────────────────────────

def phase_index(path, wanted, bbox_layer):
    print("── Phase 1: open + name-table scan (RandomAccessReader build) ──")
    t0 = time.perf_counter()
    try:
        rar = oasis_random.RandomAccessReader(
            path, wanted_layers=wanted, bbox_layer=bbox_layer)
    except Exception as exc:                       # noqa: BLE001
        print(f"  ! failed: {type(exc).__name__}: {exc}")
        return None, 0.0
    dt = time.perf_counter() - t0
    idx = rar._idx
    n_off = len(rar._by_refnum)
    has_sbbox = bool(idx.get("bbox_by_refnum"))
    n_layernames = len(idx.get("layernames") or [])
    print(f"  time                : {_fmt_secs(dt)}")
    print(f"  cells indexed       : {_fmt_n(n_off)}")
    print(f"  unit (grid/µm)      : {idx.get('unit')!r}  "
          f"(1 grid = {rar._nm_per_grid} nm)")
    sbbox_note = ("yes" if has_sbbox
                  else "NO  (ROI prune must decode cells — slow first load)")
    print(f"  S_BOUNDING_BOX      : {sbbox_note}")
    ln_note = "" if n_layernames else "  (none — Scan layers samples geometry)"
    print(f"  LAYERNAME entries   : {n_layernames}{ln_note}")
    print(f"  offsets located via : {idx.get('offsets_via')!r}")
    print()
    return rar, dt


# ── Phase 2: whole-file decode throughput (the pure decode cost) ─────────────

class _Hist:
    __slots__ = ("counts", "total", "limit", "t_start", "last_report")

    def __init__(self, limit):
        self.counts = {}
        self.total = 0
        self.limit = limit
        self.t_start = time.perf_counter()
        self.last_report = self.t_start

    def __call__(self, rid, count):
        self.counts[rid] = self.counts.get(rid, 0) + 1
        self.total += 1
        now = time.perf_counter()
        if now - self.last_report > 2.0:
            rate = self.total / (now - self.t_start)
            print(f"    … {_fmt_n(self.total)} records "
                  f"({_fmt_n(rate)}/s)", flush=True)
            self.last_report = now
        if self.limit and self.total >= self.limit:
            return False
        return True


def _decode_pass(path, wanted, limit):
    """One full consume() pass (production decode path, no storage). Returns
    (elapsed_s, histogram, total_records)."""
    reader = oas.OasisReader(path, wanted_layers=wanted, use_mmap=True,
                             defer_repetition=True)
    hist = _Hist(limit)
    t0 = time.perf_counter()
    try:
        reader.consume({}, on_each=hist)
    except Exception as exc:                       # noqa: BLE001
        print(f"  ! decode stopped: {type(exc).__name__}: {exc}")
    dt = time.perf_counter() - t0
    try:
        reader.close()
    except Exception:
        pass
    return dt, hist.counts, hist.total


def phase_decode(path, wanted, limit, size_mb):
    print("── Phase 2: whole-file decode pass (consume — pure decode, no store) ──")
    capped = " (capped — extrapolate for full file)" if limit else ""
    print(f"  decoding{(' up to ' + _fmt_n(limit)) if limit else ' the whole file'}"
          f" …{capped}", flush=True)
    dt, hist, total = _decode_pass(path, wanted, limit)
    if total == 0:
        print("  ! no records decoded")
        print()
        return dt, hist, total
    rate = total / dt if dt else 0
    print(f"  time                : {_fmt_secs(dt)}")
    print(f"  records             : {_fmt_n(total)}")
    print(f"  decode rate         : {_fmt_n(rate)} records/s")
    if not limit and size_mb:
        print(f"  throughput          : {size_mb / dt:.1f} MB/s")
    # Top record types.
    names = oas.RECORD_NAMES
    top = sorted(hist.items(), key=lambda kv: -kv[1])[:6]
    print("  record histogram    :")
    for rid, c in top:
        pct = 100.0 * c / total
        print(f"      {names.get(rid, rid):<14} {_fmt_n(c):>14}  ({pct:4.1f}%)")
    print()
    return dt, hist, total


# ── Phase 2b: cProfile breakdown (where the decode time actually goes) ───────

# Bucket cProfile rows by what kind of work they are, so the recommendation is
# data-driven rather than guessed.
_BUCKETS = [
    ("varint (byte loop)", ("read_uvarint", "read_svarint", "read_byte",
                            "decode_unsigned_int", "decode_signed_int",
                            "decode_point_list", "read_repetition_raw",
                            "decode_g_delta")),
    ("record dispatch+decode", ("consume", "_read_rectangle", "_read_polygon",
                               "_read_placement", "_read_path", "_read_text",
                               "_decode_xy", "_decode_repetition_if_set",
                               "_read_cell_header", "iter_records")),
    ("zlib (CBLOCK)", ("decompress", "_read_cblock", "<zlib")),
    ("store + numpy", ("ndarray", "frombuffer", "concatenate", "asarray",
                      "_on_rectangle", "_on_polygon", "add", "to_ndarray",
                      "<built-in method numpy")),
    ("io / mmap", ("read", "maybe_pop_exhausted", "push_cblock", "tell",
                  "seek")),
]


def phase_profile(path, wanted, sample):
    print(f"── Phase 2b: cProfile breakdown (sample ≤ {_fmt_n(sample)} records) ──")
    pr = cProfile.Profile()
    reader = oas.OasisReader(path, wanted_layers=wanted, use_mmap=True,
                             defer_repetition=True)
    counter = _Hist(sample)
    pr.enable()
    try:
        reader.consume({}, on_each=counter)
    except Exception:                              # noqa: BLE001
        pass
    pr.disable()
    try:
        reader.close()
    except Exception:
        pass

    st = pstats.Stats(pr)
    # Aggregate tottime by bucket + collect the top individual functions.
    bucket_time = {name: 0.0 for name, _ in _BUCKETS}
    bucket_time["other"] = 0.0
    rows = []
    for (fn_file, _line, fn_name), stat in st.stats.items():
        tottime = stat[2]
        rows.append((tottime, fn_name, fn_file))
        placed = False
        hay = f"{fn_name} {fn_file}"
        for bname, keys in _BUCKETS:
            if any(k in hay for k in keys):
                bucket_time[bname] += tottime
                placed = True
                break
        if not placed:
            bucket_time["other"] += tottime

    total_tt = sum(bucket_time.values()) or 1.0
    print("  time by category (tottime, the self-time in each function):")
    for bname in [b[0] for b in _BUCKETS] + ["other"]:
        t = bucket_time[bname]
        bar = "█" * int(round(40 * t / total_tt))
        print(f"      {bname:<24} {100 * t / total_tt:5.1f}%  {bar}")
    print()
    print("  top 12 functions by self-time:")
    rows.sort(reverse=True)
    for tottime, fn_name, fn_file in rows[:12]:
        loc = Path(fn_file).name if fn_file not in ("~", "") else fn_file
        print(f"      {tottime * 1e3:8.1f} ms  {fn_name}  ({loc})")
    print()
    return bucket_time, total_tt


# ── Phase 3: optional ROI walk (interactive path) ────────────────────────────

def phase_roi(rar, root, layer_dt, roi):
    if rar is None or root is None or layer_dt is None or roi is None:
        return
    cx, cy, half = roi
    bbox = (cx - half, cy - half, cx + half, cy + half)
    layer, dt = layer_dt
    print("── Phase 3: ROI walk (the interactive 'click a defect' path) ──")
    print(f"  root={root}  layer={layer}/{dt}  roi=±{_fmt_n(half)} nm "
          f"around ({_fmt_n(cx)}, {_fmt_n(cy)})")
    t0 = time.perf_counter()
    try:
        res = oasis_random.walk_roi(rar, root, bbox, layer, dt)
    except Exception as exc:                       # noqa: BLE001
        print(f"  ! failed: {type(exc).__name__}: {exc}")
        print()
        return
    dt_s = time.perf_counter() - t0
    n_rect = len(res.get("rects", []))
    n_poly = len(res.get("polys", []))
    print(f"  time                : {_fmt_secs(dt_s)}")
    print(f"  rects / polys        : {_fmt_n(n_rect)} / {_fmt_n(n_poly)}")
    print(f"  cells decoded        : {_fmt_n(rar._n_loaded)} "
          f"(cache hits {_fmt_n(getattr(rar, '_n_cache_hits', 0))})")
    print()


# ── Recommendation ───────────────────────────────────────────────────────────

def recommend(bucket_time, total_tt):
    print("══ SUMMARY / recommendation ══")
    if not total_tt:
        print("  (no profile data)")
        return
    pct = {k: 100 * v / total_tt for k, v in bucket_time.items()}
    varint = pct.get("varint (byte loop)", 0)
    decode = pct.get("record dispatch+decode", 0)
    zlib = pct.get("zlib (CBLOCK)", 0)
    store = pct.get("store + numpy", 0)
    native_helpable = varint + decode
    print(f"  varint+dispatch (native-helpable) : {native_helpable:.0f}%")
    print(f"  zlib (CBLOCK)                     : {zlib:.0f}%")
    print(f"  store + numpy                     : {store:.0f}%")
    print()
    if native_helpable >= 55:
        print("  → The decode is dominated by the pure-Python varint + record")
        print("    loop. A Cython/native hot-loop (plan F26 option A) is the")
        print("    lever; rough ceiling ≈ the varint+dispatch share above.")
    elif zlib >= 30:
        print("  → A big chunk is zlib (CBLOCK decompression), which is ALREADY")
        print("    native C. Cython on the varint loop won't touch it. Look at")
        print("    fewer/cheaper CBLOCK passes or parallel decompress instead.")
    elif store >= 30:
        print("  → A big chunk is store/numpy. The cheap pure-Python wins")
        print("    (array.array buffer, vectorized point build — F26 option B)")
        print("    apply here with no build step; Cython adds less than expected.")
    else:
        print("  → Cost is spread out. Start with the no-build-step wins")
        print("    (F26 option B) and re-measure before committing to Cython.")
    print()
    print("  (Run this on several files — D2DB vs sparse, CBLOCK vs not — the")
    print("   mix often differs and changes the recommendation.)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="path to a .oas / .oasis file")
    ap.add_argument("--layers", default="",
                    help="comma list of L/D to filter (e.g. 17/0,20/0). "
                         "Default: no filter (decode everything).")
    ap.add_argument("--bbox-layer", default="",
                    help="CE boundary layer L/D for the index (e.g. 108/250).")
    ap.add_argument("--decode-limit", type=int, default=0,
                    help="cap the Phase-2 decode at N records (0 = whole file). "
                         "Use on huge files to sample.")
    ap.add_argument("--profile-sample", type=int, default=2_000_000,
                    help="records to cProfile in Phase 2b (default 2,000,000).")
    ap.add_argument("--no-profile", action="store_true",
                    help="skip the cProfile breakdown (Phase 2b).")
    ap.add_argument("--roi", nargs=3, type=float, metavar=("CX", "CY", "HALF"),
                    help="ROI centre + half-window in nm for Phase 3.")
    ap.add_argument("--root", default=None,
                    help="root cell refnum (int) or name for the ROI walk.")
    ap.add_argument("--layer", default="",
                    help="single L/D for the ROI walk (e.g. 17/0).")
    args = ap.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        print(f"no such file: {path}")
        return 2
    size_mb = path.stat().st_size / 1024 / 1024
    wanted = _parse_layers(args.layers)
    bbox_layer = None
    if args.bbox_layer:
        bbl = _parse_layers(args.bbox_layer)
        bbox_layer = next(iter(bbl)) if bbl else None

    print("=" * 70)
    print(f"file   : {path.name}")
    print(f"size   : {size_mb:,.1f} MB")
    print(f"filter : {sorted(wanted) if wanted else '(none — decode all layers)'}")
    print("=" * 70)
    print()

    rar, _ = phase_index(path, wanted, bbox_layer)
    phase_decode(path, wanted, args.decode_limit, size_mb)

    bucket_time, total_tt = ({}, 0.0)
    if not args.no_profile:
        bucket_time, total_tt = phase_profile(path, wanted, args.profile_sample)

    root = None
    if args.root is not None:
        try:
            root = int(args.root)
        except ValueError:
            root = args.root
    layer_dt = None
    if args.layer:
        ld = _parse_layers(args.layer)
        layer_dt = next(iter(ld)) if ld else None
    roi = tuple(args.roi) if args.roi else None
    phase_roi(rar, root, layer_dt, roi)

    if not args.no_profile:
        recommend(bucket_time, total_tt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
