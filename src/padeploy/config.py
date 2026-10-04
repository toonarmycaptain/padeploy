"""Load a project's padeploy config and API token."""

import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HOST = "www.pythonanywhere.com"
DEFAULT_LOG_LEVEL = "WARNING"
TOKEN_KEY = "API_TOKEN"
ENV_PREFIX = "PADEPLOY_"
SECRETS_FILE = ".padeploy_secrets.toml"


class ConfigError(Exception):
    """The project's padeploy configuration is missing or invalid."""


@dataclass(frozen=True)
class Group:
    """Named set of path patterns. Groups with ``default=False`` deploy only with ``--with``."""

    paths: tuple[str, ...]
    default: bool = True


# Flask-friendly defaults. Patterns are globs matched against repo-relative paths;
# a trailing "/" means everything under that directory.
DEFAULT_EXCLUDE = ("tests/", ".github/", ".pre-commit-config.yaml")
DEFAULT_GROUPS: Mapping[str, Group] = {
    "code": Group(("**/*.py", "**/templates/")),
    "assets": Group(("static/",)),
}

# Each config key has one name, used as-is in [tool.padeploy] and .padeploy_secrets.toml, and with
# the PADEPLOY_ prefix in the environment, where lists and tables are TOML values,
# e.g. PADEPLOY_EXCLUDE='["scripts/"]'.
_STR_KEYS = ("USER", "REMOTE_DIR", "DOMAIN", "HOST", "LOG_LEVEL")
_LIST_KEYS = ("INCLUDE", "EXCLUDE")
_KEYS = (*_STR_KEYS, *_LIST_KEYS, "GROUPS")


@dataclass(frozen=True)
class Config:
    project_dir: Path
    user: str
    remote_dir: str
    domain: str
    host: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    groups: Mapping[str, Group]
    log_level: str


def find_pyproject(start: Path) -> Path:
    """Return the nearest pyproject.toml at or above ``start``."""
    for directory in (start, *start.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    raise ConfigError(f"No pyproject.toml found at or above {start}")


def load_config(start: Path | None = None) -> Config:
    """Load config for the project whose pyproject.toml is nearest at or above ``start`` (or cwd).

    Each config key comes from the environment (``PADEPLOY_<NAME>``), else
    ``.padeploy_secrets.toml``, else ``[tool.padeploy]``.
    """
    pyproject = find_pyproject((start or Path.cwd()).resolve())
    section = _read_toml(pyproject).get("tool", {}).get("padeploy", {})
    if not isinstance(section, Mapping):
        raise ConfigError(f"[tool.padeploy] in {pyproject} must be a table")
    project_dir = pyproject.parent
    secrets = {k: v for k, v in read_secrets(project_dir).items() if k in _KEYS}
    env = {}
    for key in _KEYS:
        if value := os.environ.get(ENV_PREFIX + key):
            env[key] = value if key in _STR_KEYS else _toml_value(ENV_PREFIX + key, value)
    return parse_config({**section, **secrets, **env}, project_dir)


def parse_config(section: Mapping[str, Any], project_dir: Path) -> Config:
    """Validate config keyed as in ``[tool.padeploy]``, and fill in defaults."""
    if TOKEN_KEY in section:
        raise ConfigError(
            f"{TOKEN_KEY} can't go in pyproject.toml: set it in {SECRETS_FILE}, or as"
            f" {ENV_PREFIX}{TOKEN_KEY} in the environment"
        )
    unknown = sorted(set(section) - set(_KEYS))
    if unknown:
        raise ConfigError(f"Unknown config key(s): {', '.join(unknown)}")
    for key in _STR_KEYS:
        if not isinstance(section.get(key, ""), str):
            raise ConfigError(f"{key} must be a string, got {section[key]!r}")
    for key in ("USER", "REMOTE_DIR"):
        if not section.get(key):
            raise ConfigError(
                f"padeploy needs {key}: set it in [tool.padeploy] or {SECRETS_FILE}, or as"
                f" {ENV_PREFIX}{key} in the environment"
            )
    remote_dir = section["REMOTE_DIR"]
    if not remote_dir.startswith("/"):
        raise ConfigError(f"REMOTE_DIR must be an absolute path, got {remote_dir!r}")
    log_level = section.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL
    if log_level not in logging.getLevelNamesMapping():
        raise ConfigError(f"LOG_LEVEL must be a logging level, e.g. DEBUG, got {log_level!r}")

    return Config(
        project_dir=project_dir,
        user=section["USER"],
        remote_dir=remote_dir.rstrip("/"),
        domain=section.get("DOMAIN") or f"{section['USER']}.pythonanywhere.com",
        host=section.get("HOST") or DEFAULT_HOST,
        include=_str_tuple("INCLUDE", section.get("INCLUDE", [])),
        exclude=DEFAULT_EXCLUDE + _str_tuple("EXCLUDE", section.get("EXCLUDE", [])),
        groups={**DEFAULT_GROUPS, **_groups(section.get("GROUPS", {}))},
        log_level=log_level,
    )


def load_token(project_dir: Path) -> str:
    """Return ``API_TOKEN`` from the environment or ``.padeploy_secrets.toml``."""
    token = os.environ.get(ENV_PREFIX + TOKEN_KEY) or read_secrets(project_dir).get(TOKEN_KEY)
    if not token:
        raise ConfigError(
            f"padeploy needs {TOKEN_KEY}: set it in {SECRETS_FILE}, or as"
            f" {ENV_PREFIX}{TOKEN_KEY} in the environment"
        )
    if not isinstance(token, str):
        raise ConfigError(f"{TOKEN_KEY} in {SECRETS_FILE} must be a string")
    return token


def read_secrets(project_dir: Path) -> dict[str, Any]:
    """The project's ``.padeploy_secrets.toml`` TOML file as a dict; empty if there isn't one."""
    secrets = project_dir / SECRETS_FILE
    return _read_toml(secrets) if secrets.is_file() else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e


def _toml_value(name: str, text: str) -> Any:
    """Parse an environment variable written as a TOML value, e.g. ``["scripts/"]``."""
    try:
        return tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{name} must be a TOML value, e.g. ["scripts/"]: {e}') from e


def _str_tuple(where: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f'{where} must be a list of strings, e.g. ["static/"], got {value!r}')
    return tuple(value)


def _groups(raw: Any) -> dict[str, Group]:
    """``{ name = { paths = [...], default = false } }`` → Groups."""
    example = '{ data = { paths = ["data.db"], default = false } }'
    if not isinstance(raw, Mapping):
        raise ConfigError(f"GROUPS must be a table, e.g. {example}")
    groups = {}
    for name, group in raw.items():
        if (
            not isinstance(group, Mapping)
            or "paths" not in group
            or set(group) - {"paths", "default"}
            or not isinstance(group.get("default", True), bool)
        ):
            raise ConfigError(f"GROUPS.{name} must be like {example}, got {group!r}")
        paths = _str_tuple(f"GROUPS.{name}.paths", group["paths"])
        groups[name] = Group(paths, group.get("default", True))
    return groups
