from pathlib import Path
from unittest.mock import Mock, call

import pytest
from cli_stubs import APP_OK, COMMIT, CONFIG, patch_deploy
from click.testing import CliRunner

from padeploy import cli
from padeploy.api import PAError, UploadError
from padeploy.config import FRAMEWORKS, MissingTokenError, parse_config
from padeploy.manifest import Manifest, ManifestError
from padeploy.selection import SelectionError

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
        pytest.param(["--changes", "--staged"], ONE_OF, id="changes-and-staged"),
        pytest.param(["--since", "abc123", "--changes"], ONE_OF, id="since-and-changes"),
        pytest.param(
            ["--code", "--only", "assets"], "Use --code or --only, not both.", id="code-and-only"
        ),
    ],
)
def test_deploy_rejects_conflicting_options(args: list[str], message: str) -> None:
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (result.exit_code, result.stderr.splitlines()[-1]) == (2, f"Error: {message}")


def test_deploy_uploads_with_client_then_writes_the_manifest_and_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, upload_files, client_class = patch_deploy(monkeypatch)
    read_manifest = Mock(return_value=Manifest())
    write_manifest = Mock()
    monkeypatch.setattr(cli, "read_manifest", read_manifest)
    monkeypatch.setattr(cli, "hash_files", Mock(return_value={"app.py": "1"}))
    monkeypatch.setattr(cli, "write_manifest", write_manifest)
    calls = Mock()
    calls.attach_mock(client_class, "PAClient")
    calls.attach_mock(read_manifest, "read_manifest")
    calls.attach_mock(upload_files, "upload_files")
    calls.attach_mock(write_manifest, "write_manifest")
    CliRunner().invoke(cli.main, ["deploy"])
    client = client_class.return_value
    assert calls.mock_calls == [
        call.PAClient("arthur", "secret", "www.pythonanywhere.com"),
        call.read_manifest(client, CONFIG),
        call.upload_files(client, CONFIG, ["app.py"]),
        call.write_manifest(client, CONFIG, Manifest({"app.py": "1"}, COMMIT)),
        call.PAClient().reload("arthur.pythonanywhere.com"),
        call.PAClient().expiry("arthur.pythonanywhere.com"),
    ]


UPLOADING = "Uploading 1 file(s) to /home/arthur/arthur\n  app.py  OK\n"
RELOADED = "Reloaded arthur.pythonanywhere.com\n"
SYNC_PYPROJECT = (
    "Warning: uploaded pyproject.toml. If dependencies changed, run `uv sync` on PythonAnywhere,"
    " then `padeploy reload`.\n"
)
FAILED_1_OF_2 = "Error: 1 of 2 file(s) failed to upload; not reloading.\n"


@pytest.mark.parametrize(
    ("args", "files", "results", "expected"),
    [
        pytest.param([], [], [], (0, "No files to deploy.\n", ""), id="no-files"),
        pytest.param(
            ["--dry-run"],
            ["app.py", "static/a.css"],
            [],
            (
                0,
                "Would upload 2 file(s) to /home/arthur/arthur:\n  static/\n    a.css\n  app.py\n",
                "",
            ),
            id="dry-run",
        ),
        pytest.param([], ["app.py"], [APP_OK], (0, UPLOADING + RELOADED, ""), id="ok"),
        pytest.param(
            [],
            ["app.py", "db.py"],
            [APP_OK, ("db.py", "Upload failed (500): boom")],
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  app.py  OK\n"
                "  db.py  FAIL: Upload failed (500): boom\n",
                FAILED_1_OF_2,
            ),
            id="failed-upload-exits-1-without-reloading",
        ),
        pytest.param(["--no-reload"], ["app.py"], [APP_OK], (0, UPLOADING, ""), id="no-reload"),
        pytest.param(
            [],
            ["pyproject.toml", "uv.lock"],
            [("pyproject.toml", None), ("uv.lock", None)],
            (
                0,
                "Uploading 2 file(s) to /home/arthur/arthur\n  pyproject.toml  OK\n  uv.lock  OK\n"
                + RELOADED,
                "Warning: uploaded pyproject.toml and uv.lock. If dependencies changed, run"
                " `uv sync` on PythonAnywhere, then `padeploy reload`.\n",
            ),
            id="uv-files-warn-to-sync",
        ),
        pytest.param(
            [],
            ["pyproject.toml", "wsgi.py"],
            [("pyproject.toml", None), ("wsgi.py", "Upload failed (500): boom")],
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  pyproject.toml  OK\n"
                "  wsgi.py  FAIL: Upload failed (500): boom\n",
                SYNC_PYPROJECT + FAILED_1_OF_2,
            ),
            id="uv-warning-survives-a-later-failure",
        ),
        pytest.param(
            [],
            ["pyproject.toml", "wsgi.py"],
            [("pyproject.toml", None), PAError("POST failed: connection reset")],
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  pyproject.toml  OK\n",
                SYNC_PYPROJECT + "Error: POST failed: connection reset\n",
            ),
            id="uv-warning-survives-an-api-error",
        ),
        pytest.param(
            [],
            SelectionError("Unknown group(s): nope"),
            [],
            (1, "", "Error: Unknown group(s): nope\n"),
            id="selection-error-exits-1",
        ),
        pytest.param(
            ["--exclude", "/elsewhere/"],
            ["app.py"],
            [],
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
    expected: tuple[int, str, str],
) -> None:
    patch_deploy(monkeypatch, files, results)
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (result.exit_code, result.stdout, result.stderr) == expected


