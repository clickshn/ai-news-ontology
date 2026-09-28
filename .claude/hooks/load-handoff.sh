#!/bin/bash
# SessionStart — 최신 handoff 를 주입한다.
#
# "최신"은 **세션 번호**로 가른다. 수정 시각(`ls -t`)으로 가르면 에디터·포매터가
# 옛 handoff 를 건드리는 순간 그 파일이 주입되고, 훅은 정상 종료하므로 아무 신호도
# 나지 않는다. session-13 재개 때 실제로 session-01 이 주입됐다.
#
# 고른 파일명과 선택 기준을 첫 줄에 찍는다 — 틀린 파일이 들어와도 눈으로 잡을 수
# 있어야 한다. 수정 시각이 가장 최근인 파일이 주입 대상과 다르면 경고를 붙인다.

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

latest=""
latest_n=-1
for path in docs/handoff/session-*.md; do
  [ -e "$path" ] || continue
  num=${path##*/session-}
  num=${num%.md}
  case $num in '' | *[!0-9]*) continue ;; esac
  n=$((10#$num))
  if [ "$n" -gt "$latest_n" ]; then
    latest_n=$n
    latest=$path
  fi
done

if [ -z "$latest" ]; then
  echo "=== [load-handoff] docs/handoff/session-NN.md 를 찾지 못했다 — 주입하지 않음 ==="
  exit 0
fi

echo "=== 이전 세션 핸드오프: $latest (선택 기준: 세션 번호 최댓값 $latest_n) ==="

newest=$(ls -t docs/handoff/*.md 2>/dev/null | head -1)
if [ -n "$newest" ] && [ "$newest" != "$latest" ]; then
  echo "⚠️ [load-handoff] 수정 시각이 가장 최근인 파일은 $newest 다 — 주입 대상과 다르다. 옛 handoff 가 수정됐는지 git status 로 확인할 것"
fi

cat "$latest"
