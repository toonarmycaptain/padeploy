"""Thin client for the PythonAnywhere API: auth, timeouts and rate-limit retries."""

import time
from collections.abc import Callable
from typing import Any

import requests

from . import __version__
from .config import DEFAULT_HOST

MAX_RETRIES = 5
TIMEOUT = 30.0


class PAError(Exception):
    """A PythonAnywhere API call failed."""


class PAClient:
    def __init__(
        self,
        user: str,
        token: str,
        host: str = DEFAULT_HOST,
        *,
        session: requests.Session | None = None,
        timeout: float = TIMEOUT,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self.base_url = f"https://{host}/api/v0/user/{user}"
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Token {token}"
        self.session.headers["User-Agent"] = f"padeploy/{__version__}"
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep

    def reload(self, domain: str) -> str | None:
        """Reload the web app. Returns a warning if it reloaded but has no CNAME record."""
        response = self._request("POST", f"/webapps/{domain}/reload/")
        if response.ok:
            return None
        if response.status_code == 409:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error") == "cname_error":
                return f"{domain} reloaded, but PythonAnywhere found no CNAME record for it."
        raise PAError(f"Reload failed ({response.status_code}): {response.text}")

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Send a request, retrying on 429 (rate limited) with backoff."""
        url = self.base_url + path
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as e:
                raise PAError(f"{method} {url} failed: {e}") from e
            if response.status_code == 401:
                raise PAError("API token rejected (401). Check API_TOKEN.")
            elif response.status_code != 429:
                return response
            elif attempt < self.max_retries:
                try:
                    delay = float(response.headers["Retry-After"])
                except (KeyError, ValueError):  # missing, or the HTTP-date form
                    delay = 2**attempt
                self.sleep(delay)
        raise PAError(f"Still rate limited after {self.max_retries} retries: {method} {url}")
