"""Command-line interface: a thin layer over config, api, selection and deploy."""

import logging
import sys
from pathlib import Path
from typing import Any

import click

from .api import PAClient, PAError
from .config import (
    Config,
    ConfigError,
    MissingTokenError,
    check_patterns,
    load_config,
    load_token,
)
from .deploy import UV_FILES, find_missing, upload_files
from .selection import SelectionError, select_files


class _EchoHandler(logging.Handler):
    """Print log messages to stderr with click."""

    def emit(self, record: logging.LogRecord) -> None:
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
    dry_run: bool,
    no_reload: bool,
) -> None:
    """Upload the project's files, then reload the web app."""
    if changes + staged + bool(since) > 1:
        raise click.UsageError("Use only one of --changes, --staged and --since.")
    if code and only:
        raise click.UsageError("Use --code or --only, not both.")
    config = _load_config(verbose)
    files = select_files(
        config,
        since=since,
        changes=changes,
        staged=staged,
        include=check_patterns("--include", _split(include), config.project_dir),
        exclude=check_patterns("--exclude", _split(exclude), config.project_dir),
        with_groups=with_groups,
        only="code" if code else only,
    )
    missing = find_missing(config, files)
    for path in missing:
        click.echo(f"Skipping {path}: not found")
    files = [path for path in files if path not in missing]
    if not files:
        click.echo("No files to deploy.")
    elif dry_run:
        click.echo(f"Would upload {len(files)} file(s) to {config.remote_dir}:")
        for path in files:
            click.echo(f"  {path}")
    else:
        client = PAClient(config.user, _token(config.project_dir), config.host)
        _upload(client, config, files)
        if not no_reload:
            _reload(client, config.domain)


def _upload(client: PAClient, config: Config, files: list[str]) -> None:
    """Upload ``files``, printing each result. Raise if any failed."""
    click.echo(f"Uploading {len(files)} file(s) to {config.remote_dir}")
    uploaded = []
    failed = 0
    try:
        for path, error in upload_files(client, config, files):
            if error:
                click.echo(f"  FAIL {path}: {error}")
                failed += 1
            else:
                click.echo(f"  OK {path}")
                uploaded.append(path)
    finally:  # if an upload raises, e.g. a rejected token, earlier ones are on the server anyway
        if uv_files := [path for path in uploaded if path in UV_FILES]:
            click.echo(
                f"Warning: uploaded {' and '.join(uv_files)}. If dependencies changed, run"
                " `uv sync` on PythonAnywhere, then `padeploy reload`.",
                err=True,
            )
    if failed:
        raise click.ClickException(
            f"{failed} of {len(files)} file(s) failed to upload; not reloading."
        )


@main.command()
@click.pass_obj
def reload(verbose: bool) -> None:
    """Reload the web app."""
    config = _load_config(verbose)
    _reload(PAClient(config.user, _token(config.project_dir), config.host), config.domain)


def _reload(client: PAClient, domain: str) -> None:
    """Reload the web app, then print that it reloaded, or PythonAnywhere's warning."""
    if warning := client.reload(domain):
        click.echo(f"Warning: {warning}", err=True)
    else:
        click.echo(f"Reloaded {domain}")


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
