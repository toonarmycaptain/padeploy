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


UPLOADING = "Uploading 1 file(s) to /home/arthur/arthur\n  OK app.py\n"
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
            (0, "Would upload 2 file(s) to /home/arthur/arthur:\n  app.py\n  static/a.css\n", ""),
            id="dry-run",
        ),
        pytest.param([], ["app.py"], [APP_OK], (0, UPLOADING + RELOADED, ""), id="ok"),
        pytest.param(
            [],
            ["app.py", "db.py"],
            [APP_OK, ("db.py", "Upload failed (500): boom")],
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  OK app.py\n"
                "  FAIL db.py: Upload failed (500): boom\n",
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
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  OK pyproject.toml\n"
                "  FAIL app.py: Upload failed (500): boom\n",
                SYNC_PYPROJECT + FAILED_1_OF_2,
            ),
            id="uv-warning-survives-a-later-failure",
        ),
        pytest.param(
            [],
            ["pyproject.toml", "app.py"],
            [("pyproject.toml", None), PAError("POST failed: connection reset")],
            (
                1,
                "Uploading 2 file(s) to /home/arthur/arthur\n  OK pyproject.toml\n",
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
    "Uploading 3 file(s) to /home/arthur/arthur\n  OK app.py\n  OK db.py\n  OK static/a.css\n"
)


@pytest.mark.parametrize(
    ("args", "manifest", "results", "expected"),
    [
        pytest.param(
            [],
            OLD,
            [DB_OK, CSS_OK],
            (
                0,
                SKIPPING_1 + UPLOADING_2 + "  OK db.py\n  OK static/a.css\n" + RELOADED,
                "",
                [["db.py", "static/a.css"]],
                [ALL_DEPLOYED],
            ),
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
            [APP_OK, DB_OK, CSS_OK],
            (0, UPLOADING_ALL + RELOADED, "", [FILES], [ALL_DEPLOYED]),
            id="all-files",
        ),
        pytest.param(
            [],
            OLD,
            [("db.py", "Upload failed (500): boom"), CSS_OK],
            (
                1,
                SKIPPING_1 + UPLOADING_2 + "  FAIL db.py: Upload failed (500): boom\n"
                "  OK static/a.css\n",
                FAILED_1_OF_2,
                [["db.py", "static/a.css"]],
                [Manifest({**OLD.files, "static/a.css": "3"}, COMMIT)],
            ),
            id="failed-file-keeps-its-old-entry",
        ),
        pytest.param(
            [],
            OLD,
            [DB_OK, PAError("POST failed: connection reset")],
            (
                1,
                SKIPPING_1 + UPLOADING_2 + "  OK db.py\n",
                "Error: POST failed: connection reset\n",
                [["db.py", "static/a.css"]],
                [],
            ),
            id="api-error-skips-the-write",
        ),
        pytest.param(
            [],
            ManifestError(".padeploy-manifest.json isn't a JSON object"),
            [APP_OK, DB_OK, CSS_OK],
            (
                0,
                UPLOADING_ALL + RELOADED,
                "Warning: .padeploy-manifest.json isn't a JSON object; uploading every selected"
                " file.\n",
                [FILES],
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
                SKIPPING_1 + "Would upload 2 file(s) to /home/arthur/arthur:\n  db.py\n"
                "  static/a.css\n",
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


def test_dry_run_without_an_api_token_still_lists_the_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_deploy(monkeypatch, token=MissingTokenError("padeploy needs API_TOKEN"))
    result = CliRunner().invoke(cli.main, ["deploy", "--dry-run"])
    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        "Would upload 1 file(s) to /home/arthur/arthur:\n  app.py\n",
        "Warning: without API_TOKEN, a dry run can't read the manifest, so it lists every selected"
        " file. A deploy skips those unchanged since the last one.\n",
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
