# Vendored into GLAS on 2026-08-17 (F31 M2).
# Source project: ADEPT — file: adept/core/ingest/klarf_core.py @ 13153f4
#   (which vendored KLIP's klarf_core.py, the KLARF 1.2/1.8 lossless engine,
#   on 2026-07-27 and added the one additive method defect_image_filename()).
# Adaptations for GLAS:
#   - **read-only subset only.** Everything from set_header() onward upstream —
#     the editing operations, the span-splice writer (to_text/save), the API-row
#     generator, and the lint / autofix / compare / HTML reporting — is NOT
#     vendored. GLAS reads KLARFs here and never writes them through this file.
#   - the health-check row-length helpers (image_block_span / effective_row_len
#     / row_len_ok) came with the writer's lint and are left out with it.
#   - otherwise copied verbatim, including the Chinese docstrings, so a future
#     re-sync against ADEPT is a plain diff.
#
# Why this exists next to glas/core/klarf_parser.py (F31 Q3): that module owns
# GLAS's lossless KLARF write-back and only understands the rSEM shape (each
# defect carrying its own `Images N { "file" … }` filename). EBI-patch KLARFs
# name one batch TIFF at lot level and address it through IMAGECOUNT/IMAGELIST
# columns, and 1.2 files are not parseable by it at all — measured, load_klarf
# returned zero images for both shapes. Rather than reopen a module that must
# round-trip byte-for-byte, the EBI ingest path reads through this vendored
# doc. klarf_parser stays untouched and stays the only writer.
"""
klarf_core.py
KLARF 1.2 / 1.8 讀取 + 就地編輯引擎（UI 與檔案格式之間的那一層）

核心原則：span-splice 無損寫回
  - 沒被使用者改到的部分，to_text() 寫回時與原檔「逐位元組相同」。
  - 只有被編輯的區塊（某個 header 欄位、DefectList）才重新產生。
  - 需要的 count（1.8 的 Data N）自動重算；不確定意義的 count（1.2 的
    ClassLookup 數字）一律原樣保留，絕不亂算。

單位備忘：
  - 1.2：座標/尺寸為「微米 µm 浮點」
  - 1.8：座標/尺寸為「奈米 nm 整數」（= µm × 1000）
  引擎內部一律以「原始字串 token」保存 defect 表，不做數值換算，
  單位換算/顯示交給 UI 層處理，避免任何精度漂移。
"""
from __future__ import annotations

import os
import re


# ---------------------------------------------------------------- 小工具

def _find_matching_brace(text, open_idx):
    """從 text[open_idx] 的 '{' 找出成對的 '}' 索引；找不到回 -1。"""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return i
    return -1


def detect_version(text):
    """回傳 '1.8' / '1.2'（或 FileVersion 指定的字串）。"""
    if re.search(r'Record\s+FileRecord', text):
        return '1.8'
    m = re.search(r'FileVersion\s+(\d+)\s+(\d+)', text)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    if re.search(r'\bList\s+\w+\s*\{', text):
        return '1.8'
    return '1.2'


# 1.8 的記錄名稱 → 對應到 1.2 的 header 欄位名
REC_FIELD = {
    'LotRecord': 'LotID',
    'WaferRecord': 'WaferID',
    'DeviceRecord': 'DeviceID',
    'StepRecord': 'StepID',
    'SetupRecord': 'SetupID',
    'FileRecord': 'FileVersion',
}

UNIT_INFO = {
    '1.2': {'coord_unit': 'µm', 'coord_type': 'float', 'to_nm': 1000.0},
    '1.8': {'coord_unit': 'nm', 'coord_type': 'int',   'to_nm': 1.0},
}


# ---------------------------------------------------------------- 主類別

