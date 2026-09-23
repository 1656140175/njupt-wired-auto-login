@echo off
rem ============================================================
rem  Allow LAN access from the CampusPi hotspot subnet.
rem  Windows blocks inbound traffic from 192.168.10.x by default
rem  (the USB NIC is treated as a Public network), so phones on
rem  the hotspot cannot reach this PC.
rem  Run as administrator.
rem    install : 1     remove : 2
rem ============================================================
chcp 936 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo  Need administrator rights.
  echo  Right-click this file - "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo  CampusPi hotspot LAN access
echo ============================================================
echo  1) Allow 192.168.10.0/24 to reach this PC
echo  2) Remove the rule
choice /c 12 /n /m "Choose (1/2): "
if errorlevel 2 goto remove

netsh advfirewall firewall delete rule name="CampusPi Hotspot LAN" >nul 2>nul
netsh advfirewall firewall add rule name="CampusPi Hotspot LAN" ^
  dir=in action=allow protocol=any remoteip=192.168.10.0/24 profile=any
netsh advfirewall firewall delete rule name="CampusPi Hotspot ICMP" >nul 2>nul
netsh advfirewall firewall add rule name="CampusPi Hotspot ICMP" ^
  dir=in action=allow protocol=icmpv4:8,any remoteip=192.168.10.0/24 profile=any
echo.
echo  Done. Phones on the hotspot can now reach this PC.
goto end

:remove
netsh advfirewall firewall delete rule name="CampusPi Hotspot LAN" >nul 2>nul
netsh advfirewall firewall delete rule name="CampusPi Hotspot ICMP" >nul 2>nul
echo.
echo  Rule removed.
:end
echo.
pause
