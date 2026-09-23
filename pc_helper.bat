@echo off
rem CampusPi PC Helper (manual)
rem All Chinese text is printed by pc_helper.py
chcp 936 >nul
cd /d "%~dp0"
echo ================================================
echo   CampusPi PC Helper
echo ================================================
where python >nul 2>nul
if %errorlevel%==0 (
  python pc_helper.py --login
) else (
  py -3 pc_helper.py --login
)
echo.
pause
