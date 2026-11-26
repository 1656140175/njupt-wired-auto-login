# -*- coding: utf-8 -*-
<#
CampusPi diagnostic collector.

Usage:
  powershell -ExecutionPolicy Bypass -File .\collect_pi_diag.ps1
  powershell -ExecutionPolicy Bypass -File .\collect_pi_diag.ps1 -SshUser pi
  powershell -ExecutionPolicy Bypass -File .\collect_pi_diag.ps1 -NoSsh
  powershell -ExecutionPolicy Bypass -File .\collect_pi_diag.ps1 -FixPc

The script uses 192.168.9.1 directly, never DNS, so it still works while the
Pi is hijacking the PC's DNS. Plug the Pi's USB Ethernet in when prompted.
It collects the Pi's web API (if open), optional SSH diagnostics, and the PC's
network state. It never prints or stores the campus account password: the
Pi-side config is redacted, and SSH asks for its own password interactively.
#>
param(
    [string]$PiHost = "192.168.9.1",
    [string]$SshUser = "pi",
    [string]$OutDir = "D:\AI\network\campuspi\diag",
    [int]$TimeoutSec = 180,
    [switch]$NoSsh,
    [switch]$FixPc,
    [switch]$Help
)

$ErrorActionPreference = "Continue"

if ($Help) {
    Write-Host "collect_pi_diag.ps1 [-PiHost 192.168.9.1] [-SshUser pi] [-OutDir ...] [-TimeoutSec 180] [-NoSsh] [-FixPc]"
    exit 0
}

