#!/usr/bin/env python3
# GLAS 更新搬運檔 — authored 2026-07-30.
"""**在家用機上**改完程式碼之後跑這一支：重產公司機拿得到的那兩樣東西。

    git add -A && python tools/release.py && git add -A

⚠ **這支是給有 git 的那台機器（家用機）用的。** 公司機不能執行 git 操作，
也不需要跑這支 —— 它只負責「拿到程式碼並執行」，見 `AGENTS.md` §2。

它做兩件事，順序不能顛倒：

1. ``tools/FILELIST.txt`` —— 全部檔案的 git blob SHA（分批搬運時用來回報
   「還缺哪幾個」）。
2. ``bundle/GLAS_bundle.py`` —— 整個 repo 壓成**一個**純文字 `.py`，
   在 GitHub 上複製 raw 就能整包搬進受限的機器。每次產完會報一句目前的
   大小（見 :func:`bundle_size_report`）—— 那是**資訊**，不是門檻。

**先清單再打包**：包裡面含著那份清單，順序反了就會把舊清單封進新包裡。

為什麼要有這支而不是叫人記得跑兩行
----------------------------------
兩行本身不難，難的是**忘了跑不會有任何症狀** —— 直到公司機上少一個檔案，
或者 `check_files.py` 說「你的檔案跟清單一致」而那份清單是三天前的。
所以 `tests/test_offline_tools.py` 有一支測試會在這些東西過期時變紅，
而它的錯誤訊息就是上面那一行指令。

``--check`` 只檢查不寫檔（測試用；有東西過期就回非零）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import make_filelist                      # noqa: E402  （tools/ 裡的同伴）
import make_text_bundle                   # noqa: E402

BUNDLE = os.path.join("bundle", "GLAS_bundle.py")

#: GitHub 的**檔案瀏覽頁**在 1 MB 以上不顯示內容（那顆「複製」鈕跟著消失）。
#:
#: ⚠ **這不是限制**（2026-08-17 使用者確認）：搬運時**直接複製 raw**
#: （`raw.githubusercontent.com/.../bundle/GLAS_bundle.py`），那條路跟檔案
#: 多大無關。這個數字留在這裡只是為了**講得出現在多大**，不是要閃避的門檻 ——
#: 不要為了它去刪文件、也不要為了它分批（`make_text_bundle.py` 預設就是一個檔案）。
BUNDLE_LIMIT_BYTES = 1024 * 1024


def bundle_size_report(nbytes: int) -> Tuple[str, str]:
    """包的大小 → ``(等級, 要印的話)``；等級恆為 ``ok``（這裡不擋任何事）。

    切開成純函式是為了測得到 —— 產一個 1 MB 的包只為了驗這段訊息太貴。

    以前這裡分 ok / warn / over 三級，85% 就開始喊。那是「1 MB 是硬牆」年代
    的產物；使用者改用 raw 複製之後，喊了也沒有人需要做任何事，而**一句沒有
    對應動作的警告只會訓練人忽略警告**。現在它只報事實。
    """
    pct = 100.0 * nbytes / BUNDLE_LIMIT_BYTES
    size = ("%.0f KB（GitHub 檔案頁的顯示上限是 1 MB，佔 %.0f%%；"
            "搬運走 raw，不受這個數字影響）" % (nbytes / 1024.0, pct))
    return "ok", "  %s" % size


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def has_git(root: str) -> bool:
    """這台機器跑得動 git 嗎。

    公司機不能執行 git 操作，而這支的每一件事都建立在 ``git ls-files`` 上 ——
    在那台機器上跑會丟一個看不懂的 subprocess 例外。與其那樣，不如直接講出
    「你跑錯機器了」以及那台機器該做什麼。
    """
    try:
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root,
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def untracked(root: str) -> List[str]:
    """還沒 ``git add`` 的檔案。

    這是這一整套裡最容易踩的坑，而且**踩到不會有任何症狀**：清單與包都是從
    ``git ls-files`` 產的，所以還沒 add 的新檔案會安靜地不在裡面 ——
    公司機上就少一個檔案，而它是唯一拿得到程式碼的地方。
    """
    out = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root, check=True, stdout=subprocess.PIPE).stdout.decode("utf-8")
    return [p for p in out.split("\n") if p.strip()]


def _bundle_entries(text: str) -> List[Tuple[str, str]]:
    """從一份 bundle 裡取出 ``(sha, 路徑)`` —— 只解壓，不寫任何檔案。

    解壓是 0.04 秒，重新壓縮是 1.3 秒；檢查用解壓就夠，所以 ``--check`` 很便宜。
    """
    import base64
    import lzma

    lines = text.split("\n")
    try:
        start = lines.index(make_text_bundle.SENTINEL) + 1
    except ValueError:
        return []
    data = lines[start:]
    enc = data[0].strip() if data else ""
    data = data[1:]
    if enc == "#ENC lzma+base64":
        blob = "".join(ln[2:] for ln in data if ln.startswith("#B"))
        data = lzma.decompress(base64.b64decode(blob)).decode("utf-8").split("\n")
    out = []
    i = 0
    while i < len(data):
        # ``#X`` = base64 記錄（裝不進行導向格式的檔案，GLAS 有一份 PDF）。
        # 漏掉它的話這支會永遠說「包裡少了那個檔案」，而重打一次也不會好。
        if data[i][:3] in ("#F ", "#X "):
            _, sha, count, path = data[i].split(" ", 3)
            out.append((sha, path))
            i += 1 + int(count)
        else:
            i += 1
    return out


def stale(root: str = "") -> List[str]:
    """哪些搬運檔過期了（空 list = 都是最新的）。"""
    root = root or repo_root()
    problems = []

    want_listing = make_filelist.build_lines(root)
    listing_path = os.path.join(root, make_filelist.MANIFEST.replace("/", os.sep))
    got = []
    if os.path.isfile(listing_path):
        with open(listing_path, "r", encoding="utf-8") as f:
            got = f.read().splitlines()
    if got != want_listing:
        problems.append("tools/FILELIST.txt 跟目前的檔案對不上")

    bundle_path = os.path.join(root, BUNDLE.replace("/", os.sep))
    if not os.path.isfile(bundle_path):
        problems.append("%s 不存在" % BUNDLE)
        return problems
    with open(bundle_path, "r", encoding="utf-8") as f:
        inside = _bundle_entries(f.read())
    want = [(sha, rel) for sha, rel in
            (ln.split(" ", 1) for ln in want_listing if not ln.startswith("#"))]
    # 包裡**也含著那份清單**（`check_files.py` 靠它），所以要一起比
    listing_sha = make_filelist.blob_sha(
        ("\n".join(want_listing) + "\n").encode("utf-8"))
    want.append((listing_sha, make_filelist.MANIFEST))
    if sorted(inside) != sorted(want):
        problems.append("%s 裡的內容跟目前的檔案對不上" % BUNDLE)
    return problems


def write(root: str = "") -> None:
    root = root or repo_root()
    # 1. 先清單（包裡面含著它）
    lines = make_filelist.build_lines(root)
    listing = os.path.join(root, make_filelist.MANIFEST.replace("/", os.sep))
    tmp = listing + ".tmp"                            # atomic（鐵則 5）
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, listing)
    print("tools/FILELIST.txt：%d 個檔案" % (len(lines) - len(make_filelist.HEADER)))

    # 2. 再打包
    bundle = os.path.join(root, BUNDLE.replace("/", os.sep))
    os.makedirs(os.path.dirname(bundle), exist_ok=True)
    text = make_text_bundle.build(os.path.basename(bundle), root, compress=True)
    tmp = bundle + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, bundle)
    n = len(make_text_bundle.collect(root))
    nbytes = len(text.encode("utf-8"))
    print("%s：%d 個檔案、%.0f KB" % (BUNDLE, n, nbytes / 1024))
    print(bundle_size_report(nbytes)[1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Regenerate the files the company machine can reach.")
    ap.add_argument("--check", action="store_true",
                    help="只檢查有沒有過期，不寫檔（過期回非零）")
    a = ap.parse_args(argv)
    root = repo_root()

    if not has_git(root):
        print("✗ 這台機器上沒有 git（或這不是一個 git work tree）。")
        print("")
        print("  這支是**給家用機用的** —— 它重產「公司機拿得到程式碼」所需的兩個檔案，")
        print("  而那件事需要 git。公司機不需要跑它。")
        print("")
        print("  你手上如果是解開的搬運包（沒有 .git），那就對了 —— 直接跑：")
        print("    pip install -r requirements.txt")
        print("    python main.py")
        return 2

    loose = untracked(root)
    if loose:
        print("⚠ 有 %d 個檔案還沒 git add —— 它們**不會**進清單也不會進包：" % len(loose))
        for p in loose[:8]:
            print("    %s" % p)
        if len(loose) > 8:
            print("    …還有 %d 個" % (len(loose) - 8))
        print("  先 `git add -A` 再跑這支。")
        return 2

    if a.check:
        problems = stale(root)
        if problems:
            print("✗ 搬運檔過期了：")
            for p in problems:
                print("    %s" % p)
            print("")
            print("  跑：git add -A && python tools/release.py && git add -A")
            return 1
        print("✓ tools/FILELIST.txt 與 %s 都是最新的。" % BUNDLE)
        # 大小是**跟過期無關**的另一條線：包可以既是最新的、又大到送不進去。
        bundle_path = os.path.join(root, BUNDLE.replace("/", os.sep))
        _level, msg = bundle_size_report(os.path.getsize(bundle_path))
        print(msg)
        return 0

    write(root)
    print("")
    print("記得 `git add -A` 再 commit —— 這兩個檔案是公司機唯一拿得到程式碼的地方。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
