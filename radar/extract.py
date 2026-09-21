"""Stage 2 - the only stage that uses an LLM.

The model reads release-note prose and returns the APIs the notes say were
removed or renamed. That is all it does. It is never asked whether a
release is "breaking" - that verdict belongs to `match.py`, which compares
these names against what the repo really imports and can point at a line
of code. Keeping the judgement out of the model is what makes every alert
checkable.

The schema is enforced by the API (structured outputs), not by parsing
free text, so a malformed extraction is a hard error rather than a silent
wrong answer.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from .models import ExtractedChanges, ReleaseNotes

MODEL = "claude-opus-5"
# Extraction is the easy job - read prose, copy names into a schema - and the
# matcher discards anything malformed anyway, so the default is a small model.
# Measured on the pydantic 2.0 notes: flash-lite, haiku-4.5, llama-4-scout and
# mistral-small all found `BaseSettings`; flash-lite was fastest and gpt-5-nano
# was the only one to invent an extra removal. Opus 5 cost 57x more for the
# same answer. Override with --model.
OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOKENS = 8000

# Per million tokens, Claude Opus 5 (see README - update when pricing moves).
INPUT_COST_PER_MTOK = 5.00
OUTPUT_COST_PER_MTOK = 25.00

SYSTEM_PROMPT = """\
You extract API changes from Python package release notes.

Report only what the notes state. Your output feeds a static analyser that \
decides, on its own, whether a change affects a particular repository - so do \
not assess severity, do not say whether anything is "breaking", and do not \
infer changes the notes do not mention.

Rules:
- `removed`: public APIs the notes say were removed, deleted, dropped, or \
moved out of this package. A deprecation with no removal in THIS release is \
not a removal.
- `renamed`: APIs that kept working under a new name. Record both names.
- `behaviour_changed`: APIs that still exist under the same name but behave \
differently (different default, different return type, stricter validation).
- Use the dotted path exactly as the notes write it (`BaseSettings`, \
`pydantic.tools.parse_obj_as`). Do not invent a prefix the notes do not use, \
and do not expand an abbreviation.
- CLI flags, config keys, and dependency bumps are not APIs. Skip them.
- `security` is true only if this release fixes a vulnerability or the notes \
cite an advisory or CVE.
- If the notes describe none of the above, return empty lists. An empty \
extraction is a correct answer, and a better one than a guess.\
"""

USER_TEMPLATE = """\
Package: {package}
Version: {version}
Source: {source}

