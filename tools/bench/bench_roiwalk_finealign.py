#!/usr/bin/env python3
"""GLAS 效能量測 harness — ROI walk + 批次 fine-align（聚焦這兩條主路徑）。

repo 內沒有大型 OASIS 樣本檔，真正的瓶頸只在 production 大檔上才會顯現，所以這支
腳本設計成 **在你的實機 + 真實 .oas 上跑**、把量測結果印成一段可直接複製回傳的報告。

最少只要給一個 OASIS 檔路徑即可（root cell / layer / ROI 都會自動推導）：

    python tools/bench/bench_roiwalk_finealign.py /path/to/prod.oas

常用選項：

    --layer 17/0           量測指定 layer（可重複；省略時用 enumerate_layers 自動挑）
    --root TOPCELL         指定 root cell 名（省略時用 top/merge 名稱啟發式或最後一個）
    --fov-um 5             FOV / ROI 邊長（微米，預設 5）→ 同時決定 fine-align 模板大小
    --repeats 3            ROI walk 暖身重跑次數（看 memo 命中後的穩態）
    --images 40            fine-align 模擬影像數（量 per-stage 與平行吞吐）
    --workers 0            批次 process pool worker 數（0=auto=每核一個、cap 16）
    --profile              對 cold ROI walk 開 cProfile，印 top 25 熱點
    --no-batch             跳過 process-pool 平行區段（只量單執行緒 per-stage）
    --json OUT.json        另存機器可讀結果

輸出五個區段：
  [1] reader build      — name-table scan 成本 + S_BOUNDING_BOX 覆蓋率（剪枝關鍵指標）
  [2] resolve target    — root / layer / ROI 自動推導結果
  [3] ROI walk          — cold + warm，RoiWalkStats 全欄位 + 可選 cProfile
  [4] fine-align stages — walk / rasterize+blur / matchTemplate 單張分段（真實幾何）
  [5] batch parallel    — process pool K worker 端到端吞吐 + 對單執行緒加速比

read-only：除了 --json 與 batch 區段的暫存 PNG（跑完即刪），不寫任何專案檔。
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import platform
import pstats
import sys
import tempfile
import time
from pathlib import Path

# ── 把 glas/core 放上 sys.path（與 main.py / conftest.py 同慣例）──────────────
_REPO = Path(__file__).resolve().parents[2]
_CORE = _REPO / "glas" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import numpy as np                       # noqa: E402

try:
    import cv2                           # noqa: E402
except Exception:                        # pragma: no cover
    cv2 = None

import oasis_random                      # noqa: E402
import fine_align                        # noqa: E402

try:
    import shapely                       # noqa: E402
    _SHAPELY_V = shapely.__version__
except Exception:                        # pragma: no cover
    _SHAPELY_V = "missing"


# ── 小工具 ───────────────────────────────────────────────────────────────────

def _hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72, flush=True)


def _fmt_bbox(b) -> str:
    if b is None:
        return "None"
    return (f"({b[0]:.0f}, {b[1]:.0f}, {b[2]:.0f}, {b[3]:.0f}) nm  "
            f"[{(b[2]-b[0])/1000.0:.1f} × {(b[3]-b[1])/1000.0:.1f} µm]")


def _parse_layer(s: str):
    s = s.replace(":", "/")
    if "/" in s:
        a, b = s.split("/", 1)
        return (int(a), int(b))
    return (int(s), 0)


# ── 區段 1：reader build ─────────────────────────────────────────────────────

def section_reader(path: str, wanted, report: dict):
    _hr("[1] reader build (name-table scan + S_BOUNDING_BOX coverage)")
    t0 = time.perf_counter()
    rar = oasis_random.RandomAccessReader(
        path, wanted_layers=set(wanted),
        bbox_layer=oasis_random.DEFAULT_BBOX_LAYER)
    build_s = time.perf_counter() - t0
    n_cells = len(rar._by_refnum) or len(rar._by_name)
    n_sbbox = len(rar._sbbox_by_refnum) + len(rar._sbbox_by_name)
    prune = "decode-free prune" if n_sbbox else "NONE -> bbox-by-decode (SLOW first walk)"
    print(f"  build time          : {build_s:.2f} s")
    print(f"  has_offsets         : {rar.has_offsets()}")
    print(f"  cells indexed       : {n_cells:,}")
    print(f"  offsets_via         : {rar._offsets_via!r}")
    print(f"  unit (grid/µm)      : {rar._unit!r}  -> 1 grid = {rar._nm_per_grid} nm")
    print(f"  S_BOUNDING_BOX cells: {n_sbbox:,}  ({prune})")
    print(f"  LAYERNAME rows      : {len(rar._layernames):,}")
    report["reader"] = {
        "build_s": round(build_s, 3), "n_cells": n_cells,
        "n_sbbox": n_sbbox, "sbbox_prune": bool(n_sbbox),
        "offsets_via": str(rar._offsets_via), "unit": rar._unit,
        "has_offsets": rar.has_offsets(),
    }
    if not rar.has_offsets():
        print("\n  ⚠ 此檔無 S_CELL_OFFSET 索引 → 無法隨機存取 ROI；"
              "請先用 KLayout 另存 .oas 補索引後再量測。")
    return rar


# ── 區段 2：resolve root / layer / ROI ───────────────────────────────────────

def _pick_root(rar, explicit):
    if explicit:
        return explicit
    names = list(rar._by_name.keys())
    if not names:
        return None
    for n in names:
        low = n.lower()
        if "top" in low or "merge" in low:
            return n
    return names[-1]


def section_resolve(rar, path, args, report):
    _hr("[2] resolve target (root / layers / ROI)")
    root = _pick_root(rar, args.root)
    print(f"  root cell           : {root!r}"
          f"{'  (auto)' if not args.root else '  (explicit)'}")

    # layers
    if args.layer:
        layers = [_parse_layer(s) for s in args.layer]
        print(f"  layers              : {layers}  (explicit)")
    else:
        try:
            t0 = time.perf_counter()
            enum = oasis_random.enumerate_layers(path, use_cache=True)
            scan_s = time.perf_counter() - t0
            rows = enum.get("layers") or []
            layers = [(r["layer"], r["datatype"]) for r in rows[:4]]
            print(f"  layers              : {layers}  (auto via "
                  f"enumerate_layers source={enum.get('source')!r} "
                  f"in {scan_s:.2f}s; first {len(layers)} of {len(rows)})")
        except Exception as exc:
            layers = [(17, 0)]
            print(f"  layers              : {layers}  (fallback; enumerate failed: {exc})")
    if not layers:
        layers = [(17, 0)]
        print(f"  layers              : {layers}  (fallback; none discovered)")

    # ROI
    if args.roi:
        roi = tuple(float(x) for x in args.roi.split(","))
        center = ((roi[0] + roi[2]) / 2.0, (roi[1] + roi[3]) / 2.0)
        print(f"  ROI                 : {_fmt_bbox(roi)}  (explicit)")
    else:
        try:
            t0 = time.perf_counter()
            chip = rar.reachable_bbox_nm(root)
            chip_s = time.perf_counter() - t0
        except Exception as exc:
            chip, chip_s = None, 0.0
            print(f"  ! reachable_bbox_nm failed: {exc}")
        fov_nm = args.fov_um * 1000.0
        if chip is not None:
            center = ((chip[0] + chip[2]) / 2.0, (chip[1] + chip[3]) / 2.0)
            print(f"  chip bbox           : {_fmt_bbox(chip)}  "
                  f"(reachable_bbox in {chip_s:.2f}s)")
        else:
            center = (0.0, 0.0)
            print("  chip bbox           : unknown -> ROI centred on origin")
        roi = (center[0] - fov_nm / 2.0, center[1] - fov_nm / 2.0,
               center[0] + fov_nm / 2.0, center[1] + fov_nm / 2.0)
        print(f"  ROI ({args.fov_um}µm centred): {_fmt_bbox(roi)}  (auto)")

    report["resolve"] = {
        "root": str(root), "layers": [list(l) for l in layers],
        "roi": [float(x) for x in roi], "fov_um": args.fov_um,
        "center": [center[0], center[1]],
    }
    return root, layers, roi, center


# ── 區段 3：ROI walk（cold + warm + 可選 cProfile）────────────────────────────

def _walk_once(rar, root, roi, layers):
    agg = {"rects": 0, "polys": 0, "stats": []}
    t0 = time.perf_counter()
    for (L, D) in layers:
        res = oasis_random.walk_roi(rar, root, roi, L, D)
        agg["rects"] += len(res["rects"])
        agg["polys"] += len(res["polys"])
        agg["stats"].append(((L, D), res["stats"]))
    agg["elapsed"] = time.perf_counter() - t0
    return agg


def _dump_stats(layers_stats):
    for key, st in layers_stats:
        print(f"  layer {key}:")
        print(f"    elapsed_s          = {st.elapsed_s:.3f}   "
              f"t_decode={st.t_decode:.3f}")
        print(f"    cells: visits={st.cell_visits}  decoded={st.cells_decoded}  "
              f"cached={st.cells_cached}  sbbox_prune={st.sbbox_prune}")
        print(f"    instances: visited={st.instances_visited}  "
              f"pruned={st.instances_pruned}  "
              f"materialized={st.instances_materialized}  "
              f"max_array_k={st.max_array_k}")
        print(f"    scanned: placements={st.placements_scanned}  "
              f"rect_specs={st.rect_specs_scanned}  "
              f"poly_specs={st.poly_specs_scanned}")
        print(f"    sections: t_place={st.t_place:.3f}  t_rect={st.t_rect:.3f}  "
              f"t_poly={st.t_poly:.3f}")
        print(f"    emitted: rects={st.rects_emitted}  polys={st.polys_emitted}")


def section_walk(rar, root, layers, roi, args, report):
    _hr("[3] ROI walk — cold (first) + warm (memo hot)")
    # cold：memo 為空的首次 walk（含解碼成本）
    if args.profile:
        prof = cProfile.Profile()
        prof.enable()
        cold = _walk_once(rar, root, roi, layers)
        prof.disable()
    else:
        cold = _walk_once(rar, root, roi, layers)
    print(f"  COLD  : {cold['elapsed']*1000:.1f} ms  "
          f"(rects={cold['rects']}, polys={cold['polys']})")
    _dump_stats(cold["stats"])

    warm_ms = []
    for i in range(max(0, args.repeats)):
        w = _walk_once(rar, root, roi, layers)
        warm_ms.append(w["elapsed"] * 1000.0)
    if warm_ms:
        print(f"\n  WARM  : {min(warm_ms):.1f} ms (best of {len(warm_ms)})  "
              f"all={['%.1f' % x for x in warm_ms]}")
        speedup = cold["elapsed"] * 1000.0 / max(1e-6, min(warm_ms))
        print(f"          cold/warm ratio = {speedup:.1f}× "
              f"(高=首次解碼成本主導；memo 命中後便宜)")

    report["walk"] = {
        "cold_ms": round(cold["elapsed"] * 1000.0, 1),
        "warm_best_ms": round(min(warm_ms), 1) if warm_ms else None,
        "warm_all_ms": [round(x, 1) for x in warm_ms],
        "rects": cold["rects"], "polys": cold["polys"],
        "per_layer": [{
            "layer": list(k),
            "elapsed_s": round(st.elapsed_s, 4),
            "t_decode": round(st.t_decode, 4),
            "cell_visits": st.cell_visits, "cells_decoded": st.cells_decoded,
            "cells_cached": st.cells_cached, "sbbox_prune": st.sbbox_prune,
            "instances_visited": st.instances_visited,
            "instances_materialized": st.instances_materialized,
            "max_array_k": st.max_array_k,
            "placements_scanned": st.placements_scanned,
            "rect_specs_scanned": st.rect_specs_scanned,
            "poly_specs_scanned": st.poly_specs_scanned,
            "t_place": round(st.t_place, 4), "t_rect": round(st.t_rect, 4),
            "t_poly": round(st.t_poly, 4),
            "rects_emitted": st.rects_emitted, "polys_emitted": st.polys_emitted,
        } for k, st in cold["stats"]],
    }

    if args.profile:
        s = io.StringIO()
        pstats.Stats(prof, stream=s).sort_stats("cumulative").print_stats(25)
        print("\n  --- cProfile (cold walk, top 25 by cumulative) ---")
        print("  " + "\n  ".join(s.getvalue().splitlines()[:40]))


# ── 區段 4：fine-align 單張分段（真實幾何 + 合成 SEM）─────────────────────────

def _cfg_for(fov_nm):
    # 鏡像 _fine_align_image 的 cfg；fov_w/fov_h 是 anchor 兩側半幅（ROI=2×）。
    return {
        "nm_auto": True, "nm_manual": 0.0,
        "fov_w": fov_nm, "fov_h": fov_nm,
        "bg_glv": 80, "blur_sigma_px": 1.0,
        "search_radius_nm": fov_nm * 0.1,
    }


def section_finealign_stages(rar, root, layers, center, args, report):
    _hr("[4] fine-align per-stage (walk / rasterize+blur / matchTemplate)")
    if cv2 is None:
        print("  cv2 不可用 → 跳過 fine-align 區段")
        return None
    fov_nm = args.fov_um * 1000.0
    # 影像像素數：取常見 SEM FOV px 邊長（依 nm_per_px 反推），用 1024² 當代表。
    W = H = args.img_px
    nm_per_px = (2.0 * fov_nm) / W   # ROI 寬=2*fov_nm；與 app auto 一致
    poi_specs = [(("raw", L, D), 200) for (L, D) in layers]
    cfg = _cfg_for(fov_nm)

    # 在 center 周圍抖動 anchor，避免每張都打同一個已暖 memo（接近真實批次跨 FOV）。
    rng = np.random.default_rng(0)
    n = max(1, args.images)
    jitter = fov_nm * 2.0
    acc = {"walk": 0.0, "tmpl": 0.0, "match": 0.0, "n": 0, "empty": 0}
    for i in range(n):
        ax = center[0] + float(rng.uniform(-jitter, jitter))
        ay = center[1] + float(rng.uniform(-jitter, jitter))
        anchor = (ax, ay)
        roi = (ax - fov_nm, ay - fov_nm, ax + fov_nm, ay + fov_nm)
        t0 = time.perf_counter()
        poi_layers = []
        for spec, fg in poi_specs:
            polys = fine_align.poi_polys_for_roi(rar, root, roi, spec)
            if polys:
                poi_layers.append((polys, fg))
        t1 = time.perf_counter()
        if not poi_layers:
            acc["empty"] += 1
            continue
        template = fine_align.render_composite_template(
            poi_layers, anchor, W, H, nm_per_px, cfg["bg_glv"],
            cfg["blur_sigma_px"])
        t2 = time.perf_counter()
        # 合成 SEM：模板 + 雜訊（matchTemplate 成本只取決於影像/模板尺寸）
        sem = template.copy()
        sem = np.clip(sem.astype(np.int16) +
                      rng.integers(-12, 12, sem.shape, dtype=np.int16),
                      0, 255).astype(np.uint8)
        radius_px = cfg["search_radius_nm"] / nm_per_px
        fine_align.fine_align_one(sem, template, nm_per_px, radius_px)
        t3 = time.perf_counter()
        acc["walk"] += t1 - t0
        acc["tmpl"] += t2 - t1
        acc["match"] += t3 - t2
        acc["n"] += 1

    m = max(1, acc["n"])
    walk_ms, tmpl_ms, match_ms = (acc["walk"]/m*1e3, acc["tmpl"]/m*1e3,
                                  acc["match"]/m*1e3)
    total_ms = walk_ms + tmpl_ms + match_ms
    print(f"  image size          : {W}×{H} px   nm_per_px={nm_per_px:.2f}")
    print(f"  images measured     : {acc['n']}  (empty/flat skipped={acc['empty']})")
    print(f"  walk (ROI+polys)    : {walk_ms:7.2f} ms/img  "
          f"({walk_ms/total_ms*100:4.1f}%)")
    print(f"  rasterize+blur      : {tmpl_ms:7.2f} ms/img  "
          f"({tmpl_ms/total_ms*100:4.1f}%)")
    print(f"  matchTemplate       : {match_ms:7.2f} ms/img  "
          f"({match_ms/total_ms*100:4.1f}%)")
    print(f"  --------            : {total_ms:7.2f} ms/img  (single-thread)")
    bott = max([("walk", walk_ms), ("template", tmpl_ms), ("match", match_ms)],
               key=lambda kv: kv[1])
    print(f"  >>> bottleneck      : {bott[0]} ({bott[1]:.2f} ms/img)")
    report["finealign_stages"] = {
        "img_px": W, "nm_per_px": round(nm_per_px, 3),
        "images": acc["n"], "empty": acc["empty"],
        "walk_ms": round(walk_ms, 2), "template_ms": round(tmpl_ms, 2),
        "match_ms": round(match_ms, 2), "total_ms": round(total_ms, 2),
        "bottleneck": bott[0],
    }
    return total_ms


# ── 區段 5：批次平行（真實 process pool 端到端吞吐）───────────────────────────

def section_batch_parallel(rar, root, layers, center, args, single_ms, report):
    _hr("[5] batch parallel — process pool end-to-end throughput")
    if cv2 is None:
        print("  cv2 不可用 → 跳過")
        return
    fov_nm = args.fov_um * 1000.0
    W = H = args.img_px
    nm_per_px = (2.0 * fov_nm) / W
    poi_specs = [(("raw", L, D), 200) for (L, D) in layers]
    cfg = _cfg_for(fov_nm)
    workers = fine_align.batch_worker_count(args.workers)
    n = max(workers * 2, args.images)
    rng = np.random.default_rng(1)
    jitter = fov_nm * 2.0

    # 產生 n 張合成 SEM PNG 到暫存目錄（worker 從磁碟 imread，鏡像真實批次）。
    tmpdir = Path(tempfile.mkdtemp(prefix="glas_bench_"))
    jobs = []
    print(f"  preparing {n} synthetic SEM frames ({W}×{H}) in {tmpdir} ...",
          flush=True)
    try:
        for i in range(n):
            ax = center[0] + float(rng.uniform(-jitter, jitter))
            ay = center[1] + float(rng.uniform(-jitter, jitter))
            img = (rng.integers(40, 120, (H, W), dtype=np.int16)).astype(np.uint8)
            p = tmpdir / f"img_{i:04d}.png"
            cv2.imwrite(str(p), img)
            jobs.append((f"img_{i:04d}", (ax, ay), str(p), True))

        # 序列基準（in-thread，單一 reader，memo 會跨張累積）
        t0 = time.perf_counter()
        for job in jobs:
            fine_align._fine_align_image(
                job, rar, root, poi_specs, cfg, fine_align._never_cancel)
        seq_s = time.perf_counter() - t0
        seq_ips = n / seq_s
        print(f"  sequential (1 thr)  : {seq_s:.2f} s  ->  {seq_ips:.1f} img/s  "
              f"({seq_s/n*1e3:.1f} ms/img)")

        if args.no_batch:
            print("  --no-batch → 跳過 process pool")
            report["batch"] = {"sequential_s": round(seq_s, 3),
                               "sequential_ips": round(seq_ips, 2),
                               "workers": 0}
            return

        # process pool（鏡像 app：注入 prebuilt_index、persistent batch_pool）
        idx = rar.index_snapshot()
        t_warm0 = time.perf_counter()
        ex = fine_align.batch_pool.get(
            rar._path, rar._init_wanted, rar._dtype, rar._bbox_layer,
            idx, workers)
        # 預熱：每個 worker 各跑一次 warm-probe，確保 reader 都建好（用模組層級
        # 函式才能跨 spawn pool pickle）。submit workers×2 提高命中每個 worker 機率。
        for f in [ex.submit(fine_align._pool_warm_probe)
                  for _ in range(workers * 2)]:
            f.result()
        warm_s = time.perf_counter() - t_warm0
        t0 = time.perf_counter()
        futs = [ex.submit(fine_align._pool_task, job, root, poi_specs, cfg)
                for job in jobs]
        for f in futs:
            f.result()
        par_s = time.perf_counter() - t0
        par_ips = n / par_s
        print(f"  pool warm ({workers} wrk) : {warm_s:.2f} s  "
              f"(spawn + import + reader build)")
        print(f"  parallel ({workers} wrk)  : {par_s:.2f} s  ->  {par_ips:.1f} img/s  "
              f"({par_s/n*1e3:.1f} ms/img)")
        print(f"  speedup vs seq      : {seq_s/par_s:.2f}×  "
              f"(理想上限 ≈ {workers}×；差距=IPC/啟動/記憶體頻寬)")
        fine_align.batch_pool.shutdown()
        report["batch"] = {
            "sequential_s": round(seq_s, 3), "sequential_ips": round(seq_ips, 2),
            "workers": workers, "warm_s": round(warm_s, 3),
            "parallel_s": round(par_s, 3), "parallel_ips": round(par_ips, 2),
            "speedup": round(seq_s / par_s, 2),
        }
    finally:
        for p in tmpdir.glob("*.png"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass


# ── 摘要（可直接複製回傳）────────────────────────────────────────────────────

def print_summary(report):
    _hr("PASTE-BACK SUMMARY  (把以下整段複製回傳即可)")
    r = report
    env = r["env"]
    print(f"GLAS bench · {env['platform']} · py{env['python']} · "
          f"{env['cpu']} cores · numpy{env['numpy']} cv2{env['cv2']} "
          f"shapely{env['shapely']}")
    if "reader" in r:
        rd = r["reader"]
        print(f"reader: build={rd['build_s']}s  cells={rd['n_cells']:,}  "
              f"sbbox={rd['n_sbbox']:,} (prune={rd['sbbox_prune']})  "
              f"via={rd['offsets_via']}")
    if "resolve" in r:
        rs = r["resolve"]
        print(f"target: root={rs['root']}  layers={rs['layers']}  "
              f"fov={rs['fov_um']}µm")
    if "walk" in r:
        w = r["walk"]
        print(f"walk: COLD={w['cold_ms']}ms  WARM={w['warm_best_ms']}ms  "
              f"rects={w['rects']} polys={w['polys']}")
        for pl in w["per_layer"]:
            print(f"  L{pl['layer']}: decoded={pl['cells_decoded']} "
                  f"cached={pl['cells_cached']} sbbox={pl['sbbox_prune']} "
                  f"inst_mat={pl['instances_materialized']} "
                  f"max_k={pl['max_array_k']} "
                  f"t_dec={pl['t_decode']} t_pl={pl['t_place']} "
                  f"t_rt={pl['t_rect']} t_py={pl['t_poly']}")
    if "finealign_stages" in r:
        f = r["finealign_stages"]
        print(f"fa-stage ({f['img_px']}px): walk={f['walk_ms']} "
              f"tmpl={f['template_ms']} match={f['match_ms']} "
              f"total={f['total_ms']}ms/img  bottleneck={f['bottleneck']}")
    if "batch" in r:
        b = r["batch"]
        if b.get("workers"):
            print(f"batch: seq={b['sequential_ips']}img/s  "
                  f"par({b['workers']}wrk)={b.get('parallel_ips')}img/s  "
                  f"speedup={b.get('speedup')}×  warm={b.get('warm_s')}s")
        else:
            print(f"batch: seq={b['sequential_ips']}img/s (pool skipped)")
    print("=" * 72, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="GLAS ROI walk + fine-align benchmark")
    ap.add_argument("oasis", help="path to a real .oas file")
    ap.add_argument("--layer", action="append",
                    help="layer as L/D (repeatable); auto if omitted")
    ap.add_argument("--root", default=None, help="root cell name (auto if omitted)")
    ap.add_argument("--roi", default=None,
                    help="explicit ROI x0,y0,x1,y1 in nm (auto if omitted)")
    ap.add_argument("--fov-um", type=float, default=5.0, dest="fov_um")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--images", type=int, default=40)
    ap.add_argument("--img-px", type=int, default=1024, dest="img_px")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--no-batch", action="store_true", dest="no_batch")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    path = args.oasis
    if not Path(path).exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    report = {"env": {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu": os.cpu_count(),
        "numpy": np.__version__,
        "cv2": (cv2.__version__ if cv2 else "missing"),
        "shapely": _SHAPELY_V,
        "file": str(path),
        "file_mb": round(Path(path).stat().st_size / 1e6, 1),
    }}
    _hr(f"GLAS benchmark · {path}  ({report['env']['file_mb']} MB)")
    print(f"  {report['env']['platform']} · py{report['env']['python']} · "
          f"{report['env']['cpu']} cores · numpy{report['env']['numpy']} "
          f"· cv2 {report['env']['cv2']} · shapely {report['env']['shapely']}")

    wanted = [_parse_layer(s) for s in args.layer] if args.layer else [(17, 0)]
    rar = section_reader(path, wanted, report)
    if not rar.has_offsets():
        print_summary(report)
        return 0

    root, layers, roi, center = section_resolve(rar, path, args, report)
    # reader 已用首批 wanted 建好；若 auto layer 與其不同，重建 reader 使過濾相符。
    if set(layers) != set(wanted):
        rar.close()
        rar = oasis_random.RandomAccessReader(
            path, wanted_layers=set(layers),
            bbox_layer=oasis_random.DEFAULT_BBOX_LAYER)

    try:
        section_walk(rar, root, layers, roi, args, report)
    except Exception as exc:
        print(f"  ! ROI walk section failed: {exc!r}")
    try:
        single_ms = section_finealign_stages(rar, root, layers, center, args, report)
    except Exception as exc:
        print(f"  ! fine-align stage section failed: {exc!r}")
        single_ms = None
    try:
        section_batch_parallel(rar, root, layers, center, args, single_ms, report)
    except Exception as exc:
        print(f"  ! batch parallel section failed: {exc!r}")

    print_summary(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"\nwrote {args.json}")
    rar.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
