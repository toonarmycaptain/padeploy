import gzip
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from ipaddress import ip_network
from typing import Any
from unittest.mock import Mock, call

import pytest
import requests

from padeploy.traffic import (
    Logs,
    Network,
    Request,
    detect_public_ips,
    log_names,
    parse_line,
    parse_networks,
    period,
    read_logs,
    report,
)

DOMAIN = "arthur.pythonanywhere.com"
LIVE = f"{DOMAIN}.access.log"


def test_log_names_picks_the_domains_access_logs_newest_first() -> None:
    listing = [
        f"{LIVE}.10.gz",
        f"{LIVE}.2.gz",
        LIVE,
        f"{LIVE}.1",
        f"{DOMAIN}.error.log",
        f"{DOMAIN}.server.log.1",
        "lancelot.pythonanywhere.com.access.log",
        "arthurXpythonanywhere.com.access.log",
        "nginx",
    ]
    assert log_names(listing, DOMAIN) == [LIVE, f"{LIVE}.1", f"{LIVE}.2.gz", f"{LIVE}.10.gz"]


OCT_10 = Request("192.0.2.1", datetime(2026, 10, 10, 12, tzinfo=UTC), "GET", "/", "200")
OCT_5 = Request("192.0.2.2", datetime(2026, 10, 5, 12, tzinfo=UTC), "GET", "/", "200")
SEP_20 = Request("192.0.2.3", datetime(2026, 9, 20, 12, tzinfo=UTC), "GET", "/", "200")
OCT_10_LINE = '192.0.2.1 - - [10/Oct/2026:12:00:00 +0000] "GET / HTTP/1.1" 200 1 "-" "-"'
OCT_5_LINE = '192.0.2.2 - - [05/Oct/2026:12:00:00 +0000] "GET / HTTP/1.1" 200 1 "-" "-"'
SEP_20_LINE = '192.0.2.3 - - [20/Sep/2026:12:00:00 +0000] "GET / HTTP/1.1" 200 1 "-" "-"'
LOG_FILES = {
    f"/var/log/{LIVE}": f"{OCT_10_LINE}\n\n".encode(),
    f"/var/log/{LIVE}.1": f"{OCT_5_LINE}\nnot a log line\n".encode(),
    f"/var/log/{LIVE}.2.gz": gzip.compress(f"{SEP_20_LINE}\n".encode()),
}


@pytest.mark.parametrize(
    ("back_to", "expected"),
    [
        pytest.param(
            None,
            Logs([LIVE, f"{LIVE}.1", f"{LIVE}.2.gz"], [OCT_10, OCT_5, SEP_20], 1),
            id="all-of-them",
        ),
        pytest.param(
            datetime(2026, 10, 6, tzinfo=UTC),
            Logs([LIVE, f"{LIVE}.1"], [OCT_10, OCT_5], 1),
            id="stops-at-a-log-going-back-far-enough",
        ),
        pytest.param(
            datetime(2026, 10, 16, tzinfo=UTC),
            Logs([LIVE], [OCT_10], 0),
            id="back-to-now-reads-the-latest",
        ),
    ],
)
def test_read_logs_reads_newest_first_until_one_goes_back_to_back_to(
    back_to: datetime | None, expected: Logs
) -> None:
    client = Mock()
    client.list_dir.return_value = [f"{LIVE}.2.gz", LIVE, f"{DOMAIN}.error.log", f"{LIVE}.1"]
    client.get_file.side_effect = LOG_FILES.__getitem__
    assert (read_logs(client, DOMAIN, back_to), client.list_dir.call_args_list) == (
        expected,
        [call("/var/log")],
    )


