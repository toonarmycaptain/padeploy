"""Upload a project's files to PythonAnywhere."""

from collections.abc import Iterable, Iterator, Sequence

from .api import NotFoundError, PAClient, UploadError
from .config import Config, ServerCommand
from .manifest import MANIFEST_FILE, Manifest, dump_manifest, parse_manifest
from .selection import matches

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
