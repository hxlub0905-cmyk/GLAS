# [F2] 獨立 GDS Align Tool（GDS ↔ SEM 對位 + Boolean 合成 layer）

> **狀態：** planned
> **§8 ID：** [F2]
> **建立：** 2026-05-07
> **負責 branch：** `claude/add-gds-functionality-OP2QY`

---

## Goal & Context

### 為什麼做

主程式目前用 gray-level + bbox 在 SEM 影像上定位 ROI（CMG / blob），在 layout 複雜或結構間 GLV 接近時失效。**GDS 是 layout 的 ground truth**，若能把 GDS 對位到 SEM，就能：

1. **精準鎖定 ROI**（取代 / 補強 gray-level bbox 定位）
2. **提供 CD reference**（GDS designed CD 跟量到的 CD 比對）
3. **組合任意 layer**（透過 Boolean 運算把多個原始 layer 合成出實際 SEM 看到的 contour）

### 想達成什麼

第一階段做**獨立工具** `tools/gds_align_tool.py`（仿 `tools/histogram_analyzer.py` 的單檔 PyQt6 模式），驗證可行後再整合回主程式 Recipe pipeline。

成功長相（end-to-end）：

1. 載入 GDS → 看到所有 layer，可切顯示 / 改色
2. 用 Boolean 表達式（如 `[(A > W:10) & B] < H:10`）即時組成「alignment layer（POI）」+ 給該 layer 一個 GLV → 產 template 影像
3. 載入 SEM (KLARF dataset) → key 一個大致 coarse offset（dx_nm, dy_nm）→ overlay 預覽
4. 對 dataset 中每張 SEM 在 coarse 附近用 `cv2.matchTemplate` fine-refine → 拿到 per-image refined offset + score
5. Export per-image offset CSV/JSON，後續供主程式 Recipe 取用

### 跟現有系統的關係

- **並存（不取代）**：第一階段是獨立工具，主程式 Recipe pipeline 不動
- **未來整合 hook**：M5 的 export 格式預留欄位 (`image_id` ↔ `MeasurementRecord.image_id`)，方便日後 Recipe 直接吃這個檔做 ROI 定位
- **依賴主程式**：僅借用 `src/gui/styles.py` QSS 與 KLARF parser (`src/core/klarf_parser.py`) 載 SEM 列表

---

## Q&A Decisions

### Q1: 工具型態 — 主程式 workspace vs 獨立 tool？
**選擇：** 獨立 `tools/gds_align_tool.py`（仿 histogram_analyzer 模式）
**理由：** 先驗證對位可行性、避免汙染主程式。驗證 OK 後再規劃整合（另開 plan）。

### Q2: GDS 用途 — 純對位 vs 對位 + CD reference？
**選擇：** 兩者皆要，但**第一版先聚焦對位**。CD reference 在 export 階段附 designed CD 欄位即可，量測比對留主程式整合階段做。
**理由：** Scope 控制；對位是更基礎的能力。

### Q3: Coarse alignment 觸發方式？
**選擇：** **User 手動 key (dx_nm, dy_nm)**
**理由：** GDS 檔大、自動 cross-correlation 太慢；user 對自己的 layout 有 prior knowledge，手動最快。後續若需要可再加自動。

### Q4: Fine alignment 演算法？
**選擇：** `cv2.matchTemplate`（template matching, `TM_CCOEFF_NORMED`）
**理由：** 比 NCC 自寫快、OpenCV 原生 SIMD；user 已經有「coarse 給好 + 局部 refine」的設定，只搜 translation 即可。

### Q5: GDS 怎麼模擬成 SEM 可比對的影像？
**選擇：** **POI (Polygon of Interest) 給一個大致 GLV** → 對 raster mask 上色 + 背景另一 GLV → 模擬 SEM
**理由：** User 提的方案；簡單可控。Boolean 合成可篩出 MG layer 之類「SEM 較亮」的部分當對位 reference。後續若 contrast 不夠可再加 Gaussian blur / edge gradient。

### Q6: Layer Boolean 運算範圍？
**選擇：** 第一版支援 **AND / OR / XOR / NOT / GROW (dilate) / SHRINK (erode)**；合成後存為 session-level 新 layer（可命名）。
**理由：** 涵蓋實際 SEM contour 推導常用操作；GROW/SHRINK 對應光學 bias / 邊緣修正。**第一版不存回 GDS 檔**（在 session 中當普通 layer 用即可），如需匯出可在 M5 加。

### Q7: Dataset 邊界（共用一個 coarse offset）？
**選擇：** **KLARF 檔為邊界**（一個 KLARF = 一個 dataset）
**理由：** 同 KLARF 通常同時段、同機台、同 wafer，coarse offset 一致可信。

### Q8: GDS library？
**選擇：** **`gdstk`**（Apache 2.0、modern、比 gdspy 快 ~10x）
**理由：** 主流、active maintained。若 user 環境裝不上再 fallback `gdspy`。

### Q9: Boolean 運算時機 — 預存 synthetic layer vs 即時運算？
**選擇：** 即時 Boolean 表達式引擎
**理由：** 彈性高（改參數立刻生效）、省 cache 空間（只存原始 layer）、與業界工具（HMI）概念一致。表達式語法參考 HMI 概念：`L0 = [(A > W:10) & B] < H:10`。

### Q10: Boolean 表達式 parser — pyparsing vs 手寫？
**選擇：** 手寫遞迴下降 parser（約 50~80 行）
**理由：** 語法固定簡單、減少外部相依、錯誤訊息更好控制。

### Q11: grow/shrink 單位 — pixel vs nm？
**選擇：** nm
**理由：** nm 有物理意義，不受 SEM 放大倍率影響。GDS 座標本身是 nm，shapely buffer(n) 直接對應 n nm。`A > W:5` = 單邊 grow 5nm（shapely buffer(5)），四面各擴 5nm，總寬 +10nm。

### Q12: Boolean 輸出形式？
**選擇：** 兩者都輸出
- A. shapely polygon list → GDS canvas 顯示，確認 ROI 定義正確
- B. numpy uint8 mask（同 SEM 影像尺寸）→ SEM MM 量測只在 mask 白色區域內進行 → 需輸入 nm_per_pixel 換算

### Q13: Coordinate Setup 設定存在哪裡？
**選擇：** 與 cache (.npz) 一起存
**理由：** chip corner offset 與特定 OASIS 檔案綁定，不同製程節點 offset 不同。與 cache 綁定可避免跨製程混用。載入 cache 時自動帶入 chip corner offset 與 FOV 大小。

### Q14: KLARF 座標換算邏輯？
- KLARF：die corner = (0,0)，XREL/YREL 相對 die corner（nm）
- GDS：chip corner = (0,0)
- 換算：`GDS_x = XREL - chip_corner_x`，`GDS_y = YREL - chip_corner_y`
- Y 軸方向目前假設同向，實測後確認。

### Q15: RFL 實際格式 → chip_corner 由 RFL **算出**（非直接輸入）（2026-05-20 user 釐清）
**現象：** 原 Q14 假設 user 直接從 RFL 抄 chip corner offset；實際 RFL 給的是**不同量**。

**RFL 提供（單位皆 mm，皆左下角原點）：**
1. **Die 尺寸** `(die_w, die_h)` — 等於 KLARF `DiePitch`
2. **GDS chip 的 Location = chip 中心相對 die 中心** `(chip_cx, chip_cy)`
3. **GDS chip 的 Size** `(chip_w, chip_h)`

**推導（chip 角落相對 die 角落）：**
```
chip_corner = die_size/2 + chip_centre_offset - chip_size/2
chip_corner_x = (die_w/2 + chip_cx - chip_w/2) × 1e6   # mm→nm
chip_corner_y = (die_h/2 + chip_cy - chip_h/2) × 1e6
```
實作：`gds_fov.rfl_to_chip_corner(die_w_mm, die_h_mm, chip_cx_mm, chip_cy_mm, chip_w_mm, chip_h_mm) → (cc_x_nm, cc_y_nm)`，結果餵進 `klarf_to_gds()`。

**已確認慣例（2026-05-20 AskUserQuestion）：**
- 原點角落：**先假定 die / chip 皆左下角（X右 Y上）**，不確定，實測可調
- Y 軸：**先假設 KLARF 與 GDS 同向（都向上）**，實測後若反向啟用 `flip_y`
- die size 來源：**KLARF DiePitch 自動讀為預設 + 允許 RFL 手動覆寫，不一致時提醒**

**GUI 連動（已完成 2026-05-20）：** M3 `CoordinateSetupPanel` 已從「chip corner X/Y (nm)」改為「die size / chip center / chip size (mm)」六欄 + DiePitch 自動帶入；cache metadata (SCHEMA_VERSION 2) 存 RFL 六參數，載入還原。offscreen smoke 通過，互動 UI 待 user 本地驗證。

---

## Milestones

### M1: GDS Loader + Layer 渲染  [status: implemented (klayout-backed), awaiting runtime verification]

獨立工具骨架；可載入 GDS / OASIS、列出 layer、把選定 layer rasterize 成 numpy 影像顯示。

- [x] 建 `tools/gds_align_tool.py`：MainWindow + 主程式 QSS fallback
- [x] 加 `requirements.txt` 中 `klayout>=0.28`（preferred）+ `gdstk>=0.9`（fallback）optional
- [x] OASIS 支援：依副檔名分派 `read_oas` / `read_gds`
- [x] Threaded loader → multiprocessing 子行程（gdstk 不釋放 GIL）
- [x] 自製 `LoadProgressDialog`：spinner + Elapsed clock，避免 QProgressBar QSS 動畫被吃
- [x] **klayout backend**：`RecursiveShapeIterator` streaming load（不 flatten）、`_POLY_HARD_LIMIT = 10M` 防 OOM
- [x] `LayerPanel` widget：(layer, datatype) 列表、click 切 visibility、double-click 改色
- [x] `GdsCanvas` widget：QPainter pan/zoom + viewport / sub-pixel culling + per-layer 80K cap
- [x] `rasterize_layer()`：cv2.fillPoly + Y 翻轉；cv2 沒裝時 fallback 純 numpy
- [ ] **驗證**：`python3 -m py_compile` ✓；待 user runtime 驗證（checklist 見 SESSION_LOG）