def test_deploy_uploads_in_tree_order_printing_each_result_after_its_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = ["app.py", "core/data/data.py", "web/config/urls.py", "web/views/views.py"]
    in_tree_order = [files[1], files[2], files[3], files[0]]
    results: list[tuple[str, str | None]] = [(path, None) for path in in_tree_order]
    results[1] = (files[2], "Upload failed (500): boom")
    _, upload_files, _ = patch_deploy(monkeypatch, files, results)
    result = CliRunner().invoke(cli.main, ["deploy"])
    assert (result.stdout, [c.args[2] for c in upload_files.mock_calls]) == (
        "Uploading 4 file(s) to /home/arthur/arthur\n"
        "  core/data/\n"
        "    data.py  OK\n"
        "  web/\n"
        "    config/\n"
        "      urls.py  FAIL: Upload failed (500): boom\n"
        "    views/\n"
        "      views.py  OK\n"
        "  app.py  OK\n",
        [in_tree_order],
    )


FILES = ["app.py", "db.py", "static/a.css"]
HASHES = {"app.py": "1", "db.py": "2", "static/a.css": "3"}
# app.py is unchanged, db.py edited and static/a.css new; gone.py isn't selected.
OLD = Manifest({"app.py": "1", "db.py": "old", "gone.py": "9"}, "abc")
ALL_DEPLOYED = Manifest({**HASHES, "gone.py": "9"}, COMMIT)
DB_OK = ("db.py", None)
CSS_OK = ("static/a.css", None)
SKIPPING_1 = "Skipping 1 unchanged file(s)\n"
SKIPPING_3 = "Skipping 3 unchanged file(s)\n"
UPLOADING_2 = "Uploading 2 file(s) to /home/arthur/arthur\n"
UPLOADING_ALL = (
    "Uploading 3 file(s) to /home/arthur/arthur\n  static/\n    a.css  OK\n  app.py  OK\n"
    "  db.py  OK\n"
)
# The order deploy uploads the changed files in: the tree's, directories first.
CHANGED = ["static/a.css", "db.py"]
CHANGED_OK = "  static/\n    a.css  OK\n  db.py  OK\n"


