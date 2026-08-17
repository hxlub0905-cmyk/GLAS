# [F31] ADEPT 介面契約：多頁 TIFF page 對應 + manifest 契約補齊

> **狀態：** planned
> **§8 ID：** [F31]
> **建立：** 2026-08-17
> **負責 branch：** `claude/adept-glas-interface-s5e8fb`

---

## Goal & Context

**來源：** ADEPT（下游）提出的《ADEPT ← GLAS 介面契約》。ADEPT 已定調**不做 OASIS/GDS
parser** —— layout 的解析與對位全部留在 GLAS，ADEPT 只吃 GLAS 匯出的
`<id>_label.png` / `<id>_gray.png` / manifest，join key 是 KLARF `DEFECTID`。
兩邊的 `image_id` 同源（GLAS `sem_loader.load_klarf` 與 ADEPT
`ingest/dataset.py` 都取 KLARF 的 `DEFECTID` 欄），所以 join 不必發明新 id。

**問題（為什麼現在要改）：** 契約有兩個 GLAS 目前**做不到**的地方，其中第一個會讓
多頁資料**整批安靜地對錯**：

1. **多頁 TIFF 沒有 page 概念。** EBI patch 的實際形式是「一個多頁 TIFF 裝一整批
   defect」（一顆佔連續幾頁）。GLAS 的 `SemImage` 沒有 page 欄位，`load_klarf` 每顆
   defect 只取 image block 裡的第一個 quoted 檔名，而讀圖是
   `cv2.imread(path, IMREAD_GRAYSCALE)` —— **恆讀第 0 頁**。於是所有 defect 指到同
   一個檔案、拿同一張圖對位 N 次，產出的 label 也全是同一顆的。三個讀圖點：
   `fine_align.py:785`、`overlay_export.py:114`、`overlay_export.py:340`。
2. **manifest 契約不足以讓下游驗證。** 現在的列沒有 `id_source`（分不出 image_id 是
   `DEFECTID` 還是檔名 stem → 猜錯整批對不上且**安靜**）、沒有 `width_px`/`height_px`
   （分不出 label 圖與 patch 是不是同一個網格）、`status` 粒度不足（score gate 沒過時
   檔名空白但 status 仍是 `ok`，下游分不出「沒跑過 / 跑了但分數低 / 沒有座標」）。

**成功長什麼樣：**

- 一份多頁 TIFF 的 KLARF 載入後，每顆 defect 的 `(file, page)` 互不相同；兩顆不同
  defect 產出的 `_label.png` 不相同。
- 「對位用第幾頁」是設定值不是寫死常數；改它 `fine_dx/dy_nm` 會變、設回去逐項相同。
- manifest 每一列都能讓 ADEPT 自我驗證：id 的來源、影像網格、以及五種可區分的
  `status`。

**與現有系統的關係：延伸，不取代。** 單頁影像（現有 KLARF/資料夾流程）走
`page is None` 的舊路徑，讀圖行為 **byte-identical**；page 是新增的可選維度。

### 為什麼「必要 2（對位用第幾頁）」確實屬於 GLAS

user 在 Q&A 中提出質疑：「GLAS 沒有吃 patch 的能力，目的是用 GDS 產生 layout PNG」。
實際上 GLAS **會吃 patch** —— `fine_align` 的自動對位就是拿 SEM 影像跟 GDS 合成
template 跑 `cv2.matchTemplate`，`export_raw` 也是直接複製那張 SEM。因此：

- 影像的 `H/W` 決定 `nm_per_px`（`nm_per_px = fov_w / W`）→ 讀錯頁、尺寸不同就整批比例錯；
- 對位分數完全取決於餵進去的是哪一頁（BSE 結構訊號 vs SE）；
- label/gray 的畫布尺寸跟著那張影像走。

必要 1 讓 GLAS「讀得到第 N 頁」，必要 2 只是「決定 N 是多少」——同一條程式路徑。

---

## Q&A Decisions

