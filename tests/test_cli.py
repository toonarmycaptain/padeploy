import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from click.testing import CliRunner

from padeploy import cli
from padeploy.api import PAError
from padeploy.config import Config, ConfigError, parse_config

CONFIG = parse_config({"USER": "arthur", "REMOTE_DIR": "/home/arthur/arthur"}, Path("/project"))


def patch_reload(
    monkeypatch: pytest.MonkeyPatch,
    config: Config | Exception = CONFIG,
    token: str | Exception = "secret",
    reload_result: str | Exception | None = None,
) -> tuple[Mock, Mock]:
    """Stub config and token loading, and the client; returns the token loader and client mocks."""
    token_loader = Mock(side_effect=[token])
    client_class = Mock()
    client_class.return_value.reload.side_effect = [reload_result]
    monkeypatch.setattr(cli, "load_config", Mock(side_effect=[config]))
    monkeypatch.setattr(cli, "load_token", token_loader)
    monkeypatch.setattr(cli, "PAClient", client_class)
    return token_loader, client_class


def test_reload_passes_config_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    token_loader, client_class = patch_reload(monkeypatch)
    CliRunner().invoke(cli.main, ["reload"])
    assert (token_loader.mock_calls, client_class.mock_calls) == (
        [call(Path("/project"))],
        [
            call("arthur", "secret", "www.pythonanywhere.com"),
            call().reload("arthur.pythonanywhere.com"),
        ],
    )


@pytest.mark.parametrize(
    ("config", "token", "reload_result", "expected"),
    [
        pytest.param(
            CONFIG, "secret", None, (0, "Reloaded arthur.pythonanywhere.com\n", ""), id="ok"
        ),
        pytest.param(
            CONFIG,
            "secret",
            "arthur.pythonanywhere.com reloaded, but no CNAME",
            (0, "", "Warning: arthur.pythonanywhere.com reloaded, but no CNAME\n"),
            id="cname-warning-exits-0",
        ),
        pytest.param(
            CONFIG,
            "secret",
            PAError("Reload failed (500): boom"),
            (1, "", "Error: Reload failed (500): boom\n"),
            id="api-error-exits-1",
        ),
        pytest.param(
            ConfigError("padeploy needs USER"),
            "secret",
            None,
            (1, "", "Error: padeploy needs USER\n"),
            id="config-error-exits-1",
        ),
        pytest.param(
            CONFIG,
            ConfigError("padeploy needs API_TOKEN"),
            None,
            (1, "", "Error: padeploy needs API_TOKEN\n"),
            id="missing-token-exits-1",
        ),
    ],
)
def test_reload_output_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    config: Config | Exception,
    token: str | Exception,
    reload_result: str | Exception | None,
    expected: tuple[int, str, str],
) -> None:
    patch_reload(monkeypatch, config, token, reload_result)
    result = CliRunner().invoke(cli.main, ["reload"])
    assert (result.exit_code, result.stdout, result.stderr) == expected


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