**Reader 路線結論（2026-05-07）：** gdstk 對 300 MB D2DB OASIS segfault（STATUS_ACCESS_VIOLATION）；klayout 對同檔 10+ 分鐘無進度。兩家現成 Python reader 都撐不住 user 的實際 production 檔案。決議自寫 streaming OASIS parser（見 M1.9–M1.12）。

---

### M1.9: OASIS byte-stream decoder + 表格 record 解析  [status: done (2026-05-07, user verified)]

奠定 streaming parser 基礎：把 OASIS 二進位轉成可程式存取的 record stream。本階段只解 table-level records（不解 cell content）；目標是能列出 cell 名表 + layer 名表，等同於 klayout scan-only 但不依賴 klayout。

- [ ] 建 `tools/oasis_streamer.py`：模組骨架 + 公開 API
- [ ] 常數：record ID（0–34）、magic `%SEMI-OASIS\r\n`
- [ ] 例外：`OasisFormatError`
- [ ] 解碼函式：
  - [ ] `decode_unsigned_int(stream)`：variable-length，每 byte bit7=continuation
  - [ ] `decode_signed_int(stream)`：unsigned 的低位 bit 0 是 sign
  - [ ] `decode_real(stream)`：6 種 type code（int / reciprocal / ratio / float32 / float64）
  - [ ] `decode_string(stream)`：length-prefixed bytes
- [ ] `ModalState` dataclass：先放 `xy_relative` / `layer` / `datatype` / `geometry_x/y` 等已用到的；其他空著 (M1.10/M1.11 補)
- [ ] `OasisReader`：
  - [ ] `__init__(path)`：open file, read magic, validate
  - [ ] `iter_records()` generator：yield `(record_id, payload_dict)`
  - [ ] START record 完整解（version / unit / offset_flag）
  - [ ] CELLNAME 3/4 解：refnum + name
  - [ ] LAYERNAME 11/12 解：name + layer interval + datatype interval
  - [ ] CELL 13/14 解 header only（refnum/name），cell content 拋 NotImplementedError（M1.10 接）
  - [ ] END 識別並停
  - [ ] XYABSOLUTE/XYRELATIVE/PAD：no-body records，正確跳過
- [ ] CLI smoke test：`python tools/oasis_streamer.py test_layout.oas` 印出 magic + START + 所有 CELLNAME + LAYERNAME，到第一個 CELL 停止
- [ ] 修 `tools/test_gdstk_noflatten.py` 加 `cell.paths` 迭代

**驗證：** `python3 -m py_compile` ✓；對 `test_layout.oas` smoke test 印出 4 個 CELLNAME (TOP/CELL_A/B/C) + 5 個 LAYERNAME (POLY/ACTIVE/METAL1/VIA/ANNOTATION)

---

### M1.10: Cell content + transform 階層 + CBLOCK 解壓  [status: done (2026-05-14, user verified on 345 MB Calibre D2DB)]

讓 streaming parser 能完整 traverse 整個 OASIS（不只 table），含 reference 階層 + 壓縮 block。

- [x] PLACEMENT 17/18 解：cell ref / 座標 / rotation / mirror / repetition / magnification (18 only)
- [x] REPETITION 解：12 種 type (0-11)，產生 displacement list
- [x] Transform 階層：每個 PLACEMENT payload 附 `in_cell`（parent cell id/name）+ (x, y, angle, mag, flip)，供後續 cell-graph walker 解析絕對座標
- [x] CBLOCK 34 解：read header → `zlib.decompress(wbits=-15)` 解 deflate → 切換 substream → 子 record 用同一個 decoder pipeline → 結束後 fall back 主 stream
- [x] `OasisStream` 透明處理 CBLOCK 邊界（`push_cblock` + `maybe_pop_exhausted` 在 iter_records 開頭 auto-pop）
- [x] PROPERTY 28/29 解（M1.9 已完成；M1.10 確認在 CELL 內也 sync 正常）
- [x] XELEMENT 32 解；XNAME (M1.9 已完成)；XGEOMETRY 33 留 `OasisNotImplemented`（標準檔幾乎不會用）

**M1.10 production 驗證（2026-05-14, user verified on 345 MB Calibre D2DB E3B_CMG_CMP_D2DB_250930.oas）：**
- 第一輪 user 報 `[STOP] <id 115>` + PLACEMENT cell_ref 為 5 KB binary garbage → 揪出 PLACEMENT info-byte N-bit 兩個 branch 寫反 bug（M1.10 plan 寫法跟 SEMI P39 §22.6 + PROPERTY decoder convention 不一致）
- 修 N-bit branch + 同步修 10 個 unit test 用錯方向的 info byte（之前 test 跟 decoder 對齊但兩者都跟 spec 反向）
- 第二輪 user 重跑：`[STOP]` 正確落在 `RECTANGLE (id 20) at byte 0`（CBLOCK substream 內第一個 record），PLACEMENT 從 1 garbage 變 4 個正常解碼，CBLOCK 成功解壓進到 substream
- 50,392 PAD records 確認為 Calibre 在 CELLNAME table 寫的 byte-alignment padding（合法 SEMI P39 §11 用法），不影響後續 sync

**M1.10 新增測試覆蓋：**
- `TestGDelta` (8)：form-1 octangular 6 dir + form-2 generic 正負
- `TestThreeDelta` / `TestTwoDelta` (4)：axis + 45° + Manhattan
- `TestRepetition` (13)：type 0–11 全覆蓋 + unknown
- `TestOasisStream` (4)：base 模式、push/pop、partial read、seek-within-substream
- `TestPlacement` (10)：refnum / inline name / 4 quarter-turn angle / modal cell / XYRELATIVE / mag+angle / flip / repetition / modal repetition reuse / 缺 modal 時 error
- `TestCBlock` (3)：CBLOCK 內 record surface 到 caller、多 record 自動 pop、unknown comp_type error（用 tmp_path 寫真 OASIS 檔）
- `TestXElement` (1)

---

### M1.11a: Geometry record decoders + layer filter  [status: implemented (sandbox 2026-05-14), awaiting user runtime verification]

User 決議 split — 本階段只做 decoder 層，讓 parser 能對任意 OASIS 跑完整檔到 END；numpy buffer + transform stack 留 M1.11b。覆蓋範圍依 user 選擇為 full set（7 個 record）。

**Byte layout 來源（authoritative）：** klayout `src/plugins/streamers/oasis/db_plugin/dbOASISReader.cc` `do_read_*` functions — 直接 fetch GitHub raw source 看 mask bits 與 read order，避免 M1.10 PLACEMENT N-bit 那種靠記憶寫反的事重演。

- [x] RECTANGLE 20 解：info byte `SWHXYRDL`（S=0x80, W=0x40, H=0x20, X=0x10, Y=0x08, R=0x04, D=0x02, L=0x01）+ modal 應用 + S=1 時 H=W
- [x] POLYGON 21 解：info byte `00PXYRDL` + point-list（含 polygon 自動 closure）
- [x] PATH 22 解：info byte `EWPXYRDL` + halfwidth + extension byte（start/end 各 2 bits encoding 4 modes：reuse modal / zero / halfwidth / explicit signed-int）+ point-list
- [x] TRAPEZOID 23/24/25 解：info byte `0WHXYRDL` + 依 record id 讀 delta_a / delta_b（23=both, 24=只 a, 25=只 b）
- [x] CTRAPEZOID 26 解：info byte `TWHXYRDL` + ctrapezoid-type uint
- [x] CIRCLE 27 解：info byte `00rXYRDL` + radius
- [x] TEXT 19 解：info byte `0CNXYRTL`（C=text string 存在, N=1→refnum 0→a-string；T=texttype, L=textlayer）+ 獨立 text_x/text_y modal + 用 (text_layer, text_type) 套 layer filter
- [x] **Point-list decoder (6 types)**：type 0/1 (Manhattan zigzag h/v) + type 2 (2-delta) + type 3 (3-delta) + type 4 (g-delta cumulative) + type 5 (g-delta velocity = 二階累積)
- [x] Delta encoding：1-delta / 2-delta / 3-delta / g-delta（M1.10 已有；M1.11 新用）
- [x] Modal state expansion：`ctrapezoid_type` / `circle_radius` / `path_start_extension` / `path_end_extension` / `text_string` / `text_layer` / `text_type` + 全部加進 `reset_on_cell_boundary`
- [x] Layer filter：constructor 加 `wanted_layers: Optional[set[(layer, datatype)]]`，不在 set 內的 record 仍 decode（保持 stream sync）但 payload 加 `filtered_out: True` 且不附 `points` / `repetition_offsets`，省記憶體
- [x] iter_records dispatch + CLI dump emit 都接 7 個新 record
- [ ] 套 transform stack 把 cell-local 座標轉成 root 座標 — **M1.11b**
- [ ] 把點累積到 per-layer numpy buffer — **M1.11b**

**M1.11a sandbox 驗證：**
- `python3 -m py_compile tools/oasis_streamer.py` ✓
- `pytest tests/test_oasis_streamer.py -q` → **113 passed**（83 M1.10 + 30 M1.11 新）
- M1.11 unit tests 涵蓋：`TestPointList`(9) / `TestRectangle`(5, 含 square / modal reuse / 兩種 filter) / `TestPolygon`(2) / `TestPath`(4, 含 extension 三種模式) / `TestTrapezoid`(3, 三個 record id) / `TestCTrapezoid`(1) / `TestCircle`(1) / `TestText`(3, 含 inline / refnum / text-layer filter) / `TestLayerFilterRoundtrip`(2, tmp_path 寫真 OASIS 跑 iter_records)
- 待 user runtime 驗證：
  1. `python tools/oasis_streamer.py tools/test_layout.oas` — 應跑到 END 不再撞 NotImplemented（之前停在 PATH id 22）
  2. `python tools/oasis_streamer.py <D2DB> --summary` — 應跑到 END，PAD 跟 RECTANGLE/POLYGON/PATH 數量出爐，幫 M1.11b 決定 numpy buffer 設計

---

### M1.11b: Per-cell numpy storage (cell-local coords)  [status: done (2026-05-14, user verified on 345 MB Calibre D2DB)]

依 user 跑 D2DB `--max-records 5000000` 拿到的 histogram（RECTANGLE 98.19% / POLYGON 0.02% / PLACEMENT 0.023% / 其他 ≈ 0%）決定 storage layout：

