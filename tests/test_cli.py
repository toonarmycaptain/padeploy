"""Behaviour shared by the commands: log output and the token prompt."""

import logging
from dataclasses import replace
from unittest.mock import call

import pytest
from cli_stubs import CONFIG, patch_deploy, patch_reload
from click.testing import CliRunner

from padeploy import cli
from padeploy.config import ConfigError, MissingTokenError


def log_warning_and_debug(domain: str) -> None:
    logger = logging.getLogger("padeploy.api")
    logger.warning("a warning")
    logger.debug("a debug message")


BOTH = "a warning\na debug message\n"


@pytest.mark.parametrize(
    ("args", "log_level", "expected_stderr"),
    [
        pytest.param(["reload"], "WARNING", "a warning\n", id="default-shows-warnings-only"),
        pytest.param(["-v", "reload"], "WARNING", BOTH, id="verbose-shows-debug"),
        pytest.param(["reload"], "DEBUG", BOTH, id="log-level-debug-shows-debug"),
        pytest.param(["reload"], "ERROR", "", id="log-level-error-hides-warnings"),
        pytest.param(["-v", "reload"], "ERROR", BOTH, id="verbose-overrides-log-level"),
    ],
)
def test_log_messages_on_stderr(
    monkeypatch: pytest.MonkeyPatch, args: list[str], log_level: str, expected_stderr: str
) -> None:
    _, client_class = patch_reload(monkeypatch, replace(CONFIG, log_level=log_level))
    client_class.return_value.reload.side_effect = log_warning_and_debug
    result = CliRunner().invoke(cli.main, args)
    assert result.stderr == expected_stderr


@pytest.mark.parametrize(
    ("interactive", "token_error", "expected"),
    [
        pytest.param(
            True,
            MissingTokenError("padeploy needs API_TOKEN"),
            (0, call("arthur", "typed", "www.pythonanywhere.com"), ""),
            id="missing-in-terminal-prompts",
        ),
        pytest.param(
            False,
            MissingTokenError("padeploy needs API_TOKEN"),
            (1, None, "Error: padeploy needs API_TOKEN\n"),
            id="missing-not-in-terminal-exits-1",
        ),
        pytest.param(
            True,
            ConfigError("API_TOKEN in .padeploy_secrets.toml must be a string"),
            (1, None, "Error: API_TOKEN in .padeploy_secrets.toml must be a string\n"),
            id="invalid-in-terminal-exits-1-without-prompting",
        ),
    ],
)
@pytest.mark.parametrize("command", ["deploy", "reload"])
def test_prompts_only_for_missing_token_in_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    interactive: bool,
    token_error: ConfigError,
    expected: tuple[int, object, str],
) -> None:
    _, _, client_class = patch_deploy(monkeypatch, token=token_error)
    monkeypatch.setattr(cli, "_interactive", lambda: interactive)
    result = CliRunner().invoke(cli.main, [command], input="typed\n")
    assert (result.exit_code, client_class.call_args, result.stderr) == expected
