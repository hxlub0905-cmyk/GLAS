"""Build the optional native decode extension (F26).

GLAS itself runs with no build step (pure Python + numpy/cv2/shapely/PyQt6 on a
flat sys.path — see CLAUDE.md §4/§6). This setup.py exists ONLY to compile the
optional ``oasis_fastdecode`` accelerator; the app falls back to pure Python
when it is absent, so building is never required to run GLAS.

Local build (needs a C compiler — MSVC on Windows, gcc/clang elsewhere):

    pip install cython setuptools
    python setup.py build_ext --inplace

That drops ``oasis_fastdecode.<tag>.pyd`` (Windows) / ``.so`` (Linux/macOS)
next to the source in ``glas/core/`` (which is on sys.path), so
``import oasis_fastdecode`` finds it. On a machine without a compiler, download
the artifact built by ``.github/workflows/build-fastdecode.yml`` (GitHub
Actions, MSVC preinstalled) and drop the matching ``.pyd`` into ``glas/core/``.
"""
from __future__ import annotations

from setuptools import setup, Extension

try:
    from Cython.Build import cythonize
except ImportError:  # pragma: no cover - Cython absent → nothing to build
    cythonize = None

_SRC = "glas/core/oasis_fastdecode.pyx"
_ext = [Extension("oasis_fastdecode", [_SRC])]

setup(
    name="oasis_fastdecode",
    version="0.1",
    description="Optional native OASIS decode helpers for GLAS (F26)",
    ext_modules=cythonize(_ext, language_level=3) if cythonize else [],
    zip_safe=False,
)
