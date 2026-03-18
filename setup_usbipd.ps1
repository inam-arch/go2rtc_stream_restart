<#
.SYNOPSIS
    Share Windows USB webcams with WSL2 using usbipd-win.

.DESCRIPTION
    1. Verifies usbipd-win is installed (shows install command if not).
    2. Lists all USB video class devices found by usbipd.
    3. Binds each one  (marks it as shareable — one-time, survives reboots).
    4. Attaches each one to the active WSL2 distribution so they appear
       as /dev/video* inside WSL.

    After this runs, open a WSL terminal and execute:
        lsusb                       # confirm device is visible in Linux
        ls /dev/video*              # confirm v4l2 nodes exist
        python stream_camera_wsl.py # start streaming to go2rtc

.NOTES
    MUST be run as Administrator (usbipd bind/attach require elevation).

    Install usbipd-win (if missing):
        winget install --id Microsoft.usbipd

    WSL2 prerequisites — run once inside WSL:
        sudo apt-get update
        sudo apt-get install -y linux-tools-generic hwdata
        sudo update-alternatives --install /usr/local/bin/usbip usbip \
            /usr/lib/linux-tools/*-generic/usbip 20
        sudo apt-get install -y v4l-utils ffmpeg

    Docs: https://learn.microsoft.com/en-us/windows/wsl/connect-usb
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# ── auto-elevate if not already running as Administrator ──────────────────────
if (-not ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {

    Write-Host "[usbipd] Re-launching as Administrator (UAC prompt will appear)..." -ForegroundColor Yellow
    Start-Process powershell.exe -ArgumentList `
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" `
        -Verb RunAs
    exit
}

# ── helpers ────────────────────────────────────────────────────────────────────

function Test-CommandExists([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-UsbipdVideoDevices {
    <#
    Parses `usbipd list` and returns objects for camera / webcam devices.
    Handles both usbipd v3 and v4 output formats.
    #>
    $lines = usbipd list 2>&1 | Where-Object { $_ -match "^\s*\d+-\d+" }

    $devices = foreach ($line in $lines) {
        # Split on 2+ whitespace to get columns (BUSID, VID:PID, DEVICE, STATE)
        $cols = ($line.Trim() -split '\s{2,}', 4)
        if ($cols.Count -lt 3) { continue }
        [PSCustomObject]@{
            BusId       = $cols[0].Trim()
            VidPid      = $cols[1].Trim()
            Description = $cols[2].Trim()
            State       = if ($cols.Count -ge 4) { $cols[3].Trim() } else { "" }
        }
    }

    # Filter to video capture / webcam devices by common keywords and USB class
    return $devices | Where-Object {
        $_.Description -match "Camera|Webcam|Video|UVC|C920|C922|C930|C910|Logitech|OBS|Elgato|Capture"
    }
}

# ── check usbipd is installed ──────────────────────────────────────────────────

if (-not (Test-CommandExists "usbipd")) {
    Write-Host ""
    Write-Host "ERROR: usbipd not found on PATH." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it with one of these commands and re-run as Administrator:" -ForegroundColor Yellow
    Write-Host "    winget install --id Microsoft.usbipd" -ForegroundColor Cyan
    Write-Host "    # or download from: https://github.com/dorssel/usbipd-win/releases" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

$usbVersion = (usbipd --version 2>&1) -join ""
Write-Host "[usbipd] $usbVersion" -ForegroundColor Cyan
Write-Host ""

# ── check WSL is running ───────────────────────────────────────────────────────

$wslList = wsl --list --running 2>&1
$wslRunning = ($LASTEXITCODE -eq 0) -and ($wslList -notmatch "no running|There are no")

if (-not $wslRunning) {
    Write-Host "WARNING: No WSL2 distribution appears to be running." -ForegroundColor Yellow
    Write-Host "         Start WSL first (e.g. open a WSL terminal window)," -ForegroundColor Yellow
    Write-Host "         then re-run this script so usbipd can attach the device." -ForegroundColor Yellow
    Write-Host ""
    $cont = Read-Host "Continue anyway? (y/N)"
    if ($cont -notin @('y','Y')) { exit 0 }
    Write-Host ""
}

# ── enumerate video USB devices ───────────────────────────────────────────────

Write-Host "All USB devices (usbipd list):" -ForegroundColor Cyan
usbipd list
Write-Host ""

$videoDevices = Get-UsbipdVideoDevices

if (-not $videoDevices) {
    Write-Host "No USB video/webcam devices matched the filter." -ForegroundColor Yellow
    Write-Host "Check the list above and re-run with a device's BusId manually:" -ForegroundColor Yellow
    Write-Host "    usbipd bind   --busid <BUSID>" -ForegroundColor Cyan
    Write-Host "    usbipd attach --wsl  --busid <BUSID>" -ForegroundColor Cyan
    exit 0
}

Write-Host "Matched video devices:" -ForegroundColor Cyan
$videoDevices | Format-Table BusId, VidPid, Description, State -AutoSize

# ── bind and attach each device ───────────────────────────────────────────────

foreach ($dev in $videoDevices) {
    $busId = $dev.BusId
    $desc  = $dev.Description

    # --- bind (makes device shareable; only needs doing once) -----------------
    if ($dev.State -match "Not shared") {
        Write-Host "[usbipd] Binding $busId  ($desc) ..." -ForegroundColor Green
        $out = usbipd bind --busid $busId 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  BIND FAILED: $out" -ForegroundColor Red
            continue
        }
        if ($out) { $out | ForEach-Object { Write-Host "  $_" } }
    } else {
        Write-Host "[usbipd] $busId already bound  ($($dev.State))" -ForegroundColor DarkGreen
    }

    # --- attach to WSL --------------------------------------------------------
    # --force detaches the device from Windows drivers first (needed when the
    # webcam is "Shared/Attached" but still held by a Windows process such as
    # the Logitech service or a previous attach session).
    Write-Host "[usbipd] Attaching $busId to WSL (--force) ..." -ForegroundColor Green
    $out = usbipd attach --wsl --force --busid $busId 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ATTACH FAILED: $out" -ForegroundColor Red
        Write-Host "  Tips:" -ForegroundColor Yellow
        Write-Host "    • Make sure a WSL2 terminal is running (wsl -d Ubuntu-22.04)" -ForegroundColor Yellow
        Write-Host "    • Kill any Windows app holding the camera (Logi Capture, Camera app, etc.)" -ForegroundColor Yellow
        Write-Host "    • Manual fallback: usbipd attach --wsl --force --busid $busId" -ForegroundColor Cyan
    } else {
        if ($out) { $out | ForEach-Object { Write-Host "  $_" } }
        Write-Host "  OK" -ForegroundColor Green
    }
    Write-Host ""
}

# ── post-attach instructions ──────────────────────────────────────────────────

Write-Host "Done. In your WSL terminal run:" -ForegroundColor Cyan
Write-Host "    lsusb                         # webcam should appear here"
Write-Host "    ls /dev/video*                # v4l2 capture nodes"
Write-Host "    v4l2-ctl --list-devices        # human-readable device list"
Write-Host ""
Write-Host "Then start streaming:" -ForegroundColor Cyan
Write-Host "    cd $(Get-Location)"
Write-Host "    python stream_camera_wsl.py"
Write-Host ""
Write-Host "NOTE: You may need to re-run this script after every Windows reboot" -ForegroundColor DarkGray
Write-Host "      (the --busid changes; use usbipd list to find the new one)." -ForegroundColor DarkGray