### Q1: 「一顆 defect 對到 TIFF 哪幾頁」怎麼算？
**選項：** 解析 KLARF image block / 依 defect 順序平均分頁 / 兩者交叉驗證 / 先讀 ADEPT repo
**選擇：** 先讀 ADEPT repo（已 `add_repo` + clone `/workspace/adept`，HEAD `13153f4`）
**理由 + 讀到的結論：** ADEPT `core/ingest/klarf_core.py::defect_image_map(n_pages)`
**已經選了「兩者交叉驗證」**，GLAS 照抄語意即可，不必重新發明：

- 優先 `imagelist` 模式：IMAGELIST 每條目的第一欄若**全是整數且不重複**，視為 TIFF
  page 編號（KLA 慣例通常 1-based）；用 `n_pages` 驗證 `[lo..hi]` 落點自動判 0/1-based。
- 1.8 結構化格式（`Images N { … }`，id 是 **defect 內的圖序號** 1..IMAGECOUNT 而非全域
  page）→ 明確排除，退回 sequential。
- 有重複 id、或範圍塞不進 `n_pages` → 退回 sequential 並在 `notes` 說明。
- `sequential` 模式：依 defect 出現順序用各自 IMAGECOUNT 切連續頁區段；總和與
  `n_pages` 不符時留 note。

回傳 `{"mode", "base", "pages": [[0-based page, ...], ...], "notes": [...]}`。

### Q2: 「對位用第幾頁」的設定放哪？
**選項：** FineAlign 面板 UI 欄位 / 只放環境變數 / UI 欄位 + 每張影像可覆寫
**選擇：** UI 欄位（面板設定），語意採「**defect 頁清單內的第幾張**」（1-based，預設 2）
**理由：** user 的資料是一顆 defect 1 BSE + 4 SE、**BSE 固定第 2 頁**，SE 順序無所謂。
用「defect 內序位」而非「檔案絕對頁號」可讓 batch TIFF（一顆 5 頁）與單檔多頁兩種形式
共用同一個設定；兩頁的舊資料（第 1 頁 test、第 2 頁 ref）也只是換個序位。user 對此項
原本存疑，經說明「fine-align 確實讀 SEM 影像、H/W 決定 nm_per_px」後仍列入本次範疇。

### Q3: 建議 4 的 status 粒度？
**選項：** 補齊五種 + bump v4 / 只補 status 不加新欄位 / 維持現狀
**選擇：** 補齊五種 status + schema bump `mmh-gds-overlay-v3` → `v4`（同時容納建議 3 的新欄位）
**理由：** 下游要把它變成 `locate_ok` / `align_score` 兩個特徵當 score 表達式的 gate；
「檔案不存在」現在混了三件處置不同的事。

### Q4: 本次範疇？
**選擇：** 必要 1 + 必要 2 + 建議 3 + 建議 4/5（全部）

---

## Milestones

### M1: TIFF page 讀取底座（Qt-free core）  [status: planned]

- [ ] 移植 ADEPT `core/ingest/tiff_index.py` → `glas/core/tiff_index.py`，保留來源標註
      （KLIP → ADEPT → GLAS）與**兩段防護的原註解**：
      - handle 快取版本鍵含 **pid** —— fork 出來的 worker 共用檔案偏移量會靜默讀到
        別頁的 bytes（GLAS 的 export pool 正好是這個情境）；
      - `read_page` 全程持 `RLock` —— QThread 併發共用 handle 會讓 tifffile 把像素當
        IFD 解析（`suspicious number of tags`）。
- [ ] 新增 `read_sem_gray(path, page=None)`（放 `glas/core/` 的讀圖單一入口）：
      - `page is None` → 原封不動 `cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)`（**byte-identical**）；
      - `page` 有值 → `tiff_index.read_page` 後轉 uint8 灰階（多通道走 cv2 灰階轉換、
        16-bit 依現有慣例縮放）；
      - `tifffile` 不在 → `cv2.imreadmulti` fallback（並在 log 說明代價）。
