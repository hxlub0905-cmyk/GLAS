# F27 M7 — LTV/E3B 大檔 export/互動加速：交接文件

> 這份是「context window 快滿」時的 session 交接。完整逐條紀錄見 `SESSION_LOG.md`
> 最上方的 M7~M7k；本檔是**濃縮版 + 待辦 + 使用者 Q&A + 測試方法**，讓新 session 直接接手。
> **Branch：** `claude/project-perf-optimization-86i8yt`（HEAD = `60d587d`）。

---

## 0. 一句話現況

LTV（1750 MB，D2DB）幾乎所有幾何塞在**一顆 10.8M-record 的巨大 flat merge cell `iMerge_Top`**。
這顆**第一次一定要解一次（~155s，無法避免）**；這輪的工作是把它從「被解很多次」變成
「**全域只解一次、之後永遠快**」。最後一塊拼圖（M7k：offset 快取 key）剛推上，**待 user 用「測法一」驗證**。

---

## 1. 本 session 做了什麼（commit 對照）

| commit | 內容 | 狀態 |
|---|---|---|
| `7077727` M7 | 解碼心跳（大 cell decode 時印 records/elapsed）+ 單一 debug 模式（`GLAS_DEBUG`/`debug.bat`，收斂 `.bat`/`--debug`/`--trace`）+ export batched-walk gate 雛形；刪 SOP pptx + 3 個舊 .bat | ✅ 已驗 |
| `816b8fb` M7b | 修 export gate（**以 sbbox 為準**，不是 `bbox_layer is not None`）+ `native_status()`（印 native 為何 OFF 的真因） | ✅ 已驗（native 其實 ON） |
| `5b147ca` M7c | polygon point-list **type 直方圖**（feasibility 探針，opt-in 零成本） | ✅ 量得 100% type 1 |
| `1b6af63` M7d | 大 sbbox 檔跳過開頭 ~91s 的無用 flatten prewarm（`native_flatten_worthwhile`） | ✅ log 確認生效 |
| `7725f23` M7e | cold-wave warm 雛形（warm 第一張 in-thread） | ❌ 無效（見下） |
| `5208f3d` M7g | **native type-0/1 多邊形解碼**（`.pyx` VERSION 8，`decode_pointlist_01`，byte-identical，~13x） | ⚠️ 對此檔幾乎無感（poly 非瓶頸） |
| `361ec34` ci | CI 重編 Windows `.pyd.b64`（VERSION 8） | ✅ 已在分支 |
| `6b77bb1` M7h | 把 `arrays_materialized/instances_materialized/max_array_k` 上到 export-timing `[mat:]` | ✅ 診斷用 |
| `86d74ad` M7i | warm 改迴圈（warm 到解到 ≥2M records 的 cell 為止） | ❌ 無效（見下） |
| `dd61793` M7j | **不靠 defect**，用 `find_giant_cells()`（offset table 的 byte 跨距）直接揪出大 cell，orchestrator 開 pool 前預解一次 | ⚠️ 待驗 |
| `60d587d` M7k | **cellcache key 改用 byte offset**（`cache_key_for()`）→ 互動(refnum)與 export(name)**共用同一份 sidecar，大 cell 全域只解一次** | ⚠️ **待 user 驗（測法一）** |

**免 CI：** 除 M7g 動 `.pyx`（已由 CI 重編 .pyd.b64）外，其餘全純 Python，重抓 ZIP 即可。全測試 **878 passed**。

---

## 2. 診斷歷程（為什麼慢，逐步釐清）

1. **不是 F27 回歸**：互動 `walk_roi` 逐字未改；native 是 ON（VERSION 8）。
2. **多邊形不是瓶頸**：VERSION 8 前後大 cell 解碼速率都 ~41K rec/s；poly 只佔那顆 cell ~5% 記錄、更小比例的**時間**。native type-0/1 對此檔幾乎無感（但程式正確、對「多邊形多」的檔以後有用）。
3. **真瓶頸是 cold-wave thrashing**：`[mat:]` 證明快（7s）與慢（420s）影像 materialization **完全一樣（~200K）**→ 慢的是「多個 worker **同時**冷解同一顆 10.8M-record cell」→ RAM 爆 → thrashing → 同樣工作慢 60×。
4. **warm 靠 defect 影像不可靠**（M7e/M7i 失敗）：會碰那顆 cell 的 defect 分散在第 179+ 張，warm 前幾張抓不到 → 改成 **M7j 直接用 byte 跨距找 cell**。
5. **「解兩次」根因**（M7k）：互動用 **refnum 44995**、export 用 **name 'iMerge_Top'** 到達同一顆 cell，cellcache key 是 id 字串 → 兩份 → 各解一次、每 session 重來。→ 改用 **offset** 當 key → 共用。

