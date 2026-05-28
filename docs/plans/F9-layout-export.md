# [F9] Layout 匯出：raw layer + Boolean 合成 layer 寫出成 OASIS（.oas，含 ROI 裁剪）

> **狀態：** approved — in progress
> **§8 ID：** [F9]
> **建立：** 2026-05-26
> **負責 branch：** claude/adoring-cannon-oKZKo

---

## Goal & Context

**問題 / 觀察：** GLAS 幾何資料流目前「只進不出」——OASIS reader → numpy/shapely → rasterize 成
mask 做對位，最後只匯出 alignment offset（CSV/JSON），**沒有任何 layout writer**。使用者用 Boolean
引擎合成的 ROI layer（L0）與 ROI 內 walk 出來的原始 geometry 無法存成 layout 檔。公司流程統一使用
**.oas**，需要能把這些 layer 反向寫出成 OASIS 檔（KLayout / 公司工具可開）。

**想達成（成功長相）：**

1. 把**選定的原始 layer 與 Boolean 合成 layer（可多個、同一檔）**寫出成 **OASIS (.oas)**，
   KLayout 開啟正常、layer / datatype / 幾何 / 座標正確。
2. 支援**「給定 GDS 座標的特定區域」裁剪輸出**——只輸出落在指定 bbox 內（裁切後）的幾何。

**明確不在本 feature 範圍（另一題，後續討論）：** 「下游接水確認每張 SEM image 的 ROI」。該需求的
重點是輸出**對位資訊**（overlay 圖 / CSV / 或直接外接 GLAS 引擎），與「匯出 layout 幾何」是不同問題，
不在 F9 處理。

**跟現有系統的關係：** 純**新增**，不改既有運算 / 不變式。新增 Qt-free 的 `glas/core/oasis_writer.py`，
app 端加 export 動作（仿既有 `_on_export_alignment` / `_on_export_cache`）。幾何來源是 layer panel
既有 entry 的 `.polygons`（root nm 座標）。

---

## Q&A Decisions

### Q1: 匯出哪些幾何？
**選擇：** 選定的**原始 layer** + **Boolean 合成 layer**，可多個 layer 寫進同一檔。
**理由：** 兩者來源都是 layer entry 的 `.polygons`，同一 writer 一次解決。

### Q2: 格式 — OASIS vs GDSII？（user 要求深入分析）
**選擇：** **OASIS (.oas)。**
**理由：** 決定因素是**下游消費端**——公司流程統一 .oas，輸出 GDS 是否能接無法保證。寫一個沒人能接的
GDS 再簡單也沒用，所以以 .oas 為準。另核對 `oasis_streamer` 後確認自寫 OASIS writer 風險**可控且有界**：

- **validation scheme = 0（無簽章）** → 不需實作 CRC32 / checksum（`oasis_streamer.py:1510,1519` 確認 scheme 0 不讀 signature）
- **CBLOCK 壓縮為選用** → 直接寫未壓縮
- **modal variables 是優化非強制** → 每筆 record 寫明確值，不玩 modal 狀態機
- **encoder = 既有 decoder 的逆**：unsigned/signed/real/point-list decode 都已存在（`oasis_streamer.py:421/454/468/913`）
- **強自我驗證**：寫出 → 餵回 GLAS 自己的 `oasis_streamer` reader → 斷言幾何 round-trip 相等，最後 user 本地 KLayout 複驗

### Q3: 是否支援 ROI 區域裁剪輸出？
**選擇：** 是，且裁剪框由使用者**直接輸入 GDS 座標**（左下 `(x1,y1)` → 右上 `(x2,y2)`），裁整塊區域；
**不指定 ROI 時則匯出整張 GDS**（已載入的全部幾何，不裁剪）。
**理由：** 使用者要的是精確指定座標的整塊 ROI 輸出 + 全圖轉檔兩種模式；用 shapely `intersection(box(x1,y1,x2,y2))` 實作裁剪，no-crop 直接全寫。

### Q4: 矩形幾何在 OASIS 怎麼寫？
**選擇：** **axis-aligned 矩形走 RECTANGLE record（寬,高,x,y），其餘多邊形走 POLYGON。**
**理由：** 資料中矩形佔多數，RECTANGLE 編碼明顯較短、檔案更小、更貼近原生 OASIS；代價是多一個 record
encoder + 矩形偵測 + 一組測試。KLayout 開啟與 POLYGON 寫法視覺/幾何完全相同。

