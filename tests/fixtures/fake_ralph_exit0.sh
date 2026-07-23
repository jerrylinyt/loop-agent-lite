#!/usr/bin/env bash
# Fake ralph that never completes and exits 0 at max iterations (公司版可能不像 snarktank 回 1)。
ITERS="${1:-2}"; [[ "$ITERS" =~ ^[0-9]+$ ]] || ITERS=2
for i in $(seq 1 "$ITERS"); do
  echo "  Ralph Iteration $i of $ITERS (claude)"
  echo "did nothing useful this iteration"
done
echo "fell off the end without completing"
exit 0
