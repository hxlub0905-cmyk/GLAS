# Session Log

---

## [2026-05-27] [F11] M5 文件：README / CLAUDE 更新（FOV / 整 chip 匯出範圍）

**變更類型：** 文件

**內容：** README OASIS 匯出條目 + CLAUDE §1 能力 5 補「範圍可選目前 FOV（含裁剪）或整顆 chip（tile 串流 +
全 chip 重算 boolean）」；§4 oasis_writer/layout_export 描述補 F11 串流 writer / tile_grid。F11 plan M5 勾選。
**剩餘待 user 本地**：`pytest` 綠 + 整 chip 端到端（worker/GUI/真實檔 KLayout 比對）+ OOM/效能實測。

**影響檔案：** `README.md`、`CLAUDE.md`、`docs/plans/F11-whole-chip-export.md`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-27] [F11] M2/M3/M4：整顆 chip 匯出（tiled raw + boolean 重算 + 串流 + dialog scope）

**變更類型：** 功能（core + app）

**實作：**
- **`layout_export.tile_grid(bbox, target=250µm, max 64/axis)`**：依 chip span 自動分格（user 選自動），
  覆蓋到角落、相鄰無縫。+ 測試（single/covers-exactly/degenerate）。
- **`WholeChipExportWorker`（app，QThread）**：分 tile 走訪——raw layer `walk_roi(tile)` → `clip_polygons`
  到 tile → `OasisStreamWriter` 串流寫；boolean 以 haloed tile（外擴 `_whole_chip_halo_nm` = 最大
  morph 距離 +1µm）建 tile-scoped `raw_provider`（per layer walk haloed 區 + cache）呼叫
  `gds_boolean.resolve_expression(fov_bbox=haloed)`，結果 clip 回 tile 串流寫。一次只持有一 tile 的
  shapely 物件 → 峰值受 tile 控制（解 OOM 顧慮）。`_walk_res_to_polys` helper（rects→4點 + polys）。
- **`OasisExportDialog`**：加 scope 下拉（Current FOV / Whole chip，whole 僅 rar+root 在時出現）；whole 停用
  裁剪欄位；`selected_specs()` 回 (entry, out_l, out_d) 供 worker 還原來源（raw 用 key、synthetic 用 expr/bindings）。
- **`_on_export_oasis` 分流** + `_start_whole_chip_export` worker 啟動（LoadProgressDialog per-tile 進度 +
  cancel + finished/failed/cancelled handler + cleanup；debug 完成後對輸出跑 `report_file`）。`import re` 新增。

**測試：** `py_compile` 全過；core tile_grid 有單元測試。**沙箱無 numpy/shapely/PyQt6 → worker/GUI 與真實
整 chip 匯出待 user 本地驗收**（含 KLayout 與原檔比對、OOM/效能實測）。

**不動（§7）：** reachable_bbox 為獨立唯讀方法、未改 walk_roi/early-stop。

**影響檔案：** `glas/core/layout_export.py`、`glas/app/gds_align_tool.py`、`tests/test_layout_export.py`、
`docs/plans/F11-whole-chip-export.md`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-27] [F11] M2 基礎：oasis_random 唯讀 reachable_bbox accessor（chip 全域 bbox）

**變更類型：** 功能（core，§7 敏感區）

**動機：** 整 chip 匯出要切 tile，需先知整 chip 範圍 = `reachable_bbox(root)`。原計算埋在 walk_roi closure。
user 選「加唯讀 accessor」+「自動分格」。

**實作（`oasis_random.py`）：** 新增 `RandomAccessReader.reachable_bbox(cell_id)`（grid frame）+
`reachable_bbox_nm`（scale 成 nm），以 `_reachable_bbox` 遞迴**忠實複製** walk_roi 內 closure 邏輯
（own bbox + 子 cell transform + repetition extent，共用 `self._reach_memo`）。**刻意做成獨立唯讀方法、
完全不改 walk_roi / CE early-stop 熱路徑**（§7：walk 用完整 load_cell、reachable 用 load_cell_bbox 的分工不變）。
測試 `TestReachableBbox`（placements 聯集 grid bbox、nm scale、unknown cell→None）。

**影響檔案：** `glas/core/oasis_random.py`、`tests/test_oasis_random.py`、`docs/plans/F11-whole-chip-export.md`、
`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-27] [F11] M2 部分：串流 OASIS writer（OasisStreamWriter）

**變更類型：** 功能（core）

**實作（`oasis_writer.py`）：** 抽出 `_oasis_header` / `_oasis_end`，`serialize_oasis` 沿用；新增
`OasisStreamWriter`（open→header→`add_polygons` 逐 layer append RECTANGLE/POLYGON→`close()` 寫 256-byte
END；context manager，錯誤時不 finalize 留半檔給呼叫端丟棄）。讓整 chip / tiled 匯出能增量寫、不持有整檔。
沙箱驗證輸出與 `serialize_oasis` **byte 完全一致**。測試 `test_stream_writer_matches_serialize` /
`test_stream_writer_roundtrips`。

**未完（M2 剩餘）：** 整 chip 走訪需 (1) chip 全域 bbox（來自 `oasis_random` reachable_bbox，§7 敏感，
擬加唯讀 accessor）+ (2) tile 大小決策（Q4，待 user）。故先 checkpoint writer，待 user 確認再動 §7 走訪。

**影響檔案：** `glas/core/oasis_writer.py`、`tests/test_oasis_writer.py`、`docs/plans/F11-whole-chip-export.md`、
`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-27] [F11] plan 修訂：M2/M3 改 tiled + 串流寫出（user 顧慮 OOM）

**變更類型：** 文件（plan 修訂）

**動機：** user 確認 M1 OK，但顧慮整 chip 全域 boolean 會 OOM、傾向 tile 切算。OOM 風險主因是全域
shapely 的數百萬中間物件。

**修訂：** M2 改為「串流/增量 OASIS writer + 整 chip raw 串流寫出」（記憶體只佔約一 cell）；M3 改為
**tiled boolean**——每 tile 載 haloed bbox（外擴 ≥ 最大 morph 距離、含跨界完整多邊形）算 boolean、結果
clip 回 tile 精確邊界串流寫出（相鄰 tile 無縫、跨界切成相鄰塊幾何正確），峰值受單 tile 控制。tile 大小
策略（自動 vs 指定）待 user 定（Q4）。

**影響檔案：** `docs/plans/F11-whole-chip-export.md`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-27] [F11] M1：GDS 座標可見性（常駐讀數 + 裁剪框一鍵帶入）

**變更類型：** 功能（UI）

**動機：** user 要 clip 特定區域需知 GDS 座標，但讀數不明顯——既有 `_status_cursor` 只接 GDS 畫布 hover、
SEM 模式看不到、且被其他訊息蓋掉。

**實作（`gds_align_tool.py`）：** (1) 新增獨立常駐讀數 `self._coord_readout`（粗體、addPermanentWidget），
與 `_status_cursor`（暫時訊息）分離；`_on_coord(w)` 接受 (x,y) 或 None，顯示 µm + nm。(2) SemViewer 新增
`cursor_gds = pyqtSignal(object)`，mouseMove emit `_view_to_world`、leaveEvent emit None；GDS 畫布
`cursor_pos_nm` 經 `_on_cursor`→`_on_coord`、SemViewer `cursor_gds` 直接接 `_on_coord`——**SEM/GDS 兩模式
都看得到座標**。(3) `OasisExportDialog` 裁剪區加「Use current view / ROI bounds」鈕（`_fill_crop_from_bbox`
以 doc.bbox_nm 填四格）。

**測試：** `py_compile` 過；GUI 待 user 本地驗收（沙箱無 PyQt6）。

**進度：** F11 M1 done（待驗收）。**未動**：M2 整 chip raw 匯出、M3 整 chip boolean 重算（32GB RAM，
全域 boolean 對中型 chip 可行、tiled 留後備）、M4 對話框 scope、M5 測試。

**影響檔案：** `glas/app/gds_align_tool.py`、`docs/plans/F11-whole-chip-export.md`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-27] [F11] 規劃：整顆 chip 匯出 + GDS 座標可見性（待核准）

**變更類型：** 文件（plan，尚未動工）

**動機：** F9 FOV 匯出已驗收 OK（含 boolean，KLayout 可開）。user 進一步要 (1) 匯出**整顆 chip**（原始 +
boolean 新 layer），目前只能匯出當前 FOV；(2) UI 沒有明顯的 GDS 座標可看，難填裁剪座標。Q&A 收斂：
boolean **全 chip 重算**、目標檔**蠻大**、座標顯示**常駐讀數 + 裁剪框一鍵帶入兩者都要**。

**探索發現：** GLAS 刻意移除 full-load 走 ROI（`gds_align_tool.py:12`）、boolean 為 FOV-local
（`:46`、`_recompute_recipes(fov)`）；座標讀數 `_status_cursor` 只接 GDS 畫布 hover、且被其他訊息蓋掉。

**plan（`docs/plans/F11-whole-chip-export.md`）：** M1 座標可見性（快速；SEM/GDS 兩模式常駐讀數 + 裁剪框帶入鈕）、
M2 整 chip raw 全遍歷匯出（worker+進度+cancel）、M3 整 chip boolean 重算（最高效能風險，tiled 為 fallback）、
M4 匯出對話框 scope（FOV/整 chip）、M5 測試+文件。**最高風險：全 chip 重算 boolean + 大檔的全域 shapely 效能/記憶體。**

**影響檔案（規劃）：** `glas/app/gds_align_tool.py`、`glas/core/layout_export.py`（或新模組）、可能
`oasis_random.py`、測試、`docs/plans/F11-whole-chip-export.md`、`CLAUDE.md`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-26] [F9/F10] 本地驗收通過：KLayout 接受 + Diagnose 抓錯確認

