from pathlib import Path
from unittest.mock import Mock, call

import pytest

from padeploy.api import NotFoundError, PAError, UploadError
from padeploy.config import Config, parse_config
from padeploy.deploy import (
    find_missing,
    read_manifest,
    server_commands,
    upload_files,
    write_manifest,
)
from padeploy.manifest import Manifest, dump_manifest


def make_project(tmp_path: Path) -> Config:
    (tmp_path / "static").mkdir()
    (tmp_path / "app.py").write_bytes(b"app")
    (tmp_path / "static/style.css").write_bytes(b"css")
    return parse_config({"USER": "x", "REMOTE_DIR": "/home/x/site"}, tmp_path)


def test_find_missing_lists_files_not_on_disk(tmp_path: Path) -> None:
    files = ["app.py", "deleted.py", "static", "static/style.css"]
    assert find_missing(make_project(tmp_path), files) == ["deleted.py", "static"]


@pytest.mark.parametrize(
    ("framework", "uploaded", "expected"),
    [
        pytest.param("flask", ["app.py", "static/a.css"], [], id="flask-has-no-server-commands"),
        pytest.param("django", ["shop/views.py"], [], id="django-code-needs-no-server-command"),
        pytest.param(
            "django",
            ["shop/migrations/__init__.py"],
            [],
            id="django-migrations-package-needs-no-server-command",
        ),
        pytest.param(
            "django",
            ["shop/migrations/0002_add_size.py"],
            ["python manage.py migrate"],
            id="django-migrations",
        ),
        pytest.param(
            "django",
            ["shop/static/shop/site.css", "static/b.css"],
            ["python manage.py collectstatic"],
            id="django-static-files",
        ),
        pytest.param(
            "django",
            ["shop/static/shop/site.css", "shop/migrations/0002_add_size.py"],
            ["python manage.py migrate", "python manage.py collectstatic"],
            id="django-both",
        ),
    ],
)
def test_server_commands(framework: str, uploaded: list[str], expected: list[str]) -> None:
    config = parse_config(
        {"USER": "x", "REMOTE_DIR": "/home/x/site", "FRAMEWORK": framework}, Path("/project")
    )
    assert [
        server_command.command for server_command in server_commands(config, uploaded)
    ] == expected


def test_upload_files_reports_each_file(tmp_path: Path) -> None:
    client = Mock()
    client.upload.side_effect = [None, UploadError("Upload failed (500): boom")]
    files = ["app.py", "static", "static/style.css"]
    results = list(upload_files(client, make_project(tmp_path), files))
    assert (results, client.upload.mock_calls) == (
        [
            ("app.py", None),
            ("static", "Can't read it: Is a directory"),
            ("static/style.css", "Upload failed (500): boom"),
        ],
        [call("/home/x/site/app.py", b"app"), call("/home/x/site/static/style.css", b"css")],
    )


def test_upload_files_raises_api_errors_other_than_refused_uploads(tmp_path: Path) -> None:
    client = Mock()
    client.upload.side_effect = PAError("API token rejected (401). Check API_TOKEN.")
    with pytest.raises(PAError, match="token rejected"):
        list(upload_files(client, make_project(tmp_path), ["app.py", "static/style.css"]))


CONFIG = parse_config({"USER": "x", "REMOTE_DIR": "/home/x/site"}, Path("/project"))
MANIFEST_PATH = "/home/x/site/.padeploy-manifest.json"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(
            NotFoundError(f"Reading {MANIFEST_PATH} failed (404)"), Manifest(), id="none-yet"
        ),
        pytest.param(
            b'{"commit": "abc", "files": {"app.py": "1f"}}',
            Manifest({"app.py": "1f"}, "abc"),
            id="manifest",
        ),
    ],
)
def test_read_manifest_from_remote_dir(result: bytes | Exception, expected: Manifest) -> None:
    client = Mock()
    client.get_file.side_effect = [result]
    assert (read_manifest(client, CONFIG), client.get_file.mock_calls) == (
        expected,
        [call(MANIFEST_PATH)],
    )


def test_write_manifest_uploads_it_to_remote_dir() -> None:
    client = Mock()
    manifest = Manifest({"app.py": "1f"}, "abc")
    write_manifest(client, CONFIG, manifest)
    assert client.upload.mock_calls == [call(MANIFEST_PATH, dump_manifest(manifest))]
