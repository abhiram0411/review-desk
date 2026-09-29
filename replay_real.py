"""
replay_real.py - replay real PRs through the review agent, memory ON vs OFF.

Usage:
    python replay_real.py            # runs both arms, writes data/metrics.json
    python replay_real.py --limit 10 # only first 10 PRs (quick test)

Assumes each data/prs/*.json has: number, title, diff, and (ideally) merged_at.
Missing keys are handled with fallbacks; adjust load_prs() if your schema differs.
"""
import argparse
import glob
import json
import os
import re
import time
from datetime import datetime

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

MODEL = "openai/gpt-oss-120b"
PR_DIR = "data/prs"
OUT_FILE = "data/metrics.json"

CATEGORIES = [
    "security", "validation", "error_handling", "bug",
    "style_naming", "debug_print", "docs", "performance", "typing", "other",
]
# Scripted "team persona": these categories get accepted, the rest rejected.
ACCEPT = {"security", "validation", "error_handling", "bug"}

llm = Groq(api_key=os.environ["GROQ_API_KEY"])


# ---------------------------------------------------------------------------
# MEMORY ADAPTER - replace the bodies with the exact retain/recall calls that
# already work in your test_memory_loop.py. Everything else stays the same.
# ---------------------------------------------------------------------------
from hindsight_client import Hindsight  # noqa: E402

_mem = Hindsight(
    base_url=os.environ["HINDSIGHT_API_URL"],
    api_key=os.environ["HINDSIGHT_API_KEY"],
)
RETAIN_WAIT = 10  # seconds; Hindsight processes retains asynchronously
SEED_WAIT = 12


def retain_memory(bank_id: str, text: str) -> None:
    _mem.retain(bank_id=bank_id, content=text, context="code-review-feedback")


def recall_memory(bank_id: str, query: str) -> list[str]:
    for attempt in range(6):
        try:
            res = _mem.recall(bank_id=bank_id, query=query)
            return [r.text for r in (res.results or [])]
        except Exception as e:
            msg = str(e).lower()
            # Bank/tables not created yet, or transient server error: wait and retry.
            transient = ("not found" in msg or "404" in msg or "does not exist" in msg
                         or "undefinedtable" in msg or "500" in msg or "503" in msg)
            if not transient:
                raise
            time.sleep(8)
    print("    (recall unavailable after retries; continuing with no memory for this PR)")
    return []
# ---------------------------------------------------------------------------


def load_prs(limit=None):
    prs = []
    for path in glob.glob(os.path.join(PR_DIR, "*.json")):
        if os.path.basename(path).lower() == "index.json":
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else [data]
        for d in items:
            if not isinstance(d, dict):
                continue
            diff = d.get("diff") or d.get("patch") or d.get("diff_text") or ""
            if not diff:
                print(f"  warning: no diff found in {os.path.basename(path)}; keys = {list(d.keys())}")
                continue
            prs.append({
                "number": d.get("number") or d.get("pr_number"),
                "title": d.get("title", ""),
                "diff": diff,
                "merged_at": d.get("merged_at") or d.get("mergedAt") or "",
            })
    prs.sort(key=lambda p: (p["merged_at"], p["number"] or 0))
    return prs[:limit] if limit else prs


SYSTEM = f"""You are a strict but useful code reviewer. Review the PR diff.
Return ONLY a JSON array (no prose, no markdown fences). Each item:
{{"line": "<file:line or hunk hint>", "category": one of {CATEGORIES}, "comment": "<one sentence>"}}
Report at most 6 comments. Include every issue you would normally raise,
including style, naming, docs and typing nitpicks, unless the team memory
below says the team rejects that kind of comment. Return [] if nothing applies."""