- [x] 新檔 `tools/oasis_store.py`：`OasisGeometryStore` class，drive `OasisReader.iter_records()` 累積 per-cell / per-layer geometry
- [x] `_RectBuffer`：chunked ndarray growth（init 1024、doubling、max 1M），避免 list-of-tuples 14 GB 爆 RAM
- [x] RECTANGLE → `ndarray[N, 4]` int32 of `(x1, y1, x2, y2)` per (cell, layer, datatype)
- [x] POLYGON → list of `ndarray[(n, 2)]` int32（rare path，不 vectorize）
- [x] PLACEMENT → `list[Placement]` per cell（target / kind / x / y / angle / mag / flip / repetition_offsets）留給 M1.11c walker 用
- [x] Layer filter pass-through `wanted_layers={(L, D), ...}`：filtered record 在 decoder 階段已 strip 重資料，store 完全 skip accumulate
- [x] **Large-file guard**：檔案 > 50 MB 且沒指定 `wanted_layers` 又沒 `allow_unfiltered=True` → raise ValueError（保護 personal laptop RAM）
- [x] Query API：`rectangles_for / polygons_for / placements_for / layer_pairs_in / summary`，empty 時 return 空 ndarray / list 不是 None
- [x] CLI smoke test：`python tools/oasis_store.py path [--layer L:D ...] [--max-records N]` 印 JSON summary
- [x] **NOT** in this PR：cell-graph transform walker（rotation / flip / mag / repetition expansion）— M1.11c
- [x] **NOT** in this PR：root-coord output、ROI bbox crop、PLACEMENT instantiation — M1.11c

**M1.11b production 驗證（2026-05-14, user verified on 345 MB Calibre D2DB E3B_CMG_CMP_D2DB_250930.oas）：**
- `python tools/oasis_store.py "<d2db>" --layer 17:102 --max-records 5000000` ~1.5 分鐘跑完
- `total_rectangles: 3,468` (CMG only) — layer filter 拒絕 99.93% 的 RECTANGLE records，符合預期（CMG 是 37 layer 之一）
- `total_polygons: 44` / `total_placements: 1,160` / `cells_with_rectangles: 31` / `cellnames_known: 13,276`
- `record_counts` 顯示全 14 種 record 完整 walk，沒 desync
- 記憶體 < 100 KB（5M sample 內的 CMG slice）；全檔線性外推 ~485K CMG rect × 16 bytes ≈ 7.7 MB，個人電腦完全 fit

---

### M1.11c: Cell-graph walker + transform expansion → root-coord ndarrays  [status: done (2026-05-14, user verified on 345 MB Calibre D2DB --max-records 5M)]

把 M1.11b 的 per-cell storage 套 PLACEMENT 階層展開到 root cell 座標。

**User 決議：** quarter-turn (0/90/180/270deg) + flip 支援；arbitrary 角 `warnings.warn` + skip。`walk_to_root` flat ndarray 輸出（個人電腦 fit）。

- [x] 新檔 `tools/oasis_walker.py`：`Transform` dataclass + `CellGraphWalker` class
- [x] `Transform`：2x2 float64 matrix + (2,) translation；`from_placement(x, y, angle, flip, mag)` 建構；`compose(child)` 套 parent ∘ child 公式 `(M_p @ M_c, M_p @ t_c + t_p)`
- [x] `apply_to_rects(N, 4)`：4 corner 套 M 後 min/max 重組 bbox。D4 元素下完全 axis-aligned，無 over-bound
- [x] `apply_to_points(n, 2)`：給 polygon 用，純 affine
- [x] `CellGraphWalker.walk_to_root(root, layer, datatype)`：DFS PLACEMENT graph，每個 leaf cell 套累積 transform；output flat `ndarray[N, 4]` int32（dtype 跟 store 一致）
- [x] `walk_polygons_to_root(...)`：同上但 output `list[ndarray (n, 2)]`
- [x] Repetition expansion：`repetition_offsets` 每個 entry 是獨立 instance；空 list fallback 成 `[(0, 0)]`
- [x] Arbitrary 角度處理：`Transform.from_placement` return `None`，walker warn + skip 該 placement instance（`WalkStats.arbitrary_angle_skipped` 計數）
- [x] Cycle detection：recursion stack ancestor set；偵測到 cyclic ref 就 warn + skip（OASIS 不該有但防呆）
- [x] Target resolution：placement target 可能是 int (refnum) / str (name) / bytes / None；都正確 map 到 store 的 cell key（int refnum）
- [x] `WalkStats` dataclass：cells_visited / placements_expanded / repetition_instances / arbitrary_angle_skipped / cycles_skipped / unknown_target_skipped / rectangles_emitted / polygons_emitted
- [x] CLI smoke test：`python tools/oasis_walker.py path --root REF --layer L:D [--max-records N]` 印 JSON summary (含 bbox in root coords + warnings)

**M1.11c sandbox 驗證：**
- `python3 -m py_compile tools/oasis_walker.py` ✓
- `pytest tests/test_oasis_walker.py -v` → **21 passed**：
  - `TestTransformBasics` (10)：identity / translation / 90deg / 180deg / 270deg / flip / 90deg+translation / mag / arbitrary 拒絕 / tolerance
  - `TestTransformCompose` (3)：translations / rot then translate / 兩個 90deg 合成 180deg
  - `TestWalker` (7)：no-placement / single placement translation / 90deg rotation / 2-level hierarchy / root-by-name / unknown root raises / layer mismatch empty
  - `TestArbitraryAngleSkip` (1)：monkey-patch 45deg placement → warn + skip
- Full F2 suite `pytest tests/test_oasis_streamer.py tests/test_oasis_store.py tests/test_oasis_walker.py -q` → **143 passed** (113 streamer + 9 store + 21 walker)
**M1.11c production 驗證（2026-05-14, user verified on 345 MB Calibre D2DB --max-records 5M）：**
- `python tools/oasis_walker.py "<d2db>" --root iMerge_Top --layer 17:102 --max-records 5000000` ~1.5 分鐘跑完
- `rectangles_in_root_coords: 3468`、`cells_visited: 3,903,362`、`placements_expanded: 3,903,361`、`unknown_target_skipped: 0`、`arbitrary_angle_skipped: 0`、`cycles_skipped: 0`、`warnings_total: 0`
- `bbox_in_root_coords: (12720000, 5488011) - (13438611, 6223189)` nm = ~0.72 × 0.73 mm die-level region
- 3.9M expansions from 1,160 stored placements = 平均每 placement 含 ~3,360 個 repetition_offsets（D2DB 典型大 array layout）
- emit 數 3468 = store total 是因為 `--max-records 5M` 只 decode 全檔 0.69%，placements 大多指到尚未 decode content 的 ICV cells（rectangles_for/placements_for 都回空 = walker 走死路 0 contribution）；real CMG rects 全在 iMerge_Top 與已 decoded 的 31 個 CMG-bearing cells 各被訪問恰好一次

**後續加 2 個 repetition expansion test（commit 中）：**
- `test_each_offset_emits_independent_rect`：3 offsets → 3 個獨立 rect emitted、stats 正確
- `test_repetition_combines_with_rotation`：repetition + 180deg → 每個 offset 套同樣 rotation

---

### M1.12: 整合進 gds_align_tool + 300 MB D2DB benchmark  [status: done (2026-05-15, user verified on 345 MB Calibre D2DB partial-load 5M records)]

把 streaming parser 接進主工具，取代 `_load_with_klayout`，在實際大檔上驗證。

split 為 4 個 sub-stage：
- **M1.12a** backend 接入 — `_load_with_oasis_streamer` + dispatcher reorder（commit 已合進）
- **M1.12b** scan via streamer + `iter_records` 微優化（commit `c8cac35`）
- **M1.12b+** GUI partial-load `--max-records` option（commit `53834c9`）
- **M1.12c** top-cell heuristic 修 + 移到 `oasis_walker.pick_top_cell()`（commit `06f1357` + hotfix `869c721`）

**End-to-end 驗證（2026-05-15, D2DB layer 17/101, 5M records）：**
store.run() 77.7s + walk_to_root 114s = **192s** → canvas 顯示 45,745 polys；heuristic ✓ / walker ✓ / no fallback ✓。
詳見 SESSION_LOG 2026-05-14 ~ 2026-05-15 條目。

**Performance gap → M1.13：** Partial 5M = 192s，全檔外推 ~3.6 hr；walker 比 store.run 還慢。下一 milestone 處理。

---

### M1.13: Parser performance — caching + walker vec + fast-consumer  [status: partial-load + cache targets done (2026-05-20)]

> **獨立 sub-plan：** see @docs/plans/F2-M1.13-parser-perf.md
> **目標：** D2DB partial 5M < 60s ✓（36.2s）/ cache 命中 < 5s ✓（0.36s）/ full load < 30 min（未測，partial+cache 工作流已涵蓋生產用途）
> **子項：** M1.13.1 caching ✓ → M1.13.2 walker vec ✓ → M1.13.3 fast-consumer ✓（3a/3b/3c done, store 77.7s→36.2s = 2.15×）→ M1.13.4 C ext (gated, 未啟動 — partial 已達標) → M1.13.5 perf regression test（待辦）

**2026-05-20 結論：** partial 5M + cache 兩個生產用途目標已達成（store 36.2s / cache hit 0.36s / bit-identical 45,745 rect）。M1.13.4 C ext 僅在 user 需要 full-load <30min 時才啟動。

**2026-05-22 user 決議：M1.13.4 (C ext) + M1.13.5 (perf regression test) 跳過** — partial+cache 已滿足生產用途，不再投入。

---

### M2: Layer Cache + 即時 Boolean 表達式引擎  [status: implemented — M2.1~M2.6 全部完成 (sandbox + offscreen GUI smoke tested, 77 tests)；互動 UI 待 user 本地驗證]

把原本「預先 Boolean → 存 synthetic layer」的設計，改為「即時 Boolean 表達式引擎」：
walker 跑完後把原始 layer 存成 .npz cache（秒開），FOV 空間查詢取出局部 polygon，
用手寫遞迴下降 parser 解析 HMI 風格表達式（`L0 = [(A > W:10) & B] < H:10`）即時運算，
輸出 shapely polygon（canvas 顯示）+ uint8 mask（SEM MM 量測）。