**變更類型：** 文件（驗收記錄，無程式碼變更）

**結果：** user 本地驗收——(1) `pytest` 45 passed；(2) KLayout 開 256-byte-END 修正後的 `sample_good.oas`，
三 layer（17/0 RECTANGLE 方形、25/0 POLYGON 三角、40/1 g-delta POLYGON 斜四邊形）**正確渲染**，writer
OASIS 格式確認 KLayout 接受（最大風險點解除）；(3) Diagnose 對 broken 檔精準捕捉 decode error + hex + traceback；
(4) 開發者模式開關 OK。**剩餘**：GUI Export+Debug 端到端需載入 layout（cache .npz / production .oas），
待 user 有資料時測（F9 3c）。plan checkbox 同步更新。

**影響檔案：** `docs/plans/F9-layout-export.md`、`docs/plans/F10-debug-mode.md`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-26] [F9] 修 KLayout 接受度：END record 補滿到 256 bytes

**變更類型：** bug fix（writer 正確性）

**動機現象：** user 本地用 KLayout 開匯出的 `sample_good.oas` 被拒：
`Format error (too few bytes after END record) (position=92)`。SEMI P39 §14 規定 END record 必須補滿到
固定長度（含 id 共 256 bytes）；我們最小 END 只寫 `[2, scheme0]`（2 bytes），自家 lenient reader 接受、
但 KLayout 嚴格要求 256 bytes 故拒檔。

**修復（`oasis_writer.serialize_oasis`）：** END = `[2] + uint(0)` 後補 `0x00` 到整個 END record 共 256 bytes
（`_END_RECORD_LEN=256`）。自家 reader 不受影響——`iter_records` 在 END `return`、padding 永不被 decode；
`_read_end` peek 機制讀到 scheme=0 即止。檔案尾端從 2 bytes → 256 bytes（sample_good 93→347 bytes）。

**連帶修 `scripts/make_sample_oas.py`：** 因 reader 在 END 即停，原本砍尾 5 bytes 只砍到 padding 不再出錯；
改成砍進最後一個幾何 record（`len - 256 - 6`），sample_broken 才會真正觸發 decode error 供測 Diagnose。

**測試：** 新增 `test_end_record_padded_to_256`（END record 從 id 到 EOF 恰 256 bytes）+
`test_padded_end_still_roundtrips`（padding 後 reader 仍正確 round-trip）。沙箱驗證 byte 長度；
reader round-trip 待 user 本地 `pytest` + **KLayout 重開 347-byte sample_good.oas 確認接受**。

**影響檔案：** `glas/core/oasis_writer.py`、`tests/test_oasis_writer.py`、`scripts/make_sample_oas.py`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-26] [F9/F10] 加測試輔助腳本 `scripts/make_sample_oas.py`

**變更類型：** 工具（測試輔助，不影響 app/core 行為）

**動機：** user 手上尚無 production .oas，需要快速產測試檔來驗 KLayout 接受度（F9）與 diagnose 抓錯（F10）。

**內容：** `scripts/make_sample_oas.py` 用 `oasis_writer` 產 `sample_good.oas`（矩形→RECTANGLE、三角→POLYGON、
45° 多邊形→g-delta POLYGON，多 layer）+ `sample_broken.oas`（砍尾 5 byte，供測 Diagnose 錯誤捕捉）。
沙箱驗證標頭正確（MAGIC/START unit=1000/CELLNAME/CELL/XYABSOLUTE/RECTANGLE 0x7b）。

**影響檔案：** `scripts/make_sample_oas.py`（新）、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-26] [F9] PR#7 review fix（P2）：匯出 UI layer/datatype 上限放寬，避免靜默截斷

**變更類型：** bug fix

**動機現象：** PR#7 review（chatgpt-codex-connector，P2）指出 `OasisExportDialog` 的 layer/datatype
QSpinBox 限 `0..65535`，但 OASIS layer/datatype 是無上限 unsigned int、writer 也支援更大值——載入的
raw layer 若 ID > 65535 會被**靜默截斷**，使用者按預設匯出就把 layer 重映射成錯誤 ID。

**修復：** 兩個 spinbox `setRange(0, 65535)` → `setRange(0, 2_147_483_647)`（QSpinBox int 上限，涵蓋所有
真實 OASIS layer 號），prefilled 大 ID 不再被截斷。

**測試：** `py_compile` 過；行為驗收併入 F9 GUI 本地驗收。

**影響檔案：** `glas/app/gds_align_tool.py`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-26] [F10] OASIS debug mode：載入/匯出雙向診斷（可複製報告 + sidecar）

**變更類型：** 功能（新增 core 模組 + app UI）

**動機：** user 反映開發 OASIS streamer 時 parse 常出錯、F9 又加了 writer；手上尚無 production .oas，
希望先備 debug mode，拿到資料一出錯就能第一時間貼回診斷。Q&A 收斂：**兩端都要**（載入+匯出）、
**兩種輸出都要**（sidecar .debug.txt + app 內可複製對話框）。

**實作：**
- **`glas/core/oasis_debug.py`（新，Qt-free）**：`report_file(path, sent_layers=None, max_records)`——走
  `oasis_streamer` 統計 record histogram / per-layer rect+poly / START unit+offset_flag / cell names；
  **永不拋例外**，decode 出錯把 streamer 的 hex-context（`OasisFormatError` 內建）+ traceback 收進報告；
  給 `sent_layers` 時做送出 vs 讀回 round-trip 比對（OK/MISMATCH）。
- **`layout_export.export_layers`**：加 `debug` 參數，回傳改 `(layers_written, report|None)`，debug 時回讀
  寫出檔產報告。
- **app**：`DebugReportDialog`（唯讀 monospace + Copy to clipboard + Saved-to）；`OasisExportDialog` 加
  Debug checkbox；`_on_export_oasis` 取報告→落 `<檔>.oas.debug.txt`+顯示；File 選單 dev-only
  「Diagnose OASIS file…」(`_on_diagnose_oasis`)；載入失敗（`_on_open_roi` except、`_on_roi_failed`）
  在 dev mode 經 `_show_load_error` 自動對該檔跑 `report_file`→sidecar+可複製框（非 dev 維持 critical box）。
  載入路徑記 `self._roi_load_path` 供診斷。

**測試：** `tests/test_oasis_debug.py`（well-formed/round-trip/truncated 捕捉/缺檔）；`test_layout_export.py`
更新 `(n, report)` 回傳 + debug 報告測試。`py_compile` 全過（沙箱無 numpy/PyQt6 → pytest/GUI 待本地）。

**不動（§7 不變式）：** 純新增診斷，未改 OASIS decode / 座標換算 / 對位 / CE early-stop。

**影響檔案：** `glas/core/oasis_debug.py`（新）、`glas/core/layout_export.py`、`glas/app/gds_align_tool.py`、
`tests/test_oasis_debug.py`（新）、`tests/test_layout_export.py`、`docs/plans/F10-debug-mode.md`（新）、
`README.md`、`CLAUDE.md`、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

## [2026-05-26] [F9] M2/M3/M5/M6 實作：ROI 裁剪 + app 匯出對話框 + 開發者模式 + 文件

**變更類型：** 功能（新增 core 模組 + app UI + 文件）

**M2（`glas/core/layout_export.py`，shapely+numpy，獨立於純 stdlib 的 writer）：** `clip_polygons`/
`clip_layers`/`export_layers` + `shapely_to_rings`。`crop_bbox=(x1,y1,x2,y2)`（角點任意序）用
shapely `intersection(box)` 裁切；`None` → 整張。**O-holes 決議（user 選）**：`shapely_to_rings` 只取
外環、丟內環（= `gds_boolean.geometry_to_polygons` 慣例，匯出「所見即所得」無洞）；裁剪無洞多邊形不會
生洞。測試 `tests/test_layout_export.py`（passthrough/內框裁切/角點任意序/全外略過/holes 丟棄/Multi/
drop-empty/clip→write→read round-trip）。

**M3（app 匯出 UI）：** `OasisExportDialog`——每 layer 一列（checkbox + 輸出 layer/datatype spin；
synthetic 內部 layer=-1 不可寫 OASIS → 給可編輯輸出值，預設 1000+；raw 預填原值）+ 四個 GDS 座標輸入框
（留空=整張、部分/非數字/零面積→warning 擋下）。`_on_export_oasis` 仿 `_on_export_cache`：getSaveFileName
(*.oas) → `layout_export.export_layers`，unit=1000（GLAS 全程座標當 nm、1 DBU=1 nm；doc 未存來源 unit，
1000 自洽），cellname=doc.top_cell_name；成功更新 `_status_doc`。

**M5（開發者模式 gating）：** `self._dev_mode` 由 `QSettings("GLAS","GLAS")` 載入（預設 False、持久化）；
About 對話框 icon `_attach_dev_toggle` 點 5 次 → `_set_dev_mode` 切換 + 寫回 QSettings + QMessageBox 回饋；
Export OASIS 按鈕 `setVisible(self._dev_mode)`、`_refresh_dev_ui` 切換。匯出入口預設隱藏。

**M6（文件）：** README Features + CLAUDE §1 能力 5 + §4 目錄（oasis_writer/layout_export）。

**測試：** `py_compile` 全過（writer/layout_export/app/兩測試檔）。沙箱無 numpy/shapely/PyQt6 →
`pytest` 與 GUI/KLayout 驗收待 user 本地。

**不動（§7 不變式）：** 純新增，未改 OASIS decode、座標換算、對位符號、SemViewer、CE early-stop。

