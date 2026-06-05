# [F21] PART/CHIP catalog 取代 Coordinate Setup + Origin δ UI 升級

> **狀態：** planned
> **§8 ID：** [F21]
> **建立：** 2026-06-05
> **負責 branch：** claude/youthful-gates-WLNYJ

---

## Goal & Context

### 為什麼做

實機評估顯示，現有 Coordinate Setup（右欄 5 區、9 個數值欄位、RFL 術語）是新手通關
率的最大殺手——估算「真小白 + 無手冊」全程通關率 ≈ 0.4%，其中 Step 3
（Coordinate Setup）的通過率只有約 10%。主要障礙：

1. **RFL 術語**（DieX/DieY/SizeW/SizeH/GDS offset）需 KLA recipe 領域知識
2. **沒有自動帶入**（KLARF 已載入但仍要手 key）
3. **單位混搭**（chip µm / FOV nm / fine-tune nm）
4. **預設折疊**（容易找不到）

但其實，**這些座標跟 fab 的「PART 碼」（產品料號）+ CHIP 是 1-to-1 對應**——同
PART 同 CHIP 的 chip-corner 與 size 永遠相同。工程師熟悉「PART：TMVG10 / CHIP：C1」
的心智模型，卻不熟悉「DieX = 12345 µm」這種底層數值。把這個對應關係預先 key 進
catalog，user 只需下拉選擇即可。

順帶解決一個長期積累的混淆：**Origin δ（全域對位修正）vs Fine tune dx/dy（FOV 內微調）**
其實是同一件事的兩個表現，user 從 UI 上分不清。F21 同步把 fine tune 完全移除，
所有對位修正只走「拖 overlay → Set Offset」一條路。

### 想達成什麼

- **使用者層面**：右欄不再有「填表單」步驟。`PART [▼] CHIP [▼]` 兩個下拉，選完
  座標 / FOV / scale 全部就位。
- **工程師層面**：開發者模式下「Edit catalog…」對話框可新增/編輯 PART 與 CHIP，
  寫進 repo 內 `glas/data/parts.json`，git commit 後其他 user 下次 pull 即生效。
- **Origin δ 升級**：永久可見的 ALIGNMENT δ 區塊（大字級 X/Y 數值、Set/Clear 按鈕、
  顯示 nudge step），不可收合，跟 SEM image list 並列為右欄的兩個主要區塊。
- **Fine tune 移除**：UI（兩個 spinbox）、state（`_fine_dx`/`_fine_dy`）、加總邏輯
  全部清掉。CSV 匯出欄位 `fine_dx_nm`/`fine_dy_nm` 保留（指 auto fine-align 結果，不變）。

### 跟現有系統的關係

- **取代**：CoordinateSetupPanel ① RFL Chip-offset（6 欄）+ ② FOV（2 欄）+ ③
  Overlay scale（auto checkbox + spinbox）+ ⑤ Fine tune（2 欄）
- **保留並升級**：④ Origin δ（read-only label → 主顯區塊 + Set/Clear/nudge）
- **新增**：`glas/data/parts.json` catalog、`glas/core/parts_catalog.py` loader、
  catalog editor dialog（dev mode）
- **Cache schema bump**：`mmh-gds-alignment-v1` 加 `part_id`/`chip_id` 欄位作追溯；
  座標值仍快照存（catalog 改不影響舊 cache）

---

## Q&A Decisions

### Q1: PART/CHIP catalog 存哪裡？
**選項：** A repo 隨 app 出貨 / B user-local / C 網路共享
**選擇：** A — **`glas/data/parts.json` 隨 repo 出貨**
**理由：** team 共用最方便、不必 IT 配合；新增 PART 走 git commit/PR 流程。

### Q2: PART/CHIP 跟 OASIS 檔的關係？
**選項：** A 綁定 / B 完全解耦 / C PART→OASIS 1對1
**選擇：** **B 完全解耦** — user 自己挑 .oas（不同人 OASIS 命名可能不同）
**理由：** catalog 只給座標 / FOV / scale；OASIS 路徑不入 catalog，避免綁死 user
路徑慣例。Wizard 中 Open OASIS 仍為獨立步驟。

