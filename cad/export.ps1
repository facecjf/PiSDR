<#
.SYNOPSIS
  Render every PiSDR printed part to STL with OpenSCAD.

.EXAMPLE
  .\export.ps1
  .\export.ps1 -OpenScad "D:\Tools\OpenSCAD\openscad.exe"

  Output goes to cad\stl\. Edit pisdr_params.scad first; the console shows the derived
  footprint, window and key positions so you can sanity-check them against the fit sheet.
#>
param(
    [string]$OpenScad = $env:OPENSCAD,
    [string]$OutDir = "",
    [string[]]$Parts = @("pisdr_front", "pisdr_rear", "pisdr_knob", "pisdr_keycaps", "pisdr_carrier", "pisdr_coupon")
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutDir) { $OutDir = Join-Path $here "stl" }

if (-not $OpenScad) {
    $candidates = @(
        "C:\Program Files\OpenSCAD\openscad.exe",
        "C:\Program Files (x86)\OpenSCAD\openscad.exe",
        "$env:LOCALAPPDATA\Programs\OpenSCAD\openscad.exe"
    ) + @(Get-Command openscad -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    $OpenScad = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $OpenScad) {
    throw "openscad.exe not found. Install OpenSCAD (https://openscad.org) or pass -OpenScad <path>."
}

$out = $OutDir
New-Item -ItemType Directory -Force -Path $out | Out-Null

foreach ($part in $Parts) {
    $src = Join-Path $here "$part.scad"
    $dst = Join-Path $out "$part.stl"
    Write-Host "== $part" -ForegroundColor Cyan
    # OpenSCAD writes its ECHO/WARNING lines to stderr; capture them without PowerShell treating them as errors.
    $log = Join-Path $out "$part.log"
    $proc = Start-Process -FilePath $OpenScad -ArgumentList @("-o", "`"$dst`"", "`"$src`"") `
        -NoNewWindow -Wait -PassThru -RedirectStandardError $log
    Get-Content $log | ForEach-Object {
        if ($_ -match "WARNING|ERROR") { Write-Host $_ -ForegroundColor Yellow }
        elseif ($_ -match "ECHO") { Write-Host ($_ -replace '^ECHO: ', '   ') }
    }
    Remove-Item $log -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { throw "OpenSCAD failed on $part" }
    $size = (Get-Item $dst).Length / 1KB
    Write-Host ("   -> {0} ({1:N0} KB)" -f $dst, $size)
}
Write-Host "Done. STLs are in $out" -ForegroundColor Green
