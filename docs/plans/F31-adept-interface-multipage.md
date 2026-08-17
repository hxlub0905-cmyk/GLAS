# [F31] ADEPT 介面契約：EBI-patch KLARF ingest + manifest 契約補齊

> **狀態：** planned
> **§8 ID：** [F31]
> **建立：** 2026-08-17
> **負責 branch：** `claude/adept-glas-interface-s5e8fb`

---

## Goal & Context

**來源：** ADEPT（下游）提出的《ADEPT ← GLAS 介面契約》。ADEPT 已定調**不做 OASIS/GDS
parser** —— layout 的解析與對位全部留在 GLAS，ADEPT 只吃 GLAS 匯出的
`<id>_label.png` / `<id>_gray.png` / manifest，join key 是 KLARF `DEFECTID`。

**要處理的資料型別（user 2026-08-17 定調）：** **一顆 KLARF DID 對應兩張 patch**
（第 1 頁 test、第 2 頁 ref），整批裝在**一個多頁 TIFF**裡，lot 層以 `TiffFileName`
指出檔名 —— 即 ADEPT `dataset.load_dataset` 的 `kind="ebi_patch"`。

> **明確排除：** 「一頁 TIFF 內含多張 patch」的 BSE/SE 拼版型**不處理** ——
> user 指出那種資料本身就沒有 KLARF 檔，不在本 feature 範圍。（原 plan 依契約文件
> 寫的「一顆 defect 1 BSE + 4 SE、BSE 固定第 2 頁」也隨之作廢。）

### 現況實測：GLAS 對 EBI-patch KLARF 載入 **0 張影像**

規劃期用 ADEPT 的 EBI fixture 實跑 `sem_loader.load_klarf`，兩種格式都失敗：

| 輸入 | 結果 |
|---|---|
| **KLARF 1.2 flat**（`FileVersion 1 2` + `DefectRecordSpec N … ;`） | `klarf_parser` 不支援此格式：`defect_columns` 為空、defect 欄位全落在 `_extra_N`，連 `SummarySpec` / `EndOfFile` 的 token 都被吃進 defect 列 → `load_klarf` 回 **[]** |
| **KLARF 1.8 hierarchical + `IMAGECOUNT`/`IMAGELIST` 欄、無 `Images {…}` 區塊** | 欄位解析正常，但 `IMAGELIST` 被當成單一純量欄（`IMAGELIST: '1'`，其餘 token 落 `_extra_8..10`）；且 `load_klarf` 對沒有 `_image_filename` 的 defect 一律 `continue` → **[]** |

所以缺口**不是**「`SemImage` 少一個 `page` 欄位」，而是 **GLAS 的 KLARF ingest 不支援
EBI-patch 這個形狀**（lot 層 `TiffFileName` + `IMAGECOUNT`/`IMAGELIST` 欄、defect 列
沒有檔名）。原始契約文件把它描述成「`cv2.imread` 只讀第 0 頁」，那是**下一層**的問題
—— 得先能載入，才輪得到讀哪一頁。

### 連帶問題：KLARF 1.2 的座標單位是 µm

ADEPT `klarf_core.UNIT_INFO`：`1.2 → µm (to_nm=1000)`、`1.8 → nm (to_nm=1.0)`。
GLAS 的 `gds_fov.klarf_to_gds` **無條件把 XREL/YREL 當 nm** —— 餵 1.2 檔會差 1000×。
CLAUDE.md §7 明令 `klarf_to_gds` 不可動（user 已實測落點正確），因此**單位換算一律在
載入端（`sem_loader`）做完**，`klarf_to_gds` 收到的永遠是 nm。1.8 的 `to_nm=1.0`
→ 現有流程逐項不變。`sem_loader.read_die_pitch_nm` 的正則同樣只認 1.8 的
`Field DiePitch 2 {…}`，1.2 的 `DiePitch 1000.0 1200.0;` 需補（且同樣要換算）。

### 第三層：讀圖恆讀第 0 頁

`SemImage` 無 page 欄位，三個讀圖點都是 `cv2.imread(path, IMREAD_GRAYSCALE)`
（`fine_align.py:785`、`overlay_export.py:114`、`overlay_export.py:340`）→ 就算載入
成功，每顆 defect 仍會拿同一張圖對位。**且影像的 `H/W` 決定 `nm_per_px = fov_w / W`**，
所以讀錯頁不只對到別顆的圖，連比例尺都可能錯。

### 成功長什麼樣

