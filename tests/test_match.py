"""Stage 4 - the verdict is plain code, so it is testable without a model."""

import pytest

from radar.match import (
    build_alert,
    candidate_paths,
    match_changes,
    module_for_distribution,
    normalize_symbol,
)
from radar.models import (
    BehaviourChange,
    ExtractedChanges,
    RemovedApi,
    RenamedApi,
    Verdict,
)
from radar.scan import scan_repo


def removed(*names):
    return ExtractedChanges(removed=[RemovedApi(name=n) for n in names])


# -- the headline case -------------------------------------------------------


def test_removed_symbol_the_repo_imports_is_breaking(sample_scan):
    alert = build_alert(
        removed("BaseSettings"), sample_scan, "pydantic", "2.0", repo="sample"
    )
    assert alert.verdict is Verdict.BREAKING
    assert alert.findings[0].symbol == "pydantic.BaseSettings"
    assert alert.findings[0].primary_location() == "app/config.py:4"


def test_qualified_name_in_the_notes_matches_too(sample_scan):
    alert = build_alert(
        removed("pydantic.BaseSettings"), sample_scan, "pydantic", "2.0", repo="s"
    )
    assert alert.verdict is Verdict.BREAKING


def test_removed_symbol_the_repo_never_imports_is_routine(sample_scan):
    alert = build_alert(
        removed("validator"), sample_scan, "pydantic", "2.0", repo="sample"
    )
    assert alert.verdict is Verdict.ROUTINE
    assert alert.findings == []
    assert "none that this repo imports" in alert.reason


def test_package_the_repo_does_not_use_at_all_is_routine(sample_scan):
    alert = build_alert(
        removed("Session"), sample_scan, "sqlalchemy", "2.0", repo="sample"
    )
    assert alert.verdict is Verdict.ROUTINE
    assert alert.reason == "repo does not import sqlalchemy"


def test_rename_is_breaking_and_names_the_replacement(sample_scan):
    changes = ExtractedChanges(
        renamed=[RenamedApi(old_name="BaseSettings", new_name="pydantic_settings.BaseSettings")]
    )
    alert = build_alert(changes, sample_scan, "pydantic", "2.0", repo="sample")
    assert alert.verdict is Verdict.BREAKING
    assert alert.findings[0].kind == "renamed"
    assert "renamed to pydantic_settings.BaseSettings" in alert.findings[0].detail


def test_behaviour_change_alone_is_not_breaking(sample_scan):
    changes = ExtractedChanges(
        behaviour_changed=[BehaviourChange(name="BaseModel", note="stricter coercion")]
    )
    alert = build_alert(changes, sample_scan, "pydantic", "2.0", repo="sample")
    assert alert.verdict is Verdict.ROUTINE


# -- security ----------------------------------------------------------------


def test_security_release_with_no_matching_symbol_is_security(sample_scan):
    changes = ExtractedChanges(security=True, security_note="CVE-2024-0001")
    alert = build_alert(changes, sample_scan, "pydantic", "2.0", repo="sample")
    assert alert.verdict is Verdict.SECURITY
    assert alert.security_note == "CVE-2024-0001"


def test_breaking_outranks_security_but_keeps_the_note(sample_scan):
    changes = ExtractedChanges(
        removed=[RemovedApi(name="BaseSettings")], security=True, security_note="CVE-1"
    )
    alert = build_alert(changes, sample_scan, "pydantic", "2.0", repo="sample")
    assert alert.verdict is Verdict.BREAKING
    assert alert.security_note == "CVE-1"


# -- precision rules ---------------------------------------------------------


def test_deeper_repo_usage_counts(tmp_path):
    """Notes remove `pydantic.tools`; the repo uses `pydantic.tools.parse_obj_as`."""
    (tmp_path / "m.py").write_text("from pydantic.tools import parse_obj_as\n")
    scan = scan_repo(tmp_path)
    findings = match_changes(removed("pydantic.tools"), scan, "pydantic")
    assert len(findings) == 1


