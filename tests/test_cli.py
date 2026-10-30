import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from click.testing import CliRunner

from padeploy import cli
from padeploy.api import PAError
from padeploy.config import Config, ConfigError, MissingTokenError, parse_config
from padeploy.selection import SelectionError
from padeploy.traffic import Logs, Network

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
    config: Config = CONFIG,
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

    _, client_class = patch_reload(monkeypatch, config, token=token, reload_result=reload_result)
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


def test_deploy_warns_about_the_frameworks_server_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    django = parse_config(
        {"USER": "arthur", "REMOTE_DIR": "/home/arthur/arthur", "FRAMEWORK": "django"},
        Path("/project"),
    )
    files = ["shop/migrations/0002_add_size.py", "shop/static/shop/site.css"]
    patch_deploy(monkeypatch, files, [(path, None) for path in files], config=django)
    result = CliRunner().invoke(cli.main, ["deploy"])
    assert result.stderr == (
        "Warning: uploaded migrations. If they changed, run `python manage.py migrate` on"
        " PythonAnywhere, then `padeploy reload`.\n"
        "Warning: uploaded static files. If they changed, run `python manage.py collectstatic` on"
        " PythonAnywhere, then `padeploy reload`.\n"
    )


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


LOG = "arthur.pythonanywhere.com.access.log"


def patch_traffic(
    monkeypatch: pytest.MonkeyPatch,
    my_ip: str = "",
    detected: Sequence[str] = (),
    sources: Sequence[str] = (LOG,),
) -> tuple[Mock, Mock, Mock]:
    """
    Stub patch_reload functionality, MY_IP, IP detection, log fetching and the report.

    Returns the client, read_logs and report mocks.
    """
    _, client_class = patch_reload(monkeypatch)
    read_logs = Mock(return_value=Logs(list(sources), [], 0))
    report = Mock(return_value="REPORT")
    monkeypatch.setattr(cli, "load_my_ip", Mock(return_value=my_ip))
    monkeypatch.setattr(cli, "detect_public_ips", Mock(return_value=set(detected)))
    monkeypatch.setattr(cli, "read_logs", read_logs)
    monkeypatch.setattr(cli, "report", report)
    return client_class, read_logs, report


START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 8, 1, tzinfo=UTC)
PERIOD_DEFAULTS: dict[str, object] = {"since": None, "until": None, "month": None, "year": None}


@pytest.mark.parametrize(
    ("args", "expected_options", "start_end", "expected_back_to", "expected_report_options"),
    [
        pytest.param([], {}, (None, None), "now", {}, id="default-reads-back-to-now"),
        pytest.param(["--all"], {}, (None, None), None, {}, id="all-reads-every-log"),
        pytest.param(
            ["--since", "2026-07-01", "--until", "2026-07-31"],
            {"since": datetime(2026, 7, 1), "until": datetime(2026, 7, 31)},
            (START, END),
            START,
            {},
            id="since-and-until",
        ),
        pytest.param(
            ["--until", "2026-07-31"],
            {"until": datetime(2026, 7, 31)},
            (None, END),
            None,
            {},
            id="lone-until-reads-every-log",
        ),
        pytest.param(["--month"], {"month": ""}, (START, None), START, {}, id="month"),
        pytest.param(
            ["--month", "July"], {"month": "July"}, (START, END), START, {}, id="named-month"
        ),
        pytest.param(["--year", "2026"], {"year": "2026"}, (START, END), START, {}, id="year"),
        pytest.param(
            ["--top", "5", "--detail"],
            {},
            (None, None),
            "now",
            {"top": 5, "detail": True},
            id="top-and-detail",
        ),
        pytest.param(
            ["--top=false"], {}, (None, None), "now", {"top": None}, id="top-false-is-no-limit"
        ),
        pytest.param(
            ["--top", "False"], {}, (None, None), "now", {"top": None}, id="false-any-case"
        ),
    ],
)
def test_traffic_passes_options_to_period_read_logs_and_report(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expected_options: dict[str, object],
    start_end: tuple[datetime | None, datetime | None],
    expected_back_to: datetime | str | None,
    expected_report_options: dict[str, object],
) -> None:
    client_class, read_logs, report = patch_traffic(monkeypatch, my_ip="1.2.3.4")
    period = Mock(return_value=start_end)
    monkeypatch.setattr(cli, "period", period)
    CliRunner().invoke(cli.main, ["traffic", *args])
    now = period.call_args.args[0]
    start, end = start_end

    assert (period.call_args, read_logs.call_args, report.call_args) == (
        call(now, **{**PERIOD_DEFAULTS, **expected_options}),
        call(
            client_class.return_value,
            "arthur.pythonanywhere.com",
            now if expected_back_to == "now" else expected_back_to,
        ),
        call(
            "arthur.pythonanywhere.com",
            read_logs.return_value,
            {ip_network("1.2.3.4")},
            start=start,
            end=end,
            **{"top": 10, "detail": False, **expected_report_options},
        ),
    )


