# Vendored into GLAS on 2026-08-17 (F31 M1).
# Source project: ADEPT — file: adept/core/ingest/tiff_index.py @ 13153f4
#   (which itself vendored KLIP's klarf_tif_probe.py on 2026-07-27: the
#   dependency-free TIFF IFD walker, not the CLI / probe / report code).
# Adaptations for GLAS:
#   - copied verbatim; only this header and read_sem_gray() below are new.
#   - GLAS's core modules bare-import their siblings off sys.path (CLAUDE.md
#     §4), so there is no package-relative import to rewrite here.
#   - read_sem_gray() is the GLAS-side single read entry (see its docstring):
#     ADEPT reads pages straight into its own pipeline, GLAS needs one entry
#     that keeps `page is None` byte-identical to today's cv2.imread.
#
# ⚠ Do NOT "simplify" the pid in _stat_key or the lock in read_page. Both guard
# real, silent corruption in GLAS's export path specifically: the batch export
# runs on a ProcessPool (fork on Linux → copied fds share one file offset) and
# the interactive preview runs on a QThread (two threads sharing one handle).
# Their original ADEPT/KLIP comments explaining why are kept verbatim below.
"""TIFF 結構索引：只讀 IFD（不解碼像素），純標準函式庫。

支援 classic TIFF 與 BigTIFF、兩種 byte order。像素解碼（read_page）
另以 lazy import 的 tifffile 完成。

GLAS 額外提供 :func:`read_sem_gray` —— 對位 / 匯出讀 SEM 影像的**單一入口**
（單頁走原本的 ``cv2.imread``、多頁走 :func:`read_page`）。
"""
from __future__ import annotations

import os
import struct
import threading
from collections import OrderedDict


# ---------------------------------------------------------------- TIFF 走訪
# 只讀 IFD 結構（不解碼像素），純標準函式庫。支援 classic TIFF 與 BigTIFF、
# 兩種 byte order。

TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4,
             10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}

COMPRESSION = {1: "none", 2: "CCITT-RLE", 3: "CCITT-G3", 4: "CCITT-G4",
               5: "LZW", 6: "old-JPEG", 7: "JPEG", 8: "deflate",
               32773: "PackBits", 32946: "deflate"}

PHOTOMETRIC = {0: "white-is-zero", 1: "black-is-zero", 2: "RGB",
               3: "palette", 4: "mask", 5: "CMYK", 6: "YCbCr"}

TAGS = {254: "subfile", 256: "width", 257: "height", 258: "bits",
        259: "compression", 262: "photometric", 270: "description",
        273: "strip_offsets", 277: "spp", 279: "strip_bytes",
        297: "page_number", 305: "software", 306: "datetime",
        322: "tile_w", 323: "tile_h", 324: "tile_offsets", 325: "tile_bytes"}


def _read_values(f, en, vtype, count, raw, big):
    """把 IFD entry 的 value/offset 解成 python 值 list。"""
    size = TYPE_SIZE.get(vtype)
    if size is None:
        return None
    total = size * count
    inline = 8 if big else 4
    if total <= inline:
        data = raw[:total]
    else:
        off = struct.unpack(en + ('Q' if big else 'I'), raw)[0]
        pos = f.tell()
        f.seek(off)
        data = f.read(total)
        f.seek(pos)
        if len(data) < total:
            return None
    if vtype == 2:                        # ASCII
        return [data.split(b'\x00')[0].decode('latin-1', 'replace')]
    fmt = {1: 'B', 3: 'H', 4: 'I', 6: 'b', 7: 'B', 8: 'h', 9: 'i',
           11: 'f', 12: 'd', 13: 'I', 16: 'Q', 17: 'q'}.get(vtype)
    if fmt is None:
        if vtype in (5, 10):              # rational
            f2 = 'I' if vtype == 5 else 'i'
            vals = struct.unpack(en + f2 * (count * 2), data)
            return [vals[i] / vals[i + 1] if vals[i + 1] else 0.0
                    for i in range(0, count * 2, 2)]
        return None
    return list(struct.unpack(en + fmt * count, data))