**影響檔案：** `glas/core/layout_export.py`（新）、`tests/test_layout_export.py`（新）、
`glas/app/gds_align_tool.py`、`README.md`、`CLAUDE.md`、`docs/plans/F9-layout-export.md`、`SESSION_LOG.md`。

**進度：** F9 M1–M6 程式碼完成，**待 user 本地驗收**後從 §8 移除。

**Branch：** `claude/adoring-cannon-oKZKo`

## [2026-05-26] [F9] M1 實作：core OASIS writer（最小合規）+ M4 測試

**變更類型：** 功能（新增 core 模組 + 測試）

**動機：** F9 plan 核准後開工。M1 = 自寫最小合規 OASIS writer，把 raw / Boolean layer 反向寫出 .oas。

**實作（`glas/core/oasis_writer.py`，純標準庫、無 Qt/numpy/shapely）：** encode 原語為 `oasis_streamer`
decode 的逆——`encode_unsigned_int`（7-bit varint）、`encode_signed_int`（mag<<1|sign）、`encode_real`
（整數 type0/1、非整數 type7 double）、`encode_string`、`encode_g_delta`（arbitrary form）。`serialize_oasis`/
`write_oasis` 輸出最小合規序列：MAGIC → START(unit, offset_flag=0, 6×(0,0)) → CELLNAME_IMP → CELL_REFNUM 0
→ XYABSOLUTE → 幾何 → END(validation scheme 0)。幾何分支（Q4）：`_axis_rect` 偵測 axis-aligned 矩形走
RECTANGLE(info `0x7b`)、其餘走 POLYGON(info `0x3b`, point-list type4 g-delta)；閉合重複頂點自動去除、
degenerate(<3) 略過。**設計依據**：逐項核對 `oasis_streamer` 的 decode（unsigned/signed/real/point-list/
RECTANGLE/POLYGON/START/END）+ 測試套件手組 OASIS 黃金 fixture（`test_oasis_streamer.py` 的 START/RECT
`0x7b`/CELL_REFNUM/END `uint0`）。offset_flag 選 0（表在 START）因 offset_flag=1 + 全 0 offset 會讓 reader
的 peek heuristic 把 0 誤判成 validation scheme。

**測試（`tests/test_oasis_writer.py`）：** encode 原語對 reader round-trip、黃金 fixture **byte 逐位元吻合**、
writer→`oasis_streamer` 幾何 round-trip（矩形/三角/45°/多 layer）、RECTANGLE vs POLYGON 偵測、閉合 ring、
空/degenerate 略過、deterministic。沙箱獨立驗證 writer byte 輸出 == 黃金 fixture（無 numpy 故 reader
round-trip 待 user 本地 `pytest`）。`py_compile` 全過。

**進度：** plan M1 done、M4 大部分 done（ROI 裁剪測試併入 M2）。**未完**：M2 幾何蒐集+ROI 裁剪（含
holes 決策 O-holes，已記 plan Risks 待 user 定）、M3 app 匯出、M5 開發者模式、M6 收尾。

**影響檔案：** `glas/core/oasis_writer.py`（新）、`tests/test_oasis_writer.py`（新）、`docs/plans/F9-layout-export.md`。

**Branch：** `claude/adoring-cannon-oKZKo`

## [2026-05-26] [F9] 規劃 v2：改為 OASIS 匯出 + ROI 裁剪（待核准）

**變更類型：** 文件（plan 修訂，尚未動工）

**修訂動機：** user 提出公司流程統一 .oas、要求更深的 GDSII vs OASIS 優缺點分析來說服。核對
`oasis_streamer` 後修正先前評估：(1) **格式決定因素是下游消費端**（公司 .oas）非 writer 難度；
(2) 自寫 OASIS writer 風險其實**可控且有界**——validation scheme 可為 0（無 CRC，`oasis_streamer.py:1510`）、
CBLOCK 選用、modal 非強制、encoder 是既有 decoder 的逆、且可用自家 reader 做 round-trip oracle。
**Q2 定案改為 OASIS (.oas)**。user 並澄清範圍：本 feature 專注「匯出 raw layer + Boolean layer 同檔 +
給 GDS 座標裁剪特定 ROI 區域」；「下游接水確認每張 SEM ROI」是**另一題**（對位資訊輸出，後續討論），移出 F9。
plan 重寫：M1 core OASIS writer（最小合規）、M2 幾何蒐集+ROI 裁剪 helper、M3 app 匯出動作、M4 測試收尾。

---

## [2026-05-26] [F9] 規劃：Layout 匯出（Boolean 合成 layer 反向寫出成 layout 檔）（待核准）

**變更類型：** 文件（plan，尚未動工）

**動機現象：** user 提出應用可擴充性，想做「GDS 匯出」。探索後確認 GLAS 幾何資料流目前「只進不出」——
OASIS reader → numpy/shapely → rasterize 成 mask → 只匯出 alignment offset（CSV/JSON），無任何 layout
writer。使用者投入合成的 Boolean layer（L0）無法存檔、無法丟回 KLayout、無法給下游工具當 ROI 來源。

**內容：** 用 AskUserQuestion 收斂三岔路——(1) 匯出對象主要為 **Boolean 合成 layer L0**（+ 允許選原始
layer）、(2) 格式 user 要求「評估一下」、(3) 走到先產 plan 再核准。探索 `gds_boolean`（evaluate→shapely
geom）、`oasis_store`/`oasis_streamer`（unit/grid、1 DBU≈1 nm）、`oasis_random`/`gds_fov`（root nm 座標）、
app 端 layer entry `.polygons` + synthetic `expr_text` + 既有 `_on_export_alignment`/`_on_export_cache`
接點後，產出 `docs/plans/F9-layout-export.md`（4 milestone：M1 Qt-free GDSII writer、M2 app 匯出動作、
M3 下游/對齊語意定案+文件、M4 測試收尾）。

**格式評估結論：建議先做 GDSII（非 user 初始講的 .OAS）**——目標「KLayout 能開 + 下游能接」GDSII 100%
達成，而自寫 OASIS writer 複雜度/正確性風險高一個量級（專案只寫過 reader），且 L0/ROI 小幾何用不到 OASIS
緊湊優勢。writer 介面設計成格式後端可分離，日後要加 OASIS 不需重寫上層。**此項與 O1（對齊後 layout 語意）
留待 user 核准時定案。**

**測試：** 無（純文件）。

**影響檔案：** `docs/plans/F9-layout-export.md`（新）、`CLAUDE.md`（§8 註冊 [F9]）、`SESSION_LOG.md`。

**Branch：** `claude/adoring-cannon-oKZKo`

## [2026-05-26] F5/F6/F7/F8 收尾 + F8 全程（規劃→實作→修測試→驗收→收尾）

**變更類型：** 收尾(F5) + 測試驗收(F6) + 功能/效能(F8) + test fix。本次對話（同日）累積數件，合併記錄。

**1. 完成 [F5]**（純文件收尾）：user 本地 GUI 驗收 fine-align 診斷工作流（Preview before/after、總覽表、
直方圖/散點、median→δ 收斂）全通過。`F5-finealign-diagnostics.md` checkbox/status 標註驗收；`CLAUDE.md`
§8 移除 [F5]（plan 留作 design history）。

**2. [F6] 等價測試本地全綠**（文件）：本地 Python 3.9.7 跑 accel/oasis_random/oasis_streamer → 170 passed
（mmap↔slurp、共享map↔獨立scan、循序↔4-worker 並行）。F6 thread-pool 之後由 F8 取代（M1/M2 mmap 仍在用），
F6/F7 連同 F8 於本次一併收尾（見第 5 點）。

**3. [F8] Batch 反應性與加速**（plan + 實作 M1–M4）：user 回報 Batch Align 非常卡、運算久、進度條花俏。
查出三根因：(a) `_on_fa_result` 每張整表重建 + 圖刪重生 = O(N²) 主執行緒重繪；(b) F6 thread-pool 8 條純
Python 解碼 thread 搶 GIL；(c) `_AnimatedBar` 漸層/發光/掃光/動畫。AskUserQuestion 收斂後產
`docs/plans/F8-batch-responsiveness.md` 並實作：
- **M1 扁平進度條**：`_AnimatedBar` 重寫成單色軌道+單色填充（去漸層/發光/掃光/in-bar%），高 20→14、
  determinate `advance()` 不重繪；API 不變→全 app 同步；移除無用 `QLinearGradient`/`QPainterPath` import。
- **M2 節流串流（修 O(N²)）**：`_batch_refresh_timer`（single-shot 300ms）合併刷新（~3x/sec）；
  `set_rows(..., rebuild_charts=False)` 串流時跳過直方圖/散點重建，只在 finished/cancelled/Results…/起跑 完整刷新。
- **M3 ProcessPool（抽 Qt-free core）**：新增 `glas/core/fine_align.py`，rasterize/template/matchTemplate/
  ROI-walk/`_fine_align_image` 等 10 個純函式逐字搬入（app re-export 取回，呼叫端與 m4b/accel 測試相容；
  `overlay_outlines_on_sem` 留 app）。`FineAlignAllWorker` 由 `ThreadPoolExecutor` 改 `ProcessPoolExecutor`
  (spawn)：worker 由**路徑**重建 reader（避開 Windows spawn 拉 PyQt6）、SEM 子行程自讀；cancel 用
  `fut.cancel()`（張邊界粒度、保留已完成）；`n<=2`/單核走 in-thread fallback（直接用 `self._rar`）。
- **M4 測試**：`test_accel_equivalence.py` 新增 `TestProcessPoolEquivalence`（`_pool_init` 由路徑重建 reader +
  `_pool_task` 每張 result 與循序 `_fine_align_image` 完全相等）。

