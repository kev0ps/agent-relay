"""Strict public command-line interface for Agent Relay."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path

from . import agent, config, server
from .catalog import CatalogError, CatalogSnapshot

_HELP = """usage: agent-relay [--config PATH] <command>\n\nAgent Relay\n\nCommands:\n  --help                         show this help and exit\n  --version                      show the program version and exit\n  config init server             create Server YAML and secret files\n  config init agent              create Agent YAML and secret file\n  config get server              print the Server YAML section\n  config get agent               print the Agent YAML section\n  config set server KEY VALUE    update a Server setting\n  config set agent KEY VALUE     update an Agent setting\n  config unset server KEY        restore a Server setting default\n  config unset agent KEY         restore an Agent setting default\n  config validate server         validate the Server configuration\n  config validate agent          validate the Agent configuration\n  tools list                     list the complete public tool inventory\n  tools enable TOOL              enable an Agent tool\n  tools disable TOOL              disable an Agent tool\n  doctor                         run the offline combined configuration audit\n  server                        start the Relay Server\n  agent                         start the outbound Agent\n\nGlobal options:\n  --config PATH                  use a specific YAML configuration file\n\nSecret values must be provided with --prompt, --stdin, or --file.\n"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


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


def _parser() -> _Parser:
    parser = _Parser(prog="agent-relay", add_help=False, description="Agent Relay")
    commands = parser.add_subparsers(dest="command", required=True)

    config_parser = commands.add_parser("config", add_help=False)
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    init_parser = config_commands.add_parser("init", add_help=False)
    init_parser.add_argument("scope", choices=("server", "agent"))
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--stdin", action="store_true", help="read an Agent token from stdin")
    init_parser.add_argument("--tools", help="comma-separated Agent tool names")
    init_parser.add_argument("--no-tools", action="store_true")

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

    tools_parser = commands.add_parser("tools", add_help=False)
    tool_commands = tools_parser.add_subparsers(dest="tool_command", required=True)
    tool_commands.add_parser("list", add_help=False)
    for action in ("enable", "disable"):
        tool_parser = tool_commands.add_parser(action, add_help=False)
        tool_parser.add_argument("tool")

    commands.add_parser("doctor", add_help=False)
    commands.add_parser("server", add_help=False)
    commands.add_parser("agent", add_help=False)
    return parser


def _secret_from_args(args: argparse.Namespace) -> str:
    if args.prompt:
        return getpass.getpass("Secret: ").strip()
    if args.stdin:
        try:
            return sys.stdin.readline().strip()
        except OSError as exc:
            raise config.ConfigError("secret cannot be read from stdin") from exc
    if args.file is not None:
        try:
            content = args.file.read_text(encoding="utf-8")
        except OSError as exc:
            raise config.ConfigError("secret source file could not be read") from exc
        return content.strip()
    raise config.ConfigError("secret values require --prompt, --stdin, or --file")


def _init_tools(
    args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> list[str] | None:
    if args.tools is not None and args.no_tools:
        raise config.ConfigError("--tools and --no-tools are mutually exclusive")
    if args.tools is not None:
        selected = [item.strip() for item in args.tools.split(",") if item.strip()]
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
        return config.select_tools_interactively({}, path, catalog=catalog)
    if args.scope == "agent":
        raise config.ConfigError(
            "non-interactive Agent init requires --tools or --no-tools"
        )
    return None


def _run_config(
    args: argparse.Namespace,
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    if args.config_command == "init":
        if args.scope == "server" and (args.stdin or args.tools is not None or args.no_tools):
            raise config.ConfigError("server init does not accept Agent-only options")
        tools = _init_tools(args, path, catalog=catalog) if args.scope == "agent" else None
        token = None
        if args.scope == "agent" and args.stdin:
            try:
                token = sys.stdin.readline().strip()
            except OSError as exc:
                raise config.ConfigError("secret cannot be read from stdin") from exc
        config.init_config(
            path,
            args.scope,
            force=args.force,
            token=token,
            tools=tools,
            use_stdin=False,
            catalog=catalog,
        )
        print(f"initialized {args.scope} configuration: {path}")
        if args.scope == "agent" and tools is not None:
            print(f"enabled tools: {', '.join(tools) if tools else 'none'}")
        return 0
    if args.config_command == "get":
        print(config.render_section(path, args.scope), end="")
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
            if args.scope == "agent" and args.key in {"browser.origin_policy", "browser.policy"} and args.value.strip().lower() in {"any", "'any'", '"any"'}:
                print("WARNING: browser origin policy any allows all supported HTTP(S) origins", file=sys.stderr)
        print(f"updated {args.scope}.{args.key}")
        return 0
    if args.config_command == "unset":
        config.unset_value(path, args.scope, args.key)
        print(f"reset {args.scope}.{args.key}")
        return 0
    report = config.validate_document(path, args.scope, catalog=catalog)
    print(config.render_validation(report))
    return 0 if report.valid else 1


def _catalog_required(args: argparse.Namespace) -> bool:
    if args.command in {"tools", "doctor", "agent"}:
        return True
    return args.command == "config" and (
        args.config_command == "validate"
        or (args.config_command == "init" and args.scope == "agent")
        or (
            args.config_command == "set"
            and args.scope == "agent"
            and args.key == "tools.allowlist"
        )
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    catalog: CatalogSnapshot | None = None,
) -> int:
    """Run the strict Agent Relay CLI and return a process-compatible status."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        print(_HELP, end="")
        return 0
    try:
        path, command_argv = _extract_config(raw)
    except SystemExit:
        raise
    if command_argv == ["--help"]:
        print(_HELP, end="")
        return 0
    if command_argv == ["--version"]:
        print(f"agent-relay {config.PUBLIC_VERSION}")
        return 0
    if "--help" in command_argv:
        parser = _parser()
        parser.error("only the top-level --help option is supported")
    if not command_argv:
        print(_HELP, end="")
        return 0
    parser = _parser()
    args = parser.parse_args(command_argv)
    try:
        effective_catalog = catalog
        if effective_catalog is None and _catalog_required(args):
            effective_catalog = config.discover_local_catalog(path)
        if args.command == "config":
            return _run_config(args, path, catalog=effective_catalog)
        if args.command == "tools":
            if args.tool_command == "list":
                print(config.render_tools(path, catalog=effective_catalog))
                return 0
            config.update_tool(
                path,
                args.tool,
                enabled=args.tool_command == "enable",
                catalog=effective_catalog,
            )
            print(f"{'enabled' if args.tool_command == 'enable' else 'disabled'} {args.tool}")
            return 0
        if args.command == "doctor":
            output, valid = config.doctor(path, catalog=effective_catalog)
            print(output)
            return 0 if valid else 1
        if args.command == "server":
            config.load_server_runtime(path)
            server.main(["--config", str(path)])
            return 0
        if args.command == "agent":
            config.load_agent_settings(path, catalog=effective_catalog)
            if effective_catalog is None:
                agent.main(["--config", str(path)])
            else:
                agent.main(["--config", str(path)], catalog=effective_catalog)
            return 0
        parser.error("unsupported command")
    except config.ConfigError as exc:
        print(f"agent-relay: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
