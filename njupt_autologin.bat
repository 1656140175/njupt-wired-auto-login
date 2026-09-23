@echo off
rem ============================================================
rem  NJUPT auto login (Windows local, no Raspberry Pi needed)
rem  Account config is read from njupt_autologin.conf in this
rem  folder - no credentials are hardcoded in this script.
rem  Double-click: check once and login.
rem ============================================================
chcp 936 >nul
cd /d "%~dp0"
echo ============================================================
echo  NJUPT auto login (local)
echo  Config: %~dp0njupt_autologin.conf
echo ============================================================
where python >nul 2>nul
if %errorlevel%==0 (
  python njupt_autologin.py --once
) else (
  py -3 njupt_autologin.py --once
)
echo.
pause
