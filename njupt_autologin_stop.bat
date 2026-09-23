@echo off
rem ============================================================
rem  Stop the NJUPT auto login watch process (running in
rem  background via pythonw). Run this to clean up.
rem ============================================================
chcp 936 >nul
echo Stopping NJUPT auto login background process...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*njupt_autologin*' } | ForEach-Object { Write-Host ('  stopped PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
echo.
echo Done. (If nothing listed, no watch process was running.)
pause