TIME = "13/Jun/2026:11:40:06 +0000"
JUN_13 = datetime(2026, 6, 13, 11, 40, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            f'198.51.100.7 - - [{TIME}] "GET /a?b=c HTTP/1.1" 200 512 "-" "Mozilla" "-"',
            Request("198.51.100.7", JUN_13, "GET", "/a?b=c", "200"),
            id="direct",
        ),
        pytest.param(
            f'172.70.1.1 - - [{TIME}] "GET / HTTP/1.1" 200 512 "-" "Mozilla"'
            ' "203.0.113.5, 172.70.1.1"',
            Request("203.0.113.5", JUN_13, "GET", "/", "200"),
            id="proxied-uses-first-x-forwarded-for",
        ),
        pytest.param(
            f'198.51.100.7 - - [{TIME}] "POST /x HTTP/1.1" 404 0 "-" "curl"',
            Request("198.51.100.7", JUN_13, "POST", "/x", "404"),
            id="no-x-forwarded-for",
        ),
        pytest.param(
            f'198.51.100.7 - - [{TIME}] "" 400 0 "-" "-" "-"',
            Request("198.51.100.7", JUN_13, "-", "-", "400"),
            id="empty-request",
        ),
        pytest.param(
            '198.51.100.7 - - [13/Foo/2026:11:40:06 +0000] "GET / HTTP/1.1" 200 1 "-" "-" "-"',
            None,
            id="unparsed-time",
        ),
        pytest.param("not a log line", None, id="unparsed"),
    ],
)
def test_parse_line(line: str, expected: Request | None) -> None:
    assert parse_line(line) == expected


@pytest.mark.parametrize(
    ("text", "expected_exception", "expected"),
    [
        pytest.param(
            "1.2.3.4, 2001:db8:1:2:3:4:5:6,",
            nullcontext(),
            {ip_network("1.2.3.4"), ip_network("2001:db8:1:2::/64")},
            id="ipv6-address-stands-for-its-64",
        ),
        pytest.param(
            "192.0.2.0/24, 2001:db8::1/128",
            nullcontext(),
            {ip_network("192.0.2.0/24"), ip_network("2001:db8::1/128")},
            id="networks-as-given",
        ),
        pytest.param("", nullcontext(), set(), id="empty"),
        pytest.param(
            "1.2.3.4, home",
            pytest.raises(ValueError, match="^'home' isn't an IP address or network$"),
            None,
            id="not-an-address",
        ),
    ],
)
def test_parse_networks(
    text: str, expected_exception: AbstractContextManager[Any], expected: set[Network] | None
) -> None:
    with expected_exception:
        assert parse_networks(text) == expected


def make_response(status: int, body: str) -> requests.Response:
    """A response with ``status`` and ``body``."""
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    return response


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        pytest.param(
            [make_response(200, "1.2.3.4\n"), make_response(200, "2001:db8::1\n")],
            {"1.2.3.4", "2001:db8::1"},
            id="ipv4-and-ipv6",
        ),
        pytest.param(
            [make_response(200, "1.2.3.4\n"), requests.ConnectionError("no IPv6")],
            {"1.2.3.4"},
            id="no-ipv6",
        ),
        pytest.param(
            [make_response(503, "down"), make_response(200, "")], set(), id="error-and-empty"
        ),
    ],
)
def test_detect_public_ips(
    monkeypatch: pytest.MonkeyPatch,
    results: list[requests.Response | Exception],
    expected: set[str],
) -> None:
    monkeypatch.setattr(requests, "get", Mock(side_effect=results))
    assert detect_public_ips() == expected


NOW = datetime(2026, 10, 16, 17, tzinfo=UTC)


