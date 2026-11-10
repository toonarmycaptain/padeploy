import signal
import subprocess
import sys
import threading
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest

from padeploy.api import DeleteError, NotFoundError, PAError, UploadError
from padeploy.config import Config, parse_config
from padeploy.deploy import (
    THREADS,
    _map_in_threads,
    delete_files,
    find_missing,
    read_manifest,
    removed_files,
    server_commands,
    server_hashes,
    server_types,
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
    def upload(path: str, content: bytes) -> None:
        if path.endswith(".css"):
            raise UploadError("Upload failed (500): boom")

    client = Mock()
    client.upload.side_effect = upload
    files = ["app.py", "static", "static/style.css"]
    results = sorted(upload_files(client, make_project(tmp_path), files))
    assert (results, sorted(c.args for c in client.upload.mock_calls)) == (
        [
            ("app.py", None),
            ("static", "Can't read it: Is a directory"),
            ("static/style.css", "Upload failed (500): boom"),
        ],
        [("/home/x/site/app.py", b"app"), ("/home/x/site/static/style.css", b"css")],
    )


def test_upload_files_raises_api_errors_other_than_refused_uploads(tmp_path: Path) -> None:
    client = Mock()
    client.upload.side_effect = PAError("API token rejected (401). Check API_TOKEN.")
    with pytest.raises(PAError, match="token rejected"):
        list(upload_files(client, make_project(tmp_path), ["app.py", "static/style.css"]))


def test_map_in_threads_runs_threads_calls_at_a_time_yielding_each_as_it_finishes() -> None:
    batch = threading.Barrier(THREADS, timeout=5)  # breaks unless THREADS calls run at once
    rest_of_batch_yielded = threading.Event()
    threads = set()

    def call(n: int) -> int:
        threads.add(threading.current_thread().name)
        batch.wait()
        if n == 0:  # started first, finishes after the rest of its batch
            assert rest_of_batch_yielded.wait(5)
        return n * 10

    items = range(2 * THREADS)
    results = []
    for result in _map_in_threads(call, items):
        results.append(result)
        if len(results) == THREADS - 1:
            rest_of_batch_yielded.set()
    assert (sorted(results), results.index((0, 0)), len(threads)) == (
        [(n, n * 10) for n in items],
        THREADS - 1,
        THREADS,
    )


def test_map_in_threads_starts_no_new_calls_once_one_raises() -> None:
    release = threading.Event()
    started = []

    def call(n: int) -> int:
        started.append(n)
        if n == 0:
            raise ValueError("boom")
        release.wait(5)
        return n

    with pytest.raises(ValueError, match="boom"):
        list(_map_in_threads(call, range(100)))
    release.set()
    for thread in threading.enumerate():
        if thread.name.startswith("padeploy-"):
            thread.join(5)
    # The calls in flight when it raised, and one more on the thread that raised.
    assert len(started) <= THREADS + 1


RATE_LIMITED_UPLOADS = """
import sys
from pathlib import Path
from unittest.mock import Mock

from padeploy.api import PAClient
from padeploy.config import parse_config
from padeploy.deploy import upload_files

def request(*_, **__):
    print("rate limited", flush=True)
    return Mock(status_code=429, headers={"Retry-After": "60"})

client = PAClient("x", "token", session=Mock(headers={}, request=request))
config = parse_config({"USER": "x", "REMOTE_DIR": "/home/x/site"}, Path(sys.argv[1]))
list(upload_files(client, config, ["app.py"]))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="sends SIGINT")
def test_ctrl_c_during_a_rate_limit_wait_exits_without_waiting(tmp_path: Path) -> None:
    make_project(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", RATE_LIMITED_UPLOADS, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == "rate limited\n"
        child.send_signal(signal.SIGINT)
        assert child.wait(timeout=5) == -signal.SIGINT
    finally:
        child.kill()


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


def test_server_hashes_hashes_each_server_copy_leaving_out_missing_ones() -> None:
    copies = {"/home/x/site/app.py": b"app", "/home/x/site/static/a.css": b"css"}

    def get_file(path: str) -> bytes:
        if path not in copies:
            raise NotFoundError(f"Reading {path} failed (404): not found")
        return copies[path]

    client = Mock()
    client.get_file.side_effect = get_file
    assert server_hashes(client, CONFIG, ["app.py", "gone.py", "static/a.css"]) == {
        "app.py": sha256(b"app").hexdigest(),
        "static/a.css": sha256(b"css").hexdigest(),
    }


def test_server_hashes_raises_api_errors_other_than_missing_files() -> None:
    client = Mock()
    client.get_file.side_effect = PAError("Reading /home/x/site/app.py failed (500): boom")
    with pytest.raises(PAError, match="500"):
        server_hashes(client, CONFIG, ["app.py"])


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({}, ["old.py", "static/old.css"], id="default-groups"),
        pytest.param({"only": "code"}, ["old.py"], id="only-code-keeps-static-files"),
        pytest.param(
            {"with_groups": ["data"]},
            ["old.py", "static/old.css"],
            id="with-opt-in-group-never-deletes-it",
        ),
        pytest.param({"only": "data"}, [], id="only-opt-in-group-never-deletes-it"),
        pytest.param({"exclude": ["static/"]}, ["old.py"], id="exclude"),
        pytest.param({"include": ["static/"]}, ["static/old.css"], id="include"),
    ],
)
def test_removed_files_are_manifest_files_gone_locally_that_the_selection_matches(
    tmp_path: Path, kwargs: dict[str, Any], expected: list[str]
) -> None:
    (tmp_path / "app.py").write_bytes(b"app")
    data = {"paths": ["skills.db"], "default": False}
    config = parse_config(
        {"USER": "x", "REMOTE_DIR": "/home/x/site", "GROUPS": {"data": data}}, tmp_path
    )
    deployed = ["app.py", "old.py", "skills.db", "static/old.css", "tests/test_old.py"]
    assert removed_files(config, Manifest(dict.fromkeys(deployed, "1")), **kwargs) == expected


GITIGNORE = b"# Created by padeploy: copies of deleted files\n*\n"
READ_500 = "Reading /home/x/site/d.py failed (500): boom"


def test_delete_files_copies_each_file_before_deleting_it(tmp_path: Path) -> None:
    config = replace(CONFIG, backup_dir=tmp_path / "deleted_from_pa")
    client = Mock()
    client.get_file.side_effect = [
        b"css",
        NotFoundError("Reading /home/x/site/b.py failed (404): not found"),
        b"c",
        PAError(READ_500),
        b"e",
    ]
    client.delete.side_effect = [
        None,
        NotFoundError("Delete failed (404): not found"),
        DeleteError("Delete failed (403): no"),
    ]
    files = ["static/a.css", "b.py", "c.py", "d.py", "e.py"]
    results = list(delete_files(client, config, files, config.backup_dir / "T"))
    copies = {
        path.relative_to(config.backup_dir).as_posix(): path.read_bytes()
        for path in config.backup_dir.rglob("*")
        if path.is_file()
    }
    assert (results, client.delete.mock_calls, copies) == (
        [
            ("static/a.css", None),
            ("b.py", None),  # already gone
            ("c.py", None),  # gone by the time it was deleted
            ("d.py", f"Can't copy it, so not deleted: {READ_500}"),
            ("e.py", "Delete failed (403): no"),
        ],
        [
            call("/home/x/site/static/a.css"),
            call("/home/x/site/c.py"),
            call("/home/x/site/e.py"),
        ],
        {".gitignore": GITIGNORE, "T/static/a.css": b"css", "T/c.py": b"c", "T/e.py": b"e"},
    )


def test_delete_files_does_not_delete_a_file_it_cant_save_a_copy_of(tmp_path: Path) -> None:
    config = replace(CONFIG, backup_dir=tmp_path)
    (tmp_path / "T").write_text("a file where the backup folder should be")
    client = Mock()
    client.get_file.return_value = b"a"
    results = list(delete_files(client, config, ["a.py"], tmp_path / "T"))
    assert (results, client.delete.mock_calls) == (
        [("a.py", "Can't copy it, so not deleted: File exists")],
        [],
    )


def test_delete_files_raises_api_errors_other_than_refused_deletes(tmp_path: Path) -> None:
    client = Mock()
    client.get_file.return_value = b"a"
    client.delete.side_effect = PAError("API token rejected (401). Check API_TOKEN.")
    with pytest.raises(PAError, match="token rejected"):
        list(delete_files(client, replace(CONFIG, backup_dir=tmp_path), ["a.py"], tmp_path / "T"))


def test_server_types_lists_each_directory_once() -> None:
    listings = {
        "/home/x/site": {"app.py": "file", "static": "directory"},
        "/home/x/site/shop/migrations": {"0003_x.py": "file"},
    }

    def dir_entries(path: str) -> dict[str, str]:
        if path not in listings:
            raise NotFoundError(f"Listing {path} failed (404): not found")
        return listings[path]

    client = Mock()
    client.dir_entries.side_effect = dir_entries
    paths = ["app.py", "static", "typo.py", "shop/migrations/0003_x.py", "gone/x.py"]
    assert (server_types(client, CONFIG, paths), client.dir_entries.mock_calls) == (
        {
            "app.py": "file",
            "static": "directory",
            "typo.py": None,
            "shop/migrations/0003_x.py": "file",
            "gone/x.py": None,
        },
        [call("/home/x/site"), call("/home/x/site/shop/migrations"), call("/home/x/site/gone")],
    )
