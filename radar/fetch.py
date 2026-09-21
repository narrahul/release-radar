"""Stage 1 - get the prose for one release.

Release notes live in two places and only one of them is reliable:
GitHub releases carry a human-written changelog, PyPI carries packaging
metadata plus the project description. So: resolve the GitHub repo from
PyPI metadata, read the matching release there, and fall back to the PyPI
description only when there is no GitHub release for that version.

Deliberately thin - one request, one timeout, no retry loop. Durable
ingest (backoff, dead-letter queue, idempotency) is the reliability layer
and is not part of the detection engine.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .models import ReleaseNotes

PYPI_JSON = "https://pypi.org/pypi/{package}/json"
GITHUB_RELEASES = "https://api.github.com/repos/{owner}/{repo}/releases?per_page=100"
USER_AGENT = "release-radar/0.1 (+https://pypi.org/project/pip/)"
DEFAULT_TIMEOUT = 15.0


class FetchError(RuntimeError):
    """Release notes could not be retrieved."""


def _get_json(url: str, timeout: float, token: Optional[str] = None) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and "api.github.com" in url:
            raise FetchError(
                "GitHub rate limit hit (60 requests/hour unauthenticated). "
                "Set GITHUB_TOKEN to raise it to 5000."
            ) from exc
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error for {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"malformed JSON from {url}: {exc}") from exc


def github_repo_from_pypi(metadata: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Find the (owner, repo) a PyPI project points at, if any."""
    info = metadata.get("info") or {}
    candidates: List[str] = []
    project_urls = info.get("project_urls") or {}
    # Source/Repository first - Homepage is often a docs site.
    preferred = ("source", "source code", "repository", "code", "github", "homepage")
    for key in preferred:
        for name, url in project_urls.items():
            if url and name.strip().lower() == key:
                candidates.append(url)
    candidates.extend(u for u in project_urls.values() if u)
    for key in ("home_page", "download_url"):
        if info.get(key):
            candidates.append(info[key])

    for url in candidates:
        match = re.search(
            r"github\.com[/:]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:/|$)",
            str(url),
        )
        if match:
            return match.group(1), match.group(2)
    return None


def _version_keys(version: str) -> List[str]:
    """Tag spellings that plausibly mean this version."""
    stripped = version.strip().lstrip("vV")
    return [stripped, f"v{stripped}"]


def _tag_matches(tag: str, version: str) -> bool:
    """`v2.0`, `2.0`, `pydantic-2.0`, `release-2.0` all mean version 2.0."""
    tag = (tag or "").strip()
    if not tag:
        return False
    keys = _version_keys(version)
    if tag in keys or tag.lstrip("vV") == version.lstrip("vV"):
        return True
    suffix = tag.rsplit("-", 1)[-1] if "-" in tag else ""
    return bool(suffix) and suffix.lstrip("vV") in keys