@pytest.mark.parametrize(
    ("args", "manifest", "results", "expected"),
    [
        pytest.param(
            [],
            OLD,
            [CSS_OK, DB_OK],
            (0, SKIPPING_1 + UPLOADING_2 + CHANGED_OK + RELOADED, "", [CHANGED], [ALL_DEPLOYED]),
            id="skips-unchanged",
        ),
        pytest.param(
            [],
            Manifest(HASHES),
            [],
            (0, SKIPPING_3 + RELOADED, "", [], []),
            id="all-unchanged-still-reloads",
        ),
        pytest.param(
            ["--no-reload"],
            Manifest(HASHES),
            [],
            (0, SKIPPING_3, "", [], []),
            id="all-unchanged-no-reload",
        ),
        pytest.param(
            ["--dry-run"],
            Manifest(HASHES),
            [],
            (0, SKIPPING_3 + "No files to upload.\n", "", [], []),
            id="dry-run-all-unchanged",
        ),
        pytest.param(
            ["--all-files"],
            OLD,
            [CSS_OK, APP_OK, DB_OK],
            (
                0,
                UPLOADING_ALL + RELOADED,
                "",
                [["static/a.css", "app.py", "db.py"]],
                [ALL_DEPLOYED],
            ),
            id="all-files",
        ),
        pytest.param(
            [],
            OLD,
            [CSS_OK, ("db.py", "Upload failed (500): boom")],
            (
                1,
                SKIPPING_1 + UPLOADING_2 + "  static/\n    a.css  OK\n"
                "  db.py  FAIL: Upload failed (500): boom\n",
                FAILED_1_OF_2,
                [CHANGED],
                [Manifest({**OLD.files, "static/a.css": "3"}, COMMIT)],
            ),
            id="failed-file-keeps-its-old-entry",
        ),
        pytest.param(
            [],
            OLD,
            [CSS_OK, PAError("POST failed: connection reset")],
            (
                1,
                SKIPPING_1 + UPLOADING_2 + "  static/\n    a.css  OK\n",
                "Error: POST failed: connection reset\n",
                [CHANGED],
                [],
            ),
            id="api-error-skips-the-write",
        ),
        pytest.param(
            [],
            ManifestError(".padeploy-manifest.json isn't a JSON object"),
            [CSS_OK, APP_OK, DB_OK],
            (
                0,
                UPLOADING_ALL + RELOADED,
                "Warning: .padeploy-manifest.json isn't a JSON object; uploading every selected"
                " file.\n",
                [["static/a.css", "app.py", "db.py"]],
                [Manifest(HASHES, COMMIT)],
            ),
            id="unreadable-manifest-uploads-everything",
        ),
        pytest.param(
            ["--dry-run"],
            OLD,
            [],
            (
                0,
                SKIPPING_1 + "Would upload 2 file(s) to /home/arthur/arthur:\n  static/\n"
                "    a.css\n  db.py\n",
                "",
                [],
                [],
            ),
            id="dry-run-reads-the-manifest",
        ),
    ],
)
def test_deploy_uploads_only_changed_files_and_updates_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    manifest: Manifest | Exception,
    results: list[tuple[str, str | None] | Exception],
    expected: tuple[int, str, str, list[list[str]], list[Manifest]],
) -> None:
    _, upload_files, _ = patch_deploy(monkeypatch, FILES, results)
    write_manifest = Mock()
    monkeypatch.setattr(cli, "read_manifest", Mock(side_effect=[manifest]))
    monkeypatch.setattr(cli, "hash_files", Mock(return_value=HASHES))
    monkeypatch.setattr(cli, "write_manifest", write_manifest)
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (
        result.exit_code,
        result.stdout,
        result.stderr,
        [c.args[2] for c in upload_files.mock_calls],
        [c.args[2] for c in write_manifest.mock_calls],
    ) == expected


def test_deploy_warns_and_still_reloads_if_the_manifest_upload_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_deploy(monkeypatch)
    write_manifest = Mock(side_effect=UploadError("Upload failed (500): boom"))
    monkeypatch.setattr(cli, "write_manifest", write_manifest)
    result = CliRunner().invoke(cli.main, ["deploy"])
    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        UPLOADING + RELOADED,
        "Warning: couldn't update the manifest: Upload failed (500): boom. The next deploy"
        " uploads these files again.\n",
    )


COPYING = ", copying them to /project/deleted_from_pa/T\n"
DELETING = "Deleting 1 file(s) from /home/arthur/arthur" + COPYING


