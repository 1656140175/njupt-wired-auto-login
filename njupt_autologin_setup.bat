@echo off
rem ============================================================
rem  NJUPT auto login - environment check & auto setup
rem  Checks: Python, PATH, pythonw, ping, curl, config file.
rem  Auto-fix: if Python is installed but not in PATH, adds it
rem  to the user PATH automatically (no admin needed).
rem  If Python missing, opens the official download page.
rem ============================================================
chcp 936 >nul
cd /d "%~dp0"
echo ============================================================
echo  NJUPT Auto Login - environment check
echo ============================================================
echo.

rem ---- 1. Python in PATH? ----
set "PYFOUND="
where python >nul 2>nul && set "PYFOUND=python"
if not defined PYFOUND ( where py >nul 2>nul && set "PYFOUND=py" )

if defined PYFOUND (
  for /f "delims=" %%v in ('python --version 2^>^&1') do set "PV=%%v"
  if not defined PV ( for /f "delims=" %%v in ('py -3 --version 2^>^&1') do set "PV=%%v" )
  echo  [OK] Python found: %PV%
  goto py_done
)

echo  [!!] Python NOT found in PATH
rem ---- 2. auto-detect common install locations ----
set "AUTO_PY="
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python*" "C:\Python3*" "C:\Program Files\Python*") do (
  if exist "%%d\python.exe" set "AUTO_PY=%%d"
)
if defined AUTO_PY (
  echo  [OK] Found Python installed at:
  echo       %AUTO_PY%
  echo  [..] Adding it to user PATH (auto setup)...
  set "OLDPATH="
  for /f "skip=2 tokens=3*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "OLDPATH=%%a %%b"
  reg add "HKCU\Environment" /v Path /t REG_EXPAND_SZ /d "%OLDPATH%;%AUTO_PY%;%AUTO_PY%\Scripts" /f >nul
  echo  [OK] PATH updated. Close this window, reopen, run again to verify.
  goto py_done
)

echo  [!!] Python is NOT installed on this machine.
echo  Options:
echo   1) Open python.org download page (recommended, official)
echo   2) Skip - I will install it myself
choice /c 12 /n /m "Choose (1/2): "
if errorlevel 2 goto py_done
start "" "https://www.python.org/downloads/"
echo  Page opened. Install Python, and make sure to check:
echo    [x] Add python.exe to PATH
:py_done

rem ---- 3. pythonw (silent watch mode) ----
where pythonw >nul 2>nul
if %errorlevel%==0 (
  echo  [OK] pythonw found - watch mode available
) else (
  echo  [..] pythonw not in PATH - watch mode needs it
)

rem ---- 4. system tools (Windows built-in) ----
where ping >nul 2>nul && echo  [OK] ping
where curl >nul 2>nul && echo  [OK] curl

rem ---- 5. config file ----
if exist "%~dp0njupt_autologin.conf" (
  echo  [OK] config file present
) else (
  echo  [!!] njupt_autologin.conf missing - re-extract the package
)

echo.
echo  ============================================================
echo  Next: fill njupt_autologin.conf, then run njupt_autologin.bat
echo  ============================================================
pause
