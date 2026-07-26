@echo off
cd /d "%~dp0"

echo ============================================
echo  构建估值快照（沪深300 + 港股通）
echo  首次运行需要联网，约几分钟，请保持窗口打开。
echo  已算过的会自动跳过（断点续跑），可重复运行补全。
echo ============================================
echo.

set "PYTHON_EXE="
if exist "%~dp0runtime\python\python.exe" set "PYTHON_EXE=%~dp0runtime\python\python.exe"
if not defined PYTHON_EXE set "PYTHON_EXE=C:\Users\hz\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

"%PYTHON_EXE%" build_valuation_cache.py

echo.
echo 构建完成（或已中断）。按任意键关闭本窗口...
pause >nul
