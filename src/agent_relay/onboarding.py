"""Guided first-run configuration built on the canonical config primitives."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import config
from .catalog import CatalogError, CatalogSnapshot

OnboardingRole = Literal["local", "server", "agent"]
OnboardingPolicy = Literal["loopback", "lan", "secure"]


@dataclass(frozen=True)
class OnboardingOptions:
    role: OnboardingRole | None = None
    non_interactive: bool = False
    force: bool = False
    host: str | None = None
    port: str | None = None
    policy: OnboardingPolicy | None = None
    relay_url: str | None = None
    workspace: str | None = None
    tools: str | None = None
    no_tools: bool = False
    cua_access: Literal["none", "standard", "full"] | None = None
    yes: bool = False
    token_file: Path | None = None
    token_stdin: bool = False
    allow_insecure_ws: bool | None = None
    check: bool | None = None

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> OnboardingOptions:
        return cls(
            role=args.role,
            non_interactive=args.non_interactive,
            force=args.force,
            host=args.host,
            port=args.port,
            policy=args.policy,
            relay_url=args.relay_url,
            workspace=args.workspace,
            tools=args.tools,
            no_tools=args.no_tools,
            cua_access=args.cua_access,
            yes=args.yes,
            token_file=args.token_file,
            token_stdin=args.token_stdin,
            allow_insecure_ws=args.allow_insecure_ws,
            check=args.check,
        )


class _Prompter:
    def __init__(self, *, non_interactive: bool) -> None:
        self.non_interactive = non_interactive

    def required(self, prompt: str, *, default: str | None = None) -> str:
        if self.non_interactive:
            if default is not None:
                return default
            raise config.ConfigError(
                "non-interactive onboarding requires an explicit value"
            )
        try:
            answer = input(prompt)
        except (EOFError, OSError) as exc:
            raise config.ConfigError("onboarding cancelled") from exc
        answer = answer.strip()
        if answer:
            return answer
        if default is not None:
            return default
        raise config.ConfigError("onboarding cancelled")

    def optional_yes_no(self, prompt: str, *, default: bool = False) -> bool:
        if self.non_interactive:
            return default
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, OSError):
            return default
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        raise config.ConfigError("please answer yes or no")


def _section_exists(path: Path, scope: Literal["server", "agent"]) -> bool:
    try:
        config.get_section(path, scope)
    except config.ConfigError as exc:
        if "is not initialized" in str(exc) or "file does not exist" in str(exc):
            return False
        raise
    return True


def _select_role(options: OnboardingOptions, prompter: _Prompter) -> OnboardingRole:
    if options.role is not None:
        return options.role
    if options.non_interactive:
        raise config.ConfigError(
            "non-interactive onboarding requires --role local, --role server, or --role agent"
        )
    print("Agent Relay onboarding")
    print("  1. Local Server + Agent")
    print("  2. Server only")
    print("  3. Agent connected to a remote Server")
    choice = prompter.required("Choose a setup [1]: ", default="1")
    roles = {"1": "local", "2": "server", "3": "agent"}
    try:
        return roles[choice]  # type: ignore[return-value]
    except KeyError as exc:
        raise config.ConfigError("onboarding role selection is invalid") from exc


def _select_policy(
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    default: OnboardingPolicy,
) -> OnboardingPolicy:
    if options.policy is not None:
        return options.policy
    if prompter.non_interactive:
        return default
    print("Deployment policy:")
    print("  1. This machine only (loopback)")
    print("  2. Trusted LAN (plaintext WebSocket)")
    print("  3. TLS/reverse proxy (wss://)")
    choice = prompter.required("Choose a policy [1]: ", default="1")
    policies = {"1": "loopback", "2": "lan", "3": "secure"}
    try:
        return policies[choice]  # type: ignore[return-value]
    except KeyError as exc:
        raise config.ConfigError("deployment policy selection is invalid") from exc


def _policy_values(policy: OnboardingPolicy) -> tuple[str, bool]:
    if policy == "loopback":
        return "127.0.0.1", True
    if policy == "lan":
        return "0.0.0.0", True
    return "0.0.0.0", False


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise config.ConfigError("server port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise config.ConfigError("server port must be between 1 and 65535")
    return port


def _server_values(
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    default_policy: OnboardingPolicy,
) -> tuple[str, int, bool]:
    policy = _select_policy(options, prompter, default=default_policy)
    default_host, allow_insecure_ws = _policy_values(policy)
    host = options.host or prompter.required(
        f"Server bind host [{default_host}]: ", default=default_host
    )
    raw_port = options.port or prompter.required("Server port [8000]: ", default="8000")
    port = _parse_port(raw_port)
    return host, port, allow_insecure_ws


def _workspace_value(options: OnboardingOptions, prompter: _Prompter) -> str:
    return options.workspace or prompter.required(
        "Agent workspace [./workspace]: ", default="./workspace"
    )


def _selected_tools(
    path: Path,
    options: OnboardingOptions,
    catalog: CatalogSnapshot | None,
) -> list[str]:
    _validate_tool_options(options)
    if options.no_tools:
        return []
    if options.tools is not None:
        selected = [item.strip() for item in options.tools.split(",") if item.strip()]
        if options.cua_access is not None and any(
            config.is_cua_public_name(item) for item in selected
        ):
            raise config.ConfigError(
                "--tools cannot include relay_cua_* when --cua-access is used"
            )
        if catalog is not None:
            try:
                catalog.validate_allowlist(selected)
            except CatalogError as exc:
                raise config.ConfigError(str(exc)) from None
        else:
            invalid = [
                name
                for name in selected
                if name not in config.PUBLIC_TOOLS - {config.SERVER_LOCAL_TOOL}
            ]
            if invalid:
                raise config.ConfigError(f"unknown Agent tool: {invalid[0]}")
        return selected
    if options.non_interactive:
        return []
    return config.select_tools_interactively({}, path, catalog=catalog)


def _validate_tool_options(options: OnboardingOptions) -> None:
    """Reject ambiguous tool/profile combinations before touching YAML."""
    if options.tools is not None and options.no_tools:
        raise config.ConfigError("--tools and --no-tools are mutually exclusive")
    if options.no_tools and options.cua_access is not None:
        raise config.ConfigError("--no-tools is exclusive with --cua-access")
    if options.tools is not None and options.cua_access is not None:
        selected = [item.strip() for item in options.tools.split(",") if item.strip()]
        if any(config.is_cua_public_name(item) for item in selected):
            raise config.ConfigError(
                "--tools cannot include relay_cua_* when --cua-access is used"
            )
    if options.yes and options.cua_access != "full":
        raise config.ConfigError("--yes is only valid with --cua-access full")


def _cua_access_value(
    options: OnboardingOptions,
    prompter: _Prompter,
) -> Literal["none", "standard", "full"]:
    if options.yes and options.cua_access != "full":
        raise config.ConfigError("--yes is only valid with --cua-access full")
    if options.cua_access is not None:
        return options.cua_access
    if prompter.non_interactive:
        return "none"
    print("CUA access (desktop and browser):")
    print("  1. None (default)")
    print("  2. Standard (common interaction and browser tools)")
    print("  3. Full (all non-blocked CUA tools)")
    choice = prompter.required("Choose CUA access [1]: ", default="1")
    levels = {"1": "none", "2": "standard", "3": "full"}
    try:
        return levels[choice]  # type: ignore[return-value]
    except KeyError as exc:
        raise config.ConfigError("CUA access selection is invalid") from exc


def _agent_token(options: OnboardingOptions, prompter: _Prompter) -> str:
    if options.token_file is not None:
        return config.read_private_secret(options.token_file)
    if options.token_stdin:
        try:
            token = sys.stdin.readline().strip()
        except OSError as exc:
            raise config.ConfigError("secret cannot be read from stdin") from exc
        if not token:
            raise config.ConfigError("agent token cannot be empty")
        return token
    if prompter.non_interactive:
        raise config.ConfigError(
            "non-interactive Agent onboarding requires --token-file or --token-stdin"
        )
    try:
        token = getpass.getpass("Agent token (input hidden): ").strip()
    except (EOFError, OSError) as exc:
        raise config.ConfigError("onboarding cancelled") from exc
    if not token:
        raise config.ConfigError("agent token cannot be empty")
    return token


def _report(path: Path, scope: Literal["server", "agent"], catalog: CatalogSnapshot | None) -> None:
    report = config.validate_document(path, scope, catalog=catalog)
    print(config.render_validation(report))
    if not report.valid:
        raise config.ConfigError(f"invalid {scope} configuration")


def _configure_server(
    path: Path,
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    catalog: CatalogSnapshot | None,
) -> int:
    if _section_exists(path, "server") and not options.force:
        print("Existing Server configuration found; leaving it unchanged.")
        _report(path, "server", catalog)
    else:
        host, port, allow_insecure_ws = _server_values(
            options, prompter, default_policy="lan"
        )
        config.init_config(path, "server", force=options.force)
        config.set_value(path, "server", "host", host)
        config.set_value(path, "server", "port", str(port))
        config.set_value(
            path,
            "server",
            "allow_insecure_ws",
            "true" if allow_insecure_ws else "false",
        )
        _report(path, "server", catalog)
    print("MCP and Agent credentials are distinct and were kept in the private .env.")
    print("Give an Agent administrator the Agent secret through a secure channel; do not paste it into a command or log.")
    print("Start the Server with: agent-relay server")
    return 0


def _configure_local(
    path: Path,
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    catalog: CatalogSnapshot | None,
) -> int:
    agent_created = not _section_exists(path, "agent") or options.force
    prepared_tools: list[str] | None = None
    prepared_cua_access: Literal["none", "standard", "full"] | None = None
    cua_confirmation_done = False
    if agent_created:
        prepared_tools = _selected_tools(path, options, catalog)
        prepared_cua_access = _cua_access_value(options, prompter)
        config.confirm_cua_access(
            prepared_cua_access,
            catalog,
            assume_yes=options.yes,
        )
        cua_confirmation_done = prepared_cua_access == "full"

    server_created = not _section_exists(path, "server") or options.force
    if server_created:
        host, port, allow_insecure_ws = _server_values(
            options,
            prompter,
            default_policy="loopback",
        )
        config.init_config(path, "server", force=options.force)
        config.set_value(path, "server", "host", host)
        config.set_value(path, "server", "port", str(port))
        config.set_value(
            path,
            "server",
            "allow_insecure_ws",
            "true" if allow_insecure_ws else "false",
        )
    else:
        print("Existing Server configuration found; leaving it unchanged.")

    if agent_created:
        server = config.get_section(path, "server")
        port = _parse_port(str(server.get("port", 8000)))
        token = config.read_server_agent_token(path)
        workspace = _workspace_value(options, prompter)
        config.init_config(
            path,
            "agent",
            force=options.force,
            token=token,
            tools=prepared_tools,
            relay_url=f"ws://127.0.0.1:{port}/ws/agent",
            workspace=workspace,
            allow_insecure_ws=True,
            catalog=catalog,
            cua_access=prepared_cua_access,
            assume_yes=options.yes or cua_confirmation_done,
        )
    else:
        print("Existing Agent configuration found; leaving it unchanged.")
        if options.cua_access is not None:
            config.update_cua_access(
                path,
                options.cua_access,
                catalog=catalog,
                assume_yes=options.yes,
            )

    _report(path, "server", catalog)
    _report(path, "agent", catalog)
    print("MCP and Agent credentials are distinct; neither credential is printed.")
    print("Start the local deployment in two terminals:")
    print("  agent-relay server")
    print("  agent-relay agent")
    return 0


def _check_connection(
    path: Path,
    options: OnboardingOptions,
    *,
    catalog: CatalogSnapshot | None,
) -> None:
    if options.check is False:
        print("Connection check not run; validation above is offline only.")
        return
    if options.check is None and options.non_interactive:
        print("Connection check not run; validation above is offline only.")
        return
    if options.check is None:
        should_check = _Prompter(non_interactive=False).optional_yes_no(
            "Run an authenticated connection check now? [y/N] ", default=False
        )
        if not should_check:
            print("Connection check not run; validation above is offline only.")
            return
    from . import agent

    settings = config.load_agent_settings(path, catalog=catalog)
    target = agent.safe_server_target(settings.server_url)
    try:
        asyncio.run(agent.check_connection(settings))
    except Exception:
        print(
            f"Connection check failed for {target}; configuration is valid, but the Server was not authenticated."
        )
    else:
        print(f"Connection check succeeded for {target}: authenticated registration confirmed.")


def _configure_agent(
    path: Path,
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    catalog: CatalogSnapshot | None,
) -> int:
    if _section_exists(path, "agent") and not options.force:
        print("Existing Agent configuration found; leaving it unchanged.")
        if options.cua_access is not None:
            config.update_cua_access(
                path,
                options.cua_access,
                catalog=catalog,
                assume_yes=options.yes,
            )
        _report(path, "agent", catalog)
        _check_connection(path, options, catalog=catalog)
        print("Start the Agent with: agent-relay agent")
        return 0

    if options.relay_url is not None:
        relay_url = options.relay_url.strip()
    else:
        relay_url = prompter.required("Relay URL (ws:// or wss://): ")
    if options.allow_insecure_ws is None:
        allow_insecure_ws = prompter.optional_yes_no(
            "Allow non-loopback plaintext ws:// for a trusted LAN? [y/N] ",
            default=False,
        )
    else:
        allow_insecure_ws = options.allow_insecure_ws
    config.validate_agent_transport(
        relay_url,
        allow_insecure_ws=allow_insecure_ws,
    )
    workspace = _workspace_value(options, prompter)
    token = _agent_token(options, prompter)
    tools = _selected_tools(path, options, catalog)
    cua_access = _cua_access_value(options, prompter)
    config.init_config(
        path,
        "agent",
        force=options.force,
        token=token,
        tools=tools,
        relay_url=relay_url,
        workspace=workspace,
        allow_insecure_ws=allow_insecure_ws,
        catalog=catalog,
        cua_access=cua_access,
        assume_yes=options.yes,
    )
    _report(path, "agent", catalog)
    print("Agent credential stored in the private .env and never printed.")
    _check_connection(path, options, catalog=catalog)
    print("Start the Agent with: agent-relay agent")
    return 0


def run(
    path: str | Path,
    options: OnboardingOptions,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    """Run one guided onboarding flow and return a CLI-compatible status."""
    _validate_tool_options(options)
    config_path = Path(path).expanduser()
    prompter = _Prompter(non_interactive=options.non_interactive)
    role = _select_role(options, prompter)
    if role == "server":
        if (
            options.tools is not None
            or options.no_tools
            or options.cua_access is not None
            or options.yes
        ):
            raise config.ConfigError("server onboarding does not accept Agent-only options")
        return _configure_server(path=config_path, options=options, prompter=prompter, catalog=catalog)
    if role == "agent":
        return _configure_agent(path=config_path, options=options, prompter=prompter, catalog=catalog)
    return _configure_local(path=config_path, options=options, prompter=prompter, catalog=catalog)
