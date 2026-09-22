"""Frozen inputs, so an eval run is reproducible and offline.

An eval that re-fetches the internet measures the internet. Release notes
and repo scans are fetched once, written here, and committed - so the score
moves only when the system changes, and anyone can re-run it without a
GitHub token or network access.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from radar.fetch import fetch_release_notes
from radar.models import ReleaseNotes, SymbolUsage, UsageSite
from radar.repo import fetch_repo
from radar.scan import ScanResult, scan_repo

GOLDEN = Path(__file__).parent / "golden"
NOTES_DIR = GOLDEN / "notes"
SCANS_DIR = GOLDEN / "scans"
SOURCES_DIR = GOLDEN / "sources"  # raw text, for the grep audit of the labels


def _notes_path(package: str, version: str) -> Path:
    return NOTES_DIR / f"{package.lower()}-{version}.json"


def _key(repo: str, ref: str = "") -> str:
    key = repo.replace("/", "__")
    return f"{key}@{ref}" if ref else key


def _scan_path(repo: str, ref: str = "") -> Path:
    return SCANS_DIR / f"{_key(repo, ref)}.json"


# -- release notes -----------------------------------------------------------


def load_notes(package: str, version: str) -> ReleaseNotes:
    path = _notes_path(package, version)
    if not path.exists():
        raise FileNotFoundError(
            f"no cached notes for {package} {version}; run `python -m eval.build`"
        )
    return ReleaseNotes.model_validate_json(path.read_text(encoding="utf-8"))


def cache_notes(package: str, version: str, refresh: bool = False) -> ReleaseNotes:
    path = _notes_path(package, version)
    if path.exists() and not refresh:
        return load_notes(package, version)
    notes = fetch_release_notes(package, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notes.model_dump_json(indent=2), encoding="utf-8")
    return notes


# -- repo scans --------------------------------------------------------------


def scan_to_dict(scan: ScanResult) -> Dict:
    return {
        "files_scanned": scan.files_scanned,
        "usages": {
            name: [site.model_dump() for site in usage.sites]
            for name, usage in sorted(scan.usages.items())
        },
        "star_imports": [site.model_dump() for site in scan.star_imports],
    }


def scan_from_dict(data: Dict) -> ScanResult:
    scan = ScanResult(root=Path("."), files_scanned=data.get("files_scanned", 0))
    for name, sites in data.get("usages", {}).items():
        scan.usages[name] = SymbolUsage(
            qualified_name=name,
            module=name.rsplit(".", 1)[0] if "." in name else name,
            sites=[UsageSite(**site) for site in sites],
        )
    scan.star_imports = [UsageSite(**site) for site in data.get("star_imports", [])]
    return scan


def load_scan(repo: str, ref: str = "") -> ScanResult:
    path = _scan_path(repo, ref)
    if not path.exists():
        raise FileNotFoundError(
            f"no cached scan for {repo}; run `python -m eval.build`"
        )
    return scan_from_dict(json.loads(path.read_text(encoding="utf-8")))


def cache_scan(
    repo: str, ref: str = "", refresh: bool = False, keep_source: bool = True
) -> ScanResult:
    """Download a repo once, store its scan - and its raw text for auditing."""
    path = _scan_path(repo, ref)
    if path.exists() and not refresh:
        return load_scan(repo, ref)

    fetched = fetch_repo(repo, ref=ref)
    try:
        scan = scan_repo(fetched.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(scan_to_dict(scan), indent=1), encoding="utf-8")
        if keep_source:
            _cache_source_text(repo, fetched.path, ref)
    finally:
        fetched.cleanup()
    return scan


def _cache_source_text(repo: str, root: Path, ref: str = "") -> None:
    """One flat file of the repo's Python source, for the label grep audit.

    The labels must not be built only from what our own scanner reports -
    that would make the scanner look perfect by construction. Keeping the
    raw text lets the audit grep for a symbol the scanner never claimed.
    """
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    out = SOURCES_DIR / f"{_key(repo, ref)}.txt"
    chunks: List[str] = []
    for file in sorted(root.rglob("*.py")):
        relative = file.relative_to(root).as_posix()
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            chunks.append(f"{relative}:{number}:{line}")
    out.write_text("\n".join(chunks), encoding="utf-8")


def load_source_text(repo: str, ref: str = "") -> Optional[str]:
    path = SOURCES_DIR / f"{_key(repo, ref)}.txt"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")
