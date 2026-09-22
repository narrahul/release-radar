# Release Radar

Tells a Python repository which dependency releases actually break **its** code.

An LLM reads the release notes and extracts the APIs that were removed or
renamed. A static `ast` scan finds the names the repo really imports. Plain
code compares the two — so every alert points at a line you can open:

```
!! pydantic 2.0 -> BREAKING
   repo: /repos/myapp
   why:  1 API(s) this repo uses changed; first at app/config.py:4

   [removed] pydantic.BaseSettings was removed in this release (moved to the
             separate pydantic-settings package)
       used at app/config.py:4   (import of `BaseSettings`)
       used at app/config.py:7   (reference of `BaseSettings`)
       used at app/aliased.py:5  (reference of `pd`)
```

On a golden set of 18 real releases: **precision 1.00, recall 1.00** —
against 0.07 precision for asking an LLM directly, and 0.05 for keyword
matching. [Full evaluation](eval/README.md).

**The model reads prose. The code decides.** The LLM is never asked whether a
release is breaking — it only reports what the notes say changed. Whether that
matters to a given repo is a deterministic comparison against real import
sites, which is why every alert carries `file:line` evidence instead of an
opinion.

## The five stages

| Stage | Module | What it does | LLM? |
|---|---|---|---|
| 1. fetch | `radar/fetch.py` | Release notes for `package==version`, from the GitHub release if the PyPI metadata points at one, else the PyPI description | no |
| 2. extract | `radar/extract.py` | Notes → `{removed, renamed, behaviour_changed, security}`, schema enforced by the API (OpenRouter or Anthropic) | **yes** |
| 3. scan | `radar/scan.py` | Every name the repo imports, with file and line, via `ast` | no |
| 4. match | `radar/match.py` | Which extracted changes touch names the repo actually uses → verdict | no |
| 5. report | `radar/report.py` | The alert: one line, a block, or JSON | no |

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env     # then put your key in it
```

`.env` is read at startup (real environment variables win). Stage 2 runs
through **OpenRouter** whenever `OPENROUTER_API_KEY` is set - default model
`google/gemini-2.5-flash-lite`, called over its OpenAI-compatible endpoint with
a strict JSON schema, so no second SDK dependency. `--provider anthropic` uses
the Anthropic SDK directly instead, and `--model` overrides either.
`GITHUB_TOKEN` is optional and only raises the release-notes rate limit.

Run it against a repo:

```bash
python -m radar check --repo ./myapp --package pydantic --version 2.0
```

Run the bundled offline example — no API key, no network:

```bash
python -m radar check \
  --repo tests/fixtures/sample_repo \
  --package pydantic --version 2.0 \
  --notes-file    tests/fixtures/notes/pydantic-2.0.md \
  --changes-file  tests/fixtures/changes/pydantic-2.0.json
```

### Other commands

```bash
# Stage 3 alone: what does this repo import from pydantic, and where?
python -m radar scan --repo ./myapp --package pydantic

