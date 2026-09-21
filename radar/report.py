"""Stage 5 - render the alert.

One line for a feed, a short block for a human, JSON for anything
downstream. Every line that says BREAKING carries a `file:line` next to
it, because an alert you cannot check is an alert you learn to ignore.
"""

from __future__ import annotations

import json
from typing import List

from .models import Alert, Verdict

_MARK = {
    Verdict.BREAKING: "!!",
    Verdict.SECURITY: "**",
    Verdict.ROUTINE: "--",
}


def one_line(alert: Alert) -> str:
    """`pydantic 2.0 -> BREAKING - BaseSettings removed - app/config.py:4`"""
    head = f"{alert.package} {alert.version} -> {alert.verdict.value}"
    if alert.findings:
        first = alert.findings[0]
        where = first.primary_location() or "no location"
        summary = f"{first.symbol} {first.kind}"
        more = f" (+{len(alert.findings) - 1} more)" if len(alert.findings) > 1 else ""
        return f"{head} - {summary} - {where}{more}"
    return f"{head} - {alert.reason}"


def render(alert: Alert, show_evidence: bool = True) -> str:
    """Multi-line report for a terminal."""
    lines: List[str] = []
    mark = _MARK[alert.verdict]
    lines.append(f"{mark} {alert.package} {alert.version} -> {alert.verdict.value}")
    lines.append(f"   repo: {alert.repo}")
    lines.append(f"   why:  {alert.reason}")
    if alert.notes_url:
        lines.append(f"   notes: {alert.notes_url}")

    if alert.security_note:
        lines.append("")
        lines.append(f"   security: {alert.security_note.strip()}")

    if alert.findings:
        lines.append("")
        for finding in alert.findings:
            lines.append(f"   [{finding.kind}] {finding.detail}")
            if show_evidence:
                for site in finding.evidence:
                    lines.append(
                        f"       used at {site.location()}"
                        f"  ({site.kind} of `{site.local_name}`)"
                    )
    return "\n".join(lines)


def to_json(alert: Alert, indent: int = 2) -> str:
    return json.dumps(alert.model_dump(mode="json"), indent=indent, sort_keys=False)