### Q5: 功能要不要 gating？
**選擇：** 要。此匯出功能較進階，**預設不開放**；以 App 內「開發者模式」gating——
入口在 **Help → About 對話框內，連續點該 icon 5 次**啟用（user 指定）。功能先做完，開發者模式後續實裝（M5）。
**理由：** 避免一般使用者誤用進階功能；隱藏式啟用符合 user 需求。

### Q6: 走到哪一步？
**選擇：** plan 已核准。依序 M1 → M6 開工。

---

## Milestones

> 每個 milestone 以「一個 session 可完成」為粒度切。

### M1: Core OASIS writer (`glas/core/oasis_writer.py`，Qt-free，最小合規)  [status: done]

- [x] 新模組，純標準庫（僅 struct），扁平 sys.path bare-import 慣例（§4），無 Qt / numpy / shapely 依賴（§6）。
- [x] encode 原語（既有 decode 的逆）：`encode_unsigned_int` / `encode_signed_int` / `encode_real` /
      `encode_string` / `encode_g_delta`；test 對 `oasis_streamer` 的 decode 做 round-trip。
- [x] 進入點 `write_oasis(path, layers, *, unit, cellname="TOP")` + `serialize_oasis(...)`，
      `layers = [(layer:int, datatype:int, polygons:list[(N,2) verts])]`。
- [x] 寫出最小合規 record 序列：MAGIC → START（unit、offset_flag=0、6×(0,0) 對）→ CELLNAME_IMP →
      CELL_REFNUM 0 → XYABSOLUTE → 幾何 record → END（**validation scheme = 0**）。
      **byte 格式對照測試套件黃金 fixture 逐 byte 吻合**（RECTANGLE `0x7b`、START、END `uint 0`）。
- [x] 幾何 record 分支（Q4）：`_axis_rect` 偵測 axis-aligned 矩形 → RECTANGLE（info `0x7b`）；
      非矩形 → POLYGON（info `0x3b`，point-list type 4 g-delta arbitrary form）。閉合重複頂點自動去除。
- [x] **unit 策略**：`unit` 參數由呼叫端帶入（app 會帶來源檔 START.unit）；座標保持原 DBU 不動。
      `encode_real` 整數值走 type 0/1、非整數走 type 7 double。
- [x] 邊界處理：空 / degenerate(<3 頂點) 幾何略過；座標 round 成 int；OASIS varint 無 int32 上限（不需溢位檢查）。
- [x] 沙箱獨立驗證：writer byte 輸出 == 黃金 fixture；helper（rect 偵測、real 編碼）正確。`oasis_streamer`
      round-trip 測試需 numpy（沙箱無）→ 待 user 本地 `pytest`。

### M2: 幾何蒐集 + ROI 裁剪 (`glas/core/layout_export.py`，shapely)  [status: done]

- [x] 新模組（shapely + numpy，獨立於純 stdlib 的 `oasis_writer`）：`clip_polygons` / `clip_layers` /
      `export_layers`，`crop_bbox=(x1,y1,x2,y2)`（GDS nm、角點任意順序）用 shapely `intersection(box)` 裁切。
- [x] `shapely_to_rings(geom)`：Polygon/MultiPolygon 攤平成外環列表，**丟棄內環**（O-holes 決議：匯出顯示用 `.polygons`、無洞）。
- [x] `crop_bbox=None` → 整張不裁；裁剪後空 layer 自動略過；跨界多邊形由 shapely 切邊。
- [x] 測試 `tests/test_layout_export.py`：passthrough、內部框裁切、角點任意序、全外略過、degenerate、
      holes 丟棄、MultiPolygon、drop-empty、clip→write→read 整張/裁切 round-trip。（本地跑，沙箱無 shapely/numpy）

### M3: App 匯出動作（選 layer + 可選 ROI 區域 → .oas）  [status: done, 待本地驗收]

- [x] `OasisExportDialog`（QDialog）：每個 layer entry 一列（checkbox + 輸出 layer/datatype spin）；
      synthetic 的內部 layer=-1 不可寫 OASIS → 給可編輯輸出 layer（預設 1000+）/datatype；raw 預填原值。
- [x] 裁剪區域：**四個 GDS 座標輸入框（x1 left / y1 bottom / x2 right / y2 top）**，placeholder 帶 doc bbox；
      留空 = 整張；部分填寫 / 非數字 / 零面積 → `QMessageBox.warning` 擋下不關閉。
- [x] `_on_export_oasis`：仿 `_on_export_cache` 的 `getSaveFileName`(*.oas)，呼叫 `layout_export.export_layers`；
      失敗 `QMessageBox.critical`、裁剪後全空提示、成功 `self._status_doc` 顯示路徑 + layer 數。