- [ ] `requirements.txt` 加 `tifffile`（標為多頁 TIFF 才需要的 optional 相依）。
- [ ] 驗證：`tests/test_tiff_index.py` —— 合成多頁 TIFF（classic + BigTIFF）頁數正確、
      單頁 `read_sem_gray(page=None)` 與 `cv2.imread` **逐 byte 相同**、越界 page 丟
      `IndexError`、無 tifffile 時 fallback 路徑可跑。

### M2: KLARF → defect 頁對應  [status: planned]

- [ ] `glas/core/klarf_parser.py`：`_map_row_tokens` 除了現有 `_image_block_raw` /
      `_image_filename`，另存**結構化** `_image_entries`（`[[tok, ...], ...]`，每張圖一條）
      與 `_image_count`。**純增補**，既有欄位與無損寫回不動。
- [ ] 新增 `glas/core/klarf_pages.py`（Qt-free）：`defect_image_map(defects, n_pages=None)`
      移植 ADEPT 雙模式 + `notes`，語意逐項對齊（含 `images18` 排除、重複 id 偵測、
      0/1-based 自動判定）。
- [ ] `sem_loader.SemImage` 加 `page: Optional[int]`（0-based，對位/匯出實際使用的頁）、
      `pages: list[int]`（該 defect 的完整頁清單）、`id_source: str`
      （`"klarf-defectid"` | `"filename-stem"`）。
- [ ] `load_klarf`：解析出 TIFF 路徑後用 `tiff_index.n_pages` 交叉驗證頁對應；單頁 /
      非 TIFF / 對不上時 `page=None`（回舊行為）並保留 note 供 UI 顯示。
- [ ] `load_folder`：`id_source="filename-stem"`。
- [ ] 驗證：`tests/test_klarf_pages.py` + `tests/test_sem_loader.py` 補測 ——
      合成「6 顆 defect × 5 頁」的多頁 fixture，載入後每顆 `(file, page)` 互不相同；
      現有 `sample_real.klarf`（單頁 jpg、`Images 1 { "…jpg" "JPG" 1 "24" }`）
      仍走 sequential 且 `page` 對應正確、**既有測試全綠**。

### M3: 對位頁可設定（必要 2）  [status: planned]

- [ ] fine-align 設定加 `align_page_ordinal`（1-based，defect 頁清單內序位，預設 **2**），
      進 `cfg` 隨 worker 走、存 QSettings。
- [ ] 三個讀圖點改走 `read_sem_gray(path, page)`：`fine_align.py:785`、
      `overlay_export.py:114`、`overlay_export.py:340`。job tuple 帶上 page。
- [ ] UI：FineAlign 面板新增「Align page」數字欄（附說明：一顆 defect 內的第幾張，
      BSE 通常是 2）。
- [ ] 驗證：`tests/test_fine_align.py` / `test_export_fused.py` 護欄 —— 單頁資料
      （`page=None`）的對位結果與匯出 PNG **byte-identical**；多頁 fixture 改 ordinal
      會讓 `fine_dx/dy_nm` 改變、設回去逐項相同。

### M4: manifest 契約（建議 3 / 4 / 5）  [status: planned]

- [ ] `fine_align.OVERLAY_MANIFEST_COLS` 增補：`id_source`、`width_px`、`height_px`、
      `nm_per_px`（逐張）、`page`。逐張 `nm_per_px` 本來就在 worker 內算
      （`fov_w / W`），只是沒寫出來。
- [ ] `status` 補齊五種：`ok` / `low-score`（gate 沒過）/ `no-coords`（無座標）/
      `flat`（ROI 無幾何）/ `missing-file` / `not-run`，與
      `gds_align_tool.fine_align_result_rows` 既有語彙一致。目前 gate 沒過只是檔名
      空白、status 仍 `ok` —— 這是本 milestone 的主要行為修正。
