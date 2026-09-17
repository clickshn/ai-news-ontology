# ADR-007: `요약` 을 별도 필드로 두고 `영향도 근거` 를 재사용하지 않는다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** `NewsOntology` 에 `요약` 필드를 추가한다. 노트 본문에 `영향도 근거` 를 대신 쓰지 않는다.
- **Scope:** ai-news-ontology (온톨로지 스키마 / Obsidian 출력 / eval)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-027**.

---

## Context

### Problem

노트 본문에 들어갈 텍스트가 필요한데, 이미 있는 `영향도 근거` 를 재사용할 것인가
새 필드를 만들 것인가를 정해야 했다.

### Constraints

- eval 은 `영향도` 점수를 채점한다. 근거가 요약 역할을 겸하면 채점 대상이 흐려진다.

## Decision

### Selected

- **Technology:** pydantic 필드 (`summary`, alias `요약`)
- **Architecture:** 두 필드의 역할을 분리한다 — **요약은 "무슨 일이 있었나"(사실),
  영향도 근거는 "왜 중요한가"(판단)** 다.
- **Implementation:** 스타일을 평서문 `~다`, 2~3문장, 메타 표현(`이 기사는`) 금지로
  고정한다.

## Rationale

1. 근거를 요약 자리에 쓰면 노트를 다시 열었을 때 **무슨 내용이었는지 알 수 없다.**
2. 반대로 요약을 근거에 쓰면 eval 이 영향도 점수를 채점할 수 없다.

## Alternatives

### `impact_rationale` 재사용 (옵션 B)

- **Pros:** 필드가 늘지 않고 출력 토큰도 아낀다.
- **Cons:** 사실과 판단이 한 칸에 섞인다.
- **Rejected because:** 두 필드는 역할이 다르고, 섞으면 노트와 eval 양쪽이 손해다.

## Consequences

### Positive

- 요약이 독립된 채점 대상이 되어 judge 루브릭을 붙일 수 있게 됐다 (ADR-014).

### Negative

- 출력 토큰이 늘어 추출 비용이 올라간다.
- 문장 수 규칙(2~3문장)이 judge 루브릭과 어긋나 있으면 **스키마를 지킨 요약이 감점되는**
  구조가 된다. 실제로 발생했고 루브릭 쪽을 고쳤다 (ADR-014).

## Reversibility

- **Reversible:** Partial
- **Rollback:** 필드를 빼면 이미 쌓인 노트 본문과 골든셋의 채점 대상이 사라진다.
- **Migration Cost:** Medium

## References

- **Related ADR:** ADR-014(judge 루브릭)
- **Documentation:** `README.md` 결정 로그 D-027, `schema.md`
