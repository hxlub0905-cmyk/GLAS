# [F16-B] 大型 cell 解碼結果持久化磁碟快取（sidecar）

> **狀態：** planned
> **§8 ID：** [F16-B]
> **建立：** 2026-06-04
> **負責 branch：** claude/friendly-franklin-9uZqU

---

## Goal & Context

**問題（實測）：** LTV 這類 D2DB 大檔，ROI walk 的剩餘瓶頸已定位為**單一巨大 flat cell 的首次解碼**。
實例：cell `44995`（chip-spanning 的 merge cell）含 **880 萬矩形 + 150 萬 placement + 48 萬多邊形 ≈ 1080 萬筆 record**，
`load_cell` 解碼一次要 **~292s**。要顯示任何 4µm FOV 都得先把這顆全解（OASIS 循序 + CBLOCK，無法跳讀內部子塊）。

**現況：** 解碼結果已在 reader 上 memoized（`_memo`），所以**同一 session 內**第 2 顆 layer / 之後任何鄰近 ROI 都重用、很快。
痛點純粹是「**每個 session 第一次**」要等 ~5min；開發/反覆重開 app 時很煩。

**目標（成功長相）：** 把大型 cell 的解碼結果序列化到 per-user sidecar 快取（對齊現有 `layerscan_cache` / layer `.npz` 慣例）。
- 第一次開某 .oas：照樣解碼 ~5min，但**順手寫快取**。
- 之後每個 session 第一次載入：從 sidecar **載入 ~幾秒**（而非 292s）。
- 檔案變更（mtime/size 不符）→ 自動 miss 重解；快取毀損 → 當 miss，永不破壞正確性。

**與現有系統關係：** 並存增強。`RandomAccessReader.load_cell` 在解碼前查快取、解碼後寫快取；其餘 walk 邏輯不變。

---

## Q&A Decisions

### Q1: 往哪個方向修首次解碼 292s？
**選項：** 持久化磁碟快取 / ROI 過濾解碼 / 加速 parser / 維持現狀
**選擇：** 持久化磁碟快取（user 2026-06-04 選定）
**理由：** 對「反覆重開 app」的開發流程 ROI 最佳；首次仍需解一次，但之後每個 session 秒級。其餘方案增益不確定或無法把 292s 變秒級。

### Q2: 快取哪些 cell？
**選項：** 全部 cell / 只大型 cell（record 數 > 門檻）
**選擇：** 只大型 cell（預設門檻：placements+rects+polys 合計 > 100,000）
**理由：** 小 cell 解碼本就毫秒級，快取它們只增加大量小檔與查找開銷；大 cell 才是成本所在（44995 一顆即占 98%）。門檻可由 env `GLAS_CELLCACHE_MIN_RECORDS` 調。

### Q3: 序列化格式？
**選項：** pickle 整個 CellContent / numpy 欄狀（columnar）
**選擇：** numpy 欄狀（`.npz`）+ 稀疏 repetition 側表
**理由：** pickle 重建 880 萬 tuple / 150 萬 dataclass 仍要數十秒，達不到「秒級」。欄狀 `.npz` 載入是少數大陣列、~幾秒。
repetition descriptor（`rt`/`rr`）多數為 None，非 None 的存稀疏側表（index→(rt,raw)），用 JSON/pickle 存少量即可。

---

## 設計細節

### 快取鍵（key）
`sha1(abs_path)[:16]` 目錄 + 檔名 `{cell_key}__{layers_digest}.npz`，內含 meta：
`{schema, src_mtime, src_size, cell_id, wanted_layers(sorted), dtype}`。載入時逐項比對，任一不符 → miss。
（cell 內容只依賴 `wanted_layers`（幾何過濾）與 dtype；bbox_layer 只影響 `load_cell_bbox`，不入此鍵。）

### 欄狀 schema（新模組 `glas/core/cellcache.py`）
`CellContent.to_cache(content) -> dict[str, np.ndarray | bytes]` / `from_cache(d) -> CellContent`：
- **rects**（每 key）：`r_<L>_<D>_xyxy`=(N,4) int64、`r_<L>_<D>_rt`=(N,) int16（None→-1）。
- **polys**（每 key，ragged → CSR）：`p_<L>_<D>_pts`=(Σn,2)、`p_<L>_<D>_off`=(P+1,) int64、`p_<L>_<D>_rt`=(P,) int16。
- **placements**（SoA）：`pl_x/pl_y`(int64)、`pl_angle/pl_mag`(float64)、`pl_flip`(bool)、`pl_rt`(int16)、
  `pl_target`(int64；name-target 記 -1 並於側表存字串)、`pl_kind`(int8: refnum/name/modal)。
