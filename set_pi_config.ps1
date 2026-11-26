# -*- coding: utf-8 -*-
<#
Set non-secret CampusPi settings over SSH.

Usage:
  powershell -ExecutionPolicy Bypass -File .\set_pi_config.ps1 -Isp campus
  powershell -ExecutionPolicy Bypass -File .\set_pi_config.ps1 -ShareOnlineOnly false
  powershell -ExecutionPolicy Bypass -File .\set_pi_config.ps1 -Isp campus -ShareOnlineOnly true

It edits /etc/campuspi/config.json atomically, keeps mode 0600, restarts
campuspi, and prints only the non-secret values it changed. It never reads or
prints the campus password. SSH asks for its own password interactively.
#>
param(
    [ValidateSet("campus", "mobile", "telecom")]
    [string]$Isp = "",
    [ValidateSet("true", "false")]
    [string]$ShareOnlineOnly = "",
    [string]$PiHost = "192.168.9.1",
    [string]$SshUser = "pi",
    [int]$TimeoutSec = 120,
    [switch]$NoRestart,
    [switch]$Help
)

$ErrorActionPreference = "Continue"

if ($Help -or ($Isp -eq "" -and $ShareOnlineOnly -eq "")) {
    Write-Host "set_pi_config.ps1 [-Isp campus|mobile|telecom] [-ShareOnlineOnly true|false] [-NoRestart]"
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

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-TcpPort $PiHost 22 900) { break }
    Start-Sleep -Seconds 2
}
if (-not (Test-TcpPort $PiHost 22 900)) {
    Write-Host "SSH 192.168.9.1:22 is not reachable. Plug the Pi in and retry."
    exit 2
}

$remoteScript = @'
import json
import os
import sys

path = "/etc/campuspi/config.json"
isp = sys.argv[1] if len(sys.argv) > 1 else ""
share = sys.argv[2] if len(sys.argv) > 2 else ""

with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

if isp:
    data.setdefault("campus", {})["isp"] = isp
if share:
    data.setdefault("pc", {})["share_online_only"] = (share == "true")

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)

print("isp=%s share_online_only=%s" % (
    data.get("campus", {}).get("isp"),
    data.get("pc", {}).get("share_online_only"),
))
'@
$remoteScriptPath = Join-Path $env:TEMP "campuspi_set_config.py"
Set-Content -Path $remoteScriptPath -Value $remoteScript -Encoding ASCII

$remoteDir = "/tmp"
& scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 $remoteScriptPath "${SshUser}@${PiHost}:$remoteDir/"
if ($LASTEXITCODE -ne 0) {
    Write-Host "scp failed (exit $LASTEXITCODE)."
    exit 3
}

$argsText = "'" + $Isp + "' '" + $ShareOnlineOnly + "'"
$remoteCmd = "sudo python3 /tmp/campuspi_set_config.py $argsText"
& ssh -t -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$SshUser@$PiHost" $remoteCmd
if ($LASTEXITCODE -ne 0) {
    Write-Host "Remote config update failed (exit $LASTEXITCODE)."
    exit 4
}

if (-not $NoRestart) {
    Write-Host "Restarting campuspi ..."
    & ssh -t -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$SshUser@$PiHost" "sudo systemctl restart campuspi; sleep 2; systemctl is-active campuspi dnsmasq hostapd || true"
}

Write-Host "Done."
