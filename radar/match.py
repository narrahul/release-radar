"""Stage 4 - decide, in plain code, whether a release breaks THIS repo.

No model runs here. The rule is deliberately narrow, because a noisy
"breaking" alert gets ignored: a removed or renamed API counts only when
the repo imports that exact name, or something underneath it. If the repo
merely imports the package, or uses a shallower name than the one that was
removed, that is not evidence and it is not reported.

`from pkg import *` is the one case the AST cannot resolve. It is surfaced
as a caveat on the alert, never as a finding.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from .models import (
    Alert,
    ExtractedChanges,
    Finding,
    SymbolUsage,
    UsageSite,
    Verdict,
)
from .scan import ScanResult

# PyPI distribution names that differ from the name you import. The general
# rule (lowercase, dashes to underscores) covers the rest.
DISTRIBUTION_TO_MODULE: Dict[str, str] = {
    "beautifulsoup4": "bs4",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "python-dateutil": "dateutil",
    "python-dotenv": "dotenv",
    "scikit-learn": "sklearn",
    "scikit-image": "skimage",
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "protobuf": "google.protobuf",
    "grpcio": "grpc",
    "psycopg2-binary": "psycopg2",
    "mysqlclient": "MySQLdb",
    "pycryptodome": "Crypto",
    "pycryptodomex": "Cryptodome",
    "attrs": "attr",
    "msgpack-python": "msgpack",
    "pytest-runner": "pytest",
    "memcached": "memcache",
    "faiss-cpu": "faiss",
    "pymupdf": "fitz",
    "docx": "docx",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "typing-extensions": "typing_extensions",
}

# A dotted Python path, optionally with a trailing call or subscript that the
# notes may have written in ("`BaseSettings()`", "Model.dict()").
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def module_for_distribution(package: str) -> str:
    """The top-level module name a PyPI distribution installs."""
    key = package.strip().lower()
    if key in DISTRIBUTION_TO_MODULE:
        return DISTRIBUTION_TO_MODULE[key]
    return key.replace("-", "_")


def normalize_symbol(raw: str) -> Optional[str]:
    """Clean a name as the notes wrote it into a dotted path, or drop it.

    The model is told to copy names verbatim, so this only strips the
    packaging around them: backticks, quotes, a trailing call, a trailing
    subscript. Anything that still is not a dotted path is discarded rather
    than guessed at.
    """
    if not raw:
        return None
    text = raw.strip().strip("`\"'").strip()
    text = re.sub(r"\(.*?\)\s*$", "", text)  # BaseSettings() -> BaseSettings
    text = re.sub(r"\[.*?\]\s*$", "", text)  # Config[str]    -> Config
    text = text.strip().strip(".,;:").strip()
    if not text or not _SYMBOL_RE.match(text):
        return None
    return text


def candidate_paths(symbol: str, module: str) -> List[str]:
    """Fully-qualified spellings of `symbol` inside `module`.

    The notes may write `BaseSettings` or `pydantic.BaseSettings`; both mean
    the same import path.
    """
    top = module.split(".")[0]
    if symbol == module or symbol.startswith(module + "."):
        return [symbol]
    if symbol == top or symbol.startswith(top + "."):
        return [symbol]
    return [f"{module}.{symbol}"]


def _usages_touching(candidate: str, usages: Sequence[SymbolUsage]) -> List[SymbolUsage]:
    """Repo usages of `candidate` itself or of something underneath it."""
    hits = []
    for usage in usages:
        name = usage.qualified_name
        if name == candidate or name.startswith(candidate + "."):
            hits.append(usage)
    return hits


def _evidence(hits: Sequence[SymbolUsage], limit: int = 5) -> List[UsageSite]:
    """Import sites first, deduplicated by location, capped for readability."""
    sites: List[UsageSite] = []
    seen = set()
    for usage in hits:
        for site in usage.sites:
            key = (site.file, site.line, site.kind)
            if key in seen:
                continue
            seen.add(key)
            sites.append(site)
    sites.sort(key=lambda s: (s.kind != "import", s.file, s.line))
    return sites[:limit]


def match_changes(
    changes: ExtractedChanges,
    scan: ScanResult,
    package: str,
    module: Optional[str] = None,
) -> List[Finding]:
    """Extracted changes that the repo actually touches."""
    module = module or module_for_distribution(package)
    usages = scan.for_module(module.split(".")[0])
    findings: List[Finding] = []

    for removed in changes.removed:
        symbol = normalize_symbol(removed.name)
        if not symbol:
            continue
        for candidate in candidate_paths(symbol, module):
            hits = _usages_touching(candidate, usages)
            if not hits:
                continue
            detail = f"{candidate} was removed in this release"
            if removed.note:
                detail += f" ({removed.note.strip()})"
            findings.append(
                Finding(kind="removed", symbol=candidate, detail=detail,
                        evidence=_evidence(hits))
            )
            break

    for renamed in changes.renamed:
        symbol = normalize_symbol(renamed.old_name)
        if not symbol:
            continue
        for candidate in candidate_paths(symbol, module):
            hits = _usages_touching(candidate, usages)
            if not hits:
                continue
            new_name = normalize_symbol(renamed.new_name) or renamed.new_name.strip()
            detail = f"{candidate} was renamed to {new_name}"
            if renamed.note:
                detail += f" ({renamed.note.strip()})"
            findings.append(
                Finding(kind="renamed", symbol=candidate, detail=detail,
                        evidence=_evidence(hits))
            )
            break

    # Stable output: the deepest evidence first, then alphabetical.
    findings.sort(key=lambda f: (f.evidence[0].file if f.evidence else "", f.symbol))
    return findings


def build_alert(
    changes: ExtractedChanges,
    scan: ScanResult,
    package: str,
    version: str,
    repo: str,
    module: Optional[str] = None,
    notes_url: Optional[str] = None,
) -> Alert:
    """Stage 4 + 5 - findings, verdict, and the one-line reason for it."""
    module = module or module_for_distribution(package)
    top_level = module.split(".")[0]
    findings = match_changes(changes, scan, package, module)
    uses_package = bool(scan.for_module(top_level))
    stars = scan.star_imports_for(top_level)

    if findings:
        verdict = Verdict.BREAKING
        where = findings[0].primary_location()
        reason = f"{len(findings)} API(s) this repo uses changed"
        if where:
            reason += f"; first at {where}"
    elif changes.security:
        verdict = Verdict.SECURITY
        reason = (
            f"security fix in this release; repo imports {top_level}"
            if uses_package
            else f"security fix in this release; repo does not import {top_level}"
        )
    elif not uses_package:
        verdict = Verdict.ROUTINE
        reason = f"repo does not import {top_level}"
    elif changes.is_empty():
        verdict = Verdict.ROUTINE
        reason = "release notes describe no API removals or renames"
    else:
        verdict = Verdict.ROUTINE
        reason = "APIs changed, but none that this repo imports"

    if stars:
        locations = ", ".join(sorted({s.location() for s in stars})[:3])
        reason += f" (unresolved `import *` at {locations} - names there are not checked)"

    return Alert(
        repo=repo,
        package=package,
        version=version,
        verdict=verdict,
        findings=findings,
        security_note=changes.security_note if changes.security else "",
        reason=reason,
        notes_url=notes_url,
    )
