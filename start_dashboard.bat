@echo off
title ArbTest Dashboard Launcher

:: [AI-2026-08-20] 启动前确认客户端已开：Enter 继续 / Esc 退出批处理
powershell -NoProfile -Command "$Host.UI.RawUI.FlushInputBuffer(); Write-Host ''; Write-Host '【启动前检查】请先确保以下客户端已启动：' -ForegroundColor Yellow; Write-Host '   - IB Gateway  (美股行情)' -ForegroundColor White; Write-Host '   - 富途 OpenD  (外盘 ETF 10档)' -ForegroundColor White; Write-Host '   - 通达信      (A股盘口)' -ForegroundColor White; Write-Host ''; Write-Host '按 [Enter] 确认已启动并继续, 按 [Esc] 退出去启动客户端' -ForegroundColor Cyan; while($true){$k=$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown'); if($k.VirtualKeyCode -eq 13){break} if($k.VirtualKeyCode -eq 27){exit 1}}"
if errorlevel 1 (
  echo [已取消] 你选择退出。请启动上述客户端后重新运行本批处理。
  exit /b
)

echo ========================================
echo  Starting ArbTest Unified Dashboard...
echo ========================================

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
:: [AI-2026-08-20] 本脚本已上移至 src\ 层，实际项目根目录在 ArbDashboard 子目录，故统一定位到 PROJ_DIR
set "PROJ_DIR=%SCRIPT_DIR%\ArbDashboard"
set "VENV=%PROJ_DIR%\.venv\Scripts"
set "PY=%VENV%\python.exe"
set "BACKEND=%PROJ_DIR%\backend"
set "FRONTEND=%PROJ_DIR%\frontend"

:: [AI-2026-08-17] 清空 PYTHONPATH：防止继承外层终端（如国金 QMT）注入的 GJQMT 路径，
:: 否则其老版本 defusedxml 会因 PYTHONPATH 在 sys.path 中先于 venv 而覆盖 venv 包，导致导入 V7 报 XMLParser 错误。
set "PYTHONPATH="

:: [AI-2026-10-06] 关掉 VPN 后，系统可能残留 HTTPS_PROXY/HTTP_PROXY 指向已死的 VPN 本地代理
:: （如 http://127.0.0.1:5986）。后端 requests 用 proxies=None 会继承该代理 → 所有对外请求
:: （腾讯/新浪/东财）连不上死代理而 ProxyError 失败。强制清掉，让后端始终直连：
:: 腾讯历史K线被WAF时由新浪兜底，新浪/东财直连可用。
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "ALL_PROXY="
set "all_proxy="

:: [AI-2026-10-06] 单一真库 = _shared/arb_master.db（与 ETFarb 一致）。显式指定，杜绝 _resolve_db_path
:: 解析到 LOFarb/database/ 下的空库（2026-10-06 分类全丢事故根因）。%SCRIPT_DIR%=.../LOFarb/src，上两层到 ARB 根。
set "ARB_MASTER_DB=%SCRIPT_DIR%\..\..\_shared\arb_master.db"

:: [AI-2026-10-06] 让后端用【系统 OpenSSH】而非 Git 自带 ssh。
:: 根因：本机 PATH 里 Git 的 ssh.exe(C:\Users\milan\.workbuddy\binaries\PortableGit\...\ssh.exe) 排在
:: C:\Windows\System32\OpenSSH 之前，而该 Git ssh 在 Windows 下不读 C:\Users\milan\.ssh\config，
:: 导致 step13 / 推送 ARM 调 `ssh arm` 报 "Could not resolve hostname arm"（把 arm 当真实主机名）。
:: 系统 OpenSSH 能正确读取 .ssh/config 的 arm/opend 别名。把系统目录置顶即可，ARM(Linux)不受影响。
set "PATH=C:\Windows\System32\OpenSSH;%PATH%"

:: [AI-2026-11-10] Robust kill: by port 8000/5173, then by command-line match *ArbDashboard*main.py* to catch zombies not yet bound to a port.
powershell -NoProfile -Command "foreach($p in @(8000,5173)){ Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -and $_.CommandLine -like '*ArbDashboard*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
:: 等待 8000 端口真正释放（最多 10 秒）再启动后端，杜绝旧进程赖着导致新进程绑端口失败
powershell -NoProfile -Command "for($i=0;$i -lt 10;$i++){ if(-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)){ break }; Start-Sleep -Seconds 1 }"

:: Start Backend (STRICTLY project venv — 禁止回退到系统/WorkBuddy python，否则 IB/富途/冻结调度器缺失依赖静默失效)
echo [1/2] Starting Backend (port 8000)...
if exist "%PY%" (
  start "ArbTest Backend" cmd /k "cd /d %BACKEND% && %PY% main.py"
) else (
  echo [FATAL] 找不到项目虚拟环境解释器: %PY%
  echo [FATAL] 请勿回退到系统 python（缺 ibapi/apscheduler 会导致 IB 行情与冻结调度器失效）
  echo [FATAL] 请确认 ArbDashboard/.venv 存在；或用 .venv\Scripts\python.exe 直接启动 main.py
  pause
  exit /b 1
)

:: Wait for backend to start (fixed wait; this program does NOT do health-check)
echo Waiting 8 seconds for backend to be ready...
timeout /t 8 /nobreak > nul

:: Start Frontend
echo [2/2] Starting Frontend (port 5173)...
start "ArbTest Frontend" cmd /k "cd /d %FRONTEND% && npm run dev"

echo.
echo ========================================
echo  Backend : http://127.0.0.1:8000
echo  Frontend: http://localhost:5173
echo ========================================
timeout /t 3 /nobreak > nul
start http://localhost:5173
echo Done.
