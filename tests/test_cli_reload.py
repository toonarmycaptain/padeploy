from pathlib import Path
from typing import Any
from unittest.mock import call

import pytest
from cli_stubs import patch_reload
from click.testing import CliRunner

from padeploy import cli
from padeploy.api import PAError
from padeploy.config import ConfigError


def test_reload_passes_config_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    token_loader, client_class = patch_reload(monkeypatch)
    CliRunner().invoke(cli.main, ["reload"])
    assert (token_loader.mock_calls, client_class.mock_calls) == (
        [call(Path("/project"))],
        [
            call("arthur", "secret", "www.pythonanywhere.com"),
            call().reload("arthur.pythonanywhere.com"),
            call().expiry("arthur.pythonanywhere.com"),
        ],
    )


@pytest.mark.parametrize(
    ("stubs", "expected"),
    [
        pytest.param({}, (0, "Reloaded arthur.pythonanywhere.com\n", ""), id="ok"),
        pytest.param(
            {"reload_result": "arthur.pythonanywhere.com reloaded, but no CNAME"},
            (0, "", "Warning: arthur.pythonanywhere.com reloaded, but no CNAME\n"),
            id="cname-warning-exits-0",
        ),
        pytest.param(
            {"reload_result": PAError("Reload failed (500): boom")},
            (1, "", "Error: Reload failed (500): boom\n"),
            id="api-error-exits-1",
        ),
        pytest.param(
            {"config": ConfigError("padeploy needs USER")},
            (1, "", "Error: padeploy needs USER\n"),
            id="config-error-exits-1",
        ),
    ],
)
def test_reload_output_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, stubs: dict[str, Any], expected: tuple[int, str, str]
) -> None:
    patch_reload(monkeypatch, **stubs)
    result = CliRunner().invoke(cli.main, ["reload"])
    assert (result.exit_code, result.stdout, result.stderr) == expected