def _open_header(f):
    """讀 TIFF 檔頭，回傳 ``(endian, is_bigtiff, first_ifd_offset)``。"""
    head = f.read(8)
    if len(head) < 8:
        raise ValueError("File too short to be a TIFF.")
    if head[:2] == b'II':
        en = '<'
    elif head[:2] == b'MM':
        en = '>'
    else:
        raise ValueError("Not a TIFF (missing II/MM byte-order mark).")
    magic = struct.unpack(en + 'H', head[2:4])[0]
    big = (magic == 43)
    if magic == 42:
        next_ifd = struct.unpack(en + 'I', head[4:8])[0]
    elif big:
        off_size, zero = struct.unpack(en + 'HH', head[4:8])
        if off_size != 8 or zero != 0:
            raise ValueError("Malformed BigTIFF header.")
        next_ifd = struct.unpack(en + 'Q', f.read(8))[0]
    else:
        raise ValueError(f"Unknown TIFF magic {magic} (expected 42 or 43).")
    return en, big, next_ifd


def count_pages(path) -> int:
    """**只數頁數**：走 IFD 鏈，但完全不解析 tag。

    為什麼要另外寫一支
    ------------------
    ``load_dataset`` 對這個 TIFF 唯一的問題是「有幾頁」（拿去跟 KLARF 的
    defect 數對映），但它以前是呼叫 :func:`read_tiff_pages` —— 那支會**每一頁
    都把所有認得的 tag 讀出來**，而讀 tag 常常要為了 out-of-line 的值再 seek
    一次。實測 4000 頁要 116 ms，而那份資料除了 ``n_pages`` 以外一個欄位都沒被
    用到。

    這一支每頁只做三件事：seek 到 IFD、讀 entry 數、跳過那些 entry 讀下一個
    IFD 位址。同樣是純標準函式庫。

    ⚠ 廠內的 TIFF 可能放在網路碟上，那裡每一次 seek 都是一次來回 —— 少做的
    seek 不只是省 CPU。
    """
    with open(path, 'rb') as f:
        en, big, next_ifd = _open_header(f)
        esize = 20 if big else 12
        n_off = 8 if big else 4
        count_size = 8 if big else 2
        seen = set()
        n = 0
        while next_ifd:
            if next_ifd in seen or n > 100000:
                raise ValueError("IFD chain loops — corrupt TIFF.")
            seen.add(next_ifd)
            f.seek(next_ifd)
            n_entries = struct.unpack(en + ('Q' if big else 'H'),
                                      f.read(count_size))[0]
            n += 1
            f.seek(next_ifd + count_size + esize * n_entries)
            next_ifd = struct.unpack(en + ('Q' if big else 'I'),
                                     f.read(n_off))[0]
        return n


