# ADR-004: 프롬프트를 버전 파일로 분리하고 실행에 쓰인 버전은 수정하지 않는다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** 프롬프트를 `{용도}.{버전}.md` 파일로 분리하고, 실행에 쓰인 버전은 수정하지 않는다. 변경이 필요하면 새 버전 파일을 만들고 이전 버전을 보존한다.
- **Scope:** ai-news-ontology (extraction / eval 프롬프트)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-010**(관례 도입), **D-023**(첫 적용 — v1 보존하고 v2 생성).
> 같은 결정과 그 첫 집행이라 하나로 묶었다. 원본 날짜와 근거는 그대로다.

---

## Context

### Problem

프롬프트를 코드에 문자열로 하드코딩하면 어떤 지시에서 나온 출력인지 추적할 수 없다.

### Constraints

- **프롬프트가 바뀌면 eval 점수도 바뀐다.** 어떤 버전에서 나온 점수인지 추적하지
  못하면 eval 자체가 의미를 잃는다.
- v1 은 이미 실제 추출 1건에 쓰였다. 그 출력이 어떤 지시에서 나왔는지 남아 있어야 한다.

## Decision

### Selected

- **Technology:** 마크다운 파일 + `# System` / `# User` 섹션 규약
- **Architecture:** `extraction/prompts/` 와 `eval/judge_prompts/` 에 같은 버저닝
  관례를 적용한다. 어떤 버전을 썼는지 결과에 함께 실어 보낸다.
- **Implementation:** `extraction/extractor.py: load_prompt()`,
  `extraction/prompts/README.md` 에 버전별 변경 내용 표.

## Rationale

1. 프롬프트가 바뀌면 eval 점수도 바뀐다. 버전 추적이 없으면 점수 추이가 모델 때문인지
   프롬프트 때문인지 영원히 가를 수 없다.
2. 이미 실행에 쓰인 파일을 덮어쓰면 과거 산출물의 근거가 사라진다. 한 단어만 바꿔도
   `prompt_sha256` 이 달라지므로 파일을 고치는 것과 버전을 올리는 것은 같은 일이 아니다.

## Alternatives

### 코드에 문자열로 하드코딩

- **Pros:** 파일 로딩 계층이 필요 없다.
- **Cons:** 어떤 지시에서 나온 출력인지 추적할 수 없다.
- **Rejected because:** eval 점수의 출처를 설명할 수 없게 된다.

### v1 을 직접 수정

- **Pros:** 파일이 늘지 않는다.
- **Cons:** v1 으로 뽑은 추출 1건의 근거가 사라진다.
- **Rejected because:** 버저닝 관례를 만들자마자 예외를 두면 관례가 아니다.

## Consequences

### Positive

- 출력 계약의 `provenance.prompt_version` / `prompt_sha256` 이 이 관례 위에서 성립한다
  (MARA ADR-018).

### Negative

- 문구 하나를 고치는 데도 새 버전이 필요하고, 새 버전은 **검증이 다시 미검증이 된다.**
  실제로 부정확한 문구를 알면서 고치지 않고 미룬 사례가 있다 (README D-055).

## Reversibility

- **Reversible:** Partial
- **Rollback:** 관례를 버리는 것은 쉽지만, 과거 산출물과 프롬프트의 연결이 끊긴다.
- **Migration Cost:** Medium

## References

- **Related ADR:** ADR-014(judge 루브릭 버전 전환)
- **Documentation:** `README.md` 결정 로그 D-010·D-023, `extraction/prompts/README.md`
