@echo off
title ArbTest Dashboard Launcher

:: [AI-2026-08-20] Pre-flight: confirm the trading clients are already running.
:: Enter = continue, Esc = abort this batch file.
powershell -NoProfile -Command "$Host.UI.RawUI.FlushInputBuffer(); Write-Host ''; Write-Host '[PRE-FLIGHT] Make sure these clients are ALREADY RUNNING:' -ForegroundColor Yellow; Write-Host '   - IB Gateway      (US quotes)' -ForegroundColor White; Write-Host '   - Futu OpenD      (overseas ETF depth)' -ForegroundColor White; Write-Host '   - TDX / Tongdaxin (A-share order book)' -ForegroundColor White; Write-Host ''; Write-Host '[Enter] = clients are up, continue   [Esc] = abort and go start them' -ForegroundColor Cyan; while($true){$k=$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown'); if($k.VirtualKeyCode -eq 13){break} if($k.VirtualKeyCode -eq 27){exit 1}}"
if errorlevel 1 (
  echo [CANCELLED] You chose to abort. Start the clients above, then re-run this file.
  exit /b
)

echo ========================================
echo  Starting ArbTest Unified Dashboard...
echo ========================================

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
:: [AI-2026-08-20] This launcher lives in src\; the real project root is the ArbDashboard subdir.
set "PROJ_DIR=%SCRIPT_DIR%\ArbDashboard"
set "VENV=%PROJ_DIR%\.venv\Scripts"
set "PY=%VENV%\python.exe"
set "BACKEND=%PROJ_DIR%\backend"
set "FRONTEND=%PROJ_DIR%\frontend"

:: [AI-2026-08-17] Blank PYTHONPATH: an outer terminal (e.g. Guojin QMT) may inject GJQMT
:: paths. Its older defusedxml then wins over the venv copy because PYTHONPATH sits earlier
:: in sys.path, and importing V7 blows up with an XMLParser error.
set "PYTHONPATH="

:: [AI-2026-10-06] After the VPN is torn down, HTTP(S)_PROXY may still point at a dead local
:: proxy (e.g. http://127.0.0.1:5986). Backend requests inherit it (proxies=None) and every
:: outbound call (Tencent / Sina / Eastmoney) dies with ProxyError. Clear them so the backend
:: always connects directly: Sina/Eastmoney work direct, and Sina is the fallback when Tencent
:: history K-line hits its WAF.
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "ALL_PROXY="
set "all_proxy="

:: [AI-2026-10-06] Single source of truth = _shared/arb_master.db (same DB as ETFarb). Set it
:: explicitly so _resolve_db_path cannot land on an empty LOFarb/database/ copy -- that was the
:: root cause of the 2026-10-06 "all categories lost" incident.
:: %SCRIPT_DIR% = .../LOFarb/src, so two levels up is the ARB root.
set "ARB_MASTER_DB=%SCRIPT_DIR%\..\..\_shared\arb_master.db"

:: [AI-2026-10-06] Force the backend to use SYSTEM OpenSSH, not the Git-bundled ssh.
:: Root cause: Git's ssh.exe (C:\Users\milan\.workbuddy\binaries\PortableGit\...\ssh.exe) comes
:: BEFORE C:\Windows\System32\OpenSSH in PATH, and that Git ssh does not read
:: C:\Users\milan\.ssh\config on Windows. So `ssh arm` fails with "Could not resolve hostname
:: arm" (it treats "arm" as a real host). System OpenSSH reads the arm/opend aliases correctly.
:: Putting the system dir first is enough; ARM (Linux) is unaffected.
set "PATH=C:\Windows\System32\OpenSSH;%PATH%"

:: [AI-2026-11-10] Robust kill: by port 8000/5173, then by command-line match
:: *ArbDashboard*main.py* to catch zombies that are not bound to a port yet.
powershell -NoProfile -Command "foreach($p in @(8000,5173)){ Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -and $_.CommandLine -like '*ArbDashboard*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
:: Wait until port 8000 is really free (max 10s) before starting the backend, otherwise a
:: lingering old process makes the new one fail to bind.
powershell -NoProfile -Command "for($i=0;$i -lt 10;$i++){ if(-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)){ break }; Start-Sleep -Seconds 1 }"

:: [AI-2026-11-10 22:31] Local health check BEFORE the backend starts.
::   Compares local _shared\arb_master.db against ARM (ARM is opened read-only) and pulls the
::   gaps from ARM: INSERT missing keys + fill blank columns, auto backup, never DELETE, never
::   overwrite non-NULL values.
::   Why here: this machine can be off for weeks (travel / busy) and the local mirror falls
::   behind ARM. This is the ONE place that catches it up -- deliberately no separate .bat.
::   Never blocks startup: any failure only prints a warning and the dashboard still starts.
::   Exit code 0 = local matches ARM, non-zero = gaps remain / ARM unreachable.
echo [1/3] Local health check (local DB vs ARM)...
set "HEALTH_PY=%SCRIPT_DIR%\..\deploy\local_health_check.py"
if not exist "%HEALTH_PY%" (
  echo   [WARN] not found: %HEALTH_PY%  [skipped]
) else (
  "%PY%" "%HEALTH_PY%" --apply
  if errorlevel 1 (
    echo   [WARN] gaps remain, or ARM unreachable - see the table above.
    echo   [WARN] retry later: .venv\Scripts\python.exe ..\deploy\local_health_check.py --apply --fallback
  ) else (
    echo   [OK] local DB matches ARM.
  )
)
echo.

:: Start Backend. STRICTLY the project venv -- never fall back to a system / WorkBuddy python,
:: because missing ibapi / apscheduler / freeze schedulers fail silently.
echo [2/3] Starting Backend (port 8000)...
if exist "%PY%" (
  start "ArbTest Backend" cmd /k "cd /d %BACKEND% && %PY% main.py"
) else (
  echo [FATAL] Project virtualenv interpreter not found: %PY%
  echo [FATAL] Do NOT fall back to a system python: missing ibapi/apscheduler silently breaks
  echo [FATAL] IB quotes and the freeze scheduler.
  echo [FATAL] Make sure ArbDashboard\.venv exists, or run .venv\Scripts\python.exe main.py directly.
  pause
  exit /b 1
)

:: Wait for the backend to come up. Fixed wait -- the health check already ran as step [1/3].
echo Waiting 8 seconds for backend to be ready...
timeout /t 8 /nobreak > nul

:: Start Frontend
echo [3/3] Starting Frontend (port 5173)...
start "ArbTest Frontend" cmd /k "cd /d %FRONTEND% && npm run dev"

echo.
echo ========================================
echo  Backend : http://127.0.0.1:8000
echo  Frontend: http://localhost:5173
echo ========================================
timeout /t 3 /nobreak > nul
start http://localhost:5173
echo Done.
