# Temporarily add 192.168.9.2/24 to the wired Ethernet adapter and test the
# Pi's eth1 at 192.168.9.1. Wi-Fi and the default route are left untouched.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\test_pi_link.ps1
#   powershell -ExecutionPolicy Bypass -File .\test_pi_link.ps1 -Keep
#
# The script self-elevates. If 192.168.9.1 answers on SSH or HTTP, the
# temporary address is kept so you can log in; otherwise it is removed.

param(
    [string]$TestAddress = "192.168.9.2",
    [string]$PiAddress = "192.168.9.1",
    [switch]$Keep
)

$ErrorActionPreference = "Continue"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host "Requesting administrator privileges..."
    $arg = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$PSCommandPath`""
    if ($Keep) { $arg += " -Keep" }
    Start-Process powershell -Verb RunAs -ArgumentList $arg
    exit 0
}

function Test-TcpPort {
    param([string]$Target, [int]$Port, [int]$TimeoutMs = 1200)
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

$adapter = Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Status -eq "Up" -and
        $_.MediaType -eq "802.3" -and
        $_.InterfaceDescription -notmatch "Wi-?Fi|Wireless|Bluetooth|802\.11"
    } | Select-Object -First 1

if ($null -eq $adapter) {
    Write-Host "No connected wired Ethernet adapter found."
    exit 2
}

$alias = $adapter.Name
$idx = $adapter.ifIndex
Write-Host ("Adapter: {0} (ifIndex {1}), link {2}" -f $alias, $idx, $adapter.LinkSpeed)

$existing = @(Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -like "192.168.9.*" })
$added = $false
if ($existing.Count -gt 0) {
    Write-Host ("Already has 192.168.9.x: {0}" -f (($existing | ForEach-Object { $_.IPAddress }) -join ", "))
} else {
    Write-Host ("Adding temporary address {0}/24 ..." -f $TestAddress)
    & netsh interface ip add address name="$alias" $TestAddress 255.255.255.0 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Failed to add the temporary address."
        exit 3
    }
    $added = $true
}

$reachable = $false
try {
    Start-Sleep -Seconds 1
    Write-Host ""
    Write-Host ("Ping {0} ..." -f $PiAddress)
    & ping -n 3 -w 1000 $PiAddress
    $ssh = Test-TcpPort $PiAddress 22
    $web = Test-TcpPort $PiAddress 8080
    Write-Host ("TCP 22  : {0}" -f $(if ($ssh) { "UP" } else { "DOWN" }))
    Write-Host ("TCP 8080: {0}" -f $(if ($web) { "UP" } else { "DOWN" }))
    Write-Host ""
    Write-Host "ARP:"
    & arp -a | Select-String -Pattern "192\.168\.9\."
    $reachable = $ssh -or $web
    Write-Host ""
    if ($reachable) {
        Write-Host "Pi eth1 is reachable from the PC."
        if ($ssh) {
            Write-Host ("SSH: ssh -o StrictHostKeyChecking=accept-new pi@{0}" -f $PiAddress)
            Write-Host "Then run:"
            Write-Host "  ip -br addr; systemctl is-active campuspi dnsmasq hostapd; sudo tail -n 80 /var/log/campuspi.log"
        }
        if ($web) {
            Write-Host ("Web UI: http://{0}:8080" -f $PiAddress)
        }
    } else {
        Write-Host "192.168.9.1 did not respond."
        Write-Host "Check that the PC cable is in the Pi's USB Ethernet adapter (eth1),"
        Write-Host "not the Pi's onboard Ethernet port (eth0)."
        Write-Host "Also check that campuspi is running on the Pi."
    }
} finally {
    if ($added -and -not $Keep -and -not $reachable) {
        Write-Host ""
        Write-Host ("Removing temporary address {0} ..." -f $TestAddress)
        & netsh interface ip delete address name="$alias" $TestAddress | Out-Null
    } elseif ($added) {
        Write-Host ""
        Write-Host ("Keeping {0} for troubleshooting. Remove it when done:" -f $TestAddress)
        Write-Host ("  netsh interface ip delete address name=`"{0}`" {1}" -f $alias, $TestAddress)
    }
}