- [ ] schema `mmh-gds-overlay-v3` → **`mmh-gds-overlay-v4`**（新增欄位為 additive，
      既有 `label_png` 整數 label map 契約與 `label_view_png` 不動）。
- [ ] 建議 5：`label_map` 的 `layer` 名穩定性規範寫進 README/本檔 ——
      同一份 recipe 生命週期內穩定、變更要 bump schema；匯出時對「不能當變數用的字元」
      （空白、減號等）發出可照做的警告（**不自動改名**，避免下游 recipe 指不到）。
- [ ] 驗證：`tests/test_overlay_manifest.py`（新）—— 五種 status 各自可重現、新欄位
      round-trip、schema 字串為 v4；ADEPT 那側的讀法（`gray[label == id]`）不受影響。

### M5: 真機驗收  [status: planned]

- [ ] user 用真實多頁 EBI KLARF 跑一次 Export…，確認每顆 defect 的 label 不同。
- [ ] 把產出的 manifest 交給 ADEPT 側試接（`roi_from_mask` 卡）。

---

## Affected Files

- `glas/core/tiff_index.py`（新，移植自 ADEPT/KLIP）
- `glas/core/klarf_pages.py`（新）
- `glas/core/klarf_parser.py`（`_image_entries` / `_image_count` 增補）
- `glas/core/fine_align.py`（讀圖入口、manifest 欄、status）
- `glas/core/overlay_export.py`（兩個讀圖點、row 欄位、status）
- `glas/app/sem_loader.py`（`page` / `pages` / `id_source`）
- `glas/app/gds_align_tool.py`（align page UI + cfg、job tuple、manifest schema v4）
- `requirements.txt`（`tifffile` optional）
- `tests/test_tiff_index.py`、`tests/test_klarf_pages.py`、`tests/test_overlay_manifest.py`（新）
- `tests/test_sem_loader.py`、`tests/test_fine_align.py`、`tests/test_export_fused.py`（增補）
- `README.md`（匯出契約章節）、`docs/plans/F31-*.md`、`CLAUDE.md` §8

---

## Risks / Open Questions

- **沒有真實多頁 EBI KLARF fixture。** M2 會用合成 fixture（自寫多頁 TIFF + 對應 KLARF）
  覆蓋兩種 mode，但 `imagelist` 模式的真實欄位排列只有 ADEPT 的推斷邏輯背書 ——
  **M5 真機驗收前不宣稱這條路徑已驗證**。若 user 能提供一份真實檔（哪怕只有前幾顆
  defect）可大幅降低風險。
- **`tifffile` 是新相依。** 標為 optional：沒裝時單頁流程完全不受影響，多頁走
  `cv2.imreadmulti` fallback（會解碼整份、大檔很慢）。
- **§7 不變式**：`fine_align` / `overlay_export` 的讀圖點在 F25 融合路徑上，
  `test_export_fused` 的 byte-identity 護欄必須維持綠 —— 單頁 `page=None` 必須走
  與現在完全相同的 `cv2.imread`。
- **schema bump 的下游時機**：v4 是 additive，但 ADEPT 若已寫死 v3 字串需同步；
  交付時要在 SESSION_LOG 註明。
- `_export_label_map` 目前用 `_entry_label(e)` 產層名，Boolean 合成層的 `name` 由使用者
  輸入 —— 建議 5 的字元檢查會在這裡發警告。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/ -v` 全綠（現況 852 passed / 4 skipped，本 feature 只增不減）
- [ ] `python3 -m py_compile` 所有改動檔
- [ ] 手動：載入多頁 KLARF → 每顆 defect 的預覽影像不同 → Export… → manifest 每列
      有 `id_source` / `width_px` / `height_px` / `page`，且五種 status 都能重現
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F31]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history（同時是 GLAS 側對 ADEPT 契約的實作紀錄；
  契約本身的唯一出處在 ADEPT repo 的 `ADEPT-GLAS-INTERFACE.md`，GLAS 不另抄一份）
