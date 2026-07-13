#!/usr/bin/env python3
"""Deterministic fake agent for local fleet integration tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


prompt = sys.stdin.read()
ws = Path(os.environ["LOOP_WS"])
state = json.loads((ws / "state.json").read_text(encoding="utf-8"))
phase = (ws / "phase").read_text(encoding="utf-8").strip()
env = dict(os.environ)

if phase == "plan":
    delay = float(os.environ.get("FLEET_FAKE_PLAN_DELAY", "0"))
    if delay:
        time.sleep(delay)
    if not state.get("plan"):
        plan = [
            {"order": 1, "task": "create alpha.txt and commit it; DoD: test -f alpha.txt", "track": "alpha"},
            {"order": 2, "task": "create beta.txt and commit it; DoD: test -f beta.txt", "track": "beta"},
            {"order": 3, "task": "verify integrated result and create final.txt; DoD: test -f final.txt", "track": "@final"},
        ]
        subprocess.run([sys.executable, "-m", "engine.work", "create-plan"],
                       input=json.dumps(plan), text=True, env=env, check=True)
    else:
        subprocess.run([sys.executable, "-m", "engine.work", "plan-ok"], env=env, check=True)
elif phase == "exec":
    delay = float(os.environ.get("FLEET_FAKE_EXEC_DELAY", "0"))
    if delay:
        time.sleep(delay)
    track = state["track"]
    artifact = Path("final.txt" if track == "@final" else f"{track}.txt")
    if not artifact.exists():
        artifact.write_text(f"{track}\n", encoding="utf-8")
        subprocess.run(["git", "add", artifact.name], check=True)
        subprocess.run(["git", "commit", "-m", f"{track} task"], check=True)
    else:
        task_id = (ws / "current_task").read_text(encoding="utf-8").strip()
        subprocess.run([sys.executable, "-m", "engine.work", "done", task_id], env=env, check=True)
elif phase == "merge":
    tip = state["merge_target_tip"]
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", tip, "HEAD"]).returncode == 0
    repair_requested = os.environ.get("FLEET_FAKE_REPAIR") == "1" and "已自動 rollback" in prompt
    if repair_requested and not Path("compat.txt").exists():
        Path("compat.txt").write_text("repaired\n", encoding="utf-8")
        subprocess.run(["git", "add", "compat.txt"], check=True)
        subprocess.run(["git", "commit", "-m", "repair integration validation"], check=True)
    elif not ancestor:
        subprocess.run(["git", "merge", "--no-edit", tip], check=True)
    else:
        subprocess.run([sys.executable, "-m", "engine.work", "done", "merge-main"], env=env, check=True)
