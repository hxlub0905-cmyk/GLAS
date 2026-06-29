# GLAS — GDS-Layout Alignment for SEM

> 把 **GDS / OASIS layout** 對位到 **SEM 影像**的獨立桌面工具。
> 載入大型 OASIS、合成 layer、KLARF 座標自動換算定位、半透明疊圖手動/自動對位，
> 匯出 per-image alignment offset 供下游量測工具使用。

<p>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white">
  <img alt="PyQt6" src="https://img.shields.io/badge/PyQt6-6.5%2B-41CD52?logo=qt&logoColor=white">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey">
</p>

---

## 功能一覽

### 解析與載入

| 功能 | 說明 |
|------|------|
| **大檔 OASIS 解析** | 自寫 streaming + random-access parser，數百 MB production OASIS 秒級 ROI 隨機存取，不依賴 klayout / gdstk |
| **Layer 掃描** | 優先讀 LAYERNAME 表（秒級）；無 LAYERNAME 時自動 bounded cell 抽樣，從幾何列舉 `layer/datatype`，結果有 sidecar 快取 |
| **KLARF ↔ GDS 座標換算** | 由 SEM defect 的 die-corner 座標定位到 layout，自動跳位 + FOV 框 |
| **PART / CHIP catalog** | fab 工程師以下拉選定 chip，自動帶入 chip corner / FOV / nm-per-px，取代 6 欄手填 |

### 對位與合成

| 功能 | 說明 |
|------|------|
| **Boolean 表達式引擎** | HMI 風格語法 `L0 = [(A > W:10) & B] < H:10`，即時合成 layer，輸出 shapely polygon + uint8 mask |
| **SEM↔GDS overlay 對位** | 手動拖動（AlignmentDelta Set Offset）+ `cv2.matchTemplate` 自動 fine-align |
| **批次加速** | 多進程平行（spawn pool），常駐 worker 跨次 Export all 重用（F23），索引一次注入省 K× 重掃；idle 300s 自動釋放 |

### 匯出

| 格式 | 說明 |
|------|------|
| **Alignment CSV / JSON** | per-image offset，schema `mmh-gds-alignment-v1`，`image_id` join key |
| **GLV 灰階圖** `<id>_gray.png` | 各 POI 層以 FG 灰階繪於背景 + blur，SEM-like 工作底圖 |
| **ROI label map** `<id>_label.png` | uint8 mask（0=背景 / 1..N=第 N POI 層），無 blur 邊界精確；MMH 端 `gray[label==id]` 單次取 ROI |
| **label 上色預覽** `<id>_label_view.png` | label map 的人眼可視版（各 id 上 POI 色）；`_label.png` 因像素值=label id 在檢視器看似全黑，此檔供目視 QC，整數 label map 機器契約不變 |
| **OASIS 匯出** *(dev mode)* | 選定 raw / Boolean layer 反向寫出 `.oas`（KLayout 可開）；可選 FOV ROI 或整顆 chip tile 串流 |

### 開發者模式 *(Help → About，點 icon ×5 啟用)*

- **OASIS 診斷**：掃描任一 `.oas` 產出 record 統計 / 錯誤上下文；載入失敗自動產 `.debug.txt`
- **終端機着色 log**：診斷訊息依類別上色（`[roi]` cyan · `[fa-timing]` magenta · `[jump]` yellow）；Non-TTY / `NO_COLOR` 自動降純文字
- **Fine-align 計時儀表**：dev mode 自動啟用，印出 read / poi / template / match 各段耗時

---

## Quick Start

```bash
git clone <repo-url>
cd GLAS

# 建立虛擬環境
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\activate

# 安裝相依
pip install -r requirements.txt

# 啟動
python main.py
```

**主要相依：** PyQt6 ≥ 6.5 · numpy ≥ 1.24 · opencv-python ≥ 4.8 · shapely ≥ 2.0

---

## 使用流程

```
1. Open OASIS   ── 三頁 Wizard：選檔 → 選 layers → 選 root cell
        ↓
2. Load SEM     ── 載入 KLARF 或影像資料夾
        ↓
3. 選 PART / CHIP  ── catalog 自動帶入 chip corner / FOV / nm-per-px
        ↓
4. 點選 image   ── 自動跳位 + 載入 GDS ROI（半透明 overlay）
        ↓
5. 對位         ── 手動拖動 Set Offset，或 Fine Align 自動 matchTemplate（單張確認幾張）
        ↓
6. Export all   ── 一鍵補跑未跑的 fine-align（已跑複用）+ Alignment CSV / JSON，
                   可加勾 gray.png + label.png（+ label_view.png 上色預覽）匯出
```

---

## 架構

```
GLAS/
├── glas/core/      無 Qt 純運算引擎（OASIS parser、座標換算、Boolean 引擎、cache）
│                   可獨立複用於其他專案
└── glas/app/       PyQt6 app 殼（主視窗、SEM loader、樣式、元件、圖示）
```

詳細目錄與模組說明見 [`CLAUDE.md`](CLAUDE.md) §4–§5。

---

## 測試

```bash
pytest tests/ -v
# ~707 項：OASIS parser / 座標換算 / Boolean / 對位 / KLARF / catalog / batch accel / devlog
```

無顯示環境執行 GUI 測試：

```bash
QT_QPA_PLATFORM=offscreen pytest tests/ -v
```

---

## 由來

GLAS 原為 [MMH](../MMH) 專案 `tools/gds_align_tool.py`（plan F2），因 OASIS 解析與 GDS↔SEM
對位核心通用、可跨專案複用，於 2026-05-24 抽離成獨立 repo。

完整開發歷史（M1–M7 所有 milestone 與 Q&A 決策）見
[`docs/plans/F2-gds-align-tool.md`](docs/plans/F2-gds-align-tool.md)。

MMH 未來透過 GLAS 匯出的 alignment CSV（`image_id` join）做 Recipe ROI 定位（MMH 側 F4）。
