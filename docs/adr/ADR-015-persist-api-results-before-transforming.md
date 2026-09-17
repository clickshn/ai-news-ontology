# ADR-015: API 결과는 받은 즉시 원본을 디스크에 쓰고, 변환은 그 뒤에 한다

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** API 응답을 쓰는 스크립트는 응답을 받은 즉시 원본을 파일로 남긴다. 변환·직렬화는 그 뒤에 한다.
- **Scope:** ai-news-ontology (API 를 호출하는 모든 스크립트)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-052**.

---

## Context

### Problem

골든셋 초안을 만들다 임시 스크립트의 속성명을 두 번 틀려
(`PipelineResult.ontology` → 실제는 `.extraction`, `Usage.model_dump` → dataclass 라
없음) **성공한 API 호출 2회를 출력 단계에서 날렸다.** 호출은 이미 끝났고 응답도
메모리에 있었는데 **그것을 파일로 옮기는 코드가 죽어서** 잃은 것이다.

### Constraints

- 비용이 이 프로젝트의 실질적 제약이다.
- 변환 코드는 임시로 짜는 경우가 많아 검증되지 않은 채로 값비싼 결과와 같은 경로에 놓인다.

## Decision

### Selected

- **Technology:** 원본 덤프(`pickle` + `repr`) 후 변환
- **Architecture:** **검증되지 않은 변환 코드를 값비싼 결과와 같은 트랜잭션에 두지
  않는다.** 비용이 발생한 시점과 그것을 디스크에 고정하는 시점 사이에 다른 코드가
  끼지 않게 한다.
- **Implementation:** API 를 호출하는 스크립트 전반. `write_scores` 에 이미 같은
  원리가 있었다.

## Evidence

- **Cost:** 성공한 호출 2회를 출력 단계 오류로 잃었다 (≈ **$0.125**).

## Rationale

1. 이건 스크립트 품질 문제가 아니라 **설계 문제**다. 같은 실수는 또 나온다.
2. 호출이 끝난 순간 비용은 이미 발생했다. 그 뒤의 코드가 죽어서 결과를 잃는 것은
   막을 수 있는 종류의 손실이다.

## Alternatives

### 스크립트를 더 조심해서 쓴다

- **Pros:** 코드가 안 늘어난다.
- **Cons:** 같은 실수가 반복된다.
- **Rejected because:** 사람의 주의력에 비용을 거는 구조다.

## Consequences

### Positive

- 출력 계약의 §12.3(추출 결과 원본 보존)이 이 원칙의 확장이다. `export/store.py` 가
  형식 결함을 **LLM 재호출 없이** 고칠 수 있게 만든다 (MARA ADR-018).

### Negative

- 프롬프트와 LLM 응답 본문이 평문으로 디스크에 남는다. `docs/governance.md` 의
  로컬 산출물 (가) 기준을 따른다.

## Reversibility

- **Reversible:** Yes
- **Rollback:** 덤프를 빼면 된다. 되돌리면 같은 손실 위험이 돌아온다.
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-012(eval 결과 저장)
- **Documentation:** `README.md` 결정 로그 D-019·D-044·D-052, `export/README.md`,
  `docs/governance.md`("로컬 산출물 취급")
