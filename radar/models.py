"""Data model shared by the five stages of the engine.

The split matters: `ExtractedChanges` is what the LLM is allowed to say
(what a release *claims* changed) and `Alert` is what the engine decides
(whether that change touches this repo). Nothing in `ExtractedChanges`
carries a verdict.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# --- stage 1: release notes -------------------------------------------------


class ReleaseNotes(BaseModel):
    """Raw prose for one release, plus where it came from."""

    package: str
    version: str
    source: str  # "github" | "pypi"
    url: Optional[str] = None
    body: str

    def is_empty(self) -> bool:
        return not self.body.strip()


# --- stage 2: LLM extraction ------------------------------------------------
#
# This is the schema handed to the model as a structured output. Keep the
# field docs written *at the model* - they are the prompt as much as the
# system prompt is.


class RemovedApi(BaseModel):
    name: str = Field(
        description=(
            "Dotted path of the removed public API as it appears in the notes, "
            "e.g. 'BaseSettings' or 'pydantic.tools.parse_obj_as'."
        )
    )
    note: str = Field(
        default="",
        description="Short quote or paraphrase from the notes describing the removal.",
    )


class RenamedApi(BaseModel):
    old_name: str = Field(description="Dotted path of the API under its old name.")
    new_name: str = Field(description="Dotted path of the API under its new name.")
    note: str = Field(default="", description="Short supporting detail from the notes.")


class BehaviourChange(BaseModel):
    name: str = Field(
        description="Dotted path of the API whose behaviour changed, if the notes name one."
    )
    note: str = Field(default="", description="What changed about it.")


class ExtractedChanges(BaseModel):
    """What the release notes say changed. No judgement about any repo."""

    removed: List[RemovedApi] = Field(default_factory=list)
    renamed: List[RenamedApi] = Field(default_factory=list)
    behaviour_changed: List[BehaviourChange] = Field(default_factory=list)
    security: bool = Field(
        default=False,
        description="True only if the notes describe a security fix or advisory in this release.",
    )
    security_note: str = Field(default="", description="The security detail, if any.")

    def is_empty(self) -> bool:
        return not (self.removed or self.renamed or self.behaviour_changed or self.security)


# --- stage 3: repo scan -----------------------------------------------------


class UsageSite(BaseModel):
    """One place in the repo where a third-party name is imported or used."""

    file: str  # repo-relative, forward slashes
    line: int
    kind: str  # "import" | "reference"
    local_name: str  # the name as bound/used in that file

    def location(self) -> str:
        return f"{self.file}:{self.line}"


class SymbolUsage(BaseModel):
    """Every site in the repo that touches one fully-qualified imported name."""

    qualified_name: str  # e.g. "pydantic.BaseSettings"
    module: str  # e.g. "pydantic"
    sites: List[UsageSite] = Field(default_factory=list)

    @property
    def import_site(self) -> UsageSite:
        """The import is the canonical evidence - it is where the break lands."""
        for site in self.sites:
            if site.kind == "import":
                return site
        return self.sites[0]


# --- stage 4/5: verdict and alert -------------------------------------------


class Verdict(str, Enum):
    BREAKING = "BREAKING"
    SECURITY = "SECURITY"
    ROUTINE = "ROUTINE"


class Finding(BaseModel):
    """One extracted change that the repo actually uses."""

    kind: str  # "removed" | "renamed"
    symbol: str  # qualified name as used in the repo
    detail: str  # human-readable reason
    evidence: List[UsageSite] = Field(default_factory=list)

    def primary_location(self) -> Optional[str]:
        return self.evidence[0].location() if self.evidence else None


class Alert(BaseModel):
    repo: str
    package: str
    version: str
    verdict: Verdict
    findings: List[Finding] = Field(default_factory=list)
    security_note: str = ""
    reason: str = ""  # why this verdict, in one line
    notes_url: Optional[str] = None
