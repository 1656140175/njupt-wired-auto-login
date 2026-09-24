@echo off
rem ============================================================
rem  Allow LAN access from the CampusPi hotspot subnet.
rem
rem  Why this is needed:
rem    Windows treats the USB NIC (to the Raspberry Pi) as a
rem    PUBLIC network by default, which blocks ALL inbound
rem    traffic. So phones on the hotspot cannot reach this PC
rem    even when the Pi routes the packets correctly.
rem
rem  Two ways to fix (this script offers both):
rem    1) Add a firewall rule allowing 192.168.10.0/24  (targeted)
rem    2) Set the network profile to Private            (broad)
rem  Option 1 is narrower. Option 2 is what usually happens
rem  when Windows asks "make this PC discoverable?" and you
rem  click Yes - that is why it sometimes works by itself.
rem
rem  Run as administrator.  install : 1 / 2 / 3,  remove : 4
rem ============================================================
chcp 936 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo.
  echo  Need administrator rights.
  echo  Right-click this file - "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo  CampusPi hotspot LAN access
echo ============================================================
echo  Current network profiles:
powershell -NoProfile -Command "Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory | Format-Table -AutoSize"
echo.
echo  1) Add firewall rule for 192.168.10.0/24  (recommended)
echo  2) Set the USB NIC network to Private
echo  3) Do both 1 and 2
echo  4) Remove everything this script added
choice /c 1234 /n /m "Choose (1/2/3/4): "
if errorlevel 4 goto remove
if errorlevel 3 goto both
if errorlevel 2 goto private
goto firewall

:firewall
call :addfw
goto end

:both
call :addfw
call :setprivate
goto end

:private
call :setprivate
goto end

:addfw
echo.
echo  Adding firewall rules...
netsh advfirewall firewall delete rule name="CampusPi Hotspot LAN" >nul 2>nul
netsh advfirewall firewall add rule name="CampusPi Hotspot LAN" ^
  dir=in action=allow protocol=any remoteip=192.168.10.0/24 profile=any
netsh advfirewall firewall delete rule name="CampusPi Hotspot ICMP" >nul 2>nul
netsh advfirewall firewall add rule name="CampusPi Hotspot ICMP" ^
  dir=in action=allow protocol=icmpv4:8,any remoteip=192.168.10.0/24 profile=any
echo  [OK] Firewall rules added
exit /b

:setprivate
echo.
echo  Setting network profile to Private (for 192.168.9.x NICs)...
powershell -NoProfile -Command ^
  "Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -ne 'NoTraffic' } | ForEach-Object { try { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private -ErrorAction Stop; Write-Host ('  [OK] ' + $_.InterfaceAlias + ' -> Private') } catch { Write-Host ('  [--] ' + $_.InterfaceAlias + ' skipped') } }"
exit /b

:remove
echo.
echo  Removing firewall rules...
netsh advfirewall firewall delete rule name="CampusPi Hotspot LAN" >nul 2>nul
netsh advfirewall firewall delete rule name="CampusPi Hotspot ICMP" >nul 2>nul
echo  [OK] Firewall rules removed
echo  (Network profile left unchanged - set it back in Settings if needed)
goto end

:end
echo.
echo  Done. Test from the phone:  ping 192.168.9.100  (this PC's IP)
echo.
pause
