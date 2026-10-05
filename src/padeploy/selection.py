"""Choose the files to deploy: git's candidates, narrowed by include/exclude and groups."""

import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

from .config import Config


class SelectionError(Exception):
    """The files to deploy couldn't be chosen, e.g. git failed or a group doesn't exist."""


def select_files(
    config: Config,
    *,
    since: str | None = None,
    changes: bool = False,
    staged: bool = False,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    with_groups: Sequence[str] = (),
    only: str | None = None,
) -> list[str]:
    """
    The files to deploy, as paths relative to the project directory.

    Candidates are the git-tracked files (see ``git_files``), plus the files in the opt-in groups
    named in ``with_groups`` or ``only`` (see ``opt_in_files``). ``filter_files`` narrows them.
    """
    added, _, _ = _group_patterns(config, with_groups, only)
    files = set(git_files(config.project_dir, since=since, changes=changes, staged=staged))
    if added:
        files.update(opt_in_files(config.project_dir, added))
    return filter_files(
        sorted(files), config, include=include, exclude=exclude, with_groups=with_groups, only=only
    )


def filter_files(
    files: Iterable[str],
    config: Config,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    with_groups: Sequence[str] = (),
    only: str | None = None,
) -> list[str]:
    """
    Narrow ``files`` by include/exclude (config's plus these) and groups.

    - Excludes always win.
    - Files in an opt-in group are left out unless the group is named in ``with_groups`` or
      ``only``. Then they're kept even if they don't match an include.
    - ``only`` keeps just the files in that group and the ``with_groups``.
    """
    added, left_out, kept = _group_patterns(config, with_groups, only)
    include = (*config.include, *include)
    exclude = (*config.exclude, *exclude)

    selected = []
    for path in files:
        if matches(path, exclude) or (only and not matches(path, kept)):
            continue
        if matches(path, added) or (
            not matches(path, left_out) and (not include or matches(path, include))
        ):
            selected.append(path)
    return selected


def _group_patterns(
    config: Config, with_groups: Sequence[str], only: str | None
) -> tuple[list[str], list[str], list[str]]:
    """The patterns of the opt-in groups asked for, the opt-in groups not asked for, and all the
    groups asked for (``with_groups`` and ``only``)."""
    wanted = {*with_groups, *([only] if only else [])}
    if unknown := sorted(wanted - set(config.groups)):
        raise SelectionError(
            f"Unknown group(s): {', '.join(unknown)}. Groups: {', '.join(config.groups)}"
        )
    added: list[str] = []
    left_out: list[str] = []
    kept: list[str] = []
    for name, group in config.groups.items():
        if name in wanted:
            kept += group.paths
        if not group.default:
            (added if name in wanted else left_out).extend(group.paths)
    return added, left_out, kept


def matches(path: str, patterns: Iterable[str]) -> bool:
    """Whether ``path`` matches any of the glob ``patterns``.

    A pattern ending in ``/`` matches everything under the directories it matches.
    """
    pure = PurePosixPath(path)
    for pattern in patterns:
        if pattern.endswith("/"):
            if any(parent.full_match(pattern[:-1]) for parent in pure.parents[:-1]):
                return True
        elif pure.full_match(pattern):
            return True
    return False


def git_files(
    project_dir: Path, *, since: str | None = None, changes: bool = False, staged: bool = False
) -> list[str]:
    """Git-tracked files under ``project_dir``, relative to it.

    All of them by default, or only those changed between ``since`` and HEAD, those that differ
    from HEAD (``changes``: staged or not), or those that are staged (``staged``). These include
    files deleted since.
    """
    if since:
        return _git(project_dir, "diff", "-z", "--name-only", "--relative", since, "HEAD")
    if changes:
        return _git(project_dir, "diff", "-z", "--name-only", "--relative", "HEAD")
    if staged:
        return _git(project_dir, "diff", "-z", "--name-only", "--relative", "--cached")
    return _git(project_dir, "ls-files", "-z")


def opt_in_files(project_dir: Path, patterns: Sequence[str]) -> list[str]:
    """Files under ``project_dir`` for opt-in group ``patterns``, tracked or not, relative to it.

    Patterns match the files git can see: tracked, or untracked and not ignored. So they never
    reach into .venv, .git or other ignored files, even under a directory they name. A gitignored
    file or directory (e.g. a locally built database) is included only when a pattern names it
    exactly, without wildcards; then so is everything in it.
    """
    visible = _git(project_dir, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    found = {path for path in visible if matches(path, patterns)}
    named = [pattern for pattern in patterns if not any(char in pattern for char in "*?[")]
    # check-ignore prints the ignored paths as given, and exits 1 if none are.
    ignored = (
        _git(project_dir, "check-ignore", "-z", "--stdin", stdin="\0".join(named), ok=(0, 1))
        if named
        else []
    )
    for pattern in ignored:
        path = project_dir / pattern
        for file in path.rglob("*") if pattern.endswith("/") else [path]:
            if file.is_file():
                found.add(file.relative_to(project_dir).as_posix())
    return sorted(found)


def _git(project_dir: Path, *args: str, stdin: str = "", ok: tuple[int, ...] = (0,)) -> list[str]:
    """Run git in ``project_dir``; return the paths it prints NUL-separated (with ``-z``).

    ``stdin`` is its input; an exit status not in ``ok`` is a ``SelectionError``.
    """
    try:
        # git prints paths as UTF-8; text=True would assume the locale's encoding, e.g. cp1252.
        result = subprocess.run(
            ["git", *args], cwd=project_dir, input=stdin, capture_output=True, encoding="utf-8"
        )
    except FileNotFoundError as e:
        raise SelectionError("git not found: padeploy deploys the files git tracks") from e
    if result.returncode not in ok:
        raise SelectionError(f"git {args[0]} failed: {result.stderr.strip()}")
    return [path for path in result.stdout.split("\0") if path]