# removed_files is stubbed to find gone.py.
@pytest.mark.parametrize(
    ("args", "files", "manifest", "results", "deletes", "expected"),
    [
        pytest.param(
            ["--delete", "--yes"],
            FILES,
            OLD,
            [CSS_OK, DB_OK],
            [("gone.py", None)],
            (
                0,
                SKIPPING_1 + UPLOADING_2 + CHANGED_OK + DELETING + "  gone.py  OK\n" + RELOADED,
                "",
                [["gone.py"]],
                [Manifest(HASHES, COMMIT)],
            ),
            id="deletes-after-uploading",
        ),
        pytest.param(
            ["--delete", "--yes"],
            [],
            Manifest({**HASHES, "gone.py": "9"}),
            [],
            [("gone.py", None)],
            (
                0,
                DELETING + "  gone.py  OK\n" + RELOADED,
                "",
                [["gone.py"]],
                [Manifest(HASHES, COMMIT)],
            ),
            id="nothing-to-upload-still-deletes",
        ),
        pytest.param(
            ["--delete", "--yes"],
            FILES,
            OLD,
            [CSS_OK, ("db.py", "Upload failed (500): boom")],
            [],
            (
                1,
                SKIPPING_1 + UPLOADING_2 + "  static/\n    a.css  OK\n"
                "  db.py  FAIL: Upload failed (500): boom\n",
                "Error: 1 of 2 file(s) failed to upload; not deleting or reloading.\n",
                [],
                [Manifest({**OLD.files, "static/a.css": "3"}, COMMIT)],
            ),
            id="failed-upload-skips-deletes",
        ),
        pytest.param(
            ["--delete", "--yes"],
            FILES,
            OLD,
            [CSS_OK, DB_OK],
            [("gone.py", "Delete failed (403): no")],
            (
                1,
                SKIPPING_1
                + UPLOADING_2
                + CHANGED_OK
                + DELETING
                + "  gone.py  FAIL: Delete failed (403): no\n"
                + RELOADED,
                "Error: 1 of 1 file(s) failed to delete.\n",
                [["gone.py"]],
                [ALL_DEPLOYED],
            ),
            id="failed-delete-still-reloads-and-exits-1",
        ),
        pytest.param(
            ["--delete", "--dry-run"],
            FILES,
            OLD,
            [],
            [],
            (
                0,
                SKIPPING_1 + "Would upload 2 file(s) to /home/arthur/arthur:\n  static/\n"
                "    a.css\n  db.py\nWould delete 1 file(s) from /home/arthur/arthur:\n  gone.py\n",
                "",
                [],
                [],
            ),
            id="dry-run-lists-deletes",
        ),
    ],
)
def test_deploy_delete_removes_files_gone_locally_after_uploading(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    files: list[str],
    manifest: Manifest,
    results: list[tuple[str, str | None]],
    deletes: list[tuple[str, str | None]],
    expected: tuple[int, str, str, list[list[str]], list[Manifest]],
) -> None:
    patch_deploy(monkeypatch, files, results)
    delete_files = Mock(return_value=iter(deletes))
    write_manifest = Mock()
    monkeypatch.setattr(cli, "read_manifest", Mock(side_effect=[manifest]))
    monkeypatch.setattr(cli, "hash_files", Mock(return_value=HASHES))
    monkeypatch.setattr(cli, "removed_files", Mock(return_value=["gone.py"]))
    monkeypatch.setattr(cli, "delete_files", delete_files)
    monkeypatch.setattr(cli, "write_manifest", write_manifest)
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (
        result.exit_code,
        result.stdout,
        result.stderr,
        [c.args[2] for c in delete_files.mock_calls],
        [c.args[2] for c in write_manifest.mock_calls],
    ) == expected


def test_delete_uses_the_selections_patterns_but_not_its_git_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_deploy(monkeypatch)
    removed_files = Mock(return_value=[])
    monkeypatch.setattr(cli, "removed_files", removed_files)
    args = [
        "--delete",
        "--changes",
        "--code",
        "--with",
        "data",
        "--include",
        "a/",
        "--exclude",
        "b/",
    ]
    CliRunner().invoke(cli.main, ["deploy", *args])
    assert removed_files.mock_calls == [
        call(
            CONFIG, Manifest(), include=("a/",), exclude=("b/",), with_groups=("data",), only="code"
        )
    ]


MIGRATION_3 = "shop/migrations/0003_x.py"
UPLOADING_CSS = "Uploading 1 file(s) to /home/arthur/arthur\n  static/\n    a.css  OK\n"


