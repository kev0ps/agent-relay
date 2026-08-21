"""Strict public command-line interface for Agent Relay."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from . import agent, config, server, uninstall
from .catalog import CatalogError, CatalogSnapshot, discover_local_catalog


def _extract_config(argv: list[str]) -> tuple[Path, list[str]]:
    path = config.DEFAULT_CONFIG_PATH
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--config":
            if index + 1 >= len(argv):
                print("agent-relay: error: --config requires a path", file=sys.stderr)
                raise SystemExit(2)
            path = Path(argv[index + 1]).expanduser()
            index += 2
            continue
        if item.startswith("--config="):
            value = item.partition("=")[2]
            if not value:
                print("agent-relay: error: --config requires a path", file=sys.stderr)
                raise SystemExit(2)
            path = Path(value).expanduser()
            index += 1
            continue
        remaining.append(item)
        index += 1
    return path, remaining


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-relay", add_help=False, description="Agent Relay"
    )
    parser.add_argument(
        "--config", metavar="PATH", help="use a specific YAML configuration file"
    )
    parser.add_argument("--help", action="help", help="show this help message and exit")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {config.PUBLIC_VERSION}",
        help="show the program version and exit",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    config_parser = commands.add_parser(
        "config", add_help=False, help="manage Agent Relay configuration"
    )
    config_parser.set_defaults(handler=_run_config)
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    init_parser = config_commands.add_parser("init", add_help=False)
    init_parser.add_argument("scope", choices=("server", "agent"))
    init_parser.add_argument("--force", action="store_true")
    token_source = init_parser.add_mutually_exclusive_group()
    token_source.add_argument("--stdin", action="store_true", help="read an Agent token from stdin")
    token_source.add_argument(
        "--from-server",
        action="store_true",
        help="reuse the effective Server Agent token",
    )
    init_parser.add_argument("--tools", help="comma-separated Agent tool names")
    init_parser.add_argument("--no-tools", action="store_true")
    init_parser.add_argument(
        "--cua-access", choices=("none", "standard", "full")
    )
    init_parser.add_argument(
        "--yes", action="store_true", help="confirm Full CUA access"
    )

    get_parser = config_commands.add_parser("get", add_help=False)
    get_parser.add_argument("scope", choices=("server", "agent"))

    set_parser = config_commands.add_parser("set", add_help=False)
    set_parser.add_argument("scope", choices=("server", "agent"))
    set_parser.add_argument("key")
    set_parser.add_argument("value", nargs="?")
    secret_group = set_parser.add_mutually_exclusive_group()
    secret_group.add_argument("--prompt", action="store_true")
    secret_group.add_argument("--stdin", action="store_true")
    secret_group.add_argument("--file", type=Path)

    unset_parser = config_commands.add_parser("unset", add_help=False)
    unset_parser.add_argument("scope", choices=("server", "agent"))
    unset_parser.add_argument("key")

    validate_parser = config_commands.add_parser("validate", add_help=False)
    validate_parser.add_argument("scope", choices=("server", "agent"))

    tools_parser = commands.add_parser(
        "tools", add_help=False, help="list and update Agent tool access"
    )
    tools_parser.set_defaults(handler=_run_tools)
    tool_commands = tools_parser.add_subparsers(dest="tool_command", required=True)
    list_parser = tool_commands.add_parser("list", add_help=False)
    list_parser.add_argument("--all", action="store_true")
    for action in ("enable", "disable"):
        tool_parser = tool_commands.add_parser(action, add_help=False)
        tool_parser.add_argument("tool")
    cua_access_parser = tool_commands.add_parser("cua-access", add_help=False)
    cua_access_parser.add_argument("level", choices=("none", "standard", "full"))
    cua_access_parser.add_argument("--yes", action="store_true")

    doctor_parser = commands.add_parser(
        "doctor", add_help=False, help="run the offline combined configuration audit"
    )
    doctor_parser.set_defaults(handler=_run_doctor)
    onboard_parser = commands.add_parser(
        "onboard", add_help=False, help="run guided Server/Agent first-run setup"
    )
    onboard_parser.set_defaults(handler=_run_onboard)
    onboard_parser.add_argument("--role", choices=("local", "server", "agent"))
    onboard_parser.add_argument("--non-interactive", action="store_true")
    onboard_parser.add_argument("--force", action="store_true")
    onboard_parser.add_argument("--host")
    onboard_parser.add_argument("--port")
    onboard_parser.add_argument(
        "--topology", choices=("local", "lan", "remote")
    )
    onboard_parser.add_argument("--relay-url")
    onboard_parser.add_argument("--workspace")
    onboard_tools = onboard_parser.add_mutually_exclusive_group()
    onboard_tools.add_argument("--tools")
    onboard_tools.add_argument("--no-tools", action="store_true")
    onboard_parser.add_argument(
        "--cua-access", choices=("none", "standard", "full")
    )
    onboard_parser.add_argument(
        "--yes", action="store_true", help="confirm Full CUA access"
    )
    onboard_tokens = onboard_parser.add_mutually_exclusive_group()
    onboard_tokens.add_argument("--token-file", type=Path)
    onboard_tokens.add_argument("--token-stdin", action="store_true")
    onboard_check = onboard_parser.add_mutually_exclusive_group()
    onboard_check.add_argument("--check", dest="check", action="store_true")
    onboard_check.add_argument("--no-check", dest="check", action="store_false")
    onboard_parser.set_defaults(check=None)
    server_parser = commands.add_parser(
        "server", add_help=False, help="start the Relay Server"
    )
    server_parser.set_defaults(handler=_run_server)
    agent_parser = commands.add_parser(
        "agent", add_help=False, help="start the outbound Agent"
    )
    agent_parser.set_defaults(handler=_run_agent)
    uninstall_parser = commands.add_parser(
        "uninstall", add_help=False, help="remove the uv-managed Agent Relay command"
    )
    uninstall_parser.set_defaults(handler=_run_uninstall)
    uninstall_parser.add_argument(
        "--purge",
        action="store_true",
        help="also remove the default configuration, .env, and workspace",
    )
    uninstall_parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm a requested data purge without prompting",
    )
    return parser


def _render_validation(report: config.ValidationReport) -> str:
    lines = [report.scope.capitalize()]
    lines.extend(f"[{issue.level}] {issue.message}" for issue in report.issues)
    lines.append("result=valid" if report.valid else "result=invalid")
    return "\n".join(lines)


def _render_section(path: Path, scope: Literal["server", "agent"]) -> str:
    return yaml.safe_dump(
        config.redact_for_output(config.get_section(path, scope)),
        sort_keys=False,
        allow_unicode=True,
    )


def _render_tools(
    path: Path,
    *,
    catalog: CatalogSnapshot | None,
    show_all: bool,
) -> str:
    statuses = config.tool_statuses(path, catalog=catalog)
    rows = ["Tool\tSource\tStatus\tRisk\tDescription"]
    if catalog is not None and not show_all:
        statuses = [
            (spec, status)
            for spec, status in statuses
            if not config.is_cua_public_name(spec.name)
        ]
    rows.extend(
        f"{spec.name}\t{spec.source}\t{status}\t{spec.risk}\t{spec.description}"
        for spec, status in statuses
    )
    if catalog is not None and not show_all:
        summary = config.cua_tool_summary(path, catalog=catalog)
        rows.append(
            "CUA\tcua\t"
            f"{summary.access}\tinteraction\t"
            f"{summary.enabled} enabled, {summary.available} available, "
            f"{summary.blocked} blocked, {len(summary.new_names)} new tools not selected"
        )
        if summary.new_names:
            rows.append(
                "CUA new\tcua\tdisabled\tinteraction\t"
                + ", ".join(summary.new_names)
            )
    return "\n".join(rows)


def _render_doctor(
    path: Path,
    *,
    catalog: CatalogSnapshot | None,
) -> tuple[str, bool]:
    reports = (
        config.validate_document(path, "server", require=False),
        config.validate_document(path, "agent", require=False, catalog=catalog),
    )
    lines = ["Agent Relay doctor"]
    for report in reports:
        lines.extend(("", report.scope.capitalize()))
        lines.extend(f"[{issue.level}] {issue.message}" for issue in report.issues)
    lines.extend(
        (
            "",
            f"Summary: {sum(len(report.errors) for report in reports)} error(s), "
            f"{sum(len(report.warnings) for report in reports)} warning(s)",
        )
    )
    return "\n".join(lines), all(report.valid for report in reports)


def _selected_tool_indexes(answer: str, count: int) -> list[int]:
    try:
        indexes = {int(item.strip()) for item in answer.split(",") if item.strip()}
    except ValueError as exc:
        raise config.ConfigError(
            "tool selection must be a comma-separated list of numbers"
        ) from exc
    if any(index < 1 or index > count for index in indexes):
        raise config.ConfigError("tool selection contains an invalid number")
    return sorted(indexes)


def _select_tools_interactively(
    path: Path,
    *,
    catalog: CatalogSnapshot | None,
) -> list[str]:
    """Render the interactive selector; config validation remains deterministic."""
    del path
    print("Select Agent tools (comma-separated numbers; empty enables none):")
    if catalog is not None:
        entries = [
            entry
            for entry in catalog.entries
            if not config.is_cua_public_name(entry.public_name)
        ]
        for index, entry in enumerate(entries, start=1):
            print(
                f"  {index}. {entry.public_name} [{entry.risk}] "
                f"{entry.status} - {entry.description}"
            )
        if not entries or not sys.stdin.isatty():
            return []
        try:
            answer = input("Tools: ").strip()
        except (EOFError, OSError) as exc:
            raise config.ConfigError("could not read tool selection") from exc
        if not answer:
            return []
        selected = [entry.public_name for entry in (entries[index - 1] for index in _selected_tool_indexes(answer, len(entries)))]
        try:
            catalog.validate_allowlist(selected)
        except CatalogError as exc:
            raise config.ConfigError(str(exc)) from None
        return selected

    specs = [spec for spec in config.TOOL_SPECS if spec.source != "server"]
    for index, spec in enumerate(specs, start=1):
        print(f"  {index}. {spec.name} - {spec.description}")
    if not specs or not sys.stdin.isatty():
        return []
    try:
        answer = input("Tools: ").strip()
    except (EOFError, OSError) as exc:
        raise config.ConfigError("could not read tool selection") from exc
    if not answer:
        return []
    return [specs[index - 1].name for index in _selected_tool_indexes(answer, len(specs))]


def _confirm_cua_access(
    level: str,
    catalog: CatalogSnapshot | None,
    *,
    assume_yes: bool,
) -> bool:
    """Validate a requested profile and obtain the one Full-access confirmation."""
    names = config.validate_cua_profile(level, catalog)
    if assume_yes and level != "full":
        raise config.ConfigError("--yes is only valid with --cua-access full")
    if level != "full" or assume_yes:
        return True
    if not sys.stdin.isatty():
        raise config.ConfigError("full CUA access requires --yes when stdin is non-interactive")
    entries = {entry.public_name: entry for entry in (catalog.entries if catalog else ())}
    sensitive = sorted(
        {
            entry.risk
            for name in names
            if (entry := entries.get(name)) is not None
            and entry.risk in {"destructive", "admin"}
        }
    )
    print("Full CUA access enables all non-blocked desktop and browser tools.")
    if sensitive:
        print(f"Sensitive categories: {', '.join(sensitive)}.")
    try:
        answer = input("Enable Full CUA access? [y/N] ")
    except (EOFError, OSError) as exc:
        raise config.ConfigError("could not read the Full CUA confirmation") from exc
    return answer.strip().lower() in {"y", "yes"}


def _agent_init_token(args: argparse.Namespace, path: Path) -> str | None:
    if args.stdin:
        try:
            return sys.stdin.readline().strip()
        except OSError as exc:
            raise config.ConfigError("secret cannot be read from stdin") from exc
    if args.from_server:
        return config.read_server_agent_token(path)
    if _section_exists(path, "agent") and (
        "RELAY_AGENT_TOKEN" in os.environ
        or config.has_token_source(path, "RELAY_AGENT_TOKEN")
    ):
        return None
    try:
        return getpass.getpass("Agent token: ").strip()
    except (EOFError, OSError) as exc:
        raise config.ConfigError("agent token cannot be read") from exc


def _secret_from_args(args: argparse.Namespace) -> str:
    if args.prompt:
        return getpass.getpass("Secret: ").strip()
    if args.stdin:
        try:
            return sys.stdin.readline().strip()
        except OSError as exc:
            raise config.ConfigError("secret cannot be read from stdin") from exc
    if args.file is not None:
        return config.read_private_secret(args.file)
    raise config.ConfigError("secret values require --prompt, --stdin, or --file")


def _init_tools(
    args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> list[str] | None:
    cua_access = getattr(args, "cua_access", None)
    assume_yes = bool(getattr(args, "yes", False))
    if assume_yes and cua_access != "full":
        raise config.ConfigError("--yes is only valid with --cua-access full")
    if args.tools is not None and args.no_tools:
        raise config.ConfigError("--tools and --no-tools are mutually exclusive")
    if args.no_tools and cua_access is not None:
        raise config.ConfigError("--no-tools is exclusive with --cua-access")
    if args.tools is not None:
        selected = [item.strip() for item in args.tools.split(",") if item.strip()]
        if cua_access is not None and any(
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
                item
                for item in selected
                if item not in config.PUBLIC_TOOLS - {config.SERVER_LOCAL_TOOL}
            ]
            if invalid:
                raise config.ConfigError(f"unknown Agent tool: {invalid[0]}")
        return selected
    if args.no_tools:
        return []
    if args.scope == "agent":
        try:
            config.get_section(path, "agent")
        except config.ConfigError:
            pass
        else:
            # Re-initialization is intentionally idempotent and preserves the
            # persisted identity and allowlist, including from a TTY.
            return None
    if args.scope == "agent" and sys.stdin.isatty():
        return _select_tools_interactively(path, catalog=catalog)
    if args.scope == "agent":
        return []
    return None


def _run_config(
    args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    if args.config_command == "init":
        if args.scope == "server" and (
            args.stdin
            or args.from_server
            or args.tools is not None
            or args.no_tools
            or args.cua_access is not None
            or args.yes
        ):
            raise config.ConfigError("server init does not accept Agent-only options")
        tools = _init_tools(args, path, catalog=catalog) if args.scope == "agent" else None
        if (
            args.scope == "agent"
            and args.cua_access is not None
            and not _confirm_cua_access(
                args.cua_access,
                catalog,
                assume_yes=args.yes,
            )
        ):
            print("CUA access update cancelled")
            return 0
        token = _agent_init_token(args, path) if args.scope == "agent" else None
        config.init_config(
            path,
            args.scope,
            force=args.force,
            token=token,
            tools=tools,
            catalog=catalog,
            cua_access=args.cua_access,
        )
        print(f"initialized {args.scope} configuration: {path}")
        if args.scope == "agent":
            allowlist = config.get_section(path, "agent")["tools"]["allowlist"]
            print(f"enabled tools: {', '.join(allowlist) if allowlist else 'none'}")
        return 0
    if args.config_command == "get":
        print(_render_section(path, args.scope), end="")
        return 0
    if args.config_command == "set":
        secret_key = args.key in {"mcp_token", "agent_token"}
        if secret_key:
            if args.value is not None:
                raise config.ConfigError("secret values must not be passed as command arguments")
            config.set_secret(path, args.scope, args.key, _secret_from_args(args))
        else:
            if args.value is None or args.prompt or args.stdin or args.file is not None:
                raise config.ConfigError("a scalar value is required for this setting")
            config.set_value(path, args.scope, args.key, args.value, catalog=catalog)
        print(f"updated {args.scope}.{args.key}")
        return 0
    if args.config_command == "unset":
        config.unset_value(path, args.scope, args.key)
        print(f"reset {args.scope}.{args.key}")
        return 0
    report = config.validate_document(path, args.scope, catalog=catalog)
    print(_render_validation(report))
    return 0 if report.valid else 1


def _run_tools(
    args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    if args.tool_command == "list":
        print(_render_tools(path, catalog=catalog, show_all=args.all))
        return 0
    if args.tool_command == "cua-access":
        if not _confirm_cua_access(args.level, catalog, assume_yes=args.yes):
            print("CUA access update cancelled")
            return 0
        config.update_cua_access(path, args.level, catalog=catalog)
        print(f"CUA access: {args.level}")
        return 0
    config.update_tool(
        path,
        args.tool,
        enabled=args.tool_command == "enable",
        catalog=catalog,
    )
    print(f"{'enabled' if args.tool_command == 'enable' else 'disabled'} {args.tool}")
    return 0


def _run_doctor(
    _args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    output, valid = _render_doctor(path, catalog=catalog)
    print(output)
    return 0 if valid else 1


def _run_onboard(
    args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    return _run_onboarding(
        path,
        OnboardingOptions.from_namespace(args),
        catalog=catalog,
    )


def _run_server(
    _args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    config.load_server_runtime(path)
    server.main(["--config", str(path)])
    return 0


def _run_agent(
    _args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    if _agent_environment_is_available(path):
        agent.main([], catalog=catalog)
        return 0
    config.load_agent_settings(path, catalog=catalog)
    if catalog is None:
        agent.main(["--config", str(path)])
    else:
        agent.main(["--config", str(path)], catalog=catalog)
    return 0


def _discover_local_catalog(path: Path) -> CatalogSnapshot:
    env, allowlist = config.catalog_environment(path)
    return discover_local_catalog(env=env, allowlist=allowlist)


def _catalog_required(args: argparse.Namespace) -> bool:
    if args.command in {"tools", "doctor", "agent"}:
        return True
    if args.command == "onboard":
        return args.role != "server"
    return args.command == "config" and (
        args.config_command == "validate"
        or (args.config_command == "init" and args.scope == "agent")
        or (
            args.config_command == "set"
            and args.scope == "agent"
            and args.key == "tools.allowlist"
        )
    )


def _confirm_uninstall_purge(data_dir: Path, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise config.ConfigError("--purge requires --yes when stdin is not interactive")
    try:
        answer = input(
            f"Remove Agent Relay configuration, .env, and workspace at {data_dir}? [y/N] "
        )
    except (EOFError, OSError) as exc:
        raise config.ConfigError("could not read the purge confirmation") from exc
    return answer.strip().lower() in {"y", "yes"}


def _run_uninstall(
    args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    if args.yes and not args.purge:
        raise config.ConfigError("--yes is only valid with --purge")

    default_config = config.DEFAULT_CONFIG_PATH
    data_dir = default_config.parent
    if args.purge:
        # Validate before invoking uv so an unsafe target cannot result in a
        # partially completed uninstall.
        uninstall.validate_purge_target(data_dir)

    if path != default_config:
        print(f"preserving custom configuration and referenced data: {path}")

    if args.purge and not _confirm_uninstall_purge(data_dir, assume_yes=args.yes):
        print("uninstall cancelled")
        return 0

    uninstall_log = uninstall.uninstall_tool()
    if uninstall_log is None:
        print("uninstalled agent-relay")
    else:
        print(
            "scheduled Agent Relay uninstallation after this process exits; "
            "it retries for up to 15 seconds if Windows still holds a lock"
        )
        print(f"uninstall status log: {uninstall_log}")

    if args.purge:
        if uninstall.purge_data(data_dir):
            print(f"removed Agent Relay data: {data_dir}")
        else:
            print(f"Agent Relay data directory did not exist: {data_dir}")
    else:
        print(f"preserved Agent Relay data: {data_dir}")
    return 0



OnboardingRole = Literal["local", "server", "agent"]
OnboardingTopology = Literal["local", "lan", "remote"]


@dataclass(frozen=True)
class OnboardingOptions:
    role: OnboardingRole | None = None
    non_interactive: bool = False
    force: bool = False
    host: str | None = None
    port: str | None = None
    topology: OnboardingTopology | None = None
    relay_url: str | None = None
    workspace: str | None = None
    tools: str | None = None
    no_tools: bool = False
    cua_access: Literal["none", "standard", "full"] | None = None
    yes: bool = False
    token_file: Path | None = None
    token_stdin: bool = False
    check: bool | None = None

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> OnboardingOptions:
        return cls(
            role=args.role,
            non_interactive=args.non_interactive,
            force=args.force,
            host=args.host,
            port=args.port,
            topology=args.topology,
            relay_url=args.relay_url,
            workspace=args.workspace,
            tools=args.tools,
            no_tools=args.no_tools,
            cua_access=args.cua_access,
            yes=args.yes,
            token_file=args.token_file,
            token_stdin=args.token_stdin,
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


def _select_topology(
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    default: OnboardingTopology,
) -> OnboardingTopology:
    if options.topology is not None:
        return options.topology
    if prompter.non_interactive:
        return default
    print("Deployment topology:")
    print("  1. Local — Server and Agent on this machine")
    print("  2. LAN — devices on a trusted local network")
    print("  3. Remote — WSS through a reverse proxy or secure tunnel")
    choice = prompter.required("Choose a topology [1]: ", default="1")
    topologies = {"1": "local", "2": "lan", "3": "remote"}
    try:
        return topologies[choice]  # type: ignore[return-value]
    except KeyError as exc:
        raise config.ConfigError("deployment topology selection is invalid") from exc


def _topology_server_host(topology: OnboardingTopology) -> str:
    if topology == "local":
        return "127.0.0.1"
    return "0.0.0.0"


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
    default_topology: OnboardingTopology,
    topology: OnboardingTopology | None = None,
) -> tuple[str, int]:
    selected_topology = topology or _select_topology(
        options, prompter, default=default_topology
    )
    default_host = _topology_server_host(selected_topology)
    host = options.host or prompter.required(
        f"Server bind host [{default_host}]: ", default=default_host
    )
    raw_port = options.port or prompter.required("Server port [8000]: ", default="8000")
    port = _parse_port(raw_port)
    return host, port


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
    return _select_tools_interactively(path, catalog=catalog)


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
    print(_render_validation(report))
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
        host, port = _server_values(
            options, prompter, default_topology="local"
        )
        config.init_config(path, "server", force=options.force)
        config.set_value(path, "server", "host", host)
        config.set_value(path, "server", "port", str(port))
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
    topology: OnboardingTopology,
    catalog: CatalogSnapshot | None,
) -> int:
    agent_created = not _section_exists(path, "agent") or options.force
    prepared_tools: list[str] | None = None
    prepared_cua_access: Literal["none", "standard", "full"] | None = None
    if agent_created:
        prepared_tools = _selected_tools(path, options, catalog)
        prepared_cua_access = _cua_access_value(options, prompter)
        if not _confirm_cua_access(
            prepared_cua_access,
            catalog,
            assume_yes=options.yes,
        ):
            print("CUA access update cancelled")
            return 0
    elif options.cua_access is not None and not _confirm_cua_access(
        options.cua_access,
        catalog,
        assume_yes=options.yes,
    ):
        print("CUA access update cancelled")
        return 0

    server_created = not _section_exists(path, "server") or options.force
    if server_created:
        host, port = _server_values(
            options,
            prompter,
            default_topology="local",
            topology=topology,
        )
        config.init_config(path, "server", force=options.force)
        config.set_value(path, "server", "host", host)
        config.set_value(path, "server", "port", str(port))
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
            catalog=catalog,
            cua_access=prepared_cua_access,
        )
    else:
        print("Existing Agent configuration found; leaving it unchanged.")
        if options.cua_access is not None:
            config.update_cua_access(
                path,
                options.cua_access,
                catalog=catalog,
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
            if not _confirm_cua_access(
                options.cua_access,
                catalog,
                assume_yes=options.yes,
            ):
                print("CUA access update cancelled")
                return 0
            config.update_cua_access(
                path,
                options.cua_access,
                catalog=catalog,
            )
        _report(path, "agent", catalog)
        _check_connection(path, options, catalog=catalog)
        print("Start the Agent with: agent-relay agent")
        return 0

    topology = _select_topology(options, prompter, default="remote")
    if options.relay_url is not None:
        relay_url = options.relay_url.strip()
    else:
        default_url = "ws://127.0.0.1:8000/ws/agent" if topology == "local" else None
        prompt = {
            "local": "Relay URL [ws://127.0.0.1:8000/ws/agent]: ",
            "lan": "Relay URL (ws://<LAN-IP>:<port>/ws/agent): ",
            "remote": "Public Relay URL (wss://.../ws/agent): ",
        }[topology]
        relay_url = prompter.required(prompt, default=default_url)
    config.validate_agent_transport(relay_url)
    if topology == "remote" and urlparse(relay_url).scheme != "wss":
        raise config.ConfigError("remote topology requires a wss:// relay URL")
    workspace = _workspace_value(options, prompter)
    token = _agent_token(options, prompter)
    tools = _selected_tools(path, options, catalog)
    cua_access = _cua_access_value(options, prompter)
    if not _confirm_cua_access(cua_access, catalog, assume_yes=options.yes):
        print("CUA access update cancelled")
        return 0
    config.init_config(
        path,
        "agent",
        force=options.force,
        token=token,
        tools=tools,
        relay_url=relay_url,
        workspace=workspace,
        catalog=catalog,
        cua_access=cua_access,
    )
    _report(path, "agent", catalog)
    print("Agent credential stored in the private .env and never printed.")
    _check_connection(path, options, catalog=catalog)
    print("Start the Agent with: agent-relay agent")
    return 0


def _run_onboarding(
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
    topology = _select_topology(options, prompter, default="local")
    if topology != "local":
        raise config.ConfigError("local role requires local topology")
    return _configure_local(
        path=config_path,
        options=options,
        prompter=prompter,
        topology=topology,
        catalog=catalog,
    )

def _agent_environment_is_available(path: Path) -> bool:
    """Allow a clean installed Agent to start from its runtime environment."""
    return (
        path == config.DEFAULT_CONFIG_PATH
        and not path.exists()
        and bool(os.environ.get("RELAY_URL"))
        and bool(os.environ.get("RELAY_AGENT_WORKSPACE"))
        and bool(os.environ.get("RELAY_AGENT_TOKEN"))
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    """Run the strict Agent Relay CLI and return a process-compatible status."""
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if not raw:
        parser.print_help()
        return 0
    path, command_argv = _extract_config(raw)
    if not command_argv:
        parser.print_help()
        return 0
    if "--help" in command_argv and command_argv != ["--help"]:
        parser.error("only the top-level --help option is supported")
    if "--version" in command_argv and command_argv != ["--version"]:
        parser.error("only the top-level --version option is supported")
    try:
        args = parser.parse_args(command_argv)
    except SystemExit as exc:
        if command_argv in (["--help"], ["--version"]) and exc.code == 0:
            return 0
        raise
    try:
        effective_catalog = catalog
        if effective_catalog is None and _catalog_required(args):
            effective_catalog = _discover_local_catalog(path)
        return args.handler(args, path, catalog=effective_catalog)
    except config.ConfigError as exc:
        print(f"agent-relay: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