---

## 3. 使用者提的問題（Q&A 存查）

- **Q：native 到底是什麼？還有用 Cython 嗎？** A：native = 編譯好的 C 擴充（`oasis_fastdecode`，Cython 寫的 `.pyx`）在跑；相對 pure Python。加速矩形/walk，**不加速多邊形/placement**。有一張圖解 artifact（見聊天）。
- **Q：多邊形能用 Cython 加速嗎？困難點？** A：能，但變長輸出 + 六種點列編碼 + byte-identical 約束 → 只做 type 0/1 最務實。已做（M7g），但此檔非瓶頸。
- **Q：walk roi 已 decode，export 又 decode 一次，差在哪？** A：**refnum vs name 兩把快取 key**（M7k 已修為 offset 共用）。
- **Q：為什麼感覺完全沒變快、一直重複 decoding？** A：同上 + cold-wave 重複解（M7j/M7k 修）。
- **Q：我該怎麼測試？要刪快取？** A：見 §5。
- **Q：兩個檔（E3B / LTV）都要快。** A：見 §4。

---

## 4. E3B vs LTV（兩個目標檔）

- **LTV**：一顆巨大 flat merge cell（`iMerge_Top`）。**一次性 ~155s 解碼**（無法避免），之後靠 cellcache（offset key，跨 session/互動/export 共用）永遠快。走 `walk_roi`（有 sbbox）。
- **E3B**：有階層、**無 sbbox**、很多密集葉 cell。走 **batched walk**（M4，需 topo build；有 CE 層可 early-stop）。一次性成本在 reach-bbox sweep + 密集葉 decode，一樣被 cellcache 快取。**本輪未在 E3B 重測** → 待辦。

---

## 5. 測試方法（給 user）

快取位置（Windows）：`%LOCALAPPDATA%\glas\celldecode`（**重抓 ZIP 不會消失**）。清快取 = 刪此資料夾。

- **測法一（驗「只解一次」，最重要）：** 不清快取 → 跑一包 export（第一次會付 ~155s 的 `[export] pre-decoding … ['iMerge_Top']`）→ **再跑第二包**。第二次那行應「**秒過**」（cache hit、無 155s 心跳），整包快 = 成功。
- **測法二（冷啟一次性成本）：** 刪快取 → 跑一包 export → 看那顆 cell 解一次 ~155s + 其餘 ~7s/張。
- 一律用 `debug.bat` 跑，看 `[roi]` / `[export] pre-decoding …` / `[export-timing]` 行。

---

## 6. 待辦 / 未解（新 session 從這裡接）

- [ ] **（最優先）M7k 驗證**：user 跑「測法一」，貼回第二包 export 的 `[export] pre-decoding …` 行。若秒過 → 成；若第二次仍解 155s → 還有 key 沒對齊，追 `load_cell` 的 memo/key 或 pool worker 的 `_init_wanted` 是否一致。
- [ ] **E3B 重測**：確認 E3B 的 export/互動在本輪改動後仍快（batched walk + cellcache）。
- [ ] **記憶體 N×（sidecar 載入時）**：預解後 workers 各自從 sidecar 載一份 `iMerge_Top` → 短暫 N× 記憶體。RAM 吃緊時仍可能小卡；若發生 → 加 export worker 數上限（`fine_align.batch_worker_count` / UI「Parallel workers」）。
- [ ] **（大工程、選作）攻擊那一次性 155s**：native 化 placement / 整顆 cell 解碼（目前只矩形 native）。風險高、需 CI；先確認 §6 第一項的「只解一次」成立後，再評估值不值得。
- [ ] KLayout 重存**不建議**（已釐清：此檔已有索引，重存不會把 flat merge cell 拆階層）。

---

## 7. 環境 / 交付備忘

- **User 機器**：TSMC locked Windows，Python 3.9.7 x64，**MSVC/git/含 .pyd 的 ZIP 全被擋**。
- **native 交付**：CI（`.github/workflows/build-fastdecode.yml`，push `.pyx` 觸發）編 Windows `.pyd` → 存成 base64 文字 `.pyd.b64` commit → user 重抓 ZIP → `python tools/unpack_fastdecode.py` 還原。目前 **VERSION 8** 已在分支。
- **debug**：`debug.bat`（設 `GLAS_DEBUG=1`）是唯一 debug 入口，印 native 狀態 + ROI 摘要 + 解碼心跳 + export 計時 + `[mat:]` + poly type 直方圖。
- **回覆語言**：繁體中文。**commit / PR / code 內不得出現 model identifier**。
- 全測試：`QT_QPA_PLATFORM=offscreen pytest tests/ -q` → 878 passed。
