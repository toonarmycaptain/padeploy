"""How deploy prints the file tree and each upload's or delete's result."""

import logging
import os
import re
from collections.abc import Iterator

import pytest
from cli_stubs import patch_deploy
from click.testing import CliRunner

from padeploy import cli

FILES = ["app.py", "core/data.py", "web/urls.py"]


def tree(data: str = "pending", urls: str = "pending", app: str = "pending") -> list[str]:
    """``FILES``' tree as printed, with each file's result."""
    return ["  core/", f"    data.py  {data}", "  web/", f"    urls.py  {urls}", f"  app.py  {app}"]


def render(output: str) -> list[str]:
    """The screen after ``output``, with the codes _LiveTree uses: cursor up and down, and erase
    to the end of the line."""
    screen, row, column = [""], 0, 0
    for count, code, char in re.findall(r"\x1b\[(\d*)([ABK])|(.)", output, re.DOTALL):
        if code == "A":
            row -= int(count)
        elif code == "B":
            row += int(count)
        elif code == "K":
            screen[row] = screen[row][:column]
        elif char == "\r":
            column = 0
        elif char == "\n":
            row, column = row + 1, 0
        else:
            line = screen[row]
            screen[row] = line[:column].ljust(column) + char + line[column + 1 :]
            column += 1
        screen += [""] * (row + 1 - len(screen))
    return screen[:-1] if screen[-1] == "" else screen


# app.py is done second, but in order it waits for urls.py, above it.
IN_ORDER = [tree("FAIL: boom")[:2], tree("FAIL: boom")[:2], tree("FAIL: boom", "OK", "OK")]


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        pytest.param(None, IN_ORDER, id="not-a-terminal-in-order"),
        pytest.param((80, 5), IN_ORDER, id="too-short-in-order"),
        pytest.param((12, 24), IN_ORDER, id="too-narrow-in-order"),
        pytest.param(
            (80, 24), [tree("FAIL"), tree("FAIL", app="OK"), tree("FAIL", "OK", "OK")], id="live"
        ),
    ],
)
def test_echo_results_prints_each_result_as_it_comes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    size: tuple[int, int] | None,
    expected: list[list[str]],
) -> None:
    monkeypatch.setattr(cli, "_terminal_size", lambda: size and os.terminal_size(size))
    results = [("core/data.py", "boom"), ("app.py", None), ("web/urls.py", None)]
    output, screens = "", []
    for _ in cli._echo_results(FILES, lambda _: iter(results)):
        output += capsys.readouterr().out
        screens.append(render(output))
    assert screens == expected


LONG = "a_very_long_module_name.py"


@pytest.mark.parametrize(
    ("files", "steps", "size", "expected"),
    [
        pytest.param(
            FILES,
            [("app.py", None), "Rate limited", ("web/urls.py", "boom"), ("core/data.py", None)],
            (80, 24),
            [*tree("OK", "FAIL", "OK"), "Rate limited", "  web/urls.py  FAIL: boom"]
            + ["Error: 1 of 3 file(s) failed to upload; not reloading."],
            id="log-messages-and-errors-under-the-tree",
        ),
        pytest.param(
            [LONG], [(LONG, None)], (24, 24), ["  a_very_long…  OK"], id="lines-cut-to-the-width"
        ),
        pytest.param(
            ["app.py", "db.py"],
            ["warning 0", "warning 1", "warning 2", ("db.py", None), ("app.py", None)],
            (80, 5),  # app.py's row ends up 5 rows up, db.py's 4
            ["  app.py  pending", "  db.py  OK", "warning 0", "warning 1", "warning 2"]
            + ["  app.py  OK"],
            id="result-under-the-tree-once-its-row-scrolled-off",
        ),
    ],
)
def test_live_deploy_output(
    monkeypatch: pytest.MonkeyPatch,
    files: list[str],
    steps: list[tuple[str, str | None] | str],
    size: tuple[int, int],
    expected: list[str],
) -> None:
    """``steps`` are upload results, and log messages to print in between."""

    def uploads(*_: object) -> Iterator[tuple[str, str | None]]:
        for step in steps:
            if isinstance(step, str):
                logging.getLogger("padeploy.api").warning(step)
            else:
                yield step

    patch_deploy(monkeypatch, files)
    monkeypatch.setattr(cli, "upload_files", uploads)
    monkeypatch.setattr(cli, "_terminal_size", lambda: os.terminal_size(size))
    result = CliRunner().invoke(cli.main, ["deploy", "--no-reload"])
    assert render(result.output)[1:] == expected  # after "Uploading N file(s) to ..."
