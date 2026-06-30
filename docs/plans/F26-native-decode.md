# [F26] 原生（Cython）OASIS 解碼加速 + 無編譯器交付

> **狀態：** in progress（M0 機制驗證中）
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

### M1: native varint 接進 decode 熱路徑  [status: planned]

- [ ] `oasis_streamer` gated 取用：`OasisStream.read_uvarint/read_svarint` 在 native 可用時走 `oasis_fastdecode`，否則純 Python。
- [ ] 量測：`oas_profile.py` 對兩檔重跑，確認 varint 佔比下降、整體加速。
- [ ] 全 round-trip 測試雙路徑綠。

### M2: native per-record 迴圈（最大收益）  [status: planned]

- [ ] 把 `consume` 內層 + RECTANGLE/POLYGON decoder + modal state 搬進 Cython，每個 cell 才回 Python 一次、直接填 numpy buffer。
- [ ] §7 spec 對齊：PLACEMENT info-byte N-bit（§22.6）、continuation/sign（§7.2/§7.3）、modal reuse、overflow guard。
- [ ] byte-identical 護欄 + 量測（目標整檔 decode ~2.5–4×）。

### M3: 收尾 + 互補的 F17  [status: planned]

- [ ] **F17 bbox sweep**（無 build、純 Python）：對**無 S_BOUNDING_BOX 的大檔**（如 E3B）做一次性 cell-bbox sweep + sidecar，
      讓後續 ROI prune 免解碼（治「首次載入很久」）。與 native 解碼互補。
- [ ] 文件：CLAUDE.md（§4 新模組、§6 build 說明、§8）、README、SESSION_LOG。

---

## Affected Files

- `glas/core/oasis_fastdecode.pyx`（新）、`setup.py`（新）、`.github/workflows/build-fastdecode.yml`（新）、`tests/test_fastdecode.py`（新）、`.gitignore`
- M1+：`glas/core/oasis_streamer.py`（gated 取用）
- M3：`glas/core/oasis_random.py`（F17 sweep）、`glas/core/cellcache.py` 或新 sidecar、docs
