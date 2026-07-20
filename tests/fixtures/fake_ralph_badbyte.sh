#!/usr/bin/env bash
# Fake ralph that emits invalid UTF-8 每輪；驗證監督層 binary 讀取不會因壞位元組卡死。
set -e
ITERS="${1:-3}"; [[ "$ITERS" =~ ^[0-9]+$ ]] || ITERS=3
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRD="$SCRIPT_DIR/prd.json"
for i in $(seq 1 "$ITERS"); do
  echo "  Ralph Iteration $i of $ITERS (claude)"
  printf 'binary noise: \xff\xfe\xf0 garbage tail\n'
  DONE=$(python3 - "$PRD" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); s=next((x for x in d.get("userStories",[]) if not x.get("passes")),None)
if s: s["passes"]=True; json.dump(d,open(sys.argv[1],"w")); print(s.get("id",""))
PY
)
  [ -n "$DONE" ] && { git add -A >/dev/null 2>&1 || true; git commit -q -m "b:$DONE" >/dev/null 2>&1 || true; }
  REMAIN=$(python3 -c "import json,sys;print(sum(1 for x in json.load(open('$PRD')).get('userStories',[]) if not x.get('passes')))")
  [ "$REMAIN" = "0" ] && { echo "<promise>COMPLETE</promise>"; exit 0; }
done
exit 1
