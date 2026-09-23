@echo off
rem ============================================================
rem  NJUPT auto login - auto start manager (Task Scheduler)
rem  Install: runs watch script at Windows login, silent.
rem  Remove : unregisters it.
rem ============================================================
chcp 936 >nul
cd /d "%~dp0"
echo ============================================================
echo  NJUPT Auto Login - auto start manager
echo ============================================================
echo  1) Install (run at Windows login, silent)
echo  2) Remove
choice /c 12 /n /m "Choose (1/2): "
if errorlevel 2 goto remove
echo  Installing task "NJUPT AutoLogin"...
schtasks /Create /TN "NJUPT AutoLogin" ^
  /TR "\"%~dp0njupt_autologin_watch.bat\"" ^
  /SC ONLOGON /RL LIMITED /F
if %errorlevel%==0 (echo  OK - will run at login.) else (echo  FAILED - try running as administrator)
goto end
:remove
echo  Removing task "NJUPT AutoLogin"...
schtasks /Delete /TN "NJUPT AutoLogin" /F >nul 2>nul
echo  Done.
:end
echo.
pause
