---
paths:
  - "extraction/**"
  - "export/**"
  - "eval/**"
  - ".claude/hooks/**"
  - "config.yaml"
---

# 외부 LLM 벤더 호출

**이 레포의 벤더 호출은 정상 경로가 아니다.** 승인받고 쓰는 예외 경로다.

근거는 ADR-017 이고, 그 앞에 MARA 의 ADR-021 이 있다. 요지는 한 문장이다 —
이 레포가 만드는 코퍼스의 소비자(`multiagent-research-lab`)는 **외부 LLM 벤더
비의존**이 전제인데, MARA Session 0.5a 에서 그 전제가 깨졌고 **실제로 벤더를 부른
코드가 여기 있었다.** 규칙은 저쪽에 있고 위반은 이쪽에서 났다.

## 기본값은 차단이다

```
.claude/external-llm-approved      ← 있으면 열린다. 없으면 막힌다. 1회용.
```

- 환경변수로 여는 우회로는 **없다.** 환경변수는 셸 한 줄로 켜지고 그 줄이 어디에도
  남지 않는다. 파일은 만들고 지우는 행위가 필요하고, 남아 있으면 보인다.
- 끝나면 **지운다.** 남겨 두면 "1회용"이 아니다.
  `tests/test_external_llm_gate.py` 가 레포에 남은 승인 파일을 잡는다.

## 집행하는 두 계층

| 계층 | 파일 | 막는 것 |
|---|---|---|
| 도구 | `.claude/hooks/check-external-llm.sh` | Bash·PowerShell **명령 문자열**의 벤더 호스트 / 비의존 벤더 SDK 임포트 / 벤더 클라이언트 생성·호출 표현 / API 키 주입 / 외부 모델 CLI. 차단은 exit 2 |
| 코드 | `extraction/egress.py` + `AnthropicClient.__init__` | 우리 코드를 경유하는 모든 벤더 클라이언트 생성. **객체 생성 시점**에 `ExternalVendorCallError` |

겹치는 부분이 아니라 **안 겹치는 부분** 때문에 둘을 둔다. 훅은 스크립트 파일 안을
못 보고, 코드 가드는 우리 코드 안에서만 유효하다.

## ⚠️ 막지 못하는 것 — 기억이 아니라 테스트에 적혀 있다

`tests/gate/external_llm_cases.txt` 에 **기대값 0(통과)** 으로 고정돼 있다. 이 줄들이
언젠가 exit 2 가 되면 그건 실패가 아니라 개선이다 — 그때 기대값을 바꾼다.

- **스크립트 파일 / 셸 스크립트 / Makefile 경유** — 명령줄에 벤더 흔적이 없다
- **난독화**(base64 등) — 문자열 검사를 통과한다
- **에이전트 자신의 모델 호출과 Agent/Task 서브에이전트** — 훅의 적용 범위 밖
- **MCP 도구 경유 네트워크 호출** — 매처가 `Bash|PowerShell` 이다
- **거부 목록의 빈칸** — 새 벤더·사설 프록시는 목록에 없다

> ⚠️ **이 레포에서는 이 구멍이 MARA 보다 크다.** 저쪽은 코드 계층이 엔드포인트
> 하나만 읽는 설계였지만, 여기에는 `anthropic` SDK 가 실제로 설치돼 있고 키가
> `.env` 에 있다. **스크립트 파일 하나면 두 계층 밖에서 진짜 호출이 나간다.**
> 이것이 vLLM 이전(ADR-017 ②)이 게이트로 대체되지 않는 이유다 — 게이트는 재발을
> 늦출 뿐이고, 경로 자체를 없애는 것은 이전뿐이다.

## 오탐은 MARA 와 다르다 — 복사하지 않는다

- **`anthropic` SDK 는 이 레포의 실제 의존성이다.** `import anthropic` 은 정상
  코드와 테스트가 한다. 그래서 훅은 **임포트가 아니라 생성·호출 표현**을 본다.
- **파이프라인 실행(`python -m extraction.extractor`)은 정상 작업이다.** MARA 훅에는
  이것을 차단하는 규칙이 있지만 여기서는 의미가 뒤집힌다. 이식하지 않았고,
  `tests/test_external_llm_gate.py` 가 되살아나는 것을 막는다.
- **`.env.example` 에서 키 이름을 읽는 것은 주입이 아니다.** `grep "..._API_KEY="`
  는 통과한다.

## 자기참조

훅은 명령 문자열을 검사하므로 **벤더 호출 표현을 예시로 적는 명령은 그 자체가
차단된다.** 문서·규칙·케이스 파일은 **Write/Edit 도구로 쓴다.** 명령줄에 트리거
문자열을 넣지 않는 것이 회피가 아니라 정상 경로다. 커밋 메시지에도 넣지 않는다.

## 검사

```bash
bash tests/gate/external_llm_cases.sh                 # 훅 케이스 46건
.venv/Scripts/python.exe -m pytest tests/test_external_llm_gate.py tests/test_blocked_state_run.py -q
```
