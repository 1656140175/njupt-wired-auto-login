@echo off
rem CampusPi PC Helper (silent, optional)
rem Put a shortcut of this file into shell:startup if needed
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw pc_helper.py --watch --login --interval 10
) else (
  start "" pyw -3 pc_helper.py --watch --login --interval 10
)
