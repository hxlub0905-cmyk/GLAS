# [F26] 原生（Cython）OASIS 解碼加速 + 無編譯器交付

> **狀態：** in progress（M0 ✓ 交付驗證 / M1 ✗ per-varint 回歸已撤 / M2 GO — 批次 poi 主導已確認 / M3 F17）
> **§8 ID：** [F26]
> **建立：** 2026-06-30
> **負責 branch：** claude/project-perf-optimization-86i8yt

---

## Goal & Context

`tools/oas_profile.py` 在兩個真實 production 檔上量測，結論一致且決定性：

| | E3B (330 MB, 純 RECTANGLE) | LTV_EBI (1.75 GB, 含 PLACEMENT/POLYGON) |
|---|---|---|
| decode 速率 | 143k rec/s | 54k rec/s |
| **varint + record loop（native 可救）** | **82%** | **85%** |
| zlib (CBLOCK) / store / IO | ~0% | ~0% |
| 最大 sink | `read_uvarint` | `read_uvarint`（21s/5M） |

解碼時間 82–85% 卡在純 Python 的逐 byte varint 迴圈 + per-record 分派——正是原生碼能直接消除的部分。
目標：把這個熱迴圈搬到 Cython，整檔 decode / ROI load 預期 **~2.5–4×**，並**保留純 Python fallback**
（無 `.pyd` 時照跑，只是慢）。

---

## Q&A Decisions

### D1: 用哪種加速？
**選擇：** Cython 原生熱迴圈。**理由：** 量測證實 82–85% 在 varint+dispatch；zlib/store/IO≈0，純 Python
向量化碰不到「每筆 RECTANGLE 的零散 scalar varint」這個最大宗。借 gdstk 只對「整檔/整片」型有利、且要整檔
parse + 改架構，不適合互動 ROI。

### D2: 交付方式（user 公司電腦的 MSVC 被 IT 擋、無法本機編譯）
**選擇：** **CI 編、本機放檔**。GitHub Actions（windows runner 內建 MSVC）以 **Python 3.9 x64**（= user 環境）
編出 `oasis_fastdecode.cp39-win_amd64.pyd` 當 artifact；user 下載後**複製進 `glas/core/`**（不是安裝，是放一個檔，
繞過 IT 限制）。改 Cython 原始碼 → CI 重編、重新下載。
**理由：** 本機裝不了編譯器；pip 可用、可下載、可放檔。包 exe 時 PyInstaller（純 pip、不需編譯器）會把 `.pyd`
一起打包，操作員端零安裝。

### D3: fallback
**選擇：** `oasis_streamer` 以 `try: import oasis_fastdecode` 取用，缺檔/載入失敗 → 純 Python 路徑。
**理由：** §6 core 跨專案可複用、測試/無 build 環境要能跑；§7 native 必須與純 Python **byte-identical**。

---

## 限制（記錄，已與 user 確認）

- `.pyd` 綁 **Python 3.9 + win_amd64**；換版本要重編。CI 用 `actions/setup-python@3.9 x64` 對齊。
- native varint 以 64-bit 計算（涵蓋所有真實座標/計數）；>64-bit 會 raise OverflowError 交回純 Python，不靜默 wrap。
- 改原始碼要重跑 CI + 重新下載；全 `test_oasis_*` + `test_fastdecode.py` round-trip 對兩條路徑都要綠。
- 包 exe 的發佈麻煩（防毒/簽章、PyQt 凍結 hook、OS 相容、體積）與本案無關，是「PyQt app 包 exe」本身的事。

---

## Milestones

### M0: 機制驗證（CI 編 → user 下載 → 放進去能 import）  [status: in progress]

- [x] `glas/core/oasis_fastdecode.pyx`：`decode_uvarint` / `decode_svarint`（memoryview 直讀、零拷貝）+ `selftest()` + `VERSION`。
- [x] `setup.py`（cythonize、`build_ext --inplace`）+ `.gitignore` 忽略 `*.pyd`/`*.so`/`build/`/生成的 `.c`。
- [x] `.github/workflows/build-fastdecode.yml`：windows + py3.9 x64 → build → 自我 smoke + round-trip → 上傳 `.pyd` artifact。
- [x] `tests/test_fastdecode.py`：`importorskip`，native 與 `OasisStream.read_uvarint/svarint` round-trip（32 例）。本機 Linux `.so` 驗證通過、全測試 782 passed。
- [ ] **CI 在 GitHub 上成功產出 cp39-win_amd64 `.pyd` artifact**（push 後確認）。
- [ ] **user 下載 artifact、放進 `glas/core/`、跑 `python -c "import oasis_fastdecode as f; print(f.selftest())"` 成功**（驗證 IT 限制下「放檔」可行）。
- [ ] 決策點：M0 成功 → 進 M1；失敗（artifact 被擋/AV 隔離）→ 改走 F17 + 純 Python。

### M1: native varint 接進 decode 熱路徑  [status: done 2026-06-30 — 量測後撤回]

