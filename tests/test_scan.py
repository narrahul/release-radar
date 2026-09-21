"""Stage 3 - the AST scan is exact, or the file:line in an alert is a lie."""

from pathlib import Path

from radar.scan import scan_repo


def sites_for(scan, qualified):
    usage = scan.usages.get(qualified)
    return [] if usage is None else usage.sites


def test_from_import_records_the_import_line(sample_scan):
    sites = sites_for(sample_scan, "pydantic.BaseSettings")
    imports = [s for s in sites if s.kind == "import"]
    assert ("app/config.py", 4) in [(s.file, s.line) for s in imports]


def test_class_base_is_recorded_as_a_reference(sample_scan):
    sites = sites_for(sample_scan, "pydantic.BaseSettings")
    references = [(s.file, s.line) for s in sites if s.kind == "reference"]
    assert ("app/config.py", 7) in references


def test_aliased_module_attribute_resolves(sample_scan):
    """`import pydantic as pd` + `pd.BaseSettings` is the same symbol."""
    sites = sites_for(sample_scan, "pydantic.BaseSettings")
    assert ("app/aliased.py", 5) in [(s.file, s.line) for s in sites]


def test_import_site_is_the_canonical_evidence(sample_scan):
    usage = sample_scan.usages["pydantic.BaseSettings"]
    assert usage.import_site.kind == "import"
    assert usage.import_site.location() == "app/config.py:4"


def test_local_class_with_the_same_name_is_not_a_hit(sample_scan):
    """The decoy file defines its own BaseSettings and imports nothing."""
    files = {s.file for s in sites_for(sample_scan, "pydantic.BaseSettings")}
    assert "app/decoy.py" not in files


def test_prose_mentioning_the_symbol_is_not_a_hit(sample_scan):
    """A keyword scan would flag the string in decoy.py. The AST does not."""
    for usage in sample_scan.usages.values():
        for site in usage.sites:
            assert site.file != "app/decoy.py"


def test_unparseable_file_is_recorded_not_fatal(sample_scan):
    skipped = dict(sample_scan.files_skipped)
    assert "app/broken.py" in skipped
    assert "syntax error" in skipped["app/broken.py"]
    assert sample_scan.files_scanned >= 4


def test_virtualenv_is_skipped(sample_scan):
    files = {s.file for u in sample_scan.usages.values() for s in u.sites}
    assert not any(f.startswith(".venv/") for f in files)


def test_star_import_is_recorded_separately(sample_scan):
    stars = sample_scan.star_imports_for("requests")
    assert [s.location() for s in stars] == ["app/star.py:1"]
    # ...and `get` is not silently attributed to requests.
    assert "requests.get" not in sample_scan.usages


def test_for_module_filters_to_one_package(sample_scan):
    names = {u.qualified_name for u in sample_scan.for_module("pydantic")}
    assert names == {"pydantic", "pydantic.BaseSettings", "pydantic.BaseModel",
                     "pydantic.Field"}


def test_scan_is_deterministic(tmp_path):
    (tmp_path / "a.py").write_text("from pydantic import BaseSettings\n")
    first = scan_repo(tmp_path)
    second = scan_repo(tmp_path)
    assert list(first.usages) == list(second.usages)


def test_relative_imports_are_ignored(tmp_path):
    (tmp_path / "m.py").write_text("from .local import thing\nthing()\n")
    scan = scan_repo(tmp_path)
    assert scan.usages == {}


def test_assignment_does_not_count_as_use(tmp_path):
    source = "from pydantic import Field\nField = 1\n"
    (tmp_path / "m.py").write_text(source)
    scan = scan_repo(tmp_path)
    kinds = [s.kind for s in scan.usages["pydantic.Field"].sites]
    assert kinds == ["import"]


def test_submodule_import_is_qualified(tmp_path):
    (tmp_path / "m.py").write_text(
        "from pydantic.tools import parse_obj_as\nparse_obj_as(int, 1)\n"
    )
    scan = scan_repo(tmp_path)
    assert "pydantic.tools.parse_obj_as" in scan.usages


def test_dotted_import_resolves_attribute_chain(tmp_path):
    (tmp_path / "m.py").write_text("import pydantic.tools\npydantic.tools.parse_obj_as(int, 1)\n")
    scan = scan_repo(tmp_path)
    assert "pydantic.tools.parse_obj_as" in scan.usages