- 一份 EBI-patch KLARF（1.2 或 1.8）載入後，每顆 defect 有 2 個頁碼、`(file, page)` 互不
  相同；兩顆不同 defect 產出的 `_label.png` 不相同。
- 座標無論來源版本，進 `klarf_to_gds` 前都已是 nm。
- 「對位用哪一張」是設定值不是寫死常數（預設 **ref = 第 2 張**）。
- manifest 每列都能讓 ADEPT 自我驗證：`id_source`、影像網格、五種可區分的 `status`。

**與現有系統的關係：並存，不取代。** 現有 rSEM 流程（per-defect `Images {…}` 檔名、
1.8 nm 座標、單頁影像）走原路徑，讀圖 **byte-identical**；EBI-patch 是偵測後才啟用的
第二條 ingest 路徑。

---

## Q&A Decisions

### Q1: 「一顆 defect 對到 TIFF 哪幾頁」怎麼算？
**選擇：** 先讀 ADEPT repo（已 clone `/workspace/adept`，HEAD `13153f4`），照抄語意。
**結論：** ADEPT `klarf_core.defect_image_map(n_pages)` 已是**雙模式交叉驗證**：
- `imagelist` 模式：IMAGELIST 每條目的第一欄若全是整數且不重複 → 當 TIFF page 編號，
  用 `n_pages` 驗證 `[lo..hi]` 落點自動判 0/1-based。
- 1.8 結構化 `Images N { … }`（id 是 defect 內序號 1..IMAGECOUNT 而非全域 page）→
  明確排除，退回 sequential。
- 重複 id、或範圍塞不進 `n_pages` → 退回 sequential 並留 `notes`。
- `sequential`：依 defect 出現順序用各自 IMAGECOUNT 切連續頁區段。

### Q2: 資料是 1.2 還是 1.8？
**選擇：** **兩種都有 / 不確定 → 兩條都做**，單位依版本自動判定（同 ADEPT `unit_info()`）。

### Q3: EBI-patch 用哪個 parser 讀？
**選項：** 移植 ADEPT `KlarfDoc` 只跑 EBI 路徑 / 擴充 GLAS 現有 parser / 讀端全面改用 KlarfDoc
**選擇：** **移植 ADEPT `KlarfDoc`，只在偵測到 EBI-patch 形狀時使用**
**理由：** GLAS 現有 `klarf_parser` 還肩負**無損寫回**的職責（`hier_prefix` /
`hier_suffix` / `_image_block_raw` 原文保留），在它裡面補 1.2 解析與 IMAGELIST 欄處理
回歸風險高；而 ADEPT 的 `KlarfDoc` 已對 1.2 / 1.8 / variant D / 單位 / `tiff_path`
有測試覆蓋。現有 rSEM 流程與無損寫回**完全不動**。
**已知代價：** 兩個 parser 共存有漂移風險（ADEPT 文件 F7-17「平行路徑會腐爛」的教訓）
→ 緩解：移植檔頂端標明來源 + commit，且 **GLAS 只讀不寫**（`KlarfDoc` 的寫回 API 不使用），
兩者職責不重疊。

### Q4: 兩張 patch，對位預設用哪一張？
**選項：** ref（第 2 張）/ test（第 1 張）/ 不設預設，首次匯出前問
**選擇：** **ref（第 2 張）**
**理由：** ref 是鄰近 die 同位置的影像、**沒有 defect 干擾**，結構與 layout 最乾淨 →
`matchTemplate` 分數最穩。對位結果對 test/ref 都適用（同一 die 內相對位置相同）。
**邊界：** 某顆 defect 只有 1 頁時退回最後一張可用頁，並在該列 `notes`/log 標明。

### Q5: 建議 4 的 status 粒度？
**選擇：** 補齊五種 status + schema bump `mmh-gds-overlay-v3` → `v4`（同時容納建議 3 新欄位）

### Q6: 本次範疇？
**選擇：** 必要 1 + 必要 2 + 建議 3 + 建議 4/5（全部）

---

## Milestones

### M1: TIFF page 讀取底座（Qt-free core）  [status: done 2026-08-17]

- [x] 移植 ADEPT `core/ingest/tiff_index.py` → `glas/core/tiff_index.py`，保留來源標註
      （KLIP → ADEPT → GLAS）與**兩段防護的原註解**：
      - handle 快取版本鍵含 **pid** —— fork 出來的 worker 共用檔案偏移量會靜默讀到
        別頁的 bytes（GLAS 的 export ProcessPool 正是這個情境）；
      - `read_page` 全程持 `RLock` —— QThread 併發共用 handle 會讓 tifffile 把像素當
        IFD 解析（`suspicious number of tags`）。