**4. 修 4 個測試失敗**：1 個 F8 回歸——`_run_in_thread` 誤用 `self._rar.clone()`（`_FakeRar` 無 clone）→ 改回
直接用 `self._rar`；3 個既有過時/過嚴測試（git diff 確認受測函式 F8 前後逐字相同）——`test_expr_spec`
（斷言改 4-tuple 含 recipes 快照）、`test_draws_outline_colour`（`cv2.LINE_AA` 反鋸齒→改斷言「明顯偏紅」非
精確 255,0,0）、`test_batch_run`（F5 起 no-coords 會回 status 列→lambda 收 6 參數、斷言 D2 為 no-coords）。

**5. 收尾 [F6][F7][F8]**：驗收通過後依 §10 收尾——三個 plan checkbox/status 標 done（F6 註記 M3 thread-pool
已由 F8 ProcessPool 取代、M1/M2 mmap 仍在用；F7 註記進度條質感由 F8 回退扁平、串流由 F8 節流），`CLAUDE.md`
§8「進行中」清空（F6/F7/F8 移除）。三份 plan 保留作 design history。

**測試：** 本地全量 **206 passed**（含 ProcessPool 等價）。**F8 實機驗收通過**：UI 不卡、多核生效（工作管理員
見多個 python 子行程）、明顯變快、取消等待可接受、結果正確、進度條扁平 OK。

**不動（§7 不變式）：** 批次計算純函式只搬家、結果 byte/value 不變、fine-align 符號、SemViewer 折疊、
CE early-stop、median→δ。取捨：cancel 粒度由逐 node 即時改為單張影像邊界。

**影響檔案：** `glas/core/fine_align.py`（新）、`glas/app/gds_align_tool.py`、`tests/test_accel_equivalence.py`、
`tests/test_gds_align_f5.py`、`tests/test_gds_align_m4b.py`、`docs/plans/F5-*.md`、`docs/plans/F6-*.md`、
`docs/plans/F7-batch-workspace-ui.md`、`docs/plans/F8-batch-responsiveness.md`、`CLAUDE.md`、`SESSION_LOG.md`。

**Branch：** `claude/practical-pascal-AtKLm`

## [2026-05-25] [F6] PR#5 review fix（P1）：批次 cancel 後保留所有已完成結果

**變更類型：** bug fix

**動機現象：** PR#5 review（chatgpt-codex-connector，P1）指出 `FineAlignAllWorker.run()` 在
`as_completed` 迴圈裡一旦 `self._cancel.is_set()` 就 `break`，會丟掉其他**已完成**（或在 pool
`__exit__` 等待期間完成）的 future 結果——那些影像即使運算已完成也不會 emit `result`、不進
`_refined`/結果表。workers>1 時 cancel 行為變得不確定，且違反「partial results kept」的設計意圖。

**修復實作：** 移除 `break`，改成**一律 drain 所有 future**。cancel 一旦設定，未開始的 task 在
`_fine_align_image` 開頭檢查 `cancel_is_set()` 立即回 None、進行中的 walk 經 `cancel_cb` 快速 bail
（仍保持即時反應），但已完成 future 仍會 emit 結果 → 「保留已完成結果」對 workers>1 變確定性。
逐 future 用 `try/except oasis_random.WalkCancelled` 包 `fut.result()`，bail 的 walk 視為無結果、
不中斷整個迴圈。

**測試：** `py_compile` 過。沙箱無 PyQt6 → cancel 路徑互動待 user 本地驗（非 cancel 路徑等價測試不受影響）。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/dazzling-cori-5T7XE`（PR #5）

## [2026-05-25] [F7] 實作 M1–M4：Batch 工作區 + inline 進度 + 進度條質感（待本地驗收）

**變更類型：** 功能（UI/UX，運算不變）

**內容：** 依核准的 F7 plan（規劃期間 user 反映「Batch 放 View 那排怪」，Q&A 改為**動作進入+返回鈕**，
不放 segmented）實作四個 milestone：
- **M1 進度條質感**：`_AnimatedBar` 升級——橘→深橘垂直漸層 + 軟外發光（外擴低 alpha rounded rect）+
  determinate 時條內置中 % 數字（先深色全畫、再白色 clip 到填充，兩種底都可讀）+ bar 加高 14→20px、
  更圓潤、保留掃光帶。API 不變 → 全 app 進度條（OASIS 載入/overlay/ROI）同步變精緻。
- **M2 `BatchResultsPanel`**：新 QWidget，把舊 `FineAlignResultsDialog` 的 summary/only-low 篩選/sortable
  表/`_ScoreHistogram`/`_ResidualScatter`/median 鈕搬入；對外 `set_rows(rows,threshold)`；頂部 inline 進度區
  （`_AnimatedBar`+spinner+done/total/%/Elapsed/ETA+Cancel，閒置隱藏）；signals image_activated/
  apply_median_requested/cancel_requested/back_requested。
- **M3 Batch 工作區**：`_center_split` 變 [canvas, batch_panel, sem_viewer]；新增 `_enter_batch_workspace()`
  （記 `_prev_view_mode`、隱藏 canvas/minimap、左結果≈55%/右 SEM≈45%）與 `_exit_batch_workspace()`；
  `_set_view_mode` 開頭一律隱藏 batch_panel（點任一 View 鈕即離開）+ gds 模式 setSizes 改 3 值；
  點結果列 → `_on_sem_image_selected` 就地換右側 overlay、不離開工作區。
- **M4 批次接線改 inline**：`_on_run_fine_align_all` 改 `_enter_batch_workspace()`+`start_progress()`，
  **不再開 modal `LoadProgressDialog`**；`_on_fa_progress`→panel.set_progress、`_on_fa_result`→更新
  `_refined`/badge 並 `_refresh_batch_panel()`（streaming 重填）；finished/cancelled/failed→`end_progress`
  + 重填、保留部分結果；cancel 由 panel 按鈕經 `_on_fa_cancel_clicked` 直接 `worker.cancel()`（threading.Event
  即時）；「Results…」鈕（`_open_fa_results`）改為進工作區+重填。移除已無用的 `FineAlignResultsDialog`
  類別與 `_fa_progress`/`_fa_results_dlg` 屬性。**OASIS 載入/overlay 匯出仍用原 modal 進度。**

**不動：** F6 批次運算與結果值、fine-align 符號、SemViewer 折疊、CE early-stop（§7 不變式）；median→δ 機制。

**測試：** `py_compile` 全過。沙箱無 PyQt6/numpy/cv2 → GUI/外觀/互動（進度條漸層發光%、Run all/Results…
進工作區、inline 進度+ETA、streaming 表、即時 cancel、點列就地換 overlay、← 回對位、四 view 切換）
待 user 本地驗收；`pytest tests/test_gds_align_f5.py`（純函式）不受影響、待本地跑。

**影響檔案：** `glas/app/gds_align_tool.py`、`docs/plans/F7-batch-workspace-ui.md`、`CLAUDE.md`。

**Branch：** `claude/dazzling-cori-5T7XE`

## [2026-05-25] [F7] 規劃：批次對位工作區（Batch view-mode + inline 進度 + 進度條質感）（待核准）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 提出批次對位的 UI/UX 想改善（批次結果是否該有專屬畫面、進度條不夠質感）。探索現有
view-mode（`_VIEW_MODES`+`QButtonGroup`+`_set_view_mode` 切 `_center_split`）、`FineAlignResultsDialog`、
`_AnimatedBar`/`LoadProgressDialog`、批次接線後，用 AskUserQuestion 收斂三個岔路：(1) 批次結果放
**第四個 view-mode「Batch」**（中央左=結果表/直方圖/散點、右=SEM overlay、點列就地換 overlay）、
(2) 批次跑時 **inline 進度+結果 streaming**（取代 modal）、(3) 進度條 **漸層+發光+條內 %**。產出
`docs/plans/F7-batch-workspace-ui.md`（5 milestone：M1 `_AnimatedBar` 質感、M2 抽 `BatchResultsPanel`、
M3 加 Batch view-mode、M4 批次接線改 inline、M5 收尾）。強調純 UI 重新安置+視覺，**不動 F6 批次運算
與 §7 不變式**。§8 註冊 [F7]。**待 user 核准後才開工。**

**測試：** 無（純 plan）。

**影響檔案：** `docs/plans/F7-batch-workspace-ui.md`、`CLAUDE.md`（§8）、`SESSION_LOG.md`。

**Branch：** `claude/dazzling-cori-5T7XE`

## [2026-05-25] [F6] 實作 M1–M3：mmap 讀取 + 單一 map 共享 + thread-pool 批次（待本地驗收）

**變更類型：** 功能（效能加速，行為不變）

**動機現象：** ROI 模式開大檔整檔 slurp 進 RAM（`oasis_streamer.py` `OasisStream`）、`RandomAccessReader`
開檔 slurp 兩次、批次 fine-align（`FineAlignAllWorker`）刻意單執行緒。plan F6 已核准。

**修復實作：**
- **M1 mmap-backed OasisStream**：`OasisStream(base=None, *, use_mmap=False, shared_buf=None)`，`use_mmap`
  且 base 有 fileno → `mmap.mmap(ACCESS_READ)`；BytesIO/平台/空檔 → fallback `base.read()`（行為不變）。
  `close()` 釋放 mmap + 持有的 base。`OasisReader`/`scan_cell_offsets` 加 `use_mmap`（預設 False，bulk
  decode 路徑維持 slurp）；`RandomAccessReader` 內部走 mmap。
- **M2 單一 map 共享**：`shared_buf` 路徑讓 `OasisStream/OasisReader/scan_cell_offsets` 包外部擁有的
  buffer（各自 `_pos`，close 不關共享 map）；`RandomAccessReader.__init__` 建一個 owning mmap，`_buf`
  同時給 persistent reader 與 scan → 檔案只 map 一次、offset index 只算一次。新增
  `RandomAccessReader.close()` / `__enter__/__exit__`（先關 wrapper 再關 owning map，idempotent）。
- **M3 thread-pool 批次**：抽出純函式 `_fine_align_image(...)`；`FineAlignAllWorker.run()` 改
  `ThreadPoolExecutor(max_workers=min(cpu_count,8))`，每 worker thread 經 `threading.local` 用
  `RandomAccessReader.clone()` 取私有 reader（私有 _memo/cursor；mmap 由 OS 共享實體頁，不耗 N× RAM），
  零共享可變狀態 → 結果與循序逐值相同。signal 由 run() 單一 thread 在 future 完成時 emit；cancel 沿用
  `threading.Event`（task 起點 + walk cancel_cb 讀 is_set，cancel 後保留已完成結果）。
  **cv2 執行緒設定維持預設不動**（避免改變 score 數值，保證 golden 等價）。

**測試：** 新增 `tests/test_accel_equivalence.py`（mmap↔slurp 的 read/iter_records/scan 等價、共享map↔
獨立scan 等價、`RandomAccessReader` close idempotent、循序↔4-worker pool 每張 result tuple 等價）。
**沙箱實跑 numpy-free 等價檢查全通過**（OasisStream/iter_records/scan/shared-map）；numpy/cv2/PyQt6-gated
項（load_cell 幾何、批次並行）與實機效能、GUI 批次加速、mmap 記憶體下降待 user 本地驗收。py_compile 全過。

**影響檔案：** `glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、`glas/app/gds_align_tool.py`、
`tests/test_accel_equivalence.py`、`docs/plans/F6-readwalk-batch-accel.md`。

