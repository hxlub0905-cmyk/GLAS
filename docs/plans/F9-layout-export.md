# [F9] Layout 匯出：Boolean 合成 layer / 選定 layer 反向寫出成 layout 檔（KLayout 可開）

> **狀態：** planned
> **§8 ID：** [F9]
> **建立：** 2026-05-26
> **負責 branch：** claude/adoring-cannon-oKZKo

---

## Goal & Context

**問題 / 觀察：** GLAS 目前的幾何資料流是「只進不出」——OASIS reader → numpy / shapely
→ rasterize 成 mask 做對位，最後只匯出 **alignment offset（CSV / JSON）**。使用者花力氣用
Boolean 引擎合成的 ROI layer（L0）以及 ROI 內 walk 出來的幾何，**沒有任何方式存成 layout 檔**，
無法丟回 KLayout 檢視，也無法給下游其他專案工具當 ROI 定義來源。

**想達成（成功長相）：**

1. **（主功能）把 Boolean 合成 layer 反向輸出成 layout 檔**，丟回 KLayout 能正常開啟、layer/幾何正確。
2. **（接續）匯出的 layout 可被後續其他專案工具接走，作為 ROI 設定來源**（與既有 alignment CSV
   的 `image_id` join 流程並存）。

**跟現有系統的關係：** 純**新增**，不改任何既有運算 / 不變式。新增一個 Qt-free 的 `glas/core/`
writer 模組，app 端加一個 export 動作（仿照既有 `_on_export_alignment` / `_on_export_cache`）。
幾何來源是 layer panel 既有的 entry `.polygons`（root nm 座標）+ synthetic layer 的 `expr_text`。

---

## Q&A Decisions

### Q1: 匯出哪一種幾何？
**選項：** Boolean 合成 layer L0 / 對齊後 layout / ROI walk 原始 layer
**選擇：** 主要是 **Boolean 合成 layer L0**（+ 允許一併選原始 layer 匯出）；「對齊後 layout 供下游
ROI」為第二訴求，語意待定（見 Open Questions O1）。
**理由：** L0 是使用者投入最多、目前完全無法保存的產物，價值最高；原始 layer 匯出近乎免費（同一 writer）。

### Q2: 輸出檔案格式 — GDSII vs OASIS？（user 要求「評估一下」）
**選項：** GDSII (.gds) / OASIS (.oas) / 兩者都做
**評估：**

| 面向 | GDSII | OASIS |
|---|---|---|
| writer 複雜度 | **低**：固定 record、big-endian、BOUNDARY/XY/ENDEL，無壓縮 / 無 modal | **高**：unsigned-int 變長編碼、modal variables、CBLOCK、name table、嚴格 END + validation signature |
| KLayout 開啟 | 完美 | 支援但格式較嚴（modal / validation 易踩雷） |
| 自寫正確性風險 | 低 | 高（專案目前只寫過 OASIS **reader**，沒寫過任何 writer） |
| 檔案大小 | 較大 | 緊湊（僅在百 MB 級幾何才有意義） |
| 座標 | int32（nm 完全夠：±2.1×10⁹ nm ≈ 2.1 m） | 任意精度 |
| 下游通用性 | 所有 layout 工具都吃 | 多數吃 |

**建議選擇：先做 GDSII writer。**
**理由：** 使用者真正目標是「KLayout 能正常開 + 下游工具能接」——這兩點 GDSII 100% 達成，而自寫
OASIS writer 的複雜度與正確性風險高一個量級（尤其要通過 KLayout 對 OASIS 的 validation）。匯出對象
是 L0 / ROI 這種**小幾何**，OASIS 的緊湊優勢完全用不上。符合專案「不依賴 klayout / gdstk、自寫
parser」哲學（§1），且能放進 `glas/core/`（Qt-free，§6）。

> ⚠️ 此項與使用者初始講的「.OAS」不同。**請於核准時定案**：採 GDSII（建議），或仍堅持 OASIS
> （則 M1 工作量與風險顯著放大、需追加 milestone）。writer 介面會設計成「幾何來源 ↔ 格式後端」可分離
> （不過度抽象，僅 `polygons → bytes` 單一進入點），日後要加 OASIS backend 不需重寫上層。

### Q3: 走到哪一步？
**選擇：** 先產 plan 再核准（本檔）。**尚未動工。**

---

## Milestones

> 每個 milestone 以「一個 session 可完成」為粒度切。

### M1: Core GDSII writer (`glas/core/gds_writer.py`，Qt-free)  [status: planned]

