"""
main.py - FastAPI backend for the code review agent (hardened).

Run (from the fetch-prs folder, next to replay_real.py):
    python -m uvicorn main:app --port 8000
Then open http://localhost:8000/docs to try every endpoint.

Optional environment variables (all off by default, so local use is unchanged):
    READ_ONLY=1         block /review and /feedback (safe public demo of Results/Replay)
    PUBLIC_MODE=1       hide /docs, /redoc and /openapi.json
    ALLOWED_ORIGINS=... comma-separated origins to allow via CORS (only needed if the
                        frontend is hosted on a different origin than this server)

Endpoints:
    POST /review          diff in -> review comments out (uses recalled memory)
    POST /feedback        accept/reject one comment -> retained in memory
    GET  /conventions     what the agent has learned so far (for the panel)
    GET  /decisions       full decision history
    GET  /metrics         replay results (data/metrics.json) for the chart
    GET  /demo-prs        real Flask PRs to pick from in the UI
    GET  /demo-prs/{n}    one PR including its diff
If a ./frontend folder with index.html exists, it is served at /.
"""
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal, Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import replay_real as core  # reuses review(), retain_memory(), recall_memory()

BANK = os.getenv("BANK_ID", "review-agent-live")  # deployed copy should use its own bank
DATA = "data"
DECISIONS_FILE = os.path.join(DATA, "decisions.json")
SEED_MARK = os.path.join(DATA, ".bank_seeded")
METRICS_FILE = os.path.join(DATA, "metrics.json")
RECALL_QUERY = "comment types the team rejected or accepted"
SEED_TEXT = "Review memory initialized"

READ_ONLY = os.getenv("READ_ONLY") == "1"
PUBLIC_MODE = os.getenv("PUBLIC_MODE") == "1"
MAX_DIFF_CHARS = 200_000
MAX_COMMENT_CHARS = 500
MAX_COMMENTS_KEPT = 1000

# The Hindsight client runs its own event loop, so keep all memory calls on one thread.
_mem_pool = ThreadPoolExecutor(max_workers=1)


def on_mem(fn, *args):
    return _mem_pool.submit(fn, *args).result()


COMMENTS: dict[str, dict] = {}  # comment_id -> comment, so /feedback can look it up
DECISIONS: list[dict] = []
_PRS: list[dict] = []
_lock = threading.Lock()


def clean_cat(value) -> str:
    """Categories come from model output and end up inside prompts, so keep them to a safe alphabet."""
    cat = re.sub(r"[^a-z0-9_]", "", str(value or "").lower().replace(" ", "_"))[:30]
    return cat or "other"


def clean_text(value, limit=MAX_COMMENT_CHARS) -> str:
    """Flatten whitespace and cap length so model text cannot smuggle in long instructions."""
    return " ".join(str(value or "").split())[:limit]


# Simple per-IP sliding window limiter. Behind a proxy, request.client is the proxy's IP.
_hits: dict[tuple, list] = {}


def limit(request: Request, key: str, n: int, per: int = 60):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in _hits.get((key, ip), []) if now - t < per]
    if len(recent) >= n:
        raise HTTPException(429, "Too many requests. Wait a minute and try again.")
    recent.append(now)
    _hits[(key, ip)] = recent


