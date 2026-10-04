import logging
from contextlib import AbstractContextManager, nullcontext
from http import HTTPStatus
from typing import Any
from unittest.mock import Mock, call

import pytest
import requests

from padeploy import __version__
from padeploy.api import MAX_RETRIES, PAClient, PAError

BASE = "https://www.pythonanywhere.com/api/v0/user/bruce"


def make_response(
    status: int, body: str = "", headers: dict[str, str] | None = None
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.reason = HTTPStatus(status).phrase
    response._content = body.encode()
    response.headers.update(headers or {})
    return response


def make_client(
    *results: requests.Response | Exception, max_retries: int = MAX_RETRIES
) -> tuple[PAClient, Mock, list[float]]:
    """
    A client whose session returns (or raises) ``results`` in order, recording sleeps.

    NB replaces self.sleep = time.sleep() with sleep.append() so that it doesn't sleep,
    but accumulates the time slept
    """
    session = Mock(headers={})
    session.request.side_effect = results
    sleeps: list[float] = []
    client = PAClient(
        "bruce", "secret", session=session, max_retries=max_retries, sleep=sleeps.append
    )
    return client, session, sleeps


def test_request_headers_and_url() -> None:
    client, session, _ = make_client(make_response(200))
    client.reload("bruce.pythonanywhere.com")
    assert (session.headers, session.request.call_args_list) == (
        {"Authorization": "Token secret", "User-Agent": f"padeploy/{__version__}"},
        [call("POST", f"{BASE}/webapps/bruce.pythonanywhere.com/reload/", timeout=30.0)],
    )


@pytest.mark.parametrize(
    ("result", "expected_exception", "expected_warning"),
    [
        pytest.param(make_response(200), nullcontext(), None, id="ok"),
        pytest.param(
            make_response(409, '{"error": "cname_error"}'),
            nullcontext(),
            "example.com reloaded, but PythonAnywhere found no CNAME record for it.",
            id="cname-warning",
        ),
        pytest.param(
            make_response(500, "boom"),
            pytest.raises(PAError, match="Reload failed \\(500\\): boom"),
            None,
            id="server-error",
        ),
        pytest.param(
            make_response(403, '{"detail":"You do not have permission to perform this action."}'),
            pytest.raises(PAError, match="Reload failed \\(403\\): .* Check USER is the account"),
            None,
            id="forbidden-hints-user-token-mismatch",
        ),
        pytest.param(
            make_response(500, "<html>error page</html>", {"Content-Type": "text/html"}),
            pytest.raises(
                PAError,
                match="Reload failed \\(500\\): Internal Server Error\\. PythonAnywhere also"
                " returns this when USER doesn't exist\\.$",
            ),
            None,
            id="html-error-page-shows-reason-only",
        ),
        pytest.param(
            make_response(409, "not json"),
            pytest.raises(PAError, match="Reload failed \\(409\\)"),
            None,
            id="409-not-json",
        ),
        pytest.param(
            make_response(409, '["cname_error"]'),
            pytest.raises(PAError, match="Reload failed \\(409\\)"),
            None,
            id="409-json-not-object",
        ),
        pytest.param(
            make_response(401),
            pytest.raises(PAError, match="token rejected"),
            None,
            id="token-rejected",
        ),
        pytest.param(
            requests.ConnectionError("down"),
            pytest.raises(PAError, match="failed: down"),
            None,
            id="connection-error",
        ),
    ],
)
def test_reload(
    result: requests.Response | Exception,
    expected_exception: AbstractContextManager[Any],
    expected_warning: str | None,
) -> None:
    client, _, _ = make_client(result)
    with expected_exception:
        assert client.reload("example.com") == expected_warning


@pytest.mark.parametrize(
    ("max_retries", "statuses", "retry_after", "expected_exception", "expected_sleeps"),
    [
        pytest.param(5, [429, 200], None, nullcontext(), [1], id="retry-once"),
        pytest.param(5, [429, 429, 429, 200], None, nullcontext(), [1, 2, 4], id="backoff-doubles"),
        pytest.param(5, [429, 200], "7", nullcontext(), [7], id="retry-after-seconds"),
        pytest.param(5, [429, 200], "0", nullcontext(), [0], id="retry-after-zero"),
        pytest.param(
            5,
            [429, 200],
            "Wed, 21 Oct 2026 07:28:00 GMT",
            nullcontext(),
            [1],
            id="retry-after-date-falls-back-to-backoff",
        ),
        pytest.param(
            5,
            [429] * 6,
            None,
            pytest.raises(PAError, match="Still rate limited after 5 retries"),
            [1, 2, 4, 8, 16],
            id="gives-up",
        ),
        pytest.param(1, [429, 200], None, nullcontext(), [1], id="last-retry-succeeds"),
        pytest.param(0, [200], None, nullcontext(), [], id="no-retries-ok"),
        pytest.param(
            0,
            [429],
            None,
            pytest.raises(PAError, match="Still rate limited after 0 retries"),
            [],
            id="no-retries-gives-up",
        ),
    ],
)
def test_rate_limit_retries(
    max_retries: int,
    statuses: list[int],
    retry_after: str | None,
    expected_exception: AbstractContextManager[Any],
    expected_sleeps: list[float],
) -> None:
    headers = {"Retry-After": retry_after} if retry_after else {}
    client, session, sleeps = make_client(
        *(make_response(s, headers=headers) for s in statuses), max_retries=max_retries
    )
    with expected_exception:
        client.reload("example.com")
    assert sleeps == expected_sleeps
    assert session.request.call_count == len(statuses)


RETRY_DEBUG = f"429 on POST {BASE}/webapps/example.com/reload/; retry 1 of 5 in"


@pytest.mark.parametrize(
    ("retry_after", "expected_logs"),
    [
        pytest.param(
            "30",
            [
                (logging.WARNING, "Rate limited by PythonAnywhere; retrying in 30s"),
                (logging.DEBUG, f"{RETRY_DEBUG} 30s"),
            ],
            id="retry-after-seconds-is-a-warning",
        ),
        pytest.param(None, [(logging.DEBUG, f"{RETRY_DEBUG} 1s")], id="backoff-is-debug-only"),
        pytest.param(
            "Wed, 21 Oct 2026 07:28:00 GMT",
            [(logging.DEBUG, f"{RETRY_DEBUG} 1s")],
            id="retry-after-date-is-debug-only",
        ),
    ],
)
def test_rate_limit_log_messages(
    caplog: pytest.LogCaptureFixture,
    retry_after: str | None,
    expected_logs: list[tuple[int, str]],
) -> None:
    caplog.set_level(logging.DEBUG, logger="padeploy")
    headers = {"Retry-After": retry_after} if retry_after else {}
    client, _, _ = make_client(make_response(429, headers=headers), make_response(200))
    client.reload("example.com")
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == expected_logs


@pytest.mark.parametrize("max_retries", [-1, -5])
def test_rejects_negative_max_retries(max_retries: int) -> None:
    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        PAClient("bruce", "secret", max_retries=max_retries)
