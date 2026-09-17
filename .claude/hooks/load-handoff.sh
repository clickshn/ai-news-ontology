#!/bin/bash
f=$(ls -t docs/handoff/*.md 2>/dev/null | head -1)
if [ -n "$f" ]; then
  echo "=== 이전 세션 핸드오프: $f ==="
  cat "$f"
fi
