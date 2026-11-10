"""Command-line interface: a thin layer over config, api, selection, deploy and traffic."""

import logging
import math
import os
import shutil
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Self

import click

from .api import PAClient, PAError, UploadError
from .config import (
    Config,
    ConfigError,
    MissingTokenError,
    check_patterns,
    load_config,
    load_my_ip,
    load_token,
)
from .deploy import (
    UV_FILES,
    backup_folder,
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
from .expiry import describe, is_due
from .manifest import Manifest, ManifestError, changed_files, hash_files
from .selection import (
    SelectionError,
    git_files,
    head_commit,
    include_warnings,
    select_files,
    skipped_commands,
    untracked_files,
)
from .traffic import LOG_DIR, detect_public_ips, parse_networks, period, read_logs, report
from .tree import TreeLine, file_tree

logger = logging.getLogger(__name__)


class _EchoHandler(logging.Handler):
    """Print log messages to stderr with click, under the live tree if there is one."""

    def emit(self, record: logging.LogRecord) -> None:
        if tree := _live_tree:
            tree.echo_below(self.format(record), err=True)
        else:
            click.echo(self.format(record), err=True)


class _Group(click.Group):
    """Report padeploy's errors as click errors: the message and exit status 1, no traceback."""

    def invoke(self, ctx: click.Context) -> Any:
        """Run the command, turning padeploy's errors into ``click.ClickException``."""
        try:
            return super().invoke(ctx)
        except (ConfigError, PAError, SelectionError) as e:
            raise click.ClickException(str(e)) from e


@click.group(cls=_Group)
@click.option("-v", "--verbose", is_flag=True, help="Also show debug output, e.g. each retry.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Deploy and manage PythonAnywhere web apps."""
    ctx.obj = verbose
    logger = logging.getLogger("padeploy")
    if not logger.handlers:
        logger.addHandler(_EchoHandler())


def _load_config(verbose: bool) -> Config:
    """Load the project's config and set padeploy's log level from it (DEBUG with -v)."""
    config = load_config()
    logging.getLogger("padeploy").setLevel(logging.DEBUG if verbose else config.log_level)
    return config


@main.command()
@click.option(
    "--changes", is_flag=True, help="Only tracked files that differ from HEAD, staged or not."
)
@click.option("--staged", is_flag=True, help="Only staged files.")
@click.option("--since", metavar="COMMIT", help="Only files changed between COMMIT and HEAD.")
@click.option(
    "--with", "with_groups", multiple=True, metavar="GROUP", help="Also deploy an opt-in group."
)
@click.option("--only", metavar="GROUP", help="Only deploy this group, and any --with groups.")
@click.option("--code", is_flag=True, help="Same as --only code.")
@click.option(
    "--include", multiple=True, metavar="PATTERNS", help="Comma-separated; added to INCLUDE."
)
@click.option(
    "--exclude", multiple=True, metavar="PATTERNS", help="Comma-separated; added to EXCLUDE."
)
@click.option("--all-files", is_flag=True, help="Upload every selected file, even unchanged ones.")
@click.option(
    "--verify",
    is_flag=True,
    help="Compare the selected files with the server's copies, and upload those that differ.",
)
@click.option(
    "--delete",
    multiple=True,
    is_flag=False,
    flag_value="",
    metavar="[PATHS]",
    help="Delete files from the server that you've deleted locally; with PATHS (comma-separated),"
    " exactly those files. Asks first, and copies them to BACKUP_DIR.",
)
@click.option("-y", "--yes", is_flag=True, help="Delete without asking, e.g. in CI.")
@click.option("--dry-run", is_flag=True, help="List the files without uploading them.")
@click.option("--no-reload", is_flag=True, help="Don't reload the web app after uploading.")
@click.pass_obj
def deploy(
    verbose: bool,
    changes: bool,
    staged: bool,
    since: str | None,
    with_groups: tuple[str, ...],
    only: str | None,
    code: bool,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    all_files: bool,
    verify: bool,
    delete: tuple[str, ...],
    yes: bool,
    dry_run: bool,
    no_reload: bool,
) -> None:
    """Upload the project's files, then reload the web app."""
    if changes + staged + bool(since) > 1:
        raise click.UsageError("Use only one of --changes, --staged and --since.")
    if code and only:
        raise click.UsageError("Use --code or --only, not both.")
    if verify and all_files:
        raise click.UsageError("Use --verify or --all-files, not both.")
    config = _load_config(verbose)
    include_patterns = check_patterns("--include", _split(include), config.project_dir)
    exclude_patterns = check_patterns("--exclude", _split(exclude), config.project_dir)
    named = _delete_paths(delete, config.project_dir)
    only_group = "code" if code else only
    files = select_files(
        config,
        since=since,
        changes=changes,
        staged=staged,
        include=include_patterns,
        exclude=exclude_patterns,
        with_groups=with_groups,
        only=only_group,
    )
    files = [path for path in files if path not in named]  # named for deletion, so not uploaded
    for warning in include_warnings(config, include_patterns, exclude_patterns):
        click.echo(f"Warning: {warning}", err=True)
    if since or changes or staged:
        changed = [
            *git_files(config.project_dir, since=since, changes=changes, staged=staged),
            *untracked_files(config.project_dir),
        ]
        for server_command in skipped_commands(config, changed, with_groups, only_group):
            click.echo(
                f"Warning: {server_command.what} changed but won't be uploaded. Add"
                f" `--with {server_command.group}`, then run `{server_command.command}` on"
                " PythonAnywhere immediately.",
                err=True,
            )
    missing = find_missing(config, files)
    for path in missing:
        click.echo(f"Skipping {path}: not found")
    files = [path for path in files if path not in missing]
    # A bare --delete takes the selection's patterns, but not its git mode.
    removed = (
        partial(
            removed_files,
            config,
            include=include_patterns,
            exclude=exclude_patterns,
            with_groups=with_groups,
            only=only_group,
        )
        if "" in delete
        else None
    )
    if not files and not removed and not named:
        click.echo("No files to deploy.")
    elif dry_run:
        _dry_run(config, files, all_files, verify, removed, named)
    else:
        _deploy(config, files, all_files, verify, no_reload, removed, named, yes)


def _delete_paths(values: tuple[str, ...], project_dir: Path) -> tuple[str, ...]:
    """The files named with ``--delete``, relative to the project.

    Only file paths: a pattern could match more than meant, and deleting a directory on
    PythonAnywhere deletes everything in it.
    """
    paths = check_patterns("--delete", _split(values), project_dir)
    for path in paths:
        is_pattern = any(char in path for char in "*?[")
        if is_pattern or path.endswith("/") or (project_dir / path).is_dir():
            raise click.UsageError(
                f"--delete takes file paths, not patterns or directories, got '{path}'"
            )
    return paths


def _on_server(client: PAClient, config: Config, named: Sequence[str]) -> list[str]:
    """The ``named`` files that are on the server. Raise if any is a directory there, and warn
    about any that aren't there."""
    types = server_types(client, config, named)
    if directories := [path for path in named if types[path] == "directory"]:
        raise click.ClickException(
            "--delete takes files, but these are directories on PythonAnywhere:"
            f" {', '.join(directories)}"
        )
    for path in named:
        if types[path] is None:
            click.echo(f"Warning: --delete {path}: not on PythonAnywhere", err=True)
    return [path for path in named if types[path] == "file"]


def _dry_run(
    config: Config,
    files: list[str],
    all_files: bool,
    verify: bool,
    removed: Callable[[Manifest], list[str]] | None,
    named: Sequence[str],
) -> None:
    """List the files a deploy would upload, and those it would delete: the ``named`` ones, and
    those ``removed`` finds.

    It reads the manifest, compares the server's copies for ``verify``, and checks the ``named``
    files are on the server, only if ``API_TOKEN`` is set, and never prompts for it.
    """
    client = _client_if_token(config)
    manifest = (
        _read_manifest(client, config, verify) if client and (removed or not all_files) else None
    )
    present = _on_server(client, config, named) if client and named else list(named)
    notes: dict[str, str] = {}
    if all_files:
        to_upload = files
    elif client and manifest is not None:
        baseline, notes = (
            _verify(client, config, files, manifest) if verify and files else (manifest, {})
        )
        to_upload = _skip_unchanged(files, hash_files(config.project_dir, files), baseline)
    elif verify:
        click.echo(
            "Warning: without API_TOKEN, a dry run can't compare the files with the server's"
            " copies, so it lists every selected file. A deploy skips those that match.",
            err=True,
        )
        to_upload = files
    else:
        click.echo(
            "Warning: without API_TOKEN, a dry run can't read the manifest, so it lists every"
            " selected file. A deploy skips those unchanged since the last one.",
            err=True,
        )
        to_upload = files
    if to_upload:
        click.echo(f"Would upload {len(to_upload)} file(s) to {config.remote_dir}:")
        _echo_tree(to_upload, notes)
    else:
        click.echo("No files to upload.")
    if removed and manifest is None:
        click.echo(
            "Warning: without API_TOKEN, a dry run can't list the files --delete would remove.",
            err=True,
        )
    found = removed(manifest) if removed and manifest is not None else []
    if to_delete := [*found, *(path for path in present if path not in found)]:
        click.echo(f"Would delete {len(to_delete)} file(s) from {config.remote_dir}:")
        _echo_tree(to_delete)
    if client:
        _warn_if_expiring(config, client)


def _deploy(
    config: Config,
    files: list[str],
    all_files: bool,
    verify: bool,
    no_reload: bool,
    removed: Callable[[Manifest], list[str]] | None,
    named: Sequence[str],
    yes: bool,
) -> None:
    """Upload the ``files`` that changed since the last deploy (all of them with ``all_files``,
    those whose server copies differ with ``verify``), delete the ``named`` files and those
    ``removed`` finds (asking first, unless ``yes``), then reload the web app."""
    client = PAClient(config.user, _token(config.project_dir), config.host)
    manifest = _read_manifest(client, config, verify)
    present = _on_server(client, config, named) if named else []  # checked before changing anything
    hashes = hash_files(config.project_dir, files)
    baseline, notes = (
        _verify(client, config, files, manifest) if verify and files else (manifest, {})
    )
    to_upload = files if all_files else _skip_unchanged(files, hashes, baseline)
    found = removed(manifest) if removed else []
    to_delete = [*found, *(path for path in present if path not in found)]
    if to_delete and not yes:
        _confirm_delete(config, to_delete)
    uploaded = _upload(client, config, to_upload, notes) if to_upload else []
    failed = len(to_upload) - len(uploaded)
    deleted = set(_delete(client, config, to_delete)) if to_delete and not failed else set()
    # Failed files keep their old entries, which still describe the server.
    if uploaded or deleted or baseline != manifest:
        deployed = {**baseline.files, **{path: hashes[path] for path in uploaded if path in hashes}}
        kept = {path: digest for path, digest in deployed.items() if path not in deleted}
        _write_manifest(client, config, Manifest(kept, head_commit(config.project_dir)))
    if failed:
        raise click.ClickException(
            f"{failed} of {len(to_upload)} file(s) failed to upload;"
            f" not {'deleting or ' if to_delete else ''}reloading."
        )
    if not no_reload:  # even with nothing uploaded: the last deploy's reload may have failed
        _reload(client, config.domain)
    _warn_if_expiring(config, client)
    # A failed delete still reloads, since the uploaded code is complete, but exits 1.
    if failed_deletes := len(to_delete) - len(deleted):
        raise click.ClickException(
            f"{failed_deletes} of {len(to_delete)} file(s) failed to delete."
        )


def _read_manifest(client: PAClient, config: Config, verify: bool = False) -> Manifest:
    """The server's manifest; if it can't be read, a warning and an empty one."""
    try:
        return read_manifest(client, config)
    except ManifestError as e:
        # With --verify, the server's copies decide what uploads.
        then = "" if verify else "; uploading every selected file"
        click.echo(f"Warning: {e}{then}.", err=True)
        return Manifest()


def _verify(
    client: PAClient, config: Config, files: list[str], manifest: Manifest
) -> tuple[Manifest, dict[str, str]]:
    """Compare ``files`` with their server copies. Return the ``manifest`` with the copies' real
    hashes for those files, and a note for each copy changed or missing since it was written."""
    click.echo(f"Comparing {len(files)} file(s) with their copies in {config.remote_dir}")
    found = server_hashes(client, config, files)
    selected = set(files)
    others = {path: digest for path, digest in manifest.files.items() if path not in selected}
    notes = {
        path: "changed on PythonAnywhere" if path in found else "missing on PythonAnywhere"
        for path in files
        if path in manifest.files and found.get(path) != manifest.files[path]
    }
    return Manifest({**others, **found}, manifest.commit), notes


def _skip_unchanged(files: list[str], hashes: dict[str, str], manifest: Manifest) -> list[str]:
    """The ``files`` changed since the ``manifest`` was written. Print how many are skipped."""
    changed = changed_files(files, hashes, manifest)
    changed_set = set(changed)
    if unchanged := [path for path in files if path not in changed_set]:
        for path in unchanged:
            logger.debug(f"Unchanged: {path}")
        click.echo(f"Skipping {len(unchanged)} unchanged file(s)")
    return changed


def _label(line: TreeLine, notes: Mapping[str, str] | None) -> str:
    """The line's label, followed by its file's note, if it has one."""
    note = notes.get(line.path) if notes and line.path else None
    return f"{line.label} ({note})" if note else line.label


def _echo_tree(files: Iterable[str], notes: Mapping[str, str] | None = None) -> None:
    """Print ``files`` as a tree, indented under a heading, with any ``notes`` on them."""
    for line in file_tree(files):
        click.echo(f"{'  ' * (line.depth + 1)}{_label(line, notes)}")


def _echo_results(
    files: list[str],
    run: Callable[[list[str]], Iterable[tuple[str, str | None]]],
    notes: Mapping[str, str] | None = None,
) -> Iterator[tuple[str, str | None]]:
    """Call ``run`` with ``files`` in tree order, and yield its (path, error or None) results as
    they come, printing the tree with any of the ``notes`` and each file's result.

    In a terminal the tree fits in, the tree prints first with each file pending, and each is
    marked OK or FAIL in place as its result comes; the errors follow the tree. Otherwise the tree
    prints down to each file once it and the files above it have results, with OK, or FAIL and
    the error, after the file's name.
    """
    lines = file_tree(files)
    size = _terminal_size()
    tree = (
        _LiveTree(lines, notes, size)
        if size and _LiveTree.fits(lines, size)
        else _InOrderTree(lines, notes)
    )
    with tree:
        for path, error in run([line.path for line in lines if line.path is not None]):
            tree.show(path, error)
            yield path, error


class _InOrderTree:
    """Prints the tree down to each file once it and the files above it have results."""

    def __init__(self, lines: list[TreeLine], notes: Mapping[str, str] | None) -> None:
        self.lines = lines
        self.notes = notes
        self.results: dict[str, str | None] = {}
        self.printed = 0  # lines

    def __enter__(self) -> Self:
        """Nothing to print before the first result."""
        return self

    def __exit__(self, *_: object) -> None:
        """Nothing to print after the last result: errors print on their files' lines."""

    def show(self, path: str, error: str | None) -> None:
        """Record ``path``'s result, then print down to the last file that has one, as do all the
        files above it."""
        self.results[path] = error
        end = self.printed
        for i in range(self.printed, len(self.lines)):
            if (file := self.lines[i].path) is not None:
                if file not in self.results:
                    break
                end = i + 1
        for line in self.lines[self.printed : end]:
            text = f"{'  ' * (line.depth + 1)}{_label(line, self.notes)}"
            if line.path is not None:
                error = self.results[line.path]
                text += f"  FAIL: {error}" if error else "  OK"
            click.echo(text)
        self.printed = end


PENDING = "pending"


class _LiveTree:
    """Prints the tree with each file pending, then marks each OK or FAIL in place, by moving the
    cursor with ANSI codes. The errors follow the tree.

    The cursor can only reach rows still on screen, and its moves count rows, not lines. So each
    line is cut to fit one row, and output under the tree goes through ``echo_below``, which
    counts its rows.
    """

    def __init__(
        self, lines: list[TreeLine], notes: Mapping[str, str] | None, size: os.terminal_size
    ) -> None:
        self.lines = lines
        self.notes = notes
        self.size = size
        self.row = {line.path: i for i, line in enumerate(lines) if line.path is not None}
        self.errors: dict[str, str] = {}
        self.below = 0  # rows printed under the tree
        self.lock = threading.Lock()  # results and log messages from other threads

    @staticmethod
    def fits(lines: list[TreeLine], size: os.terminal_size) -> bool:
        """Whether ``lines`` fit on screen with a row for the cursor, each with room for its
        indent, two characters of its name and the widest result."""
        indent = 2 * (max((line.depth for line in lines), default=0) + 1)
        return len(lines) < size.lines and indent + 2 + len(f"  {PENDING}") < size.columns

    def __enter__(self) -> Self:
        """Print the tree with each file pending, and send log messages under it."""
        global _live_tree
        for line in self.lines:
            click.echo(self._text(line, PENDING))
        _live_tree = self
        return self

    def __exit__(self, *_: object) -> None:
        """Stop sending log messages under the tree, then print the errors in tree order."""
        global _live_tree
        with self.lock:
            _live_tree = None
        for line in self.lines:
            if line.path in self.errors:
                click.echo(f"  {line.path}  FAIL: {self.errors[line.path]}")

    def show(self, path: str, error: str | None) -> None:
        """Mark ``path`` OK or FAIL in place, or under the tree once its row has scrolled off."""
        result = "FAIL" if error else "OK"
        with self.lock:
            if error:
                self.errors[path] = error
            up = len(self.lines) - self.row[path] + self.below
            if up < self.size.lines:
                text = self._text(self.lines[self.row[path]], result)
                # One write: Ctrl-C between separate writes would strand the cursor in the tree.
                click.echo(f"\x1b[{up}A\r{text}\x1b[K\x1b[{up}B\r", nl=False, color=True)
            else:
                self._echo_below(f"  {path}  {result}")

    def echo_below(self, message: str, err: bool = False) -> None:
        """Print ``message`` under the tree."""
        with self.lock:
            self._echo_below(message, err)

    def _echo_below(self, message: str, err: bool = False) -> None:
        """Print ``message`` under the tree and count its rows; the caller holds the lock."""
        click.echo(message, err=err)
        columns = self.size.columns
        self.below += sum(max(1, math.ceil(len(part) / columns)) for part in message.split("\n"))

    def _text(self, line: TreeLine, result: str) -> str:
        """``line``, with ``result`` after a file's name, cut to fit one row."""
        indent = "  " * (line.depth + 1)
        label = _label(line, self.notes)
        room = self.size.columns - 1 - len(indent)
        if line.path is not None:
            room -= len(f"  {PENDING}")
        if len(label) > room:
            label = label[: room - 1] + "…"
        return f"{indent}{label}  {result}" if line.path is not None else f"{indent}{label}"


_live_tree: _LiveTree | None = None  # the tree being marked in place, if any


def _upload(
    client: PAClient, config: Config, files: list[str], notes: Mapping[str, str]
) -> list[str]:
    """Upload ``files``, printing each result and any of the ``notes``. Return those uploaded."""
    click.echo(f"Uploading {len(files)} file(s) to {config.remote_dir}")
    uploaded = []
    try:
        for path, error in _echo_results(files, partial(upload_files, client, config), notes):
            if not error:
                uploaded.append(path)
    finally:  # if an upload raises, e.g. a rejected token, earlier ones are on the server anyway
        if uv_files := [path for path in UV_FILES if path in uploaded]:
            click.echo(
                f"Warning: uploaded {' and '.join(uv_files)}. If dependencies changed, run"
                " `uv sync` on PythonAnywhere, then `padeploy reload`.",
                err=True,
            )
        for server_command in server_commands(config, uploaded):
            click.echo(
                f"Warning: uploaded {server_command.what}. If they changed, run"
                f" `{server_command.command}` on PythonAnywhere, then `padeploy reload`.",
                err=True,
            )
    return uploaded


def _confirm_delete(config: Config, files: list[str]) -> None:
    """Ask before deleting ``files``. With no terminal to ask in, e.g. in CI, refuse."""
    if not _interactive():
        raise click.ClickException(
            "deploy asks before deleting files: run it in a terminal, or add --yes."
        )
    click.echo(
        f"Delete {len(files)} file(s) from {config.remote_dir}? Copies go to {config.backup_dir}."
    )
    _echo_tree(files)
    click.confirm("Delete them?", abort=True)


def _delete(client: PAClient, config: Config, files: list[str]) -> list[str]:
    """Copy ``files`` from the server, then delete them, printing each result. Return those
    deleted."""
    backup = backup_folder(config, datetime.now(UTC))
    click.echo(f"Deleting {len(files)} file(s) from {config.remote_dir}, copying them to {backup}")
    run = partial(delete_files, client, config, backup=backup)
    return [path for path, error in _echo_results(files, run) if not error]


def _write_manifest(client: PAClient, config: Config, manifest: Manifest) -> None:
    """Replace the server's manifest; if that fails, only warn: the files are deployed anyway."""
    try:
        write_manifest(client, config, manifest)
    except UploadError as e:
        click.echo(
            f"Warning: couldn't update the manifest: {e}. The next deploy uploads these files"
            " again.",
            err=True,
        )


@main.command()
@click.pass_obj
def reload(verbose: bool) -> None:
    """Reload the web app."""
    config = _load_config(verbose)
    client = PAClient(config.user, _token(config.project_dir), config.host)
    _reload(client, config.domain)
    _warn_if_expiring(config, client)


def _reload(client: PAClient, domain: str) -> None:
    """Reload the web app, then print that it reloaded, or PythonAnywhere's warning."""
    if warning := client.reload(domain):
        click.echo(f"Warning: {warning}", err=True)
    else:
        click.echo(f"Reloaded {domain}")


@main.command()
@click.pass_obj
def expiry(verbose: bool) -> None:
    """Show when the web app expires. Exits 1 if that's close, or past."""
    config = _load_config(verbose)
    client = PAClient(config.user, _token(config.project_dir), config.host)
    expires = client.expiry(config.domain)
    now = datetime.now(UTC)
    if expires is None:
        click.echo(f"{config.domain} doesn't expire.")
        return
    click.echo(describe(config.domain, expires, now))
    if is_due(expires, now):
        click.echo(_extend_hint(config))
        sys.exit(1)


def _client_if_token(config: Config) -> PAClient | None:
    """A client if ``API_TOKEN`` is set, else None. It never prompts, so dry runs can use it."""
    try:
        return PAClient(config.user, load_token(config.project_dir), config.host)
    except ConfigError as e:
        logger.debug(f"Not using the API: {e}")
        return None


def _warn_if_expiring(config: Config, client: PAClient) -> None:
    """Warn if the web app is close to expiring. A failed lookup is only logged."""
    try:
        expires = client.expiry(config.domain)
    except PAError as e:
        logger.debug(f"Couldn't check when {config.domain} expires: {e}")
        return
    now = datetime.now(UTC)
    if expires and is_due(expires, now):
        click.echo(
            f"Warning: {describe(config.domain, expires, now)} {_extend_hint(config)}", err=True
        )


def _extend_hint(config: Config) -> str:
    """Where to extend the web app."""
    return f"Extend it on the Web tab: https://{config.host}/user/{config.user}/webapps/"


def _top(ctx: click.Context, param: click.Parameter, value: str) -> int | None:
    """``--top``'s value: a number of at least 1, or None for ``false`` (no limit)."""
    if value.lower() == "false":
        return None
    if not value.isdigit() or int(value) < 1:
        raise click.BadParameter(f"must be a number of at least 1, or false, got {value!r}")
    return int(value)


@main.command()
@click.option(
    "--my-ip",
    metavar="IPS",
    help="Your IP addresses or networks, comma-separated; IPv6 counts its /64. Overrides MY_IP.",
)
@click.option("--all", "all_logs", is_flag=True, help="Include every rotated log.")
@click.option(
    "--since",
    type=click.DateTime(["%Y-%m-%d"]),
    metavar="DATE",
    help="Only requests from DATE (YYYY-MM-DD, UTC) on.",
)
@click.option(
    "--until",
    type=click.DateTime(["%Y-%m-%d"]),
    metavar="DATE",
    help="Only requests up to and including DATE.",
)
@click.option(
    "--month",
    is_flag=False,
    flag_value="",
    metavar="[MONTH]",
    help="The last 30 days, or a month: 2026-07, or July for the latest July.",
)
@click.option(
    "--year",
    is_flag=False,
    flag_value="",
    metavar="[YEAR]",
    help="The last 365 days, or a calendar year, e.g. 2026.",
)
@click.option("--detail", is_flag=True, help="Also list others' most recent requests.")
@click.option(
    "--top",
    default="10",
    show_default=True,
    metavar="N",
    callback=_top,
    help="Length of the top lists, or false for no limit.",
)
@click.pass_obj
def traffic(
    verbose: bool,
    my_ip: str | None,
    all_logs: bool,
    since: datetime | None,
    until: datetime | None,
    month: str | None,
    year: str | None,
    detail: bool,
    top: int | None,
) -> None:
    """Summarize the web app's access log: your requests and everyone else's."""
    if all_logs + bool(since or until) + (month is not None) + (year is not None) > 1:
        raise click.UsageError("Use only one of --all, --since/--until, --month and --year.")
    now = datetime.now(UTC)
    try:
        start, end = period(now, since=since, until=until, month=month, year=year)
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    config = _load_config(verbose)
    client = PAClient(config.user, _token(config.project_dir), config.host)
    try:
        my_networks = parse_networks(my_ip or load_my_ip(config.project_dir))
    except ValueError as e:
        raise click.ClickException(f"{'--my-ip' if my_ip else 'MY_IP'}: {e}") from e
    if not my_networks:
        detected = detect_public_ips()
        my_networks = parse_networks(",".join(detected))
        if detected:
            click.echo(
                f"Auto-detected your IP(s): {', '.join(sorted(detected))}"
                " (override with --my-ip or MY_IP)."
            )
        else:
            click.echo(
                "Warning: couldn't auto-detect your IP; counting everything as others'."
                " Use --my-ip or MY_IP to split out your traffic.",
                err=True,
            )
    # Every log for --all or a lone --until; else back to the start, or by default to now.
    back_to = None if all_logs or (end and not start) else start or now
    logs = read_logs(client, config.domain, back_to)
    if not logs.names:
        raise click.ClickException(f"No access log found at {LOG_DIR}/{config.domain}.access.log")
    click.echo(
        report(config.domain, logs, my_networks, start=start, end=end, top=top, detail=detail)
    )
    _warn_if_expiring(config, client)


def _split(values: tuple[str, ...]) -> list[str]:
    """Repeated, comma-separated option values as one list."""
    return [pattern for value in values for pattern in value.split(",") if pattern]


def _token(project_dir: Path) -> str:
    """``API_TOKEN`` from config; if it isn't set, a hidden prompt when run in a terminal."""
    try:
        return load_token(project_dir)
    except MissingTokenError:
        if not _interactive():
            raise
        token: str = click.prompt("PythonAnywhere API token", hide_input=True)
        return token


def _interactive() -> bool:
    """Whether stdin is a terminal, so someone can answer a prompt.

    A function of its own so tests can patch it: CliRunner's stdin is never a terminal.
    """
    return sys.stdin.isatty()


def _terminal_size() -> os.terminal_size | None:
    """The terminal's size if stdout and stderr are both one (log messages print under a live
    tree too), else None.

    A function of its own so tests can patch it: CliRunner's output is never a terminal.
    """
    if sys.stdout.isatty() and sys.stderr.isatty():
        return shutil.get_terminal_size()
    return None