- [x] `oasis_streamer` gated 取用 read_uvarint/read_svarint（已實作、211 oasis 測試雙路徑綠）。
- [x] **本機合成大檔量測（決定性）**：per-call native varint 是**回歸** —— 0.79–0.81×（更慢）。原因：每個
      varint 一次 Python→C 呼叫 + tuple 配置/拆解的成本，超過它取代的 1–2 圈純 Python 迴圈（小 varint 是常態）。
- [x] **撤回 per-varint 整合**（保留純 Python read_uvarint/svarint）。`oasis_fastdecode` 模組 + 交付管道（M0）
      仍保留、已驗證，留給 M2 的 record-loop 用。**結論：唯一能贏的粒度是整個 record / record-run 在 native 攤提
      C 邊界，不是 per-scalar。**

### M2: native record-RUN 解碼（GO — user 確認批次 poi 主導）  [status: planned]

> **動機確認（2026-06-30）：** user 真實痛點＝整包 KLARF（~3000 顆散落 defect）批次對位，`GLAS_FA_TIMING`
> 顯示 **poi（ROI walk＝解碼+Boolean）主導**（非 match）。3000 顆散落 → 加總碰整顆 chip 一大部分 cell →
> 解碼吞吐 bound → native 是對的槓桿。M1 的回歸只證明「per-varint 粒度錯」，M2 走「per-run」攤提 C 邊界。
>
> **整合點：** 不是 `iter_records`，而是 **oasis_store 的 per-cell 幾何解碼**（`walk_roi`→`load_cell`→decode
> 觸發的那段）——批次 3000 次散落 `load_cell` 的成本就在這。native 解碼一個 cell 的 RECTANGLE/POLYGON run、
> 遇到非幾何 record（CELL/PLACEMENT/PROPERTY/CBLOCK）就回 Python。PLACEMENT §22.6 留在 Python（native 不碰）。

- [ ] **M2a：native RECTANGLE-run 解碼器**（98% record）：`decode_rect_run(buf, pos, modal…) → (new_pos,
      modal_out, rects(N,4) int32, layer/dt arrays, stop_rid)`，一次解一整串 rect、填 numpy、遇非-rect 即停回
      Python。modal state（layer/dt/w/h/x/y/xy_rel）在 C 維護、byte-identical（含 modal reuse / square bit /
      signed x,y / repetition raw）。**先量測**（合成 + 你的真檔）確認攤提後是淨贏（≠ M1 的 per-call 回歸）。
- [ ] **M2b：擴充 POLYGON**（point-list g-delta run 在 C 解）+ repetition raw 在 C 解（檔1 `read_repetition_raw`
      2s、檔2 `decode_point_list`/`decode_g_delta` ~7s 都在這）。
- [ ] **M2c：接進 oasis_store**（gated：native 可用走 C run-decoder、否則純 Python `run()`）；cellcache 不變。
- [ ] §7：continuation/sign（§7.2/§7.3）、modal reuse、overflow（>64-bit 回 Python）、(N,4) int32 欄序/dtype/空
      sentinel byte-identical。全 `test_oasis_*` + 新 `test_native_run` 雙路徑綠。
- [ ] 每步**先量測再進下一步**（避免再踩 M1 那種「看似該快、實測回歸」）。目標 cell 解碼 ~2.5–4× → 批次 poi 同比例降。

### M3: F17 bbox sweep（互補、無 binary）+ 收尾  [status: planned]

- [ ] **F17**：對**無 S_BOUNDING_BOX 的大檔（檔1 E3B）**做一次性 cell-bbox sweep + 持久 sidecar，讓 ROI prune
      免解碼（批次 3000 次散落 walk 跨 worker 不再重複「解碼學 bbox」）。純 Python、零 binary，與 M2 疊加。
      （檔2 有 sbbox → 本來就免解碼剪枝，F17 不影響它。）
- [ ] 文件：CLAUDE.md（§4 新模組、§6 build/交付說明、§8）、README、SESSION_LOG。

### M3: 收尾 + 互補的 F17  [status: planned]

- [ ] **F17 bbox sweep**（無 build、純 Python）：對**無 S_BOUNDING_BOX 的大檔**（如 E3B）做一次性 cell-bbox sweep + sidecar，
      讓後續 ROI prune 免解碼（治「首次載入很久」）。與 native 解碼互補。
- [ ] 文件：CLAUDE.md（§4 新模組、§6 build 說明、§8）、README、SESSION_LOG。

---

## Affected Files

- `glas/core/oasis_fastdecode.pyx`（新）、`setup.py`（新）、`.github/workflows/build-fastdecode.yml`（新）、`tests/test_fastdecode.py`（新）、`.gitignore`
- M1+：`glas/core/oasis_streamer.py`（gated 取用）
- M3：`glas/core/oasis_random.py`（F17 sweep）、`glas/core/cellcache.py` 或新 sidecar、docs
