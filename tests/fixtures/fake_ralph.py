#!/usr/bin/env python3
"""Portable fake Ralph loop used by Windows and no-Bash integration tests."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def parse_args(argv):
    iterations = 10
    tool = "claude"
    model = ""
    positional = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--tool" and index + 1 < len(argv):
            tool = argv[index + 1]
            index += 2
            continue
        if value.startswith("--tool="):
            tool = value.split("=", 1)[1]
        elif value.isdigit():
            iterations = int(value)
        else:
            positional.append(value)
        index += 1
    if positional:
        # Positional style is <iterations> <tool> <model>; the numeric token was
        # already consumed, leaving tool/model in order.
        tool = positional[0]
        model = positional[1] if len(positional) > 1 else ""
    return iterations, tool, model


def git_commit(repo: Path, story_id: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["git", "commit", "-q", "-m", f"fake-ralph: {story_id}"], cwd=repo,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def main(argv=None):
    iterations, tool, _model = parse_args(list(sys.argv[1:] if argv is None else argv))
    repo = Path(__file__).resolve().parent
    prd_path = repo / "prd.json"
    progress_path = repo / "progress.txt"
    if not progress_path.exists():
        progress_path.write_text("# Ralph Progress Log\nStarted: fake\n---\n", encoding="utf-8",
                                 newline="\n")
    print(f"Starting Ralph - Tool: {tool} - Max iterations: {iterations}", flush=True)
    for iteration in range(1, iterations + 1):
        print(f"  Ralph Iteration {iteration} of {iterations} ({tool})", flush=True)
        data = json.loads(prd_path.read_text(encoding="utf-8"))
        story = next((item for item in data.get("userStories", []) if not item.get("passes")), None)
        if story is not None:
            story["passes"] = True
            prd_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
                                newline="\n")
            story_id = str(story.get("id") or "")
            with progress_path.open("a", encoding="utf-8", newline="\n") as progress:
                progress.write(f"iteration {iteration}: story {story_id} -> passes\n")
            with (repo / "ralph-work.log").open("a", encoding="utf-8", newline="\n") as work:
                work.write(f"{story_id} done at iteration {iteration}\n")
            git_commit(repo, story_id)
        remaining = sum(1 for item in data.get("userStories", []) if not item.get("passes"))
        if remaining == 0:
            print("Ralph completed all tasks!", flush=True)
            print("<promise>COMPLETE</promise>", flush=True)
            return 0
        time.sleep(float(os.environ.get("FAKE_RALPH_SLEEP", "0")))
    print(f"Ralph reached max iterations ({iterations}) without completing all tasks.", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
