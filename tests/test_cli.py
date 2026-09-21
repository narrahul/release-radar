"""End to end, offline: notes file + saved extraction -> alert with file:line."""

import json

from radar.cli import main
from radar.models import Verdict
from radar.report import one_line, render
from radar.models import Alert, Finding, UsageSite

from conftest import FIXTURES, SAMPLE_REPO

NOTES = str(FIXTURES / "notes" / "pydantic-2.0.md")
CHANGES = str(FIXTURES / "changes" / "pydantic-2.0.json")

BASE_ARGS = [
    "check",
    "--repo", str(SAMPLE_REPO),
    "--package", "pydantic",
    "--version", "2.0",
    "--notes-file", NOTES,
    "--changes-file", CHANGES,
]


def test_check_reports_breaking_with_evidence(capsys):
    code = main(BASE_ARGS)
    out = capsys.readouterr().out

    assert code == 1  # non-zero so CI can gate on it
    assert "BREAKING" in out
    assert "pydantic.BaseSettings was removed" in out
    assert "app/config.py:4" in out


def test_check_json_output_is_machine_readable(capsys):
    code = main(BASE_ARGS + ["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["verdict"] == "BREAKING"
    assert payload["package"] == "pydantic"
    finding = payload["findings"][0]
    assert finding["symbol"] == "pydantic.BaseSettings"
    assert finding["evidence"][0] == {
        "file": "app/config.py", "line": 4,
        "kind": "import", "local_name": "BaseSettings",
    }


def test_check_oneline_output(capsys):
    main(BASE_ARGS + ["--oneline"])
    line = capsys.readouterr().out.strip()
    assert line.startswith("pydantic 2.0 -> BREAKING")
    assert line.endswith("app/config.py:4")


def test_unused_package_exits_zero(capsys):
    code = main([
        "check", "--repo", str(SAMPLE_REPO),
        "--package", "sqlalchemy", "--version", "2.0",
        "--notes-file", NOTES, "--changes-file", CHANGES,
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "ROUTINE" in out


def test_missing_notes_file_is_an_error_not_a_crash(capsys):
    code = main([
        "check", "--repo", str(SAMPLE_REPO),
        "--package", "pydantic", "--version", "2.0",
        "--notes-file", "does-not-exist.md", "--changes-file", CHANGES,
    ])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_scan_subcommand_lists_usage(capsys):
    code = main(["scan", "--repo", str(SAMPLE_REPO), "--package", "pydantic"])
    out = capsys.readouterr().out
    assert code == 0
    assert "pydantic.BaseSettings" in out
    assert "app/config.py:4" in out


def test_extract_subcommand_prints_the_schema(capsys):
    code = main([
        "extract", "--package", "pydantic", "--version", "2.0",
        "--notes-file", NOTES, "--changes-file", CHANGES,
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["removed"][0]["name"] == "BaseSettings"


# -- stage 5 rendering -------------------------------------------------------


def sample_alert():
    return Alert(
        repo="/repos/app", package="pydantic", version="2.0",
        verdict=Verdict.BREAKING,
        findings=[
            Finding(
                kind="removed", symbol="pydantic.BaseSettings",
                detail="pydantic.BaseSettings was removed in this release",
                evidence=[UsageSite(file="app/config.py", line=4,
                                    kind="import", local_name="BaseSettings")],
            )
        ],
        reason="1 API(s) this repo uses changed; first at app/config.py:4",
    )


def test_one_line_is_scannable():
    assert one_line(sample_alert()) == (
        "pydantic 2.0 -> BREAKING - pydantic.BaseSettings removed - app/config.py:4"
    )


def test_render_shows_every_evidence_site():
    text = render(sample_alert())
    assert "used at app/config.py:4" in text
    assert "import of `BaseSettings`" in text