拆成 6 個 sub-milestone：

#### M2.1 — Layer cache 系統

目標：walker 跑完後把每個 layer 的 polygon 存成 .npz，下次啟動秒開不需要重新讀
OASIS。cache 同時儲存 metadata（chip corner offset、FOV 大小、來源 OASIS 檔名），
載入 cache 時自動帶入這些設定。

- [x] 設計 cache 格式（`tools/gds_layer_cache.py`，獨立於 oasis_cache）：
  - layer data：layer key (layer, datatype) → polygons + bboxes (N,4)（沿用 oasis_cache 的 pts/offs/bbs npz 打包，保留 polygon 而非只 (N,4)）
  - metadata：`LayerCacheMeta`（chip_corner_x/y、fov_w/h、source_oas + mtime + size、top_cell_name、nm_units）
- [x] `cache_save(path, layers, meta)`：選取 layer 的 polygon+bbox + metadata 一起原子寫成 .npz
- [x] `cache_load(path)`：載入 .npz，重建 layer list；GUI `_on_load_cache()` 自動帶入 chip corner / FOV 到 MainWindow 狀態（M3 panel 接手顯示）
- [x] cache 驗證：`check_source()` 比對 source_oas 檔名 + mtime + size，回 ok/missing/name_mismatch/stale_mtime
- [x] cache 只包含使用者選取（已載入文件）的 raw layer，不存 synthetic 與未載入 layer
- [x] GUI：toolbar「Export Cache…」/「Load Cache…」按鈕（含 `<stem>_expr.json` expression sidecar）

#### M2.2 — 空間查詢（FOV query）

目標：給定座標範圍，快速取出該 FOV 內的 polygon。

- [x] 實作 `query_fov(cx, cy, fov_w, fov_h, bboxes)`：用 numpy broadcast 過濾 bbox，不需要 R-tree；回傳 ndarray (M, 4)，M <= N（`tools/gds_fov.py`）
- [x] 支援多 layer 同時查詢：`query_fov_multi(..., layers={...}, keys=[(17,101),(6,0)])`
- [x] 效能目標：FOV 內幾百個 polygon，查詢 < 1ms（純 numpy mask，無 R-tree）
- [x] 輸入座標為 GDS 座標（nm，相對 chip corner）

#### M2.3 — 座標換算系統

目標：KLARF 座標（相對 die corner）→ GDS 座標（相對 chip corner）。

座標關係：

```
KLARF 座標系：die corner = (0, 0)
              XREL, YREL = 相對 die corner 的 nm 座標
GDS 座標系：  chip corner = (0, 0)
              chip_corner_x/y = chip corner 相對 die corner 的座標
                                （從 RFL 檔查得，使用者手動輸入）
```

換算公式（Y 軸目前假設同向，實測後再確認）：

```
GDS_x = XREL - chip_corner_x
GDS_y = YREL - chip_corner_y
```

- [x] 實作換算函式：`klarf_to_gds(xrel, yrel, chip_corner_x, chip_corner_y)` → `(gds_x, gds_y)`（`tools/gds_fov.py`，支援 scalar / ndarray）
- [x] **RFL → chip_corner**：`rfl_to_chip_corner(die_w_mm, die_h_mm, chip_cx_mm, chip_cy_mm, chip_w_mm, chip_h_mm)` → `(cc_x_nm, cc_y_nm)`（見 Q15；mm→nm + `die/2 + centre - chip/2`）
- [ ] chip_corner_x / chip_corner_y 由使用者從 RFL 檔手動查詢後輸入（GUI 端，M2.6 / M3）
- [x] 整合進 query_fov：`query_fov_klarf()` 直接吃 KLARF 座標輸入，內部呼叫 `klarf_to_gds()` 後再做空間查詢
- [x] Y 軸方向：目前假設 KLARF 與 GDS 同向（皆向上）；`klarf_to_gds(..., flip_y=True)` 預留實測後翻轉

#### M2.4 — Boolean 表達式語法設計

目標：定義表達式語言描述 layer 幾何運算，**只設計不實作**。

語法規則：

```
基本 layer 引用：大寫字母 A、B、C...
AND（交集）：         A & B
OR（聯集）：          A | B
差集：                A - B
補集：                ~A
grow（單邊向外擴張）： A > W:10  （10 = nm，四面各擴 10nm，總寬 +20nm）
shrink（單邊向內收縮）：A < H:10 （10 = nm，四面各縮 10nm）
群組：                (...)
完整範例：            L0 = [(A > W:10) & B] < H:10
```

Layer 綁定方式：

```
A = (17, 101)   # (layer, datatype)
B = (25, 0)
```

單位說明：

```
W:n 與 H:n 的 n 單位為 nm
GDS 座標本身已是 nm，shapely buffer(n) 直接對應 n nm
A > W:5 = 單邊 grow 5nm（shapely buffer(5)），四面各擴 5nm，總寬 +10nm
A < H:5 = 單邊 shrink 5nm（shapely buffer(-5)）
```

運算子優先順序（高到低）：

```
1. ~（補集，最高）
2. > W:n / < H:n（形態學）
3. &（AND）
4. | / -（OR / 差集，最低）
括號可覆蓋優先順序
```

- [x] 語法規則文件化（即上述內容）
- [x] 定義 layer 綁定格式
- [x] 定義運算子優先順序
- [x] 不實作 parser，只設計

#### M2.5 — Boolean 運算引擎

目標：實作表達式的解析與執行。

輸出形式（兩者都輸出）：

```
A. polygon list（shapely geometry）
   → 用於 GDS canvas 顯示，讓使用者確認 ROI 定義正確
B. numpy uint8 mask（跟 SEM 影像同尺寸）
   → 用於 SEM MM 量測，只在 mask 白色區域內找 defect
   → 白色 = ROI 區域，黑色 = 忽略區域
```

Parser 實作方式：

```
手寫遞迴下降 parser（約 50~80 行）
不使用 pyparsing，減少外部相依
解析結果為 AST（Abstract Syntax Tree）
```

運算子實作（用 shapely >= 2.0）：

```
& → shapely intersection()
| → shapely union()
- → shapely difference()
~ → shapely difference(FOV bbox polygon, A)
> W:n → shapely buffer(n)     # grow n nm（單邊）
< H:n → shapely buffer(-n)    # shrink n nm（單邊）
```

- [x] 手寫遞迴下降 parser：把表達式字串解析成 AST（`tools/gds_boolean.py` `parse_expression()`，含可選 `NAME =` 前綴）
- [x] AST evaluator：遞迴執行 AST，對 shapely geometry 做運算（`evaluate()`）
- [x] 執行範圍：只在當前 FOV 內的 polygon 上運算，不處理整個晶片
- [x] 輸入：FOV 內的 polygon list（來自 M2.2 query_fov）— `rects_to_geometry()` / `polys_to_geometry()` / `layer_geometry()` 把 bbox + polygon 轉 shapely
- [x] 輸出 A：shapely polygon list（供 canvas 顯示）— `geometry_to_polygons()`
- [x] 輸出 B：numpy uint8 mask，尺寸 = SEM 影像尺寸，需輸入 nm_per_pixel 換算 — `make_mask()`（cv2.fillPoly，含 hole 扣除 + invert_y）
- [x] 新增套件相依：shapely >= 2.0（已加進 requirements.txt）

#### M2.6 — GUI 整合

目標：在 gds_align_tool 的 LayerPanel 支援表達式 layer。

- [x] LayerPanel 加「+ Expression…」按鈕（`add_expression_requested` signal → MainWindow）
- [x] `ExpressionLayerDialog`：
  - 表達式輸入框（placeholder `[(A > W:5) & B] < H:5`）
  - 綁定區：依表達式 referenced letters 動態建 layer 下拉選單（A/B/…）
  - layer 名稱輸入框（預設 L0）
  - 預覽按鈕（在目前 canvas viewport 當 FOV 即時算並顯示結果）
- [x] Expression layer 在 LayerPanel 標 `[expr]` 前綴（`LayerKey.label()`）
- [x] Canvas 上顯示 expression layer（synthetic LayerEntry，沿用 _draw_layers 半透明填色 + 獨立調色）
- [x] 儲存 / 載入表達式定義（JSON 格式，存在 cache .npz 同目錄 `<stem>_expr.json`；load cache 時 re-evaluate 還原）
- [x] **驗證（offscreen smoke test 通過，互動 UI 待 user 本地驗證）：** `A & B`→area 5000、`A | B`→15000、`[(A > W:10) & B] < H:5`→4500（手算一致）；dialog binding rebuild、preview、cache export/import + expr restore 全通過。完整互動流程（滑鼠 / 渲染）需 user 本地跑 `python tools/gds_align_tool.py` 驗證

---

### M3: SEM 載入 + 雙視窗 + 自動座標換算  [status: implemented — UI + jump 完成 (sandbox + offscreen GUI smoke)；SEM-上 GDS overlay 延到 M4；互動 UI 待 user 本地驗證]

加入 SEM 影像端，與 GDS canvas 並排；點選影像時用 KLARF 座標自動換算到 GDS 位置並跳位。

- [x] `SemPanel`（右側）：QListWidget 列影像（image_id + 檔名 + 無座標標記）；上方按鈕「Load KLARF」/「Load Folder」（`tools/gds_align_tool.py`）
- [x] KLARF 載入：`tools/sem_loader.py` `load_klarf()` 用 `src/core/klarf_parser.py` 取 image 列表 + die-corner XREL/YREL + `_image_filename`（同目錄解析影像檔）
- [x] Load Folder：`load_folder()` 掃資料夾 PNG/TIF/JPG/BMP（無座標）
- [x] 中央改 `QSplitter`：左 GdsCanvas / 右 `SemViewer`（QPixmap 縮放 fit + 中心十字）
- [ ] `OverlayCanvas`：在 SEM 上半透明畫 GDS（POI layer outline）— **延到 M4**（需精確 alignment 後才有意義；M3 先 SEM 影像 + GDS canvas FOV 框雙邊對照）

Coordinate Setup 面板（一次性設定，存在 cache .npz 內）：

