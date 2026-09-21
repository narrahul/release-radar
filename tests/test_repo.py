"""Fetching a stranger's repo - the parts that must not be trusting."""

import zipfile

import pytest

from radar.repo import (
    MAX_FILE_BYTES,
    RepoError,
    extract_python_files,
    parse_repo,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("pallets/flask", ("pallets", "flask")),
        ("https://github.com/pallets/flask", ("pallets", "flask")),
        ("https://github.com/pallets/flask/", ("pallets", "flask")),
        ("https://github.com/pallets/flask.git", ("pallets", "flask")),
        ("git@github.com:pallets/flask.git", ("pallets", "flask")),
        ("https://github.com/pallets/flask/tree/main/src", ("pallets", "flask")),
    ],
)
def test_parse_repo(value, expected):
    assert parse_repo(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "not a repo", "https://gitlab.com/a/b"])
def test_parse_repo_rejects_junk(value):
    with pytest.raises(RepoError):
        parse_repo(value)


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)
    return path


def test_wrapper_directory_is_stripped(tmp_path):
    archive = make_zip(tmp_path / "r.zip", {
        "owner-repo-abc123/app/config.py": "import pydantic\n",
        "owner-repo-abc123/README.md": "# hi",
    })
    out = tmp_path / "out"
    out.mkdir()
    assert extract_python_files(archive, out) == 1
    assert (out / "app" / "config.py").exists()  # not owner-repo-abc123/app/...


def test_non_python_files_are_not_written(tmp_path):
    archive = make_zip(tmp_path / "r.zip", {
        "r-1/a.py": "x = 1\n",
        "r-1/big.bin": "\x00" * 100,
        "r-1/notes.md": "text",
    })
    out = tmp_path / "out"
    out.mkdir()
    assert extract_python_files(archive, out) == 1
    assert not (out / "big.bin").exists()


def test_zip_slip_entry_is_refused(tmp_path):
    """An archive entry escaping the destination must never be written."""
    archive = make_zip(tmp_path / "r.zip", {
        "r-1/../../escaped.py": "danger = 1\n",
        "r-1/ok.py": "fine = 1\n",
    })
    out = tmp_path / "out"
    out.mkdir()
    written = extract_python_files(archive, out)

    assert written == 1
    assert (out / "ok.py").exists()
    assert not (tmp_path.parent / "escaped.py").exists()
    assert not (tmp_path / "escaped.py").exists()


def test_absurdly_large_file_is_skipped(tmp_path):
    archive = make_zip(tmp_path / "r.zip", {
        "r-1/huge.py": "# pad\n" * (MAX_FILE_BYTES // 3),
        "r-1/small.py": "ok = 1\n",
    })
    out = tmp_path / "out"
    out.mkdir()
    assert extract_python_files(archive, out) == 1
    assert (out / "small.py").exists()


def test_corrupt_archive_raises_repo_error(tmp_path):
    archive = tmp_path / "r.zip"
    archive.write_bytes(b"this is not a zip file")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RepoError, match="not a valid zip"):
        extract_python_files(archive, out)