**Branch：** `claude/dazzling-cori-5T7XE`

---

## [2026-05-25] [F6] 規劃：OAS 讀取 + 批次 fine-align 加速（待核准）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 要求在「功能完全不變」前提下找 OAS 讀取與批次 fine-align 的加速點。探索熱路徑後
（`OasisStream` slurp `oasis_streamer.py:214`、`RandomAccessReader` 雙重 slurp `oasis_random.py:215`、
`FineAlignAllWorker` 刻意單執行緒 `gds_align_tool.py:1116`）與 user 用 AskUserQuestion 收斂三個岔路：
(1) 批次平行化用 **thread pool**（per-thread reader + 共享 mmap；cv2 釋放 GIL）、(2) worker 數
**自動**（cpu_count 上限 8）、(3) mmap **只用在 ROI/隨機存取路徑**（bulk decode 維持 slurp）。
產出 `docs/plans/F6-readwalk-batch-accel.md`（4 milestone：M1 mmap-backed OasisStream + fallback、
M2 單一 map 共享去雙重 slurp、M3 thread-pool 批次、M4 等價總驗收），核心驗收條件為 **golden-output
逐 byte／逐數值等價測試**（mmap↔slurp 幾何相等、共享map↔獨立scan index 相等、循序↔並行批次結果
相等），強調不改演算法與 §7 不變式。§8 註冊 [F6]。**待 user 核准後才開工。**

**測試：** 無（純 plan）。

**影響檔案：** `docs/plans/F6-readwalk-batch-accel.md`、`CLAUDE.md`（§8）、`SESSION_LOG.md`。

**Branch：** `claude/dazzling-cori-5T7XE`

---

## [2026-05-25] [F5] PR#4 review fix：非 ok 批次狀態清掉舊 refined offset

**變更類型：** bug fix

**動機現象：** PR#4 review（P1）指出 `_on_fa_result` 對每張影像更新 `_fa_meta`，但只在
`status == "ok"` 時寫 `_refined`。若某影像先前對位成功、後續 run 回 `flat`/`missing-file`/
`no-scale`/`no-coords`，舊 offset 仍留在 `_refined`，jump/overlay/export 經 `_refined_offset()`
會繼續用過期對位渲染/匯出失敗影像。

**修復實作：** `_on_fa_result` 在非 ok 分支 `self._refined.pop(image_id, None)` 並呼叫新增的
`SemPanel.clear_score(image_id)`（移除 score badge；對本來就無座標的影像保留「no coords」標記）。

**測試：** `py_compile` 過。sandbox 無 PyQt6 → GUI 待 user 驗。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/sharp-lamport-YIk3z`（PR #4）

---

## [2026-05-25] [F5] 實作完成（M1–M6，待 user 本地驗收）

**變更類型：** 功能（新功能 + bug fix）

**內容：** fine-align 診斷 + 工作流六個 milestone 全數實作（plan 已核准）：
- **M1 殘差疊圖**：新增純函式 `overlay_outlines_on_sem(sem_gray, entries, anchor, nm_per_px)`
  （對齊 `rasterize_layer` 的 X 右 / Y-flip 慣例，cv2.polylines + numpy fallback `_draw_polyline_np`）；
  `TemplatePreviewDialog` 由 3 格擴成含「Overlay·before（coarse）/ after（coarse+refined）」共 5 格
  （grid 每列 3 格自動換行）；`_on_preview_template` 備好 before/after anchor + entries。
- **M2 批次總覽**：新增 `FineAlignResultsDialog`（可排序表格 image_id/score/dx/dy/used_radius/status、
  score 上色、「只看低於 threshold」篩選）+ 自繪 `_ScoreHistogram`（含 threshold 線）+ `_ResidualScatter`
  （原點十字 + median 標記）；純函式 `fine_align_result_rows` / `score_histogram` / `residual_median`。
  `FineAlignAllWorker.result` signal 擴成 `(image_id,dx,dy,score,used_r,status)`，每張影像都回報
  狀態（ok/no-coords/missing-file/no-scale/flat）；新增 `self._fa_meta`；單張路徑也存 used_r/status。
  `Run all` 完成後自動開窗，Fine Align 面板加「Results…」可重開；雙擊列跳到該影像。
- **M3 中位殘差→δ**：對話框「Apply median residual to origin δ」鈕 → `_on_apply_median_residual`
  把 ok/low-score 影像的 median(dx,dy) 加進 `_origin_dx/dy`（與 refined 同號、沿用 Set Offset 機制），
  不清空既有 `_refined`，提示重跑。
- **M4 setup 持久化 + 命名**：`BG grey`→`Background GL`、每列 `FG`→`Foreground GL`（不動 `values()` key）；
  新增 `self._fa_setup`，`_capture_fa_setup`（載新 ROI 前快照 visible/colour/opacity/POI/FG）+
  `_apply_fa_setup`（`_on_roi_finished` recompute 後依 layer key 還原、缺層略過、不自動 run）；
  `LayerPanel.check_pois` / `FineAlignPanel.set_fgs` 還原 POI 勾選與 FG。
- **M5 cancel 即時生效 + ETA**：`FineAlignAllWorker` cancel 改 `threading.Event`，`cancel_requested`
  以 `DirectConnection` 在 GUI 執行緒直接 set（修「按終止仍一直跑」根因）；`LoadProgressDialog` 先前
  已加自繪 `_AnimatedBar` + `set_progress`（done/total/%/elapsed/ETA）；cancel 後保留部分結果。
- **M6 overlay+image 匯出**：`AlignmentExportDialog` 加「Raw SEM PNG / Aligned overlay PNG」勾選 +
  `selected()` 多回傳兩旗標；新增 `OverlayExportWorker`（逐張輸出 `<id>_raw.png` / `<id>_overlay.png`
  到選定資料夾，overlay 用 M1 helper 在 coarse+refined anchor 描 POI 輪廓、RGB→BGR 翻轉再 imwrite）+
  manifest（`overlay_manifest.csv/.json`，schema `mmh-gds-overlay-v1`，image_id join）；`_safe_name`。

**測試：** `py_compile` 全過。新增 `tests/test_gds_align_f5.py`（score_histogram / residual_median /
fine_align_result_rows status / _safe_name / overlay_outlines_on_sem 落點 / manifest 寫出）。
**sandbox 無 PyQt6/numpy/cv2 → pytest 與 GUI 未跑**，全部 GUI/互動待 user 本地驗收
（preview before/after、Run all 進度條/ETA、即時 cancel、Results 表/直方圖/散點、median→δ、
切 DID 保留 setup 按一次即可、overlay PNG + manifest 匯出）。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_f5.py`、
`docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/sharp-lamport-YIk3z`

## [2026-05-25] [F5-M5部分] Batch 進度條動態化 + ETA

**變更類型：** 功能（UI）

**動機現象：** batch（Run all）進度對話框只有 Braille spinner + elapsed，缺乏「正在做事」的
實感與剩餘時間。user 要求進度條精美、有動態感。

**修復實作：** `glas/app/gds_align_tool.py` 新增自繪 `_AnimatedBar`（QWidget）：圓角軌道 +
橘色填充，填充上有一條淺色光澤帶隨 `advance()`（每 120ms tick）掃過 → 動態感；支援
determinate（依 fraction 填充）與 indeterminate（滑塊 ping-pong，給 ROI 載入等未知總量階段）。
全自繪不受 app QSS 影響（解掉先前避用 `QProgressBar` 的 chunk 動畫被壓平問題）。
`LoadProgressDialog` 嵌入該 bar + 新增 `set_progress(done,total)`：切 determinate 並由
`_refresh_detail` 顯示「done/total · NN% · Elapsed m:ss · ETA m:ss」（ETA 用 elapsed/done×剩餘）。
`_on_fa_progress` 改呼叫 `set_progress`，title 顯示當前 image_id。對應 F5 M5 的 ETA checkbox 勾掉。

**注意：** M5 其餘兩項（threading.Event 即時 cancel、cancel 後 results 預覽）尚未做。

