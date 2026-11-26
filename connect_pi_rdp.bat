@echo off
rem Connect to the Raspberry Pi desktop over the local USB Ethernet link.
rem Requires xrdp on the Pi (install once from the CampusPi web page).
mstsc /v:192.168.9.1
