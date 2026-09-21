"""Stage 1 - pure parsing only. Nothing here touches the network."""

import json

import pytest

from radar.fetch import (
    _tag_matches,
    github_repo_from_pypi,
    load_notes_file,
)

from conftest import FIXTURES


def pypi_metadata():
    with open(FIXTURES / "pypi" / "pydantic.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_repo_is_resolved_from_pypi_project_urls():
    assert github_repo_from_pypi(pypi_metadata()) == ("pydantic", "pydantic")


def test_source_url_wins_over_a_docs_homepage():
    metadata = {
        "info": {
            "home_page": "https://github.com/wrong/docs-site",
            "project_urls": {
                "Homepage": "https://docs.example.com",
                "Source": "https://github.com/right/lib",
            },
        }
    }
    assert github_repo_from_pypi(metadata) == ("right", "lib")


def test_git_suffix_and_trailing_path_are_stripped():
    metadata = {"info": {"project_urls": {"Source": "https://github.com/o/r.git"}}}
    assert github_repo_from_pypi(metadata) == ("o", "r")

    metadata = {"info": {"project_urls": {"Source": "https://github.com/o/r/issues"}}}
    assert github_repo_from_pypi(metadata) == ("o", "r")


def test_project_without_github_returns_none():
    metadata = {"info": {"project_urls": {"Homepage": "https://gitlab.com/o/r"}}}
    assert github_repo_from_pypi(metadata) is None


@pytest.mark.parametrize(
    "tag,version,expected",
    [
        ("v2.0", "2.0", True),
        ("2.0", "2.0", True),
        ("pydantic-2.0", "2.0", True),
        ("release-v2.0", "2.0", True),
        ("2.0", "v2.0", True),
        ("v2.0.1", "2.0", False),
        ("v20", "2.0", False),
        ("", "2.0", False),
    ],
)
def test_tag_matching(tag, version, expected):
    assert _tag_matches(tag, version) is expected


def test_notes_file_round_trip():
    path = FIXTURES / "notes" / "pydantic-2.0.md"
    notes = load_notes_file(str(path), "pydantic", "2.0")
    assert notes.source == "file"
    assert "BaseSettings" in notes.body
    assert not notes.is_empty()