### Q3: 已存的 layer cache (.npz) 怎麼處理？
**選項：** A 快照所有值 / B 只存 id / C 兩者都存
**選擇：** **C 兩者都存（快照為主、catalog id 只作追溯）**
**理由：** 舊 cache 仍能完整還原（catalog 改了不影響）；新 cache 額外帶 `part_id`/
`chip_id` 方便日後追溯來源。

### Q4: 未知 CHIP 怎麼處理？
**選項：** A 完全擋住 / B 彈出手填連結 / C Custom 永遠保留
**選擇：** **A 完全擋住** — 下拉只列 catalog 內已存的；catalog 沒有就不顯示
**理由：** 嚴格清單避免 user 自己亂 key 錯座標；要新增就走開發者模式 + git PR。

### Q5: FOV 算誰的屬性、預設值？
**選項：** A chip 固定 / B catalog 預設 + UI 可 override / C 完全脫離 catalog
**選擇：** **B — catalog 存預設值（每 chip 一份），UI 提供「Custom FOV…」可
override**。預設值 **1500 nm（W）× 1500 nm（H）**。
**理由：** 同 chip 跨不同 SEM recipe 仍可用；override 後不寫回 catalog，只進 cache。

### Q6: Fine tune dx/dy 怎麼處理？
**選項：** A 併入 Alignment δ / B 維持分開
**選擇：** **完全移除（UI + 邏輯）** — 所有對位修正只走拖曳 → Set Offset
**理由：** Fine tune 本質是 δ 的子集（< 1 FOV 殘差），UI 分開反而讓使用者
混淆「我該動哪一個」。簡化心智模型。

### Q7: Origin δ UI 怎麼擺？
**選項：** A 收合 / B 完全拿掉（拖+Set 控制） / C 明顯可見
**選擇：** **C 明顯可見、不可收合** — 獨立常駐區塊，大字級數值
**理由：** δ 是對位品質的關鍵讀數，要常駐視野；可順手 `Set` / `Clear` 不需展開。

---

## Milestones

> 每個 milestone 以「一個 session 可完成」為粒度切。

### M1: Catalog data model + loader [status: planned]

定義 catalog schema、寫純函式 loader、給 catalog 寫一份種子資料。**core 無 Qt 依賴**。

- [ ] 新增 `glas/data/parts.json`（schema v1，種子資料：至少 1 個 PART + 2 個 CHIP，
      含註解說明欄位）
- [ ] 新增 `glas/core/parts_catalog.py`：
  - `@dataclass ChipSpec`：`chip_x_um / chip_y_um / chip_w_um / chip_h_um /
    gds_off_x_um / gds_off_y_um / fov_w_nm / fov_h_nm / nm_per_px (Optional, None=auto) /
    notes`
  - `@dataclass PartSpec`：`description / chips: dict[str, ChipSpec]`
  - `load_catalog(path) -> dict[str, PartSpec]`：讀 JSON、schema 驗證、缺欄補預設
  - `save_catalog(path, parts)`：atomic write（tempfile + rename）
  - `DEFAULT_FOV_NM = 1500`
  - schema 常數 `CATALOG_SCHEMA = "glas-parts-v1"`
- [ ] 新增 `tests/test_parts_catalog.py`：load round-trip、缺欄位、壞 JSON、
      schema version mismatch、atomic write 中斷不會壞檔
- [ ] 驗證：`pytest tests/test_parts_catalog.py -v` 全綠

### M2: 右欄重構 — PART/CHIP 下拉 + FOV badge [status: planned]

把 CoordinateSetupPanel 改造為 PartChipPanel，移除 RFL 6 欄、FOV 兩欄、scale 兩元件、
fine tune 兩欄。新增 PART/CHIP 下拉 + chip-corner badge + FOV badge + Custom FOV
collapse 區。

- [ ] 改寫 `CoordinateSetupPanel` → `PartChipPanel`（保留 `coord_setup` 引用名以
      免動 MainWindow 既有 signal wiring，類別名與 emit 的 dict key 改成新 schema）
