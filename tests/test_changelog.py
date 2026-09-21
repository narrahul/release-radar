"""Picking the right prose - the difference between a demo and a dud.

A GitHub release body is often a generated PR list, or a two-line summary
pointing at the real changelog. Neither names an API, so neither can produce
a finding. These tests pin the selection rules.
"""

from radar.fetch import find_version_section, looks_like_pr_list

MARKDOWN = """\
# Changelog

## 2.1 (2024-01-01)

- Added a thing.

## 2.0 (2023-06-30)

- Removed `BaseSettings`; it now lives in pydantic-settings.
- Renamed `Model.dict()` to `Model.model_dump()`.

## 1.10 (2022-01-01)

- Older stuff.
"""

RST = """\
.. currentmodule:: werkzeug

Version 3.0.0
-------------

Released 2023-09-30

-   Remove previously deprecated code: ``url_quote``.

Version 2.3.8
-------------

-   Older fix.
"""

PR_LIST = """\
## What's Changed
* Fix typo by @alice in https://github.com/o/r/pull/1
* Bump deps by @bob in https://github.com/o/r/pull/2
* Tidy tests by @carol in https://github.com/o/r/pull/3

**Full Changelog**: https://github.com/o/r/compare/v1...v2
"""


def test_markdown_section_is_isolated():
    section = find_version_section(MARKDOWN, "2.0")
    assert "BaseSettings" in section
    assert "Added a thing" not in section  # 2.1 must not leak in
    assert "Older stuff" not in section  # nor 1.10


def test_rst_version_heading_is_understood():
    section = find_version_section(RST, "3.0.0")
    assert "url_quote" in section
    assert "Older fix" not in section


def test_leading_v_is_tolerated_on_either_side():
    assert find_version_section(MARKDOWN, "v2.0") is not None
    assert find_version_section("## v2.0\n\n- gone\n", "2.0") is not None


def test_missing_version_returns_none():
    assert find_version_section(MARKDOWN, "9.9") is None


def test_generated_pr_list_is_recognised():
    assert looks_like_pr_list(PR_LIST) is True


def test_real_prose_is_not_mistaken_for_a_pr_list():
    assert looks_like_pr_list(MARKDOWN) is False
    assert looks_like_pr_list(RST) is False


def test_empty_body_counts_as_useless():
    assert looks_like_pr_list("") is True
