from __future__ import annotations

from pathlib import Path

import pytest

from agent_relay import cli


def test_no_arguments_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "usage: agent-relay" in output
    assert "--config PATH" in output
    assert "config" in output
    assert "onboard" in output
    assert "tools" in output
    assert "doctor" in output


def test_onboarding_parser_exposes_the_three_transport_topologies() -> None:
    parser = cli._parser()
    assert parser.parse_args(["onboard", "--topology", "local"]).topology == "local"
    assert parser.parse_args(["onboard", "--topology", "lan"]).topology == "lan"
    assert parser.parse_args(["onboard", "--topology", "remote"]).topology == "remote"


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "get", "server"],
        ["tools", "list"],
        ["doctor"],
        ["onboard"],
        ["server"],
        ["agent"],
        ["uninstall"],
    ],
)
def test_parser_binds_each_top_level_command_to_a_handler(argv: list[str]) -> None:
    args = cli._parser().parse_args(argv)
    assert callable(getattr(args, "handler", None))


def test_help_and_version_are_top_level_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "--version" in capsys.readouterr().out
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "agent-relay 0.1.0"

    with pytest.raises(SystemExit) as error:
        cli.main(["server", "--help"])
    assert error.value.code == 2


def test_config_option_can_be_global_before_or_after_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(path), "config", "init", "server"]) == 0
    capsys.readouterr()
    assert cli.main(["config", "get", "server", "--config", str(path)]) == 0
    assert "host:" in capsys.readouterr().out


def test_legacy_runtime_commands_are_rejected() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["client"])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        cli.main(["server", "run"])
    assert error.value.code == 2