# OLD's app.py is unchanged; a bare --delete finds gone.py (removed_files is stubbed).
@pytest.mark.parametrize(
    ("args", "types", "results", "deletes", "expected"),
    [
        pytest.param(
            ["--yes", "--delete", "db.py"],
            {"db.py": "file"},
            [CSS_OK],
            [("db.py", None)],
            (
                0,
                SKIPPING_1 + UPLOADING_CSS + DELETING + "  db.py  OK\n" + RELOADED,
                "",
                [["db.py"]],
                [Manifest({"app.py": "1", "static/a.css": "3", "gone.py": "9"}, COMMIT)],
            ),
            id="named-file-is-deleted-not-uploaded",
        ),
        pytest.param(
            ["--yes", "--delete", MIGRATION_3],
            {MIGRATION_3: "file"},
            [CSS_OK, DB_OK],
            [(MIGRATION_3, None)],
            (
                0,
                SKIPPING_1
                + UPLOADING_2
                + CHANGED_OK
                + DELETING
                + "  shop/migrations/\n    0003_x.py  OK\n"
                + RELOADED,
                "",
                [[MIGRATION_3]],
                [ALL_DEPLOYED],
            ),
            id="named-opt-in-file-not-in-the-manifest",
        ),
        pytest.param(
            ["--yes", "--delete", "--delete", "db.py"],
            {"db.py": "file"},
            [CSS_OK],
            [("db.py", None), ("gone.py", None)],
            (
                0,
                SKIPPING_1
                + UPLOADING_CSS
                + "Deleting 2 file(s) from /home/arthur/arthur"
                + COPYING
                + "  db.py  OK\n  gone.py  OK\n"
                + RELOADED,
                "",
                [["db.py", "gone.py"]],
                [Manifest({"app.py": "1", "static/a.css": "3"}, COMMIT)],
            ),
            id="bare-and-named",
        ),
        pytest.param(
            ["--delete", "typo.py"],
            {"typo.py": None},
            [CSS_OK, DB_OK],
            [],
            (
                0,
                SKIPPING_1 + UPLOADING_2 + CHANGED_OK + RELOADED,
                "Warning: --delete typo.py: not on PythonAnywhere\n",
                [],
                [ALL_DEPLOYED],
            ),
            id="not-on-the-server-warns",
        ),
        pytest.param(
            ["--delete", "static"],
            {"static": "directory"},
            [],
            [],
            (
                1,
                "",
                "Error: --delete takes files, but these are directories on PythonAnywhere:"
                " static\n",
                [],
                [],
            ),
            id="server-directory-refused-before-changing-anything",
        ),
        pytest.param(
            ["--dry-run", "--delete", "db.py"],
            {"db.py": "file"},
            [],
            [],
            (
                0,
                SKIPPING_1
                + "Would upload 1 file(s) to /home/arthur/arthur:\n  static/\n    a.css\n"
                "Would delete 1 file(s) from /home/arthur/arthur:\n  db.py\n",
                "",
                [],
                [],
            ),
            id="dry-run",
        ),
    ],
)
def test_deploy_delete_named_files(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    types: dict[str, str | None],
    results: list[tuple[str, str | None]],
    deletes: list[tuple[str, str | None]],
    expected: tuple[int, str, str, list[list[str]], list[Manifest]],
) -> None:
    patch_deploy(monkeypatch, FILES, results)
    delete_files = Mock(return_value=iter(deletes))
    write_manifest = Mock()
    monkeypatch.setattr(cli, "read_manifest", Mock(side_effect=[OLD]))
    monkeypatch.setattr(cli, "hash_files", Mock(return_value=HASHES))
    monkeypatch.setattr(cli, "server_types", Mock(return_value=types))
    monkeypatch.setattr(cli, "removed_files", Mock(return_value=["gone.py"]))
    monkeypatch.setattr(cli, "delete_files", delete_files)
    monkeypatch.setattr(cli, "write_manifest", write_manifest)
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (
        result.exit_code,
        result.stdout,
        result.stderr,
        [c.args[2] for c in delete_files.mock_calls],
        [c.args[2] for c in write_manifest.mock_calls],
    ) == expected


PROMPT = (
    "Delete 1 file(s) from /home/arthur/arthur? Copies go to /project/deleted_from_pa.\n"
    "  gone.py\nDelete them? [y/N]: "
)


@pytest.mark.parametrize(
    ("interactive", "answer", "expected"),
    [
        pytest.param(
            True,
            "y\n",
            (
                0,
                SKIPPING_1
                + PROMPT
                + "y\n"
                + UPLOADING_2
                + CHANGED_OK
                + DELETING
                + "  gone.py  OK\n"
                + RELOADED,
                "",
                [["gone.py"]],
            ),
            id="yes-deletes",
        ),
        pytest.param(
            True,
            "n\n",
            (1, SKIPPING_1 + PROMPT + "n\n", "Aborted!\n", []),
            id="no-changes-nothing",
        ),
        pytest.param(
            False,
            "",
            (
                1,
                SKIPPING_1,
                "Error: deploy asks before deleting files: run it in a terminal, or add --yes.\n",
                [],
            ),
            id="no-terminal-refuses-without-yes",
        ),
    ],
)
def test_deploy_asks_before_deleting(
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
    answer: str,
    expected: tuple[int, str, str, list[list[str]]],
) -> None:
    _, upload_files, _ = patch_deploy(monkeypatch, FILES, [CSS_OK, DB_OK])
    delete_files = Mock(return_value=iter([("gone.py", None)]))
    monkeypatch.setattr(cli, "read_manifest", Mock(return_value=OLD))
    monkeypatch.setattr(cli, "hash_files", Mock(return_value=HASHES))
    monkeypatch.setattr(cli, "removed_files", Mock(return_value=["gone.py"]))
    monkeypatch.setattr(cli, "delete_files", delete_files)
    monkeypatch.setattr(cli, "_interactive", Mock(return_value=interactive))
    result = CliRunner().invoke(cli.main, ["deploy", "--delete"], input=answer)
    assert (
        result.exit_code,
        result.stdout,
        result.stderr,
        [c.args[2] for c in delete_files.mock_calls],
    ) == expected


