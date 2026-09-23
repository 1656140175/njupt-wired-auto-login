@echo off
rem ============================================================
rem  NJUPT auto login - watch mode (silent background)
rem  Account config is read from njupt_autologin.conf in this
rem  folder - no credentials are hardcoded in this script.
rem  Default: 3 login attempts, 6s apart, silent (no window).
rem  Put a shortcut of this file into shell:startup to run
rem  automatically at Windows login.
rem ============================================================
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw njupt_autologin.py --watch --attempts 3 --attempt-delay 6
) else (
  start "" pyw -3 njupt_autologin.py --watch --attempts 3 --attempt-delay 6
)
