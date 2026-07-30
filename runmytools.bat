@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "PY=%~dp0runtime\python\python.exe"
set "LOG=%~dp0launch.log"

rem 第一件事：立刻写一条日志。若双击后连 launch.log 都不更新，说明 bat 根本没被执行。
echo [%date% %time%] === runmytools.bat 开始执行 === > "%LOG%"
echo [%date% %time%] 当前目录: %~dp0 >> "%LOG%"
echo [%date% %time%] python 路径: %PY% >> "%LOG%"

if not exist "%PY%" (
  echo [%date% %time%] [错误] 找不到 runtime\python\python.exe >> "%LOG%"
  echo.
  echo [错误] 找不到 runtime\python\python.exe
  echo 请确认你拷贝的是“投资小工具”完整文件夹（必须包含 runtime 子目录）。
  echo 当前目录：%~dp0
  echo.
  pause
  exit /b 1
)

echo 正在检查运行环境（首次稍慢，属正常）...
echo [%date% %time%] 运行 check_env.py ... >> "%LOG%"
"%PY%" check_env.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] check_env.py 返回错误（退出码 %errorlevel%） >> "%LOG%"
  echo.
  echo 运行环境存在问题，详情已写入 launch.log。
  echo 常见原因：文件夹没有完整拷贝（缺少 runtime 子目录或依赖）。
  echo.
  echo ===== launch.log 末尾 20 行 =====
  powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 20"
  echo.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   投资小工具正在启动……
echo   - 浏览器会在几秒后自动打开（若未自动打开，手动访问下方网址）
echo   - 本黑色窗口是服务运行日志，请勿关闭；关闭窗口即停止服务
echo   - 本机访问：  http://127.0.0.1:8098
echo   - 若双击仍是“一闪而过没有黑框”，请用 VS Code 打开本文件夹，
echo     在集成终端运行：  runtime\python\python.exe app.py
echo ============================================================
echo.

echo [%date% %time%] 启动 app.py（前台运行）... >> "%LOG%"
"%PY%" app.py >> "%LOG%" 2>&1

echo [%date% %time%] app.py 已退出（退出码 %errorlevel%） >> "%LOG%"
echo.
echo [已停止] 服务已退出。若不是你主动关闭，请重新启动。
echo 日志已保存到 launch.log，可发给我看。
echo.
pause