Release notes:
<notes>
{body}
</notes>
"""

# Long enough to be worth flagging; the notes are never silently truncated.
LARGE_NOTES_CHARS = 120_000


@dataclass
class Usage:
    """Token spend for one extraction - the per-run cost line."""

    input_tokens: int = 0
    output_tokens: int = 0
    reported_cost_usd: Optional[float] = None  # OpenRouter bills this directly

    @property
    def cost_usd(self) -> float:
        if self.reported_cost_usd is not None:
            return self.reported_cost_usd
        return (
            self.input_tokens / 1_000_000 * INPUT_COST_PER_MTOK
            + self.output_tokens / 1_000_000 * OUTPUT_COST_PER_MTOK
        )

    def summary(self) -> str:
        return (
            f"{self.input_tokens} in / {self.output_tokens} out tokens "
            f"= ${self.cost_usd:.4f}"
        )


class Extractor(Protocol):
    """Anything that turns release notes into structured changes."""

    last_usage: Optional[Usage]

    def extract(self, notes: ReleaseNotes) -> ExtractedChanges: ...


class ExtractionError(RuntimeError):
    """The model could not be reached, or returned nothing usable."""


class ClaudeExtractor:
    """Extraction via the Anthropic API, with the schema enforced server-side."""

    def __init__(self, client=None, model: str = MODEL):
        self.model = model
        self.last_usage: Optional[Usage] = None
        if client is not None:
            self._client = client
        else:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency check
                raise ExtractionError(
                    "the `anthropic` package is required: pip install anthropic"
                ) from exc
            # No key check here: the SDK also resolves ANTHROPIC_AUTH_TOKEN
            # and an `ant auth login` profile, so an unset ANTHROPIC_API_KEY
            # is not by itself a missing credential.
            self._client = anthropic.Anthropic()

    def extract(self, notes: ReleaseNotes) -> ExtractedChanges:
        if notes.is_empty():
            self.last_usage = Usage()
            return ExtractedChanges()

        prompt = USER_TEMPLATE.format(
            package=notes.package,
            version=notes.version,
            source=notes.source,
            body=notes.body,
        )
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_format=ExtractedChanges,
            )
        except Exception as exc:  # surfaced with context; see cli.py
            raise ExtractionError(f"extraction failed: {exc}") from exc

        usage = getattr(response, "usage", None)
        self.last_usage = Usage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            stop = getattr(response, "stop_reason", None)
            raise ExtractionError(
                f"model returned no structured output (stop_reason={stop!r})"
            )
        return parsed


def _strict_schema(model: type) -> Dict[str, Any]:
    """Pydantic JSON schema, tightened for strict structured outputs.

    Strict mode requires every object to forbid extra properties and to list
    all of its properties as required - so a field the model skips comes back
    as an empty list, never as a missing key.
    """
    schema = model.model_json_schema()

    def tighten(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                tighten(value)
        elif isinstance(node, list):
            for item in node:
                tighten(item)

    tighten(schema)
    return schema


class OpenRouterExtractor:
    """Extraction through OpenRouter, using its OpenAI-compatible endpoint.

    Same contract as `ClaudeExtractor` - the model still only reads prose and
    returns the schema; the verdict still happens in `match.py`. Written
    against the HTTP API with the standard library so the project does not
    take a second SDK dependency for one request.
    """

    def __init__(
        self,
        model: str = OPENROUTER_MODEL,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.model = model
        self.timeout = timeout
        self.last_usage: Optional[Usage] = None
        # .strip() is load-bearing: a key pasted into a hosting dashboard
        # often carries a trailing newline, and Python refuses to put a
        # newline in an HTTP header ("Invalid header value").
        self.api_key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if not self.api_key:
            raise ExtractionError(
                "OPENROUTER_API_KEY is not set (put it in .env or the environment)"
            )

    def extract(self, notes: ReleaseNotes) -> ExtractedChanges:
        if notes.is_empty():
            self.last_usage = Usage()
            return ExtractedChanges()

        payload = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": USER_TEMPLATE.format(
                        package=notes.package,
                        version=notes.version,
                        source=notes.source,
                        body=notes.body,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "extracted_changes",
                    "strict": True,
                    "schema": _strict_schema(ExtractedChanges),
                },
            },
            "usage": {"include": True},  # ask OpenRouter for the actual cost
        }
        request = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "release-radar",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ExtractionError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ExtractionError(f"OpenRouter request failed: {exc}") from exc

        usage = data.get("usage") or {}
        self.last_usage = Usage(
            input_tokens=usage.get("prompt_tokens", 0) or 0,
            output_tokens=usage.get("completion_tokens", 0) or 0,
            reported_cost_usd=usage.get("cost"),
        )

        if data.get("error"):
            raise ExtractionError(f"OpenRouter error: {data['error']}")
        choices = data.get("choices") or []
        if not choices:
            raise ExtractionError(f"OpenRouter returned no choices: {str(data)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        finish = choices[0].get("finish_reason")
        if not content:
            raise ExtractionError(
                f"model returned no structured output (finish_reason={finish!r})"
            )
        try:
            return ExtractedChanges.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ExtractionError(
                f"structured output did not match the schema: {exc}"
            ) from exc


class StubExtractor:
    """A fixed extraction - for tests and for running the engine offline."""

    def __init__(self, changes: ExtractedChanges):
        self.changes = changes
        self.last_usage: Optional[Usage] = Usage()

    def extract(self, notes: ReleaseNotes) -> ExtractedChanges:
        return self.changes