function Test-TcpPort {
    param([string]$Target, [int]$Port, [int]$TimeoutMs = 900)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($Target, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Save-Text {
    param([string]$Path, [string]$Text)
    Set-Content -Path $Path -Value $Text -Encoding UTF8
}

function Save-Json {
    param([string]$Path, $Object)
    if ($null -eq $Object) {
        Save-Text $Path "null"
        return
    }
    $Object | ConvertTo-Json -Depth 12 | Set-Content -Path $Path -Encoding UTF8
}

function Get-JsonUrl {
    param([string]$Url)
    try {
        return Invoke-RestMethod -Uri $Url -TimeoutSec 6 -ErrorAction Stop
    } catch {
        $msg = $_.Exception.Message
        $safe = ($Url -replace "^https?://", "" -replace "[^A-Za-z0-9._-]", "_")
        Save-Text (Join-Path $script:RunDir ("http-error-" + $safe + ".txt")) $msg
        return $null
    }
}

function Collect-PcState {
    param([string]$Path)
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("=== collected_at ===")
    $lines.Add((Get-Date).ToString("o"))
    $lines.Add("=== Get-NetAdapter ===")
    $lines.Add((Get-NetAdapter | Format-Table -AutoSize | Out-String))
    $lines.Add("=== Get-NetIPConfiguration ===")
    $lines.Add((Get-NetIPConfiguration | Format-List | Out-String))
    $lines.Add("=== Get-DnsClientServerAddress IPv4 ===")
    $lines.Add((Get-DnsClientServerAddress -AddressFamily IPv4 | Format-Table -AutoSize | Out-String))
    $lines.Add("=== default routes ===")
    $lines.Add((Get-NetRoute -DestinationPrefix "0.0.0.0/0" | Sort-Object RouteMetric | Format-Table -AutoSize | Out-String))
    $lines.Add("=== IP interfaces ===")
    $lines.Add((Get-NetIPInterface -AddressFamily IPv4 | Sort-Object InterfaceMetric | Format-Table -AutoSize | Out-String))
    $lines.Add("=== neighbors ===")
    $lines.Add((Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.State -ne "Unreachable" } | Format-Table -AutoSize | Out-String))
    $lines.Add("=== ipconfig /all ===")
    $lines.Add((ipconfig /all | Out-String))
    Save-Text $Path ($lines -join [Environment]::NewLine)
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$RunDir = Join-Path $OutDir $stamp
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$script:RunDir = $RunDir

Write-Host ""
Write-Host "CampusPi diagnostic collector"
Write-Host "  Pi        : $PiHost (IP only; DNS not used)"
Write-Host "  Output    : $RunDir"
Write-Host "  SSH       : $(if ($NoSsh) { 'disabled' } else { $SshUser + '@' + $PiHost + ' (interactive password)' })"
Write-Host ""
Write-Host "Plug the Pi's USB Ethernet into this PC now."
Write-Host "Do not unplug it until this script says it is done."
Write-Host ""

Collect-PcState (Join-Path $RunDir "pc-before.txt")

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$httpOk = $false
$sshOk = $false
$lastNotice = Get-Date
Write-Host "Waiting for the Pi (HTTP 8080 / SSH 22) ..."
while ((Get-Date) -lt $deadline) {
    $httpOk = Test-TcpPort $PiHost 8080 800
    $sshOk = Test-TcpPort $PiHost 22 800
    if ($httpOk -or $sshOk) {
        break
    }
    $now = Get-Date
    if (($now - $lastNotice).TotalSeconds -ge 15) {
        Write-Host ("  still waiting ... HTTP={0} SSH={1}" -f $httpOk, $sshOk)
        $lastNotice = $now
    }
    Start-Sleep -Seconds 2
}

if (-not ($httpOk -or $sshOk)) {
    Write-Host ""
    Write-Host "The Pi did not answer on 192.168.9.1:8080 or :22 within $TimeoutSec seconds."
    Write-Host "Check the USB Ethernet cable, the Pi power, and that the Pi booted."
    Collect-PcState (Join-Path $RunDir "pc-after.txt")
    Write-Host "PC state saved. You can unplug the Pi."
    exit 2
}

Write-Host ("Reached the Pi: HTTP={0} SSH={1}" -f $httpOk, $sshOk)

if ($httpOk) {
    Write-Host "Collecting web API ..."
    $status = Get-JsonUrl ("http://" + $PiHost + ":8080/api/status")
    $config = Get-JsonUrl ("http://" + $PiHost + ":8080/api/config")
    $logs = Get-JsonUrl ("http://" + $PiHost + ":8080/api/logs?lines=500")
    Save-Json (Join-Path $RunDir "pi-status.json") $status
    Save-Json (Join-Path $RunDir "pi-config-masked.json") $config
    Save-Json (Join-Path $RunDir "pi-logs.json") $logs
    if ($null -eq $status) {
        Write-Host "  Web API requires authentication or is unavailable; saved error details."
    } else {
        Write-Host ("  online={0} state={1}" -f $status.online, $status.state)
    }
} else {
    Write-Host "HTTP 8080 not open; skipping web API."
}

if (-not $NoSsh -and $sshOk) {
    $ssh = Get-Command ssh -ErrorAction SilentlyContinue
    if ($null -eq $ssh) {
        Write-Host "ssh.exe not found; skipping deep SSH collection."
    } else {
        Write-Host ""
        Write-Host "Starting one SSH session for deep diagnostics."
        Write-Host "If prompted, enter the Pi SSH password (it is not stored by this script)."
        $remoteScriptPath = Join-Path $RunDir "remote_collect.sh"
        $remote = @'
set +e
echo "=== BASIC ==="
date -Is 2>/dev/null
hostname 2>/dev/null
uptime 2>/dev/null
uname -a 2>/dev/null
cat /etc/os-release 2>/dev/null
echo "=== IP ==="
ip -br addr 2>/dev/null
ip route 2>/dev/null
echo "=== SERVICES ==="
systemctl is-active campuspi dnsmasq hostapd 2>&1
systemctl status --no-pager -l campuspi dnsmasq hostapd 2>&1 | head -n 160
echo "=== PROCESSES ==="
ps aux 2>/dev/null | grep -E 'dnsmasq|hostapd|dhcpd|campuspi' | grep -v grep
echo "=== LISTENERS ==="
ss -lunp 2>/dev/null | grep -E ':(53|67|68)([[:space:]]|$)' || true
echo "=== DNSMASQ CONF ==="
cat /etc/dnsmasq.d/campuspi.conf 2>/dev/null || echo MISSING
echo "=== HOSTAPD CONF (REDACTED) ==="
sed -E 's/^(wpa_passphrase=).*/\1***/' /etc/hostapd/hostapd.conf 2>/dev/null || echo MISSING
echo "=== CAMPUSPI CONFIG (REDACTED) ==="
if [ -r /etc/campuspi/config.json ]; then
  sed -E 's/("(password|username|user_account|user_password)"[[:space:]]*:[[:space:]]*")[^"]*"/\1***"/g' /etc/campuspi/config.json
elif sudo -n test -r /etc/campuspi/config.json 2>/dev/null; then
  sudo -n sed -E 's/("(password|username|user_account|user_password)"[[:space:]]*:[[:space:]]*")[^"]*"/\1***"/g' /etc/campuspi/config.json
else
  echo "UNREADABLE (need root/sudo)"
fi
echo "=== IPTABLES NAT ==="
iptables -t nat -S 2>&1
echo "=== IPTABLES FILTER ==="
iptables -S 2>&1
echo "=== DHCPCD ==="
grep -vE '^#|^$' /etc/dhcpcd.conf 2>/dev/null || true
echo "=== NETWORKMANAGER ==="
nmcli -t -f NAME,DEVICE,TYPE,STATE connection show 2>/dev/null || true
echo "=== CAMPUSPI LOG (tail 200) ==="
tail -n 200 /var/log/campuspi.log 2>/dev/null || true
echo "=== JOURNAL (tail 120) ==="
journalctl -u campuspi -n 120 --no-pager 2>/dev/null || true
echo "=== ARP ==="
cat /proc/net/arp 2>/dev/null || true
echo "=== WIFI ==="
iw dev 2>/dev/null || true
iwconfig 2>/dev/null || true
'@
        Set-Content -Path $remoteScriptPath -Value $remote -Encoding ASCII
        $sshOut = Join-Path $RunDir "pi-ssh.txt"
        $sshCmd = 'ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "' + $SshUser + '@' + $PiHost + '" "bash -s" < "' + $remoteScriptPath + '"'
        & cmd.exe /c $sshCmd 2>&1 | Tee-Object -FilePath $sshOut
        if ($LASTEXITCODE -ne 0) {
            Write-Host ("  SSH exited with code {0}; see pi-ssh.txt" -f $LASTEXITCODE)
        } else {
            Write-Host "  SSH diagnostics saved."
        }
    }
} elseif (-not $NoSsh) {
    Write-Host "SSH 22 not open; skipping deep collection."
}

Collect-PcState (Join-Path $RunDir "pc-after.txt")

if ($FixPc) {
    Write-Host ""
    Write-Host "Attempting PC-side fix: Ethernet metric=100, DNS=223.5.5.5,119.29.29.29"
    try {
        $ethIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -like "192.168.9.*" } |
            Select-Object -First 1
        if ($null -eq $ethIp) {
            Write-Host "  No 192.168.9.x interface found; is the Pi plugged in?"
        } else {
            $adapter = Get-NetAdapter -InterfaceIndex $ethIp.InterfaceIndex -ErrorAction Stop
            Set-NetIPInterface -InterfaceAlias $adapter.Name -InterfaceMetric 100 -ErrorAction Stop
            Set-DnsClientServerAddress -InterfaceAlias $adapter.Name -ServerAddresses @("223.5.5.5", "119.29.29.29") -ErrorAction Stop
            Write-Host ("  Updated {0}: metric=100, DNS=223.5.5.5,119.29.29.29" -f $adapter.Name)
            Write-Host "  You may now keep the Pi plugged in and use Wi-Fi."
        }
    } catch {
        Write-Host ("  PC fix failed: {0}" -f $_.Exception.Message)
        Write-Host "  Run this script from an elevated PowerShell, or set the metric/DNS manually."
    }
}

$zip = Join-Path $OutDir ("campuspi-diag-" + $stamp + ".zip")
try {
    Compress-Archive -Path (Join-Path $RunDir "*") -DestinationPath $zip -Force
    Write-Host ""
    Write-Host "Saved: $zip"
} catch {
    Write-Host ("Zip failed: {0}" -f $_.Exception.Message)
}

Write-Host ""
Write-Host "Collection complete. You can unplug the Pi now."
Write-Host "Send the zip directory path above back to the assistant."
