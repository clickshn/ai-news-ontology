# ADR-009: 기업명 정규화에 퍼지 매칭을 쓰지 않고, 사전을 단일 출처로 강제한다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** 사전(`config.yaml: company_aliases`)에 명시적으로 등록된 표기만 정규화한다. 흡수하는 차이는 대소문자·공백뿐이며, `CompanyRef` validator 가 `canonical`/`resolved` 를 입력값과 무관하게 항상 재계산한다.
- **Scope:** ai-news-ontology (extraction / 엔티티 정규화)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-025**(퍼지 매칭 금지), **D-026**(validator 항상 재계산).
> 사전을 단일 출처로 만드는 한 결정의 두 면이라 묶었다.

---

## Context

### Problem

`관련기업` 은 자유 엔티티라 같은 회사가 `오픈AI`, `Open AI`, `OpenAI` 로 들어온다.
표기 흔들림을 어디까지 흡수할 것인가가 문제였다.

### Constraints

- **오류가 비대칭이다.** 정규화를 놓치면 `정규화성공=false` 로 남고 원문이 보존돼
  나중에 사전을 보강하면 복구된다 — 손실은 "그래프 노드가 하나 더 생김"이다.
  정규화를 잘못하면 서로 다른 기업이 한 노드로 합쳐지고, **합쳐진 뒤에는 원래 무엇이었는지
  알 수 없다.**
- LLM 이 정규화까지 하면 결정적으로 처리 가능한 일을 확률적 모델에 맡기는 셈이다.

## Decision

### Selected

- **Technology:** 정규화 키(대소문자·공백 제거) 기반 정확 매칭. 편집거리·부분문자열 없음.
- **Architecture:** 정규화 **판정**은 스키마 층(항상·결정적), 정규화 실패의 **기록**은
  파이프라인 층(실제 실행에서만) — 두 층을 나눈 이유는 ADR 밖 README D-035 참고.
- **Implementation:** `extraction/normalize.py`. `CompanyRef` validator 가 입력에
  `정규명`·`정규화성공` 이 들어와도 무시하고 사전 결과로 덮어쓴다.
  `Open AI` → `OpenAI` 는 흡수하지만 `OpenAI Korea` 는 별개 엔티티로 남긴다.

## Rationale

1. **미탐은 되돌릴 수 있고 오탐은 되돌릴 수 없다.** 그래서 정밀도를 재현율보다
   우선한다 — 관련성 게이트가 재현율을 우선하는 것(ADR-006)과 **정반대의 이유로**
   정반대 방향을 택한 것이다.
2. 사전이 두 필드의 단일 출처여야 같은 원문 표기가 언제나 같은 대표명으로 떨어진다.
   입력값을 존중하면 LLM 이 보낸 값과 사전 결과가 섞여 **어느 쪽이 적용됐는지 알 수
   없어진다.**
3. 사전에 없는 표기는 추측하지 않고 원문을 승계한 뒤 관측 큐에 남긴다. 사전 보강은
   관측된 빈도를 보고 한다.

## Alternatives

### 편집거리·부분문자열 기반 퍼지 매칭

- **Pros:** 사전 없이도 표기 흔들림을 많이 흡수한다.
- **Cons:** 서로 다른 기업을 한 노드로 합칠 수 있고 되돌릴 수 없다.
- **Rejected because:** 되돌릴 수 없는 오류를 자동화하는 방향이다.

### LLM 에게 정규화를 맡김

- **Pros:** 사전 유지 비용이 없다.
- **Cons:** 결정적으로 처리 가능한 일이 실행마다 달라진다.
- **Rejected because:** 같은 표기가 같은 대표명으로 떨어진다는 보장이 사라진다.

### 값이 없을 때만 채우기

- **Pros:** LLM 이 이미 맞게 채운 경우 계산을 아낀다.
- **Cons:** 사전 결과와 모델 출력이 섞인다.
- **Rejected because:** 단일 출처가 둘이 되면 사전을 고쳐도 결과가 바뀌지 않는 경우가 생긴다.

## Consequences

### Positive

- 사전을 보강하면 과거 미정규화 표기가 복구 가능한 상태로 남아 있다.
- 출력 계약이 이 성질을 그대로 명시한다 — `resolved=false` 롱테일이 항상 존재하고
  **MARA 는 이 값을 보정하지 않는다** (계약 §3.2).

### Negative

- `resolved=false` 항목이 계속 쌓인다. 사전 보강은 사람이 주기적으로 해야 하는 일로 남는다.

## Reversibility

- **Reversible:** Yes
- **Rollback:** 사전에 표기를 추가하면 이후 실행부터 정규화된다. 과거 노트는 원문이
  보존돼 있어 재처리로 복구된다.
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-006(정반대 방향의 정밀도/재현율 선택)
- **Documentation:** `README.md` 결정 로그 D-025·D-026·D-035, `extraction/normalize.py`