def read_tiff_pages(path):
    """回傳 (pages, info)。pages = [dict,...]（每頁一筆），info = 檔案層資訊。

    要**完整的每頁資訊**（`fab_probe` 的報告、健檢）才用這支；只要頁數請用
    :func:`count_pages`，它便宜一個數量級。
    """
    pages = []
    with open(path, 'rb') as f:
        en, big, next_ifd = _open_header(f)

        seen = set()
        while next_ifd:
            if next_ifd in seen or len(pages) > 100000:
                raise ValueError("IFD chain loops — corrupt TIFF.")
            seen.add(next_ifd)
            f.seek(next_ifd)
            n = struct.unpack(en + ('Q' if big else 'H'),
                              f.read(8 if big else 2))[0]
            page = {"index": len(pages), "ifd_offset": next_ifd}
            esize = 20 if big else 12
            entries = f.read(esize * n)
            for k in range(n):
                e = entries[k * esize:(k + 1) * esize]
                tag, vtype = struct.unpack(en + 'HH', e[:4])
                if big:
                    count = struct.unpack(en + 'Q', e[4:12])[0]
                    raw = e[12:20]
                else:
                    count = struct.unpack(en + 'I', e[4:8])[0]
                    raw = e[8:12]
                name = TAGS.get(tag)
                if name is None:
                    continue
                vals = _read_values(f, en, vtype, count, raw, big)
                if vals is None:
                    continue
                page[name] = vals[0] if len(vals) == 1 else vals
            b = page.get("strip_bytes", page.get("tile_bytes", 0))
            page["data_bytes"] = sum(b) if isinstance(b, list) else b
            pages.append(page)
            next_ifd = struct.unpack(en + ('Q' if big else 'I'),
                                     f.read(8 if big else 4))[0]

    info = {"byte_order": "little-endian (II)" if en == '<' else "big-endian (MM)",
            "bigtiff": big, "n_pages": len(pages),
            "file_bytes": os.path.getsize(path)}
    return pages, info


def _page_geom(p):
    bits = p.get("bits", "?")
    if isinstance(bits, list):
        bits = "/".join(str(b) for b in bits)
    return (f'{p.get("width", "?")}x{p.get("height", "?")} '
            f'{bits}-bit {COMPRESSION.get(p.get("compression", 1), "compression?")} '
            f'{PHOTOMETRIC.get(p.get("photometric", -1), "")}'.strip())


# ------------------------------------------------------- ADEPT additions

def n_pages(path) -> int:
    """回傳多頁 TIFF 的頁數（純 IFD 走訪，不解碼像素、不需第三方套件）。"""
    return count_pages(path)


# --------------------------------------------------------------------------- #
# 開著的 TIFF handle 快取
# --------------------------------------------------------------------------- #
#: ``path -> (TiffFile, stat_key)``。只留少數幾個 —— 一個 lot 通常就一個 TIFF。
_OPEN: "OrderedDict[str, tuple]" = OrderedDict()
_OPEN_MAX = 4
#: RLock：``read_page`` 全程持有，而它會呼叫也要拿鎖的取 handle。
_OPEN_LOCK = threading.RLock()


def _stat_key(path):
    st = os.stat(path)
    # **pid 是版本鍵的一部分**，這不是多餘的。
    #
    # 批次是 fork 出來的（`batch._pool_context`：主執行緒 fork）。fork 會把父
    # 行程開著的檔案描述子複製給每一個子行程，而複製出來的 fd **共用同一個檔案
    # 偏移量** —— 於是四個 worker 各自 seek 再 read，會互相把對方的位置移掉，
    # 讀回來的是別頁的位元組。那不會丟例外、不會變慢，只會讓某幾顆 defect 拿到
    # 錯的影像。
    #
    # 帶上 pid，子行程一進來就發現「這個 handle 不是我的」而重開一個自己的。
    return (os.getpid(), st.st_mtime_ns, st.st_size)


