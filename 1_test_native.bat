@echo off
REM F26: correctness gate — native decode must match pure Python byte-for-byte.
REM Run this ONCE after unpacking the v4 .pyd, before any timing.
cd /d "%~dp0"
echo ============================================================
echo   [1] byte-identical guard  (native vs pure Python)
echo ============================================================
python -c "import sys; sys.path.insert(0,'glas/core'); import oasis_fastdecode as f; print('   native VERSION =', f.VERSION, ' selftest =', f.selftest())"
echo.
python -m pytest tests\test_fastdecode.py tests\test_oasis_native_decode.py -q
echo.
echo   (if you see "No module named pytest" -> run:  pip install pytest)
echo ============================================================
pause
