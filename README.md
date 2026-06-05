# GLAS — GDS-Layout Alignment for SEM

把 **GDS / OASIS layout** 對位到 **SEM 影像**的獨立桌面工具：載入大型 OASIS、瀏覽/合成
layer、用 KLARF 座標自動換算定位、SEM 上半透明疊圖手動或自動對位，匯出 per-image
alignment offset 供下游量測工具使用。

> **桌面應用 · Python 3.9+ · PyQt6**

---

## Features

- **大檔 OASIS 解析**：自寫 streaming + random-access parser，對數百 MB production OASIS
  做秒級 ROI 隨機存取（不依賴 klayout / gdstk）。
- **Layer 掃描（含無 LAYERNAME 檔）**：「Scan layers」優先讀 LAYERNAME 表（秒級、帶層名）；
  若檔案沒有 LAYERNAME 表（常見於非 Calibre 寫出、KLayout 轉檔補過索引的檔），改用
  **有上限的 cell 抽樣**從幾何列舉出現過的數字 `layer/datatype`（只顯示數字、免手 key），
  結果有 sidecar 快取。抽樣不保證列出 100% layer（罕見層仍可手動輸入，或用 `GLAS_SCAN_*`
  環境變數放寬抽樣預算，如 `GLAS_SCAN_MAX_CELLS` / `GLAS_SCAN_STOP_AFTER_NO_NEW`）；若連
  S_CELL_OFFSET 索引都沒有，會提示先用 KLayout 另存（strict mode）補索引。掃描細節會印在終端機
  `[gds-scan]` 區塊（含 offset_flag / 找到的 cell-offset 數 / layer 清單）。
- **KLARF ↔ GDS 座標換算**：由 SEM defect 的 die-corner 座標定位到 layout，自動跳位 + FOV 框。
- **即時 Boolean 表達式引擎**：HMI 風格表達式（`L0 = [(A > W:10) & B] < H:10`）即時合成 layer，
  輸出 shapely polygon + uint8 mask。
- **SEM↔GDS overlay 對位**：手動拖動（Set Offset δ）+ `cv2.matchTemplate` 自動 fine-align。
- **匯出**：per-image alignment offset（CSV / JSON，schema `mmh-gds-alignment-v1`，`image_id` join key）。
  影像匯出（給下游 MMH 區域量測）可勾 **模擬 GLV 灰階圖**（`<id>_gray.png`，各 POI 層以
  其 FG 灰階畫在背景灰階、含 blur 的 SEM-like 工作底圖）與 **ROI label map**（`<id>_label.png`，
  uint8：0=背景、1..N=第 N 個 POI 層、無 blur 邊界精確）；兩張由同一組 per-layer 幾何
  rasterize、像素網格一致，MMH 端 `gray[label == id]` 單次 boolean index 即取得該層 ROI。
  manifest（`overlay_manifest.json`，schema `mmh-gds-overlay-v2`）含 `gray_png`/`label_png`
  欄與 `label_map`（id → 層名 + fg_glv）。兩者皆以 fine-align score 門檻把關，只輸出對齊達標
  的影像（下游免 fallback）。
- **批次加速**：Run all 與 image 匯出皆多進程平行（spawn process pool）。Fine Align 面板
  「Parallel workers」可調並行度（0 = auto，每核一個、cap 16）；大量影像時明顯縮短時間。
- **OASIS 匯出**（開發者模式）：把選定的 raw layer + Boolean 合成 layer 反向寫出成 `.oas`（自寫
  writer、不依賴 klayout / gdstk，KLayout 可開）。匯出範圍可選 **目前 FOV**（可再以 GDS 座標框裁剪
  特定 ROI）或 **整顆 chip**（分 tile 串流走訪 + 全 chip 重算 Boolean，記憶體受單一 tile 控制）。入口在
  Help → About 點 icon 5 次啟用開發者模式後出現。
- **OASIS 診斷**（開發者模式）：匯出可勾 Debug 回讀驗證並產報告；File → Diagnose OASIS file… 可掃描任一
  `.oas` 產出 record 統計 / 錯誤上下文；載入失敗時也會給可複製報告 + `.debug.txt` sidecar，方便回報。

---

## Quick Start

```bash
git clone <repo-url>
cd GLAS
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

主要相依：PyQt6 ≥ 6.5、numpy ≥ 1.24、opencv-python ≥ 4.8、shapely ≥ 2.0。

---

## 使用流程

```
Open OASIS (ROI) → Load SEM (KLARF) → 選 PART / CHIP（catalog 帶座標）→ 點選 image 自動跳位/載 ROI
→ 拖動對齊 + Set Offset（或 Fine Align 自動 matchTemplate）→ Export Alignment
```

---

## 架構

- `glas/core/` — 無 Qt 引擎（OASIS parser、座標換算、FOV query、Boolean 引擎、layer cache）。
  純運算，設計上可被其他專案複用。
- `glas/app/` — PyQt6 app 殼（主視窗、SEM loader、樣式 / 元件 / 圖示）。

詳見 `CLAUDE.md` §4–§5。

---

## 由來

GLAS 原為 [MMH](../MMH) 專案 `tools/gds_align_tool.py`（plan F2），因核心能力（OASIS 解析、
GDS↔SEM 對位）通用、可跨專案複用，於 2026-05-24 抽離成獨立 repo。完整開發歷史（M1–M7
所有 milestone 與 Q&A 決策）見 `docs/plans/F2-gds-align-tool.md`。

MMH 未來透過 GLAS 匯出的 alignment CSV（`image_id` join）做 Recipe ROI 定位（MMH 側 [F4]）。

---

## 測試

```bash
pytest tests/ -v          # ~218 項（OASIS parser / 座標 / Boolean / 對位 / KLARF 載入）
```
