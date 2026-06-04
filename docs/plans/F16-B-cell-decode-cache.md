# [F16-B] 大型 cell 解碼加速（欄狀儲存 + 磁碟快取 + placement prep 快取）

> **狀態：** done（核心 M1–M7 + debug/UX；M5 撤案；後續見 §收尾）
> **§8 ID：** F16-B（注意：與舊 §8 `[F16-B]` 無 sbbox bbox-sweep 不同，後者已改號 `[F17]`）
> **建立：** 2026-06-04 · **完成：** 2026-06-04
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

### Q4: 三招（①磁碟快取 ②ROI 過濾解碼 ③parser 加速）的範圍與順序？
**選擇：** user 2026-06-04 選定「1+2+3 全做」。
**關鍵互動（重要）：** 那顆 flat 大 cell（44995）**內部無空間索引**，任何一次 view 都得把它整段 byte-stream parse 一遍。
- **①磁碟快取是 keystone**：把「parse 一次」的結果存欄狀、之後每個 session 秒級重用——這才是讓「重複使用變快」的根本。
- **②ROI 過濾解碼只省「那一次 parse」**：parse 時把 FOV 外的幾何不建物件，降低首解的物件建構成本（但仍要循序 parse 維持 modal；且過濾後的結果不適合當「整顆 cell」快取重用，故設為 cache miss 時的首解加速、且只在 cache 關閉/首建時用）。
- **③parser/儲存加速貫穿全程**：欄狀儲存 + `Placement` 改 NamedTuple(slots)，同時加速首解、降低記憶體、並讓①序列化變 trivial。
**預設策略：** 快取存「整顆 cell（未過濾）」；命中→秒級載入（①）。未命中→走「③ 加速過的完整解碼」並寫快取（首解較舊版快、之後 session 秒級）。②ROI 過濾解碼做成 env 開關 `GLAS_ROI_DECODE=1`（預設關），給「不在意快取、只要首解更快」的情境；開啟時不寫整顆快取。

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

> 順序原則：先把「儲存/型別」打底（③ 的一部分，也是 ① 的前提），再做 ①，最後 ②。每步測試全綠才前進。

### M1: ③ Placement → NamedTuple（slots）+ decode 微優化  [status: done]

- [x] `Placement` dataclass 改 `NamedTuple`（immutable、無 `__dict__`、建構更快、150 萬實例記憶體砍半）。確認無欄位 mutation、僅屬性存取。
- [x] decode 內圈微優化：last-key 快取避免每筆 `setdefault` 的 throwaway `[]` 配置；幾何分支排前（CELL/END 排後）；`Placement` 改 positional 建構。
- [x] 驗證：`pytest tests/` 592 全綠（行為不變）。

### M2: ③ 幾何欄狀儲存（CellContent 雙後端）  [status: done]

- [x] `CellContent` 加 optional 欄狀後端 `_rcol[key]=(coords, rt, rr)` / `_pcol[key]=(pts, off, rt, rr)`；decode 仍產 tuple-list（M1 不變），cache 載入則填欄狀。
- [x] 加 accessors（`rect_keys/poly_keys`、`rect_count/poly_count`、`rect_spec_at/poly_spec_at`、`all_rect_rtypes/all_poly_rtypes`、`total_rects/total_polys`）優先吃欄狀、否則退回 tuple；`rect_arrays/poly_arrays/rects()/polys()/is_empty` 改走 accessor。
- [x] walk 的 _feat / survivor / count 改用 accessor（decoded cell 行為不變 → 退回 tuple）。
- [x] 驗證：`pytest tests/` 592 全綠。

### M3: ① cellcache 序列化模組 + round-trip 測試  [status: done]

- [x] `CellContent.to_cache_arrays/from_cache_arrays`（欄狀 + 稀疏 rr 側表 idx/val，避免 880 萬 mostly-None object array pickle）。
- [x] 新增 `glas/core/cellcache.py`：`SCHEMA_VERSION`、per-user cache dir、`load/save`（mtime/size/schema 驗證、原子寫、毀損/版本不符當 miss、env 開關與門檻）。
- [x] `tests/test_cellcache.py`：各 repetition type（1/2/3/8/10/11/None）+ name-target + 空 cell 的 round-trip 逐 spec 相等；walk_roi decode-vs-cache bit-identical。
- [x] 驗證：`pytest tests/test_cellcache.py` 綠。

### M4: ① 整合進 load_cell + 真檔等價/失效測試  [status: done]

- [x] `load_cell` 先查 `cellcache.load`（命中→填 `_memo`、`_n_loaded+=1`、return）；decode 後若 record 數 ≥ 門檻 → `cellcache.save`。門檻 `GLAS_CELLCACHE_MIN_RECORDS`、目錄 `GLAS_CELLCACHE_DIR`、開關 `GLAS_CELLCACHE=0`。
- [x] 測試：e2e「decode 寫 sidecar → 新 reader load_cell 走欄狀」walk_roi bit-identical；改檔→miss、不同 wanted_layers→不同條目、env 關閉。
- [x] 驗證：`pytest tests/` 599 全綠。手動 LTV 待 user 量測（首次寫快取、重開 app 秒級）。

