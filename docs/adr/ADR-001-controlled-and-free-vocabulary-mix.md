# ADR-001: 온톨로지를 통제어휘와 자유 필드의 혼합으로 정의한다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** 온톨로지 5필드 중 `기술영역`·`발표유형`은 닫힌 통제어휘로, `관련기업`·`관련기존기술`은 열린 자유 필드로 두고 섞어 쓴다.
- **Scope:** ai-news-ontology (온톨로지 스키마)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-001**. 원본 날짜와 근거를 그대로 옮긴 것이며 새로 분석하지 않았다.

---

## Context

### Problem

AI 뉴스·논문을 구조화해 Obsidian 에 쌓으려면 각 항목에 어떤 라벨을 붙일지 정해야
한다. 라벨 체계를 전부 고정할 것인가, 전부 열어 둘 것인가가 첫 갈림길이었다.

### Constraints

- 어휘를 전부 닫으면 현실과 어긋난다 — 예측하지 못한 주제가 들어올 때 담을 칸이 없다.
- 어휘를 전부 열면 태그가 파편화돼 검색이 안 된다. Obsidian 그래프와 Dataview 쿼리는
  같은 개념이 여러 표기로 갈리면 값을 잃는다.

## Decision

### Selected

- **Technology:** pydantic 모델 + `StrEnum` 통제어휘
- **Architecture:** 필드마다 성질에 맞는 어휘 정책을 따로 준다. 검색 필터로 쓸 축
  (`기술영역` 14개, `발표유형` 8개)은 닫고, 열거할 수 없는 축(`관련기업`,
  `관련기존기술`)은 열어 둔다.
- **Implementation:** `extraction/schema.py` 의 `TechDomain` / `ReleaseType` Enum,
  자유 필드는 `list[str]` 과 `CompanyRef`.

## Rationale

1. **전부 닫으면 현실과 어긋나고, 전부 열면 태그가 파편화돼 검색이 안 된다.** 두
   실패 모드가 반대 방향이라 한쪽 극단은 어느 쪽이든 손해다.
2. 필드마다 카디널리티가 다르다. 기업명과 기존기술은 목록을 미리 셀 수 없고, 기술영역과
   발표유형은 셀 수 있다.

## Alternatives

### 전부 자유 태그

- **Pros:** 어떤 주제가 들어와도 담긴다. 어휘 유지 비용이 없다.
- **Cons:** 같은 개념이 여러 표기로 갈려 검색·필터가 무의미해진다.
- **Rejected because:** 태그가 파편화되면 구조화의 목적 자체가 사라진다.

### 전부 고정 어휘

- **Pros:** 값이 깨끗하고 필터가 정확하다.
- **Cons:** 기업명·기존기술처럼 열거 불가능한 축까지 고정하면 현실과 어긋난다.
- **Rejected because:** 담을 칸이 없는 항목이 계속 생긴다.

## Consequences

### Positive

- 검색 필터로 쓸 축은 값이 깨끗하고, 예측 불가능한 축은 막히지 않는다.

### Negative

- 두 정책이 한 스키마 안에 공존해 필드마다 다루는 방법이 다르다. 채점 방식도 필드별로
  갈라진다 (ADR-015).

### Risks

- 닫힌 어휘에 빈 칸이 생기면 항목이 잘못된 값으로 밀려난다. 실제로 두 번 발생했다
  (ADR-008, ADR-016 관련 어휘 추가).

## Reversibility

- **Reversible:** Partial
- **Rollback:** 스키마를 고치는 것 자체는 쉽지만, **이미 쌓인 노트의 라벨이 소급해서
  틀린 것이 된다.** 어휘 정책을 바꾸면 과거 항목 재라벨링이 따라온다.
- **Migration Cost:** High

## References

- **Documentation:** `README.md` 결정 로그 D-001, `schema.md`, `extraction/schema.py`