@pytest.mark.parametrize(
    ("args", "my_ip", "detected", "expected"),
    [
        pytest.param(
            ["--my-ip", "5.6.7.8, 2001:db8::1"],
            "1.2.3.4",
            ["9.9.9.9"],
            ({ip_network("5.6.7.8"), ip_network("2001:db8::/64")}, "REPORT\n", ""),
            id="option-overrides-my-ip",
        ),
        pytest.param(
            [],
            "1.2.3.4",
            ["9.9.9.9"],
            ({ip_network("1.2.3.4")}, "REPORT\n", ""),
            id="my-ip-skips-detection",
        ),
        pytest.param(
            [],
            "",
            ["9.9.9.9", "::9"],
            (
                {ip_network("9.9.9.9"), ip_network("::/64")},
                "Auto-detected your IP(s): 9.9.9.9, ::9 (override with --my-ip or MY_IP).\n"
                "REPORT\n",
                "",
            ),
            id="auto-detected",
        ),
        pytest.param(
            [],
            "",
            [],
            (
                set(),
                "REPORT\n",
                "Warning: couldn't auto-detect your IP; counting everything as others'. Use"
                " --my-ip or MY_IP to split out your traffic.\n",
            ),
            id="not-detected-warns",
        ),
    ],
)
def test_traffic_chooses_your_ips_and_prints_report(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    my_ip: str,
    detected: list[str],
    expected: tuple[set[Network], str, str],
) -> None:
    _, _, report = patch_traffic(monkeypatch, my_ip, detected)
    result = CliRunner().invoke(cli.main, ["traffic", *args])

    assert (report.call_args.args[2], result.stdout, result.stderr) == expected


ONE_PERIOD = "Error: Use only one of --all, --since/--until, --month and --year."


@pytest.mark.parametrize(
    ("args", "my_ip", "sources", "expected"),
    [
        pytest.param(["--all", "--month"], "1.2.3.4", [LOG], (2, ONE_PERIOD), id="all-and-month"),
        pytest.param(
            ["--since", "2026-07-01", "--year"],
            "1.2.3.4",
            [LOG],
            (2, ONE_PERIOD),
            id="since-and-year",
        ),
        pytest.param(
            ["--since", "July"],
            "1.2.3.4",
            [LOG],
            (2, "Error: Invalid value for '--since': 'July' does not match the format '%Y-%m-%d'."),
            id="since-not-a-date",
        ),
        pytest.param(
            ["--month", "Julember"],
            "1.2.3.4",
            [LOG],
            (2, "Error: --month must be like 2026-07 or July, got 'Julember'"),
            id="unknown-month",
        ),
        pytest.param(
            ["--top", "0"],
            "1.2.3.4",
            [LOG],
            (
                2,
                "Error: Invalid value for '--top': must be a number of at least 1, or false,"
                " got '0'",
            ),
            id="top-below-1",
        ),
        pytest.param(
            ["--my-ip", "home"],
            "",
            [LOG],
            (1, "Error: --my-ip: 'home' isn't an IP address or network"),
            id="my-ip-option-not-an-address",
        ),
        pytest.param(
            [],
            "home",
            [LOG],
            (1, "Error: MY_IP: 'home' isn't an IP address or network"),
            id="my-ip-config-not-an-address",
        ),
        pytest.param(
            [],
            "1.2.3.4",
            [],
            (1, f"Error: No access log found at /var/log/{LOG}"),
            id="no-log-exits-1",
        ),
    ],
)
def test_traffic_errors(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    my_ip: str,
    sources: list[str],
    expected: tuple[int, str],
) -> None:
    patch_traffic(monkeypatch, my_ip, sources=sources)
    result = CliRunner().invoke(cli.main, ["traffic", *args])

    assert (result.exit_code, result.stderr.splitlines()[-1]) == expected
