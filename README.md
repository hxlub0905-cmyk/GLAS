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
| **KLARF ↔ GDS 座標換算** | 由 SEM defect 的 die-corner 座標定位到 layout，自動跳位 + FOV 框。KLARF 1.2 的 µm 座標在載入時換算成 nm |
| **EBI patch（多頁 TIFF）** | 一顆 DEFECTID 對應多張 patch、整批裝在一個多頁 TIFF（lot 層 `TiffFileName` + `IMAGECOUNT`/`IMAGELIST`）時，每顆 defect 取得自己的頁；對位用哪一張可設定（預設第 2 張 = ref） |
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
| **Overlay manifest** | 每張影像一列，schema `mmh-gds-overlay-v4`。除檔名與 `fine_dx/dy_nm`／`score` 外帶 `id_source`（`image_id` 是 KLARF `DEFECTID` 還是檔名 stem —— 下游 join 前可自驗）、`page`、`width_px`／`height_px`／`nm_per_px`（label/gray 實際的像素網格），以及六種可區分的 `status`：`ok` / `low-score` / `no-coords` / `flat` / `missing-file` / `not-run` |
| **GLV 灰階圖** `<id>_gray.png` | 各 POI 層以 FG 灰階繪於背景 + blur，SEM-like 工作底圖 |
| **ROI label map** `<id>_label.png` | uint8 mask（0=背景 / 1..N=第 N POI 層），無 blur 邊界精確；MMH 端 `gray[label==id]` 單次取 ROI |
| **label 上色預覽** `<id>_label_view.png` | label map 的人眼可視版（各 id 上 POI 色）；`_label.png` 因像素值=label id 在檢視器看似全黑，此檔供目視 QC，整數 label map 機器契約不變 |
| **OASIS 匯出** *(dev mode)* | 選定 raw / Boolean layer 反向寫出 `.oas`（KLayout 可開）；可選 FOV ROI 或整顆 chip tile 串流 |

### 開發者模式 *(Help → About，點 icon ×5 啟用)*

- **OASIS 診斷**：掃描任一 `.oas` 產出 record 統計 / 錯誤上下文；載入失敗自動產 `.debug.txt`
- **終端機着色 log**：診斷訊息依類別上色（`[roi]` cyan · `[fa-timing]` magenta · `[jump]` yellow）；Non-TTY / `NO_COLOR` 自動降純文字
- **Fine-align 計時儀表**：dev mode 自動啟用，印出 read / poi / template / match 各段耗時

---

## 即時效能監控 HUD

程式啟動時開一個**獨立的深色監控台視窗**（可拖到副螢幕邊看邊操作），把每個關鍵操作的耗時
即時分類上色顯示——取代盯 `debug.bat` 終端。

- **開關**：主視窗 **View → Performance monitor**（或 **Ctrl+Shift+P**）；**預設開啟**，關掉會記住。
- **頂部 KPI 總覽**：export 進行時即時顯示 worker ramp `R→W` / 吞吐 img/s / 可用 RAM / 進度。
- **聚合表**：每個操作（或 export 的每個 worker `pid`）的 最近 / 次數 / 平均 / 最大 耗時。
- **分類彩色 log**：ROI / 解碼 / Boolean / 對位 / export worker … 各類別各自上色；異常
  （thrash：單張 ≥ 30s、decode error）自動**標紅**，可按類別篩選、暫停、存 `.txt` 回貼分析。

事件匯流排 `glas/core/perfmon.py`（Qt-free）與 HUD `glas/app/perf_panel.py`；插樁點皆走主行程
callback，`core` 引擎不依賴此系統。

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
# ~850 項：OASIS parser / 座標換算 / Boolean / 對位 / KLARF / catalog / batch accel / devlog / perfmon
```

無顯示環境執行 GUI 測試：

```bash
QT_QPA_PLATFORM=offscreen pytest tests/ -v
```

---

## 下游介面契約（ADEPT）

ADEPT 不做 OASIS/GDS parser —— layout 的解析與對位留在 GLAS，它只吃
`<id>_label.png`、`<id>_gray.png` 與 overlay manifest，join key 是 KLARF `DEFECTID`。
契約本身的唯一出處在 ADEPT repo，GLAS 不另抄一份；GLAS 側的實作紀錄見
[`docs/plans/F31-adept-interface-multipage.md`](docs/plans/F31-adept-interface-multipage.md)。

有兩件事在 GLAS 這側必須維持穩定：

- **`<id>_label.png` 是整數 label map**（0 = 背景、1..N = 第 N 個 POI 層）、**不 blur**，
  且與 `<id>_gray.png` 共用同一組幾何與同一個像素網格。不要改成二值、不要用 GLV 值編碼
  區域 —— 下游用 `gray[label == id]` 一次 boolean index 取 ROI。
- **manifest `label_map` 的層名要穩定。** 下游把它當「具名區域」的名字寫進 score 表達式，
  名字換了 recipe 就指不到。匯出時 GLAS 會對「不能當變數用的字元」（空白、減號…）與
  「兩個 id 同名」發出警告，但**不會自動改名** —— 改名等於改契約，該由 recipe 的擁有者決定。
  層名的命名規則若真的要變，manifest 的 schema 版本要一起 bump。

---

## 離線搬運（不能 clone 的機器）

`bundle/GLAS_bundle.py` 是整個 repo 壓成的**單一純文字自解 `.py`**：複製 raw 的內容、
存成檔案、執行它就展開整個 repo。每個檔案都帶 git blob SHA-1，解開時逐檔驗過才落地，
傳輸途中被改動會當場講出來，而不是給你一份安靜壞掉的程式碼。

```bash
python GLAS_bundle.py                 # 解到 .\GLASpython GLAS_bundle.py --dest D:	ools
python GLAS_bundle.py --list          # 只看裡面有什麼，不寫任何檔案
```

改完程式碼後**重產**（順序不能顛倒，包裡面含著那份清單）：

```bash
git add -A && python tools/release.py && git add -A
```

忘了跑不會有當下症狀，所以
`tests/test_bundle_tools.py::test_the_transfer_files_are_up_to_date` 會變紅。

---

## 由來

GLAS 原為 [MMH](../MMH) 專案 `tools/gds_align_tool.py`（plan F2），因 OASIS 解析與 GDS↔SEM
對位核心通用、可跨專案複用，於 2026-05-24 抽離成獨立 repo。

完整開發歷史（M1–M7 所有 milestone 與 Q&A 決策）見
[`docs/plans/F2-gds-align-tool.md`](docs/plans/F2-gds-align-tool.md)。

MMH 未來透過 GLAS 匯出的 alignment CSV（`image_id` join）做 Recipe ROI 定位（MMH 側 F4）。
