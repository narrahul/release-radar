"""Score Radar against two baselines on the golden set.

    python -m eval.run_eval              # scored from cached extractions
    python -m eval.run_eval --refresh    # re-run every model call

Two numbers matter, and they are not the same question:

- **symbol-level** precision/recall: of the APIs a system names, how many
  were really removed *and* really used here, and how many did it miss.
- **verdict-level** accuracy: would you have been told to look at this
  upgrade at all.

Precision is the one to optimise. A false BREAKING costs a developer a
wasted investigation, and a few of those and nobody reads the alerts again.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

from radar.cli import load_dotenv
from radar.extract import OPENROUTER_MODEL, ExtractedChanges, OpenRouterExtractor, Usage
from radar.match import build_alert, module_for_distribution
from radar.models import Verdict

from .baselines import keyword_baseline, llm_only_baseline
from .cache import GOLDEN, load_notes, load_scan

load_dotenv()

CASES_PATH = GOLDEN / "cases.json"
EXTRACTIONS = GOLDEN / "extractions"
RESULTS_PATH = Path(__file__).parent / "results.json"


@dataclass
class Score:
    """Micro-averaged over every case: one symbol, one vote."""

    name: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    verdict_correct: int = 0
    verdict_total: int = 0
    cost_usd: float = 0.0
    mistakes: List[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else 1.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 1.0

    @property
    def f1(self) -> float:
        if not (self.precision + self.recall):
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)

    @property
    def verdict_accuracy(self) -> float:
        return self.verdict_correct / self.verdict_total if self.verdict_total else 0.0

    def add(self, predicted: Sequence[str], expected: Sequence[str],
            case_id: str, verdict: str, expected_verdict: str) -> None:
        got, want = set(predicted), set(expected)
        self.true_positives += len(got & want)
        self.false_positives += len(got - want)
        self.false_negatives += len(want - got)
        self.verdict_total += 1
        if verdict == expected_verdict:
            self.verdict_correct += 1
        for symbol in sorted(got - want):
            self.mistakes.append(f"FP  {case_id}: {symbol}")
        for symbol in sorted(want - got):
            self.mistakes.append(f"FN  {case_id}: {symbol}")


def _extraction_path(model: str, package: str, version: str) -> Path:
    return EXTRACTIONS / f"{model.replace('/', '__')}__{package}-{version}.json"


def extraction_for(package: str, version: str, model: str, refresh: bool) -> ExtractedChanges:
    """Radar's stage 2, cached so a scoring run is free and repeatable."""
    path = _extraction_path(model, package, version)
    if path.exists() and not refresh:
        return ExtractedChanges.model_validate_json(path.read_text(encoding="utf-8"))
    notes = load_notes(package, version)
    changes = OpenRouterExtractor(model=model).extract(notes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(changes.model_dump_json(indent=2), encoding="utf-8")
    return changes


def _baseline_verdict(symbols: Sequence[str]) -> str:
    return Verdict.BREAKING.value if symbols else Verdict.ROUTINE.value


def run(model: str, refresh: bool, skip_llm_baseline: bool) -> Dict[str, Score]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    scores = {
        "radar": Score("Radar (LLM extract + AST match)"),
        "keyword": Score("Keyword baseline (no model)"),
    }
    if not skip_llm_baseline:
        scores["llm_only"] = Score("LLM-only baseline (model decides)")

    print(f"{'case':<40}{'expected':<10}{'radar':<10}{'keyword':<10}{'llm-only':<10}")
    print("-" * 80)

    for case in cases:
        notes = load_notes(case["package"], case["version"])
        scan = load_scan(case["repo"], case.get("ref", ""))
        module = module_for_distribution(case["package"])
        expected, expected_verdict = case["symbols"], case["verdict"]

        changes = extraction_for(case["package"], case["version"], model, refresh)
        alert = build_alert(
            changes=changes, scan=scan, package=case["package"],
            version=case["version"], repo=case["repo"], module=module,
        )
        radar_symbols = [f.symbol for f in alert.findings]
        scores["radar"].add(radar_symbols, expected, case["id"],
                            alert.verdict.value, expected_verdict)

        keyword_symbols = keyword_baseline(notes, scan, case["package"])
        scores["keyword"].add(keyword_symbols, expected, case["id"],
                              _baseline_verdict(keyword_symbols), expected_verdict)

        llm_cell = "-"
        if not skip_llm_baseline:
            symbols, verdict, usage = llm_only_baseline(
                notes, scan, case["package"], module, model
            )
            scores["llm_only"].add(symbols, expected, case["id"],
                                   verdict or "ROUTINE", expected_verdict)
            scores["llm_only"].cost_usd += usage.cost_usd
            llm_cell = f"{len(symbols)}"

        print(f"{case['id'][:39]:<40}{expected_verdict[:8]:<10}"
              f"{alert.verdict.value[:8]:<10}"
              f"{_baseline_verdict(keyword_symbols)[:8]:<10}{llm_cell:<10}")

    return scores


def report(scores: Dict[str, Score], model: str) -> str:
    lines = [
        "",
        f"model: {model}",
        "",
        f"{'system':<38}{'prec':>7}{'recall':>8}{'F1':>7}{'verdict acc':>13}",
        "-" * 73,
    ]
    for score in scores.values():
        lines.append(
            f"{score.name:<38}{score.precision:>7.2f}{score.recall:>8.2f}"
            f"{score.f1:>7.2f}{score.verdict_accuracy:>12.0%}"
        )
    for score in scores.values():
        if score.mistakes:
            lines.append("")
            lines.append(f"{score.name} - every mistake:")
            lines.extend(f"    {m}" for m in score.mistakes)
    return "\n".join(lines)


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description="Score Radar against baselines.")
    parser.add_argument("--model", default=OPENROUTER_MODEL)
    parser.add_argument("--refresh", action="store_true",
                        help="re-run model calls instead of using cached extractions")
    parser.add_argument("--no-llm-baseline", action="store_true",
                        help="skip the LLM-only baseline (it costs money every run)")
    args = parser.parse_args(argv or sys.argv[1:])

    scores = run(args.model, args.refresh, args.no_llm_baseline)
    text = report(scores, args.model)
    print(text)

    RESULTS_PATH.write_text(json.dumps({
        "model": args.model,
        "systems": {
            key: {
                "name": s.name, "precision": round(s.precision, 4),
                "recall": round(s.recall, 4), "f1": round(s.f1, 4),
                "verdict_accuracy": round(s.verdict_accuracy, 4),
                "true_positives": s.true_positives,
                "false_positives": s.false_positives,
                "false_negatives": s.false_negatives,
                "mistakes": s.mistakes,
            } for key, s in scores.items()
        },
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
