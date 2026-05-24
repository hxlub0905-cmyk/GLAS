"""KLARF file parser — supports two formats.

Flat KLARF 1.8 (legacy)
────────────────────────
  FileVersion 1 8
  RecordFileRecord "..."
  DefectList N col1 col2 …
  Data N
  1 "img.tif" 100 200 ;
  …
  EndOfList

Hierarchical KLARF (KLA Tencor extended)
─────────────────────────────────────────
  Record FileRecord "1.8"
  {
    Record LotRecord "..."
    {
      Record WaferRecord "..."
      {
        Field ...
        List DefectList
        {
          Columns 42 { int32 DEFECTID, int32 XREL, ... ImageList IMAGEINFO, ... }
          Data N
          {
            257 7672524 … Images 1 { "file.jpg" "JPG" 1 "23" } … ;
            …
          }
        }
      }
    }
  }

Format is auto-detected from the first non-blank line:
  starts with "record filerecord" (case-insensitive) → hierarchical
  otherwise                                           → flat

Both formats produce the same parsed-dict schema so that KlarfWriter and
KlarfTopNExporter work unchanged.  Hierarchical format adds extra keys
(hier_prefix / hier_suffix / …) used by the writer to reconstruct the
file faithfully.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# ── Tokeniser ─────────────────────────────────────────────────────────────────

_QUOTED  = re.compile(r'"[^"]*"')
_NEWLINE = re.compile(r'\r\n|\r|\n')


def _tokenise(text: str) -> list[str]:
    """Split a line (or block) of KLARF text into tokens, keeping quoted strings intact."""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in (' ', '\t', '\r', '\n'):
            i += 1
            continue
        if ch == '"':
            end = text.find('"', i + 1)
            if end == -1:
                tokens.append(text[i:])
                break
            tokens.append(text[i:end + 1])
            i = end + 1
        else:
            j = i
            while j < n and text[j] not in (' ', '\t', '\r', '\n'):
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


# ── Public parser ─────────────────────────────────────────────────────────────

class KlarfParser:
    """Parse a KLARF file (flat or hierarchical).

    Returns a dict with keys:
      format_type           str          – "flat" | "hierarchical"
      raw_sections          list[dict]   – flat format only; non-DefectList sections
      defect_columns        list[str]    – column names (no type prefix)
      defect_column_types   list[str]    – type prefix per column (hierarchical only)
      defects               list[dict]   – one dict per defect, keyed by column name
      image_count           int
      data_count            int
      defect_list_header_raw str         – flat format verbatim DefectList header line
      hier_prefix           str          – hierarchical: text before "Data N" line
      hier_data_indent      str          – hierarchical: whitespace indent of Data line
      hier_rows_indent      str          – hierarchical: whitespace indent of data rows
      hier_suffix           str          – hierarchical: text from closing "}" of Data block
    """

    def parse(self, filepath: str | Path) -> dict[str, Any]:
        text = Path(filepath).read_text(encoding="latin-1")
        return self._parse_text(text)

    def parse_text(self, text: str) -> dict[str, Any]:
        return self._parse_text(text)

    # ── Format detection ──────────────────────────────────────────────────────

    def _parse_text(self, text: str) -> dict[str, Any]:
        lines = _NEWLINE.split(text)
        first_nonblank = next((l.strip() for l in lines if l.strip()), "")

        if first_nonblank.lower().startswith("record filerecord"):
            _log.debug(
                "[parser] Detected HIERARCHICAL format (first line: %r)",
                first_nonblank[:80],
            )
            return self._parse_hierarchical(lines)

        _log.debug(
            "[parser] Detected FLAT format (first line: %r)",
            first_nonblank[:80],
        )
        return self._parse_by_lines(lines)

    # ── Hierarchical parser ───────────────────────────────────────────────────

    def _parse_hierarchical(self, lines: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "format_type":            "hierarchical",
            "raw_sections":           [],
            "defect_columns":         [],
            "defect_column_types":    [],
            "defects":                [],
            "image_count":            0,
            "data_count":             0,
            "defect_list_header_raw": "",
            "hier_prefix":            "",
            "hier_data_indent":       "        ",
            "hier_rows_indent":       "          ",
            "hier_suffix":            "",
        }
        n = len(lines)

        # ── Step 1: locate "List DefectList" ──────────────────────────────────
        defect_list_i = -1
        for idx, line in enumerate(lines):
            if line.strip().lower() == "list defectlist":
                defect_list_i = idx
                _log.debug("[parser] 'List DefectList' found at line %d", idx)
                break

        if defect_list_i == -1:
            _log.warning("[parser] 'List DefectList' NOT found — defects will be empty")
            return result

        # ── Step 2: parse "Columns N { type COL, … }" ────────────────────────
        i = defect_list_i + 1
        columns_end_i = defect_list_i
        while i < n:
            stripped = lines[i].strip()
            if stripped.lower().startswith("columns "):
                brace_depth = 0
                col_lines: list[str] = []
                j = i
                while j < n:
                    line = lines[j]
                    col_lines.append(line)
                    for ch in line:
                        if ch == "{":
                            brace_depth += 1
                        elif ch == "}":
                            brace_depth -= 1
                    if brace_depth <= 0:
                        columns_end_i = j
                        break
                    j += 1

                col_text = "\n".join(col_lines)
                open_idx  = col_text.find("{")
                close_idx = col_text.rfind("}")
                if open_idx != -1 and close_idx != -1:
                    inner = col_text[open_idx + 1 : close_idx]
                    cols, types = _parse_column_defs(inner)
                    result["defect_columns"]      = cols
                    result["defect_column_types"] = types
                _log.debug(
                    "[parser] Columns parsed: %d → %s",
                    len(result["defect_columns"]),
                    result["defect_columns"],
                )
                i = columns_end_i + 1
                break
            i += 1

        # ── Step 3: locate "Data N" ───────────────────────────────────────────
        data_i = -1
        while i < n:
            stripped = lines[i].strip()
            if stripped.lower().startswith("data "):
                data_i = i
                raw_indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                result["hier_data_indent"] = raw_indent
                try:
                    result["data_count"] = int(stripped.split()[1])
                except (IndexError, ValueError):
                    pass
                _log.debug(
                    "[parser] 'Data' found at line %d (indent=%r, declared count=%d)",
                    i, raw_indent, result["data_count"],
                )
                break
            i += 1

        if data_i == -1:
            _log.warning("[parser] 'Data N' NOT found inside DefectList")
            return result

        # Everything before the Data line is kept verbatim for the writer.
        result["hier_prefix"] = "\n".join(lines[:data_i])

        # ── Step 4: skip opening "{" of data block ────────────────────────────
        i = data_i + 1
        while i < n:
            if lines[i].strip() == "{":
                i += 1  # move past the opening brace
                break
            i += 1

        # ── Step 5: collect row lines until closing "}" ───────────────────────
        row_lines: list[str] = []
        while i < n:
            if lines[i].strip() == "}":
                break  # closing brace of Data block
            row_lines.append(lines[i])
            i += 1

        data_close_i = i  # the "}" line itself

        # Detect row indent from first non-empty row line
        rows_indent = result["hier_data_indent"] + "  "
        for rl in row_lines:
            if rl.strip():
                rows_indent = rl[: len(rl) - len(rl.lstrip())]
                break
        result["hier_rows_indent"] = rows_indent

        # Suffix = the closing "}" of Data block and everything that follows
        result["hier_suffix"] = "\n".join(lines[data_close_i:])

        # ── Step 6: parse defect rows ─────────────────────────────────────────
        columns = result["defect_columns"]
        defects: list[dict[str, Any]] = []
        row_tokens: list[str] = []

        for line in row_lines:
            toks = _tokenise(line)
            row_tokens.extend(toks)
            if row_tokens and row_tokens[-1] == ";":
                row_tokens.pop()
                if row_tokens:
                    defect = self._map_row_tokens(row_tokens, columns)
                    defects.append(defect)
                    _log.debug(
                        "[parser]   defect[%d] _image_filename=%r",
                        len(defects) - 1,
                        defect.get("_image_filename", ""),
                    )
                row_tokens = []

        result["defects"] = defects
        _log.debug("[parser] Total defects parsed (hierarchical): %d", len(defects))
        return result

    # ── Flat KLARF 1.8 parser ─────────────────────────────────────────────────

    def _parse_by_lines(self, lines: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "format_type":            "flat",
            "raw_sections":           [],
            "defect_columns":         [],
            "defect_column_types":    [],
            "defects":                [],
            "image_count":            0,
            "data_count":             0,
            "defect_list_header_raw": "",
            "hier_prefix":            "",
            "hier_data_indent":       "",
            "hier_rows_indent":       "",
            "hier_suffix":            "",
        }

        i = 0
        n = len(lines)

        while i < n:
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue

            # ── DefectList section ─────────────────────────────────────────────
            if stripped.startswith("DefectList"):
                result["defect_list_header_raw"] = lines[i]
                parts = stripped.split()
                columns = parts[2:] if len(parts) > 2 else []
                result["defect_columns"] = columns

                i += 1
                if i < n and lines[i].strip().lower().startswith("data "):
                    data_parts = lines[i].strip().split()
                    try:
                        result["data_count"] = int(data_parts[1])
                    except (IndexError, ValueError):
                        pass
                    i += 1

                i = self._parse_defect_rows(lines, i, columns, result)
                _log.debug(
                    "[parser] Flat DefectList: %d columns, %d defects",
                    len(columns), len(result["defects"]),
                )
                continue

            # ── SummaryRecord / TestSummaryList ────────────────────────────────
            if stripped.startswith("SummaryRecord") or stripped.startswith("TestSummaryList"):
                section_lines = [lines[i]]
                i += 1
                while i < n:
                    section_lines.append(lines[i])
                    if lines[i].strip().startswith("EndOfList"):
                        i += 1
                        break
                    i += 1
                raw_text = "\n".join(section_lines)
                result["raw_sections"].append({
                    "name": stripped.split()[0],
                    "text": raw_text,
                })
                continue

            # ── All other sections ─────────────────────────────────────────────
            keyword = stripped.split()[0] if stripped else ""
            if keyword and keyword[0].isupper() and keyword not in ("Data",):
                section_lines = [lines[i]]
                i += 1
                while i < n:
                    next_stripped = lines[i].strip()
                    if next_stripped and _is_section_start(next_stripped) and not _is_continuation(next_stripped):
                        break
                    section_lines.append(lines[i])
                    i += 1
                raw_text = "\n".join(section_lines)
                for sl in section_lines:
                    m = re.search(r'\bIMAGECOUNT\b\s+(\d+)', sl)
                    if m:
                        try:
                            result["image_count"] = int(m.group(1))
                        except ValueError:
                            pass
                result["raw_sections"].append({"name": keyword, "text": raw_text})
                continue

            i += 1

        return result

    def _parse_defect_rows(
        self,
        lines: list[str],
        start: int,
        columns: list[str],
        result: dict[str, Any],
    ) -> int:
        i = start
        n = len(lines)
        defects = result["defects"]

        while i < n:
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue
            if stripped.startswith("EndOfList"):
                i += 1
                break

            row_tokens: list[str] = []
            while i < n:
                line_stripped = lines[i].strip()
                if line_stripped.startswith("EndOfList"):
                    break
                toks = _tokenise(lines[i])
                row_tokens.extend(toks)
                i += 1
                if row_tokens and row_tokens[-1] == ";":
                    break

            if not row_tokens:
                continue
            if row_tokens and row_tokens[-1] == ";":
                row_tokens = row_tokens[:-1]

            defect = self._map_row_tokens(row_tokens, columns)
            defects.append(defect)

        return i

    def _map_row_tokens(self, tokens: list[str], columns: list[str]) -> dict[str, Any]:
        """Map a flat token list (one defect row) to a column-keyed dict.

        Handles both "Image N { … }" (flat) and "Images N { … }" (hierarchical)
        blocks as a single compound token stored in _image_block_raw.
        """
        result: dict[str, Any] = {}
        col_idx = 0
        tok_idx = 0
        n_tok = len(tokens)
        n_col = len(columns)

        while tok_idx < n_tok:
            tok = tokens[tok_idx]

            # ── Image / Images block ───────────────────────────────────────────
            if tok in ("Image", "Images") and tok_idx + 1 < n_tok:
                block_tokens = [tok]
                tok_idx += 1
                while tok_idx < n_tok:
                    block_tokens.append(tokens[tok_idx])
                    tok_idx += 1
                    if tokens[tok_idx - 1] == "}":
                        break

                # Extract first quoted string inside braces as the filename
                image_filename = ""
                in_brace = False
                for bt in block_tokens:
                    if bt == "{":
                        in_brace = True
                        continue
                    if bt == "}":
                        break
                    if in_brace and bt.startswith('"'):
                        image_filename = _unquote(bt)
                        break

                result["_image_block_raw"] = " ".join(block_tokens)
                result["_image_filename"]  = image_filename
                _log.debug(
                    "[parser]     image block: %r → filename=%r",
                    result["_image_block_raw"][:60],
                    image_filename,
                )

                # Advance column index past the ImageInfo slot
                if col_idx < n_col and "image" in columns[col_idx].lower():
                    col_idx += 1
                continue

            # ── Normal token ──────────────────────────────────────────────────
            if col_idx < n_col:
                result[columns[col_idx]] = tok
            else:
                result[f"_extra_{tok_idx}"] = tok
            col_idx += 1
            tok_idx += 1

        return result


# ── Module-level helpers ──────────────────────────────────────────────────────

def _parse_column_defs(inner: str) -> tuple[list[str], list[str]]:
    """Parse 'int32 COL1,  float COL2,  ImageList COL3' → (names, types).

    Each definition is separated by commas.  The first whitespace-separated
    token is the type; the second is the column name.
    """
    columns: list[str] = []
    types:   list[str] = []
    for defn in inner.split(","):
        parts = defn.split()
        if len(parts) >= 2:
            types.append(parts[0])
            columns.append(parts[1])
        elif len(parts) == 1 and parts[0] and not parts[0][0].isdigit():
            # Lone name without a type prefix
            types.append("")
            columns.append(parts[0])
    return columns, types


_TOP_LEVEL_KEYWORDS = {
    "RecordFileRecord", "LotRecord", "WaferRecord", "DefectList",
    "SummaryRecord", "TestRecord", "ClassLookupList", "EndOfList",
    "TestSummaryList", "FileVersion",
}


def _is_section_start(stripped: str) -> bool:
    word = stripped.split()[0] if stripped else ""
    return word in _TOP_LEVEL_KEYWORDS or (
        word and word[0].isupper() and "Record" in word
    )


def _is_continuation(stripped: str) -> bool:
    return bool(stripped) and stripped[0].isdigit()