def _tiff_handle_locked(path):
    """拿一個開好的 ``tifffile.TiffFile``（同一個檔就重複使用）。

    **呼叫端必須持有 ``_OPEN_LOCK``**（見 :func:`read_page` 的說明）。

    為什麼要快取
    ------------
    ``read_page`` 以前每次呼叫都 ``with tifffile.TiffFile(path)`` 開一次檔，
    而且用 ``len(tf.pages)`` 檢查範圍 —— **那一行會強迫 tifffile 走完整條 IFD
    鏈**。於是「換下一顆 defect」＝ 兩張圖 ＝ 兩次全檔走訪：實測一個 4000 頁的
    TIFF 每張圖 16 ms，而那張圖本身只有 16 KB。整條 pipeline 才 9 ms，
    所以絕大部分的等待花在重複打開同一個檔案上。

    快取以 ``(mtime_ns, size)`` 當版本：檔案被換掉就重開，不會餵回舊內容。

    行程內共用，所以要鎖 —— 預覽跑在 QThread、批次跑在子行程（各自一份快取）。
    """
    import tifffile  # lazy: pixel decode only; structural code above stays pure-stdlib

    path = str(path)
    key = _stat_key(path)
    with _OPEN_LOCK:                      # RLock：呼叫端通常已經持有
        hit = _OPEN.get(path)
        if hit is not None and hit[1] == key:
            _OPEN.move_to_end(path)
            return hit[0]
        if hit is not None:               # 檔案變了：關掉舊的
            try:
                hit[0].close()
            except Exception:             # noqa: BLE001 — 關檔失敗不該擋住讀取
                pass
            _OPEN.pop(path, None)
        tf = tifffile.TiffFile(path)
        # **開檔的當下就把整份頁面清單建起來，一次。**
        #
        # tifffile 的 ``tf.pages`` 是 lazy 的：索引到第 i 頁才去解析那一段 IFD，
        # 而它靠的是「檔案位置停在上一頁結尾」這個內部狀態。問題是
        # ``page.asarray()`` 會為了讀像素移動檔案位置 —— 所以「讀第 0 頁的像素、
        # 再要第 1 頁」在**同一個 handle** 上會從錯的位置開始解析，tifffile 丟出
        # ``suspicious number of tags 8827`` 之類的訊息。
        #
        # 以前每次讀都重開檔，所以碰不到；快取住 handle 之後它每次換 defect 都
        # 會發生（實測第二張圖必失敗）。``len()`` 逼它把清單一次走完，之後的索引
        # 就只是查表，不再依賴檔案位置。
        #
        # 這一趟 IFD 走訪本來就要付（原本是**每讀一張圖付一次**）；現在是
        # 每開一次檔付一次。4000 頁的檔案：第一次 35 ms，之後每張 0.4 ms。
        len(tf.pages)
        _OPEN[path] = (tf, key)
        while len(_OPEN) > _OPEN_MAX:
            _old, (old_tf, _k) = _OPEN.popitem(last=False)
            try:
                old_tf.close()
            except Exception:             # noqa: BLE001
                pass
        return tf


def close_cached_tiffs() -> None:
    """關掉所有快取的 TIFF handle（換 lot、關視窗、測試收尾時呼叫）。"""
    with _OPEN_LOCK:
        for tf, _key in _OPEN.values():
            try:
                tf.close()
            except Exception:             # noqa: BLE001
                pass
        _OPEN.clear()


def read_page(path, page_index):
    """解碼並回傳第 page_index（0-based）頁的像素 (np.ndarray)。

    像素解碼交給 tifffile（lazy import：只有真的要讀像素才需要它）。
    檔案 handle 會被快取重複使用，見 :func:`_tiff_handle`。

    **整段讀取是序列化的**（鎖從拿 handle 一路持有到 ``asarray()`` 回來）。
    一個 ``TiffFile`` 底下就是一個檔案描述子，而讀一頁像素是「seek 到那一頁、
    讀下去」—— 兩個執行緒共用同一個 handle 就會互相把位置移掉，讀回來的是別頁
    的位元組。Studio 真的會這樣：點一張卡會排一次背景預覽，而使用者可能同時
    觸發另一次，兩個執行緒就同時進來了。實測症狀是 tifffile 丟
    ``suspicious number of tags 13111`` —— 它把像素當成 IFD 在解析。

    這跟 :func:`_stat_key` 裡的 pid 是同一件事的兩半：**fork 出來的子行程共用
    偏移量、同一行程的執行緒共用 handle**。前者用版本鍵擋，後者用鎖擋。

    序列化的代價可以忽略：一次讀 0.4 ms，而預覽本來就有請求合併。
    """
    with _OPEN_LOCK:
        tf = _tiff_handle_locked(path)
        # 頁面清單在開檔時就建好了（見 :func:`_tiff_handle_locked`），所以問總數
        # 不用再走一次 IFD —— 範圍檢查是免費的，訊息也講得出「總共幾頁」。
        n = len(tf.pages)
        if page_index < 0 or page_index >= n:
            raise IndexError(
                f"TIFF page {page_index} out of range (0..{n - 1})")
        return tf.pages[page_index].asarray()


