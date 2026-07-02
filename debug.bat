@echo off
REM ============================================================================
REM  GLAS — the ONE debug launcher.
REM
REM  Double-click this (or run it) instead of launching GLAS normally when a
REM  load / export is slow or looks stuck. It starts GLAS with full perf
REM  telemetry on the console so we can see WHERE the time goes in a single run
REM  (no back-and-forth). Everything else works exactly as usual.
REM
REM  What you'll see in this window (leave it open):
REM    [roi] reader built in ..s . N cells indexed . S_BOUNDING_BOX ...
REM        -> the file opened and was indexed (decode-free prune if S_BOUNDING_BOX).
REM    [roi]   . decoding cell <id>: N records, Ts elapsed
REM        -> a big cell is being decoded RIGHT NOW (first load only). If the
REM           record count keeps rising it is WORKING, not hung; the first load
REM           of a large cell can take minutes, later ROIs reuse the cache.
REM    [roi] -- loaded in ..s . rects/polys . decode ..s (X from cache, Y decoded)
REM        -> the ROI walk finished. "decoded" high on a tiny FOV = the slow
REM           first-load case (unavoidable one-time cost for that file).
REM    [export-timing] / [fa-timing] ...
REM        -> per-image export stage times (read / walk / boolean / match).
REM
REM  If you hit a perf problem, copy the [roi] and [export-timing] lines from
REM  this window into the chat -- that is enough to diagnose most issues.
REM ============================================================================
cd /d "%~dp0"

REM One switch turns on ROI summaries + decode heartbeat + export timing.
set GLAS_DEBUG=1

echo ============================================================
echo   GLAS debug mode
echo ------------------------------------------------------------
REM Confirm the native decode extension is present + healthy (optional but handy).
python -c "import sys; sys.path.insert(0,'glas/core'); import oasis_fastdecode as f; print('   native oasis_fastdecode VERSION =', f.VERSION, ' selftest =', f.selftest())" 2>nul || echo   (native oasis_fastdecode not built -> pure-Python decode; run tools\unpack_fastdecode.py)
echo ------------------------------------------------------------
echo   Launching GLAS with full telemetry. Keep this window open.
echo ============================================================
python main.py
pause
