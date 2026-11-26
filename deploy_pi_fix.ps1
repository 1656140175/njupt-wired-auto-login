# -*- coding: utf-8 -*-
<#
Deploy the offline-DNS fix to a reachable CampusPi.

Usage:
  powershell -ExecutionPolicy Bypass -File .\deploy_pi_fix.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy_pi_fix.ps1 -SshUser pi
  powershell -ExecutionPolicy Bypass -File .\deploy_pi_fix.ps1 -FixPc

It copies hotspot.py, config.py and portal.py to /tmp, installs them into
/opt/campuspi, restarts campuspi, and prints the regenerated dnsmasq config. SSH asks for its
own password interactively; this script stores no password.

-FixPc additionally sets the PC's 192.168.9.x Ethernet metric to 100 and DNS
to public DNS, so Wi-Fi wins even before the new DHCP lease is applied.
Requires an elevated PowerShell.
#>
param(
    [string]$PiHost = "192.168.9.1",
    [string]$SshUser = "pi",
    [int]$TimeoutSec = 180,
    [switch]$FixPc,
    [switch]$Help
)

$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Help) {
    Write-Host "deploy_pi_fix.ps1 [-PiHost 192.168.9.1] [-SshUser pi] [-TimeoutSec 180] [-FixPc]"
    exit 0
}

function Test-TcpPort {
    param([string]$Target, [int]$Port, [int]$TimeoutMs = 900)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($Target, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

$ssh = Get-Command ssh -ErrorAction SilentlyContinue
$scp = Get-Command scp -ErrorAction SilentlyContinue
if ($null -eq $ssh -or $null -eq $scp) {
    Write-Host "ssh/scp not found in PATH."
    exit 1
}

$hotspot = Join-Path $Here "hotspot.py"
$config = Join-Path $Here "config.py"
$portal = Join-Path $Here "portal.py"
$main = Join-Path $Here "main.py"
foreach ($f in @($hotspot, $config, $portal, $main)) {
    if (-not (Test-Path $f)) {
        Write-Host "Missing file: $f"
        exit 1
    }
}

Write-Host ""
Write-Host "CampusPi offline-DNS fix deploy"
Write-Host "  Pi     : $SshUser@$PiHost"
Write-Host "  Files  : hotspot.py, config.py, portal.py, main.py"
Write-Host ""

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-TcpPort $PiHost 22 900) { break }
    Start-Sleep -Seconds 2
}
if (-not (Test-TcpPort $PiHost 22 900)) {
    Write-Host "SSH 192.168.9.1:22 is not reachable. Plug the Pi in and retry."
    exit 2
}

Write-Host "Copying files to /tmp ..."
& scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 $hotspot $config $portal $main "${SshUser}@${PiHost}:/tmp/"
if ($LASTEXITCODE -ne 0) {
    Write-Host "scp failed (exit $LASTEXITCODE)."
    exit 3
}

Write-Host "Installing, clearing stale processes, and restarting campuspi ..."
$remoteCmd = @'
set -e
echo "=== service path ==="
systemctl show campuspi -p WorkingDirectory -p ExecStart --no-pager
sudo install -m 644 /tmp/hotspot.py /opt/campuspi/hotspot.py
sudo install -m 644 /tmp/config.py /opt/campuspi/config.py
sudo install -m 644 /tmp/portal.py /opt/campuspi/portal.py
sudo install -m 644 /tmp/main.py /opt/campuspi/main.py
sudo rm -rf /opt/campuspi/__pycache__
sudo systemctl stop campuspi
sudo pkill -f '[p]ython3 /opt/campuspi/main.py' || true
sudo systemctl start campuspi
sleep 3
echo "=== installed markers ==="
grep -n CAMPUSPI_OFFLINE_DNS_V1 /opt/campuspi/hotspot.py || true
grep -n CAMPUSPI_SUFFIX_FALLBACK_V1 /opt/campuspi/portal.py || true
echo "=== running process ==="
ps -eo pid,args | grep '[c]ampuspi/main.py' || true
echo "=== dnsmasq config after restart ==="
sudo cat /etc/dnsmasq.d/campuspi.conf
echo "=== eth1 range check ==="
grep 'dhcp-range=interface:eth1' /etc/dnsmasq.d/campuspi.conf || true
echo "=== addresses ==="
ip -br addr || true
echo "=== service status ==="
systemctl is-active campuspi dnsmasq hostapd || true
echo "=== dnsmasq journal ==="
sudo journalctl -u dnsmasq -n 30 --no-pager || true
echo "=== hostapd journal ==="
sudo journalctl -u hostapd -n 20 --no-pager || true
echo "=== campuspi log tail ==="
sudo tail -n 40 /var/log/campuspi.log || true
echo "=== login test ==="
sudo python3 /opt/campuspi/main.py --check-login || true
'@
$remoteCmd = $remoteCmd -replace "`r`n", "`n"
& ssh -t -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$SshUser@$PiHost" $remoteCmd
if ($LASTEXITCODE -ne 0) {
    Write-Host "Remote install/restart failed (exit $LASTEXITCODE)."
    exit 4
}

if ($FixPc) {
    Write-Host ""
    Write-Host "Applying PC-side fallback: Ethernet metric=100, DNS=223.5.5.5,119.29.29.29"
    try {
        $ethIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -like "192.168.9.*" } |
            Select-Object -First 1
        if ($null -eq $ethIp) {
            Write-Host "  No 192.168.9.x interface found."
        } else {
            $adapter = Get-NetAdapter -InterfaceIndex $ethIp.InterfaceIndex -ErrorAction Stop
            Set-NetIPInterface -InterfaceAlias $adapter.Name -InterfaceMetric 100 -ErrorAction Stop
            Set-DnsClientServerAddress -InterfaceAlias $adapter.Name -ServerAddresses @("223.5.5.5", "119.29.29.29") -ErrorAction Stop
            Write-Host ("  Updated {0}: metric=100, DNS=223.5.5.5,119.29.29.29" -f $adapter.Name)
        }
    } catch {
        Write-Host ("  PC fix failed: {0}" -f $_.Exception.Message)
        Write-Host "  Run from an elevated PowerShell, or set metric/DNS manually."
    }
}

Write-Host ""
Write-Host "Done. Run 'ipconfig /renew' once if the PC still has the old lease."
Write-Host "Expected offline config: no dhcp-option 3, option 6 = 223.5.5.5, lease 2m."
