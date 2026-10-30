"""Upload a project's files to PythonAnywhere."""

from collections.abc import Iterable, Iterator, Sequence

from .api import PAClient, UploadError
from .config import Config, Step
from .selection import matches

# The server needs `uv sync` after these change.
UV_FILES = ("pyproject.toml", "uv.lock")


def server_steps(config: Config, uploaded: Sequence[str]) -> list[Step]:
    """The framework's server steps that the ``uploaded`` files call for."""
    return [step for step in config.steps if any(matches(path, step.patterns) for path in uploaded)]


def find_missing(config: Config, files: Iterable[str]) -> list[str]:
    """The ``files`` not on disk, e.g. deleted since ``--since``. Deploys skip them."""
    return [path for path in files if not (config.project_dir / path).is_file()]


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
