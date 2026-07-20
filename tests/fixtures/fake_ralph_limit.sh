#!/usr/bin/env bash
# Fake ralph for usage-limit testing. Positional args: <iterations> <tool> <model>.
# 若 model 含 "limited"：每輪印出 tier-1 usage-limit 字樣但「不 commit、不寫 progress」
# （no-progress），讓監督層偵測器確認用量上限。否則（降級後的 model）正常推進 story 到完成。
set -e

ITERS="${1:-10}"
TOOL="${2:-claude}"
MODEL="${3:-}"
[[ "$ITERS" =~ ^[0-9]+$ ]] || ITERS=10

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRD_FILE="$SCRIPT_DIR/prd.json"
PROGRESS_FILE="$SCRIPT_DIR/progress.txt"
[ -f "$PROGRESS_FILE" ] || echo "# Ralph Progress Log" > "$PROGRESS_FILE"

echo "Starting Ralph - Tool: $TOOL - model: ${MODEL:-none} - iterations: $ITERS"

for i in $(seq 1 "$ITERS"); do
  echo ""
  echo "==============================================================="
  echo "  Ralph Iteration $i of $ITERS ($TOOL)"
  echo "==============================================================="

  if [[ "$MODEL" == *limited* ]]; then
    # tier-1 訊號 + 明確 reset epoch；本輪不做任何 repo/progress 變更 → no-progress。
    echo "Claude usage limit reached|1893456000"
    echo "Iteration $i complete. Continuing..."
    sleep "${FAKE_RALPH_SLEEP:-0}"
    continue
  fi

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
    # 模擬 agent 正在寫「rate limit 處理」相關程式碼:輸出含 tier-2 字樣但本輪有 commit(有進展)。
    # no-progress gate 必須因此不誤判為用量上限。
    echo "note: implementing rate limit handling for $DONE_ID (429 backoff)"
    echo "healthy $MODEL implemented $DONE_ID at iteration $i" >> "$PROGRESS_FILE"
    echo "$DONE_ID at $i" >> "ralph-work.log"
    git add -A >/dev/null 2>&1 || true
    git commit -q -m "fake-ralph($MODEL): $DONE_ID" >/dev/null 2>&1 || true
    echo "Iteration $i complete for $DONE_ID."
  fi
  REMAIN=$(python3 - "$PRD_FILE" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print(sum(1 for s in data.get("userStories", []) if not s.get("passes")))
PY
)
  if [ "$REMAIN" = "0" ]; then
    echo "<promise>COMPLETE</promise>"
    exit 0
  fi
  sleep "${FAKE_RALPH_SLEEP:-0}"
done
exit 1