- [x] ~~RFL 六欄輸入（mm，Q15 模型）：die W/H、chip centre X/Y、chip W/H~~ → **改為 RFL Chip-offset 表（選項 A，2026-05-21）**：user 在 RFL 找到「Chip offset」表(DieX/DieY = chip corner、SizeW/SizeH、GDS default offset，全 µm)，klayout 驗證落點正確且比六參推導準 ~60µm。Panel ① 區改成直接抄這一行（Chip corner X/Y、Chip W/H、GDS offset X/Y，µm）；`_chip_corner_nm()` = `(DieX − GDS_off)×1000` nm，取代 `rfl_to_chip_corner` 公式
- [x] **DiePitch 自動帶入**：load KLARF 時 `sem_loader.read_die_pitch_nm()` 讀 DiePitch（nm→mm）填 die size；user 已填且不一致則 QMessageBox 警告並保留 user 值（Q15「兩者可比對」）
- [x] FOV 大小輸入：W / H SpinBox（nm）；載入 cache 時自動帶入
- [x] cache metadata 擴充：`LayerCacheMeta` 存 RFL Chip-offset 參數 `chip_x_um/chip_y_um/chip_w_um/chip_h_um/gds_off_x_um/gds_off_y_um`（SCHEMA_VERSION→4，選項 A 取代原六參 mm 欄）；`set_from_meta()` 載入還原；export 從 panel `values()` 取

Image List 行為：

- [x] 從 KLARF 讀取每張圖的 XREL / YREL（`SemImage.xrel/yrel`）
- [x] 點選 Image List 某張圖時：`_jump_to_image()` 呼叫 M2.3 `klarf_to_gds()` 自動換算 → `GdsCanvas.focus_fov()` 置中 + 縮放 + 畫 FOV 虛線框
- [x] 切換影像時自動更新 GDS canvas 位置與 FOV 框；無座標影像（folder）清掉 FOV 框

Fine tune 微調：

- [x] dx_nm / dy_nm SpinBox，range ±FOV（FOV 改變時 `_sync_fine_range()` 連動）；預設 0；改值即時 re-jump
- [x] **驗證（offscreen smoke 通過，互動 UI 待 user 本地驗證）：** set_chip_and_fov → load KLARF → 選影像 → canvas 置中 (500,500)+FOV 框；chip offset (100,200) → (5400,5300)；fine tune (50,-30) → (5450,5270)；fine range ±FOV；folder 無座標清框；canvas.grab() paint 無誤；`py_compile` ✓

**M3 UI 合併（2026-05-21, user D2DB ROI 驗證成功後）：**
- [x] **單一 GDS 入口**：移除舊「Open GDS / OASIS…」全載按鈕 + menu，toolbar 只留「Open OASIS…」走 `_on_open_roi`；`LayerFilterDialog(roi_mode=True)` 在 ROI 流程做 scan + 多選 layer（隱藏 partial-load knob），取代手打 `17/0, 6/0`。`_on_open`/`_start_load`/`LoadWorker` 全載引擎**保留代碼但無 UI 入口**（無 S_CELL_OFFSET 檔的潛在 fallback，待 §M3 待辦評估）。
- [x] **單一 SEM 入口**：SemPanel 兩個鈕（Load KLARF / Load Folder）併成一個「Load SEM…」+ QMenu（From KLARF file… / From image folder…）；KLARF 檔案對話框加 `.000` 及任意三位數字副檔名（`*.[0-9][0-9][0-9]`）。
- offscreen smoke：LayerFilterDialog(roi_mode) 隱藏 partial、標題/按鈕文案正確；SemPanel 合併鈕 + menu signal 正確；MainWindow toolbar 只剩單一 Open 入口。互動待 user 本地驗證。

**M3 auto-jump UX 修復（2026-05-21）：** user 報「點不同 DefectID，FOV 只在固定位置動一點點」。診斷 `tools/diag_klarf_coords.py` 對 user N9U179.03.000 證明 XREL/YREL 確實在變（Y 跨 11.6mm、X 0.66mm，1554/1187 unique），量級正確 nm → **非資料/尺度問題**。根因是 `focus_fov` 每次置中 + 固定縮放，框永遠在畫面中央 → 視覺上「不動」。
- [x] **選項 1**：跳位改 `set_fov_marker`（保持總覽、只移框，不置中放大）；`_fit_view_to_defects()` 在 load KLARF / open ROI 後 fit 視野到所有 defect 的 GDS bbox，框在這片總覽上明顯跳動。`GdsCanvas.set_view_to_bbox` 新增；`focus_fov` 移除。
- [x] **選項 2**：`_on_sem_image_selected`（點圖）自動後台載入該處 ROI 幾何；放在點選而非 `_jump_to_image`，避免 fine-tune 微調每按鍵重跑 parser。
- 張力：總覽看框跳 vs 看清局部幾何無法同時 → 兩段式（總覽定位 + 手動放大看細節）。offscreen smoke 通過，互動待 user 驗證。

**M3 待辦（user 指派）：**
- [x] **座標對位 KLARF→GDS 落點正確** — user 確認沒問題了（2026-05-22）。RFL Chip-offset(選項 A) + `klarf_to_gds(flip_y=False)` + auto-jump UX 修復後落點對。殘差用 M4a 拖動 / M4b template match 收。
- [x] **SEM↔GDS overlay** — M4a 已完成（SEM 上半透明疊 GDS + 拖動）。
- [x] **操作順序引導** — M6.6 已完成（toolbar 下 `_guidance` Step 1–5）。
- [x] **全載引擎去留（2026-05-23, user 決議「整段移除」）：工具改為 OASIS-only**。移除 `_on_open`/`_start_load`/`_cleanup_load`/`_on_load_*`/`LoadWorker`/`_gds_loader_main`/`_child_log`/`_load_with_oasis_streamer`/`_load_with_klayout`/`_load_with_gdstk`/`GdsDocument.load`/`GdsLoadCancelled`/LayerFilterDialog partial 旋鈕 + `max_records()`、刪 `tools/oasis_cache.py` + `tests/test_oasis_cache.py`、scan 改 OASIS-only（移除 `_scan_gds_with_klayout` + klayout/gdstk import guard + `_backend_install_hint`）、`requirements.txt` 移除 klayout/gdstk。共 −889+−107+−42+−119 行。放棄 .gds / 無 S_CELL_OFFSET OASIS 支援（user 接受）。ROI scan + LoadProgressDialog + RoiWalkWorker 保留。460 passed（4 pre-existing pandas/cmg 失敗無關），offscreen launch + ROI dialog smoke OK。
- [x] **diag_* 歸類**：`tools/diag_klarf_coords.py` / `tools/diag_layer_bbox.py` 移到 `tools/diag/`（修 `_REPO` 路徑 + usage 字串）。
- [ ] RFL 六參數可從 RFL 檔匯入（M6.6 標 optional，需 user 提供實際 RFL 格式）。（保留）

---

### M3.5: ROI-bounded GDS load — random-access via S_CELL_OFFSET  [status: implemented (M3.5a–d, sandbox + offscreen smoke); 互動 + D2DB 實測待 user]

> **User 決議（2026-05-20）：** 完整大檔 load 太慢沒意義；blind partial-5M 又會截斷 geometry。改為「給定 SEM image 的 GDS 座標 + FOV，只載入該 ROI 附近的 geometry」。
>
> **關鍵限制（2026-05-20 釐清）：** 「walk 階段 ROI 裁剪」只能加速展開/輸出，**無法減少 decode 整檔的時間**（OASIS 循序流，每 record 都要 decode 才知下一個位置）。要「第一次就又快又完整」只能靠檔案內建 per-cell byte offset 做隨機存取。
>
> **診斷結果（2026-05-20，user 跑 `diag_oasis_offsets.py` on D2DB）：GO** — 13,276/13,276 cellname 全有 `S_CELL_OFFSET`；START offset_flag=0，name-table offsets present。→ **走 random-access（option B）**。

**演算法（top-down demand-driven）：**
1. 讀 name table（<1s）→ 建 `{cell → byte_offset}`（S_CELL_OFFSET value）+ cellname refnum↔name
2. 從 root cell 開始 DFS：`seek(offset)` 只 decode 該 cell 的 geometry + placements + local bbox（memoize，每個 unique cell 至多 decode 一次）
3. 每個 placement：展開 repetition → 用 child cell 的 transformed bbox 對 ROI 做 intersection 剪枝（array 整批 anchor 剪枝），不相交就 skip，不往下 decode
4. 命中的 geometry 套累積 transform → root 座標，clip 到 ROI bbox
5. 因為積極剪枝，只 decode 與 ROI 相交子樹的少數 cell，避免碰整檔