**測試：** `py_compile` 過。沙箱無 PyQt6 → GUI 動畫/ETA 待 user 本地驗。

**影響檔案：** `glas/app/gds_align_tool.py`、`docs/plans/F5-finealign-diagnostics.md`。

**Branch：** `claude/sharp-lamport-YIk3z`

## [2026-05-25] [F5] 規劃再擴充：納入 setup 持久化 / cancel 修復 / overlay 匯出 / 命名（6 milestone，待核准）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 回饋四項 fine-align 工作流痛點，探索後收斂並擴充 F5：
(#1) `BG grey`→`Background GL`、`FG`→`Foreground GL`（命名）；
(#2) 切 DID 走 `set_document(新doc)` 用全新 `LayerEntry` 重建 → POI 勾選/可見性/顏色遺失，
決策「記住 setup、新 doc 自動重套、user 手動按一次 Run fine align」；
(#3) batch cancel 跑不停根因 = worker 緊迴圈內 `cancel()` 是 queued slot 永不執行，
決策改 `threading.Event` 直接旗標 + 進度對話框 ETA + cancel 後保留已完成結果；
(#4) overlay+image 匯出，決策「可勾選 raw / 對位後 overlay PNG / 兩者 + manifest（image_id join）」
供 MMH 串接，複用 M1 overlay helper。plan 由 3 → 6 milestone（新增 M4 持久化+命名、M5 cancel/ETA、
M6 overlay 匯出）。§8 條目同步。**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/sharp-lamport-YIk3z`

---

## [2026-05-25] [F5] 規劃擴充：診斷範圍收斂為 3 milestone（待核准）

**變更類型：** 文件（plan，尚未動工）

**內容：** 探索 fine-align 子系統後與 user 收斂 F5 範圍。除原 M1 殘差疊圖、M2 批次結果表，
納入 C1 殘差散點圖、C5 score 直方圖（並入 M2）、C3 狀態/失敗原因 + C4 used radius（表格欄）、
C2 中位殘差→origin δ 一鍵套用（新 M3）。改寫 `docs/plans/F5-finealign-diagnostics.md`
為 3 milestone；§8 F5 條目同步。**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] 完成 [F4] + [F1]：user 驗收通過，§8 結案

**變更類型：** 文件（任務結案，無程式碼變更）

**內容：** user 本地驗收 [F4]（Boolean 食譜化重算 + 巢狀 + 編輯 + 對話框內嵌預覽 + 方向性
W/H morphology + coordinate setup 版面修復）與 [F1]（互動驗收：SEM↔GDS 對位 / 拖動 δ /
fine-align / 批次 Run all / 匯出 / 折疊 UX）皆 OK。從 `CLAUDE.md` §8 移除 [F4]、[F1]；
`docs/plans/F4-boolean-enhance.md` 標 done、勾完驗收 checkbox。§8 進行中僅剩 [F5]（待核准）。

**影響檔案：** `CLAUDE.md`、`docs/plans/F4-boolean-enhance.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 方向性 W/H morphology + coordinate setup 版面溢出修復

**變更類型：** 功能（語意變更）+ bug fix

**動機現象：** (1) `A > W:10` 看起來把高度也加大 —— 原本 morph 是**等向** buffer，W/H 只是
標籤、`>`/`<` 為 grow/shrink 全方向。user 要 W/H 變成**方向性**（W=寬/X、H=高/Y）。
(2) Coordinate Setup 輸入第一個值後整個面板暴寬、超出 UI。

**修復實作：**
- **方向性 W/H（`gds_boolean.py`）**：`> W:n`=只長寬、`> H:n`=只長高、`< W:n`/`< H:n`=各軸縮，
  每邊各 ±n nm（總 ±2n）。新增 `_dilate_axis`（與軸線段的 Minkowski sum：geom + 平移副本 +
  各邊掃成平行四邊形 → 對任意多邊形精確）與 `_morph_axis`（grow 用 dilate；shrink 用補集-膨脹-
  補集 erosion，需 fov_bbox）。`evaluate` 的 Morph 分支改呼叫 `_morph_axis`；parser 限制軸
  標籤僅 W/H（大小寫不拘）否則報錯。更新 `tests/test_gds_boolean.py` morph 測試（方向性
  面積 + bounds、shrink 缺 fov 報錯、非法軸標籤報錯）。對話框運算子鈕擴成 `>W: >H: <W: <H:`。
- **版面溢出（`gds_align_tool.py`）**：`CoordinateSetupPanel` 的 `_corner_lbl`/`_origin_lbl`
  無 word-wrap，輸入值後標籤文字變長（含千分位 + nm/µm），單行 QLabel 撐寬固定 300px 的 SEM
  面板而溢出。兩個 label 加 `setWordWrap(True)`。

**測試：** `py_compile` 三檔過。sandbox 無 numpy/shapely/PyQt6 → 未跑 pytest / GUI，待 user
本地 `pytest tests/test_gds_boolean.py -v` + 驗 GUI（W/H 方向、coordinate setup 不再溢出）。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/app/gds_align_tool.py`、
`tests/test_gds_boolean.py`、`CLAUDE.md`、`docs/plans/F4-boolean-enhance.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 修復 edit 閃退 + 對話框內嵌預覽

**變更類型：** bug fix + 功能

**動機現象：** (1) 編輯 expression layer 時偶發閃退（終端機有 Error）。(2) 預覽新 Boolean
layer 要回主視窗才看得到，且 modal 對話框擋住主畫面 canvas。

**修復實作：**
- **閃退根因**：edit/delete 由 `_LayerRow` 的按鈕點擊 / 雙擊 signal 觸發，handler 內同步
  開 modal 對話框（`exec()`）；對話框關閉後 `_recompute_recipes()` → `set_document()` 會
  刪掉那個 row widget，待 `exec()` 返回時控制流回到「已被刪除的 C++ row 物件」的事件
  handler → use-after-free，PyQt6 直接 abort。改為 `_on_edit_recipe`/`_on_delete_recipe`/
  `_on_add_expression` 一律用 `QTimer.singleShot(0, …)` 延遲，等 row 的 handler 完全 unwind
  後再開對話框，避免在 row 事件處理中刪除自身。
- **內嵌預覽**：`ExpressionLayerDialog` 新增 `_ExprPreview` 迷你 canvas（fit-to-view），按
  Preview 直接在對話框內渲染結果（filled highlight）疊在綁定的 raw layer（細外框）上，
  對話框不關、不再動主視窗 doc/canvas；確認無誤再按 **Save**（OK 鈕改名 Save）儲存。
  `_preview_expression` 改回傳 `(ok, msg, data)` 且**不再 mutate 主 doc**（移除原本塞臨時層
  + cancel 時 recompute 的迂迴）。

**測試：** `py_compile` 過。sandbox 無 PyQt6 → GUI 待 user 本地驗（編輯不再閃退、Preview 在
對話框內顯示、Save 才存）。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 實作：Boolean 強化（食譜化重算 + 巢狀 + 編輯 + 對話框重設計）

**變更類型：** 功能（新功能 + 重構）

**動機現象：** synthetic（Boolean 表達式）layer 只算一次、載新 ROI（跳 defect）就遺失、
無法編輯、binding 只能綁 raw layer（無法巢狀）。

**修復實作：**
- **引擎（`gds_boolean.py`）**：新增 `normalize_binding`（舊 `(layer,datatype)` →
  `("raw",l,d)`、支援 `("ref",name)`）、`recipe_dependency_order`（拓樸排序 + 循環/未知
  ref 偵測，純函式）、`resolve_expression`（raw/recipe provider 抽象 + 遞迴解析巢狀 ref +
  cache memoize + 循環防護）。core 維持無 Qt。
- **app（`gds_align_tool.py`）**：MainWindow 新增 `self._recipes` 作 synthetic 層唯一事實
  來源；`_recompute_recipes()` 在 `_on_roi_finished`（每次載 ROI/跳 defect 的 FOV）與
  cache 還原時自動重算所有 recipe → synthetic 層跟著 defect 走。`_eval_expression` 改用
  `resolve_expression`（display 路徑）；`poi_polys_for_roi`（F3 batch）同步支援巢狀 +
  recipe 快照。`_LayerRow` 加編輯/刪除按鈕（雙擊=編輯），刪除被其他 recipe 引用時擋下。
  `ExpressionLayerDialog` 重設計：layer/synthetic chip + 運算子按鈕插入 token、即時語法
  檢查（disable OK + inline 錯誤）、binding 下拉含 raw + ref、編輯預填。cache sidecar
  改由 recipe 序列化/還原（tagged binding，含舊格式遷移）；開新 OASIS(ROI)/載新 cache 會
  清掉前一檔的 recipe，ROI reload 則保留。
- **tests**：`tests/test_gds_boolean.py` 加 `normalize_binding` / `recipe_dependency_order`
  / `resolve_expression`（巢狀、循環、未知 ref、舊格式）測試。

**測試：** `py_compile` 三檔皆過。**sandbox 無 numpy/shapely → 未跑 pytest；GUI 互動未驗。**
待 user 本地 `pytest tests/test_gds_boolean.py -v` + GUI 驗收（定義 L0/L1 巢狀、跳 defect
自動重算、編輯連動、刪除、新對話框）。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/app/gds_align_tool.py`、
`tests/test_gds_boolean.py`、`docs/plans/F4-boolean-enhance.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 規劃：Boolean 強化（食譜化重算 + 巢狀 + 編輯 + 對話框重設計）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 調整優先序：F4 改做 Boolean 強化、原 fine align 診斷改排 F5。經探索確認現況
（synthetic layer 只算一次、ROI reload 即遺失、無法編輯、無法巢狀），Q&A 收斂為四項：
食譜化每 FOV 自動重算、巢狀引用 synthetic、編輯/刪除、表達式對話框完整重設計。
新增 `docs/plans/F4-boolean-enhance.md`（3 milestone）；§8 更新 [F4] 指向新 plan、
[F5] = fine align 診斷。**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F4-boolean-enhance.md`、`docs/plans/F5-finealign-diagnostics.md`
（renumber）、`CLAUDE.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F4] 規劃：Fine align 診斷（殘差疊圖 + 批次結果總覽）

**變更類型：** 文件（plan，尚未動工）

**內容：** 經 Q&A 收斂 fine align 強化方向＝結果可視化/診斷，具體交付兩項：殘差疊圖
overlay（對位前/後輪廓畫在 SEM）與批次結果總覽（可排序/篩選/點列跳轉的表格）。
新增 `docs/plans/F4-finealign-diagnostics.md`（2 milestone），於 CLAUDE.md §8 註冊 [F4]。
**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F4-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] 後續：toolbar 不裁切、layer name catch-all 修正、移除透明度 slider

**變更類型：** UI 修正 + bug fix（PR #3 後續 user 回饋 1/2/3）

**動機/現象：**
1. 視窗非最大化時中間 toolbar 按鈕（Open OASIS / Load Cache / Export Cache…）文字被裁切。
2. 讀 OASIS LAYERNAME 時所有 layer 都顯示同一個名字（NW）。
3. Layers 列的透明度搖桿沒實際用途，要移除。

**修復：**
1. `_build_toolbar` 結尾把每顆按鈕 `setMinimumWidth(sizeHint().width())`（在設粗體後），
   並新增 `_wrap_toolbar()` 用橫向 `QScrollArea`（v-scroll off、h-scroll as-needed、
   高度 = bar + scrollbar extent）包住，窄視窗改為橫向捲動而非裁字。
2. `resolve_layer_name` 改為「最具體（最窄 layer 區間，其次 datatype 區間）優先」，
   並跳過 `(0, INF)` 全層 catch-all（placeholder 名稱不再蓋到每一層）。
   注意：LAYERNAME 表若在檔尾（scan_cell_offsets 於首個 CELL 即停）仍可能收不到，
   屆時退回 L/D；若仍有問題需後續加讀檔尾 name table。
3. `_LayerRow` 移除 opacity slider/`_pct`/`_on_opacity`（`LayerEntry.opacity` 保留，
   渲染用預設值）；移除未用的 `QSlider` import；hint 文字更新。

**測試：** py_compile 全通過；更新 `test_oasis_random.py::TestResolveLayerName`（catch-all
跳過、不蓋其他層）、移除 `test_gds_align_m6.py::test_slider_sets_opacity_and_emits`。
sandbox 無 PyQt6/numpy/cv2，toolbar 捲動/透明度移除等 GUI 行為待 user 本地驗收。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/core/oasis_random.py`、
`tests/test_oasis_random.py`、`tests/test_gds_align_m6.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] 修正：多 POI 選取以 row 狀態重建，避免 ndarray __eq__ 報錯

**變更類型：** Bug fix（PR #3 review，P1）

**動機/現象：** `LayerEntry` 是含 NumPy 陣列（polygons/bboxes）的 dataclass，
`_on_poi_toggled` 用 `entry not in self._poi_entries` / `.remove(entry)` 會觸發
dataclass `__eq__` 對陣列比較，實際 ROI 資料下選/取消第二個 POI 會丟
`ValueError: truth value of an array ... is ambiguous`，破壞多 POI 互動。

**修復（`glas/app/gds_align_tool.py`）：** 移除多餘且有 bug 的 append/remove 區塊，
直接由各 row 的 `poi_btn.isChecked()` 以 panel 順序重建 `_poi_entries`（原本下方
本就有此重建，append/remove 為冗餘）。不再對 LayerEntry 做相等比較。

**測試：** py_compile 通過；既有 `test_gds_align_m4b.py::test_multi_select_and_run_enabled`
覆蓋多選 toggle 路徑。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] M3–M5：多 POI fine align（合成樣板）＋ POI 鈕／預覽彈窗

