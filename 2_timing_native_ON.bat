@echo off
REM F26: measure decode timing WITH native (v4). Uses a throwaway cell cache
REM dir and clears it first, so poi reflects a COLD decode (fair A/B vs OFF).
cd /d "%~dp0"

REM self-heal: if a previous OFF run left the .pyd disabled, re-enable it.
if exist glas\core\oasis_fastdecode.cp39-win_amd64.pyd.off (
  ren glas\core\oasis_fastdecode.cp39-win_amd64.pyd.off oasis_fastdecode.cp39-win_amd64.pyd
)

set GLAS_FA_TIMING=1
set GLAS_CELLCACHE_DIR=%TEMP%\glas_cache_test
if exist "%GLAS_CELLCACHE_DIR%" rmdir /s /q "%GLAS_CELLCACHE_DIR%"

echo ============================================================
echo   [2] NATIVE = ON   (cell cache cleared -> cold decode)
echo ------------------------------------------------------------
python -c "import sys; sys.path.insert(0,'glas/core'); import oasis_random as o; print('   native _FAST =', o._FAST)"
echo   In the GUI: load your KLARF, pick a batch (try 100-200 first),
echo   press  Export...   Watch this window for lines like:
echo     [fa-timing] pid=.. n=25  read=..  poi(walk+bool)=XX.X  ...
echo   Write down the  poi  number.
echo ============================================================
python main.py
pause
