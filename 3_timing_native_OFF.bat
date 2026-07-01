@echo off
REM F26: measure decode timing WITHOUT native (pure Python). Disables the .pyd
REM by renaming it to .off, runs, then re-enables it on exit. Clears the same
REM throwaway cache dir so it's a fair COLD-decode A/B vs the ON run.
REM IMPORTANT: run the SAME batch of images as the ON run.
cd /d "%~dp0"

REM disable native: rename .pyd -> .off (import fails -> pure-Python fallback).
if exist glas\core\oasis_fastdecode.cp39-win_amd64.pyd (
  ren glas\core\oasis_fastdecode.cp39-win_amd64.pyd oasis_fastdecode.cp39-win_amd64.pyd.off
)

set GLAS_FA_TIMING=1
set GLAS_CELLCACHE_DIR=%TEMP%\glas_cache_test
if exist "%GLAS_CELLCACHE_DIR%" rmdir /s /q "%GLAS_CELLCACHE_DIR%"

echo ============================================================
echo   [3] NATIVE = OFF  (pure Python; cache cleared -> cold decode)
echo ------------------------------------------------------------
python -c "import sys; sys.path.insert(0,'glas/core'); import oasis_random as o; print('   native _FAST =', o._FAST, ' (should be None)')"
echo   Run the SAME batch as [2] -> Export...  again note the  poi  number.
echo ============================================================
python main.py

REM re-enable native no matter what.
if exist glas\core\oasis_fastdecode.cp39-win_amd64.pyd.off (
  ren glas\core\oasis_fastdecode.cp39-win_amd64.pyd.off oasis_fastdecode.cp39-win_amd64.pyd
)
echo.
echo   native re-enabled (.pyd restored).  Compare:  poi(OFF) / poi(ON) = speedup
pause
