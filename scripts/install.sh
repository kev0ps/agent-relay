#!/usr/bin/env bash

set -euo pipefail

repository="https://github.com/kev0ps/agent-relay"
source_ref="${AGENT_RELAY_REF:-main}"
source_ref_kind="${AGENT_RELAY_REF_KIND:-heads}"
python_version="${AGENT_RELAY_PYTHON_VERSION:-3.13.5}"

if [[ "$(uname -s)" != "Linux" ]]; then
    printf 'Agent Relay Linux installer requires Linux.\n' >&2
    exit 1
fi
if [[ "$source_ref_kind" != "heads" && "$source_ref_kind" != "tags" ]]; then
    printf "AGENT_RELAY_REF_KIND must be 'heads' or 'tags'.\n" >&2
    exit 1
fi
if [[ ! "$source_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
    printf 'AGENT_RELAY_REF contains unsupported characters.\n' >&2
    exit 1
fi
if [[ ! "$python_version" =~ ^[0-9]+(\.[0-9]+){1,2}$ ]]; then
    printf 'AGENT_RELAY_PYTHON_VERSION contains unsupported characters.\n' >&2
    exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
    printf 'curl is required. Install curl and rerun the installer.\n' >&2
    exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
    printf 'tar is required. Install tar and rerun the installer.\n' >&2
    exit 1
fi

temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/agent-relay-install.XXXXXXXX")"
cleanup() {
    if [[ -d "$temporary_root" ]]; then
        rm -rf -- "$temporary_root"
    fi
}
trap cleanup EXIT

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

if ! uv_path="$(find_uv)"; then
    printf 'uv not found; downloading the official uv installer...\n'
    curl -fsSL https://astral.sh/uv/install.sh -o "$temporary_root/uv-install.sh"
    sh "$temporary_root/uv-install.sh"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! uv_path="$(find_uv)"; then
        printf 'uv was installed but could not be found on PATH.\n' >&2
        exit 1
    fi
fi

printf 'Installing or verifying Python %s with uv...\n' "$python_version"
"$uv_path" python install "$python_version"
export UV_PYTHON="$python_version"

sync_root="${AGENT_RELAY_SYNC_ROOT:-}"
if [[ -n "$sync_root" ]]; then
    if [[ ! -d "$sync_root" || ! -f "$sync_root/pyproject.toml" || ! -f "$sync_root/uv.lock" ]]; then
        printf 'AGENT_RELAY_SYNC_ROOT is not a locked Agent Relay project: %s\n' "$sync_root" >&2
        exit 1
    fi
    sync_profile="${AGENT_RELAY_SYNC_PROFILE:-base}"
    case "$sync_profile" in
        base) sync_arguments=(sync --locked) ;;
        browser) sync_arguments=(sync --locked --extra browser) ;;
        computer) sync_arguments=(sync --locked --extra browser --extra computer) ;;
        *)
            printf "AGENT_RELAY_SYNC_PROFILE must be 'base', 'browser', or 'computer'.\n" >&2
            exit 1
            ;;
    esac
    printf 'Installing locked %s dependencies with uv...\n' "$sync_profile"
    (
        cd -- "$sync_root"
        "$uv_path" "${sync_arguments[@]}"
    )
fi

archive_path="$temporary_root/agent-relay.tar.gz"
expanded_root="$temporary_root/expanded"
project_root="${AGENT_RELAY_PROJECT_ROOT:-}"
if [[ -n "$project_root" ]]; then
    if [[ ! -d "$project_root" || ! -f "$project_root/pyproject.toml" ]]; then
        printf 'AGENT_RELAY_PROJECT_ROOT is not a valid Agent Relay project: %s\n' "$project_root" >&2
        exit 1
    fi