class KlarfDoc:
    """一份 KLARF 檔的可編輯模型。UI 對 1.2 / 1.8 用同一組介面操作。"""

    # 1.2 常見的單行 header 欄位（可編輯）；未列出的欄位會被原樣保留
    FIELDS_12 = [
        "FileVersion", "FileTimestamp", "InspectionStationID", "SampleType",
        "ResultTimestamp", "LotID", "SampleSize", "DeviceID", "SetupID",
        "StepID", "SampleOrientationMarkType", "OrientationMarkLocation",
        "DiePitch", "DieOrigin", "WaferID", "Slot", "ScribeID",
        "SampleCenterLocation",
    ]

    def __init__(self, text, source_path=None):
        self._text = text
        self.source_path = source_path
        self.version = detect_version(text)
        self._is18 = (self.version == '1.8')
        self.warnings = []
        self.summary_stale = False
        self.auto_recompute_summary = False  # 決策：SummaryList 一律維持原樣，不自動重算
                                             #（DefectList 常是子集、summary 是全量，重算會洗掉真數字）

        # summary（目前 1.2 支援自動重算；1.8 待真檔）
        self._summary_columns = []
        self._summary_rows = []
        self._summary_orig = None

        # 可編輯元素
        self._header = {}            # name -> {"orig":原字串, "value":現值, "dirty":bool}
        self.class_lookup = {}       # code(int) -> name(str)
        self._classlookup_orig = None
        self._classlookup_dirty = False
        self.defect_columns = []     # 欄位名 list
        self.defects = []            # list[list[str]]，原始 token
        self._defect_dirty = False
        self._defect_orig = None     # 原始 DefectList / Data 區塊字串（供 replace-once）

        # patch 影像（帶圖 KLARF）：TiffFileName 指到多頁 TIFF，
        # TiffSpec 宣告 IMAGELIST 每張圖佔幾個 token
        self.tiff_file_name = None   # 原始字串（可能含 Windows 路徑）
        self.tiff_spec = None        # {"version": str, "nfields": int|None, "fields": [str]}
        self._img_layout = 'unset'   # 快取：(start_col, nfields, how) 或 None
        self._il18 = None            # 1.8：型別為 ImageList 的欄位索引（名稱不限）

        if self._is18:
            self._parse_18()
        else:
            self._parse_12()
        self._parse_tiff_fields()

    # ---------- 對外唯讀資訊 ----------

    def unit_info(self):
        return UNIT_INFO.get(self.version, UNIT_INFO['1.2'])

    def header_items(self):
        """回傳 [(name, value_str), ...] 給 UI 顯示/編輯。"""
        return [(n, h["value"]) for n, h in self._header.items()]

    def col_index(self, name):
        up = [c.upper() for c in self.defect_columns]
        return up.index(name.upper()) if name.upper() in up else -1

    # ---------- 解析 1.2 ----------

    def _parse_12(self):
        t = self._text
        for name in self.FIELDS_12:
            m = re.search(rf'(?m)^[ \t]*{name}\b[ \t]+([^;]*);', t)
            if m:
                self._header[name] = {"orig": m.group(0),
                                      "value": m.group(1).strip(),
                                      "dirty": False}
        # ClassLookup：保留原本那個數字（它常是 class 檔總數，不是列數）
        m = re.search(r'(?m)^[ \t]*ClassLookup\b[ \t]+\d+[ \t]*\r?\n([\s\S]*?);', t)
        if m:
            self._classlookup_orig = m.group(0)
            for line in m.group(1).splitlines():
                cm = re.match(r'\s*(\d+)\s+"([^"]*)"', line)
                if cm:
                    self.class_lookup[int(cm.group(1))] = cm.group(2)

        m = re.search(r'DefectRecordSpec\s+\d+\s+([^;]+);', t)
        if m:
            self.defect_columns = m.group(1).split()

        matches = list(re.finditer(r'(?m)^[ \t]*DefectList\b[ \t]*\r?\n([\s\S]*?);', t))
        if len(matches) > 1:
            self.warnings.append(
                f"Detected {len(matches)} DefectLists (multi-test); only the first is editable.")
        if matches:
            self._defect_orig = matches[0].group(0)
            for line in matches[0].group(1).splitlines():
                line = line.strip()
                if line and not line.startswith(';'):
                    self.defects.append(line.split())

        # SummarySpec / SummaryList
        m = re.search(r'SummarySpec\s+\d+\s+([^;]+);', t)
        if m:
            self._summary_columns = m.group(1).split()
        m = re.search(r'(?m)^[ \t]*SummaryList\b[ \t]*\r?\n([\s\S]*?);', t)
        if m:
            self._summary_orig = m.group(0)
            for line in m.group(1).splitlines():
                line = line.strip()
                if line and not line.startswith(';'):
                    self._summary_rows.append(line.split())

    # ---------- 解析 1.8 ----------

    def _parse_18(self):
        t = self._text
        for m in re.finditer(r'Field\s+(\w+)\s+\d+\s*\{[^}]*\}', t):
            name = m.group(1)
            if name not in self._header:
                inner = m.group(0)[m.group(0).index('{') + 1:-1].strip()
                self._header[name] = {"orig": m.group(0), "value": inner, "dirty": False}

        # 1.8 把 LotID / WaferID 等放在 Record 的名稱上（例：Record LotRecord "N9S641.06"），
        # 不是 Field，所以另外抓出來，讓 Header 頁能和 1.2 一樣檢視／編輯。
        for m in re.finditer(r'Record\s+(\w+Record)\s+("(?:[^"\\]|\\.)*")', t):
            rec = m.group(1)
            name = REC_FIELD.get(rec)
            if name and name not in self._header:
                self._header[name] = {"orig": m.group(0), "value": m.group(2),
                                      "dirty": False, "rec": True}

        cl = re.search(r'List\s+ClassLookupList\s*\{', t)
        if cl:
            b0 = t.index('{', cl.start())
            b1 = _find_matching_brace(t, b0)
            body = t[b0 + 1:b1]
            dm = re.search(r'Data\s+\d+\s*\{', body)
            if dm:
                db0 = body.index('{', dm.start())
                db1 = _find_matching_brace(body, db0)
                for row in body[db0 + 1:db1].split(';'):
                    cm = re.match(r'\s*(\d+)\s+"([^"]*)"', row)
                    if cm:
                        self.class_lookup[int(cm.group(1))] = cm.group(2)

        dls = list(re.finditer(r'List\s+DefectList\s*\{', t))
        if len(dls) > 1:
            self.warnings.append(
                f"Detected {len(dls)} DefectLists; only the first is editable.")
        if dls:
            b0 = t.index('{', dls[0].start())
            b1 = _find_matching_brace(t, b0)
            block = t[b0:b1 + 1]
            cm = re.search(r'Columns\s+\d+\s*\{([^}]*)\}', block)
            if cm:
                cols = []
                for c in cm.group(1).split(','):
                    parts = c.strip().split()
                    cols.append(parts[-1] if parts else c.strip())
                    # 影像欄常不叫 IMAGELIST（例：ImageList ImageInfo），記型別位置
                    if len(parts) >= 2 and parts[0].lower() == 'imagelist':
                        self._il18 = len(cols) - 1
                self.defect_columns = cols
            dm = re.search(r'Data\s+\d+\s*\{', block)
            if dm:
                db0 = block.index('{', dm.start())
                db1 = _find_matching_brace(block, db0)
                self._defect_orig = block[dm.start():db1 + 1]   # 'Data N { ... }'
                for row in block[db0 + 1:db1].split(';'):
                    row = row.strip()
                    if row:
                        self.defects.append(re.findall(r'[^"\s]+|"[^"]*"', row))

    # ---------- 解析 TIFF patch 影像欄位 ----------

    def _parse_tiff_fields(self):
        t = self._text
        # 1.2：TiffFileName xxx; / TiffSpec 6.1 2 "IMAGEVERSION" "IMAGEXYPOS";
        m = re.search(r'(?m)^[ \t]*TiffFileName\b[ \t]+([^;]*);', t)
        if m:
            self.tiff_file_name = m.group(1).strip().strip('"')
        elif 'TiffFileName' in self._header:      # 1.8 放在 Field 裡
            self.tiff_file_name = self._header['TiffFileName']["value"].strip().strip('"')
        elif 'ImageFileName' in self._header:     # 1.8：Field ImageFileName {"x.tif", "TIF"}
            q = re.findall(r'"([^"]*)"', self._header['ImageFileName']["value"])
            if q:
                self.tiff_file_name = q[0]
        m = re.search(r'(?m)^[ \t]*TiffSpec\b[ \t]+([\d.]+)[ \t]+(\d+)[ \t]*([^;]*);', t)
        if m:
            fields = re.findall(r'"([^"]*)"', m.group(3)) or m.group(3).split()
            n = int(m.group(2))
            if fields and len(fields) != n:
                self.warnings.append(
                    f"TiffSpec declares {n} fields but lists {len(fields)}; using {len(fields)}.")
                n = len(fields)
            if n > 0:
                self.tiff_spec = {"version": m.group(1), "nfields": n, "fields": fields}
        elif 'TiffSpec' in self._header:          # 1.8：Field TiffSpec {"6.0", "G", "R"}
            vals = re.findall(r'"([^"]*)"', self._header['TiffSpec']["value"])
            if vals:
                # 這裡的欄位是「影像類型」（例：G/R），不是每張圖的 token 數
                self.tiff_spec = {"version": vals[0], "nfields": None, "fields": vals[1:]}

    # ---------- TIFF patch 影像對應 ----------

    def image_col_index(self):
        """回傳 (IMAGECOUNT 欄索引, 影像清單欄索引)；沒有為 -1。
           影像清單欄優先找名為 IMAGELIST 的欄，其次是 1.8 型別為
           ImageList 的欄（名稱不限，例：ImageInfo）。"""
        il = self.col_index('IMAGELIST')
        if il < 0 and self._il18 is not None:
            il = self._il18
        return self.col_index('IMAGECOUNT'), il

    def defect_image_count(self, row):
        ic, _ = self.image_col_index()
        if ic < 0 or ic >= len(row):
            return 0
        try:
            return max(0, int(row[ic]))
        except ValueError:
            return 0

    def image_layout(self):
        """影像條目在列中的排列：(start_col, nfields, how)；沒有帶圖資訊回 None。
           how = 'declared'（IMAGELIST 欄 + TiffSpec）或 'inferred'（從資料推斷）。

           有些 1.8 檔沒有 TiffSpec、影像欄也不叫 IMAGELIST（甚至掛在宣告欄位
           之後）；此時從所有帶圖列推斷：candidate 起點取「宣告欄位之後」與
           「IMAGECOUNT 的下一欄（若它是最後一欄）」，要求每列多出的 token 數
           都能被自己的 IMAGECOUNT 整除、且每張圖的 token 數全檔一致。"""
        if self._img_layout != 'unset':
            return self._img_layout
        ic, il = self.image_col_index()
        layout = None
        if ic >= 0:
            layout = self._detect_images18(ic, il)
            if layout is None:
                if il >= 0 and self.tiff_spec and self.tiff_spec.get("nfields"):
                    layout = (il, self.tiff_spec["nfields"], 'declared')
                else:
                    layout = self._infer_image_layout(ic, il)
        self._img_layout = layout
        return layout

    def _detect_images18(self, ic, il):
        """1.8 結構化影像欄：儲存格是 'Images N {id "type" ,id "type" …}' 子區塊。
           以第一筆帶圖列判定；回 (start_col, None, 'images18') 或 None。"""
        s = il if il >= 0 else ic + 1
        for r in self.defects:
            if self.defect_image_count(r) > 0:
                return (s, None, 'images18') if s < len(r) and r[s] == 'Images' else None
        return None

    def _infer_image_layout(self, ic, il):
        n = len(self.defect_columns)
        # 候選起點：影像清單欄本身、宣告欄位之後、IMAGECOUNT 的下一欄（若是最後一欄）
        starts = []
        for s in ([il] if il >= 0 else []) + [n] + ([n - 1] if ic == n - 2 else []):
            if s not in starts:
                starts.append(s)
        cands = []
        for s in starts:
            # 多數決：每張圖的 token 數取眾數，容忍少數壞列（health 會另外抓）
            tally, total = {}, 0
            for r in self.defects:
                cnt = self.defect_image_count(r)
                if cnt <= 0:
                    continue
                total += 1
                extra = len(r) - s
                if extra > 0 and extra % cnt == 0:
                    k = extra // cnt
                    tally[k] = tally.get(k, 0) + 1
            if not tally:
                continue
            nf, votes = max(tally.items(), key=lambda kv: kv[1])
            if nf >= 1 and votes >= total - max(1, total // 10):
                cands.append((s, nf, 'inferred'))   # 容忍最多 ~10%（至少 1 列）壞列
        if len(cands) <= 1:
            return cands[0] if cands else None
        # 多個一致解：優先挑「第一個欄位像 page 編號（全整數且不重複）」的
        for s, nf, how in cands:
            ids, good = [], True
            for r in self.defects:
                cnt = self.defect_image_count(r)
                toks = r[s:]
                if len(toks) < cnt * nf:
                    continue        # 壞列不參與判斷
                for k in range(cnt):
                    v = toks[k * nf]
                    if not v.lstrip('-').isdigit():
                        good = False
                        break
                    ids.append(int(v))
                if not good:
                    break
            if good and ids and len(set(ids)) == len(ids):
                return (s, nf, how)
        return cands[0]

    def defect_image_entries(self, row):
        """該列的影像條目 [[tok, ...], ...]，每張圖一條；解不出來回 []。"""
        cnt = self.defect_image_count(row)
        layout = self.image_layout()
        if cnt <= 0 or layout is None:
            return []
        s, nf, how = layout
        if how == 'images18':
            m = re.match(r'Images\s+\d+\s*\{(.*)\}\s*$', ' '.join(row[s:]))
            if not m:
                return []
            entries = [e.split() for e in m.group(1).split(',') if e.strip()]
            return entries if len(entries) == cnt else []
        toks = row[s:]
        if len(toks) < cnt * nf:
            return []
        return [toks[k * nf:(k + 1) * nf] for k in range(cnt)]

    def defect_image_filename(self, row) -> "Optional[str]":
        """回傳該列的 per-defect 影像檔名；沒有則回 None（ADEPT 增補）。

        1.8（rSEM 類）KLARF 的列尾常帶
        `Image N { "file.jpg" "JPG" ... }` 或 `Images N { ... }` 子區塊；
        取區塊內第一個帶引號的字串當檔名（概念移植自 GLAS klarf_parser 的
        _map_row_tokens / _image_filename，改寫在 raw-token 列表示上）。
        純唯讀查詢，不影響既有解析與無損寫回。"""
        for k, tok in enumerate(row):
            if tok in ('Image', 'Images'):
                tail = ' '.join(row[k:])
                b0 = tail.find('{')
                if b0 < 0:
                    return None
                b1 = _find_matching_brace(tail, b0)
                block = tail[b0 + 1:b1] if b1 > b0 else tail[b0 + 1:]
                m = re.search(r'"([^"]*)"', block)
                return m.group(1) if m else None
        return None

    def total_image_count(self):
        return sum(self.defect_image_count(r) for r in self.defects)

    def defect_image_map(self, n_pages=None):
        """建立 defect → TIFF page（0-based）的對應。

        回傳 {"mode": 'imagelist' | 'sequential' | None,
              "base": 0 | 1 | None,       # imagelist 模式時 id 的起算基準
              "pages": [ [page, ...] 依 self.defects 順序 ],
              "notes": [str]}

        mode 判定：
          - IMAGELIST 每條目的第一個欄位若全是整數且不重複，視為 TIFF 的
            page 編號（KLA 慣例，通常 1-based）→ 'imagelist'
          - 否則退回「依 defect 出現順序連續配頁」→ 'sequential'
        """
        notes = []
        ic, il = self.image_col_index()
        if ic < 0:
            return {"mode": None, "base": None,
                    "pages": [[] for _ in self.defects],
                    "notes": ["No IMAGECOUNT column; this KLARF carries no patch info."]}
        counts = [self.defect_image_count(r) for r in self.defects]
        total = sum(counts)
        if total == 0:
            return {"mode": None, "base": None,
                    "pages": [[] for _ in self.defects],
                    "notes": ["IMAGECOUNT is 0 for every defect."]}

        # 嘗試 imagelist 模式：第一欄全為整數且完整
        ids_per_row, usable = [], (self.image_layout() is not None)
        if usable:
            for r, cnt in zip(self.defects, counts):
                entries = self.defect_image_entries(r)
                if len(entries) != cnt or not all(
                        e and e[0].lstrip('-').isdigit() for e in entries):
                    usable = False
                    break
                ids_per_row.append([int(e[0]) for e in entries])
        layout = self.image_layout()
        if usable and layout and layout[2] == 'images18' and all(
                ids == list(range(1, len(ids) + 1)) for ids in ids_per_row):
            # 1.8 結構化格式：id 是「defect 內的圖序號」，不是全域 page 編號
            usable = False
            notes.append("ImageList ids are per-defect ordinals (1..IMAGECOUNT); "
                         "mapping pages sequentially in defect order.")
        if usable:
            flat = [i for ids in ids_per_row for i in ids]
            if len(set(flat)) != len(flat):
                usable = False
                notes.append("IMAGELIST ids contain duplicates; "
                             "falling back to sequential mapping.")
        if usable:
            lo, hi = min(flat), max(flat)
            if n_pages is not None:
                if lo >= 1 and hi <= n_pages and not (lo == 0):
                    base = 1
                elif lo >= 0 and hi <= n_pages - 1:
                    base = 0
                else:
                    usable = False
                    notes.append(
                        f"IMAGELIST ids [{lo}..{hi}] do not fit in {n_pages} TIFF pages; "
                        "falling back to sequential mapping.")
            else:
                base = 0 if lo == 0 else 1
        if usable:
            if base == 1:
                notes.append("IMAGELIST first field treated as 1-based TIFF page number.")
            else:
                notes.append("IMAGELIST first field treated as 0-based TIFF page index.")
            return {"mode": "imagelist", "base": base,
                    "pages": [[i - base for i in ids] for ids in ids_per_row],
                    "notes": notes}

        # sequential：依出現順序連續分配
        pages, cum = [], 0
        for cnt in counts:
            pages.append(list(range(cum, cum + cnt)))
            cum += cnt
        if n_pages is not None and total != n_pages:
            notes.append(f"Sum of IMAGECOUNT ({total}) != TIFF page count ({n_pages}); "
                         "sequential mapping may be off.")
        notes.append("Pages assigned sequentially in defect order.")
        return {"mode": "sequential", "base": None, "pages": pages, "notes": notes}

    def tiff_path(self):
        """猜出對應的 TIFF 檔路徑（存在才回傳）。
           依序嘗試：TiffFileName（含只取檔名放同資料夾）、KLARF 同名 .tif/.tiff。"""
        cands = []
        base_dir = os.path.dirname(self.source_path) if self.source_path else None
        if self.tiff_file_name:
            name = self.tiff_file_name.replace('\\', '/')
            cands.append(name)                          # 絕對或相對於 cwd
            if base_dir is not None:
                cands.append(os.path.join(base_dir, os.path.basename(name)))
        if self.source_path:
            stem = os.path.splitext(self.source_path)[0]
            for p in (stem, self.source_path):
                for ext in ('.tif', '.tiff', '.TIF', '.TIFF'):
                    cands.append(p + ext)
        seen = set()
        for c in cands:
            if c and c not in seen:
                seen.add(c)
                if os.path.isfile(c):
                    return c
        return None



def load(src):
    """src 可為檔案路徑或 KLARF 文字內容。"""
    if isinstance(src, str) and ('\n' not in src) and os.path.exists(src):
        with open(src, 'r', encoding='utf-8', errors='replace') as f:
            return KlarfDoc(f.read(), source_path=src)
    return KlarfDoc(src)
