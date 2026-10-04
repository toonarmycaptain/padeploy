"""Command-line interface: a thin layer over config and api."""

import logging

import click

from .api import PAClient, PAError
from .config import Config, ConfigError, load_config, load_token


class _EchoHandler(logging.Handler):
    """Print log messages to stderr with click."""

    def emit(self, record: logging.LogRecord) -> None:
        click.echo(self.format(record), err=True)


@click.group()
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
@click.pass_obj
def reload(verbose: bool) -> None:
    """Reload the web app."""
    try:
        config = _load_config(verbose)
        token = load_token(config.project_dir)
        warning = PAClient(config.user, token, config.host).reload(config.domain)
    except (ConfigError, PAError) as e:
        raise click.ClickException(str(e)) from e
    if warning:
        click.echo(f"Warning: {warning}", err=True)
    else:
        click.echo(f"Reloaded {config.domain}")