- [ ] PART/CHIP 下拉：catalog 為空時 disabled + 提示「無 catalog 資料 — 請聯絡管理員」
- [ ] 選 CHIP 後即時更新 chip-corner / FOV badge，並 emit `changed`
- [ ] Custom FOV collapse 區：勾「Custom」展開兩個 nm spinbox + scale auto checkbox；
      勾掉回 catalog 預設
- [ ] 右欄整體佈局：`Load SEM… → PART/CHIP block → Alignment δ block → image list →
      Set/Clear Offset → Load GDS ROI → Fine Align（保留）`
- [ ] 修掉所有 `self._fine_dx`/`_fine_dy` 引用（@5003-5004、5423-5424、5545-5546、
      5560-5561、5583-5584、5606-5607、5723-5724、6068-6069），改成只用 `_origin_dx/dy`
- [ ] 移除 CoordinateSetupPanel 原 ①/②/③/⑤ 區塊建構碼
- [ ] 驗證：`python main.py` 手動跑——選 PART/CHIP 後 SEM jump 落點與舊版手填同數值
      時一致；Custom FOV 可覆蓋預設並反映到 SEM viewer FOV 框

### M3: Alignment δ 常駐區塊 [status: planned]

把 origin δ 從 read-only QLabel 升級為視覺主元素。

- [ ] 新增 `AlignmentDeltaPanel` widget：
  - 大字級（`_FS_LABEL` × 1.5 左右）X / Y 數值、_TK_ACCENT_DK 配色
  - `Set Offset` / `Clear Offset` 按鈕（搬自原 SemPanel 底下）
  - 顯示目前 nudge step（`Ctrl+方向鍵 = 10 nm`）
  - 「coords copy」icon button：點一下複製 `(dx, dy) nm` 到剪貼簿
- [ ] 接 MainWindow 的 `_origin_dx/dy` 讀取（既有 signal 不動，只換顯示元件）
- [ ] 右欄佈局：PART/CHIP block 下方緊接 AlignmentDeltaPanel（兩者都常駐、不收合）
- [ ] 原 status-bar 的「origin δ nudged to (...)」訊息仍保留，不衝突
- [ ] 驗證：拖 GDS → Set Offset → 數值即時更新；Clear → 歸零；Ctrl+方向鍵 nudge
      數值會走動；點 copy icon → clipboard 內容正確

### M4: Catalog editor（dev mode）[status: planned]

開發者模式下提供 catalog 編輯 UI，工程師預先 key 好 PART/CHIP。

- [ ] 新增 `CatalogEditorDialog(QDialog)`：
  - 左側 PART 樹（PART → CHIP 二層）+ Add/Remove PART/CHIP 按鈕
  - 右側選中 CHIP 後顯示其欄位編輯表單（µm/nm spinbox 加單位後綴）
  - `Save` 按鈕：寫回 `glas/data/parts.json`（用 M1 的 atomic save）+ status bar
    提示「Saved · 重新打開 PART 下拉生效」
  - `Reload` 按鈕：放棄修改、重讀
- [ ] 右欄 PART/CHIP block 下方加 `⚙ Edit catalog…` 按鈕（**只在 dev mode 顯示**，
      用 `self._dev_mode` 控制）
- [ ] 編輯完按 Save 後，PartChipPanel 重新 `load_catalog` 並刷新下拉
- [ ] 驗證：dev mode 開 → 新增 PART/CHIP → Save → 關 dialog → PART 下拉看到新項目；
      重啟 app 仍存在；非 dev mode 看不到此按鈕

### M5: Cache schema bump + part_id/chip_id 追溯 [status: planned]

cache 多存 PART/CHIP id 作追溯，舊 cache 仍可載入（缺 id 就顯示「(legacy)」）。

- [ ] `glas/core/gds_layer_cache.py`：`LayerCacheMeta` 加 `part_id: Optional[str]` /
      `chip_id: Optional[str]` 欄位，schema version bump（`v1` → `v2`，loader 容忍 v1
      = 舊欄位都 None）
- [ ] 移除 `fine_dx`/`fine_dy` 欄位（讀 v1 cache 時略過、寫 v2 時不寫）
- [ ] 載 v2 cache 時：若 `part_id`/`chip_id` 在當前 catalog 內 → 自動選下拉；不在
      → 下拉空白 + status bar 提示「cache from PART X / CHIP Y, not in current catalog —
      showing snapshot values only」（仍能用，只是下拉空）
