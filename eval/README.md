# Evaluation

Does the LLM-extract + AST-match split actually beat the two obvious
alternatives? This measures it on real releases and real repositories.

```bash
python -m eval.run_eval                    # score from cached extractions
python -m eval.run_eval --no-llm-baseline  # free: no model calls at all
python -m eval.run_eval --refresh          # re-run every model call
```

## Results

18 cases, 9 labelled breaking symbols, `google/gemini-2.5-flash-lite`.

| System | Precision | Recall | F1 | Verdict accuracy | FP |
|---|---|---|---|---|---|
| **Radar** (LLM extracts, code decides) | **1.00** | **1.00** | **1.00** | **100%** | **0** |
| LLM-only (model decides) | 0.07 | 1.00 | 0.13 | 56% | 118 |
| Keyword (no model) | 0.05 | 0.33 | 0.09 | 39% | 58 |

**Read the two baselines as one sentence each.**

The **LLM-only** baseline gets the same release notes and the same list of
the repo's imports, and is asked directly what breaks. It finds every real
break — recall 1.00, the same as Radar — and then flags 118 things that are
fine. On `pallets/flask` it called 76 imports breaking. It is not bad at
reading; it is bad at *stopping*. Asked "what might break", a model finds
something wrong with nearly everything you show it.

That is the whole argument for the split: **the model's recall is worth
having, its judgement is not.** Radar keeps the extraction and replaces the
decision with a comparison that can be checked against a line of code —
same recall, precision from 0.07 to 1.00.

The **keyword** baseline fails the other way. It flags `flask.Markup`
because the removal-shaped line mentioning it says *deprecated*, not
removed; it flags `_request_ctx_stack` although the notes say `top` still
exists. And it misses every `jinja2` break, because those notes name the
removed functions in an indented sub-list its line matching never
associates with a removal. Precision 0.05 *and* recall 0.33 — worse at both
jobs, not a precision/recall trade.

## What the score does and does not mean

**A perfect score on 18 cases means the set is too small to discriminate
further — not that the system is perfect.** The honest next step is to grow
the golden set until it produces failures again, especially with
harder negatives. Treat 1.00 as "no known failures in this set", not as an
accuracy claim.

Three more limits worth stating plainly:

- **The task is scoped to what the notes say.** Every system is scored on
  finding the APIs *the release notes name* as removed. Recall 1.00 does
  not mean "no upgrade will ever surprise you" — it means nothing the notes
  named was missed. `werkzeug 3.0.0` says only "Remove previously
  deprecated code" and names nothing; no system can do anything with that,
  and the case is labelled `ROUTINE` accordingly.
- **The set is ecosystem-skewed** — mostly Flask/Jinja2, plus urllib3. A
  broader set (numpy, pandas, sqlalchemy, django) would test the matcher on
  different naming conventions.
- **Cost.** Radar is one extraction per release, cached by package+version,
  ~$0.0002. The LLM-only baseline costs about the same per case and is
  useless, which is the point.

## The golden set

`golden/cases.json` — 18 `(repo, ref, package, version)` cases, 6 breaking
and 12 routine, each with the exact symbols expected and a note on why.
Inputs are frozen in `golden/`: release notes, repo scans, and the raw
source text. An eval that re-fetches the internet measures the internet.

Repos are pinned to old tags where that is the real situation —
`flask-admin@v1.5.7` against `jinja2 3.1.0` is a 2019 codebase meeting a
2022 release, which is exactly who this tool is for.

### How the labels were made

1. Read the changelog section for the release and list the APIs it states
   were removed or renamed. Deprecations are **not** removals.
2. For each repo, find which of those names it imports.
3. **Audit against the raw source text**, not only against Radar's own
   scan. Labelling from what the scanner reports would make the scanner
   perfect by construction; the raw text is kept in `golden/sources/`
   precisely so the labels can be checked for symbols the scanner never
   claimed.

Step 3 earned its keep immediately. Grepping the negatives for `Markup` and
`escape` turns up dozens of hits — all `from markupsafe import Markup` or
`from sphinx.util.rst import escape`. Same words, different packages. The
AST scan is right to ignore them and a keyword scan is not, which is the
difference showing up as 58 false positives.

### The eval found a labelling error

The first run showed Radar with one false positive:
`urllib3.exceptions.SNIMissingWarning` in `psf/requests`. It was not a false
positive. urllib3's notes do say *"Removed
`urllib3.exceptions.SNIMissingWarning`"*, and requests imports it at
`tests/__init__.py:6`. The label was wrong because only the first part of
urllib3's 14,000-character notes had been read when labelling.

The label was corrected and the system left alone. Recorded here because an
eval whose disagreements are always resolved in the system's favour is not
an eval — and because the near-misses in the same notes are instructive:
`pyopenssl` (only SSLv3 support dropped), `HTTPConnectionPool` (only
`_prepare_conn` removed) and `Retry` (only `method_whitelist` removed) all
appear on removal lines, are all imported by requests, and are all
correctly *not* findings.

## Files

| File | What it is |
|---|---|
| `golden/cases.json` | The labelled set |
| `golden/notes/` | Frozen release notes |
| `golden/scans/` | Frozen AST scans of each repo at each ref |
| `golden/sources/` | Raw source text, for auditing the labels |
| `golden/extractions/` | Cached stage-2 output, so scoring is free and repeatable |
| `baselines.py` | The keyword and LLM-only baselines |
| `run_eval.py` | Scoring |
| `results.json` | Last run, including every individual mistake |
