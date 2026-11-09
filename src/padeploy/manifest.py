"""The deploy manifest: each deployed file's sha256, kept on the server to skip unchanged files."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MANIFEST_FILE = ".padeploy-manifest.json"


class ManifestError(Exception):
    """The manifest on the server isn't one padeploy can read."""


@dataclass(frozen=True)
class Manifest:
    """The deployed files' sha256 hex digests by path, and the commit checked out at the deploy."""

    files: Mapping[str, str] = field(default_factory=dict)
    commit: str | None = None


def hash_files(project_dir: Path, files: Iterable[str]) -> dict[str, str]:
    """The sha256 hex digest of each of ``files``, leaving out any that can't be read."""
    hashes = {}
    for path in files:
        try:
            hashes[path] = hashlib.sha256((project_dir / path).read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def parse_manifest(data: bytes) -> Manifest:
    """The manifest in ``data``; unknown keys are ignored."""
    try:
        raw = json.loads(data)
    except ValueError as e:
        raise ManifestError(f"{MANIFEST_FILE} isn't valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError(f"{MANIFEST_FILE} isn't a JSON object")
    files = raw.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(path, str) and isinstance(digest, str) for path, digest in files.items()
    ):
        raise ManifestError(f'{MANIFEST_FILE}: "files" isn\'t an object of path: sha256')
    for path in files:  # --delete deletes these paths, so none may point outside REMOTE_DIR
        pure = PurePosixPath(path)
        if not pure.parts or pure.is_absolute() or ".." in pure.parts:
            raise ManifestError(
                f"{MANIFEST_FILE}: {path!r} isn't a relative path inside REMOTE_DIR"
            )
    commit = raw.get("commit")
    if commit is not None and not isinstance(commit, str):
        raise ManifestError(f'{MANIFEST_FILE}: "commit" isn\'t a string or null')
    return Manifest(files, commit)


def dump_manifest(manifest: Manifest) -> bytes:
    """``manifest`` as JSON, indented and sorted so it reads well on the server."""
    raw = {"commit": manifest.commit, "files": dict(manifest.files)}
    return (json.dumps(raw, indent=2, sort_keys=True) + "\n").encode()


def changed_files(files: Iterable[str], hashes: Mapping[str, str], manifest: Manifest) -> list[str]:
    """The ``files`` with no hash in ``hashes``, or whose hash differs from ``manifest``'s."""
    return [
        path for path in files if path not in hashes or manifest.files.get(path) != hashes[path]
    ]
