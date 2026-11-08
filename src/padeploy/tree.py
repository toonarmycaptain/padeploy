"""Show lists of files as a tree, the way an IDE's project view does."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple


class TreeLine(NamedTuple):
    """One line of a file tree: a directory, or a file with its path."""

    depth: int
    label: str
    path: str | None  # None for a directory


@dataclass
class _Dir:
    """A directory: its subdirectories by name, and its files' paths by name."""

    dirs: dict[str, "_Dir"] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)


def file_tree(paths: Iterable[str]) -> list[TreeLine]:
    """``paths`` (/-separated) as a tree.

    At each level, directories come before files, each sorted by name, ignoring case. A directory
    holding only one directory shares its line with it (``core/data/``), like PyCharm's "Compact
    Middle Packages".
    """
    root = _Dir()
    for path in paths:
        *directories, name = path.split("/")
        node = root
        for directory in directories:
            node = node.dirs.setdefault(directory, _Dir())
        node.files[name] = path
    lines: list[TreeLine] = []
    _walk(root, 0, lines)
    return lines


def _walk(node: _Dir, depth: int, lines: list[TreeLine]) -> None:
    """Add ``node``'s directories (each followed by its contents), then its files, to ``lines``."""
    for name in sorted(node.dirs, key=_by_name):
        label, child = f"{name}/", node.dirs[name]
        while len(child.dirs) == 1 and not child.files:
            ((only, child),) = child.dirs.items()
            label += f"{only}/"
        lines.append(TreeLine(depth, label, None))
        _walk(child, depth + 1, lines)
    for name in sorted(node.files, key=_by_name):
        lines.append(TreeLine(depth, name, node.files[name]))


def _by_name(name: str) -> tuple[str, str]:
    """Sort key: the name ignoring case, then exactly, so the order is stable."""
    return name.casefold(), name
