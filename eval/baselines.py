"""The two things Radar has to beat.

Both are honest attempts at the same job, not strawmen:

- `keyword_baseline` is what you would write in an afternoon: find the lines
  of the release notes that sound like removals, pull the identifiers out of
  them, and grep the repo. No model, no parser.
- `llm_only_baseline` is the obvious LLM answer: hand the model the notes
  and the repo's imports and ask it, directly, what breaks. It gets exactly
  the same information Radar has - the only difference is who decides.

The comparison is therefore not "LLM vs no LLM". It is "who decides", which
is the design claim the project makes.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import List, Optional, Set

from radar.extract import OPENROUTER_URL, ExtractionError, Usage
from radar.models import ReleaseNotes
from radar.scan import ScanResult

# Lines that announce something going away.
_REMOVAL_LINE = re.compile(
    r"\b(remove[sd]?|removal|delete[sd]?|drop(?:s|ped)?|no longer|"
    r"renamed?|deprecat\w*|breaking)\b",
    re.IGNORECASE,
)

# Identifiers a changelog line might be naming: `backticked`, CamelCase, or
# snake_case with a dotted path.
_IDENTIFIER = re.compile(r"`([A-Za-z_][\w.]*)`|\b([A-Z][A-Za-z0-9]{3,})\b")

_STOPWORDS = {
    "Python", "None", "True", "False", "This", "These", "Those", "Added",
    "Fixed", "Changed", "Removed", "Deprecated", "Release", "Support",
    "Version", "Breaking", "Note", "Notes", "Warning", "Also", "When",
    "With", "Using", "The", "For", "And", "But", "All", "Now",
}


def keyword_baseline(notes: ReleaseNotes, scan: ScanResult, package: str) -> List[str]:
    """Identifiers from removal-ish lines that appear anywhere in the repo.

    Matching is by bare name, the way a grep would do it - it has no way to
    tell `flask.json.JSONEncoder` from anybody else's `JSONEncoder`.
    """
    candidates: Set[str] = set()
    for line in notes.body.splitlines():
        if not _REMOVAL_LINE.search(line):
            continue
        for backticked, camel in _IDENTIFIER.findall(line):
            name = backticked or camel
            if not name or name in _STOPWORDS:
                continue
            candidates.add(name)

    flagged: Set[str] = set()
    for candidate in candidates:
        tail = candidate.rsplit(".", 1)[-1]
        for qualified in scan.usages:
            if qualified.rsplit(".", 1)[-1] == tail or qualified.endswith("." + candidate):
                flagged.add(qualified)
    return sorted(flagged)


LLM_ONLY_PROMPT = """\
You are deciding whether a dependency release breaks a specific repository.

Release notes for {package} {version}:
<notes>
{body}
</notes>

Every name this repository imports from {package}:
<imports>
{imports}
</imports>

Which of those imported names will break if the repository upgrades to this
version? Return the fully-qualified names exactly as they appear in the
imports list, and a verdict.
"""

_LLM_ONLY_SCHEMA = {
    "type": "object",
    "properties": {
        "breaking_symbols": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Imported names that break, exactly as listed.",
        },
        "verdict": {
            "type": "string",
            "enum": ["BREAKING", "SECURITY", "ROUTINE"],
        },
    },
    "required": ["breaking_symbols", "verdict"],
    "additionalProperties": False,
}


def llm_only_baseline(
    notes: ReleaseNotes,
    scan: ScanResult,
    package: str,
    module: str,
    model: str,
    timeout: float = 120.0,
) -> tuple:
    """Ask the model to make the whole decision. Returns (symbols, verdict, usage)."""
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise ExtractionError("OPENROUTER_API_KEY is not set")

    imported = sorted(u.qualified_name for u in scan.for_module(module.split(".")[0]))
    payload = {
        "model": model,
        "max_tokens": 4000,
        "messages": [{
            "role": "user",
            "content": LLM_ONLY_PROMPT.format(
                package=package, version=notes.version, body=notes.body,
                imports="\n".join(imported) or "(none)",
            ),
        }],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "breaking_decision", "strict": True,
                "schema": _LLM_ONLY_SCHEMA,
            },
        },
        "usage": {"include": True},
    }
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "release-radar-eval",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))

    usage_data = data.get("usage") or {}
    usage = Usage(
        input_tokens=usage_data.get("prompt_tokens", 0) or 0,
        output_tokens=usage_data.get("completion_tokens", 0) or 0,
        reported_cost_usd=usage_data.get("cost"),
    )
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    if not content:
        raise ExtractionError("LLM-only baseline returned no content")
    parsed = json.loads(content)
    return sorted(set(parsed.get("breaking_symbols", []))), parsed.get("verdict"), usage
