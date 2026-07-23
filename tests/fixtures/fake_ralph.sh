#!/usr/bin/env bash
# 自足的 fake ralph 迴圈:給 engine.ralph 監督層測試用,不需網路、不需真 agent。
# 行為刻意貼近 snarktank/ralph.sh:每輪標記一個 story passes=true、append progress、
# 印出 "Ralph Iteration i of N" banner;全部完成印 <promise>COMPLETE</promise> 後 exit 0。
#
# 引數:接受位置參數 `<iters> <tool> <model>`(公司版預設),也接受 `--tool X <iters>`。
set -e

MAX_ITERATIONS=10
TOOL="claude"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tool) TOOL="$2"; shift 2 ;;
    --tool=*) TOOL="${1#*=}"; shift ;;
    *) if [[ "$1" =~ ^[0-9]+$ ]]; then MAX_ITERATIONS="$1"; fi; shift ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRD_FILE="$SCRIPT_DIR/prd.json"
PROGRESS_FILE="$SCRIPT_DIR/progress.txt"

if [ ! -f "$PROGRESS_FILE" ]; then
  echo "# Ralph Progress Log" > "$PROGRESS_FILE"
  echo "Started: fake" >> "$PROGRESS_FILE"
  echo "---" >> "$PROGRESS_FILE"
fi

echo "Starting Ralph - Tool: $TOOL - Max iterations: $MAX_ITERATIONS"

for i in $(seq 1 "$MAX_ITERATIONS"); do
  echo ""
  echo "==============================================================="
  echo "  Ralph Iteration $i of $MAX_ITERATIONS ($TOOL)"
  echo "==============================================================="

  # 標記下一個 passes=false 的 story 為完成,並在 repo 產生一個 commit(模擬 agent 動作)。
  DONE_ID=$(python3 - "$PRD_FILE" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
stories = data.get("userStories", [])
target = next((s for s in stories if not s.get("passes")), None)
if target is not None:
    target["passes"] = True
    json.dump(data, open(path, "w"), indent=2)
    print(target.get("id", ""))
PY
)

  if [ -n "$DONE_ID" ]; then
    echo "Implemented story $DONE_ID" >> "$PROGRESS_FILE"
    echo "iteration $i: story $DONE_ID -> passes" >> "$PROGRESS_FILE"
    # 在 repo 內做一個真 commit,讓監督層的 commit_count 投影有東西可數。
    echo "$DONE_ID done at iteration $i" >> "ralph-work.log"
    git add -A >/dev/null 2>&1 || true
    git commit -q -m "fake-ralph: $DONE_ID" >/dev/null 2>&1 || true
    echo "Iteration $i complete for $DONE_ID."
  fi

  REMAIN=$(python3 - "$PRD_FILE" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print(sum(1 for s in data.get("userStories", []) if not s.get("passes")))
PY
)

  if [ "$REMAIN" = "0" ]; then
    echo ""
    echo "Ralph completed all tasks!"
    echo "<promise>COMPLETE</promise>"
    exit 0
  fi
  sleep "${FAKE_RALPH_SLEEP:-0}"
done

echo ""
echo "Ralph reached max iterations ($MAX_ITERATIONS) without completing all tasks."
exit 1
