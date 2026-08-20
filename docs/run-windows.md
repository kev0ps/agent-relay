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
the local configuration. A newly initialized Agent starts with an empty
allowlist, and the installer never prints token values. The guided onboarding
command offers Local Server + Agent, Server-only, and remote Agent setup; use
`agent-relay onboard` after installation when you need one of those flows.

For a release, host the script and source from the same reviewed release tag.
Replace the placeholder only with a tag that actually exists:

```powershell
$releaseTag = "<RELEASE_TAG>"
$env:AGENT_RELAY_REF = $releaseTag
$env:AGENT_RELAY_REF_KIND = "tags"
iex (irm "https://raw.githubusercontent.com/kev0ps/agent-relay/$releaseTag/scripts/install.ps1")
```

A Git tag can be moved and is not a cryptographic integrity proof. Verify the
resolved commit independently and use a published hash or signature when the
release process provides one.

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
- installs or verifies managed Python 3.14.4 through `uv`;
- downloads the Agent Relay source archive without requiring Git;
- installs the runtime and its Python dependencies for the current user through
  `uv tool install`;
- preserves an existing Agent section and tool allowlists;
- creates a local Server configuration when requested and no configuration file
  exists, then adds an Agent section only when one is missing;
- enables no tools in a newly created Agent section; CUA access defaults to
  `none` and is selected with `--cua-access none|standard|full`.

Set `AGENT_RELAY_SETUP=local`, `server`, `agent`, or `skip` to select the
installer onboarding mode. The `agent` mode asks for a `local`, `lan`, or `remote`
topology and its Relay URL, then reads the masked Agent credential; use a
private token file or stdin only as a one-time import channel for non-interactive
setup; the value is copied into the adjacent `.env`.
That `.env` contains only `RELAY_AGENT_TOKEN` (or both canonical token keys for
a combined Server/Agent configuration); URL, workspace, and tool settings stay
in YAML or explicit process environment variables.
`AGENT_RELAY_REF` and
`AGENT_RELAY_REF_KIND` select a branch or tag; `heads/main` is the default and
is intended for development until a release process exists. Override the
managed Python with `AGENT_RELAY_PYTHON_VERSION` when necessary.

The CI-only `AGENT_RELAY_SYNC_ROOT` variable lets the same bootstrapper install
the locked development/test environment. It is not set by the public one-line
installer; the standard runtime already includes the CUA provider and its
bundled driver.

The Linux installer uses the same variables:

```bash
release_tag='<RELEASE_TAG>'
export AGENT_RELAY_REF="$release_tag"
export AGENT_RELAY_REF_KIND=tags
curl -fsSL "https://raw.githubusercontent.com/kev0ps/agent-relay/$release_tag/scripts/install.sh" | bash
```

## Uninstall

For a user-scoped bootstrapper installation, remove the Agent Relay command and
its uv tool environment while keeping `%USERPROFILE%\.agent-relay`:

```powershell
agent-relay uninstall
```

On Windows, the command schedules uv removal after the current executable has
exited and retries every 500 ms for up to 15 seconds. Stop other
`agent-relay server` or `agent-relay agent` processes first; if the tool remains
installed, the status log records every attempt and gives the manual command.
The uninstall never terminates unrelated processes automatically.

To also remove the default configuration, private `.env`, and workspace, use
an explicit purge:

```powershell
agent-relay uninstall --purge
```

The purge asks for confirmation; pass `--yes` for non-interactive automation.
Custom configurations and data outside `%USERPROFILE%\.agent-relay` are
preserved. The command does not remove `uv`, managed Python, or the shared uv
tool directory.

After installation, run these commands in separate PowerShell windows:

```powershell
agent-relay server
agent-relay agent
```

## CUA and limits

The one-line installation includes `cua-driver`, resolves its executable
automatically, and discovers the provider catalogue when the Agent starts.
Every descriptor is exposed as `relay_cua_<name>` and remains disabled until
explicitly enabled or included in the selected `standard`/`full` profile.
The native Windows CI gate uses preinstalled Google Chrome, the same synthetic
HTML fixture, shared browser scenario, and shared functional oracles as Linux.
It runs in the hosted runner's interactive session and fails closed in Session
0. This is evidence for the bounded synthetic browser path, not a claim that
arbitrary desktop applications or personal browser profiles are supported.
Browser descriptors use the same CUA path; there is no separate wrapper.
The Server defaults to the Local topology (`127.0.0.1:8000`) and the Agent uses
`ws://127.0.0.1:8000/ws/agent` on the same machine. For a trusted LAN, bind the
Server explicitly to a LAN address or `0.0.0.0` and use `ws://<LAN-IP>:8000`;
that transport is unencrypted and requires a firewall. For Remote, use
`wss://<public-host>/ws/agent` through an external TLS reverse proxy or secure
tunnel. Agent Relay does not terminate TLS or trust forwarded headers.
