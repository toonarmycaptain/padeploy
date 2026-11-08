"""Upload a project's files to PythonAnywhere, and delete those removed locally."""

from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath

from .api import DeleteError, NotFoundError, PAClient, PAError, UploadError
from .config import Config, ServerCommand
from .manifest import MANIFEST_FILE, Manifest, dump_manifest, parse_manifest
from .selection import filter_files, matches

# The server needs `uv sync` after these change.
UV_FILES = ("pyproject.toml", "uv.lock")


def server_commands(config: Config, uploaded: Sequence[str]) -> list[ServerCommand]:
    """The framework's server commands that the ``uploaded`` files call for."""
    return [
        server_command
        for server_command in config.server_commands
        if any(matches(path, server_command.patterns) for path in uploaded)
    ]


def find_missing(config: Config, files: Iterable[str]) -> list[str]:
    """The ``files`` not on disk, e.g. deleted since ``--since``. Deploys skip them."""
    return [path for path in files if not (config.project_dir / path).is_file()]


def removed_files(
    config: Config,
    manifest: Manifest,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    with_groups: Sequence[str] = (),
    only: str | None = None,
) -> list[str]:
    """The files ``--delete`` removes: those in the ``manifest`` that are gone locally and that
    the selection's patterns match (see ``filter_files``), except opt-in groups' files.

    Those are often untracked or gitignored, e.g. a database, so a fresh clone doesn't have them:
    gone locally doesn't mean deleted.
    """
    opt_in = [path for group in config.groups.values() if not group.default for path in group.paths]
    gone = [
        path for path in find_missing(config, sorted(manifest.files)) if not matches(path, opt_in)
    ]
    return filter_files(
        gone, config, include=include, exclude=exclude, with_groups=with_groups, only=only
    )


def server_types(client: PAClient, config: Config, paths: Iterable[str]) -> dict[str, str | None]:
    """Each path's type under ``REMOTE_DIR`` (``file`` or ``directory``), or None if it isn't
    there. Each directory is listed once."""
    by_dir: dict[PurePosixPath, list[str]] = {}
    for path in paths:
        by_dir.setdefault(PurePosixPath(path).parent, []).append(path)
    types: dict[str, str | None] = {}
    for directory, in_dir in by_dir.items():
        try:
            entries = client.dir_entries(str(PurePosixPath(config.remote_dir) / directory))
        except NotFoundError:
            entries = {}
        for path in in_dir:
            types[path] = entries.get(PurePosixPath(path).name)
    return types


def read_manifest(client: PAClient, config: Config) -> Manifest:
    """The manifest in ``REMOTE_DIR``, or an empty one if there isn't one yet."""
    try:
        return parse_manifest(client.get_file(f"{config.remote_dir}/{MANIFEST_FILE}"))
    except NotFoundError:
        return Manifest()


def write_manifest(client: PAClient, config: Config, manifest: Manifest) -> None:
    """Upload ``manifest`` to ``REMOTE_DIR``, replacing the one there."""
    client.upload(f"{config.remote_dir}/{MANIFEST_FILE}", dump_manifest(manifest))


def upload_files(
    client: PAClient, config: Config, files: Iterable[str]
) -> Iterator[tuple[str, str | None]]:
    """Upload each file to the same path under ``REMOTE_DIR``; yield (path, error or None).

    Files that can't be read, and per-file refusals, are yielded as errors; other API errors, such
    as a rejected token, are raised.
    """
    for path in files:
        try:
            content = (config.project_dir / path).read_bytes()
        except OSError as e:
            yield path, f"Can't read it: {e.strerror or e}"
            continue
        try:
            client.upload(f"{config.remote_dir}/{path}", content)
        except UploadError as e:
            yield path, str(e)
        else:
            yield path, None


def backup_folder(config: Config, now: datetime) -> Path:
    """Where a deploy at ``now`` (UTC) copies the files it deletes: a folder in ``BACKUP_DIR``."""
    return config.backup_dir / now.strftime("%Y-%m-%dT%H-%M-%SZ")


def delete_files(
    client: PAClient, config: Config, files: Iterable[str], backup: Path
) -> Iterator[tuple[str, str | None]]:
    """Copy each file from under ``REMOTE_DIR`` into ``backup``, then delete it; yield (path,
    error or None).

    A file that's already gone counts as deleted. One that can't be copied, for any reason, isn't
    deleted. Refused deletes are yielded as errors; other API errors, such as a rejected token,
    are raised.
    """
    for path in files:
        remote = f"{config.remote_dir}/{path}"
        try:
            content = client.get_file(remote)
        except NotFoundError:
            yield path, None
            continue
        except PAError as e:
            yield path, f"Can't copy it, so not deleted: {e}"
            continue
        try:
            _keep_out_of_git(config.backup_dir)
            (backup / path).parent.mkdir(parents=True, exist_ok=True)
            (backup / path).write_bytes(content)
        except OSError as e:
            yield path, f"Can't copy it, so not deleted: {e.strerror or e}"
            continue
        try:
            client.delete(remote)
        except DeleteError as e:
            yield path, str(e)
            continue
        except NotFoundError:
            pass
        yield path, None


def _keep_out_of_git(directory: Path) -> None:
    """Create ``directory`` with a .gitignore that ignores everything in it, as uv does for .venv:
    copies of deleted files can include credentials."""
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# Created by padeploy: copies of deleted files\n*\n")
