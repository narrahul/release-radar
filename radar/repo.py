"""Get a GitHub repo onto disk so stage 3 can scan it.

The CLI scans a local path. A hosted demo cannot - it has to fetch the repo
first, from a URL a stranger typed. So this module is written defensively:
it streams the zipball with a hard byte cap, extracts only `.py` files, and
refuses any archive entry whose path escapes the destination directory.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

ZIPBALL = "https://api.github.com/repos/{owner}/{repo}/zipball"
USER_AGENT = "release-radar/0.1"

MAX_ARCHIVE_BYTES = 80 * 1024 * 1024  # a repo bigger than this is not a demo
MAX_PYTHON_FILES = 5000
MAX_FILE_BYTES = 2 * 1024 * 1024  # one absurd generated file should not win


class RepoError(RuntimeError):
    """The repository could not be fetched or unpacked."""


@dataclass
class FetchedRepo:
    owner: str
    name: str
    path: Path  # directory to scan
    tempdir: Optional[str] = None  # caller cleans this up
    python_files: int = 0

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    def cleanup(self) -> None:
        if self.tempdir:
            shutil.rmtree(self.tempdir, ignore_errors=True)


def parse_repo(value: str) -> Tuple[str, str]:
    """Accept `owner/repo`, a github.com URL, or a git@ remote."""
    text = (value or "").strip().rstrip("/")
    if not text:
        raise RepoError("no repository given")
    match = re.search(
        r"github\.com[/:]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:/.*)?$", text
    )
    if match:
        return match.group(1), match.group(2)
    match = re.fullmatch(r"([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)", text)
    if match:
        return match.group(1), match.group(2)
    raise RepoError(f"not a GitHub repository: {value!r}")


def _download(url: str, destination: Path, token: Optional[str], timeout: float) -> None:
    """Stream to disk, refusing anything over the cap mid-download."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_ARCHIVE_BYTES:
                raise RepoError(
                    f"repository archive is {int(declared) // 1024 // 1024} MB; "
                    f"the limit is {MAX_ARCHIVE_BYTES // 1024 // 1024} MB"
                )
            written = 0
            with open(destination, "wb") as handle:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise RepoError(
                            "repository archive exceeds "
                            f"{MAX_ARCHIVE_BYTES // 1024 // 1024} MB"
                        )
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RepoError("repository not found (or private)") from exc
        if exc.code in (403, 429):
            raise RepoError(
                "GitHub rate limit hit; set GITHUB_TOKEN to raise it"
            ) from exc
        raise RepoError(f"GitHub returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RepoError(f"network error fetching the repository: {exc.reason}") from exc


def extract_python_files(archive: Path, destination: Path) -> int:
    """Unpack only the `.py` files, safely. Returns how many were written.

    GitHub wraps everything in a single `owner-repo-sha/` directory, which is
    stripped so the scanned paths match what a reader sees on GitHub.
    """
    written = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                if info.is_dir() or not info.filename.endswith((".py", ".pyi")):
                    continue
                if info.file_size > MAX_FILE_BYTES:
                    continue
                parts = Path(info.filename).parts[1:]  # drop the wrapper directory
                if not parts:
                    continue
                relative = Path(*parts)
                if relative.is_absolute() or ".." in relative.parts:
                    continue  # zip-slip attempt; skip it silently
                target = (destination / relative).resolve()
                if not str(target).startswith(str(destination.resolve())):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle, length=64 * 1024)
                written += 1
                if written >= MAX_PYTHON_FILES:
                    break
    except zipfile.BadZipFile as exc:
        raise RepoError(f"downloaded archive is not a valid zip: {exc}") from exc
    return written


def fetch_repo(
    value: str, token: Optional[str] = None, timeout: float = 60.0
) -> FetchedRepo:
    """Download a GitHub repo and return a directory of its Python files."""
    owner, name = parse_repo(value)
    token = token or os.environ.get("GITHUB_TOKEN") or None

    tempdir = tempfile.mkdtemp(prefix="radar-repo-")
    archive = Path(tempdir) / "repo.zip"
    source = Path(tempdir) / "src"
    source.mkdir()
    try:
        _download(ZIPBALL.format(owner=owner, repo=name), archive, token, timeout)
        count = extract_python_files(archive, source)
        archive.unlink(missing_ok=True)
    except Exception:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise

    if count == 0:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise RepoError(f"{owner}/{name} contains no Python files to scan")

    return FetchedRepo(
        owner=owner, name=name, path=source, tempdir=tempdir, python_files=count
    )