**變更類型：** 功能（fine align 多 POI / UI）

**動機/現象：** 原 fine align 僅支援單一 POI，真實半導體 SEM 影像含多層結構。改為可選
多個 POI layer，各自輸入 FG gray，合成一張類 SEM 樣板做單次 matchTemplate，並能彈窗
並排 SEM/GDS/Template 做視覺分析。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- 核心：新增 `render_composite_template(poi_layers,...)`（各層 mask 以各自 fg 疊到共用 bg、
  一次 blur）；`render_poi_template` 改為 n=1 thin wrapper（行為不變）。
- `LayerPanel`：POI 改多選，`poi_changed`→`pois_changed(list)`，`_on_poi_toggled` 去互斥、
  以 panel 順序重組；`_LayerRow` POI 鈕放大改「POI」＋`_POI_BTN_QSS`（解決全白看不到）。
- `FineAlignPanel`：移除單一 FG，改 `_poi_box` 每 POI 一列（名稱＋FG spin），`set_pois()`
  保留既有值，新增 `poi_fgs()`；BG/blur/radius/threshold 維持全局；加「Preview template…」。
- `MainWindow`：`_on_pois_changed` / `_poi_layers` / `_build_template` / `_coarse_anchor` /
  `_poi_specs` 全多 POI；`FineAlignAllWorker` 改吃 `poi_specs=[(spec,fg)]`；匯出 `poi_layer`
  多層串接；新增 `TemplatePreviewDialog` + `_on_preview_template` + `_render_gds_preview`。

**測試：** py_compile 全通過；更新 `test_gds_align_m4b.py`（多選、`_poi_specs`、worker
建構子、composite）、`test_gds_align_m7.py`（`set_pois`）。sandbox 無 PyQt6/numpy/cv2，
GUI／matchTemplate 互動驗收待 user 本地。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_m4b.py`、
`tests/test_gds_align_m7.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-25] [F3] M1+M2：版面裁切/最小尺寸修正＋OASIS 圖層名稱顯示

**變更類型：** 功能（UI 修正 + 圖層名稱）

**動機/現象：** (M1) 視窗縮放時版面擠迫、Coordinate Setup 展開時欄位/按鈕被裁切、
對話框最小寬可能撐破小螢幕。(M2) 左側 layer 只顯示 `L17/D0`，user 想看名稱。

**修復/實作：**
- M1（`collapsible.py`）：`CollapsibleSection` body layout 加
  `setSizeConstraint(SetMinimumSize)`，展開段落不再被下方 list 擠壓裁切。
- M1（`gds_align_tool.py`）：新增 `_screen_avail()` / `_capped_min_width()`，三個對話框
  最小寬夾到螢幕；`MainWindow.setMinimumSize(min(940,avw),min(600,avh))`。
- M2（`oasis_streamer.py`）：`scan_cell_offsets` 同輪收集 LAYERNAME → `layernames`。
- M2（`oasis_random.py`）：`resolve_layer_name()` 純函式 + `RandomAccessReader.layer_display_name()`。
- M2（`gds_align_tool.py`）：`LayerEntry.display_name`（display-only，不入 LayerKey identity）；
  `_roi_entry` 填名；`_LayerRow` 顯示 `NAME (L17/D0) · n`，無名稱退回 `L17/D0 · n`。

**測試：** py_compile 全通過；新增 `tests/test_oasis_random.py::TestResolveLayerName` 5 項
（純函式，已於 sandbox 以等價邏輯驗過）。GUI 版面修正待 user 本地確認。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/app/collapsible.py`、
`glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、`tests/test_oasis_random.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-25] 規劃 [F3]：多 POI Fine Align ＋ UI 優化（plan only，尚未動程式）

**變更類型：** 規劃（plan 文件 + §8 任務註冊）

**動機/現象：** user 一次提出 6 項（4 改動 + 2 問題）。問題已於對話回答：(1) Fine
Align 的 FG/BG grey 是合成樣板的前景/背景灰階；(2) View mode 的 minimap 與 SEM mode
差別在 minimap 只是 SEM 滿版再浮一個 defect 色點小地圖、不畫 OASIS 幾何。4 項改動經
`AskUserQuestion` 收斂：維持側欄固定寬只修擠迫/裁切、Layer 列用 OASIS LAYERNAME 顯示
名稱、POI 鈕放大改「POI」字樣、Fine Align 改多 POI（每 POI 自己 FG gray、合成一張樣板、
彈窗並排 SEM/GDS/Template）。

**實作：** 新增 `docs/plans/F3-multipoi-and-ui.md`（5 milestone：M1 版面/裁切、
M2 LAYERNAME 名稱、M3 多 POI 核心、M4 POI 鈕+多選 UI、M5 視覺化彈窗），於 CLAUDE.md
§8「進行中」註冊 [F3]。**尚未修改任何程式碼**，待 user 核准 plan 後從 M1 開工。

**測試：** 無（純規劃）。

**影響檔案：** `docs/plans/F3-multipoi-and-ui.md`、`CLAUDE.md`、`SESSION_LOG.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-24] UI batch 1：Load SEM 主色按鈕 / Coord 折疊 badge / image list badge

**變更類型：** 功能（UI / UX）

**動機/現象：** 三項視覺強化：(1) `Load SEM…` 按鈕視覺權重不足，與 `Open OASIS…`
不對等；(2) Coordinate Setup 收起後看不出 FOV 是否已設定；(3) image list 每列無法
一眼看出對位狀態（有無座標 / fine-align 分數）。

**修復/實作：**
- **Fix 1（`gds_align_tool.py`）**：新增 `_LOAD_SEM_BTN_QSS`（橘底白字 + hover 深橘 +
  menu-indicator），`SemPanel` 的 Load SEM 按鈕存為 `self.load_sem_btn` 並套用該 QSS。
  （按鈕實際在 `SemPanel` 而非 toolbar。）
