"""The hosted demo: a GitHub repo URL in, an alert with file:line out.

Same five stages as the CLI, with three additions a public endpoint needs:
the repo is fetched instead of read from disk (`radar/repo.py`), extractions
are cached by package+version so the same demo does not pay twice, and there
is a crude per-IP rate limit so a stranger cannot spend the API budget.
"""

from __future__ import annotations

import html
import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from radar.cli import load_dotenv
from radar.extract import (
    OPENROUTER_MODEL,
    ExtractedChanges,
    ExtractionError,
    OpenRouterExtractor,
)
from radar.fetch import FetchError, fetch_release_notes
from radar.match import build_alert
from radar.models import Alert, Verdict
from radar.repo import RepoError, fetch_repo
from radar.scan import scan_repo

load_dotenv()

app = FastAPI(title="Release Radar", docs_url=None, redoc_url=None)

# One extraction per (package, version) for the life of the process. Release
# notes for a published version never change, so this is safe - and it makes
# the common demo path free.
_EXTRACTION_CACHE: Dict[Tuple[str, str], Tuple[ExtractedChanges, Optional[str]]] = {}

RATE_LIMIT = 20  # checks
RATE_WINDOW = 3600  # seconds
_HITS: Dict[str, Deque[float]] = defaultdict(deque)

# One that breaks and one that does not - the second matters as much as the
# first, because it shows the engine does not cry wolf.
EXAMPLES = [
    ("mattupstate/flask-security", "flask", "2.3.0"),
    ("pallets/flask", "werkzeug", "3.0.0"),
]


def _rate_limited(client: str) -> bool:
    now = time.time()
    hits = _HITS[client]
    while hits and now - hits[0] > RATE_WINDOW:
        hits.popleft()
    if len(hits) >= RATE_LIMIT:
        return True
    hits.append(now)
    return False


def run_check(repo_value: str, package: str, version: str) -> Alert:
    """The whole pipeline, for one request."""
    repo = fetch_repo(repo_value)
    try:
        scan = scan_repo(repo.path)
        key = (package.strip().lower(), version.strip())
        if key in _EXTRACTION_CACHE:
            changes, notes_url = _EXTRACTION_CACHE[key]
        else:
            notes = fetch_release_notes(package.strip(), version.strip())
            changes = OpenRouterExtractor(model=OPENROUTER_MODEL).extract(notes)
            notes_url = notes.url
            _EXTRACTION_CACHE[key] = (changes, notes_url)
        return build_alert(
            changes=changes,
            scan=scan,
            package=package.strip(),
            version=version.strip(),
            repo=repo.slug,
            notes_url=notes_url,
        )
    finally:
        repo.cleanup()  # the checkout is never kept


# -- rendering ---------------------------------------------------------------

STYLE = """
:root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --line:#30363d;
        --text:#e6edf3; --muted:#8b949e; --red:#f85149; --amber:#d29922;
        --green:#3fb950; --link:#58a6ff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.6
       ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 780px; margin: 0 auto; padding: 48px 20px 80px; }
h1 { font-size: 26px; margin: 0 0 6px; letter-spacing:-.02em; }
.sub { color: var(--muted); margin: 0 0 32px; }
form { background: var(--panel); border:1px solid var(--line); border-radius:10px;
       padding: 20px; display:grid; gap:12px; }
label { font-size:13px; color:var(--muted); display:block; margin-bottom:4px; }
input { width:100%; padding:9px 11px; background:#0d1117; color:var(--text);
        border:1px solid var(--line); border-radius:6px; font-size:14px;
        font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
input:focus { outline:none; border-color:var(--link); }
.row { display:grid; grid-template-columns: 1fr 140px; gap:12px; }
button { padding:10px 16px; background:#238636; color:#fff; border:0;
         border-radius:6px; font-size:14px; font-weight:600; cursor:pointer; }
button:hover { background:#2ea043; }
.examples { color:var(--muted); font-size:13px; margin-top:14px; }
.examples a { color:var(--link); text-decoration:none; }
.card { margin-top:28px; background:var(--panel); border:1px solid var(--line);
        border-left-width:4px; border-radius:10px; padding:20px; }
.BREAKING { border-left-color: var(--red); }
.SECURITY { border-left-color: var(--amber); }
.ROUTINE  { border-left-color: var(--green); }
.verdict { font-size:20px; font-weight:700; letter-spacing:-.01em; }
.verdict .BREAKING { color: var(--red); }
.verdict .SECURITY { color: var(--amber); }
.verdict .ROUTINE  { color: var(--green); }
.why { color:var(--muted); margin:6px 0 0; font-size:14px; }
.finding { margin-top:18px; padding-top:14px; border-top:1px solid var(--line); }
.detail { font-size:14px; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
              font-size:13px; }
.site { color:var(--muted); font-size:13px; margin-top:4px; }
.site a { color: var(--link); text-decoration:none; }
.site a:hover { text-decoration:underline; }
.err { border-left-color: var(--red); color: var(--red); }
.foot { margin-top:40px; color:var(--muted); font-size:13px; line-height:1.8; }
.foot a { color: var(--link); text-decoration:none; }
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Release Radar</title><style>{style}</style></head><body><div class="wrap">
<h1>Release Radar</h1>
<p class="sub">Does this dependency release break <em>your</em> repo? An LLM reads
the release notes; a static <code>ast</code> scan reads your imports; plain code
decides &mdash; so every alert points at a line.</p>
<form method="post" action="/check">
  <div>
    <label for="repo">GitHub repository</label>
    <input id="repo" name="repo" value="{repo}" placeholder="owner/name" required>
  </div>
  <div class="row">
    <div>
      <label for="package">PyPI package</label>
      <input id="package" name="package" value="{package}" placeholder="pydantic" required>
    </div>
    <div>
      <label for="version">Version</label>
      <input id="version" name="version" value="{version}" placeholder="2.0" required>
    </div>
  </div>
  <button type="submit">Check this upgrade</button>
</form>
<p class="examples">Try: {examples}</p>
{result}
<p class="foot">
  The model never decides severity &mdash; it only extracts what the notes say
  changed. Whether that matters to a repo is a deterministic comparison against
  real import sites.<br>
  <a href="/api/check?repo=mattupstate/flask-security&amp;package=flask&amp;version=2.3.0">JSON API</a>
  &middot; <a href="https://github.com/">source</a>
</p>
</div></body></html>
"""