**Sub-tasks：**
- [ ] **M3.5a** streamer 抓 S_CELL_OFFSET value（目前 `_read_property` 跳過 value）→ `scan_cell_offsets(path) → {cell: offset}`；驗證：seek 到 offset 確認該處是 CELL record
- [x] **M3.5b** random-access 單 cell decoder：`oasis_random.RandomAccessReader.load_cell(cell_id)` → `CellContent`(rects/polys/placements/local_bbox)，memoized；seek 前 `clear_substreams()`，CELL boundary 重置 modal 故 mid-stream seek 安全；layer filter 丟幾何但保 placement
- [x] **M3.5c** top-down ROI walker：`oasis_random.walk_roi(rar, root, roi_bbox, layer, datatype)` → root 座標 rects/polys + `RoiWalkStats`；`reachable_bbox`（memoized，cycle 防呆）做 child-bbox ROI 剪枝，repetition offset 用 numpy 向量化算 root bbox 後 mask 剪枝（只 recurse 命中 ROI 的 instance）；重用 `oasis_walker.Transform`
- [x] **M3.5d** 接進 gds_align_tool：toolbar「Open OASIS (ROI)…」→ 選檔 + layer + root cell（cellname 清單，heuristic 預設）建持久 `RandomAccessReader`；點 SEM image → `_jump_to_image` 算 ROI（FOV + half-FOV margin）→ `roi_document_from_reader()` walk_roi → 建 `OASIS-ROI` GdsDocument 顯示 + status 印 rects/pruned。`build_roi_document()`/`roi_document_from_reader()` headless-friendly（offscreen smoke 通過）。cache key 記 ROI **待 M2.1 整合做**（目前每次 click 即時 walk，已夠快）
- [x] **M3.5d.1 背景化（2026-05-20）**：user 實測首次 ROI 凍結 UI。根因：`reachable_bbox` 為了剪枝需 decode root 可達的**所有** unique cell（OASIS 無 per-cell bbox），同步跑在 UI thread → 凍結。改 `RoiWalkWorker`(QThread) + `LoadProgressDialog`（顯示已掃 cell 數）+ cancel；`walk_roi(cancel_cb=)` 拋 `WalkCancelled`。UI 不再凍結、可取消、session 內第二次 click 走 memo 快取。
- [x] **M3.5e.1 lazy repetition（2026-05-20）**：根因 = placement repetition 被 eager 展開成百萬元素 Python list（reachable_bbox 只為取 min/max）+ `_decode_at` 漏掉 RECTANGLE repetition（丟幾何）。修：`OasisReader(defer_placement_repetition=True)` 讓 placement 存 compact `repetition_raw`；新增 `repetition_extent/count/offsets_np`（解析式 extent，grid 用 numpy meshgrid）；`reachable_bbox` 用解析 extent（不 materialize）；`walk` 先用 extent 整批剪枝（array 在 ROI 外 → instant，不 materialize），命中才 numpy 展開做 per-instance 剪枝；`_decode_at` 正確展開 RECTANGLE/POLYGON repetition。進度對話框顯示 `N/≤total cells (X%)`。store 77.7s→？待 user 重測。1M-instance array：ROI 外 0.34ms、ROI 內 1.4s、bit-correct。
- [x] **M3.5e.3 CE 邊界層 early-stop 讀取（2026-05-21, GO 已實作）**：診斷工具修抽樣偏差（原抽 span 最小者全是 placement-only 容器，verdict 0/12 為偽陰；改抽 span 大者優先 + 湊滿 N 個有矩形 cell + 檢查候選矩形 extent == cell 全 bbox + layout 探針回報 `records/ce@index/placements/last_place`）。**User D2DB 實測 GO**：(108,250) 在 12/12 幾何 cell 皆「恰 1 矩形 == cell 全 bbox」，且記錄順序固定為 `placements → CE rect(ce@13–54) → 海量 device(15–21萬條)`，`last_place < ce_index` 全滿足 → early-stop sound 且 CE 落在前 0.026% → **cheap**。
  **實作**：`RandomAccessReader(bbox_layer=(108,250))`（並入 wanted_layers 確保不被過濾）+ `load_cell_bbox()`／`_decode_bbox_at()`：decode 到邊界矩形即停，回 placements + own bbox（CE rect），跳過後續 device 幾何；無 CE 的純容器 cell 讀到底（便宜）。`walk_roi` 的 `reachable_bbox` 改用 `load_cell_bbox`（剪枝），`walk` 仍用完整 `load_cell`（只對 ROI 內 cell）。GUI 兩處 reader 構造傳 `bbox_layer=DEFAULT_BBOX_LAYER`。`reachable_bbox` decode 量從每 cell ~20 萬條降到 ~50 條。
  **測試**：新增 `TestCeBoundaryEarlyStop`(4)：early-stop 不含 device rects、reachable_bbox 與 child union、**與全 decode bit-identical**、ROI 外經 CE bbox 剪枝。oasis suite 204 passed。
  **User D2DB 實測（2026-05-21）：成功**。`--debug` 顯示首層 17/0 = 11.0s（掃 13,283 cell 算 bbox，每 cell ~50 條 vs 舊 ~20 萬條，0.83ms/cell ≈ 舊 0.13s/cell 的 160×），ROI 內 25 rect、`reader_errors=0`、`pruned=103140`。canvas 正確顯示 GDS。
  **後續優化（reach_memo 跨 walk 複用）**：實測後續層各 5–7s 且 `newly_decoded_cells=0`，成本全在 `reachable_bbox` 遞迴重算（每次 walk 重建局部 memo）。`reachable_bbox(cid)` 為 cid-local、與 ROI/layer/image 無關 → 提升到 reader 級 `_reach_memo` 跨所有 walk 複用：首層付一次遍歷，後續 layer + 切換 image 的剪枝 pass 全 memo 命中（測試 `test_reach_memo_reused_across_walks` 驗證第二次 walk 不再呼叫 loader）。oasis suite 205 passed。**待 user 驗證後續 layer / 切 image 是否降到 <1s**。
- [ ] **M3.5e.2（保留作 fallback，目前不需）**：把 per-cell reachable-bbox 索引存 .npz cache。CE early-stop 已讓首載秒級，此項僅在未來遇到無 CE 邊界層的檔案時才需要。
- [x] **驗證：** 對 D2DB 給 SEM 座標 + FOV → 載入秒級（<< 全檔/partial-5M），ROI 內 geometry 與全載 bit-identical — **user 確認 M3.5 完成（2026-05-22）**（CE early-stop 首層 11s/後續 memo 命中，canvas 正確顯示）

---

### M4a: SEM↔GDS overlay 拖動對齊 + Set Offset（δ 校準）  [status: implemented (sandbox + offscreen smoke); 互動 + 真實檔拖動待 user]

> **User 決議（2026-05-21）：** 先做手動拖動對齊(取代/先於 template match)。座標除錯結論：KLARF(P 世界)與 GDS(G 世界)間有常數偏移 δ(= align mark P vs die 幾何角 G + GDS 檔原點小 offset)，δ 不在任何單方資料裡，只能「同一顆 defect 在兩世界各測一次」求得。最直觀的測法 = 把 GDS 半透明疊在 SEM image 上、滑鼠拖到對齊、按 Set Offset 自動填 δ。
>
> **決定（AskUserQuestion 2026-05-21）：** nm_per_pixel **自動算(FOV÷影像像素) + 可手填覆寫**；δ **全域一個**(存 Coordinate Setup + cache)。

- [x] **M4a.1 nm_per_pixel**：`CoordinateSetupPanel` 加「nm per pixel」欄 + 「auto = FOV ÷ image px」勾選框。`MainWindow._effective_nm_per_px()`：auto → `fov_w / sem_viewer.native_size()[0]`;手動 → 填入值。
- [x] **M4a.2 GDS-on-SEM overlay 渲染**：`SemViewer.set_overlay(entries, anchor, nm_per_px)` 把 doc 的 visible polygon 半透明描邊畫在 SEM image 上。映射 `screen = img_centre + ((px,py) − eff_anchor)/nm_per_px·s`(s = 顯示/原生像素比),`eff_anchor = anchor − drag`,Y 翻轉。
- [x] **M4a.3 拖動**：`SemViewer` mousePress/Move/Release 累積 `_drag_x/y`(nm = 螢幕位移×nm_per_px÷s),即時重畫 + `drag_changed` signal(MainWindow 即時預覽 δ)。
- [x] **M4a.4 Set / Clear Offset 按鈕**(中央 viewer 下方一排)：Set 把拖動併入全域 `_origin_dx/dy`(`origin -= drag`,符合 anchor 折疊不變式),清拖動、重新定位 + fit;Clear 歸零。
- [x] **M4a.5 δ 套用 + cache**：`_jump_to_image` / `_current_image_gds` / `_fit_view_to_defects` 的 gds 都加 `_origin_dx/dy`(與 fine-tune 同鏈、語義為原點補正)。`LayerCacheMeta` 加 `origin_dx/dy` + `nm_per_px`(SCHEMA_VERSION 2→3),export 寫入 / load 還原。
- [x] **驗證(offscreen smoke 通過 → 2026-05-22 改為 committed 回歸測試)**：SemViewer 折疊不變式 `render(anchor,drag)==render(anchor−drag,0)` ✓;Set Offset 把 drag(300,−150) 折成 origin(−300,150) 且 drag 歸零 ✓;Clear 歸零 ✓;cache round-trip origin/nm_per_px + RFL Chip-offset µm 六參 ✓。**這些不變式現已鎖進 `tests/test_gds_align_m4a.py`(17, PyQt6 offscreen，無 Qt 環境自動 skip) + `tests/test_gds_layer_cache.py` 新增 2 個 δ/RFL round-trip test**，取代原 ad-hoc smoke。**仍待 user 本地拖動實測 δ → 確認多數 in-chip defect 落點對齊。**

設計決定(預設,可改)：overlay 畫在右側 SEM viewer(SEM 底、GDS 半透明前景)；拖動只調平移(δ)，不調縮放/旋轉(對不上代表 nm_per_px/FOV 要修)；δ 與 fine-tune 分開(δ range ±die、fine ±FOV)。

---

### M4b: POI Template + 自動 Fine Alignment  [status: implemented — 單張 + Run all (sandbox + offscreen, 23 tests)；互動待 user]

選 POI layer + 給 GLV 產 template，對 SEM 跑 `cv2.matchTemplate` 在 coarse 附近 refine（自動求 δ/殘差，取代人工拖動）。

> **User 決議（2026-05-22, AskUserQuestion）：** 單張 fine-align（對目前已載入 ROI 的影像）先完整做；「Run all」整個 dataset 批次延後（大檔每張都要 ROI walk 太慢）。

