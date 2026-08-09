Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DriverCommit = "1760f253d3c4d76618a8c97a04f2c100ffc491ac"
$DriverVersion = "0.12.6"
$InstallerSha256 = "85227ad5400240ccdcd8be18024ad871d1382d9e0b7f66dcce778e0ae4427f73"

if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    throw "RUNNER_TEMP is unavailable"
}
if ([string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) {
    throw "GITHUB_ENV is unavailable"
}

$driverRoot = Join-Path $env:RUNNER_TEMP "agent-relay-cua-driver"
$driverBin = Join-Path $driverRoot "bin"
$driverHome = Join-Path $driverRoot "home"
New-Item -ItemType Directory -Force -Path $driverRoot, $driverHome | Out-Null
if (Test-Path -LiteralPath $driverBin) {
    Remove-Item -LiteralPath $driverBin -Recurse -Force
}

$installerUrl = "https://raw.githubusercontent.com/trycua/cua/$DriverCommit/libs/cua-driver/scripts/install.ps1"
$installer = Join-Path $env:RUNNER_TEMP "agent-relay-cua-driver-install.ps1"
Invoke-WebRequest -Uri $installerUrl -OutFile $installer
$actualSha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -cne $InstallerSha256) {
    throw "Pinned cua-driver installer SHA256 mismatch"
}

$env:CUA_DRIVER_RS_INSTALL_DIR = $driverBin
$env:CUA_DRIVER_RS_HOME = $driverHome
$env:CUA_DRIVER_RS_KEEP_VERSIONS = "1"
$env:CUA_DRIVER_RS_VERSION = $DriverVersion
& $installer -Release $DriverVersion -NoAutoStart -NoPathUpdate
$installerExit = $LASTEXITCODE

$driver = Join-Path $driverBin "cua-driver.exe"
if (-not (Test-Path -LiteralPath $driver -PathType Leaf)) {
    throw "Installed cua-driver binary is missing"
}
$binInfo = Get-Item -LiteralPath $driverBin -Force
$currentDir = Join-Path $driverHome "packages\current"
$currentInfo = Get-Item -LiteralPath $currentDir -Force
if (($binInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -or
    ($currentInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
    throw "Installed cua-driver junction layout is invalid"
}
$version = (& $driver --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($version)) {
    throw "Installed cua-driver --version failed"
}
Write-Host "Installed cua-driver: $version"
if ($installerExit -ne 0) {
    Write-Warning "Pinned cua-driver installer returned $installerExit after a valid binary/junction/version check"
}

"RELAY_AGENT_COMPUTER_DRIVER_PATH=$driver" | Add-Content -LiteralPath $env:GITHUB_ENV
"CUA_DRIVER_RS_HOME=$driverHome" | Add-Content -LiteralPath $env:GITHUB_ENV
"CUA_DRIVER_RS_INSTALL_DIR=$driverBin" | Add-Content -LiteralPath $env:GITHUB_ENV
