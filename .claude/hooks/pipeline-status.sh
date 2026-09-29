#!/bin/bash
# SessionStart — 매일 자동 실행의 상태를 주입한다 (ADR-025).
#
# 왜 있는가. 자동 실행의 경보는 stderr 로 가면 아무도 안 본다. 개발은 Claude Code
# 세션에서 하므로 세션 첫 화면이 사람이 이미 보는 곳 중 하나다 (다른 하나는 Vault 의
# _pipeline-status.md).
#
# **실행 부재는 여기서 계산한다.** 스케줄러가 안 돌면 파이프라인은 아무것도 쓰지 못한다
# — 쓰는 쪽은 자기가 안 돈 것을 기록할 수 없다. 그래서 상태 파일에는 마지막 실행 시각만
# 있고, 나이는 읽는 쪽(이 훅)이 잰다. CRLF 로 훅이 조용히 안 돈 전례와 같은 모양의
# 고장을 막으려는 것이다.
#
# 자동 실행을 쓰지 않는 상태(상시 승인 기록도 상태 파일도 없음)면 아무것도 출력하지 않는다.
# 실패해도 세션을 막지 않는다 — 항상 exit 0.

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

status="data/pipeline/status.txt"
approval=".claude/scheduled-run-approved.json"

if [ ! -f "$status" ]; then
  if [ -f "$approval" ]; then
    echo "=== [pipeline-status] ⚠️ 상시 승인 기록은 있는데 자동 실행 기록($status)이 없다 — 스케줄러가 한 번도 안 돌았을 수 있다 ==="
  fi
  exit 0
fi

value() { sed -n "s/^$1=//p" "$status" | head -1 | tr -d '\r'; }

last=$(value last_run_epoch)
last_at=$(value last_run_at)
outcome=$(value outcome)
code=$(value exit_code)
absent=$(value absent_after_hours)
expires=$(value approval_expires)
case $absent in '' | *[!0-9]*) absent=36 ;; esac

now=$(date +%s)
age_h="?"
case $last in
  '' | *[!0-9]*) ;;
  *) age_h=$(( (now - last) / 3600 )) ;;
esac

echo "=== 자동 실행 상태: 마지막 $last_at (${age_h}시간 전) · $outcome · exit=$code ==="
if [ "$age_h" = "?" ] || [ "$age_h" -gt "$absent" ]; then
  echo "⚠️ [pipeline-status] 마지막 자동 실행이 ${absent}시간보다 오래됐다 — 스케줄러가 안 돌았을 수 있다. 작업 스케줄러와 PC 전원·VPN 을 볼 것"
fi
if [ -n "$expires" ]; then
  today=$(date -u +%Y-%m-%d)
  if [ "${expires:0:10}" \< "$today" ] || [ "${expires:0:10}" = "$today" ]; then
    echo "⚠️ [pipeline-status] 상시 승인이 만료됐다 ($expires). 표본 3건 대조 후 approve-schedule 로 갱신 — 그 전까지 자동 실행은 멈춘다"
  fi
fi
sed -n '/^---$/,$p' "$status" | sed '1d' | tr -d '\r'
exit 0