def test_shallower_repo_usage_does_not_count(tmp_path):
    """The repo imports `pydantic`; a removed member of it is not evidence."""
    (tmp_path / "m.py").write_text("import pydantic\n")
    scan = scan_repo(tmp_path)
    assert match_changes(removed("BaseSettings"), scan, "pydantic") == []


def test_same_symbol_is_reported_once(tmp_path):
    """Two files importing the same removed name are one finding, two sites."""
    (tmp_path / "a.py").write_text("from pydantic import BaseSettings\n")
    (tmp_path / "b.py").write_text("from pydantic import BaseSettings\n")
    scan = scan_repo(tmp_path)
    findings = match_changes(removed("BaseSettings"), scan, "pydantic")
    assert len(findings) == 1
    assert {s.file for s in findings[0].evidence} == {"a.py", "b.py"}


def test_star_import_is_a_caveat_not_a_finding(sample_scan):
    alert = build_alert(removed("get"), sample_scan, "requests", "3.0", repo="sample")
    assert alert.verdict is Verdict.ROUTINE
    assert "import *" in alert.reason
    assert "app/star.py:1" in alert.reason


# -- name handling -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BaseSettings", "BaseSettings"),
        ("`BaseSettings`", "BaseSettings"),
        ("BaseSettings()", "BaseSettings"),
        ("pydantic.tools.parse_obj_as", "pydantic.tools.parse_obj_as"),
        ("Model.dict()", "Model.dict"),
        ("the settings class", None),
        ("", None),
        ("--flag", None),
    ],
)
def test_normalize_symbol(raw, expected):
    assert normalize_symbol(raw) == expected


def test_garbage_extraction_produces_no_findings(sample_scan):
    changes = removed("the old settings class", "see the migration guide", "")
    assert match_changes(changes, sample_scan, "pydantic") == []


@pytest.mark.parametrize(
    "distribution,module",
    [
        ("pydantic", "pydantic"),
        ("PyYAML", "yaml"),
        ("beautifulsoup4", "bs4"),
        ("python-dateutil", "dateutil"),
        ("typing-extensions", "typing_extensions"),
        ("some-new-lib", "some_new_lib"),
    ],
)
def test_module_for_distribution(distribution, module):
    assert module_for_distribution(distribution) == module


def test_candidate_paths_do_not_double_prefix():
    assert candidate_paths("BaseSettings", "pydantic") == ["pydantic.BaseSettings"]
    assert candidate_paths("pydantic.BaseSettings", "pydantic") == ["pydantic.BaseSettings"]


def test_distribution_name_differs_from_import_name(tmp_path):
    (tmp_path / "m.py").write_text("from yaml import safe_load\n")
    scan = scan_repo(tmp_path)
    alert = build_alert(removed("safe_load"), scan, "PyYAML", "7.0", repo="r")
    assert alert.verdict is Verdict.BREAKING
    assert alert.findings[0].symbol == "yaml.safe_load"


# -- deduplication -----------------------------------------------------------


def test_same_api_named_twice_is_one_finding(sample_scan):
    """Notes often name an API bare and qualified; that is still one API."""
    changes = removed("BaseSettings", "pydantic.BaseSettings")
    alert = build_alert(changes, sample_scan, "pydantic", "2.0", repo="sample")
    assert len(alert.findings) == 1
    assert alert.reason.startswith("1 API(s)")


def test_rename_of_an_already_removed_name_is_not_repeated(sample_scan):
    changes = ExtractedChanges(
        removed=[RemovedApi(name="BaseSettings")],
        renamed=[RenamedApi(old_name="BaseSettings", new_name="pydantic_settings.BaseSettings")],
    )
    alert = build_alert(changes, sample_scan, "pydantic", "2.0", repo="sample")
    assert len(alert.findings) == 1
    assert alert.findings[0].kind == "removed"


def test_distinct_apis_are_still_separate_findings(sample_scan):
    changes = removed("BaseSettings", "BaseModel")
    alert = build_alert(changes, sample_scan, "pydantic", "2.0", repo="sample")
    assert {f.symbol for f in alert.findings} == {
        "pydantic.BaseSettings", "pydantic.BaseModel"
    }
