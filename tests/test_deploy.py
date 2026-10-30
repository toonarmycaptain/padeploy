from pathlib import Path
from unittest.mock import Mock, call

import pytest

from padeploy.api import PAError, UploadError
from padeploy.config import Config, parse_config
from padeploy.deploy import find_missing, server_steps, upload_files


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
        pytest.param("flask", ["app.py", "static/a.css"], [], id="flask-has-no-steps"),
        pytest.param("django", ["shop/views.py"], [], id="django-code-needs-no-step"),
        pytest.param(
            "django",
            ["shop/migrations/__init__.py"],
            [],
            id="django-migrations-package-needs-no-step",
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
def test_server_steps(framework: str, uploaded: list[str], expected: list[str]) -> None:
    config = parse_config(
        {"USER": "x", "REMOTE_DIR": "/home/x/site", "FRAMEWORK": framework}, Path("/project")
    )
    assert [step.command for step in server_steps(config, uploaded)] == expected


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