- [ ] 新模組 `glas/core/gds_writer.py`：純 numpy + 標準庫，沿用扁平 sys.path bare-import 慣例（§4）。
- [ ] 進入點 `write_gds(path, layers, *, dbu_nm=1.0, libname="GLAS", cellname="L0")`，其中
      `layers = [(layer:int, datatype:int, polygons:list[ndarray(N,2) int])]`。同時提供
      `shapely_to_rings(geom)` 把 shapely Polygon/MultiPolygon（含 holes）攤平成 ring 列表。
- [ ] 寫出 GDSII record 序列：HEADER → BGNLIB → LIBNAME → UNITS → BGNSTR → STRNAME →
      （每個 polygon：BOUNDARY → LAYER → DATATYPE → XY → ENDEL）→ ENDSTR → ENDLIB。
      big-endian、record 長度 / 偶數 byte padding、XY 為 int32 且 ring **首尾閉合**。
- [ ] UNITS record：依 `dbu_nm` 算 (user_unit, meters)；預設 1 DBU = 1 nm →
      (1e-3, 1e-9)。**確認 OASIS START.unit ↔ nm 對應**（目前引擎假設 1 DBU = 1 nm，見 gds_fov 註解）。
- [ ] 邊界處理：holes（GDSII 無 hole 概念 → 用 cut/keyhole 或拆成外環+內環同 layer 的標準做法）、
      空幾何、超出 int32 的座標（raise 明確錯誤）。
- [ ] 驗證：在 test 內寫一個極簡 GDSII record 結構 reader，round-trip 解回 layer/datatype/座標並
      逐點比對；至少一個含 hole 與 MultiPolygon 的 case。

### M2: App 匯出動作（synthetic / 選定 layer → .gds）  [status: planned]

- [ ] layer panel 對 synthetic layer entry 加「Export as GDS…」（仿 `_on_export_cache` 的
      `getSaveFileName` 流程）；亦提供「Export visible layers as GDS…」匯出目前選的多個 layer。
- [ ] 從 layer entry 取 `.polygons`（root nm），synthetic 取其 (layer=自訂或 0, datatype)；
      raw layer 用原始 (layer, datatype)。多 layer 合併到單一 cell。
- [ ] unit 從已載入 OASIS 的 START.unit 帶入 writer（fallback 1 nm）。
- [ ] 失敗用 `QMessageBox.critical`，成功 status bar 提示路徑。
- [ ] 驗證：本地 GUI 匯出 L0 → KLayout 開啟確認 layer / 幾何 / 座標（與 canvas 一致）。

### M3: 下游「對齊後 / ROI 來源」匯出語意定案 + 文件  [status: planned]

- [ ] 依 O1 決議實作：要嘛 (a) 維持 root 座標匯出、定位仍走既有 offset CSV（`image_id` join）；
      要嘛 (b) 額外提供「套用 per-image offset 後」的匯出選項。
- [ ] README「Features / 匯出」段補 GDS 匯出；CLAUDE.md §1 能力清單 + §5 資料流補一句。
- [ ] 驗證：與下游工具接水流程的 end-to-end 一致性（若 user 提供下游格式需求則對齊）。

### M4: 測試 + 收尾  [status: planned]

- [ ] `tests/test_gds_writer.py`：record 結構、座標 round-trip、hole/MultiPolygon、int32 溢位 raise、
      shapely→rings。
- [ ] `python3 -m py_compile` 全過；`pytest tests/test_gds_writer.py -v` 綠。
- [ ] SESSION_LOG 收尾條目；CLAUDE.md §8 移除 [F9]。

---

## Affected Files

- `glas/core/gds_writer.py`（新）
- `glas/app/gds_align_tool.py`（export 動作接線）
- `tests/test_gds_writer.py`（新）
- `README.md`、`CLAUDE.md`、`SESSION_LOG.md`、本 plan 檔

---

## Risks / Open Questions

- **O1（待 user 確認）：** 「對齊後 layout 供下游 ROI」的確切語意——是 (a) 匯出 **canonical root 座標**
  的 layout、定位交給既有 alignment CSV（`image_id` join，與 README 現行流程一致，推薦），還是 (b) 要把
  per-image offset **烘進**幾何後匯出（per-image 各一檔）？影響 M3。
- **O2（核准時定案）：** 格式 GDSII（建議）vs 使用者初始提的 OASIS（見 Q2）。
- **風險：** GDSII hole 表示法（KLayout 對 keyhole/cut 的容忍度）需實測；OASIS START.unit 對 nm 的換算需驗。
- **外部依賴：** 驗收需 user 本地的 KLayout 開啟匯出檔確認；沙箱無 PyQt6/KLayout。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/test_gds_writer.py -v` 通過
- [ ] 手動：GUI 匯出 L0 → KLayout 正常開、layer/幾何/座標正確；下游工具能接
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F9]`
- 從 `CLAUDE.md` §8 移除 [F9]
- 本檔保留作 design history
