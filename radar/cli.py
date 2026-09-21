"""Command line entry point - runs the five stages end to end.

    python -m radar check --repo ./myapp --package pydantic --version 2.0

Subcommands `scan` and `extract` run stage 3 and stage 2 on their own,
which is how you tell an extraction problem from a matching problem.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import report
from .extract import (
    LARGE_NOTES_CHARS,
    MODEL,
    OPENROUTER_MODEL,
    ClaudeExtractor,
    ExtractionError,
    OpenRouterExtractor,
    StubExtractor,
)
from .fetch import FetchError, fetch_release_notes, load_notes_file
from .match import build_alert, module_for_distribution
from .models import ExtractedChanges, Verdict
from .scan import scan_repo

EXIT_OK = 0
EXIT_BREAKING = 1
EXIT_ERROR = 2


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _force_utf8_output() -> None:
    """Release notes are full of emoji; a Windows console defaults to cp1252.

    Without this, printing an alert whose detail quotes the notes dies with
    a UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # already detached or not a tty
            pass


def _load_notes(args):
    if args.notes_file:
        return load_notes_file(args.notes_file, args.package, args.version)
    return fetch_release_notes(args.package, args.version, timeout=args.timeout)


def load_dotenv(path: str = ".env") -> None:
    """Minimal KEY=VALUE loader - real environment variables always win."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _build_extractor(args):
    if args.changes_file:
        with open(args.changes_file, "r", encoding="utf-8") as handle:
            changes = ExtractedChanges.model_validate(json.load(handle))
        return StubExtractor(changes)

    provider = args.provider
    if provider == "auto":
        provider = "openrouter" if os.environ.get("OPENROUTER_API_KEY") else "anthropic"
    if provider == "openrouter":
        return OpenRouterExtractor(
            model=args.model or OPENROUTER_MODEL, timeout=args.llm_timeout
        )
    return ClaudeExtractor(model=args.model or MODEL)


def cmd_check(args) -> int:
    try:
        notes = _load_notes(args)
    except (FetchError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if len(notes.body) > LARGE_NOTES_CHARS:
        _warn(
            f"release notes are {len(notes.body):,} characters "
            f"(source: {notes.source}); this is a large extraction request"
        )

    try:
        extractor = _build_extractor(args)
        changes = extractor.extract(notes)
    except (ExtractionError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        scan = scan_repo(args.repo)
    except (NotADirectoryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    alert = build_alert(
        changes=changes,
        scan=scan,
        package=args.package,
        version=args.version,
        repo=str(Path(args.repo).resolve()),
        module=args.module,
        notes_url=notes.url,
    )

    if args.json:
        print(report.to_json(alert))
    elif args.oneline:
        print(report.one_line(alert))
    else:
        print(report.render(alert))
        if scan.files_skipped and args.verbose:
            print(f"\n   {len(scan.files_skipped)} file(s) could not be parsed:")
            for path, error in scan.files_skipped[:5]:
                print(f"       {path}: {error}")

    usage = getattr(extractor, "last_usage", None)
    if usage and args.show_cost:
        print(f"\n   extraction: {usage.summary()}", file=sys.stderr)

    return EXIT_BREAKING if alert.verdict is Verdict.BREAKING else EXIT_OK


def cmd_scan(args) -> int:
    try:
        scan = scan_repo(args.repo)
    except (NotADirectoryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.package:
        top = (args.module or module_for_distribution(args.package)).split(".")[0]
        usages = sorted(scan.for_module(top), key=lambda u: u.qualified_name)
    else:
        usages = sorted(scan.usages.values(), key=lambda u: u.qualified_name)

    if args.json:
        print(json.dumps([u.model_dump(mode="json") for u in usages], indent=2))
        return EXIT_OK

    print(f"scanned {scan.files_scanned} file(s) under {scan.root}")
    if scan.files_skipped:
        print(f"skipped {len(scan.files_skipped)} unparseable file(s)")
    for usage in usages:
        sites = ", ".join(f"{s.location()}" for s in usage.sites[:3])
        extra = "" if len(usage.sites) <= 3 else f" (+{len(usage.sites) - 3})"
        print(f"  {usage.qualified_name:<50} {sites}{extra}")
    for site in scan.star_imports:
        print(f"  ! unresolved `from {site.local_name} import *` at {site.location()}")
    return EXIT_OK


def cmd_extract(args) -> int:
    try:
        notes = _load_notes(args)
        extractor = _build_extractor(args)
        changes = extractor.extract(notes)
    except (FetchError, ExtractionError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(json.dumps(changes.model_dump(mode="json"), indent=2))
    usage = getattr(extractor, "last_usage", None)
    if usage and args.show_cost:
        print(f"extraction: {usage.summary()}", file=sys.stderr)
    return EXIT_OK


def _add_notes_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--package", required=True, help="PyPI distribution name")
    parser.add_argument("--version", required=True, help="release version, e.g. 2.0")
    parser.add_argument(
        "--notes-file",
        help="read release notes from a file instead of GitHub/PyPI",
    )
    parser.add_argument(
        "--changes-file",
        help="skip the LLM and use a saved ExtractedChanges JSON file",
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "openrouter", "anthropic"),
        default="auto",
        help="auto uses OpenRouter when OPENROUTER_API_KEY is set",
    )
    parser.add_argument("--model", default=None, help="model id override")
    parser.add_argument(
        "--llm-timeout", type=float, default=120.0,
        help="seconds to wait for the extraction request",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="HTTP timeout in seconds"
    )
    parser.add_argument(
        "--show-cost", action="store_true", help="print token use and cost to stderr"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar",
        description="Tell a Python repo which dependency releases break it.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check", help="run all five stages and print an alert"
    )
    check.add_argument("--repo", required=True, help="path to the Python repo")
    _add_notes_args(check)
    check.add_argument(
        "--module", help="import name, if it differs from the distribution name"
    )
    check.add_argument("--json", action="store_true", help="emit the alert as JSON")
    check.add_argument("--oneline", action="store_true", help="emit one summary line")
    check.add_argument(
        "--verbose", action="store_true", help="also list unparseable files"
    )
    check.set_defaults(func=cmd_check)

    scan = subparsers.add_parser("scan", help="stage 3 only - what the repo imports")
    scan.add_argument("--repo", required=True)
    scan.add_argument("--package", help="limit output to one distribution")
    scan.add_argument("--module", help="import name override")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    extract = subparsers.add_parser(
        "extract", help="stage 2 only - what the notes say changed"
    )
    _add_notes_args(extract)
    extract.set_defaults(func=cmd_extract)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_output()
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
