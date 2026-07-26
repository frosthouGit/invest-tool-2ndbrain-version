@echo off
cd /d "%~dp0"

echo 正在重启投资小工具（先关闭占用 8098 的旧进程，确保加载最新代码）...
:: 杀掉占用 8098 端口的旧 Flask 进程（若有），避免“重启”后仍是旧页面
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr ":8098" ^| findstr "LISTENING"') do (
  taskkill /PID %%P /F >nul 2>nul
  echo   已结束旧进程 PID=%%P
)
timeout /t 1 >nul

echo 正在启动投资小工具，请稍候...
:: 必须用本文件夹内自带的 runtime\python（已内置 akshare 等全部依赖，拷贝到其他电脑无需安装 Python）
set "PYTHON_EXE="
if exist "%~dp0runtime\python\python.exe" (
  set "PYTHON_EXE=%~dp0runtime\python\python.exe"
) else (
  echo [错误] 找不到本文件夹里的 runtime\python\python.exe
  echo 请确认你拷贝的是“投资小工具”完整文件夹（必须包含 runtime 子目录）。
  pause
  exit /b 1
)
start /B "" "%PYTHON_EXE%" app.py

set PORT_READY=0
for /L %%i in (1,1,30) do (
  powershell -NoProfile -Command "(Test-NetConnection -ComputerName 127.0.0.1 -Port 8098 -WarningAction SilentlyContinue).TcpTestSucceeded" | findstr /i "True" >nul
  if not errorlevel 1 (
    set PORT_READY=1
    goto :ready
  )
  timeout /t 1 >nul
)

:ready
if %PORT_READY%==1 (
  echo 启动成功，正在打开浏览器...
  start "" http://127.0.0.1:8098
  echo.
  echo ============================================
  echo  本机访问：  http://127.0.0.1:8098
  echo --------------------------------------------
  for /f "delims=" %%a in ('powershell -NoProfile -Command "((Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object {$_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.*'}).IPAddress ^| Select-Object -First 1)"') do set LAN_IP=%%a
  if not "%LAN_IP%"=="" (
    echo  同网络其他设备访问：  http://%LAN_IP%:8098
    echo  （手机/笔记本连同一个 Wi-Fi 或路由器，浏览器输上面这行即可）
  )
  echo ============================================
  echo  浏览器已打开。本黑色窗口请保持打开，关闭它就会停止服务。
  echo  按任意键可退出并停止服务...
  pause >nul
) else (
  echo.
  echo 启动失败：30 秒内未能连上 8098 端口。
  echo 请看上方 Flask 日志中的红色报错，或重新双击本文件重试。
  pause
)