- **稀疏 repetition 側表**：`rr_blob`=JSON/pickle `{"rect":{key:{idx:[rt,raw]}}, "poly":{...}, "pl":{idx:[rt,raw]}}`，
  只存 `rr is not None` 的少數項；`raw` 為純量/list 巢狀，JSON 可序列化（tuple→list，載入時還原）。
- **bbox**：4-tuple meta。

### 整合點（`oasis_random.py`）
- `load_cell(cid)`：`_memo` miss 後，先 `cellcache.load(...)`；命中 → 反序列化、放 `_memo`、`_n_loaded+=1`、return。
- 未命中 → 照常 `_decode_at`；若 `content` 的 record 數 > 門檻 → `cellcache.save(...)`（原子寫，失敗僅警告不拋）。
- 反序列化出的 CellContent 的 `rect_specs/poly_specs` 仍以「list of tuple」介面對外（walk/`rect_arrays` 不變），
  由 `from_cache` 重建；或進一步讓 `rect_arrays/poly_arrays` 能直接吃欄狀陣列（M2 最佳化，非必要）。

### 正確性護欄
- round-trip 測試：`from_cache(to_cache(c))` 對任意 CellContent 還原出**逐 spec 相等**（含 repetition、polys、placements、bbox）。
- 真檔等價測試：對 fixture，「直接解碼」vs「寫快取再讀回」走 `walk_roi` 結果 bit-identical。
- 毀損/版本不符/檔案變更 → miss（不拋例外）。

---

## Milestones

### M1: cellcache 序列化模組 + round-trip 測試  [status: planned]

- [ ] 新增 `glas/core/cellcache.py`：`to_cache/from_cache`（欄狀 + 稀疏 rr 側表）、`SCHEMA_VERSION`、per-user cache dir（沿用 layerscan 慣例）、`load/save`（mtime/size/key 比對、原子寫、毀損當 miss）。
- [ ] 測試：隨機構造含各 repetition type（1/2/3/8/10/11/None）的 rect/poly/placement 的 CellContent，`from_cache(to_cache(...))` 逐 spec 相等；name-target placement、空 cell、多 layer。
- [ ] 驗證：`pytest tests/test_cellcache.py -v` 綠。

### M2: 整合進 load_cell + 真檔等價/失效測試  [status: planned]

- [ ] `RandomAccessReader.load_cell` 接快取讀/寫（大 cell 門檻、env override）；快取目錄可由 `GLAS_CELLCACHE_DIR` override，可由 `GLAS_CELLCACHE=0` 關閉。
- [ ] 測試：fixture 大 cell「解碼 vs 快取讀回」`walk_roi` bit-identical；改檔（碰 mtime）→ miss 重解；wanted_layers 不同 → 不同條目。
- [ ] 驗證：`pytest tests/ -k "cellcache or oasis_random"` 綠；手動 LTV：第一次 ~5min 寫快取、重開 app 第一次載入秒級（貼 `[roi]` 計時）。

### M3:（選做）walk 直接吃欄狀、免重建 tuple list  [status: planned]

- [ ] `rect_arrays/poly_arrays` 與 walk survivor 展開直接用欄狀陣列，`from_cache` 不重建 8.8M tuple（進一步壓低載入與記憶體）。
- [ ] 驗證：載入時間再降；全測試綠。

---

## 風險 / 備註

- **記憶體**：44995 欄狀 ≈ rects (8.8M×4×8=281MB) + polys + placements。與目前 in-memory 同量級；`.npz` 壓縮後磁碟較小。
- **門檻**：太低 → 大量小 sidecar；預設 100K records，env 可調。
- **不解決**：首次（cache 尚未建立）仍需 292s 解碼——本案目標是「第二次起秒級」，不是消滅首解。若日後要連首解都快，再評估 ROI 過濾解碼 / parser 加速（已記於思路，非本案）。