def _example_links() -> str:
    parts = []
    for repo, package, version in EXAMPLES:
        parts.append(
            f'<a href="/?repo={repo}&package={package}&version={version}">'
            f"{html.escape(repo)} vs {html.escape(package)} {html.escape(version)}</a>"
        )
    return " &middot; ".join(parts)


def _github_link(repo_slug: str, file: str, line: int) -> str:
    return f"https://github.com/{repo_slug}/blob/HEAD/{file}#L{line}"


def render_alert(alert: Alert) -> str:
    verdict = alert.verdict.value
    out = [
        f'<div class="card {verdict}">',
        f'<div class="verdict"><span class="{verdict}">{verdict}</span> '
        f"&middot; <span class='mono'>{html.escape(alert.package)} "
        f"{html.escape(alert.version)}</span></div>",
        f'<p class="why">{html.escape(alert.reason)}</p>',
    ]
    if alert.security_note:
        out.append(f'<p class="why">security: {html.escape(alert.security_note)}</p>')
    for finding in alert.findings:
        out.append('<div class="finding">')
        out.append(f'<div class="detail">{html.escape(finding.detail)}</div>')
        for site in finding.evidence:
            url = _github_link(alert.repo, site.file, site.line)
            out.append(
                f'<div class="site">used at <a href="{html.escape(url)}" '
                f'target="_blank" rel="noopener" class="mono">'
                f"{html.escape(site.location())}</a> "
                f"({html.escape(site.kind)} of <code>"
                f"{html.escape(site.local_name)}</code>)</div>"
            )
        out.append("</div>")
    out.append("</div>")
    return "\n".join(out)


def render_page(
    repo: str = "", package: str = "", version: str = "", result: str = ""
) -> str:
    return PAGE.format(
        style=STYLE,
        repo=html.escape(repo, quote=True),
        package=html.escape(package, quote=True),
        version=html.escape(version, quote=True),
        examples=_example_links(),
        result=result,
    )


def _error_card(message: str) -> str:
    return f'<div class="card err">{html.escape(message)}</div>'


# -- routes ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(repo: str = "", package: str = "", version: str = "") -> HTMLResponse:
    return HTMLResponse(render_page(repo, package, version))


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "model": OPENROUTER_MODEL})


@app.post("/check", response_class=HTMLResponse)
def check(
    request: Request,
    repo: str = Form(...),
    package: str = Form(...),
    version: str = Form(...),
) -> HTMLResponse:
    client = request.client.host if request.client else "unknown"
    if _rate_limited(client):
        return HTMLResponse(
            render_page(repo, package, version,
                        _error_card("Rate limit reached. Try again later.")),
            status_code=429,
        )
    try:
        alert = run_check(repo, package, version)
    except (RepoError, FetchError, ExtractionError) as exc:
        return HTMLResponse(
            render_page(repo, package, version, _error_card(str(exc))), status_code=400
        )
    return HTMLResponse(render_page(repo, package, version, render_alert(alert)))


@app.get("/api/check")
def api_check(request: Request, repo: str, package: str, version: str) -> JSONResponse:
    client = request.client.host if request.client else "unknown"
    if _rate_limited(client):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    try:
        alert = run_check(repo, package, version)
    except (RepoError, FetchError, ExtractionError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(alert.model_dump(mode="json"))


if __name__ == "__main__":  # local run: python web/app.py
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
