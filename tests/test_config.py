import os
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from padeploy.config import (
    DEFAULT_EXCLUDE,
    DEFAULT_GROUPS,
    DEFAULT_HOST,
    Config,
    ConfigError,
    Group,
    load_config,
    load_token,
    parse_config,
    read_secrets,
)

PROJECT_DIR = Path("/project")
MINIMAL_TOOL_PADEPLOY: dict[str, Any] = {
    "USER": "arthur",
    "REMOTE_DIR": "/home/arthur/arthur/",
}
MINIMAL_PYPROJECT = """
[tool.padeploy]
USER = "arthur"
REMOTE_DIR = "/home/arthur/arthur/"
"""
DEFAULT_CONFIG = Config(
    project_dir=PROJECT_DIR,
    user="arthur",
    remote_dir="/home/arthur/arthur",
    domain="arthur.pythonanywhere.com",
    host=DEFAULT_HOST,
    include=(),
    exclude=DEFAULT_EXCLUDE,
    groups=DEFAULT_GROUPS,
    log_level="WARNING",
)
NO_PADEPLOY_PYPROJECT = "[project]\nname = 'x'\n"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's or CI's real token and config out of the tests."""
    for name in list(os.environ):
        if name.startswith("PADEPLOY_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize(
    ("tool_padeploy", "expected_exception", "expected_config"),
    [
        pytest.param(MINIMAL_TOOL_PADEPLOY, nullcontext(), DEFAULT_CONFIG, id="defaults"),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "DOMAIN": "arthur.theround.table"},
            nullcontext(),
            replace(DEFAULT_CONFIG, domain="arthur.theround.table"),
            id="domain",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "HOST": "eu.pythonanywhere.com"},
            nullcontext(),
            replace(DEFAULT_CONFIG, host="eu.pythonanywhere.com"),
            id="host",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "INCLUDE": ["app.py", "templates/"]},
            nullcontext(),
            replace(DEFAULT_CONFIG, include=("app.py", "templates/")),
            id="include",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "EXCLUDE": ["scripts/"]},
            nullcontext(),
            replace(DEFAULT_CONFIG, exclude=(*DEFAULT_EXCLUDE, "scripts/")),
            id="exclude-adds-to-defaults",
        ),
        pytest.param(
            {
                **MINIMAL_TOOL_PADEPLOY,
                "GROUPS": {
                    "data": {"paths": ["data.db"], "default": False},
                    "assets": {"paths": ["static/img/"]},
                },
            },
            nullcontext(),
            replace(
                DEFAULT_CONFIG,
                groups={
                    "code": DEFAULT_GROUPS["code"],
                    "assets": Group(("static/img/",)),
                    "data": Group(("data.db",), default=False),
                },
            ),
            id="groups-add-and-override",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "LOG_LEVEL": "DEBUG"},
            nullcontext(),
            replace(DEFAULT_CONFIG, log_level="DEBUG"),
            id="log-level",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "LOG_LEVEL": "loud"},
            pytest.raises(ConfigError, match="LOG_LEVEL must be a logging level, e.g. DEBUG"),
            None,
            id="unknown-log-level",
        ),
        pytest.param(
            {"REMOTE_DIR": "/home/x"},
            pytest.raises(ConfigError, match="needs USER"),
            None,
            id="missing-user",
        ),
        pytest.param(
            {"USER": "x"},
            pytest.raises(ConfigError, match="needs REMOTE_DIR"),
            None,
            id="missing-remote_dir",
        ),
        pytest.param(
            {"USER": "x", "REMOTE_DIR": "x"},
            pytest.raises(ConfigError, match="must be an absolute path"),
            None,
            id="relative-remote_dir",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "usr": "typo"},
            pytest.raises(ConfigError, match="Unknown config key\\(s\\): usr"),
            None,
            id="unknown-key",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "API_TOKEN": "x"},
            pytest.raises(ConfigError, match="API_TOKEN can't go in pyproject.toml"),
            None,
            id="api-token-not-allowed",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "host": "eu.pythonanywhere.com"},
            pytest.raises(ConfigError, match="Unknown config key\\(s\\): host"),
            None,
            id="lowercase-name-is-unknown",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "HOST": 1},
            pytest.raises(ConfigError, match="HOST must be a string, got 1"),
            None,
            id="non-string-host",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "EXCLUDE": "tests/"},
            pytest.raises(ConfigError, match="EXCLUDE must be a list of strings"),
            None,
            id="non-list-exclude",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "EXCLUDE": ["tests/", 1]},
            pytest.raises(ConfigError, match="EXCLUDE must be a list of strings"),
            None,
            id="non-string-in-exclude",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "GROUPS": ["data"]},
            pytest.raises(ConfigError, match="GROUPS must be a table"),
            None,
            id="non-table-groups",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "GROUPS": {"data": ["data.db"]}},
            pytest.raises(ConfigError, match="GROUPS.data must be like"),
            None,
            id="non-table-group",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "GROUPS": {"data": {"default": False}}},
            pytest.raises(ConfigError, match="GROUPS.data must be like"),
            None,
            id="group-missing-paths",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "GROUPS": {"data": {"paths": [], "opt": 1}}},
            pytest.raises(ConfigError, match="GROUPS.data must be like"),
            None,
            id="group-unknown-key",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "GROUPS": {"data": {"paths": [], "default": 0}}},
            pytest.raises(ConfigError, match="GROUPS.data must be like"),
            None,
            id="non-bool-group-default",
        ),
        pytest.param(
            {**MINIMAL_TOOL_PADEPLOY, "GROUPS": {"data": {"paths": [1]}}},
            pytest.raises(ConfigError, match="GROUPS.data.paths must be a list of strings"),
            None,
            id="non-string-group-path",
        ),
    ],
)
def test_parse_config(
    tool_padeploy: dict[str, Any],
    expected_exception: AbstractContextManager[Any],
    expected_config: Config | None,
) -> None:
    with expected_exception:
        assert parse_config(tool_padeploy, PROJECT_DIR) == expected_config


@pytest.mark.parametrize(
    ("pyproject", "secrets", "env", "start", "expected_exception", "expected_changes"),
    [
        pytest.param(MINIMAL_PYPROJECT, None, {}, ".", nullcontext(), {}, id="project-root"),
        pytest.param(MINIMAL_PYPROJECT, None, {}, "src/pkg", nullcontext(), {}, id="subdirectory"),
        pytest.param(
            NO_PADEPLOY_PYPROJECT,
            'USER = "arthur"\nREMOTE_DIR = "/home/arthur/arthur/"\n',
            {},
            ".",
            nullcontext(),
            {},
            id="config-in-secrets-file",
        ),
        pytest.param(
            NO_PADEPLOY_PYPROJECT,
            None,
            {"PADEPLOY_USER": "arthur", "PADEPLOY_REMOTE_DIR": "/home/arthur/arthur/"},
            ".",
            nullcontext(),
            {},
            id="config-in-environment",
        ),
        pytest.param(
            MINIMAL_PYPROJECT,
            'API_TOKEN = "t"\nMY_IP = "1.2.3.4"\n',
            {},
            ".",
            nullcontext(),
            {},
            id="token-and-other-keys-in-secrets-file-ignored",
        ),
        pytest.param(
            MINIMAL_PYPROJECT,
            "USER=arthur\n",
            {},
            ".",
            pytest.raises(ConfigError, match="padeploy_secrets.toml is not valid TOML"),
            {},
            id="secrets-file-not-toml",
        ),
        pytest.param(
            MINIMAL_PYPROJECT,
            'HOST = "eu.pythonanywhere.com"\n',
            {},
            ".",
            nullcontext(),
            {"host": "eu.pythonanywhere.com"},
            id="secrets-file-overrides-pyproject",
        ),
        pytest.param(
            MINIMAL_PYPROJECT,
            'USER = "lancelot"\n',
            {"PADEPLOY_USER": "galahad"},
            ".",
            nullcontext(),
            {"user": "galahad", "domain": "galahad.pythonanywhere.com"},
            id="environment-overrides-secrets-file",
        ),
        pytest.param(
            MINIMAL_PYPROJECT,
            None,
            {"PADEPLOY_EXCLUDE": '["scripts/"]'},
            ".",
            nullcontext(),
            {"exclude": (*DEFAULT_EXCLUDE, "scripts/")},
            id="list-in-environment",
        ),
        pytest.param(
            MINIMAL_PYPROJECT,
            'GROUPS={ data = { paths = ["data.db"], default = false } }\n',
            {},
            ".",
            nullcontext(),
            {"groups": {**DEFAULT_GROUPS, "data": Group(("data.db",), default=False)}},
            id="table-in-secrets-file",
        ),
        pytest.param(
            MINIMAL_PYPROJECT,
            None,
            {"PADEPLOY_EXCLUDE": "scripts/"},
            ".",
            pytest.raises(ConfigError, match="EXCLUDE must be a TOML value"),
            {},
            id="list-in-environment-not-toml",
        ),
        pytest.param(
            None,
            None,
            {},
            ".",
            pytest.raises(ConfigError, match="No pyproject.toml found"),
            {},
            id="no-pyproject",
        ),
        pytest.param(
            NO_PADEPLOY_PYPROJECT,
            None,
            {},
            ".",
            pytest.raises(ConfigError, match="needs USER: .* PADEPLOY_USER"),
            {},
            id="no-config",
        ),
        pytest.param(
            "[tool]\npadeploy = 1\n",
            None,
            {},
            ".",
            pytest.raises(ConfigError, match="\\[tool.padeploy\\] in .* must be a table"),
            {},
            id="padeploy-not-a-table",
        ),
    ],
)
def test_load_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pyproject: str | None,
    secrets: str | None,
    env: dict[str, str],
    start: str,
    expected_exception: AbstractContextManager[Any],
    expected_changes: dict[str, Any],
) -> None:
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    if secrets is not None:
        (tmp_path / ".padeploy_secrets.toml").write_text(secrets)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    (tmp_path / start).mkdir(parents=True, exist_ok=True)
    expected_config = replace(DEFAULT_CONFIG, project_dir=tmp_path, **expected_changes)
    with expected_exception:
        assert load_config(tmp_path / start) == expected_config


MISSING_TOKEN = pytest.raises(ConfigError, match="needs API_TOKEN: .* PADEPLOY_API_TOKEN")


@pytest.mark.parametrize(
    ("env", "secrets", "expected_exception", "expected_token"),
    [
        pytest.param("from-env", None, nullcontext(), "from-env", id="environment"),
        pytest.param(
            None, 'API_TOKEN = "from-file"\n', nullcontext(), "from-file", id="secrets-file"
        ),
        pytest.param(
            "from-env",
            'API_TOKEN = "from-file"\n',
            nullcontext(),
            "from-env",
            id="environment-overrides-secrets-file",
        ),
        pytest.param(
            None,
            '# API_TOKEN = "commented"\n\nAPI_TOKEN = "t"  # note\n',
            nullcontext(),
            "t",
            id="comments-ignored",
        ),
        pytest.param(
            None,
            'API_TOKEN = "abc#def"\n',
            nullcontext(),
            "abc#def",
            id="hash-in-quotes-is-part-of-value",
        ),
        pytest.param(None, None, MISSING_TOKEN, None, id="missing"),
        pytest.param(None, 'API_TOKEN = ""\n', MISSING_TOKEN, None, id="empty"),
        pytest.param(None, 'OTHER = "x"\n', MISSING_TOKEN, None, id="not-in-secrets-file"),
        pytest.param(
            None,
            "API_TOKEN = 123\n",
            pytest.raises(
                ConfigError, match="API_TOKEN in .padeploy_secrets.toml must be a string"
            ),
            None,
            id="non-string",
        ),
    ],
)
def test_load_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    secrets: str | None,
    expected_exception: AbstractContextManager[Any],
    expected_token: str | None,
) -> None:
    if env is not None:
        monkeypatch.setenv("PADEPLOY_API_TOKEN", env)
    if secrets is not None:
        (tmp_path / ".padeploy_secrets.toml").write_text(secrets)
    with expected_exception:
        assert load_token(tmp_path) == expected_token


@pytest.mark.parametrize(
    ("secrets", "expected"),
    [
        pytest.param(None, {}, id="no-file"),
        pytest.param(
            'API_TOKEN = "t"\nMY_IP = "1.2.3.4, ::1"\n',
            {"API_TOKEN": "t", "MY_IP": "1.2.3.4, ::1"},
            id="every-key",
        ),
    ],
)
def test_read_secrets(tmp_path: Path, secrets: str | None, expected: dict[str, Any]) -> None:
    if secrets is not None:
        (tmp_path / ".padeploy_secrets.toml").write_text(secrets)
    assert read_secrets(tmp_path) == expected
