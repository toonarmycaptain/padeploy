"""Load a project's padeploy config and API token."""

import logging
import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, NamedTuple

DEFAULT_HOST = "www.pythonanywhere.com"
DEFAULT_LOG_LEVEL = "WARNING"
TOKEN_KEY = "API_TOKEN"
MY_IP_KEY = "MY_IP"
ENV_PREFIX = "PADEPLOY_"
SECRETS_FILE = ".padeploy_secrets.toml"


class ConfigError(Exception):
    """The project's padeploy configuration is missing or invalid."""


class MissingTokenError(ConfigError):
    """``API_TOKEN`` isn't set."""


@dataclass(frozen=True)
class Group:
    """Named set of path patterns. Groups with ``default=False`` deploy only with ``--with``."""

    paths: tuple[str, ...]
    default: bool = True


class ServerCommand(NamedTuple):
    """A server command that uploading files matching ``patterns`` calls for.

    ``group`` names the opt-in group that holds those files, if there is one.
    """

    what: str
    command: str
    patterns: tuple[str, ...]
    group: str = ""


@dataclass(frozen=True)
class Framework:
    """A web framework's defaults: what to leave out, the groups, and the server commands."""

    exclude: tuple[str, ...]
    groups: Mapping[str, Group]
    server_commands: tuple[ServerCommand, ...] = ()


# Patterns are globs matched against repo-relative paths; a trailing "/" means everything under
# that directory. DEFAULT_EXCLUDE applies to every framework.
DEFAULT_EXCLUDE = (".github/", ".pre-commit-config.yaml", SECRETS_FILE)
DEFAULT_FRAMEWORK = "flask"
_CODE = Group(("**/*.py", "**/templates/"))
FRAMEWORKS: Mapping[str, Framework] = {
    "flask": Framework(
        exclude=("tests/",),
        groups={"code": _CODE, "assets": Group(("static/",))},
    ),
    "django": Framework(
        exclude=("**/tests/", "**/tests.py"),
        groups={
            "code": _CODE,
            "assets": Group(("**/static/",)),
            "migrations": Group(("**/migrations/*.py",), default=False),
        },
        server_commands=(
            ServerCommand(
                "migrations",
                "python manage.py migrate",
                ("**/migrations/[0-9]*.py",),
                group="migrations",
            ),
            ServerCommand("static files", "python manage.py collectstatic", ("**/static/",)),
        ),
    ),
}

# Each config key has one name, used as-is in [tool.padeploy] and .padeploy_secrets.toml, and with
# the PADEPLOY_ prefix in the environment, where lists and tables are TOML values,
# e.g. PADEPLOY_EXCLUDE='["scripts/"]'.
_STR_KEYS = ("USER", "REMOTE_DIR", "DOMAIN", "HOST", "LOG_LEVEL", "FRAMEWORK")
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
    server_commands: tuple[ServerCommand, ...]
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
    for key in (TOKEN_KEY, MY_IP_KEY):
        if key in section:
            raise ConfigError(
                f"{key} can't go in pyproject.toml: set it in {SECRETS_FILE}, or as"
                f" {ENV_PREFIX}{key} in the environment"
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
    name = section.get("FRAMEWORK") or DEFAULT_FRAMEWORK
    if name not in FRAMEWORKS:
        raise ConfigError(f"FRAMEWORK must be one of {', '.join(FRAMEWORKS)}, got {name!r}")
    framework = FRAMEWORKS[name]

    return Config(
        project_dir=project_dir,
        user=section["USER"],
        remote_dir=remote_dir.rstrip("/"),
        domain=section.get("DOMAIN") or f"{section['USER']}.pythonanywhere.com",
        host=section.get("HOST") or DEFAULT_HOST,
        include=_patterns("INCLUDE", section.get("INCLUDE", []), project_dir),
        exclude=DEFAULT_EXCLUDE
        + framework.exclude
        + _patterns("EXCLUDE", section.get("EXCLUDE", []), project_dir),
        groups={**framework.groups, **_groups(section.get("GROUPS", {}), project_dir)},
        server_commands=framework.server_commands,
        log_level=log_level,
    )


def load_token(project_dir: Path) -> str:
    """Return ``API_TOKEN`` from the environment or ``.padeploy_secrets.toml``."""
    if token := _secret(project_dir, TOKEN_KEY):
        return token
    raise MissingTokenError(
        f"padeploy needs {TOKEN_KEY}: set it in {SECRETS_FILE}, or as"
        f" {ENV_PREFIX}{TOKEN_KEY} in the environment"
    )


def load_my_ip(project_dir: Path) -> str:
    """Return ``MY_IP`` from the environment or ``.padeploy_secrets.toml``; empty if unset."""
    return _secret(project_dir, MY_IP_KEY)


def _secret(project_dir: Path, key: str) -> str:
    """``key`` from the environment or ``.padeploy_secrets.toml``; empty if unset."""
    value = os.environ.get(ENV_PREFIX + key) or read_secrets(project_dir).get(key) or ""
    if not isinstance(value, str):
        raise ConfigError(f"{key} in {SECRETS_FILE} must be a string")
    return value


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


def _patterns(where: str, value: Any, project_dir: Path) -> tuple[str, ...]:
    """``value`` as checked patterns (see ``check_patterns``); it must be a list of strings."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f'{where} must be a list of strings, e.g. ["static/"], got {value!r}')
    return check_patterns(where, value, project_dir)


def check_patterns(where: str, patterns: Sequence[str], project_dir: Path) -> tuple[str, ...]:
    """
    ``patterns`` as git writes paths: /-separated and relative to ``project_dir``.

    Paths follow the OS's rules; absolute paths inside the project are made relative to it.
    Empty patterns, and ones outside the project, are errors.
    """
    root = project_dir.resolve()
    checked = []
    for pattern in patterns:
        path = PurePath(pattern)
        # ".." is rejected as written: resolving an absolute path would collapse it.
        if path.is_absolute() and ".." not in path.parts:
            # Resolve symlinks in the parent only, as git tracks a symlink by its own name.
            path = Path(pattern).parent.resolve() / path.name
            if not path.is_relative_to(root):
                raise ConfigError(f"{where} pattern {pattern!r} is outside the project, {root}")
            path = path.relative_to(root)
        if path.anchor or ".." in path.parts or not path.parts:
            raise ConfigError(
                f"{where} patterns must be paths inside the project, without '..', e.g. static/,"
                f" got {pattern!r}"
            )
        relative = path.as_posix()
        checked.append(relative + "/" if pattern.endswith(("/", os.sep)) else relative)
    return tuple(checked)


def _groups(raw: Any, project_dir: Path) -> dict[str, Group]:
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
        paths = _patterns(f"GROUPS.{name}.paths", group["paths"], project_dir)
        groups[name] = Group(paths, group.get("default", True))
    return groups