### M6: ① walk 的 placement gather per-cell 快取（換 ROI 加速）  [status: done]

- [x] 把 walk 的 placement prune「gather」（base_M/base_t/placed_all/arr_local/rcount/valid + arb/unk skip）抽成 ROI/T 無關的 per-cell 預計算，快取在 `CellContent._place_prep`，跨 ROI/layer 重用；每次 walk 只剩 `T.apply_to_rects(arr_local)` + mask + survivor。
- [x] 測試：`test_placement_prep_cached_across_rois`（prep 同物件重用、不同 ROI 結果正確）。
- [x] 驗證：`pytest tests/` 599 全綠。
- 註：in-memory（跨 ROI 同 session）。geometry extent 早已由 `_ext_cache` 跨 ROI 快取。**故第二個 ROI 起秒級**；每 session 第一個 ROI 仍付 gather(~20s)+geom ext(~40s) 預計算。on-disk 持久化 prep/ext（連每 session 第一個 ROI 也快）列為日後選做。

### M7: ① batch 暖機加速（persist prep + _feat gate）  [status: done]

- [x] placement prune precompute（gather）做成獨立 sidecar：`cellcache.save_prep/load_prep`（keyed by file+cell，layer 無關，mtime/size 驗證、原子寫、毀損當 miss）。walk 對大 cell（N≥門檻）先 `load_prep`、未命中才 gather 並 `save_prep` → 每個 batch worker / 每 session 第一個 ROI 跳過 ~15s gather。
- [x] `_feat` 收集用 `DEBUG` 包住（非 debug 不掃 150 萬 placement）。
- [x] 測試：`test_prep_cache_round_trips`（prep 持久化 + 新 reader 走磁碟 prep + walk 結果一致）。`pytest tests/` 602 全綠。
- 註：cell deser（~10s 重建 150 萬 placement）尚未 lazy（需 property 重構，風險高）→ 留待日後；目前 batch 暖機已大幅下降（省 15s gather + _feat）。

### M5: ② ROI 過濾解碼（env 開關）  [status: 撤案 — 對「看多 defect」工作流更差]

- 架構衝突：ROI 過濾結果是 ROI-specific，與「按 cell memo/快取」相斥；只省第一次解碼 ~2x。user 選「先測 ①/M6 再決定」。
- [ ] （若要做）`_decode_at` 接 local-ROI、按 `(cell, roi)` memo、不寫整顆快取、env `GLAS_ROI_DECODE=1` 預設關。

---

## 風險 / 備註

- **記憶體**：44995 欄狀 ≈ rects (8.8M×4×8=281MB) + polys + placements。與目前 in-memory 同量級；`.npz` 壓縮後磁碟較小。
- **門檻**：太低 → 大量小 sidecar；預設 100K records，env 可調。
- **不解決**：首次（cache 尚未建立）仍需 292s 解碼——本案目標是「第二次起秒級」，不是消滅首解。若日後要連首解都快，再評估 ROI 過濾解碼 / parser 加速（已記於思路，非本案）。

---

## 收尾備註（2026-06-04）

**已完成（commit 4af7f5b … 502e3df）：** M1（Placement→NamedTuple + lean decode）、M2（CellContent 欄狀雙後端）、
M3/M4（`cellcache` 序列化 + load_cell 整合）、M6（placement gather per-cell 記憶體快取，換 ROI 秒級）、
M7（prep sidecar 持久化 + `_feat` DEBUG gate，batch 暖機）、ext build 向量化、`--debug` 分層（L1 摘要 / L2 trace）+
ROI 進度畫面精確化 + Qt/jump 雜訊收掉。`pytest tests/` 602 全綠。

**實測（user，重開 app／快取已建）：** 換 ROI ~9–12s（原 ~2min）；44995 磁碟載入 ~20s（原解碼 292s）；
每 session 第一個 ROI ~76s→（ext 向量化後）更低。

**M5（ROI 過濾解碼）撤案：** 對「看多 defect」工作流更差（每換區域要重 parse）+ 與 cell 快取相斥。

**後續（皆已完成 2026-06-04）：**
- **[F18]** ✅ lazy placement：`placements` 改 lazy property（decoded=list、cache=SoA、用到才建），cache 格式 v2
  （int target/kind + 稀疏 name），prep 命中時完全不建 150 萬 Placement → cache 載入 placement 段 ~10s→~0.3s。
- **[F19]** ✅ sidecar LRU 自動清理（`_evict` + `clear()` + `GLAS_CELLCACHE_MAX_MB`）。