- [x] LayerPanel 每列加「P」POI 切換鈕（`_LayerRow.poi_btn` + `poi_toggled`）；`LayerPanel._on_poi_toggled` 手動互斥（同時只能一個 POI）+ `poi_changed` signal
- [x] POI 設定 + fine-align 控制集中在右側 `FineAlignPanel`（QGroupBox）：`fg_glv`(200) / `bg_glv`(80) / `blur_sigma_px`(1.0)、`Search radius (nm)`(200)、`Score threshold`(0.5)、`Run fine align` 鈕、結果 label。`make_template(mask, fg, bg, blur) -> uint8`（cv2.GaussianBlur）
- [x] `render_poi_template(polys, anchor, W, H, nm_per_px, fg, bg, blur)`：POI 在 coarse anchor 的 FOV 內 rasterize 成 SEM 尺寸 grey template
- [x] `fine_align_one(sem_img, template_full, nm_per_px, search_radius_px)` → `(dx_nm, dy_nm, score, used_r)`：center-crop template(留 search_radius 邊界)→ `cv2.matchTemplate(TM_CCOEFF_NORMED)` → `minMaxLoc` + 3-point parabola subpixel(`_parabola_subpx`)。回 anchor 修正量（sign：image x 右 = GDS x，anchor.x 減；image y 下 = GDS y 上，anchor.y 加）
- [x] 搜尋中心 = M3 自動換算座標（`klarf_to_gds` + fine + δ，**排除** refined 本身，每次從 coarse 起算）
- [x] fine align 完成 → `self._refined[image_id]=(dx,dy,score)`；`_refined_offset()` 併入 `_jump_to_image` / `_current_image_gds`，overlay + FOV marker 自動切 refined；`reset_drag` 後 re-jump
- [x] Per-image 結果 dict `{image_id: (dx, dy, score)}`；`SemPanel.set_score()` 在 list 行尾顯示 `[score]` + 紅(<thr−0.2)/黃/綠(≥thr) 著色；`FineAlignPanel.set_result()` 同色顯示 score + Δ
- [x] cv2 缺 / 無 POI / 無座標影像 / nm_per_px≤0 → 各自 graceful no-op + status 提示
- [x] **「Run all」按鈕（2026-05-23 實作）**：整個 dataset 批次 fine（`FineAlignAllWorker` QThread + `LoadProgressDialog` + cancel）。headless `poi_polys_for_roi(rar, root, roi, poi_spec)`（raw → `walk_roi`；expr → 走各 bound layer + Boolean eval）讓每張圖在自己的 ROI 重算 POI；worker 逐張 walk→render→matchTemplate，emit per-image result + progress；主執行緒存 `_refined` + list 著色，結束 re-jump。`_poi_spec()` 把 POI entry 轉 ('raw',l,d) 或 ('expr',text,bindings)。執行時 disable 兩個 run 鈕。reach_memo 跨 walk 複用故後續較快。大檔仍可能慢（user 已知），可取消。
- [x] **驗證（sandbox + offscreen, `tests/test_gds_align_m4b.py` 15）：** make_template glv/blur、render 尺寸+置中、parabola(對稱 0/偏移/邊界)、fine_align_one(對齊 0、+4x+3y→dx=−20/dy=+15、flat no-signal、size mismatch raise、**apply-correction-realigns 不變式**：套 (dx,dy) 後重 render == SEM)、POI 互斥+run enable、end-to-end run fine align(refined=−500/+300、score>0.9、anchor 反映、list 顯示 score)、no-POI no-op。全 GDS+F2 suite **374 passed**；`py_compile` ✓；assembled-window render-grab(main/sempanel/finealign) 無 crash。互動（真實 SEM 對位、score 著色、overlay 切位）待 user 本地驗

---

### M6: 單一大視窗 UX — SEM+overlay 為主視圖、GDS 總覽可折疊、每層獨立透明度  [status: implemented (sandbox + offscreen smoke/render, 16 tests); 互動 UI 待 user 本地驗證]

> **User 決議（2026-05-22, AskUserQuestion）：** ① GDS 總覽 canvas **保留但可折疊**（預設只顯示 SEM+overlay 大視窗，按鈕叫出總覽）② overlay 透明度 **每層獨立**（隱藏沿用 LayerPanel 點選）③ 側邊面板（LayerPanel / SemPanel）**保留**。

**動機：** 現在中央是「GdsCanvas（GDS 總覽 pan/zoom + FOV marker）｜ SemViewer（SEM image + GDS overlay 拖動）」左右雙視圖。alignment 打通後，使用者實際盯著看的是 SEM+overlay；雙視圖各佔半邊很擠。改成以 SEM+overlay 為單一大視圖、GDS 總覽收成可折疊次視圖，並讓每個 layer 各自調透明度，方便對位時看清 SEM 底圖。

#### M6.1 — LayerEntry 每層透明度欄 + 兩視圖套用

- [x] `LayerEntry` 加 `opacity: int = 35`（0–100 %）+ `fill_alpha()`（`round(opacity/100*255)`，clamp 0–255）。預設 35% → alpha 89
- [x] `GdsCanvas._draw_layers`：fill alpha 從寫死 `110` 改 `entry.fill_alpha()`；outline pen 維持細線（恆可見，利對位）
- [x] `SemViewer` overlay：fill alpha 從寫死 `70` 改用 entry alpha；`set_overlay` entries 由 `(polys, color)` 擴成 `(polys, color, alpha)`；`_update_overlay` 用 `e.fill_alpha()` 組
- [x] **per-session only**（不進 cache）

#### M6.2 — LayerPanel 每層 row widget（checkbox + 色塊 + opacity slider）

- [x] QListWidget 由 delegate-painting 改為 `setItemWidget` per-row `_LayerRow` widget：`[✓可見 checkbox][色塊按鈕(點=改色)][label·polyN][opacity slider 0–100 + % label]`
- [x] checkbox toggled → `entry.visible` → `changed`；色塊點擊 → QColorDialog → `entry.color`（cancel 保留原色）；slider valueChanged → `entry.opacity` → `changed`；`_LayerRow.changed` 接到 `LayerPanel.layers_changed`
- [x] 移除 `_LayerItemDelegate` + 不再用的 QStyle/QStyledItemDelegate/QStyleOptionViewItem import；`set_document` 改建 row widget
- [x] hint 文案改「checkbox: 顯示／隱藏 · slider: 透明度 · 點色塊: 換色」

#### M6.3 — 中央版面：SEM+overlay 主視圖 + 可折疊 GDS 總覽

- [x] 預設 `GdsCanvas` 隱藏（`setVisible(False)`）、`SemViewer` 佔滿中央
- [x] toolbar 加 checkable button「GDS 總覽」：toggle `canvas.setVisible()`；顯示時 `_center_split.setSizes()` 50/50
- [x] canvas 隱藏時 `set_fov_marker` / auto-jump / Goto 照常呼叫（只是看不到），render-grab smoke 不報錯；Set/Clear Offset row 不受影響。新增 `_on_layers_changed`（canvas.refresh + `_update_overlay`）讓圖層變更同步刷新 SEM overlay（修補既有「toggle 可見性不更新 overlay」小缺口）

#### M6.4 — 驗證

- [x] **offscreen 回歸測試** `tests/test_gds_align_m6.py`（16）：fill_alpha 映射 + clamp、`_LayerRow` slider/checkbox/色塊（含 cancel）連動 entry + emit、`LayerPanel.set_document` 建 row widget、canvas 預設 hidden + toggle、overlay 帶 per-layer alpha、`_on_layers_changed` 刷新、隱藏層排除
- [x] 既有 F2 suite 不退化：**361 passed**（345 + 16 m6）；render-grab smoke（sem_viewer / canvas / layer_panel）無 crash
- [x] `python3 -m py_compile tools/gds_align_tool.py` ✓
- [ ] 互動 UI（滑鼠拖動、slider 即時重繪、折疊）待 user 本地 `python tools/gds_align_tool.py` 驗證

#### M6.5 — SemViewer 滾輪縮放 + 平移（user 要求, 2026-05-22）  [status: implemented (sandbox + offscreen, 8 tests); 互動待 user]

合併成單一大視窗後，SEM+overlay 變成主要工作區，需要能放大看清對位細節。

- [x] `SemViewer` 加 `_view_zoom`（fit=1.0, clamp 0.2–60）+ `_pan_x/_pan_y`；`_compute_geometry()` 算 image 放置（zoom+pan，paint 前也可用）
- [x] `paintEvent` 改手動 `drawPixmap(QRectF target, ...)`（取代 `scaled()` fit），套 zoom/pan；overlay `_world_to_view` 公式不變（用 `_img_rect` 中心 + `_scale`，自動跟著縮放/平移）
- [x] `wheelEvent`：以游標為中心縮放（游標下的 image 點固定不動）；左鍵維持 overlay 對位拖動、**中/右鍵拖動平移**、雙擊 `reset_view()` 回 fit
- [x] `set_image` 重設 view（每張圖從 fit 開始）；hint 文案更新（左鍵對齊/滾輪縮放/中右鍵平移/雙擊重設）
- [x] 回歸測試 `TestSemViewerZoomPan`(8, 併入 `tests/test_gds_align_m4a.py`)：default fit、wheel-in 游標點守恆、wheel-out、clamp、reset、set_image 重設、右鍵平移、左鍵只對位不平移。fold 不變式不受影響（公式未動）

**Risks：**
- LayerPanel 由 delegate 改 row widget 是中度改寫，外觀會變（接受，換取可放 slider）。
- per-layer opacity 暫不進 cache（per-session）；若 user 要持久化再 bump schema。
- 隱藏 GdsCanvas 後失去「即時看 FOV marker 跨 chip 跳」的總覽感 → 用 toggle 隨時叫回補償。

#### M6.6 — UX 友善化批次（user 要求, 2026-05-22, AskUserQuestion 複選全選 + UI 統一英文）  [status: implemented (sandbox + offscreen, 18 tests); 互動待 user]

- [x] **UI 統一英文**：掃出 26 處中文/混雜 UI 字串（tooltip / hint / label / 按鈕 / 狀態）全改英文；只剩開發註解為中文（非 UI）。grep 確認非註解行 0 CJK
- [x] **Viewer 角落即時資訊**：`SemViewer` `setMouseTracking(True)` + `_cursor_screen` + `_view_to_world()`（`_world_to_view` 逆映射）；`_draw_hud()` 畫右上 zoom 倍率(`x.xx×`)、左上游標 GDS 座標(µm)、右下比例尺（`_nice_round` 取 1/2/5×10ⁿ）；`leaveEvent` 清游標
- [x] **操作順序引導**：toolbar 下方 `_guidance` 條，`_update_guidance()` 依狀態（rar/doc → sem_images → FOV → current_sem）顯示 Step 1–5；在 open ROI / load SEM / coord changed / image selected / __init__ 連動更新
- [x] **Coordinate Setup 易用性**：已有的分組(①–⑤) + live chip-corner 預覽保留；corner label 加顯示 µm；頂部加一行 intro 說明（one-time setup / 從 RFL 抄）。**RFL 檔匯入未做**（需 user 提供實際 RFL 格式 + 標記 optional）
- [x] **鍵盤快捷 / 微調**：`QShortcut` — Ctrl+0 reset view、Ctrl++/=/− zoom（`SemViewer.zoom_by()` 以中心縮放，重構共用 `_apply_zoom`）、G toggle GDS 總覽、Ctrl+方向鍵 nudge 原點 δ（`_nudge_origin`，每步 10 nm + re-jump）
- [x] **驗證**：`tests/test_gds_align_m6_6.py`(18)：nice_round / view_to_world 逆映射+round-trip+leave 清除 / zoom_by clamp / guidance 5 步進+英文 / nudge δ / overview toggle / corner µm。全 F2+GDS suite **387 passed**；assembled-window render-grab 無 crash；`py_compile` ✓
- [ ] 互動 UI（滑鼠即時 readout、快捷鍵實按、引導條動態）待 user 本地驗證

