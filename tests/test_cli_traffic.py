from datetime import UTC, datetime
from ipaddress import ip_network
from typing import Any
from unittest.mock import Mock, call

import pytest
from cli_stubs import LOG, patch_traffic
from click.testing import CliRunner

from padeploy import cli
from padeploy.traffic import Network

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
    ("args", "stubs", "expected"),
    [
        pytest.param(["--all", "--month"], {}, (2, ONE_PERIOD), id="all-and-month"),
        pytest.param(["--since", "2026-07-01", "--year"], {}, (2, ONE_PERIOD), id="since-and-year"),
        pytest.param(
            ["--since", "July"],
            {},
            (2, "Error: Invalid value for '--since': 'July' does not match the format '%Y-%m-%d'."),
            id="since-not-a-date",
        ),
        pytest.param(
            ["--month", "Julember"],
            {},
            (2, "Error: --month must be like 2026-07 or July, got 'Julember'"),
            id="unknown-month",
        ),
        pytest.param(
            ["--top", "0"],
            {},
            (
                2,
                "Error: Invalid value for '--top': must be a number of at least 1, or false,"
                " got '0'",
            ),
            id="top-below-1",
        ),
        pytest.param(
            ["--my-ip", "home"],
            {},
            (1, "Error: --my-ip: 'home' isn't an IP address or network"),
            id="my-ip-option-not-an-address",
        ),
        pytest.param(
            [],
            {"my_ip": "home"},
            (1, "Error: MY_IP: 'home' isn't an IP address or network"),
            id="my-ip-config-not-an-address",
        ),
        pytest.param(
            [],
            {"sources": []},
            (1, f"Error: No access log found at /var/log/{LOG}"),
            id="no-log-exits-1",
        ),
    ],
)
def test_traffic_errors(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    stubs: dict[str, Any],
    expected: tuple[int, str],
) -> None:
    patch_traffic(monkeypatch, **stubs)
    result = CliRunner().invoke(cli.main, ["traffic", *args])

    assert (result.exit_code, result.stderr.splitlines()[-1]) == expected
