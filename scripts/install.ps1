#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repository = "https://github.com/kev0ps/agent-relay"
$sourceRef = if ([string]::IsNullOrWhiteSpace($env:AGENT_RELAY_REF)) {
    "main"
} else {
    $env:AGENT_RELAY_REF
}
$sourceRefKind = if ([string]::IsNullOrWhiteSpace($env:AGENT_RELAY_REF_KIND)) {
    "heads"
} else {
    $env:AGENT_RELAY_REF_KIND
}
$script:pythonVersion = if ([string]::IsNullOrWhiteSpace($env:AGENT_RELAY_PYTHON_VERSION)) {
    "3.13.5"
} else {
    $env:AGENT_RELAY_PYTHON_VERSION
}

if ($sourceRefKind -notin @("heads", "tags")) {
    throw "AGENT_RELAY_REF_KIND must be 'heads' or 'tags'."
}
if ($sourceRef -notmatch "^[A-Za-z0-9][A-Za-z0-9._/-]*$") {
    throw "AGENT_RELAY_REF contains unsupported characters."
}
if ($script:pythonVersion -notmatch "^[0-9]+(\.[0-9]+){1,2}$") {
    throw "AGENT_RELAY_PYTHON_VERSION contains unsupported characters."
}

function Get-UvPath {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "uv\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Refresh-ProcessPath {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $env:Path = @($userPath, $machinePath) -join ";"
}

function Ensure-Uv {
    $uvPath = Get-UvPath
    if ($null -ne $uvPath) {
        return $uvPath
    }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($null -ne $winget) {
        Write-Host "Installing uv with WinGet..."
        & $winget.Source install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements
        Refresh-ProcessPath
        $uvPath = Get-UvPath
    } else {
        Write-Host "WinGet is unavailable; downloading the official uv installer..."
        $uvInstaller = Join-Path $script:temporaryRoot "uv-install.ps1"
        Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/install.ps1" -OutFile $uvInstaller
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uvInstaller
        Refresh-ProcessPath
        $uvPath = Get-UvPath
    }

    if ($null -eq $uvPath) {
        throw "uv was installed but could not be found on PATH. Open a new PowerShell window and rerun the installer."
    }
    return $uvPath
}

function Ensure-Python([string] $uvPath) {
    Write-Host "Installing or verifying Python $script:pythonVersion with uv..."
    & $uvPath python install $script:pythonVersion
    if ($LASTEXITCODE -ne 0) {
        throw "uv python install failed with exit code $LASTEXITCODE."
    }
    $env:UV_PYTHON = $script:pythonVersion
}

