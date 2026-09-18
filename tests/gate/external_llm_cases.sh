#!/bin/bash
# 외부 LLM 벤더 호출 게이트(.claude/hooks/check-external-llm.sh) 검사 (ADR-017).
#
# 실행: bash tests/gate/external_llm_cases.sh      (레포 루트를 cwd 로)
# 종료 코드 0 = 전부 통과. `tests/test_external_llm_gate.py` 가 이 스크립트를 호출한다.
#
# **왜 셸 스크립트 파일인가.** 검사 케이스에는 트리거 문자열이 들어간다. 이것을
# 명령줄에 인라인으로 적으면 훅이 자기 자신의 검사 명령을 차단한다. 파일에 넣고
# `bash <파일>` 로 돌리면 명령줄에 트리거가 없다.
#
# **오탐 대조를 함께 돈다.** 차단 케이스만 있는 검사는 "전부 차단"인 고장 난 게이트도
# 통과시킨다. 이 레포에서는 특히 중요하다 — `anthropic` SDK 가 실제 의존성이라
# 정탐과 오탐의 경계가 MARA 보다 훨씬 가깝다 (MARA session-11 §5.3).
#
# 케이스는 `tests/gate/external_llm_cases.txt` 에 있다. `<기대코드>|<설명>|<명령>`
# 형식이고, 명령의 `\n` 은 개행으로 풀린다(heredoc 케이스). 케이스를 셸 배열이 아니라
# 데이터 파일로 뺀 이유는 페이로드 생성을 **python 한 번**으로 끝내기 위해서다 —
# 케이스마다 python 을 띄우면 이 검사 자체가 분 단위가 된다.
set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-.}"
HOOK="$ROOT/.claude/hooks/check-external-llm.sh"
CASES="$(dirname "$0")/external_llm_cases.txt"
[ -f "$HOOK" ] || { echo "훅을 찾을 수 없습니다: $HOOK"; exit 2; }
[ -f "$CASES" ] || { echo "케이스 파일을 찾을 수 없습니다: $CASES"; exit 2; }

# `command -v` 로 있는지만 보지 않고 실제로 도는지까지 본다 — pyenv-win shim 은 PATH 에
# 잡히지만 bash 에서는 실행되지 않는다 (훅도 같은 이유로 같은 검사를 한다).
PY=""
for c in python python3 py; do
  command -v "$c" >/dev/null 2>&1 || continue
  "$c" -c "pass" >/dev/null 2>&1 && { PY="$c"; break; }
done
[ -n "$PY" ] || { echo "python 이 필요합니다 (JSON 페이로드 생성)"; exit 2; }

# 승인 파일 유무를 레포를 건드리지 않고 조작하기 위해 가짜 프로젝트 디렉터리를 쓴다.
# **이 레포의 `.claude/external-llm-approved` 는 이 검사로 생기지 않는다.**
WORK=$(mktemp -d)
mkdir -p "$WORK/.claude" "$WORK/payloads"
trap 'rm -rf "$WORK"' EXIT

# 케이스 → JSON 페이로드 파일 (한 번에).
"$PY" - "$CASES" "$WORK/payloads" <<'PYEOF'
import json, os, sys
cases_path, out_dir = sys.argv[1], sys.argv[2]
with open(cases_path, encoding="utf-8") as fh:
    rows = [ln.rstrip("\n") for ln in fh if ln.strip() and not ln.startswith("#")]
for i, row in enumerate(rows):
    want, desc, cmd = row.split("|", 2)
    cmd = cmd.replace("\\n", "\n")
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    with open(os.path.join(out_dir, "%03d.json" % i), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload))
    with open(os.path.join(out_dir, "%03d.meta" % i), "w", encoding="utf-8") as fh:
        fh.write("%s\t%s" % (want, desc))
PYEOF
[ $? -eq 0 ] || { echo "페이로드 생성 실패"; exit 2; }

pass=0
fail=0

run_case() {  # $1=페이로드 파일 → 훅 종료 코드
  CLAUDE_PROJECT_DIR="$WORK" bash "$HOOK" <"$1" >/dev/null 2>&1
  echo $?
}

check_file() {  # $1=페이로드 번호
  local n="$1" want desc got
  IFS=$'\t' read -r want desc <"$WORK/payloads/$n.meta"
  got=$(run_case "$WORK/payloads/$n.json")
  if [ "$got" = "$want" ]; then
    pass=$((pass + 1)); printf '  ok   [exit %s] %s\n' "$got" "$desc"
  else
    fail=$((fail + 1)); printf '  FAIL [exit %s, 기대 %s] %s\n' "$got" "$want" "$desc"
  fi
}

for f in "$WORK"/payloads/*.json; do
  check_file "$(basename "$f" .json)"
done

echo "== 승인 파일이 있으면 통과한다 =="
approved_case="$WORK/payloads/000.json"   # 000 = 벤더 엔드포인트 직접 호출(차단 케이스)
touch "$WORK/.claude/external-llm-approved"
got=$(run_case "$approved_case")
if [ "$got" = "0" ]; then
  pass=$((pass + 1)); printf '  ok   [exit 0] 승인 파일 존재 시 통과\n'
else
  fail=$((fail + 1)); printf '  FAIL [exit %s, 기대 0] 승인 파일 존재 시 통과\n' "$got"
fi
rm -f "$WORK/.claude/external-llm-approved"
got=$(run_case "$approved_case")
if [ "$got" = "2" ]; then
  pass=$((pass + 1)); printf '  ok   [exit 2] 승인 파일 제거 후 다시 차단\n'
else
  fail=$((fail + 1)); printf '  FAIL [exit %s, 기대 2] 승인 파일 제거 후 다시 차단\n' "$got"
fi

echo "== 파서가 없으면 통과가 아니라 차단이다 =="
# PATH 를 비워 jq·python 을 모두 감춘다. 게이트가 사라지는 것이 기본값이면 안 된다.
# bash 자신은 절대경로로 부른다 — PATH 를 비웠으므로 이름으로는 찾지 못한다(127).
BASH_BIN=$(command -v bash)
noparser=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"echo hi"}}' \
  | env -i PATH=/nonexistent "$BASH_BIN" "$HOOK" >/dev/null 2>&1; echo $?)
if [ "$noparser" = "2" ]; then
  pass=$((pass + 1)); printf '  ok   [exit 2] 파서 부재 시 fail-closed\n'
else
  fail=$((fail + 1)); printf '  FAIL [exit %s, 기대 2] 파서 부재 시 fail-closed\n' "$noparser"
fi

echo
echo "통과 ${pass} / 실패 ${fail}"
[ "$fail" -eq 0 ] || exit 1
exit 0
