from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

import pytest

from padeploy.manifest import (
    Manifest,
    ManifestError,
    changed_files,
    dump_manifest,
    hash_files,
    parse_manifest,
)

APP_SHA256 = "a172cedcae47474b615c54d510a5d84a8dea3032e958587430b413538be3f333"  # of b"app"


def test_hash_files_leaves_out_files_it_cant_read(tmp_path: Path) -> None:
    (tmp_path / "static").mkdir()
    (tmp_path / "app.py").write_bytes(b"app")
    assert hash_files(tmp_path, ["app.py", "static", "deleted.py"]) == {"app.py": APP_SHA256}


FILES_SHAPE = '"files" isn\'t an object of path: sha256'


@pytest.mark.parametrize(
    ("data", "expected_exception", "expected"),
    [
        pytest.param(
            b'{"commit": "abc", "files": {"app.py": "1f"}}',
            nullcontext(),
            Manifest({"app.py": "1f"}, "abc"),
            id="manifest",
        ),
        pytest.param(
            b'{"commit": null, "files": {}, "source": "server"}',
            nullcontext(),
            Manifest(),
            id="unknown-keys-ignored",
        ),
        pytest.param(b'{"files": {}}', nullcontext(), Manifest(), id="no-commit"),
        pytest.param(
            b"{", pytest.raises(ManifestError, match="isn't valid JSON"), None, id="not-json"
        ),
        pytest.param(
            b"\xff", pytest.raises(ManifestError, match="isn't valid JSON"), None, id="not-utf-8"
        ),
        pytest.param(
            b"[]", pytest.raises(ManifestError, match="isn't a JSON object"), None, id="not-object"
        ),
        pytest.param(
            b'{"commit": null}',
            pytest.raises(ManifestError, match=FILES_SHAPE),
            None,
            id="no-files",
        ),
        pytest.param(
            b'{"files": ["app.py"]}',
            pytest.raises(ManifestError, match=FILES_SHAPE),
            None,
            id="files-not-object",
        ),
        pytest.param(
            b'{"files": {"app.py": 1}}',
            pytest.raises(ManifestError, match=FILES_SHAPE),
            None,
            id="hash-not-string",
        ),
        pytest.param(
            b'{"commit": 1, "files": {}}',
            pytest.raises(ManifestError, match='"commit" isn\'t a string or null'),
            None,
            id="commit-not-string",
        ),
    ],
)
def test_parse_manifest(
    data: bytes, expected_exception: AbstractContextManager[Any], expected: Manifest | None
) -> None:
    with expected_exception:
        assert parse_manifest(data) == expected


def test_dump_manifest_writes_sorted_indented_json_that_parses_back() -> None:
    manifest = Manifest({"b.py": "2", "a.py": "1"}, "abc")
    dumped = dump_manifest(manifest)
    assert (dumped, parse_manifest(dumped)) == (
        b'{\n  "commit": "abc",\n  "files": {\n    "a.py": "1",\n    "b.py": "2"\n  }\n}\n',
        manifest,
    )


def test_changed_files_are_new_edited_or_unreadable_in_the_order_given() -> None:
    manifest = Manifest({"same.py": "1", "edited.py": "1", "unreadable.py": "1"})
    hashes = {"same.py": "1", "edited.py": "2", "new.py": "3"}
    files = ["unreadable.py", "same.py", "new.py", "edited.py"]
    assert changed_files(files, hashes, manifest) == ["unreadable.py", "new.py", "edited.py"]
