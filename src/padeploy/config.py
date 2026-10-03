"""Load a project's padeploy settings and API token."""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HOST = "www.pythonanywhere.com"
TOKEN_KEY = "API_TOKEN"
ENV_PREFIX = "PADEPLOY_"
SECRETS_FILE = ".padeploy_secrets"


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

# Each setting has one name, used as-is in [tool.padeploy] and .padeploy_secrets, and with the
# PADEPLOY_ prefix in the environment.
# List and table settings take a TOML value outside pyproject.toml, e.g. EXCLUDE=["x/"].
_KEYS = {"USER", "REMOTE_DIR", "DOMAIN", "HOST", "INCLUDE", "EXCLUDE", "GROUPS"}
_TOML_KEYS = {"INCLUDE", "EXCLUDE", "GROUPS"}
_GROUP_KEYS = {"paths", "default"}


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


def find_pyproject(start: Path) -> Path:
    """Return the nearest pyproject.toml at or above ``start``."""
    for directory in (start, *start.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    raise ConfigError(f"No pyproject.toml found at or above {start}")


def load_config(start: Path | None = None) -> Config:
    """Load settings for the project whose pyproject.toml is nearest at or above ``start`` (or cwd).

    Each setting comes from the environment (``PADEPLOY_<NAME>``), else ``.padeploy_secrets``, else
    ``[tool.padeploy]``.
    """
    pyproject = find_pyproject((start or Path.cwd()).resolve())
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    section = data.get("tool", {}).get("padeploy", {})
    if not isinstance(section, Mapping):
        raise ConfigError(f"[tool.padeploy] in {pyproject} must be a table")
    project_dir = pyproject.parent
    overrides: dict[str, Any] = {}
    for key in _KEYS:
        value = _lookup(project_dir, key)
        if value:
            overrides[key] = _toml_value(key, value) if key in _TOML_KEYS else value
    return parse_config({**section, **overrides}, project_dir)


def parse_config(section: Mapping[str, Any], project_dir: Path) -> Config:
    """Validate a ``[tool.padeploy]`` table and fill in defaults."""
    _reject_unknown(section, _KEYS, "[tool.padeploy]")
    user = _required_str(section, "USER")
    remote_dir = _required_str(section, "REMOTE_DIR")
    if not remote_dir.startswith("/"):
        raise ConfigError(f"REMOTE_DIR must be an absolute path, got {remote_dir!r}")

    raw_groups = section.get("GROUPS", {})
    if not isinstance(raw_groups, Mapping):
        raise ConfigError("GROUPS must be a table")
    groups = dict(DEFAULT_GROUPS)
    for name, raw in raw_groups.items():
        groups[name] = _parse_group(name, raw)

    return Config(
        project_dir=project_dir,
        user=user,
        remote_dir=remote_dir.rstrip("/"),
        domain=_optional_str(section, "DOMAIN") or f"{user}.pythonanywhere.com",
        host=_optional_str(section, "HOST") or DEFAULT_HOST,
        include=_str_list(section, "INCLUDE", "[tool.padeploy]"),
        exclude=DEFAULT_EXCLUDE + _str_list(section, "EXCLUDE", "[tool.padeploy]"),
        groups=groups,
    )


def load_token(project_dir: Path) -> str:
    """Return ``API_TOKEN`` from the environment or ``.padeploy_secrets``."""
    token = _lookup(project_dir, TOKEN_KEY)
    if not token:
        raise ConfigError(
            f"padeploy needs {TOKEN_KEY}: set it in {SECRETS_FILE}, or as"
            f" {ENV_PREFIX}{TOKEN_KEY} in the environment"
        )
    return token


def read_secret(project_dir: Path, key: str) -> str | None:
    """Read ``KEY=value`` from the project's ``.padeploy_secrets`` file; None if absent."""
    secrets = project_dir / SECRETS_FILE
    if not secrets.is_file():
        return None
    for line in secrets.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip() or None
    return None


def _lookup(project_dir: Path, key: str) -> str | None:
    """``PADEPLOY_<key>`` from the environment, else ``key`` from ``.padeploy_secrets``.

    An empty value counts as unset.
    """
    return os.environ.get(ENV_PREFIX + key) or read_secret(project_dir, key)


def _toml_value(key: str, text: str) -> Any:
    """Parse a list or table setting written as a TOML value, e.g. ``["scripts/"]``."""
    try:
        return tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"{key} must be a TOML value, as in pyproject.toml, e.g. "
            f'["scripts/"] or {{ data = {{ paths = ["data.db"] }} }}: {e}'
        ) from e


def _parse_group(name: str, raw: Any) -> Group:
    where = f"GROUPS.{name}"
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a table, e.g. {{ paths = [...] }}")
    _reject_unknown(raw, _GROUP_KEYS, where)
    if "paths" not in raw:
        raise ConfigError(f"{where} needs 'paths'")
    default = raw.get("default", True)
    if not isinstance(default, bool):
        raise ConfigError(f"{where} 'default' must be true or false")
    return Group(_str_list(raw, "paths", where), default)


def _reject_unknown(table: Mapping[str, Any], known: set[str], where: str) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"Unknown key(s) in {where}: {', '.join(unknown)}")


def _required_str(section: Mapping[str, Any], key: str) -> str:
    value = _optional_str(section, key)
    if not value:
        raise ConfigError(
            f"padeploy needs {key}: set it in [tool.padeploy] or {SECRETS_FILE}, or as"
            f" {ENV_PREFIX}{key} in the environment"
        )
    return value


def _optional_str(section: Mapping[str, Any], key: str) -> str | None:
    value = section.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"[tool.padeploy] '{key}' must be a string")
    return value


def _str_list(table: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} '{key}' must be a list of strings")
    return tuple(value)