else
    archive_source="${AGENT_RELAY_ARCHIVE_SOURCE:-}"
    if [[ -n "$archive_source" && -f "$archive_source" ]]; then
        cp -- "$archive_source" "$archive_path"
    else
        archive_uri="https://codeload.github.com/kev0ps/agent-relay/tar.gz/refs/$source_ref_kind/$source_ref"
        printf 'Downloading Agent Relay (%s/%s)...\n' "$source_ref_kind" "$source_ref"
        curl -fsSL "$archive_uri" -o "$archive_path"
    fi
    mkdir -p "$expanded_root"
    tar -xzf "$archive_path" -C "$expanded_root"

    mapfile -t projects < <(find "$expanded_root" -type f -name pyproject.toml -print)
    if [[ "${#projects[@]}" -ne 1 ]]; then
        printf 'The downloaded Agent Relay archive did not contain exactly one project.\n' >&2
        exit 1
    fi
    project_root="$(dirname "${projects[0]}")"
fi

printf 'Installing the Agent Relay command for the current user...\n'
"$uv_path" tool install --force "$project_root"
tool_bin="$("$uv_path" tool dir --bin)"
if [[ ! -d "$tool_bin" ]]; then
    printf 'uv did not report a valid tool bin directory.\n' >&2
    exit 1
fi
export PATH="$tool_bin:$PATH"
if [[ "${AGENT_RELAY_SKIP_PATH_UPDATE:-0}" != "1" ]]; then
    if ! "$uv_path" tool update-shell >/dev/null 2>&1; then
        printf 'Warning: uv could not update the shell profile; add %s to PATH manually.\n' "$tool_bin" >&2
    fi
fi

agent_relay_command="$tool_bin/agent-relay"
if [[ ! -x "$agent_relay_command" ]]; then
    agent_relay_command="$(command -v agent-relay || true)"
fi
if [[ -z "$agent_relay_command" || ! -x "$agent_relay_command" ]]; then
    printf 'The agent-relay command was not found in %s.\n' "$tool_bin" >&2
    exit 1
fi

invoke_agent_relay() {
    "$agent_relay_command" "$@"
}

setup_mode="${AGENT_RELAY_SETUP:-prompt}"
case "$setup_mode" in
    prompt|local|skip) ;;
    *)
        printf "AGENT_RELAY_SETUP must be 'prompt', 'local', or 'skip'.\n" >&2
        exit 1
        ;;
esac

if [[ "$setup_mode" == "prompt" ]]; then
    if [[ -r /dev/tty ]]; then
        answer=''
        read -r -p 'Configure a local Server and Agent now? [Y/n] ' answer </dev/tty
        if [[ "$answer" =~ ^[Nn] ]]; then
            setup_mode='skip'
        else
            setup_mode='local'
        fi
    else
        setup_mode='skip'
    fi
fi

if [[ "$setup_mode" == "local" ]]; then
    config_path="$HOME/.agent-relay/config.yaml"
    if [[ ! -f "$config_path" ]]; then
        invoke_agent_relay config init server
        invoke_agent_relay config set server host 127.0.0.1
    else
        printf 'Existing Agent Relay configuration found; leaving it unchanged.\n'
    fi

    agent_token_path="$HOME/.agent-relay/secrets/server/agent_token"
    agent_config_marker="$HOME/.agent-relay/secrets/agent/agent_token"
    if [[ -f "$config_path" && ! -f "$agent_config_marker" ]]; then
        if [[ ! -f "$agent_token_path" ]]; then
            printf 'The Server Agent token file was not created.\n' >&2
            exit 1
        fi
        invoke_agent_relay config init agent --stdin --no-tools < "$agent_token_path"
    else
        printf 'Existing Agent secret found; leaving it unchanged.\n'
    fi
fi

printf '\nAgent Relay installed for the current user.\n'
printf 'Start the local deployment in two shell windows:\n'
printf '  agent-relay server\n'
printf '  agent-relay agent\n'
printf '\nFor a remote Server, set AGENT_RELAY_SETUP=skip and configure the Agent URL and token file separately.\n'
