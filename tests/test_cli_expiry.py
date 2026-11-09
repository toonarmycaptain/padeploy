"""The expiry command, and the expiry warning from the other commands."""

from collections.abc import Callable
from datetime import date
from unittest.mock import Mock

import pytest
from cli_stubs import patch_deploy, patch_reload, patch_traffic
from click.testing import CliRunner

from padeploy import cli
from padeploy.api import PAError

DESCRIBED = "arthur.pythonanywhere.com expires soon."
EXTEND = "Extend it on the Web tab: https://www.pythonanywhere.com/user/arthur/webapps/"


# Each lambda stubs its command and returns the client class mock.
@pytest.mark.parametrize(
    ("command", "patch"),
    [
        pytest.param(["deploy"], lambda mp: patch_deploy(mp)[2], id="deploy"),
        pytest.param(["deploy", "--dry-run"], lambda mp: patch_deploy(mp)[2], id="dry-run"),
        pytest.param(["reload"], lambda mp: patch_reload(mp)[1], id="reload"),
        pytest.param(
            ["traffic"], lambda mp: patch_traffic(mp, my_ip="203.0.113.7")[0], id="traffic"
        ),
    ],
)
def test_commands_warn_when_the_web_app_expires_soon(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    patch: Callable[[pytest.MonkeyPatch], Mock],
) -> None:
    patch(monkeypatch).return_value.expiry.side_effect = [date(2000, 1, 1)]
    monkeypatch.setattr(cli, "describe", Mock(return_value=DESCRIBED))
    result = CliRunner().invoke(cli.main, command)
    assert (result.exit_code, result.stderr) == (0, f"Warning: {DESCRIBED} {EXTEND}\n")


@pytest.mark.parametrize(
    ("args", "expires", "expected_stderr"),
    [
        pytest.param([], None, "", id="does-not-expire"),
        pytest.param([], date.max, "", id="not-close"),
        pytest.param([], PAError("boom"), "", id="failed-lookup-is-silent"),
        pytest.param(
            ["-v"],
            PAError("boom"),
            "Couldn't check when arthur.pythonanywhere.com expires: boom\n",
            id="failed-lookup-verbose",
        ),
    ],
)
def test_expiry_warning_is_only_for_a_close_date_and_never_fails_the_command(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expires: date | Exception | None,
    expected_stderr: str,
) -> None:
    _, client_class = patch_reload(monkeypatch)
    client_class.return_value.expiry.side_effect = [expires]
    monkeypatch.setattr(cli, "describe", Mock(return_value=DESCRIBED))
    result = CliRunner().invoke(cli.main, [*args, "reload"])
    assert (result.exit_code, result.stderr) == (0, expected_stderr)


@pytest.mark.parametrize(
    ("expires", "expected"),
    [
        pytest.param(
            None, (0, "arthur.pythonanywhere.com doesn't expire.\n", ""), id="does-not-expire"
        ),
        pytest.param(date.max, (0, f"{DESCRIBED}\n", ""), id="not-close"),
        pytest.param(date(2000, 1, 1), (1, f"{DESCRIBED}\n{EXTEND}\n", ""), id="due-exits-1"),
        pytest.param(PAError("boom"), (1, "", "Error: boom\n"), id="api-error-exits-1"),
    ],
)
def test_expiry_output_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    expires: date | Exception | None,
    expected: tuple[int, str, str],
) -> None:
    _, client_class = patch_reload(monkeypatch)
    client_class.return_value.expiry.side_effect = [expires]
    monkeypatch.setattr(cli, "describe", Mock(return_value=DESCRIBED))
    result = CliRunner().invoke(cli.main, ["expiry"])
    assert (result.exit_code, result.stdout, result.stderr) == expected
