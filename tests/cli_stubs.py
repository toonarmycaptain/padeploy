"""Stubs and shared values for the CLI tests."""

from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest

from padeploy import cli
from padeploy.config import Config, parse_config
from padeploy.manifest import Manifest
from padeploy.traffic import Logs

CONFIG = parse_config({"USER": "arthur", "REMOTE_DIR": "/home/arthur/arthur"}, Path("/project"))


def patch_reload(
    monkeypatch: pytest.MonkeyPatch,
    config: Config | Exception = CONFIG,
    token: str | Exception = "secret",
    reload_result: str | Exception | None = None,
) -> tuple[Mock, Mock]:
    """Stub config and token loading, and the client; returns the token loader and client mocks.

    The client's expiry lookup finds no expiry date.
    """
    token_loader = Mock(side_effect=[token])
    client_class = Mock()
    client_class.return_value.reload.side_effect = [reload_result]
    client_class.return_value.expiry.return_value = None
    monkeypatch.setattr(cli, "load_config", Mock(side_effect=[config]))
    monkeypatch.setattr(cli, "load_token", token_loader)
    monkeypatch.setattr(cli, "PAClient", client_class)
    return token_loader, client_class


APP_OK = ("app.py", None)
COMMIT = "709e179"


def patch_deploy(
    monkeypatch: pytest.MonkeyPatch,
    files: Sequence[str] | Exception = ("app.py",),
    results: Sequence[tuple[str, str | None] | Exception] = (APP_OK,),
    token: str | Exception = "secret",
    missing: Sequence[str] = (),
    config: Config = CONFIG,
) -> tuple[Mock, Mock, Mock]:
    """
    Stub patch_reload functionality, file selection, include warnings, missing-file check,
    the manifest and uploads.

    The server has no manifest yet, and no file has a hash, so every file counts as changed.
    An exception in ``results`` is raised when the uploads reach it.
    Returns the select_files, upload_files and client mocks.
    """

    def uploads(*_: object) -> Iterator[tuple[str, str | None]]:
        for result in results:
            if isinstance(result, Exception):
                raise result
            yield result

    _, client_class = patch_reload(monkeypatch, config, token=token)
    select_files = Mock(side_effect=[files])
    upload_files = Mock(side_effect=uploads)
    monkeypatch.setattr(cli, "select_files", select_files)
    monkeypatch.setattr(cli, "include_warnings", Mock(return_value=[]))
    monkeypatch.setattr(cli, "git_files", Mock(return_value=[]))
    monkeypatch.setattr(cli, "untracked_files", Mock(return_value=[]))
    monkeypatch.setattr(cli, "skipped_commands", Mock(return_value=[]))
    monkeypatch.setattr(cli, "find_missing", Mock(return_value=list(missing)))
    monkeypatch.setattr(cli, "read_manifest", Mock(return_value=Manifest()))
    monkeypatch.setattr(cli, "hash_files", Mock(return_value={}))
    monkeypatch.setattr(cli, "upload_files", upload_files)
    monkeypatch.setattr(cli, "write_manifest", Mock())
    monkeypatch.setattr(cli, "head_commit", Mock(return_value=COMMIT))
    return select_files, upload_files, client_class


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
