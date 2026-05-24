# GLAS — GDS-Layout Alignment for SEM

把 **GDS / OASIS layout** 對位到 **SEM 影像**的獨立桌面工具：載入大型 OASIS、瀏覽/合成
layer、用 KLARF 座標自動換算定位、SEM 上半透明疊圖手動或自動對位，匯出 per-image
alignment offset 供下游量測工具使用。

> **桌面應用 · Python 3.9+ · PyQt6**

---

## Features

- **大檔 OASIS 解析**：自寫 streaming + random-access parser，對數百 MB production OASIS
  做秒級 ROI 隨機存取（不依賴 klayout / gdstk）。
- **KLARF ↔ GDS 座標換算**：由 SEM defect 的 die-corner 座標定位到 layout，自動跳位 + FOV 框。
- **即時 Boolean 表達式引擎**：HMI 風格表達式（`L0 = [(A > W:10) & B] < H:10`）即時合成 layer，
  輸出 shapely polygon + uint8 mask。
- **SEM↔GDS overlay 對位**：手動拖動（Set Offset δ）+ `cv2.matchTemplate` 自動 fine-align。
- **匯出**：per-image alignment offset（CSV / JSON，schema `mmh-gds-alignment-v1`，`image_id` join key）。

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
Open OASIS (ROI) → Load SEM (KLARF) → Coordinate Setup（一次性）→ 點選 image 自動跳位/載 ROI
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
