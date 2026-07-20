#!/usr/bin/env python3
"""Fake coding agent for driving the REAL snarktank ralph.sh in tests without an LLM.

真正的 ralph.sh 會呼叫 `amp` 或 `claude`;e2e 用一支同名 wrapper 指到本檔,讓真正的
ralph 迴圈跑起來但不需要網路或真 agent。行為:讀 stdin(prompt,忽略)→ 把 cwd 下 prd.json
第一個 passes=false 的 story 標記完成、append progress.txt、做一個 git commit;全部完成時
印出 <promise>COMPLETE</promise>(ralph.sh 靠它判定收斂並 exit 0)。
"""
import json
import subprocess
import sys
from pathlib import Path


def main():
    """讀 stdin 後推進一個 story;全部完成則輸出 sentinel。"""
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001 — 沒有 stdin 也要能跑
        pass
    prd_path = Path("prd.json")
    if not prd_path.exists():
        print("fake-agent: no prd.json in cwd", flush=True)
        return 0
    data = json.loads(prd_path.read_text(encoding="utf-8"))
    stories = data.get("userStories", [])
    target = next((s for s in stories if not s.get("passes")), None)
    if target is not None:
        target["passes"] = True
        prd_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        story_id = target.get("id", "?")
        progress = Path("progress.txt")
        with progress.open("a", encoding="utf-8") as handle:
            handle.write(f"Implemented {story_id} (fake-agent)\n")
        Path("fake-agent-work.log").write_text(f"last: {story_id}\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", f"fake-agent: {story_id}"], capture_output=True)
        print(f"fake-agent implemented {story_id}", flush=True)
    remaining = sum(1 for s in stories if not s.get("passes"))
    if remaining == 0:
        print("<promise>COMPLETE</promise>", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
