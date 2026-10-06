import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

from padeploy.config import Config, parse_config
from padeploy.selection import SelectionError, filter_files, git_files, matches, select_files


def make_config(project_dir: Path = Path("/project"), **extra: Any) -> Config:
    data_group = {"paths": ["skills.db", "data/"], "default": False}
    return parse_config(
        {"USER": "x", "REMOTE_DIR": "/home/x/x", "GROUPS": {"data": data_group}, **extra},
        project_dir,
    )


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    )


def write(root: Path, *paths: str) -> None:
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(path)


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("app.py", "*.py", True),
        ("pkg/mod.py", "*.py", False),
        ("pkg/mod.py", "**/*.py", True),
        ("app.py", "**/*.py", True),
        ("static/css/a.css", "static/", True),
        ("static", "static/", False),
        ("static/a.css", "static", False),
        ("templates/index.html", "**/templates/", True),
        ("app/templates/index.html", "**/templates/", True),
        ("templates.py", "**/templates/", False),
        ("tests/test_app.py", "tests/", True),
        ("pkg/tests/test_app.py", "tests/", False),
        ("skills.db", "skills.db", True),
    ],
)
def test_matches(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, [pattern]) is expected


FILES = [
    "app.py",
    "pkg/mod.py",
    "templates/index.html",
    "static/style.css",
    "pyproject.toml",
    "tests/test_app.py",
    "scripts/seed.py",
    "skills.db",
    ".padeploy_secrets.toml",
]
NOT_EXCLUDED_OR_DATA = [
    f for f in FILES if f not in ("tests/test_app.py", "skills.db", ".padeploy_secrets.toml")
]
CODE = ["app.py", "pkg/mod.py", "templates/index.html", "scripts/seed.py"]


@pytest.mark.parametrize(
    ("config_extra", "kwargs", "expected"),
    [
        pytest.param(
            {}, {}, NOT_EXCLUDED_OR_DATA, id="default-leaves-out-tests-secrets-and-opt-in-group"
        ),
        pytest.param(
            {"GROUPS": {"secrets": {"paths": [".padeploy_secrets.toml"], "default": False}}},
            {"only": "secrets"},
            [],
            id="secrets-file-never-deploys",
        ),
        pytest.param(
            {},
            {"with_groups": ["data"]},
            [*NOT_EXCLUDED_OR_DATA, "skills.db"],
            id="with-opt-in-group",
        ),
        pytest.param({}, {"only": "code"}, CODE, id="only-code"),
        pytest.param(
            {}, {"only": "code", "with_groups": ["data"]}, [*CODE, "skills.db"], id="only-with"
        ),
        pytest.param({}, {"only": "data"}, ["skills.db"], id="only-opt-in-group"),
        pytest.param(
            {},
            {"only": "code", "exclude": ["scripts/", "pkg/"]},
            ["app.py", "templates/index.html"],
            id="exclude-flag",
        ),
        pytest.param(
            {"EXCLUDE": ["scripts/"]},
            {"only": "code", "exclude": ["pkg/"]},
            ["app.py", "templates/index.html"],
            id="exclude-flag-adds-to-config",
        ),
        pytest.param(
            {},
            {"include": ["app.py", "templates/"]},
            ["app.py", "templates/index.html"],
            id="include-flag",
        ),
        pytest.param(
            {"INCLUDE": ["app.py"]},
            {"include": ["templates/"]},
            ["app.py", "templates/index.html"],
            id="include-flag-adds-to-config",
        ),
        pytest.param(
            {"INCLUDE": ["app.py"]},
            {"with_groups": ["data"]},
            ["app.py", "skills.db"],
            id="opt-in-group-needs-no-include",
        ),
        pytest.param(
            {},
            {"with_groups": ["data"], "exclude": ["*.db"]},
            NOT_EXCLUDED_OR_DATA,
            id="exclude-beats-opt-in-group",
        ),
        pytest.param({"INCLUDE": ["app.py", "tests/"]}, {}, ["app.py"], id="exclude-beats-include"),
    ],
)
def test_filter_files(
    config_extra: dict[str, Any], kwargs: dict[str, Any], expected: list[str]
) -> None:
    assert filter_files(FILES, make_config(**config_extra), **kwargs) == expected