- [x] 新增 `read_sem_gray(path, page=None)` 作為讀圖單一入口：
      - `page is None` → 原封不動 `cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)`（**byte-identical**）；
      - `page` 有值 → `tiff_index.read_page` 後轉 uint8 灰階（多通道走 cv2 灰階轉換、
        16-bit 依現有慣例縮放）；
      - `tifffile` 不在 → `cv2.imreadmulti` fallback（log 說明代價）。
- [x] `requirements.txt` 加 `tifffile`（標為多頁 TIFF 才需要的 optional 相依）。
- [x] 驗證：`tests/test_tiff_index.py` —— 合成多頁 TIFF（classic + BigTIFF）頁數正確、
      單頁 `read_sem_gray(page=None)` 與 `cv2.imread` **逐 byte 相同**、越界 page 丟
      `IndexError`、無 tifffile 時 fallback 可跑。

### M2: EBI-patch KLARF ingest  [status: done 2026-08-17]

- [x] 移植 ADEPT `core/ingest/klarf_core.py` 的**唯讀**部分 → `glas/core/klarf_doc.py`
      （`KlarfDoc` 載入 / `unit_info` / `col_index` / `image_layout` /
      `defect_image_entries` / `defect_image_map` / `tiff_path`）。**不移植寫回 API。**
- [x] `sem_loader` 新增 EBI-patch 偵測與載入路徑：找得到 patch TIFF
      （`TiffFileName` 或 KLARF 同名 `.tif`）且 `defect_image_map` 對得出頁 → 走新路徑；
      否則**完全走現有 rSEM 路徑**（現行程式碼一行不改）。
- [x] `SemImage` 加 `page: Optional[int]`（0-based，實際用於對位/匯出的頁）、
      `pages: list[int]`（該 defect 完整頁清單）、`id_source: str`
      （`"klarf-defectid"` | `"filename-stem"`）。
- [x] **單位換算在載入端做完**：`xrel/yrel = raw × to_nm`（1.2 → ×1000、1.8 → ×1）。
      `klarf_to_gds` 不動（§7）。`read_die_pitch_nm` 補 1.2 的 `DiePitch a b;` 寫法 + 換算。
- [x] `load_folder`：`id_source="filename-stem"`。
- [x] 驗證：`tests/test_klarf_doc.py` + `tests/test_sem_loader.py` 補測 ——
      1.2 flat 與 1.8 hierarchical 的 EBI fixture 各載入出「每顆 2 頁、`(file,page)` 互異」；
      1.2 的 µm 座標換算後與等價 1.8 檔逐項相同；`sample_real.klarf`（rSEM 單頁）
      **既有測試全綠且逐項不變**。

### M3: 對位頁可設定（必要 2）  [status: done 2026-08-17]

- [x] fine-align 設定加 `align_page_ordinal`（1-based，defect 頁清單內序位，預設 **2 = ref**），
      進 `cfg` 隨 worker 走、存 QSettings；頁數不足時退回最後一張並標記。
- [x] 讀圖點改走 `read_sem_gray(path, page)`。**實作時發現是 5 處不是 plan 寫的 3 處**：
      除 `fine_align.py:785` / `overlay_export.py:114` / `overlay_export.py:340` 外，app 還有
      `_load_sem_gray`（單張 Run + template 預覽）與 `SemViewer.set_image` 的 `QPixmap`。
      後兩者不改的話，畫面顯示第 0 頁而批次對位第 1 頁 —— 使用者會對著看不到的影像調參數。
      job tuple 以**可選末位元素**帶 page（舊的 4/5-tuple 仍合法 → 既有測試與呼叫端不動）。
- [x] UI：FineAlign 面板新增「Align page」欄（說明：一顆 defect 內的第幾張；
      第 1 張 = test、第 2 張 = ref）。
- [x] 驗證：`test_fine_align` / `test_export_fused` 護欄 —— 單頁資料（`page=None`）的
      對位結果與匯出 PNG **byte-identical**；多頁 fixture 改 ordinal 會讓 `fine_dx/dy_nm`
      改變、設回去逐項相同。

### M4: manifest 契約（建議 3 / 4 / 5）  [status: planned]

