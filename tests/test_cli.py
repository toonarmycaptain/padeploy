import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from click.testing import CliRunner

from padeploy import cli
from padeploy.api import PAError
from padeploy.config import Config, ConfigError, MissingTokenError, parse_config
from padeploy.selection import SelectionError

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


APP_OK = ("app.py", None)


def patch_deploy(
    monkeypatch: pytest.MonkeyPatch,
    files: Sequence[str] | Exception = ("app.py",),
    results: Sequence[tuple[str, str | None] | Exception] = (APP_OK,),
    token: str | Exception = "secret",
    reload_result: str | Exception | None = None,
    missing: Sequence[str] = (),
) -> tuple[Mock, Mock, Mock]:
    """
    Stub patch_reload functionality, file selection, include warnings, missing-file check and
    uploads.

    An exception in ``results`` is raised when the uploads reach it.
    Returns the select_files, upload_files and client mocks.
    """

    def uploads(*_: object) -> Iterator[tuple[str, str | None]]:
        for result in results:
            if isinstance(result, Exception):
                raise result
            yield result

    _, client_class = patch_reload(monkeypatch, token=token, reload_result=reload_result)
    select_files = Mock(side_effect=[files])
    upload_files = Mock(side_effect=uploads)
    monkeypatch.setattr(cli, "select_files", select_files)
    monkeypatch.setattr(cli, "include_warnings", Mock(return_value=[]))
    monkeypatch.setattr(cli, "find_missing", Mock(return_value=list(missing)))
    monkeypatch.setattr(cli, "upload_files", upload_files)
    return select_files, upload_files, client_class


SELECT_DEFAULTS: dict[str, object] = {
    "since": None,
    "changes": False,
    "staged": False,
    "include": (),
    "exclude": (),
    "with_groups": (),
    "only": None,
}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param([], {}, id="defaults"),
        pytest.param(["--changes"], {"changes": True}, id="changes"),
        pytest.param(["--staged"], {"staged": True}, id="staged"),
        pytest.param(["--since", "abc123"], {"since": "abc123"}, id="since"),
        pytest.param(
            ["--with", "data", "--with", "media"], {"with_groups": ("data", "media")}, id="with"
        ),
        pytest.param(["--only", "assets"], {"only": "assets"}, id="only"),
        pytest.param(["--code"], {"only": "code"}, id="code-is-only-code"),
        pytest.param(
            ["--include", "a/,b.py", "--include=c/"],
            {"include": ("a/", "b.py", "c/")},
            id="include-splits-commas-and-repeats",
        ),
        pytest.param(
            ["--exclude", "a/,b.py", "--exclude=c/"],
            {"exclude": ("a/", "b.py", "c/")},
            id="exclude-splits-commas-and-repeats",
        ),
    ],
)
def test_deploy_passes_options_to_select_files(
    monkeypatch: pytest.MonkeyPatch, args: list[str], expected: dict[str, object]
) -> None:
    select_files, _, _ = patch_deploy(monkeypatch, files=[])
    CliRunner().invoke(cli.main, ["deploy", *args])
    assert select_files.mock_calls == [call(CONFIG, **{**SELECT_DEFAULTS, **expected})]


ONE_OF = "Use only one of --changes, --staged and --since."


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--changes", "--staged"], ONE_OF),
        (["--since", "abc123", "--changes"], ONE_OF),
        (["--code", "--only", "assets"], "Use --code or --only, not both."),
    ],
)
def test_deploy_rejects_conflicting_options(args: list[str], message: str) -> None:
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (result.exit_code, result.stderr.splitlines()[-1]) == (2, f"Error: {message}")


def test_deploy_uploads_with_client_then_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    _, upload_files, client_class = patch_deploy(monkeypatch)
    calls = Mock()
    calls.attach_mock(client_class, "PAClient")
    calls.attach_mock(upload_files, "upload_files")
    CliRunner().invoke(cli.main, ["deploy"])
    assert calls.mock_calls == [
        call.PAClient("arthur", "secret", "www.pythonanywhere.com"),
        call.upload_files(client_class.return_value, CONFIG, ["app.py"]),
        call.PAClient().reload("arthur.pythonanywhere.com"),
    ]


UPLOADING = "Uploading 1 file(s) to /home/arthur/arthur\n  OK app.py\n"
RELOADED = "Reloaded arthur.pythonanywhere.com\n"