@pytest.mark.parametrize("path", ["*.py", "static/"], ids=["pattern", "directory"])
def test_delete_rejects_patterns_and_directories(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    patch_deploy(monkeypatch)
    result = CliRunner().invoke(cli.main, ["deploy", "--delete", path])
    assert (result.exit_code, result.stderr.splitlines()[-1]) == (
        2,
        f"Error: --delete takes file paths, not patterns or directories, got '{path}'",
    )


def test_deploy_warns_about_the_frameworks_server_commands(monkeypatch: pytest.MonkeyPatch) -> None:
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


MIGRATION = ["shop/migrations/0002_add_size.py"]
UNTRACKED_MIGRATION = ["shop/migrations/0003_add_color.py"]
CHANGED = [*MIGRATION, *UNTRACKED_MIGRATION]
SKIPPED_MIGRATIONS = (
    "Warning: migrations changed but won't be uploaded. Add `--with migrations`, then run"
    " `python manage.py migrate` on PythonAnywhere immediately.\n"
)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(
            ["--changes", "--with", "assets"],
            (
                [call(Path("/project"), since=None, changes=True, staged=False)],
                [call(CONFIG, CHANGED, ("assets",), None)],
                SKIPPED_MIGRATIONS,
            ),
            id="changes",
        ),
        pytest.param(
            ["--since", "v1", "--code"],
            (
                [call(Path("/project"), since="v1", changes=False, staged=False)],
                [call(CONFIG, CHANGED, (), "code")],
                SKIPPED_MIGRATIONS,
            ),
            id="since-code",
        ),
        pytest.param([], ([], [], ""), id="full-deploy-has-no-baseline"),
    ],
)
def test_deploy_warns_about_changed_files_it_leaves_out(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expected: tuple[list[object], list[object], str],
) -> None:
    patch_deploy(monkeypatch)
    git_files = Mock(return_value=MIGRATION)
    skipped_commands = Mock(return_value=[FRAMEWORKS["django"].server_commands[0]])
    monkeypatch.setattr(cli, "git_files", git_files)
    monkeypatch.setattr(cli, "untracked_files", Mock(return_value=UNTRACKED_MIGRATION))
    monkeypatch.setattr(cli, "skipped_commands", skipped_commands)
    result = CliRunner().invoke(cli.main, ["deploy", *args])
    assert (git_files.mock_calls, skipped_commands.mock_calls, result.stderr) == expected


NO_MANIFEST = (
    "Warning: without API_TOKEN, a dry run can't read the manifest, so it lists every selected"
    " file. A deploy skips those unchanged since the last one.\n"
)


@pytest.mark.parametrize(
    ("args", "expected_stderr"),
    [
        pytest.param([], NO_MANIFEST, id="upload"),
        pytest.param(
            ["--delete"],
            NO_MANIFEST
            + "Warning: without API_TOKEN, a dry run can't list the files --delete would remove.\n",
            id="delete",
        ),
    ],
)
def test_dry_run_without_an_api_token_still_lists_the_files(
    monkeypatch: pytest.MonkeyPatch, args: list[str], expected_stderr: str
) -> None:
    patch_deploy(monkeypatch, token=MissingTokenError("padeploy needs API_TOKEN"))
    result = CliRunner().invoke(cli.main, ["deploy", "--dry-run", *args])
    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        "Would upload 1 file(s) to /home/arthur/arthur:\n  app.py\n",
        expected_stderr,
    )


@pytest.mark.parametrize(
    ("args", "files", "expected"),
    [
        pytest.param(
            [],
            ["app.py", "old.py"],
            (
                "Skipping old.py: not found\nUploading 1 file(s) to /home/arthur/arthur\n"
                "  app.py  OK\n" + RELOADED,
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
