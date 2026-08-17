"""Tests for the offline transfer tooling (``tools/make_filelist.py`` +
``tools/make_text_bundle.py``, ported from ADEPT).

The bundle exists because the company machine can't take a ``.zip`` and the
proxy won't let Python fetch files one by one — a single plain-text ``.py`` is
the only thing that gets through. Two ways that silently fails, both pinned
here:

* **``tools/FILELIST.txt`` rots.** Someone adds a file and doesn't regenerate
  the list. A split bundle then reports "complete" while that file is missing
  on the target machine, because the completeness check reads this list.
* **The round trip isn't byte-exact.** The format counts *lines*, not bytes, so
  it survives CRLF rewriting in transit — but only if every packed file really
  is LF + UTF-8, and only if binaries take the base64 path instead.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tools"))

import make_filelist       # noqa: E402
import make_text_bundle    # noqa: E402


def _git_available() -> bool:
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(_ROOT),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(
    not _git_available(), reason="needs a git checkout (both tools use git ls-files)")


class TestFileList:

    def test_matches_the_repo(self):
        """Regenerating the list must produce what is committed. If this fails,
        run ``python tools/make_filelist.py`` — a stale list makes a split
        bundle claim it is complete when it isn't."""
        path = _ROOT / make_filelist.MANIFEST
        assert path.is_file(), "tools/FILELIST.txt is missing — run make_filelist.py"
        want = "\n".join(make_filelist.build_lines(str(_ROOT))) + "\n"
        assert path.read_text(encoding="utf-8") == want

    def test_excludes_the_bundle_output(self):
        """The bundle is a *copy* of the repo, not part of it. Listing it would
        make every rebuild pack the previous build — growth on growth."""
        assert not any(rel.startswith("bundle/")
                       for rel in make_filelist.tracked_files(str(_ROOT)))

    def test_shas_are_git_blob_shas(self):
        readme = (_ROOT / "README.md").read_bytes()
        out = subprocess.run(["git", "hash-object", "README.md"], cwd=str(_ROOT),
                             check=True, stdout=subprocess.PIPE)
        assert make_filelist.blob_sha(readme) == out.stdout.decode().strip()


class TestBundleRoundTrip:

    #: A handful of real repo files, chosen to cover what the format has to
    #: survive: CJK text, a long docstring-heavy module, and the one binary.
    SAMPLE = ("README.md", "CLAUDE.md", "glas/core/tiff_index.py",
              "GLAS_快速參考卡.pdf")

    def _items(self):
        items = []
        for rel in self.SAMPLE:
            p = _ROOT / rel
            if p.is_file():
                items.append((rel, p.read_bytes()))
        assert items, "no sample files found"
        return items

    def _extract(self, tmp_path, text, name="B.py"):
        script = tmp_path / name
        script.write_text(text, encoding="utf-8", newline="\n")
        dest = tmp_path / "out"
        rc = subprocess.run([sys.executable, str(script), "--dest", str(dest)],
                            capture_output=True, text=True)
        assert rc.returncode == 0, rc.stdout + rc.stderr
        return dest

    @pytest.mark.parametrize("compress", [False, True])
    def test_files_survive_byte_for_byte(self, tmp_path, compress):
        items = self._items()
        text = make_text_bundle.build("B.py", items=items, compress=compress)
        dest = self._extract(tmp_path, text)
        for rel, data in items:
            assert (dest / rel).read_bytes() == data, rel

    def test_binary_goes_through_base64_and_text_does_not(self):
        """base64 is the exception, not the rule: DLP tends to block what it
        can't read, so only files that cannot be stored as lines use it."""
        items = self._items()
        lines = make_text_bundle._data_lines(items)
        text_recs = [l for l in lines if l.startswith("#F ")]
        b64_recs = [l for l in lines if l.startswith("#X ")]
        assert any(r.endswith("README.md") for r in text_recs)
        assert all(r.endswith(".pdf") for r in b64_recs)

    def test_crlf_in_transit_does_not_break_it(self, tmp_path):
        """The whole reason the format counts lines: a bundle mailed around or
        re-saved by an editor comes back with CRLF endings."""
        items = self._items()
        text = make_text_bundle.build("B.py", items=items)
        script = tmp_path / "B.py"
        script.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        dest = tmp_path / "out"
        rc = subprocess.run([sys.executable, str(script), "--dest", str(dest)],
                            capture_output=True, text=True)
        assert rc.returncode == 0, rc.stdout + rc.stderr
        for rel, data in items:
            assert (dest / rel).read_bytes() == data, rel

    def test_tampered_content_is_reported_not_written(self, tmp_path):
        """A file altered in transit must be named, not silently delivered."""
        items = [("a.txt", b"hello\nworld\n")]
        text = make_text_bundle.build("B.py", items=items)
        text = text.replace("#hello", "#hell0")
        script = tmp_path / "B.py"
        script.write_text(text, encoding="utf-8", newline="\n")
        dest = tmp_path / "out"
        rc = subprocess.run([sys.executable, str(script), "--dest", str(dest)],
                            capture_output=True, text=True)
        assert rc.returncode == 1
        assert "a.txt" in rc.stdout
        assert not (dest / "a.txt").exists()

    def test_list_writes_nothing(self, tmp_path):
        items = self._items()
        text = make_text_bundle.build("B.py", items=items)
        script = tmp_path / "B.py"
        script.write_text(text, encoding="utf-8", newline="\n")
        dest = tmp_path / "out"
        rc = subprocess.run([sys.executable, str(script), "--list",
                             "--dest", str(dest)], capture_output=True, text=True)
        assert rc.returncode == 0
        assert not dest.exists()
        assert "README.md" in rc.stdout

    def test_the_whole_repo_is_packable(self):
        """Every tracked file is either LF + UTF-8 or routed to base64 — so a
        real build can never quietly drop one."""
        items = make_text_bundle.collect(str(_ROOT))
        tracked = set(make_filelist.tracked_files(str(_ROOT)))
        tracked.add(make_filelist.MANIFEST)
        assert {rel for rel, _d in items} == tracked


class TestShippedBundle:
    """The committed bundle under ``bundle/`` is what the operator actually
    copies; it has to be in step with the repo."""

    def test_parts_exist_and_are_under_the_github_display_limit(self):
        parts = sorted((_ROOT / "bundle").glob("GLAS_bundle*.py"))
        if not parts:
            pytest.skip("no bundle built in this checkout")
        for p in parts:
            size_kb = p.stat().st_size / 1024
            # GitHub won't render a file over 1 MB, and the clipboard is the
            # only way in — an unrenderable part cannot be copied at all.
            assert size_kb <= make_text_bundle.LIMIT_KB, f"{p.name}: {size_kb:.0f} KB"