# Stage 2 alone: what did the model get out of the notes?
python -m radar extract --package pydantic --version 2.0 --show-cost
```

`--json` gives machine-readable output, `--oneline` gives a feed line.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | `ROUTINE` or `SECURITY` — nothing in this release touches the repo's imports |
| `1` | `BREAKING` — at least one finding, with evidence |
| `2` | The run failed (no notes, extraction error, bad repo path) |

So CI can gate on it: `python -m radar check ... || echo "review this upgrade"`.

## Design decisions worth defending

**Why an AST scan and not a regex.** `grep BaseSettings` matches a comment, a
docstring, a string constant, and `my_own.BaseSettings` — and misses
`import pydantic as pd` followed by `pd.BaseSettings`. The AST resolves alias
bindings and knows a `Load` from a `Store`, and every node carries the line
number that ends up in the alert. `tests/fixtures/sample_repo/app/decoy.py`
exists to make that difference a test: it mentions `BaseSettings` in prose and
defines a class with that name, and the scanner correctly reports nothing.

**Why the LLM does not decide.** Severity from a model is unfalsifiable. Split
the job — the model does the part that needs reading comprehension (prose →
names), code does the part that needs to be right (names → your repo) — and
every alert becomes checkable against a line of source.

**Tuned for precision.** A removed symbol counts only when the repo uses that
exact name *or something underneath it*. Importing `pydantic` is not evidence
that `pydantic.BaseSettings` was used. `from pkg import *` cannot be resolved
by the AST, so it is reported as a caveat on the alert, never as a finding.
Deprecations that do not remove anything in that release are not findings, and
a name the model returns that is not a valid dotted path is discarded rather
than guessed at. A noisy "breaking" alert is worse than no alert — people stop
reading them.

**Verdict precedence.** `BREAKING` (a change lands on a name the repo uses)
outranks `SECURITY` (the release fixes a vulnerability), which outranks
`ROUTINE`. A breaking release that is also a security fix reports `BREAKING`
and keeps the security note.

## Does it actually work?

18 hand-labelled cases, real repos pinned to real tags, real releases
([methodology and every mistake](eval/README.md)):

| System | Precision | Recall | F1 | Verdict accuracy |
|---|---|---|---|---|
| **Radar** (LLM extracts, code decides) | **1.00** | **1.00** | **1.00** | **100%** |
| LLM-only (model decides) | 0.07 | 1.00 | 0.13 | 56% |
| Keyword (no model) | 0.05 | 0.33 | 0.09 | 39% |

The LLM-only baseline is the interesting one. Given the same notes and the
same import list, it finds *every* real break — recall 1.00, identical to
Radar — and also flags 118 things that are fine, 76 of them in a single
repo. Its recall is worth having; its judgement is not. Replacing the
decision with a deterministic match keeps the recall and takes precision
from 0.07 to 1.00.

A perfect score on 18 cases means the set is too small to discriminate
further, not that the system is perfect — the next step is growing it until
it fails again.

```bash
python -m eval.run_eval --no-llm-baseline   # free, no model calls
```

## Known limits

- **Scope model.** Alias bindings are tracked per file, not per function
  scope. A local variable shadowing an imported name can add a spurious
  *reference* site; import sites — what an alert leads with — are exact.
- **Notes quality is the ceiling.** Many GitHub releases are an auto-generated
  "What's Changed" PR list, or a two-line summary pointing elsewhere - neither
  names an API. Stage 1 detects both and falls back to the project's own
  `CHANGES.rst`/`CHANGELOG.md`, slicing out the section for that version. When
  even that is a stub, extraction returns nothing and the verdict is `ROUTINE`;
  `--notes-file` is the manual override.
- **Distribution vs import name.** `PyYAML` installs `yaml`. Common cases are
  mapped in `radar/match.py`; anything else falls back to
  `name.lower().replace("-", "_")` and can be overridden with `--module`.
- **Dynamic imports.** `importlib.import_module("pydantic")` and
  `__import__` are invisible to the scan, by design — there is no line to cite.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

106 tests, no network and no API key: the Anthropic client is faked in
`tests/test_extract.py`, and the end-to-end CLI test runs off a saved
extraction. The sample repo in `tests/fixtures/sample_repo` covers aliased
imports, submodule imports, star imports, a file with a syntax error, a
vendored `.venv` that must be skipped, and the decoy file above. `test_repo.py`
covers the archive handling, including a zip-slip entry that must never be
written; `test_changelog.py` pins which prose wins when a release body is a
generated PR list.

## The hosted demo

`web/app.py` is the same engine behind a one-page UI: paste a GitHub repo,
a package and a version, get the alert with every `file:line` linked straight
to the line on GitHub.

```bash
pip install -r requirements.txt
uvicorn web.app:app --reload        # http://127.0.0.1:8000
```

`GET /api/check?repo=owner/name&package=flask&version=2.3.0` returns the same
alert as JSON. `GET /healthz` is the health check.

Three things the hosted version needs that the CLI does not, all in
`radar/repo.py` and `web/app.py`:

- **It fetches the repo.** Streams the GitHub zipball with a hard 80 MB cap,
  unpacks only `.py` files, refuses any archive entry whose path escapes the
  destination (zip-slip), and deletes the checkout when the request ends.
- **It caches extractions** by package+version. Published release notes never
  change, so the same demo never pays for the model twice.
- **It rate-limits** per IP (20 checks/hour) so a stranger cannot spend the
  API budget.

### Deploy (Render)

`render.yaml` is a blueprint - point Render at the repo and it reads it.

1. Push this repo to GitHub.
2. Render → **New** → **Blueprint** → pick the repo.
3. Set `OPENROUTER_API_KEY` in the dashboard (it is `sync: false`, so it is
   never in git). Optionally set `GITHUB_TOKEN` to raise the API rate limit.
4. Deploy. Health check is `/healthz`.

The free plan sleeps after 15 minutes idle and takes ~30s to wake - fine for a
portfolio link, worth warming before you show it to anyone.

## Not built yet

Deliberately out of scope for the detection engine:

- **Reliability.** Durable ingest worker, exponential backoff with jitter,
  dead-letter queue for unparseable releases, idempotency on release ID.
  `fetch.py` is one request with one timeout, on purpose.

## Cost

Extraction is one request per release. `--show-cost` prints the token count
and dollar cost - on OpenRouter that is what it actually billed
(`usage.include`), not an estimate.

The default is a small model on purpose. On the pydantic 2.0 notes
(1,899 in / 585 out tokens), every cheap model tested found `BaseSettings`:

| Model | Result | Speed | Cost |
|---|---|---|---|
| `google/gemini-2.5-flash-lite` (default) | correct | 1.8s | $0.0004 |
| `mistralai/mistral-small-3.2-24b-instruct` | correct | 3.8s | $0.0003 |
| `meta-llama/llama-4-scout` | correct | 4.0s | $0.0004 |
| `anthropic/claude-haiku-4.5` | correct | 6.9s | $0.0048 |
| `deepseek/deepseek-v4-flash` | correct | 24.5s | $0.0002 |
| `openai/gpt-5-nano` | invented an extra removal | 23.6s | $0.0003 |
| `anthropic/claude-opus-5` | correct | 11s | $0.0241 |

Opus 5 cost 57x the default for the same answer. This is a one-release smoke
test, not an eval - the golden set is what would turn it into evidence.