# ── GLAS additions (F31 M1) ──────────────────────────────────────────────────

def read_sem_gray(path, page=None):
    """Read ONE SEM frame as 8-bit single-channel grayscale.

    The single read entry for fine-align / export (``fine_align`` and
    ``overlay_export`` both call this instead of ``cv2.imread``), so the
    "which page" decision lives in exactly one place.

    ``page`` is a 0-based TIFF page index, or ``None`` for a plain
    single-image file.

    **``page is None`` is byte-identical to the pre-F31 behaviour** — it is
    literally ``cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)``, including its
    ``None``-on-failure contract, so every existing single-image flow (rSEM
    KLARF, folder mode) is unaffected. Only a multi-page read takes the new
    path.

    Returns the frame, or ``None`` when it cannot be read — callers already
    treat ``None`` as "missing-file" (:func:`overlay_export.export_one_image`),
    so a multi-page failure degrades exactly like a missing single image
    instead of killing a batch.

    cv2 is imported lazily so the structural half of this module (the IFD
    walk in :func:`count_pages`) stays pure-stdlib and importable without it.
    """
    try:
        import cv2  # noqa: PLC0415 - lazy: keeps the IFD walker dependency-free
    except Exception:  # pragma: no cover - cv2 is a hard dep of the app
        return None
    if page is None:
        return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    img = _read_page_any(str(path), int(page), cv2)
    return None if img is None else _to_gray_u8(img, cv2)


def _read_page_any(path, page, cv2):
    """One TIFF page as a raw ndarray: tifffile if present, else cv2.

    ``tifffile`` is the wanted path — :func:`read_page` reuses a cached handle
    and costs ~0.4 ms per page after the first. The cv2 fallback keeps GLAS
    working when tifffile isn't installed (it is an optional requirement), at a
    real cost: ``imreadmulti`` without page bounds **decodes every page** of the
    file, so a big batch TIFF is slow. Newer OpenCV takes ``start``/``count``,
    which avoids that; we try it first and fall back to decoding all pages.
    """
    try:
        return read_page(path, page)
    except ImportError:
        pass                     # no tifffile → cv2 fallback below
    except (OSError, ValueError, IndexError):
        return None
    try:
        ok, pages = cv2.imreadmulti(path, [], page, 1, cv2.IMREAD_UNCHANGED)
        if ok and pages:
            return pages[0]
    except (cv2.error, TypeError):
        pass                     # older cv2: no start/count overload
    try:
        ok, pages = cv2.imreadmulti(path, [], cv2.IMREAD_UNCHANGED)
    except cv2.error:
        return None
    if not ok or page >= len(pages):
        return None
    return pages[page]


def _to_gray_u8(img, cv2):
    """Coerce a decoded page to 8-bit single-channel.

    Same rules as ADEPT's ``ingest/imageio.load_gray`` (BGRA/BGR → gray;
    non-uint8 → min-max normalised to 0..255), so a page GLAS aligns against is
    the same array ADEPT sees for that page. Note this differs from
    ``cv2.IMREAD_GRAYSCALE`` on a 16-bit file, which rescales by a fixed
    factor rather than per-image min/max — it only applies to the multi-page
    path, never to the ``page is None`` path above.
    """
    import numpy as np  # noqa: PLC0415 - lazy, same reason as cv2 above
    if img.ndim == 3:
        img = cv2.cvtColor(
            img, cv2.COLOR_BGRA2GRAY if img.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
    if img.dtype != np.uint8:
        img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return img