function Sync-Project([string] $uvPath) {
    $syncRootValue = $env:AGENT_RELAY_SYNC_ROOT
    if ([string]::IsNullOrWhiteSpace($syncRootValue)) {
        return
    }
    if (-not (Test-Path -LiteralPath $syncRootValue -PathType Container) -or
        -not (Test-Path -LiteralPath (Join-Path $syncRootValue "pyproject.toml") -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $syncRootValue "uv.lock") -PathType Leaf)) {
        throw "AGENT_RELAY_SYNC_ROOT is not a locked Agent Relay project: $syncRootValue"
    }

    $syncProfile = if ([string]::IsNullOrWhiteSpace($env:AGENT_RELAY_SYNC_PROFILE)) {
        "base"
    } else {
        $env:AGENT_RELAY_SYNC_PROFILE.ToLowerInvariant()
    }
    if ($syncProfile -notin @("base", "browser", "computer")) {
        throw "AGENT_RELAY_SYNC_PROFILE must be 'base', 'browser', or 'computer'."
    }
    $syncArguments = @("sync", "--locked")
    if ($syncProfile -eq "browser") {
        $syncArguments += @("--extra", "browser")
    } elseif ($syncProfile -eq "computer") {
        $syncArguments += @("--extra", "browser", "--extra", "computer")
    }

    $resolvedRoot = (Resolve-Path -LiteralPath $syncRootValue).Path
    Write-Host "Installing locked $syncProfile dependencies with uv..."
    Push-Location -LiteralPath $resolvedRoot
    try {
        & $uvPath @syncArguments
        if ($LASTEXITCODE -ne 0) {
            throw "uv sync failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }
}

function Add-UserPathEntry([string] $entry) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @(
        $userPath -split ";" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($entries | Where-Object { $_.TrimEnd("\") -ieq $entry.TrimEnd("\") }) {
        return
    }
    [Environment]::SetEnvironmentVariable("Path", (@($entries) + $entry) -join ";", "User")
}

function Invoke-AgentRelay([string[]] $Arguments) {
    & $script:agentRelayCommand @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "agent-relay failed with exit code $LASTEXITCODE."
    }
}

$script:temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("agent-relay-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $script:temporaryRoot -Force | Out-Null

try {
    $uv = Ensure-Uv
    Ensure-Python $uv
    Sync-Project $uv
    $projectRoot = $env:AGENT_RELAY_PROJECT_ROOT
    if ([string]::IsNullOrWhiteSpace($projectRoot)) {
        $archivePath = Join-Path $script:temporaryRoot "agent-relay.zip"
        $expandedRoot = Join-Path $script:temporaryRoot "expanded"
        $archiveSource = $env:AGENT_RELAY_ARCHIVE_SOURCE
        if ([string]::IsNullOrWhiteSpace($archiveSource)) {
            $archiveUri = "https://codeload.github.com/kev0ps/agent-relay/zip/refs/$sourceRefKind/$sourceRef"
            Write-Host "Downloading Agent Relay ($sourceRefKind/$sourceRef)..."
            Invoke-WebRequest -UseBasicParsing -Uri $archiveUri -OutFile $archivePath
        } elseif (Test-Path -LiteralPath $archiveSource -PathType Leaf) {
            Copy-Item -LiteralPath $archiveSource -Destination $archivePath -Force
        } else {
            throw "AGENT_RELAY_ARCHIVE_SOURCE is not a file: $archiveSource"
        }
        Expand-Archive -LiteralPath $archivePath -DestinationPath $expandedRoot -Force

        $projects = @(Get-ChildItem -LiteralPath $expandedRoot -Filter "pyproject.toml" -File -Recurse)
        if ($projects.Count -ne 1) {
            throw "The downloaded Agent Relay archive did not contain exactly one project."
        }
        $projectRoot = $projects[0].Directory.FullName
    } else {
        if (-not (Test-Path -LiteralPath $projectRoot -PathType Container) -or
            -not (Test-Path -LiteralPath (Join-Path $projectRoot "pyproject.toml") -PathType Leaf)) {
            throw "AGENT_RELAY_PROJECT_ROOT is not a valid Agent Relay project: $projectRoot"
        }
        $projectRoot = (Resolve-Path -LiteralPath $projectRoot).Path
    }

    Write-Host "Installing the Agent Relay command for the current user..."
    & $uv tool install --force $projectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool install failed with exit code $LASTEXITCODE."
    }

    $toolBin = (& $uv tool dir --bin).Trim()
    if ([string]::IsNullOrWhiteSpace($toolBin) -or -not (Test-Path -LiteralPath $toolBin -PathType Container)) {
        throw "uv did not report a valid tool bin directory."
    }
    if ($env:AGENT_RELAY_SKIP_PATH_UPDATE -ne "1") {
        Add-UserPathEntry $toolBin
    }
    $env:Path = "$toolBin;$env:Path"

    $agentRelayCandidates = @(Get-ChildItem -LiteralPath $toolBin -Filter "agent-relay*" -File)
    $agentRelay = $agentRelayCandidates |
        Where-Object { $_.Name -in @("agent-relay.exe", "agent-relay.cmd", "agent-relay.ps1") } |
        Select-Object -First 1
    if ($null -eq $agentRelay) {
        throw "The agent-relay command was not found in $toolBin."
    }
    $script:agentRelayCommand = $agentRelay.FullName

    $setupMode = if ([string]::IsNullOrWhiteSpace($env:AGENT_RELAY_SETUP)) {
        "prompt"
    } else {
        $env:AGENT_RELAY_SETUP.ToLowerInvariant()
    }
    if ($setupMode -notin @("prompt", "local", "skip")) {
        throw "AGENT_RELAY_SETUP must be 'prompt', 'local', or 'skip'."
    }
    if ($setupMode -eq "prompt") {
        $answer = Read-Host "Configure a local Server and Agent now? [Y/n]"
        if ($answer.Trim() -match "^[Nn]") {
            $setupMode = "skip"
        } else {
            $setupMode = "local"
        }
    }

    if ($setupMode -eq "local") {
        $configPath = Join-Path $env:USERPROFILE ".agent-relay\config.yaml"
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            Invoke-AgentRelay @("config", "init", "server")
            Invoke-AgentRelay @("config", "set", "server", "host", "127.0.0.1")
        } else {
            Write-Host "Existing Agent Relay configuration found; leaving it unchanged."
        }

        $agentTokenPath = Join-Path $env:USERPROFILE ".agent-relay\secrets\server\agent_token"
        $agentConfigMarker = Join-Path $env:USERPROFILE ".agent-relay\secrets\agent\agent_token"
        if ((Test-Path -LiteralPath $configPath -PathType Leaf) -and
            -not (Test-Path -LiteralPath $agentConfigMarker -PathType Leaf)) {
            if (-not (Test-Path -LiteralPath $agentTokenPath -PathType Leaf)) {
                throw "The Server Agent token file was not created."
            }
            Get-Content -Raw -LiteralPath $agentTokenPath |
                & $script:agentRelayCommand config init agent --stdin --no-tools
            if ($LASTEXITCODE -ne 0) {
                throw "Agent configuration initialization failed with exit code $LASTEXITCODE."
            }
        } else {
            Write-Host "Existing Agent secret found; leaving it unchanged."
        }
    }

    Write-Host ""
    Write-Host "Agent Relay installed for the current user."
    Write-Host "Start the local deployment in two PowerShell windows:"
    Write-Host "  agent-relay server"
    Write-Host "  agent-relay agent"
    Write-Host ""
    Write-Host "For a remote Server, set AGENT_RELAY_SETUP=skip and configure the Agent URL and token file separately."
} finally {
    if (Test-Path -LiteralPath $script:temporaryRoot) {
        Remove-Item -LiteralPath $script:temporaryRoot -Recurse -Force
    }
}
