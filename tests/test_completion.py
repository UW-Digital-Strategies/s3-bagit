"""The shipped bash completion script matches what the parser renders."""

import argparse
from pathlib import Path

import pytest

from s3_bagit._cli_parsers import _build_parser
from s3_bagit._completion import _render

SCRIPT = Path(__file__).resolve().parent.parent / "completions" / "s3-bagit.bash"


def test_shipped_script_matches_parser() -> None:
    # A failure here means a subcommand or option changed without
    # `make completions`; regenerate and commit the script.
    assert SCRIPT.read_text(encoding="utf-8") == _render(_build_parser())


def test_every_subcommand_is_offered() -> None:
    script = _render(_build_parser())
    for name in ("extract", "verify", "verify-against", "create-bag", "ls", "config", "issue"):
        assert f"        {name}) opts=" in script


def test_value_taking_options_suppress_completion_of_their_value() -> None:
    script = _render(_build_parser())
    assert 'valued="--bag-name --algorithm --bag-info"' in script
    assert 'valued="--profile"' in script


def test_top_level_value_option_is_refused() -> None:
    parser = argparse.ArgumentParser(prog="x")
    parser.add_argument("--region")
    parser.add_subparsers(dest="command").add_parser("go")
    with pytest.raises(ValueError, match="--region"):
        _render(parser)


def test_parser_without_subcommands_is_refused() -> None:
    with pytest.raises(ValueError, match="no subcommands"):
        _render(argparse.ArgumentParser(prog="x"))
