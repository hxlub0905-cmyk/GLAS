# [F11] 整顆 chip OASIS 匯出 + GDS 座標可見性

> **狀態：** planned — 待核准
> **§8 ID：** [F11]
> **建立：** 2026-05-27
> **負責 branch：** claude/adoring-cannon-oKZKo

---

## Goal & Context

F9 已能匯出**目前載入 FOV/ROI** 的 raw + Boolean layer 成 .oas（user 已驗收：KLayout 開得起來、
boolean layer 也看得到）。但 user 真正要的是：

1. **整顆 chip 匯出**——原始 layer + Boolean 新 layer，涵蓋整片，而非只有當前 FOV。
   （Q&A：Boolean 採**全 chip 重算**；目標檔**蠻大**。）
2. **GDS 座標可見性**——要 clip 特定區域需知道 GDS 座標，但目前讀數不明顯。
   （Q&A：**兩者都要**——常駐 cursor 座標 + 裁剪框一鍵帶入。）

**現況限制（探索結果）：**
- GLAS 刻意移除 full-load、改走 ROI 隨機存取（`gds_align_tool.py:12`）；Boolean layer 是
  **FOV-local**（`:46`、`_recompute_recipes(fov)`），非全 chip。
- 座標讀數 `_status_cursor` 只接 **GDS 畫布 hover**（`:4598`）、且會被其他訊息蓋掉（`:5139/5150/5228`）；
  SEM 模式下看不到。

---

## Q&A Decisions

- **Q1 Boolean 範圍：** 全 chip 重算（正確的全域合成）。
- **Q2 檔案大小：** 蠻大 → 效能/記憶體是首要風險；必須 worker thread + 進度 + cancel；保留 tiled fallback。
- **Q3 座標顯示：** 常駐 cursor GDS 讀數 **且** 裁剪框「帶入目前視窗/ROI 範圍」鈕。（M1 已完成）
- **Q4 OOM/tiled（user 後續顧慮）：** 改採 tiled + 串流寫出為主，避免全域 shapely OOM。tile 大小策略待定
  （自動依記憶體預算 vs 使用者指定）。

---

## Milestones

### M1: GDS 座標可見性（快速里程碑，先做）  [status: done, 待本地驗收]

- [x] 常駐、明顯的 cursor GDS 座標讀數：新增 `self._coord_readout`（粗體、addPermanentWidget、獨立於
      `_status_cursor` 暫時訊息）；SemViewer 新增 `cursor_gds` signal（`_view_to_world` 換算，mouseMove emit、
      leave emit None），GDS 畫布 `cursor_pos_nm` 與 SemViewer `cursor_gds` 都接到 `_on_coord`。
      同時顯示 µm + nm。**SEM 與 GDS 兩模式都可見**。
- [x] `OasisExportDialog` 裁剪區加「**Use current view / ROI bounds**」鈕 → `_fill_crop_from_bbox` 以
      doc.bbox_nm 填四格（無 doc 時 disabled）。
- [ ] 驗證：SEM/GDS 移動滑鼠都看得到座標；裁剪框一鍵帶入正確。**待 user 本地 GUI。**

> **更新（user 顧慮 OOM）：** M2/M3 改採 **tiled + 串流寫出**為主（非 fallback）。全域 shapely 的
> OOM 風險來自中間的數百萬 shapely 物件；tile 切算讓峰值受單一 tile 控制。

### M2: 串流 OASIS writer + 整 chip RAW 匯出  [status: planned]

- [ ] `oasis_writer` 加**串流/增量**模式（open 檔 → 寫 MAGIC/START/CELL → 逐 layer/逐多邊形 append
      RECTANGLE/POLYGON → 寫 256-byte END）。現有 `serialize_oasis`（一次組包）保留給 FOV 小量匯出。
- [ ] 整 chip RAW：走訪整 chip（oasis_random ROI=root bbox，或分區走訪）→ **逐多邊形串流寫出**，
      記憶體只佔約一個 cell。不污染對位中的 `self._doc`。
- [ ] worker + `LoadProgressDialog` + cancel。驗證：整 chip raw → KLayout 與原檔同區比對一致。

### M3: tiled Boolean 重算 + 匯出  [status: planned]

- [ ] 把 chip bbox 切成 grid tiles；每 tile：
  - 載入 **haloed bbox**（tile 外擴 margin ≥ 運算式最大 morph 距離；含跨界完整多邊形）的 bound 幾何；
  - 以 `fov = haloed bbox` 重算 recipes（沿用 `_eval_expression`，morph bbox 用 haloed）；
  - 結果 **clip 回 tile 精確邊界** → 串流寫出（相鄰 tile 無縫、不重疊；跨界圖形切成相鄰塊，幾何正確）。
- [ ] 一次只持有一個 tile 的 shapely 物件 → 峰值受 tile 大小控制。
- [ ] tile 大小：可設定，預設依記憶體預算自動（見 Q4）；halo 由運算式 morph 距離推導 + 最小值。
- [ ] worker + 進度（per-tile）+ cancel。驗證：tiled 結果與單塊小範圍 global 結果一致（無邊界假影）。

### M4: 匯出對話框 scope 選項 + 接線  [status: planned]

- [ ] `OasisExportDialog` 加「匯出範圍」選擇：**目前 FOV（現狀）/ 整顆 chip**。
- [ ] 整 chip 模式走 M2/M3 的 worker 流程；FOV 模式維持 F9 既有路徑。
- [ ] Debug 報告 / sidecar 沿用 F10。

### M5: 測試 + 文件  [status: planned]

- [ ] core 全 chip 遍歷 + 整 chip boolean 重算的單元測試（小型合成 OASIS）。
- [ ] `py_compile` + `pytest`；README/CLAUDE 更新。

---

## Affected Files

- `glas/app/gds_align_tool.py`（座標讀數、export dialog scope、worker）
- `glas/core/layout_export.py`（整 chip 蒐集 helper）或新 core 模組
- 可能 `glas/core/oasis_random.py`（全 chip 走訪入口，若需要）
- 測試 + `README.md` / `CLAUDE.md` / `SESSION_LOG.md` / 本 plan

---

## Risks / Open Questions

- **效能（最高）：** 「全 chip 重算 boolean + 大檔」是最重組合；全域 shapely 可能 OOM/極慢。需 worker +
  cancel + 量級警告；tiled 為 fallback。實測 user 真實檔後再定是否需 tiled。
- 全 chip 走訪 = 放棄 ROI 剪枝、等同 full load；大檔記憶體峰值高（幾何 + shapely + writer bytes 三份）。
  可評估邊走邊寫（streaming）降低峰值，但 boolean 需要全域幾何故難全 streaming。
- 整 chip 匯出 cellname / 座標原點沿用 doc.top_cell_name / 既有 nm 座標（與 F9 一致）。
- 不動 §7 不變式（OASIS decode / 座標換算 / 對位）；新增獨立匯出路徑。

---

## 驗證方式

- [ ] M1：兩模式座標可見 + 裁剪框帶入；M2/M3：整 chip 匯出 → KLayout 與原檔比對；M5：`pytest` 綠
- [ ] `SESSION_LOG.md` 有紀錄

---

## 完成後

- SESSION_LOG 註記 `完成 [F11]`；從 `CLAUDE.md` §8 移除；本檔留作 design history。
