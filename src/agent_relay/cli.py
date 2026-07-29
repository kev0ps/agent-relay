"""Explicit role selector for the single Agent Relay OCI image."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import agent, server


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch an explicitly selected role without loading its configuration."""
    role_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="agent-relay", description="Agent Relay")
    roles = parser.add_subparsers(dest="role")
    roles.add_parser("server", help="run the Relay control server")
    roles.add_parser("client", help="run or configure the outbound client")
    roles.add_parser("agent", help="compatibility alias for client")

    if role_argv and role_argv[0] == "server":
        server_argv = role_argv[1:]
        if server_argv and server_argv[0] == "run":
            server_argv = server_argv[1:]
        server.main(server_argv)
        return 0
    if role_argv and role_argv[0] == "client":
        client_argv = role_argv[1:]
        if client_argv and client_argv[0] == "run":
            agent.main(client_argv[1:])
            return 0
        if client_argv and client_argv[0] == "config":
            return agent.config_main(client_argv[1:])
        parser.error("client requires the run or config subcommand")
    if role_argv and role_argv[0] == "agent":
        agent_argv = role_argv[1:]
        if agent_argv and agent_argv[0] == "run":
            agent_argv = agent_argv[1:]
        if agent_argv and agent_argv[0] == "config":
            return agent.config_main(agent_argv[1:], prog="agent-relay agent config")
        agent.main(agent_argv)
        return 0

    if role_argv:
        parser.parse_args(role_argv)

    parser.print_help()
    return 2