@pytest.mark.parametrize(
    ("args", "files", "results", "reload_result", "expected"),
    [
        pytest.param([], [], [], None, (0, "No files to deploy.\n", ""), id="no-files"),
        pytest.param(
            ["--dry-run"],
            ["app.py", "static/a.css"],
            [],
            None,
            (0, "Would upload 2 file(s) to /home/arthur/arthur:\n  app.py\n  static/a.css\n", ""),
            id="dry-run",
        ),
        pytest.param([], ["app.py"], [APP_OK], None, (0, UPLOADING + RELOADED, ""), id="ok"),
        pytest.param(
            [],
            ["app.py", "db.py"],
            [APP_OK, ("db.py", "Upload failed (500): boom")],
            None,
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  OK app.py\n"
                "  FAIL db.py: Upload failed (500): boom\n",
                "Error: 1 of 2 file(s) failed to upload; not reloading.\n",
            ),
            id="failed-upload-exits-1-without-reloading",
        ),
        pytest.param(
            ["--no-reload"], ["app.py"], [APP_OK], None, (0, UPLOADING, ""), id="no-reload"
        ),
        pytest.param(
            [],
            ["pyproject.toml", "uv.lock"],
            [("pyproject.toml", None), ("uv.lock", None)],
            None,
            (
                0,
                "Uploading 2 file(s) to /home/arthur/arthur\n  OK pyproject.toml\n  OK uv.lock\n"
                + RELOADED,
                "Warning: uploaded pyproject.toml and uv.lock. If dependencies changed, run"
                " `uv sync` on PythonAnywhere, then `padeploy reload`.\n",
            ),
            id="uv-files-warn-to-sync",
        ),
        pytest.param(
            [],
            ["pyproject.toml", "app.py"],
            [("pyproject.toml", None), ("app.py", "Upload failed (500): boom")],
            None,
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  OK pyproject.toml\n"
                "  FAIL app.py: Upload failed (500): boom\n",
                "Warning: uploaded pyproject.toml. If dependencies changed, run `uv sync` on"
                " PythonAnywhere, then `padeploy reload`.\n"
                "Error: 1 of 2 file(s) failed to upload; not reloading.\n",
            ),
            id="uv-warning-survives-a-later-failure",
        ),
        pytest.param(
            [],
            ["pyproject.toml", "app.py"],
            [("pyproject.toml", None), PAError("POST failed: connection reset")],
            None,
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  OK pyproject.toml\n",
                "Warning: uploaded pyproject.toml. If dependencies changed, run `uv sync` on"
                " PythonAnywhere, then `padeploy reload`.\n"
                "Error: POST failed: connection reset\n",
            ),
            id="uv-warning-survives-an-api-error",
        ),
        pytest.param(
            [],
            ["app.py"],
            [APP_OK],
            "arthur.pythonanywhere.com reloaded, but no CNAME",
            (0, UPLOADING, "Warning: arthur.pythonanywhere.com reloaded, but no CNAME\n"),
            id="cname-warning-exits-0",
        ),
        pytest.param(
            [],
            ["app.py"],
            [APP_OK],
            PAError("Reload failed (500): boom"),
            (1, UPLOADING, "Error: Reload failed (500): boom\n"),
            id="reload-error-exits-1",
        ),
        pytest.param(
            [],
            SelectionError("Unknown group(s): nope"),
            [],
            None,
            (1, "", "Error: Unknown group(s): nope\n"),
            id="selection-error-exits-1",
        ),
        pytest.param(
            ["--exclude", "/elsewhere/"],
            ["app.py"],
            [],
            None,
            (1, "", "Error: --exclude pattern '/elsewhere/' is outside the project, /project\n"),
            id="flag-pattern-outside-project-exits-1",
        ),
    ],
)
def test_deploy_output_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    files: list[str] | Exception,
    results: list[tuple[str, str | None] | Exception],
    reload_result: str | Exception | None,
    expected: tuple[int, str, str],
) -> None:
    patch_deploy(monkeypatch, files, results, reload_result=reload_result)
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (result.exit_code, result.stdout, result.stderr) == expected


@pytest.mark.parametrize(
    ("args", "files", "expected"),
    [
        pytest.param(
            [],
            ["app.py", "old.py"],
            (
                "Skipping old.py: not found\nUploading 1 file(s) to /home/arthur/arthur\n"
                "  OK app.py\n" + RELOADED,
                [["app.py"]],
            ),
            id="deploy",
        ),
        pytest.param(
            ["--dry-run"],
            ["app.py", "old.py"],
            (
                "Skipping old.py: not found\n"
                "Would upload 1 file(s) to /home/arthur/arthur:\n  app.py\n",
                [],
            ),
            id="dry-run",
        ),
        pytest.param(
            [],
            ["old.py"],
            ("Skipping old.py: not found\nNo files to deploy.\n", []),
            id="only-missing",
        ),
    ],
)
def test_deploy_skips_missing_files(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    files: list[str],
    expected: tuple[str, list[list[str]]],
) -> None:
    _, upload_files, _ = patch_deploy(monkeypatch, files=files, missing=["old.py"])
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (result.stdout, [c.args[2] for c in upload_files.mock_calls]) == expected


@pytest.mark.parametrize("args", [[], ["--dry-run"]], ids=["deploy", "dry-run"])
def test_deploy_prints_include_warnings(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    patch_deploy(monkeypatch)
    include_warnings = Mock(return_value=["INCLUDE pattern 'a.svg' matches nothing"])
    monkeypatch.setattr(cli, "include_warnings", include_warnings)
    result = CliRunner().invoke(cli.main, ["deploy", "--include", "a.svg", *args])
    assert (include_warnings.mock_calls, result.stderr) == (
        [call(CONFIG, ("a.svg",), ())],
        "Warning: INCLUDE pattern 'a.svg' matches nothing\n",
    )


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
