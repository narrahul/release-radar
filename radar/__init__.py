"""Release Radar - which dependency releases actually break this repo.

Five stages, in order:

    fetch   -> release notes for one package version      (fetch.py)
    extract -> what the notes say changed, via an LLM     (extract.py)
    scan    -> what the repo really imports, via `ast`    (scan.py)
    match   -> which changes touch the repo, in plain code(match.py)
    report  -> the alert, with file:line evidence         (report.py)

The model reads prose. The code decides.
"""

from .models import Alert, ExtractedChanges, ReleaseNotes, Verdict

__version__ = "0.1.0"

__all__ = ["Alert", "ExtractedChanges", "ReleaseNotes", "Verdict", "__version__"]