def review(pr, memory_notes):
    mem_block = ""
    if memory_notes:
        mem_block = (
            "\n\nTEAM MEMORY (past accept/reject decisions). Do NOT repeat "
            "comment types the team rejected:\n- " + "\n- ".join(memory_notes[:12])
        )
    user = f"PR #{pr['number']}: {pr['title']}\n\n{pr['diff'][:12000]}{mem_block}"
    for attempt in range(3):
        try:
            r = llm.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": user}],
                temperature=0.2,
            )
            return parse_comments(r.choices[0].message.content)
        except Exception as e:  # rate limit / transient
            print(f"    retry {attempt + 1} after error: {e}")
            time.sleep(5 * (attempt + 1))
    return []


def parse_comments(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for c in items:
        if isinstance(c, dict) and c.get("comment"):
            cat = c.get("category", "other")
            out.append({
                "line": c.get("line", ""),
                "category": cat if cat in CATEGORIES else "other",
                "comment": c["comment"],
            })
    return out


def run_arm(prs, use_memory):
    label = "memory_on" if use_memory else "memory_off"
    bank = f"replay-{datetime.now():%Y%m%d-%H%M%S}-{label}"
    rejected_before = set()  # categories rejected in earlier PRs (for metrics)
    rows = []
    print(f"\n=== {label} (bank {bank}) ===")

    if use_memory:
        # Seed the bank with one retain so its tables exist before the first recall.
        retain_memory(bank, "Review memory initialized. Team accept/reject decisions on "
                            "code review comments will be recorded in this bank.")
        print(f"  seeding bank, waiting {SEED_WAIT}s...")
        time.sleep(SEED_WAIT)

    for pr in prs:
        to_retain = []
        notes = []
        if use_memory:
            notes = recall_memory(bank, "comment types the team rejected or accepted")
        comments = review(pr, notes)

        accepted, rejected, repeats = 0, 0, 0
        for c in comments:
            ok = c["category"] in ACCEPT
            if ok:
                accepted += 1
            else:
                rejected += 1
                if c["category"] in rejected_before:
                    repeats += 1
            if use_memory:
                verdict = "ACCEPTED" if ok else "REJECTED"
                if ok:
                    text = (
                        f"In a review of PR #{pr['number']}, the reviewer suggested: "
                        f"\"{c['comment']}\". This suggestion was ACCEPTED. "
                        f"The team values '{c['category']}' feedback like this."
                    )
                else:
                    text = (
                        f"In a review of PR #{pr['number']}, the reviewer suggested: "
                        f"\"{c['comment']}\". This suggestion was REJECTED. "
                        f"Reason: the team does not want '{c['category']}' comments. "
                        f"Do not suggest '{c['category']}' feedback again on similar code."
                    )
                to_retain.append(text)
        rejected_before |= {c["category"] for c in comments if c["category"] not in ACCEPT}
        if use_memory and to_retain:
            retain_memory(bank, "\n".join(to_retain))  # one call per PR
            time.sleep(RETAIN_WAIT)

        rows.append({
            "pr": pr["number"], "title": pr["title"],
            "generated": len(comments), "accepted": accepted,
            "rejected": rejected, "repeat_rejected": repeats,
        })
        print(f"  #{pr['number']}: gen={len(comments)} acc={accepted} "
              f"rej={rejected} repeat_rej={repeats}")
        time.sleep(1)
    return rows


def summarize(rows):
    return {
        "total_generated": sum(r["generated"] for r in rows),
        "total_repeat_rejected": sum(r["repeat_rejected"] for r in rows),
        "total_accepted": sum(r["accepted"] for r in rows),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    prs = load_prs(args.limit)
    print(f"Loaded {len(prs)} PRs")
    result = {}
    for use_memory in (False, True):
        rows = run_arm(prs, use_memory)
        key = "memory_on" if use_memory else "memory_off"
        result[key] = {"per_pr": rows, "summary": summarize(rows)}

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print("\n=== SUMMARY ===")
    for k in ("memory_off", "memory_on"):
        print(k, result[k]["summary"])
    print(f"Saved {OUT_FILE}")