def save_decisions():
    tmp = DECISIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DECISIONS, f, indent=2)
    os.replace(tmp, DECISIONS_FILE)  # atomic: a crash cannot leave a half-written file


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(DATA, exist_ok=True)
    if os.path.exists(DECISIONS_FILE):
        try:
            with open(DECISIONS_FILE, encoding="utf-8") as f:
                DECISIONS.extend(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            bad = DECISIONS_FILE + ".bad"
            os.replace(DECISIONS_FILE, bad)
            print(f"WARNING: could not read decisions.json ({e}). Moved it to {bad}, starting empty.")
    if not os.path.exists(SEED_MARK):
        print("First run: seeding memory bank...")
        on_mem(core.retain_memory, BANK,
               f"{SEED_TEXT}. Team accept/reject decisions on code review "
               "comments will be recorded in this bank.")
        time.sleep(core.SEED_WAIT)
        with open(SEED_MARK, "w") as f:
            f.write("1")
    _PRS.extend(core.load_prs())
    yield


app = FastAPI(
    title="Code Review Agent with Memory",
    lifespan=lifespan,
    docs_url=None if PUBLIC_MODE else "/docs",
    redoc_url=None if PUBLIC_MODE else "/redoc",
    openapi_url=None if PUBLIC_MODE else "/openapi.json",
)

# The bundled frontend is served from this same origin, so CORS is not needed by default.
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


class ReviewIn(BaseModel):
    diff: str = Field(max_length=MAX_DIFF_CHARS)
    title: str = Field(default="", max_length=300)
    pr_number: Optional[int] = None


class FeedbackIn(BaseModel):
    comment_id: str = Field(max_length=16)
    decision: Literal["accept", "reject"]
    # Only the reasons the UI offers are accepted, so free text never reaches memory.
    reason: Literal["", "Not a priority for us", "Against our conventions", "Wrong for this code"] = ""


def block_if_read_only():
    if READ_ONLY:
        raise HTTPException(403, "Live reviews are turned off on this deployment.")


@app.post("/review")
def review(body: ReviewIn, request: Request):
    block_if_read_only()
    limit(request, "review", 20)

    notes = on_mem(core.recall_memory, BANK, RECALL_QUERY)
    notes = [n for n in notes if not n.startswith(SEED_TEXT)]

    # Rules built from the decision log, so broad categories are enforced even
    # when Hindsight stored a rejection as a narrow fact.
    with _lock:
        snapshot = list(DECISIONS)
    acc = {clean_cat(d["category"]) for d in snapshot if d["decision"] == "accept"}
    rej = {clean_cat(d["category"]) for d in snapshot if d["decision"] == "reject"}
    rules = []
    if rej - acc:
        rules.append("Team rules: the team rejects these categories, so write NO comments in them: "
                     + ", ".join(f"'{c}'" for c in sorted(rej - acc)) + ".")
    if acc - rej:
        rules.append("Team standard: always check every diff for "
                     + ", ".join(f"'{c}'" for c in sorted(acc - rej))
                     + " issues, because the team accepted these before.")
    notes = rules + notes  # rules first: the reviewer only reads the first 12 notes

    pr = {"number": body.pr_number or 0, "title": body.title, "diff": body.diff}
    comments = core.review(pr, notes)
    out = []
    for c in comments:
        rec = {**c,
               "category": clean_cat(c.get("category")),
               "comment": clean_text(c.get("comment")),
               "id": uuid.uuid4().hex[:8],
               "pr_title": clean_text(body.title, 300)}
        out.append(rec)
    with _lock:
        for rec in out:
            COMMENTS[rec["id"]] = rec
        while len(COMMENTS) > MAX_COMMENTS_KEPT:  # drop the oldest so memory use stays bounded
            COMMENTS.pop(next(iter(COMMENTS)))
    return {"comments": out, "memory_used": notes[:12]}


@app.post("/feedback")
def feedback(body: FeedbackIn, request: Request):
    block_if_read_only()
    limit(request, "feedback", 60)

    with _lock:
        c = COMMENTS.get(body.comment_id)
        if not c:
            raise HTTPException(404, "Unknown comment_id (server restarted? run /review again)")
        if c.get("decided"):
            raise HTTPException(409, "This comment already has a decision.")
        c["decided"] = True

    cat = c["category"]
    if body.decision == "accept":
        text = (f"In a review, the reviewer suggested: \"{c['comment']}\". "
                f"This suggestion was ACCEPTED. The team values '{cat}' feedback like this.")
    else:
        why = body.reason or f"the team does not want '{cat}' comments"
        text = (f"In a review, the reviewer suggested: \"{c['comment']}\". "
                f"This suggestion was REJECTED. Reason: {why}. "
                f"Do not suggest '{cat}' feedback again on similar code.")
    try:
        on_mem(core.retain_memory, BANK, text)
    except Exception:
        with _lock:
            c["decided"] = False  # let the person try again
        raise HTTPException(502, "The memory service did not respond. Try again.")

    with _lock:
        DECISIONS.append({"comment_id": c["id"], "category": cat, "decision": body.decision,
                          "comment": c["comment"], "reason": body.reason})
        save_decisions()
    return {"ok": True, "category": cat,
            "note": "Memory takes ~10 seconds to process before it affects new reviews."}


GH_PR_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:/(?:files|commits))?/?(?:[?#].*)?$")


class GithubIn(BaseModel):
    url: str = Field(max_length=200)


@app.get("/config")
def config():
    return {"read_only": READ_ONLY}


@app.post("/github-pr")
def github_pr(body: GithubIn, request: Request):
    """Fetch a public GitHub pull request by link so any PR can be reviewed, not just the saved ones."""
    block_if_read_only()
    limit(request, "github", 20)
    m = GH_PR_RE.match(body.url.strip())
    if not m:
        raise HTTPException(400, "Paste a link like https://github.com/owner/repo/pull/123")
    owner, repo, num = m.groups()
    if set(owner) <= {"."} or set(repo) <= {"."}:  # block "." and ".." path tricks
        raise HTTPException(400, "Paste a link like https://github.com/owner/repo/pull/123")
    # Host and path shape are fixed above, so this can only ever call api.github.com/repos/.../pulls/N.
    base = f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}"
    headers = {"User-Agent": "review-desk"}
    token = os.getenv("GITHUB_TOKEN")  # use a fine-grained token with public-repo read access only
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        meta = requests.get(base, headers={**headers, "Accept": "application/vnd.github+json"}, timeout=15)
        if meta.status_code in (403, 404):
            raise HTTPException(404, "Pull request not found, or the repository is private.")
        meta.raise_for_status()
        diff = requests.get(base, headers={**headers, "Accept": "application/vnd.github.v3.diff"}, timeout=20)
        diff.raise_for_status()
    except HTTPException:
        raise
    except requests.RequestException:
        raise HTTPException(502, "GitHub did not respond. Try again in a moment.")
    if len(diff.text) > MAX_DIFF_CHARS:
        raise HTTPException(413, "This pull request is too large to review here.")
    return {"number": int(num), "title": clean_text(meta.json().get("title", ""), 300),
            "diff": diff.text, "repo": f"{owner}/{repo}"}


@app.get("/conventions")
def conventions():
    with _lock:
        snapshot = list(DECISIONS)
    stats: dict[str, dict] = {}
    for d in snapshot:
        s = stats.setdefault(d["category"], {"accepted": 0, "rejected": 0})
        s["accepted" if d["decision"] == "accept" else "rejected"] += 1
    learned = []
    for cat, s in stats.items():
        label = cat.replace("_", " ")
        if s["rejected"] > s["accepted"]:
            status, rule = "avoid", f"Stop flagging {label} comments"
        elif s["accepted"] > s["rejected"]:
            status, rule = "keep", f"Keep flagging {label} issues"
        else:
            status, rule = "mixed", f"Mixed feedback on {label} comments"
        learned.append({"category": cat, "status": status, "rule": rule, **s})
    learned.sort(key=lambda x: (x["status"] != "avoid", -(x["accepted"] + x["rejected"])))
    return {"learned": learned, "total_decisions": len(snapshot)}


@app.get("/decisions")
def decisions():
    with _lock:
        return list(reversed(DECISIONS))


@app.get("/metrics")
def metrics():
    if not os.path.exists(METRICS_FILE):
        raise HTTPException(404, "No metrics yet. Run replay_real.py first.")
    with open(METRICS_FILE, encoding="utf-8") as f:
        return json.load(f)


@app.get("/demo-prs")
def demo_prs():
    return [{"number": p["number"], "title": p["title"], "diff_lines": len(p["diff"].splitlines())}
            for p in _PRS]


@app.get("/demo-prs/{number}")
def demo_pr(number: int):
    for p in _PRS:
        if p["number"] == number:
            return p
    raise HTTPException(404, "PR not found")


# Mount the frontend last so it doesn't shadow the API routes.
if os.path.isdir("frontend"):
    app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")