def github_release_notes(
    owner: str,
    repo: str,
    version: str,
    timeout: float = DEFAULT_TIMEOUT,
    token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The release object whose tag matches `version`, or None."""
    releases = _get_json(
        GITHUB_RELEASES.format(owner=owner, repo=repo), timeout, token
    )
    if not isinstance(releases, list):
        return None
    for release in releases:
        if _tag_matches(release.get("tag_name", ""), version) or _tag_matches(
            release.get("name", "") or "", version
        ):
            return release
    return None


RAW_FILE = "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}"
CHANGELOG_PATHS = (
    "CHANGES.rst", "CHANGELOG.md", "CHANGELOG.rst", "CHANGES.md",
    "CHANGELOG", "CHANGES", "HISTORY.md", "HISTORY.rst", "NEWS.rst",
    "docs/changelog.md", "docs/changelog.rst", "docs/CHANGELOG.md",
)

# A heading that announces a version: `## 2.0`, `Version 2.0`, `v2.0 (2023-..)`,
# or a bare `2.0` on its own line above an RST underline.
_VERSION_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,4}\s*)?(?:Version\s+|Release\s+|v)?"
    r"(\d+\.\d+(?:\.\d+)?(?:[-.\w]*)?)\s*"
    r"(?:\(|\[|-|:|$)",
    re.IGNORECASE,
)

# Below this, a changelog section is a stub (a date and a link) and the
# release body is the better source.
MIN_CHANGELOG_CHARS = 400

# An auto-generated "What's Changed" body names PRs, not APIs.
_PR_LINE = re.compile(r"\bby @[\w-]+ in https?://github\.com/\S+/pull/\d+")


def looks_like_pr_list(body: str) -> bool:
    """True when a release body is mostly GitHub's generated PR list."""
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return True
    pr_lines = sum(1 for line in lines if _PR_LINE.search(line))
    return pr_lines >= max(3, len(lines) // 2)


def _normalize_version(value: str) -> str:
    return value.strip().lstrip("vV")


def find_version_section(text: str, version: str) -> Optional[str]:
    """The slice of a changelog that belongs to one version."""
    target = _normalize_version(version)
    lines = text.splitlines()
    headings: List[Tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _VERSION_HEADING.match(line)
        if match:
            headings.append((index, _normalize_version(match.group(1))))

    for position, (index, found) in enumerate(headings):
        if found != target:
            continue
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        section = "\n".join(lines[index:end]).strip()
        if section:
            return section
    return None


def changelog_notes(
    owner: str,
    repo: str,
    version: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[Tuple[str, str]]:
    """(section, url) from the repo's changelog file, if one covers `version`."""
    headers = {"User-Agent": USER_AGENT}
    for path in CHANGELOG_PATHS:
        url = RAW_FILE.format(owner=owner, repo=repo, path=path)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        section = find_version_section(text, version)
        if section:
            return section, url
    return None


def fetch_release_notes(
    package: str,
    version: str,
    timeout: float = DEFAULT_TIMEOUT,
    token: Optional[str] = None,
) -> ReleaseNotes:
    """Release notes for `package==version`, from GitHub if possible."""
    token = (token or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    metadata = _get_json(
        PYPI_JSON.format(package=urllib.parse.quote(package)), timeout
    )
    info = metadata.get("info") or {}
    repo = github_repo_from_pypi(metadata)
    if repo:
        owner, name = repo
        try:
            release = github_release_notes(owner, name, version, timeout, token)
        except FetchError:
            release = None  # fall through rather than fail the run
        body = (release or {}).get("body") or ""

        # Two bad release bodies are common: a generated "What's Changed" PR
        # list, which names no APIs, and a two-line summary that points at the
        # changelog instead of listing anything. In both cases the project's
        # own changelog is the better source - but only when it actually has
        # substance, so a stub section never beats a real release body.
        if not body.strip() or looks_like_pr_list(body) or len(body) < 1500:
            changelog = changelog_notes(owner, name, version, timeout)
            if changelog:
                section, url = changelog
                if len(section) >= MIN_CHANGELOG_CHARS:
                    return ReleaseNotes(
                        package=package, version=version,
                        source="changelog", url=url, body=section,
                    )

        if body.strip():
            return ReleaseNotes(
                package=package,
                version=version,
                source="github",
                url=release.get("html_url"),
                body=body,
            )

    description = (info.get("description") or "").strip()
    if description:
        return ReleaseNotes(
            package=package,
            version=version,
            source="pypi",
            url=info.get("project_url") or info.get("package_url"),
            body=description,
        )

    raise FetchError(
        f"no release notes found for {package}=={version}. "
        f"Pass --notes-file to supply them directly."
    )


def load_notes_file(path: str, package: str, version: str) -> ReleaseNotes:
    """Release notes read from a local file (offline runs and tests)."""
    with open(path, "r", encoding="utf-8") as handle:
        body = handle.read()
    return ReleaseNotes(
        package=package, version=version, source="file", url=path, body=body
    )