@pytest.mark.parametrize(
    ("options", "expected_exception", "expected"),
    [
        pytest.param({}, nullcontext(), (None, None), id="open"),
        pytest.param(
            {"since": datetime(2026, 7, 1)},
            nullcontext(),
            (datetime(2026, 7, 1, tzinfo=UTC), None),
            id="since",
        ),
        pytest.param(
            {"until": datetime(2026, 7, 31)},
            nullcontext(),
            (None, datetime(2026, 8, 1, tzinfo=UTC)),
            id="until-includes-the-day",
        ),
        pytest.param(
            {"month": ""}, nullcontext(), (NOW - timedelta(days=30), None), id="last-30-days"
        ),
        pytest.param(
            {"month": "2026-07"},
            nullcontext(),
            (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)),
            id="year-and-month",
        ),
        pytest.param(
            {"month": "2025-12"},
            nullcontext(),
            (datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
            id="december-ends-next-year",
        ),
        pytest.param(
            {"month": "July"},
            nullcontext(),
            (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)),
            id="name-is-the-latest",
        ),
        pytest.param(
            {"month": "oct"},
            nullcontext(),
            (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
            id="short-name-of-this-month",
        ),
        pytest.param(
            {"month": "November"},
            nullcontext(),
            (datetime(2025, 11, 1, tzinfo=UTC), datetime(2025, 12, 1, tzinfo=UTC)),
            id="name-after-this-month-is-last-years",
        ),
        pytest.param(
            {"month": "Julember"},
            pytest.raises(
                ValueError, match="^--month must be like 2026-07 or July, got 'Julember'$"
            ),
            None,
            id="unknown-month",
        ),
        pytest.param(
            {"year": ""}, nullcontext(), (NOW - timedelta(days=365), None), id="last-365-days"
        ),
        pytest.param(
            {"year": "2025"},
            nullcontext(),
            (datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
            id="calendar-year",
        ),
        pytest.param(
            {"year": "twenty"},
            pytest.raises(ValueError, match="^--year must be a year, e.g. 2026, got 'twenty'$"),
            None,
            id="not-a-year",
        ),
    ],
)
def test_period(
    options: dict[str, Any],
    expected_exception: AbstractContextManager[Any],
    expected: tuple[datetime | None, datetime | None] | None,
) -> None:
    with expected_exception:
        assert period(NOW, **options) == expected


ME = "203.0.113.5"
MINE = {ip_network(ME)}
REQUESTS = [
    Request(ME, datetime(2026, 6, 13, 11, 40, 6, tzinfo=UTC), "GET", "/mine", "200"),
    Request("198.51.100.7", datetime(2026, 6, 13, 12, tzinfo=UTC), "GET", "/", "200"),
    Request("198.51.100.7", datetime(2026, 6, 14, 9, 15, 30, tzinfo=UTC), "GET", "/missing", "404"),
    Request("192.0.2.9", datetime(2026, 6, 14, 10, tzinfo=UTC), "POST", "/", "200"),
]
REPORT_DETAIL = f"""
PythonAnywhere access log — {DOMAIN}
Sources: {LIVE} (+1 rotated)
Lines parsed: 4 (1 unparsed)
Time range: 2026-06-13 11:40 → 2026-06-14 10:00 UTC

Total requests: 4
  You ({ME}): 1  (25.0%)
  Others:           3  (75.0%)
Unique visitor IPs (excluding you): 2

Others — status codes:
  2  200
  1  404

Others — top 2 errors:
  1  404  /missing

Others — top 2 paths:
  2  /
  1  /missing

Others — top 2 IPs:
  2  198.51.100.7
  1  192.0.2.9

Others — most recent 2 requests:
  14/Jun/2026:10:00:00 +0000  192.0.2.9     200  POST /
  14/Jun/2026:09:15:30 +0000  198.51.100.7  404  GET /missing"""
REPORT_NO_LIMIT = f"""
PythonAnywhere access log — {DOMAIN}
Sources: {LIVE}
Lines parsed: 4
Time range: 2026-06-13 11:40 → 2026-06-14 10:00 UTC

Total requests: 4
  You ({ME}): 1  (25.0%)
  Others:           3  (75.0%)
Unique visitor IPs (excluding you): 2

Others — status codes:
  2  200
  1  404

Others — all errors:
  1  404  /missing

Others — all paths:
  2  /
  1  /missing

Others — all IPs:
  2  198.51.100.7
  1  192.0.2.9

Others — all requests, newest first:
  14/Jun/2026:10:00:00 +0000  192.0.2.9     200  POST /
  14/Jun/2026:09:15:30 +0000  198.51.100.7  404  GET /missing
  13/Jun/2026:12:00:00 +0000  198.51.100.7  200  GET /"""
REPORT_EMPTY = f"""
PythonAnywhere access log — {DOMAIN}
Sources: {LIVE}
Lines parsed: 0

Total requests: 0
  You (unknown): 0  (0.0%)
  Others:           0  (0.0%)
Unique visitor IPs (excluding you): 0

Others — status codes:
  (none)

Others — top 2 errors:
  (none)

Others — top 2 paths:
  (none)

Others — top 2 IPs:
  (none)"""


@pytest.mark.parametrize(
    ("logs", "my_networks", "top", "detail", "expected"),
    [
        pytest.param(
            Logs([LIVE, f"{LIVE}.1"], REQUESTS, 1),
            MINE,
            2,
            True,
            REPORT_DETAIL,
            id="yours-others-and-detail",
        ),
        pytest.param(Logs([LIVE], REQUESTS, 0), MINE, None, True, REPORT_NO_LIMIT, id="no-limit"),
        pytest.param(Logs([LIVE], [], 0), set(), 2, False, REPORT_EMPTY, id="no-requests"),
    ],
)
def test_report(
    logs: Logs, my_networks: set[Network], top: int | None, detail: bool, expected: str
) -> None:
    assert report(DOMAIN, logs, my_networks, start=None, end=None, top=top, detail=detail) == (
        expected
    )


V6_REQUESTS = [
    Request(ip, datetime(2026, 6, 13, tzinfo=UTC), "GET", "/", "200")
    for ip in ("2001:db8:1:2::a", "2001:db8:1:2::b", "2001:db8:1:3::a", "203.0.113.5", "-")
]


@pytest.mark.parametrize(
    ("logged", "my_networks", "start", "end", "expected"),
    [
        pytest.param(
            V6_REQUESTS,
            {ip_network("2001:db8:1:2::/64")},
            None,
            None,
            [
                "Lines parsed: 5",
                "Time range: 2026-06-13 00:00 → 2026-06-13 00:00 UTC",
                "",
                "Total requests: 5",
                "  You (2001:db8:1:2::/64): 2  (40.0%)",
                "  Others:           3  (60.0%)",
                "Unique visitor IPs (excluding you): 3",
            ],
            id="your-whole-64",
        ),
        pytest.param(
            V6_REQUESTS,
            {ip_network("2001:db8:1::/48"), ip_network("203.0.113.5")},
            None,
            None,
            [
                "Lines parsed: 5",
                "Time range: 2026-06-13 00:00 → 2026-06-13 00:00 UTC",
                "",
                "Total requests: 5",
                "  You (2001:db8:1::/48, 203.0.113.5): 4  (80.0%)",
                "  Others:           1  (20.0%)",
                "Unique visitor IPs (excluding you): 1",
            ],
            id="your-wider-network-and-ipv4-address",
        ),
        pytest.param(
            REQUESTS,
            MINE,
            datetime(2026, 6, 14, tzinfo=UTC),
            datetime(2026, 6, 14, 10, tzinfo=UTC),
            [
                "Lines parsed: 4",
                "Time range: 2026-06-14 09:15 → 2026-06-14 09:15 UTC",
                "",
                "Total requests: 1",
                f"  You ({ME}): 0  (0.0%)",
                "  Others:           1  (100.0%)",
                "Unique visitor IPs (excluding you): 1",
            ],
            id="period-start-included-end-not",
        ),
        pytest.param(
            REQUESTS,
            MINE,
            datetime(2026, 6, 13, 11, 40, 6, tzinfo=UTC),
            None,
            [
                "Lines parsed: 4",
                "Time range: 2026-06-13 11:40 → 2026-06-14 10:00 UTC",
                "",
                "Total requests: 4",
                f"  You ({ME}): 1  (25.0%)",
                "  Others:           3  (75.0%)",
                "Unique visitor IPs (excluding you): 2",
            ],
            id="logs-going-back-to-start-get-no-note",
        ),
        pytest.param(
            REQUESTS,
            MINE,
            datetime(2026, 6, 1, tzinfo=UTC),
            None,
            [
                "Lines parsed: 4",
                "Time range: 2026-06-13 11:40 → 2026-06-14 10:00 UTC",
                "Note: the logs only go back to 2026-06-13 11:40, not to 2026-06-01 00:00 UTC.",
                "",
                "Total requests: 4",
                f"  You ({ME}): 1  (25.0%)",
                "  Others:           3  (75.0%)",
                "Unique visitor IPs (excluding you): 2",
            ],
            id="start-before-the-logs-gets-a-note",
        ),
    ],
)
def test_report_summary(
    logged: list[Request],
    my_networks: set[Network],
    start: datetime | None,
    end: datetime | None,
    expected: list[str],
) -> None:
    out = report(
        DOMAIN, Logs([LIVE], logged, 0), my_networks, start=start, end=end, top=2, detail=False
    )
    assert out.splitlines()[3 : 3 + len(expected)] == expected