- [ ] `fine_align.OVERLAY_MANIFEST_COLS` 增補 `id_source`、`width_px`、`height_px`、
      逐張 `nm_per_px`、`page`。逐張 `nm_per_px` 本來就在 worker 內算（`fov_w / W`），
      只是沒寫出來。
- [ ] `status` 補齊五種：`ok` / `low-score`（gate 沒過）/ `no-coords` / `flat` /
      `missing-file` / `not-run`，與 `gds_align_tool.fine_align_result_rows` 既有語彙一致。
      **目前 gate 沒過只是檔名空白、status 仍 `ok`** —— 這是本 milestone 的主要行為修正。
- [ ] schema `mmh-gds-overlay-v3` → **`mmh-gds-overlay-v4`**（新增欄位 additive；
      `label_png` 整數 label map 契約與 `label_view_png` 不動）。
- [ ] 建議 5：`label_map` 的 `layer` 名穩定性規範寫進 README ——
      同一份 recipe 生命週期內穩定、變更要 bump schema；匯出時對「不能當變數用的字元」
      （空白、減號等）發出可照做的警告（**不自動改名**，避免下游 recipe 指不到）。
- [ ] 驗證：`tests/test_overlay_manifest.py`（新）—— 五種 status 各自可重現、新欄位
      round-trip、schema 字串為 v4。

### M5: 真機驗收  [status: planned]

- [ ] user 用真實 EBI-patch KLARF 跑一次 Export…，確認每顆 defect 的 label 不同。
- [ ] 產出的 manifest 交給 ADEPT 側試接（`roi_from_mask` 卡）。

---

## Affected Files

- `glas/core/tiff_index.py`（新，移植自 ADEPT/KLIP）
- `glas/core/klarf_doc.py`（新，移植自 ADEPT `klarf_core` 的唯讀部分）
- `glas/core/fine_align.py`（讀圖入口、manifest 欄、status）
- `glas/core/overlay_export.py`（兩個讀圖點、row 欄位、status）
- `glas/app/sem_loader.py`（EBI 偵測路徑、`page`/`pages`/`id_source`、單位換算、DiePitch 1.2）
- `glas/app/gds_align_tool.py`（align page UI + cfg、job tuple、manifest schema v4）
- `requirements.txt`（`tifffile` optional）
- `tests/test_tiff_index.py`、`tests/test_klarf_doc.py`、`tests/test_overlay_manifest.py`（新）
- `tests/test_sem_loader.py`、`tests/test_fine_align.py`、`tests/test_export_fused.py`（增補）
- `README.md`、`docs/plans/F31-*.md`、`CLAUDE.md` §8
- **不動**：`glas/core/klarf_parser.py`（無損寫回職責）、`glas/core/gds_fov.py::klarf_to_gds`（§7）

---

## Risks / Open Questions

- **沒有真實 EBI-patch KLARF fixture。** M2 會用合成 fixture（1.2 + 1.8 各一）覆蓋兩種
  mode，但真實欄位排列只有 ADEPT 的推斷邏輯背書 —— **M5 真機驗收前不宣稱這條路徑已驗證**。
  user 若能提供一份真實檔（哪怕只有前幾顆 defect）可大幅降低風險。
- **兩個 KLARF parser 共存的漂移風險**（Q3 已權衡）：緩解是 GLAS 側 `KlarfDoc` **只讀不寫**、
  檔頂標明來源與 commit。
- **`tifffile` 是新相依**：標為 optional；沒裝時單頁流程完全不受影響，多頁退回
  `cv2.imreadmulti`（會解碼整份，大檔很慢）。
- **§7 不變式**：`klarf_to_gds` 與 `test_export_fused` byte-identity 護欄必須維持綠 ——
  單頁 `page=None` 必須走與現在完全相同的 `cv2.imread`；單位換算只在載入端。
- **schema bump 的上線順序**：v4 是 additive，但 ADEPT 若已寫死 v3 字串需同步 ——
  交付時在 SESSION_LOG 註明，兩邊上線先後由 user 決定。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/ -v` 全綠（現況 852 passed / 4 skipped，本 feature 只增不減）
- [ ] `python3 -m py_compile` 所有改動檔
- [ ] 手動：載入 EBI-patch KLARF → 每顆 defect 的預覽影像不同 → Export… → manifest 每列
      有 `id_source` / `width_px` / `height_px` / `page`，五種 status 都能重現
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F31]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history（契約本身的唯一出處在 ADEPT repo，GLAS 不另抄一份）
