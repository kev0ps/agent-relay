# Install Agent Relay on Windows

Agent Relay is an experimental Python application. There is no MSI or Windows
service package yet. The supported convenience path is a PowerShell
bootstrapper that installs the command for the current user and can initialize
a local Server plus Agent.

## One-line installation

For the moving development branch:

```powershell
iex (irm https://raw.githubusercontent.com/kev0ps/agent-relay/main/scripts/install.ps1)
```

The Linux equivalent is:

```bash
curl -fsSL https://raw.githubusercontent.com/kev0ps/agent-relay/main/scripts/install.sh | bash
```

The installer downloads the source archive, installs `agent-relay` through
`uv`, adds the user-level tool directory to `PATH`, and asks whether to create
the local configuration. It starts with an empty Agent allowlist and never
prints token values.

For a release, host the script and source from the same immutable tag. The
installer accepts an explicit source reference when needed:

```powershell
$env:AGENT_RELAY_REF = "v0.1.0"
$env:AGENT_RELAY_REF_KIND = "tags"
iex (irm https://raw.githubusercontent.com/kev0ps/agent-relay/v0.1.0/scripts/install.ps1)
```

Inspect a downloaded script before executing it when the source or tag is not
trusted:

```powershell
irm https://raw.githubusercontent.com/kev0ps/agent-relay/main/scripts/install.ps1 -OutFile .\install-agent-relay.ps1
Get-Content .\install-agent-relay.ps1
.\install-agent-relay.ps1
```

The one-line form executes remote PowerShell and should be reserved for a
reviewed, HTTPS-hosted, versioned script. GitHub hosting is convenient, but a
mutable `main` URL is not a release or integrity pin.

## Installer behavior

The script:

- uses WinGet to install `uv` when it is missing, with an official uv installer
  fallback when WinGet is unavailable;
- installs or verifies managed Python 3.13.5 through `uv`;
- downloads the Agent Relay source archive without requiring Git;
- installs the runtime and its Python dependencies for the current user through
  `uv tool install`;
- leaves an existing `%USERPROFILE%\.agent-relay` configuration untouched;
- creates a new local Server and Agent configuration only when requested;
- creates no enabled Agent tools by default.

Set `AGENT_RELAY_SETUP=skip` for installation only, or
`AGENT_RELAY_SETUP=local` to avoid the setup prompt. `AGENT_RELAY_REF` and
`AGENT_RELAY_REF_KIND` select a branch or tag; `heads/main` is the default and
is intended for development until a release process exists. Override the
managed Python with `AGENT_RELAY_PYTHON_VERSION` when necessary.

The CI-only `AGENT_RELAY_SYNC_ROOT` and `AGENT_RELAY_SYNC_PROFILE` variables
also let the same bootstrapper install the locked development/test profile.
They are not set by the public one-line installer, so end users do not receive
pytest, Browser, or Computer Use test dependencies by default.

The Linux installer uses the same variables:

```bash
export AGENT_RELAY_REF=v0.1.0
export AGENT_RELAY_REF_KIND=tags
curl -fsSL https://raw.githubusercontent.com/kev0ps/agent-relay/v0.1.0/scripts/install.sh | bash
```

After installation, run these commands in separate PowerShell windows:

```powershell
agent-relay server
agent-relay agent
```

## Optional capabilities and limits

Browser requires the browser dependency and Chromium installation. Windows
Computer Use remains experimental and is not installed by this bootstrapper.
Windows CUA remains experimental and is not part of the one-line installation.
Do not expose a plaintext `ws://` deployment to the public Internet; use a
trusted firewalled LAN or an externally terminated WSS endpoint.
