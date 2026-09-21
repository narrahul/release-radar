"""Credentials pasted into a hosting dashboard arrive with whitespace.

A trailing newline on the API key raised `ValueError: Invalid header value`
from `http.client` on every hosted run, while the page itself loaded fine.
These pin the strip so it cannot regress.
"""

import pytest

from radar.extract import ExtractionError, OpenRouterExtractor


def test_key_with_trailing_newline_is_usable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-123\n")
    extractor = OpenRouterExtractor()
    assert extractor.api_key == "sk-or-test-123"
    assert "\n" not in f"Bearer {extractor.api_key}"


@pytest.mark.parametrize("raw", ["sk-or-1\n", " sk-or-1 ", "sk-or-1\r\n", "\tsk-or-1"])
def test_surrounding_whitespace_is_removed(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_API_KEY", raw)
    assert OpenRouterExtractor().api_key == "sk-or-1"


def test_whitespace_only_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "   \n")
    with pytest.raises(ExtractionError, match="OPENROUTER_API_KEY"):
        OpenRouterExtractor()


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ExtractionError, match="not set"):
        OpenRouterExtractor()


def test_github_token_is_stripped(monkeypatch):
    """Same hazard, both places that read GITHUB_TOKEN."""
    import radar.fetch as fetch_module
    import radar.repo as repo_module

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test\n")
    captured = {}

    def fake_get_json(url, timeout, token=None):
        if "pypi.org" in url:
            return {  # PyPI itself takes no token; the GitHub call does
                "info": {
                    "description": "notes",
                    "project_urls": {"Source": "https://github.com/o/r"},
                }
            }
        captured["fetch_token"] = token
        return [{"tag_name": "v1.0", "body": "x" * 2000, "html_url": "u"}]

    monkeypatch.setattr(fetch_module, "_get_json", fake_get_json)
    fetch_module.fetch_release_notes("pkg", "1.0")
    assert captured["fetch_token"] == "ghp_test"

    def fake_download(url, destination, token, timeout):
        captured["repo_token"] = token
        raise repo_module.RepoError("stop here")

    monkeypatch.setattr(repo_module, "_download", fake_download)
    with pytest.raises(repo_module.RepoError):
        repo_module.fetch_repo("o/r")
    assert captured["repo_token"] == "ghp_test"
