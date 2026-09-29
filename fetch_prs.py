"""
Fetch real merged PRs from pallets/flask, filter to ones with readable,
demo-sized diffs, and save them as JSON files the replay engine can load.

Run: python fetch_prs.py
Output: data/prs/pr_<number>.json, one per kept PR, plus data/prs/index.json
"""

import os
import json
import time
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

REPO = "pallets/flask"
TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    raise SystemExit("GITHUB_TOKEN not set in .env — see the setup steps before running this.")

HEADERS_JSON = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
HEADERS_DIFF = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github.v3.diff",
}

DATA_DIR = Path(__file__).parent / "data" / "prs"
DATA_DIR.mkdir(parents=True, exist_ok=True)

TARGET_COUNT = 25
MAX_DIFF_LINES = 200      # keep diffs small enough to be readable + cheap for the LLM
MIN_DIFF_LINES = 4        # skip trivial one-line diffs, not interesting for a demo
PAGES_TO_SCAN = 20        # how many pages of closed PRs to look through to find enough good ones
SKIP_WORDS = ("release", "bump", "codespell", "flit_core", "pre-commit", "typo")

def get_closed_prs(page: int):
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls",
        headers=HEADERS_JSON,
        params={"state": "closed", "sort": "updated", "direction": "desc",
                "per_page": 30, "page": page},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_diff(pr_number: int) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{pr_number}",
        headers=HEADERS_DIFF,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def main():
    kept = []
    seen_numbers = set()

    for page in range(1, PAGES_TO_SCAN + 1):
        if len(kept) >= TARGET_COUNT:
            break

        prs = get_closed_prs(page)
        if not prs:
            break

        for pr in prs:
            if len(kept) >= TARGET_COUNT:
                break
            if not pr.get("merged_at"):
                continue  # only want PRs that were actually merged, not just closed
            if any(w in pr["title"].lower() for w in SKIP_WORDS):
                continue
            if pr["number"] in seen_numbers:
                continue
            seen_numbers.add(pr["number"])

            try:
                diff = get_diff(pr["number"])
            except requests.HTTPError as e:
                print(f"  skip PR #{pr['number']}: {e}")
                continue

            diff_lines = diff.count("\n")
            if diff_lines < MIN_DIFF_LINES or diff_lines > MAX_DIFF_LINES:
                continue  # too trivial or too big for a clean demo

            record = {
                "id": f"PR-{pr['number']}",
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["html_url"],
                "merged_at": pr["merged_at"],
                "diff": diff,
            }
            out_path = DATA_DIR / f"pr_{pr['number']}.json"
            out_path.write_text(json.dumps(record, indent=2))
            kept.append({"id": record["id"], "number": pr["number"],
                          "title": pr["title"], "url": pr["url"],
                          "diff_lines": diff_lines})
            print(f"  kept PR #{pr['number']} ({diff_lines} diff lines): {pr['title'][:70]}")

            time.sleep(0.3)  # be polite to the API

    index_path = DATA_DIR / "index.json"
    index_path.write_text(json.dumps(kept, indent=2))

    print(f"\nSaved {len(kept)} PRs to {DATA_DIR}")
    if len(kept) < TARGET_COUNT:
        print(f"(wanted {TARGET_COUNT}, only found {len(kept)} that fit the size window — "
              f"you can lower MIN_DIFF_LINES/raise MAX_DIFF_LINES or increase PAGES_TO_SCAN and re-run)")


if __name__ == "__main__":
    main()
