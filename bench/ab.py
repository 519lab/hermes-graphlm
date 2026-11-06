"""A/B harness: the same navigation tasks with and without the graphlm plugin.

Runs real ``hermes chat -q --oneshot --format stream-json`` sessions and counts
what the agent did. Enable or disable the plugin yourself between arms
(``hermes plugins enable|disable graphlm``); this script only runs and records.

    python bench/ab.py --arm with  --reps 2 --out bench/results/with.jsonl
    python bench/ab.py --arm without --reps 2 --out bench/results/without.jsonl
    python bench/ab.py --report bench/results/with.jsonl bench/results/without.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

TASKS = [
    {
        "id": "graphlm-signature",
        "repo": "~/coding-projects/graphLM",
        "prompt": ("If I change the signature of generate_graph in graphlm/__init__.py, which "
                   "files import it and would need updating? List the files only."),
    },
    {
        "id": "argus-tooladapter",
        "repo": "~/coding-projects/argus",
        "prompt": ("Which files depend on src/argus/tools/base.py (directly or through other "
                   "files) and would be affected if I changed the ToolAdapter class? "
                   "List the files only."),
    },
    {
        "id": "skynet-orientation",
        "repo": "~/coding-projects/skynet",
        "prompt": ("Give me a quick orientation: what are the main modules of this repository "
                   "and how do they depend on each other? Under 150 words."),
    },
]
SEARCH_TOOLS = {"search_files", "read_file", "terminal", "execute_code"}


def run_task(task: dict, timeout: int) -> dict:
    repo = str(Path(task["repo"]).expanduser())
    cmd = ["hermes", "chat", "-q", task["prompt"], "--oneshot", "--format", "stream-json",
           "--in", repo, "--max-turns", "30"]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.monotonic() - started
    tools: Counter[str] = Counter()
    result: dict = {}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "tool_use":
            tools[event.get("name") or "?"] += 1
        elif event.get("type") == "result":
            result = event
    tokens = result.get("tokens") or {}
    return {
        "task": task["id"], "exit": proc.returncode, "seconds": round(elapsed, 1),
        "tool_calls": sum(tools.values()),
        "search_read_calls": sum(n for t, n in tools.items() if t in SEARCH_TOOLS),
        "graphlm_calls": sum(n for t, n in tools.items() if t.startswith("graphlm_")),
        "tools": dict(tools),
        "input_tokens": tokens.get("input"), "output_tokens": tokens.get("output"),
        "answer": (result.get("text") or "")[:1500],
    }


def report(paths: list[str]) -> None:
    for path in paths:
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line]
        print(f"\n== {path} ({len(rows)} runs)")
        for task in sorted({r["task"] for r in rows}):
            rs = [r for r in rows if r["task"] == task]

            def med(key: str) -> float:
                return statistics.median(r[key] or 0 for r in rs)

            print(f"  {task:22} tools={med('tool_calls'):5.1f}  search/read="
                  f"{med('search_read_calls'):5.1f}  graphlm={med('graphlm_calls'):4.1f}  "
                  f"input_tok={med('input_tokens'):9.0f}  secs={med('seconds'):6.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", choices=["with", "without"])
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--out")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", help="run a single task id")
    ap.add_argument("--report", nargs="+")
    args = ap.parse_args()
    if args.report:
        report(args.report)
        return 0
    if not args.arm or not args.out:
        ap.error("--arm and --out are required to run")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tasks = [t for t in TASKS if not args.only or t["id"] == args.only]
    with out.open("a", encoding="utf-8") as fh:
        for rep in range(args.reps):
            for task in tasks:
                row = {"arm": args.arm, "rep": rep, **run_task(task, args.timeout)}
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                print(f"{args.arm} rep{rep} {task['id']}: tools={row['tool_calls']} "
                      f"search/read={row['search_read_calls']} graphlm={row['graphlm_calls']} "
                      f"in_tok={row['input_tokens']} {row['seconds']}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
