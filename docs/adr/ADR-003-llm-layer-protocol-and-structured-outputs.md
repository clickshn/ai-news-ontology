# ADR-003: LLM 호출을 `LLMClient` Protocol 뒤에 두고 structured outputs 로 받는다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** 모든 LLM 호출은 `LLMClient` Protocol 의 `parse_into` 하나를 거치고, 구조화 출력은 `messages.parse(output_format=<pydantic 모델>)` 로 받는다. strict tool use 를 쓰지 않는다.
- **Scope:** ai-news-ontology (extraction / eval 의 LLM 계층)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-004**, **D-009**. 둘 다 "LLM 계층을 어떻게 만들 것인가"
> 한 설계의 두 면이라 하나로 묶었다. 원본 날짜와 근거는 그대로다.

---

## Context

### Problem

기본 프로바이더는 Anthropic(`claude-opus-5`)이지만 호출부가 특정 SDK 에 묶이면
judge 를 다른 모델로 바꿔 편향을 확인할 여지가 사라진다. 동시에 "구조화된 결과를
어떻게 받을 것인가"를 정해야 했다 — 후보는 strict tool use 와 structured outputs 였다.

### Constraints

- 이 파이프라인의 LLM 호출은 전부 "구조화된 결과를 받는" 형태다. 자유 텍스트를 받아
  우리가 파싱하면 **파싱 실패와 스키마 실패를 구분할 수 없다.**
- 스키마 정의(`schema.py`)와 런타임 검증 사이에 손으로 옮겨 적는 단계가 생기면
  단일 출처 원칙(ADR-002)이 깨진다.

## Decision

### Selected

- **Technology:** Anthropic Python SDK 의 `messages.parse(output_format=...)`
- **Architecture:** `LLMClient` Protocol 의 유일한 메서드가 `parse_into` 다. 자유 텍스트
  경로를 아예 두지 않는다.
- **Implementation:** `extraction/llm.py`. 스키마 검증 실패는 오류 메시지를 되먹여
  1회 재시도하고, 그래도 실패하면 `SchemaMismatchError` 를 올린다 — **부분 결과를
  반환하지 않는다.**

## Rationale

1. 기본은 Anthropic 이지만 judge 를 다른 모델로 바꿔 편향을 확인할 여지를 남긴다.
2. 우리가 하는 일은 **도구 호출이 아니라 단일 추출**이다. tool use 는 "모델이 도구를
   부르도록 유도"하는 우회로이고, 도구를 안 부르고 텍스트로 답하는 분기가 항상 남아
   호출부가 그것을 방어해야 한다.
3. `parse` 는 pydantic 모델을 그대로 넘기고 검증된 객체를 그대로 받는다. `schema.py` 와
   런타임 사이에 손으로 옮겨 적는 단계가 없어 ADR-002 와 일관된다.
4. 도구가 여러 개이거나 모델이 호출 여부를 스스로 판단해야 할 때 tool use 가 값을
   하는데, 여기엔 그런 요구가 없다.

## Alternatives

### strict tool use

- **Pros:** 입력 스키마를 강제할 수 있고 널리 쓰이는 방식이다.
- **Cons:** 도구를 안 부르고 텍스트로 답하는 분기를 늘 방어해야 한다.
- **Rejected because:** 이건 도구 호출이 아니라 단일 추출이라 우회로를 탈 이유가 없다.

### 프롬프트로 JSON 을 유도하고 자체 파싱

- **Pros:** SDK 기능에 의존하지 않는다.
- **Cons:** 파싱 실패와 스키마 실패가 섞여 원인을 가릴 수 없다.
- **Rejected because:** 실패 구분이 안 되면 격리 판단의 근거가 사라진다.

### SDK 직접 호출 (추상화 없음)

- **Pros:** 계층이 하나 줄어든다.
- **Cons:** judge 모델 교체가 코드 변경이 된다.
- **Rejected because:** 편향 확인 여지를 미리 닫아 버린다.

## Consequences

### Positive

- 검증 실패가 예외로 올라와 호출부가 기사 단위로 격리할 수 있다. 부분 결과가 Vault 를
  조용히 오염시키지 않는다.
- eval 의 judge 가 같은 인터페이스를 재사용한다.

### Negative

- 모델마다 받는 파라미터가 달라 Protocol 아래에서 분기가 생긴다 (ADR-013).

## Reversibility

- **Reversible:** Yes
- **Rollback:** 다른 구현체를 Protocol 뒤에 끼우면 된다. 호출부는 바뀌지 않는다.
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-002(스키마 단일 출처), ADR-013(모델별 파라미터)
- **Documentation:** `README.md` 결정 로그 D-004·D-009, `extraction/llm.py`