- [ ] 載 v1 cache：跳過下拉、顯示 PART/CHIP「(legacy)」徽章，仍可用快照值
- [ ] 驗證：新增 `tests/test_gds_layer_cache.py` 加 v1→v2 round-trip、缺 id 容錯、
      `pytest tests/test_gds_layer_cache.py -v` 全綠

### M6: 收尾 — 文件、§8、SESSION_LOG [status: planned]

- [ ] 更新 `README.md`：把使用流程裡的「Coordinate Setup」段改成「Select PART/CHIP」
- [ ] 更新 `CLAUDE.md` §5.2「對位流程」描述
- [ ] 更新 §7 不要碰的地方：移除 fine tune 相關不變式
- [ ] 從 `CLAUDE.md` §8 移除 [F21]
- [ ] `SESSION_LOG.md` 加完成條目
- [ ] 全套件 `pytest tests/ -v` 通過（預期 ~610+ 項）

---

## Affected Files

預期改動 / 新增：

**新增：**
- `glas/data/parts.json` — catalog seed data
- `glas/core/parts_catalog.py` — catalog data model + I/O
- `tests/test_parts_catalog.py` — catalog 單元測試

**改動：**
- `glas/app/gds_align_tool.py`
  - `CoordinateSetupPanel` → 重構為 `PartChipPanel`（移除 ①/②/③/⑤、fine tune）
  - 新增 `AlignmentDeltaPanel` / `CatalogEditorDialog`
  - MainWindow：移除 `_fine_dx/_fine_dy` 所有 reference（~10 處）
  - `_build_menu` / About dialog：dev-mode 控制 Edit catalog 按鈕顯示
- `glas/core/gds_layer_cache.py` — schema v2（加 part_id/chip_id、移 fine_dx/fine_dy）
- `tests/test_gds_layer_cache.py` — v1/v2 互換測試
- `README.md` — 使用流程段
- `CLAUDE.md` — §5.2、§7、§8
- `SESSION_LOG.md` — 完成條目

---

## Risks / Open Questions

- **catalog seed 內容**：M1 寫 seed 時，user 是否要提供真實 PART/CHIP 數值？或先放
  範例 `EXAMPLE_PART/C1` 供測試？→ **預設用範例**；真實資料由 user 在 dev mode 加。
- **舊 cache 相容**：M5 v1 cache 的 chip-corner 值仍能完整還原，user 可繼續用舊
  alignment；只是 PART/CHIP 下拉會空白。
- **PART/CHIP 命名衝突**：catalog editor 沒做命名重複檢查 → M4 補：Add 時若 id 已存在
  → 跳 QMessageBox 拒絕。
- **OASIS 路徑記憶**：解耦後，user 換 chip 時要再開 OASIS 嗎？→ **要**，符合
  「OASIS 跟 PART/CHIP 解耦」決定。Wizard 化（F20）之後流程會更順。
- **F20 Wizard 相依**：F21 完成後，F20 Open OASIS wizard 可順手把「Select PART/CHIP」
  併入 Page 0 作可選快捷；非必需。F21 獨立可用。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/ -v` 全綠（含新增測試）
- [ ] 手動驗證：
  - 啟動 app → 右欄 PART/CHIP 下拉顯示種子 PART → 選 CHIP → SEM jump 落點正確
  - 拖 GDS overlay → Set Offset → AlignmentDeltaPanel X/Y 即時更新（大字級顯示）
  - Custom FOV 勾選 → 兩個 nm spinbox 可手動覆蓋預設 1500 nm
  - 開 dev mode（About icon × 5）→ 右欄出現 `⚙ Edit catalog…` → 新增 PART/CHIP →
    Save → 下拉立即出現新項目 → 重啟仍存在
  - 載入舊 v1 cache → 仍能還原座標（PART/CHIP 顯示 (legacy)）
  - 載入新 v2 cache → PART/CHIP 自動選回原值
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F21]`
- 從 `CLAUDE.md` §8 移除 [F21]
- **本檔保留**，作為 design history
- F20 Wizard 與 F22 Welcome 接續排程