@pytest.mark.parametrize(
    "kwargs", [{"with_groups": ["nope"]}, {"only": "nope"}], ids=["with", "only"]
)
def test_unknown_group_raises(kwargs: dict[str, Any]) -> None:
    with pytest.raises(SelectionError, match="Unknown group\\(s\\): nope. Groups: code, assets"):
        filter_files(FILES, make_config(), **kwargs)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A project in site/ of a repo, with a commit tagged ``first`` and work in progress.

    ``rm_cached.py`` and ``rm_cached_staged.py`` are removed from git but kept on disk.
    """
    git(tmp_path, "init")
    write(tmp_path, "README.md", "site/app.py", "site/db.py", "site/old.py")
    write(tmp_path, "site/rm_cached.py", "site/rm_cached_staged.py")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "first")
    git(tmp_path, "tag", "first")
    (tmp_path / "site/db.py").write_text("changed")
    (tmp_path / "site/old.py").unlink()
    write(tmp_path, "site/new.py")
    (tmp_path / "README.md").write_text("changed")
    git(tmp_path, "add", "-A")
    git(tmp_path, "rm", "--cached", "site/rm_cached.py")
    git(tmp_path, "commit", "-m", "second")
    (tmp_path / "site/app.py").write_text("changed, unstaged")
    (tmp_path / "README.md").write_text("changed again")
    write(tmp_path, "site/staged.py", "site/untracked.py")
    git(tmp_path, "add", "site/staged.py")
    git(tmp_path, "rm", "--cached", "site/rm_cached_staged.py")
    return tmp_path / "site"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({}, ["app.py", "db.py", "new.py", "staged.py"], id="all-tracked"),
        pytest.param({"since": "first"}, ["db.py", "new.py"], id="since"),
        pytest.param({"changes": True}, ["app.py", "staged.py"], id="changes"),
        pytest.param({"staged": True}, ["staged.py"], id="staged"),
    ],
)
def test_git_files(project_dir: Path, kwargs: dict[str, Any], expected: list[str]) -> None:
    assert git_files(project_dir, **kwargs) == expected


@pytest.mark.parametrize(
    ("in_repo", "since", "expected"),
    [
        (True, "nope", pytest.raises(SelectionError, match="git diff failed: fatal: .*nope")),
        (False, None, pytest.raises(SelectionError, match="git ls-files failed: fatal: not a git")),
    ],
    ids=["bad-since", "not-a-repo"],
)
def test_git_failure_raises(
    project_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
    in_repo: bool,
    since: str | None,
    expected: AbstractContextManager[Any],
) -> None:
    with expected:
        git_files(project_dir if in_repo else tmp_path_factory.mktemp("no_repo"), since=since)


@pytest.mark.parametrize(
    ("with_groups", "expected"),
    [
        pytest.param([], [".gitignore", "app.py"], id="no-opt-in-groups"),
        pytest.param(
            ["data"],
            [".gitignore", "app.py", "data/sub/y.csv", "data/x.csv", "skills.db"],
            id="ignored-files-named-exactly-are-added",
        ),
        pytest.param(
            ["images"],
            [".gitignore", "app.py", "static/new.png"],
            id="wildcards-skip-ignored-files",
        ),
        pytest.param(
            ["everything"],
            [".gitignore", "app.py", "notes.txt", "scripts/seed.py", "static/new.png"],
            id="wildcards-skip-ignored-files-and-git-dir",
        ),
        pytest.param(
            ["tools"],
            [".gitignore", "app.py", "scripts/seed.py"],
            id="named-dir-not-ignored-skips-ignored-files-in-it",
        ),
    ],
)
def test_select_files(tmp_path: Path, with_groups: list[str], expected: list[str]) -> None:
    git(tmp_path, "init")
    write(tmp_path, "app.py", "tests/test_app.py")
    (tmp_path / ".gitignore").write_text(".venv/\n__pycache__/\nbuild/\nskills.db\ndata/\n")
    git(tmp_path, "add", ".")
    write(
        tmp_path,
        "static/new.png",
        "notes.txt",
        ".venv/lib/icon.png",
        "build/out.png",
        "skills.db",
        "data/x.csv",
        "data/sub/y.csv",
        "scripts/seed.py",
        "scripts/__pycache__/seed.pyc",
        "scripts/.venv/lib/big.so",
    )
    paths = {
        "data": ["skills.db", "data/"],
        "images": ["**/*.png"],
        "everything": ["**/*"],
        "tools": ["scripts/"],
    }
    groups = {name: {"paths": paths[name], "default": False} for name in with_groups}
    config = make_config(tmp_path, GROUPS=groups)
    assert select_files(config, with_groups=with_groups) == expected
