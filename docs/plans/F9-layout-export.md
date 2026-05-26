# [F9] Layout 匯出：raw layer + Boolean 合成 layer 寫出成 OASIS（.oas，含 ROI 裁剪）

> **狀態：** planned
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
**選擇：** 是。使用者可給一組 **GDS 座標的 bbox**（或沿用目前 FOV / ROI），只輸出裁切後落在區域內的幾何。
**理由：** 對應使用者「裁減特定區域的 ROI（給 GDS 座標）輸出」訴求；用 shapely `intersection(box(...))` 實作。

### Q4: 走到哪一步？
**選擇：** 先產 plan 再核准（本檔）。**尚未動工。**

---

## Milestones

> 每個 milestone 以「一個 session 可完成」為粒度切。

### M1: Core OASIS writer (`glas/core/oasis_writer.py`，Qt-free，最小合規)  [status: planned]

- [ ] 新模組，純 numpy + 標準庫，沿用扁平 sys.path bare-import 慣例（§4），無 Qt 依賴（§6）。
- [ ] encode 原語（既有 decode 的逆）：`encode_unsigned_int` / `encode_signed_int` / `encode_real` /
      point-list；先寫單元測試對 `oasis_streamer` 的 decode 做 round-trip。
- [ ] 進入點 `write_oasis(path, layers, *, unit, cellname="L0")`，
      `layers = [(layer:int, datatype:int, polygons:list[ndarray(N,2) int])]`。
- [ ] 寫出最小合規 record 序列：MAGIC bytes → START（含 `unit`、offset_flag=0）→ CELLNAME/CELL →
      每個 polygon 一筆 POLYGON（layer/datatype + point-list）→ END（**validation scheme = 0**）。
      矩形可走 RECTANGLE 優化或一律當 POLYGON（先求正確，矩形優化視情況）。
- [ ] **unit 策略**：寫檔時**沿用來源檔的 START.unit**（座標保持原 DBU 不動）→ 完全避開 nm↔micron
      換算爭議，KLayout 顯示的尺度與原檔一致。無來源時 fallback（1 DBU = 1 nm → unit 依 spec 對應值，M1 內驗證）。
- [ ] 邊界處理：空幾何、座標型別（int）、point-list 首尾與閉合規則依 SEMI P39 §7.7.9。
- [ ] 驗證：寫出 → `oasis_streamer` 讀回 → 逐 layer / 逐點比對；多 layer、含一般多邊形與矩形的 case。

### M2: 幾何蒐集 + ROI 裁剪 (Qt-free helper)  [status: planned]

- [ ] helper（放 core，可單測）：輸入 layer entries 的 polygons + 可選 `crop_bbox=(x1,y1,x2,y2)`（GDS nm），
      用 shapely `intersection(box(...))` 裁切，輸出回 `(layer, datatype, rings)` 給 writer。
- [ ] `shapely_to_rings(geom)`：把 Polygon/MultiPolygon（含 holes）攤平成 ring 列表（holes 依 OASIS 處理）。
- [ ] 裁剪後空 layer 自動略過；裁剪邊界落在多邊形中間 → 由 shapely 產生正確切邊。
- [ ] 驗證：對含跨界多邊形的 case 斷言裁切後頂點正確、面積符合預期。

### M3: App 匯出動作（選 layer + 可選 ROI 區域 → .oas）  [status: planned]

- [ ] export 對話框 / 動作（仿 `_on_export_cache` 的 `getSaveFileName`）：可勾選要匯出的 raw layer +
      synthetic layer；可選「裁剪區域」來源（目前 FOV / 手動輸入 GDS bbox）。
- [ ] 從 layer entry 取 `.polygons`，synthetic 用自訂 (layer, datatype)，raw 用原始 (layer, datatype)；
      unit 由已載入 OASIS 的 START.unit 帶入 writer。
- [ ] 失敗 `QMessageBox.critical`，成功 status bar 提示路徑。
- [ ] 驗證：本地 GUI 匯出（含一次全區、一次 ROI 裁剪）→ KLayout 開啟確認 layer/幾何/座標/裁切邊界。

### M4: 測試 + 收尾  [status: planned]

- [ ] `tests/test_oasis_writer.py`：encode 原語 round-trip、writer→reader 幾何 round-trip、多 layer、
      ROI 裁剪頂點、空幾何。
- [ ] `python3 -m py_compile` 全過；`pytest tests/test_oasis_writer.py -v` 綠。
- [ ] SESSION_LOG 收尾條目；CLAUDE.md §8 移除 [F9]；README/§1/§5 補一句 OASIS 匯出能力。

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
- **holes 表示：** OASIS POLYGON 為單一外環；含洞幾何如何拆（外環 + 內環同 layer，或 cut）M2 定。
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
