from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from signoz_mcp import sz_runner
from signoz_mcp.sz_commands import (
    ALLOWED_COMMANDS,
    SZ_COMMANDS,
    command_usage,
    help_payload,
    option_flags,
    sz_run_description,
)


SZ_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sz"


def _load_sz_module():
    loader = importlib.machinery.SourceFileLoader("sz_cli_for_tests", str(SZ_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("sz parser has no subcommands")


def test_runner_allow_list_is_derived_from_command_spec():
    assert sz_runner._ALLOWED_COMMANDS == ALLOWED_COMMANDS == frozenset(SZ_COMMANDS)


def test_command_spec_matches_scripts_sz_argparse():
    parser = _load_sz_module().build_parser()
    subparsers = _subparsers(parser)

    assert set(subparsers) == set(SZ_COMMANDS)
    for name, subparser in subparsers.items():
        flags = set()
        positionals = []
        for action in subparser._actions:
            if action.option_strings:
                long_flags = [flag for flag in action.option_strings if flag.startswith("--")]
                flags.add(long_flags[0] if long_flags else action.option_strings[0])
            else:
                positionals.append(action.dest)
        flags -= {"--help", "--json", "--quiet"}
        assert flags == set(option_flags(name)), name
        assert len(positionals) == len(SZ_COMMANDS[name]["positionals"]), name


def test_sz_run_description_lists_every_command_and_time_flags():
    description = sz_run_description()

    for name in SZ_COMMANDS:
        assert command_usage(name) in description
    assert "--start ISO" in description
    assert "--end ISO" in description
    assert '["help"]' in description


@pytest.mark.parametrize("args", [["help"], ["--help"], ["-h"]])
def test_help_payload_lists_all_commands(args):
    payload = help_payload(args)

    assert payload["success"] is True
    assert [command["name"] for command in payload["data"]["commands"]] == list(SZ_COMMANDS)


@pytest.mark.parametrize("args", [["query", "--help"], ["help", "query"]])
def test_help_payload_for_one_command(args):
    payload = help_payload(args)

    assert [command["name"] for command in payload["data"]["commands"]] == ["query"]
    flags = [option["flag"] for option in payload["data"]["commands"][0]["options"]]
    assert "--start ISO" in flags


@pytest.mark.parametrize(
    "args",
    [["query", "SELECT 1", "--hours", "3"], ["services"], "help", [], [1]],
)
def test_help_payload_ignores_normal_commands(args):
    assert help_payload(args) is None


def test_unsupported_command_error_lists_supported_commands():
    with pytest.raises(ValueError) as excinfo:
        sz_runner._validate_args(["drop"])

    message = str(excinfo.value)
    assert "unsupported sz command: drop" in message
    for name in SZ_COMMANDS:
        assert name in message
    assert "help" in message