- **Fix 2（`collapsible.py` + `gds_align_tool.py`）**：`CollapsibleSection` header 加
  `self._badge` QLabel（右對齊，no-trailing 路徑也包一層 row）+ `set_badge(text,fg,bg)`
  + `_update_badge_visibility()`（僅在收起且有文字時顯示）；`SemPanel.update_coord_badge()`
  讀 `fov_w_nm`/`fov_w`（皆 nm，/1000→µm）顯示綠色 `FOV W × H` 或琥珀 `not set`，
  於 `__init__` 末 seed 一次、`MainWindow._on_coord_changed` 每次更新。
- **Fix 3（`gds_align_tool.py`）**：新增 `_ImageListDelegate(QStyledItemDelegate)`，在右
  邊距以 UserRole+2/+3/+4 資料畫圓角 badge；`set_images` 對無座標列調暗文字 + 設
  `no coords` 灰 badge；`set_score` 改設分數 badge（綠/琥珀/紅，門檻 `>=t` / `>=0.7t` /
  else），不再 inline 改文字。

**測試：** `py_compile` 兩檔通過；更新 `test_gds_align_m4b.py::test_end_to_end`（改驗 badge
資料角色而非 `[score]` 文字）；`test_gds_align_m7.py` 新增 7 項（accent QSS / coord badge
not-set / set / hidden-when-expanded / no-coords badge / score green / score red）。完整
`pytest tests/` 442 passed。offscreen render-grab 煙霧測試：視窗正常顯示、Load SEM 橘色、
badge 正確。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/app/collapsible.py`、
`tests/test_gds_align_m7.py`、`tests/test_gds_align_m4b.py`。

**Branch：** `claude/jolly-babbage-8nwED`（PR #2）

## [2026-05-24] LAYERS empty hint 置中微調

**變更類型：** UI 微調

**動機/現象：** `LayerPanel._show_empty_hint()` 的三個 item 用 `AlignCenter`，
改為 `AlignHCenter` 明確水平置中（QListWidget 無 list-wide setAlignment API，
per-item setTextAlignment 即正確機制）。`_group()` 橘色標籤上一輪已完成，本次未動。

**修復/實作（`glas/app/gds_align_tool.py`）：** icon/title/hint 三 item 的
`setTextAlignment` 由 `Qt.AlignmentFlag.AlignCenter` → `AlignHCenter`。

**測試：** `python3 -m py_compile` 通過；`pytest tests/test_gds_align_m6.py
tests/test_gds_align_m7.py` 59 passed。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/jolly-babbage-8nwED`（PR #2）

## [2026-05-24] GLAS UI 五項修正（依 docs/glas_ui_fixes.md）

**變更類型：** 功能（UI / UX 微調）

**動機/現象：** 依 `docs/glas_ui_fixes.md` 修正五個 UI 問題：右欄 Coordinate Setup
預設展開把 image list 擠掉、左欄 LAYERS 空白引導視覺太輕、Set/Clear Offset 放在中央
視圖下方定位不清、toolbar group label 對比不足、中央 empty state 與 guidance strip
文字重複。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- 問題1：`SemPanel` 的 Coordinate Setup `_wrap_section(..., collapsed=True)`（原 False）；
  `MainWindow.__init__` 加 `self._coord_collapsed_once = True`，使自動收起邏輯不再干預
  （預設已收起，user 再展開即固定）。
- 問題2：`LayerPanel._show_empty_hint()` 由單行小字改為圖示 + 主文「Open an OASIS」+
  次文「toolbar → Open OASIS…」三層置中結構。
- 問題3：Set/Clear Offset 由中央 `center_layout` 移入 `SemPanel`（image list 下方、
  Load GDS ROI 上方），改名 `self.sem_panel.set_offset_btn/clear_offset_btn`，
  signal 在 `MainWindow.__init__` 重新接線；原 `self._set_offset_btn/_clear_offset_btn`
  區塊整段刪除（無其他 setEnabled 引用）。
- 問題4：`_build_toolbar` 的 `_group()` label 改用 `_TK_ACCENT_DK` 色、letter-spacing
  1px、padding；FILE group 前加 `h.addSpacing(4)`。
- 問題5：`SemViewer._draw_empty_state()` 三步驟提示改為「Follow the steps above to get
  started」，由 guidance strip 負責引導。

**測試：** `python3 -m py_compile` 通過；同步更新 4 個測試的舊行為斷言
（`test_gds_align_m6.py::test_set_document_none_clears`、`test_gds_align_m7.py` 的
`test_initial_collapse_state` / `test_no_collapse_without_valid_fov` / `test_layers_empty_hint`），
`pytest tests/test_gds_align_m6.py tests/test_gds_align_m7.py` 59 passed，
完整 `pytest tests/` 435 passed。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_m6.py`、
`tests/test_gds_align_m7.py`。

**Branch：** `claude/jolly-babbage-8nwED`

## [2026-05-24] GLAS 品牌元素整合（icon / wordmark / About）

**變更類型：** 功能（UI / branding）

**動機/現象：** 應用程式仍沿用舊名 "GDS Align Tool"，缺視窗 icon、wordmark 與品牌化
About 對話框。依 `docs/IMPLEMENT.md` 將五項品牌元素整合進 app。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- import：QtGui import 補上 `QIcon`。
- `main()`：設定 `setApplicationName/DisplayName/Version/OrganizationName("GLAS"...)`，
  並以 `icons/glas_icon_256.svg` 設 `app.setWindowIcon`（所有視窗共用）。
- `MainWindow.__init__()`：視窗標題 "GDS Align Tool" → "GLAS"，並以
  `icons/glas_icon_32.svg` 設 titlebar icon。
- `_build_toolbar()`：toolbar 最左側插入 `icons/glas_wordmark.svg` wordmark（高度 28px）
  + VLine 分隔線（沿用既有 `_divider()` helper）。
- `_show_about()`：由 `QMessageBox.information` 升級為自繪 `QDialog`（128 icon + 大字 GLAS
  + subtitle + 版本 + 說明 + OK 按鈕）。
- 四個 SVG（256/128/32/wordmark）此 session 前已置於 `glas/app/icons/`。

**測試：** `python3 -m py_compile glas/app/gds_align_tool.py` 通過；sandbox 無 PyQt6
無法實際啟動 GUI 驗收（taskbar icon / wordmark 顯示 / About 對話框待 user 本地確認）。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/determined-einstein-Bfo0G`

## [2026-05-24] GLAS 專案自 MMH 抽離成立

**變更類型：** 專案建立 / 重構（抽離）

**動機/現象：** GDS Align Tool 原藏在 MMH 專案 `tools/` 下（plan F2，M1–M7 全實作）。
其核心能力——大檔 OASIS streaming / random-access 解析、KLARF↔GDS 座標換算、FOV 空間查詢、
即時 Boolean 表達式引擎、SEM↔GDS overlay 對位——不只 MMH 用得到，未來其他專案也想複用。
藏在 MMH 內定位不對，故抽離成獨立 repo **GLAS（GDS-Layout Alignment for SEM）**。

**實作（自 MMH git HEAD 搬移，零行為改動）：**
- **glas/core/（無 Qt 引擎）**：`oasis_streamer` / `oasis_store` / `oasis_walker` / `oasis_random`
  / `gds_fov` / `gds_boolean` / `gds_layer_cache` + 自 MMH `src/core` 複製的 `klarf_parser`。
  core 模組原本即無 src 依賴，零修改。
- **glas/app/（PyQt6 殼）**：`gds_align_tool`（改寫 header：`from src.gui.*` soft import →
  flat `from styles/collapsible/icons`，repo-root path hack → core+app sys.path 設定；
  subprocess streamer import 指向 glas/core）、`sem_loader`（`from src.core.klarf_parser` →
  `from klarf_parser`）+ 自 MMH `src/gui` 複製的 `styles` / `collapsible` / `icons/`（無 src 依賴）。
- **import 慣例**：core/app 以扁平 sys.path 模組互相 bare-import（沿用原 `tools/` 慣例）；
  `main.py` + `conftest.py` 把 `glas/core` 與 `glas/app` 放上 sys.path。
- **規則機制移植**：`CLAUDE.md`（保留 §2 工作規則 / §6 慣例 / §8 任務 / §10 checklist 機制，
  改寫 §1/§4/§5/§7 為 GLAS 實況）、`.claude/settings.json` + `hooks/check_progress.sh` +
  `check_session_log.sh`（SessionStart 訊息改 GLAS，腳本邏輯不變）、`README.md`、本 `SESSION_LOG.md`。
- **design history**：`docs/plans/F2-gds-align-tool.md` + `F2-M1.13-parser-perf.md` + `_template.md`
  搬入保留。
- **tests**：14 個 test 檔（`test_oasis_*` / `test_gds_*` / `test_sem_loader`）+ `fixtures/sample_real.klarf` 搬入。

**測試：** sandbox 無 numpy / cv2 / shapely / PyQt6 / pytest，無法跑完整 suite；已 `py_compile`
全檔通過。完整 `pytest tests/`（~218 項應全綠，證明搬移零行為改動）待有相依的環境執行。

**接續任務：** [F1] 互動驗收（真實 SEM 對位 / 拖動 / fine-align / 批次 / 匯出 / 折疊 UX）—
這些在 MMH 抽離前即標記「待 user 本地驗證」，移到 GLAS 接續。

**影響檔案：** 整個 GLAS repo（新建）。MMH 側對應移除見 MMH SESSION_LOG 同日條目。

**Branch：** （新 repo，待 user 在 GitHub 建立後上傳）
