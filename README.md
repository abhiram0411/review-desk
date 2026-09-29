# The Review Desk

A code review agent that remembers what your team accepts and rejects. It reads a pull request diff, places comments on the lines they refer to, and learns from every Accept and Reject so it stops repeating feedback your team has turned down.

Built on [Hindsight](https://github.com/vectorize-io/hindsight) agent memory, Groq (`openai/gpt-oss-120b`), FastAPI, the GitHub API and a single-file React frontend.

## How Hindsight memory is used

Memory is the core of the product, not a side feature.

1. **Retain.** Every Accept or Reject is written to a Hindsight memory bank as one plain-language sentence: the comment, its category, the outcome and the reason. See `feedback()` in `main.py`.
2. **Recall.** Before each review the agent recalls memories from the same bank and passes them to the reviewer as notes. See `review()` in `main.py`.
3. **Category rules.** Hindsight can store a rejection as a narrow fact ("rejected a docstring on the load function"), so a broader docs comment can slip through. The live app also keeps a decision log and turns it into category-level rules that go first in the prompt.

The reviewer only reads the first 12 notes, so the rules come first and recalled memories follow.

## Measured results

21 real `pallets/flask` pull requests were replayed in merge order, once with memory off and once with memory on, each with a fresh memory bank. The team was scripted: it accepts security, validation, error handling and bug comments and rejects everything else.

| | Memory off | Memory on |
|---|---|---|
| Comments generated | 96 | 47 |
| Repeated rejections | 62 | 0 |
| Accepted | 29 | 39 |
| Acceptance rate | 30% | 83% |

An earlier 10-PR run gave repeated rejections of 26 without memory and 0 with.

Limits: one run per arm, and model output varies. The team was simulated. Accepted counts moved in opposite directions between the two runs, so this does not show that memory raises accepted comments. The replay (`replay_real.py`) uses Hindsight recall only; the category rules in the live app were not part of the measured numbers. Review comments are model output and are not verified.

## Run locally

```powershell
pip install -r requirements.txt
copy .env.example .env   # then fill in your keys
python -m uvicorn main:app --port 8000
```

Open http://localhost:8000 for the UI or http://localhost:8000/docs for the API.

`.env` needs `HINDSIGHT_API_URL`, `HINDSIGHT_API_KEY`, `GROQ_API_KEY` and `GITHUB_TOKEN` (the last one is only used by `fetch_prs.py`).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/review` | Diff in, comments out, using recalled memory |
| POST | `/feedback` | Accept or reject one comment, retained in memory |
| GET | `/conventions` | Rules learned so far |
| GET | `/decisions` | Full decision history |
| GET | `/metrics` | Replay results |
| GET | `/demo-prs`, `/demo-prs/{n}` | Real Flask PRs |
| POST | `/github-pr` | Fetch any public GitHub pull request by link |
| GET | `/config` | Tells the UI whether the server is read only |

## Deployment settings

| Variable | Effect |
|---|---|
| `READ_ONLY=1` | Blocks `/review` and `/feedback`, for a public link |
| `PUBLIC_MODE=1` | Hides `/docs` and `/openapi.json` |
| `BANK_ID` | Memory bank name, so a deployed copy stays separate from local tests |
| `ALLOWED_ORIGINS` | Comma-separated origins for CORS (off by default) |

Inputs are size-limited, reasons are restricted to a fixed list, categories are sanitized before they reach a prompt, and `/review` and `/feedback` are rate limited per IP.

## Known limits

- Hindsight needs about 10 seconds to process a retained memory before recall reflects it.
- Comment ids live in server memory, so a restart invalidates old ids. Saved decisions survive.
- Inline comment placement is matched from the model's `file:line` text and can land a line or two off.
- Any public GitHub pull request can be reviewed by pasting its link (`/github-pr`). Only public repositories are supported. Use a fine-grained `GITHUB_TOKEN` with read access to public repositories only, so a deployed copy can never fetch private code.
- Comments are shown in the UI and are not posted back to GitHub. The next step is a GitHub App or Action that posts comments on the pull request and treats resolved and dismissed threads as accept and reject signals. That is not built.

## Layout

```
main.py            FastAPI backend
replay_real.py     review, retain and recall helpers, plus the measured replay
fetch_prs.py       downloads real pull requests from GitHub
frontend/          single-file React UI
data/prs/          the pull requests used
data/metrics.json  replay results shown on the Results and Replay pages
```

Flask pull request content belongs to the Flask project and is used here for evaluation.
