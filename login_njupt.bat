@echo off
rem ============================================================
rem  CampusPi NJUPT login init (pure ASCII, no encoding issues)
rem  Reads account/password/suffix from njupt_autologin.conf in
rem  this folder - no credentials are hardcoded in this script.
rem  Step 1: POST to gateway directly (bypasses Pi, quick check)
rem  Step 2: save account to Pi and trigger login
rem ============================================================
chcp 936 >nul
set PI=http://192.168.9.1:8080
set TMPFILE=%TEMP%\campuspi_login_result.html

rem ---- read config file (njupt_autologin.conf) ----
set U=
set P=
set S=
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%~dp0njupt_autologin.conf") do (
  if "%%a"=="user" set U=%%b
  if "%%a"=="pass" set P=%%b
  if "%%a"=="suffix" set S=%%b
)
if not defined U (echo ERROR: no user found in njupt_autologin.conf & pause & exit /b 1)
if "%S%"=="@cmcc" (set ISP=mobile) else if "%S%"=="@njxy" (set ISP=telecom) else (set ISP=campus)
set GATEWAY=http://10.10.244.11:801/eportal/?c=ACSetting&a=Login

echo ============================================================
echo  CampusPi login init
echo  Account : %U%%S%  (ISP: %ISP%)
echo ============================================================

echo.
echo [1/2] Testing gateway directly (POST, same as portal page)...
curl -s -L -X POST "%GATEWAY%" ^
  -d "DDDDD=%U%%S%" ^
  -d "upass=%P%" ^
  -d "0MKKey=123456&R1=0&R2=0&R3=0&R6=0&para=00&url=drappall&buttonClicked=&redirect_url=&err_flag=&username=&password=&user=&cmd=&Login=&v6ip=" ^
  -o "%TMPFILE%"
echo  Gateway response:
findstr /i "LoginID_3 LoginID_2 success fail" "%TMPFILE%" 2>nul

echo.
echo [2/2] Saving account to Pi and triggering login...
set CFG={"campus":{"username":"%U%","password":"%P%","isp":"%ISP%","isps":{"campus":{"username_suffix":"","ac_id":"1","extra":{}},"mobile":{"username_suffix":"@cmcc","ac_id":"1","extra":{}},"telecom":{"username_suffix":"@njxy","ac_id":"2","extra":{}}}}}
> "%TEMP%\campuspi_cfg.json" echo %CFG%
curl -s -X POST %PI%/api/config -H "Content-Type: application/json" -d "@%TEMP%\campuspi_cfg.json"
echo.
curl -s -X POST %PI%/api/action/login -H "Content-Type: application/json" -d "{}"
echo.

echo.
echo ============================================================
echo  Done. If step1 shows "LoginID_3"/success - gateway OK.
echo  Pi result shown above (ok/msg). Check log:
echo    ssh pi@IP  then:  tail -5 /var/log/campuspi.log
echo ============================================================
pause
