"""Command-line interface: a thin layer over config, api, selection, deploy and traffic."""

import logging
import sys
from datetime import UTC, datetime
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
    load_my_ip,
    load_token,
)
from .deploy import UV_FILES, find_missing, server_steps, upload_files
from .expiry import describe, is_due
from .selection import SelectionError, include_warnings, select_files
from .traffic import LOG_DIR, detect_public_ips, parse_networks, period, read_logs, report

logger = logging.getLogger(__name__)


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
    include_patterns = check_patterns("--include", _split(include), config.project_dir)
    exclude_patterns = check_patterns("--exclude", _split(exclude), config.project_dir)
    files = select_files(
        config,
        since=since,
        changes=changes,
        staged=staged,
        include=include_patterns,
        exclude=exclude_patterns,
        with_groups=with_groups,
        only="code" if code else only,
    )
    for warning in include_warnings(config, include_patterns, exclude_patterns):
        click.echo(f"Warning: {warning}", err=True)
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
        _warn_if_expiring(config)
    else:
        client = PAClient(config.user, _token(config.project_dir), config.host)
        _upload(client, config, files)
        if not no_reload:
            _reload(client, config.domain)
        _warn_if_expiring(config, client)


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
        for step in server_steps(config, uploaded):
            click.echo(
                f"Warning: uploaded {step.what}. If they changed, run `{step.command}` on"
                " PythonAnywhere, then `padeploy reload`.",
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


def _warn_if_expiring(config: Config, client: PAClient | None = None) -> None:
    """Warn if the web app is close to expiring. A failed lookup is only logged.

    Without a ``client``, it uses ``API_TOKEN`` if that's set and never prompts for it.
    """
    try:
        client = client or PAClient(config.user, load_token(config.project_dir), config.host)
        expires = client.expiry(config.domain)
    except (ConfigError, PAError) as e:
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
