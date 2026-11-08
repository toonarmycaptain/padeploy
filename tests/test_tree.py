import pytest

from padeploy.tree import TreeLine, file_tree


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        pytest.param([], [], id="empty"),
        pytest.param(
            ["b.py", "A.py", "c.py"],
            [TreeLine(0, "A.py", "A.py"), TreeLine(0, "b.py", "b.py"), TreeLine(0, "c.py", "c.py")],
            id="files-sorted-ignoring-case",
        ),
        pytest.param(
            ["web/manage.py", "web/config/urls.py", "app.py"],
            [
                TreeLine(0, "web/", None),
                TreeLine(1, "config/", None),
                TreeLine(2, "urls.py", "web/config/urls.py"),
                TreeLine(1, "manage.py", "web/manage.py"),
                TreeLine(0, "app.py", "app.py"),
            ],
            id="directories-before-files",
        ),
        pytest.param(
            ["core/discrete_data/data.py", "core/discrete_data/__init__.py"],
            [
                TreeLine(0, "core/discrete_data/", None),
                TreeLine(1, "__init__.py", "core/discrete_data/__init__.py"),
                TreeLine(1, "data.py", "core/discrete_data/data.py"),
            ],
            id="directory-holding-only-a-directory-shares-its-line",
        ),
        pytest.param(
            ["a/b/c/d.py", "a/b/e.py"],
            [
                TreeLine(0, "a/b/", None),
                TreeLine(1, "c/", None),
                TreeLine(2, "d.py", "a/b/c/d.py"),
                TreeLine(1, "e.py", "a/b/e.py"),
            ],
            id="directory-with-files-keeps-its-own-line",
        ),
        pytest.param(
            ["static", "static/a.css"],
            [
                TreeLine(0, "static/", None),
                TreeLine(1, "a.css", "static/a.css"),
                TreeLine(0, "static", "static"),
            ],
            id="file-and-directory-of-the-same-name",
        ),
    ],
)
def test_file_tree(paths: list[str], expected: list[TreeLine]) -> None:
    assert file_tree(paths) == expected