---

### M5: Export + 後續整合 hook  [status: implemented (CSV/JSON, sandbox + offscreen, 10 tests); .gds 匯出延後；互動待 user]

匯出 per-image alignment 結果與合成 layer，定義跟主程式 Recipe 的對接 schema。

- [x] Export dialog（`AlignmentExportDialog`）：格式下拉（CSV / JSON）+ image checklist（預設全選 + Select all/none）
- [x] CSV 欄位（`ALIGNMENT_COLUMNS`）：`image_id, klarf_path, gds_path, poi_layer, coarse_dx_nm, coarse_dy_nm, fine_dx_nm, fine_dy_nm, score, nm_per_px`。`coarse_dx/dy` = FOV 中心 GDS（klarf_to_gds + fine + δ，**排除** refined）；`fine_dx/dy` = M4b template-match 修正量；無座標 / 未跑 fine 的格留空白（非 0，round-trip 保留語義）
- [x] JSON 同欄位（`alignments` array）+ `schema: mmh-gds-alignment-v1` + `columns` + `synthetic_layers: [{name, expr, bindings}]`（Boolean 譜系，取自 expression layer）
- [ ] 選配：合成 layer 匯出成新 .gds — **延後**：expression layer 是 FOV-local（即時運算），非全域幾何，匯成 .gds 意義低；docstring 已註記
- [x] `tools/gds_align_tool.py` 頂部 docstring 寫清楚對接 schema（image_id ↔ MeasurementRecord.image_id，每欄說明，aligned = coarse + fine）
- [x] **驗證（`tests/test_gds_align_m5.py` 10）：** alignment_rows(有/無座標、有/無 refined、blank 格)、synthetic_layer_specs、CSV/JSON round-trip(欄位齊全 == ALIGNMENT_COLUMNS、blank 保留)、ExportDialog(預設全選 / select-none + format)、MainWindow `_coarse_gds`(排除 refined)。全 GDS+F2 suite **179 passed**；`py_compile` ✓；dialog render-grab 無 crash。pandas 讀回需 user 本地（sandbox 無 pandas，已用 csv module 驗欄位）。互動（toolbar Export Alignment… → 存檔）待 user 本地驗

---

### M7: UI/UX 優化  [status: implemented (sandbox + offscreen, 15 tests)；互動待 user]

> see @docs/plans 之外的 planning doc（已核准）。純呈現層，不改行為。user 確認優先：版面重整 + 視覺一致性 + toolbar 分組/圖示；Coordinate Setup 可折疊。

- [x] **M7.1 右欄解擠**：soft-import `src/gui/collapsible.CollapsibleSection`（+ fallback）；把 `CoordinateSetupPanel` / `FineAlignPanel` 各包進 `CollapsibleSection`（`SemPanel._wrap_section`，清掉內層 QGroupBox title 避免重複），**保留 `self.coord_setup` / `self.fine_align` 參照**。右欄重排：Load SEM → Coordinate Setup(可折疊) → image list(stretch) → Load ROI → Fine Align(可折疊)。Coordinate Setup 預設展開，`_maybe_collapse_coord_setup`（FOV 有效後第一次跳位）或 load cache 後自動收起一次（`_coord_collapsed_once` 旗標，re-expand 不再被收）；Fine Align 預設收起，選 POI 時展開。
- [x] **M7.2 視覺一致性**：新增語意/結構 token（`_TK_SUCCESS`/`_TK_DANGER`/`_TK_SECTION_HEAD`/`_TK_GUIDANCE_*`/`_TK_TOOLBAR_BG`）+ 字級常數（`_FS_MICRO/CAPTION/LABEL/BODY/TITLE`，對齊 styles.py）+ helper（`_hint_qss`/`_result_qss`）。把約 15 處散落 inline `setStyleSheet` 寫死色/字級換成 token/helper；fine-align 結果 + expression result/error 改由 `_TK_SUCCESS`/`_TK_DANGER` 驅動；swatch radius 3→5px。（LoadProgressDialog 內聚 QSS 區塊保留。）
- [x] **M7.3 Toolbar 分組 + 圖示**：toolbar 用 VLine 分隔成 File（Open OASIS / Load Cache / Export Cache）/ View（GDS overview / Fit view / Goto µm）/ Export（Export Alignment）；新增 7 個 Lucide 風格 SVG 到 `src/gui/icons/`（folder-open, folder, save, download, maximize, layers, target），用 soft-import 的 `qicon()` 套用（缺檔回空 icon fallback）。Goto 改 icon-only。
- [x] **驗證（`tests/test_gds_align_m7.py` 15）：** 折疊包裝後 panel 參照仍在、初始折疊狀態、POI→Fine 展開、jump/cache→Coord 收起一次、無效 FOV 不收、token helper QSS、fine result 語意色、7 個新 icon 非空。全 GDS+F2 suite **207 passed**；`py_compile` ✓；assembled-window render-grab(main/sem/coord/fine) 無 crash。互動（折疊手感、icon 外觀、toolbar 群組、整體一致性）待 user 本地驗。

---

## Affected Files

預期會改動或新增：

- **新增：** `tools/gds_align_tool.py`（單檔，預估 1500–2500 行；仿 histogram_analyzer 結構）
- **新增：** `tools/gds_fov.py`（M2.2/M2.3）、`tools/gds_boolean.py`（M2.5）、`tools/gds_layer_cache.py`（M2.1）、`tools/sem_loader.py`（M3）
- **修改：** `requirements.txt`（加 `shapely>=2.0`）
- **修改：** `CLAUDE.md` §8（[F2] 進行中→完成後移除）
- **修改：** `SESSION_LOG.md`（每個 milestone 完成後一筆）
- **參考（不改）：** `src/gui/styles.py`、`src/core/klarf_parser.py`、`tools/histogram_analyzer.py`

不在 scope（留給主程式整合階段，另開 plan）：

- `src/core/recipes/cmg_recipe.py` 改用 GDS-anchored ROI
- `src/gui/workspaces/measure_workspace.py` 顯示 GDS overlay
- `MeasurementRecord` 加 designed_cd_nm 欄位
- `tests/` 新增 GDS 相關測試（第一階段以 user 手動驗證為主）

---

## Risks / Open Questions

### 已知風險

1. **GDS 大檔效能**：實際 GDS 可能含百萬 polygon。Mitigation：強制 flatten 到 top cell + 只 rasterize bbox 內的 polygon；視需要加 spatial index (gdstk 的 `Cell.bounding_box()`)。
2. **POI GLV 不夠 robust**：MG 在某些 SEM contrast 下未必最亮。Mitigation：M4 預留 `blur_sigma_px` 與「invert template」toggle；fine align 失敗時 score 會直接反映。
3. **Sub-pixel refine 精度**：parabola fit 對低對比 ROI 可能跳。Mitigation：score 低於 threshold 標紅，不採納。
4. **`gdstk` 安裝**：Windows 可能要 build tool。Mitigation：requirements 標 optional + 工具啟動時 try/except 給安裝指引；fallback 留 gdspy hook。
5. **座標換算精度**：KLARF die corner 為原點 (0,0)，chip corner offset 由使用者從 RFL 檔手動輸入。理論殘差應 < 1 FOV，但 stage 誤差、機台誤差可能造成初始 overlay 位置偏移。Mitigation：M3 保留 fine tune SpinBox（±1 FOV）供人工微調；M4 的 matchTemplate fine alignment 負責自動修正殘差。
6. **Y 軸方向**：KLARF 與 GDS 座標系 Y 軸方向目前假設同向（皆向上）。若實測發現方向相反，需在 M2.3 `klarf_to_gds()` 加入 Y flip。Mitigation：第一次實測時用已知座標的影像驗證方向。

### 待 user 後續確認

- [ ] M3 SEM 載入是否真的讀 KLARF？或先支援「資料夾載 PNG/TIF」就好（user 提到 KLARF 邊界，但載入媒介可能再議）
- [ ] M5 是否要把 alignment 結果寫進 `~/.mmh/runs.db`（跟 batch run 關聯）？第一版先存獨立 CSV/JSON，整合階段再決定。

已決定（不再 open）：

- M4 fine align 第一版只搜 translation，不做 rotation。
- M3 座標換算 Y 軸方向：KLARF 與 GDS 目前假設同向，實測後若有 flip 在 M2.3 調整。
- Coordinate Setup（chip corner offset + FOV 大小）與 cache (.npz) 一起存，載入 cache 時自動帶入。

### 外部依賴

- `shapely>=2.0`（新增，M2.5 Boolean 運算引擎）
- 既有：`PyQt6 >= 6.5`、`opencv-python >= 4.8`、`numpy >= 1.24`

---

## 驗證方式

整個 feature 結束時的 end-to-end 驗證：

- [ ] 所有 M1–M5 milestone checkbox 已勾
- [ ] `python3 -m py_compile tools/gds_align_tool.py` 通過
- [ ] **手動驗證**：
  1. 啟動 `python tools/gds_align_tool.py`
  2. 載入一份真實 GDS → layer panel 完整列出 → 切顯示色塊
  3. Expression layer：輸入表達式（如 `[(A > W:5) & B] < H:5`）綁定 layer → 命名 → 出現在 panel（標 `[expr]`）
  4. 載入對應 KLARF（或資料夾的 SEM）→ 影像列表正確
  5. Key coarse (dx, dy) → overlay 大致對齊
  6. 設 POI（合成的 alignment layer）+ GLV → 產 template
  7. Run fine align（單張 + 全部）→ refined offset + score 顯示
  8. Export CSV → 用 pandas 讀回欄位齊全
- [ ] `SESSION_LOG.md` 每個 milestone 完成有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F2]`
- 從 `CLAUDE.md` §8 移除 [F2]
- **本檔保留**作為 design history
- 開新 plan `[F3]` 規劃整合進主程式 Recipe pipeline（取代 / 補強現行 bbox 定位）