- [x] **unit**：用 `1000.0`（GLAS 全程把座標當 nm、1 DBU=1 nm；`GdsDocument` 未保留來源 unit，1000 與 GLAS
      的 nm 世界觀自洽，KLayout 顯示 1000 DBU/µm）。cellname 用 `doc.top_cell_name`。
- [x] **KLayout 接受度驗證通過**（2026-05-26 user 本地）：修正 END record 補滿 256 bytes（KLayout 嚴格要求）
      後，`sample_good.oas` 三 layer（17/0 RECTANGLE、25/0 POLYGON、40/1 g-delta POLYGON）KLayout 正確渲染。
- [ ] GUI Export + Debug 端到端（需載入 layout：cache .npz 或 production .oas）→ **待 user 有資料時測**（3c）。

### M4: 測試  [status: in progress]

- [x] `tests/test_oasis_writer.py`：encode 原語 round-trip、黃金 fixture byte 比對、writer→reader 幾何
      round-trip（矩形/三角/45°/多 layer）、**RECTANGLE 偵測**、閉合 ring、空/degenerate 略過、deterministic。
- [ ] ROI 裁剪頂點測試 → 併入 M2（需 shapely）。
- [x] `python3 -m py_compile` 全過。
- [x] `pytest` 綠 → user 本地 45 passed（writer + layout_export + oasis_debug）。

### M5: 開發者模式 gating  [status: done, 待本地驗收]

- [x] `self._dev_mode` 由 `QSettings("GLAS","GLAS")` 的 `dev_mode` 載入（預設 False、跨重啟持久化）。
- [x] About 對話框 icon `_attach_dev_toggle`：點 5 次 → `_set_dev_mode(toggle)`、寫回 QSettings、
      `QMessageBox` 回饋 enabled/disabled（再點 5 次可關閉）。
- [x] Export OASIS 按鈕在 Export group，建立時 `setVisible(self._dev_mode)`；`_refresh_dev_ui` 切換顯示。
- [ ] 驗證：預設看不到 Export OASIS；About 點 icon 5 次後出現；重開 App 維持狀態。**待 user。**

### M6: 收尾  [status: planned]

- [ ] SESSION_LOG 收尾條目；CLAUDE.md §8 移除 [F9]；README/§1/§5 補 OASIS 匯出能力 + 開發者模式說明。

---

## Affected Files

- `glas/core/oasis_writer.py`（新）
- `glas/app/gds_align_tool.py`（export 動作接線）
- `tests/test_oasis_writer.py`（新）
- `README.md`、`CLAUDE.md`、`SESSION_LOG.md`、本 plan 檔

---

## Risks / Open Questions

- **風險（主）：** OASIS record byte layout 必須精確對齊 SEMI P39，KLayout reader 較嚴。緩解：最小子集
  （無 CRC/CBLOCK/非 modal）+ 以自家 `oasis_streamer` 做 round-trip oracle + user 本地 KLayout 複驗。
- **待確認（M1 內）：** 無來源檔時的 fallback unit 值與 spec 對應（有來源檔則直接沿用其 unit，無爭議）。
- **holes 表示（M2 待 user 決策 O-holes）：** 兩層問題——(1) app 的 `LayerEntry.polygons` 經
  `gds_boolean.geometry_to_polygons` 轉出時**已丟棄內環**（`gds_boolean.py:556`，display-only；mask 才正確處理洞），
  故直接匯出 `.polygons` 會把「環狀/甜甜圈」ROI 變成填實。(2) OASIS POLYGON 與 GDS 一樣無原生 hole，
  同 layer 疊多個多邊形是聯集（不會自動挖洞）。**選項：** (a) 匯出 display 用的 `.polygons`（無洞、最簡、所見即所得，
  但環狀 ROI 失真）；(b) 匯出時**重新評估** boolean 取得帶洞 shapely geom，再用 keyhole/cut 正確表示洞（正確但工程量大）。
- **效能：** ROI 裁剪用 shapely，幾何量受 ROI/FOV 限制故有界；全 layer 無裁剪大量輸出時再評估。
- **外部依賴：** 驗收需 user 本地 KLayout 開檔確認；沙箱無 PyQt6/KLayout。
- **範圍外（另立題）：** 「下游接水確認每張 SEM ROI」的對位資訊輸出（overlay/CSV/外接引擎）——非 F9。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/test_oasis_writer.py -v` 通過
- [ ] 手動：GUI 匯出（全區 + ROI 裁剪）→ KLayout 正常開、layer/幾何/座標/裁切正確
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F9]`
- 從 `CLAUDE.md` §8 移除 [F9]
- 本檔保留作 design history
