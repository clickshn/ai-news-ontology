# ADR-011: frontmatter 키를 영문으로 쓴다 (D-003 의 frontmatter 부분을 대체)

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** Obsidian frontmatter 키를 영문(`impact_score`, `tech_domain` …)으로 쓴다. 한국어 alias 는 LLM 이 보는 스키마와 로그에서만 쓴다.
- **Scope:** ai-news-ontology (obsidian_writer / 스키마 직렬화)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-029**, 그리고 그것이 부분 번복한 **D-003**. 번복 기록이
> 결정의 일부이므로 한 문서에 함께 옮겼다. **D-003 을 지우지 않는 원칙**(README 결정
> 로그의 규칙)을 이관본에서도 그대로 지킨다.

---

## Context

### Problem

D-003(2026-08-29)은 "코드 필드명은 영어, 한국어는 pydantic alias" 로 정하면서
**"Obsidian frontmatter 는 한국어가 검색에 낫다"** 고 봤다. 실제로 써 보니 그 전제가
틀렸다.

### Constraints

- Dataview 쿼리(`WHERE impact_score > 3`)와 템플릿에서 한글 키는 **매번 따옴표로
  감싸야 한다.**
- 노트에서 사람이 읽는 것은 **값**이지 키가 아니다.

## Decision

### Selected

- **Technology:** pydantic alias 를 쓰되 frontmatter 직렬화에서는 영문 필드명 사용
- **Architecture:** 한국어 alias 의 적용 범위를 좁힌다 — LLM 에게 보내는 스키마와
  로그에만 남기고, 노트 출력에서는 쓰지 않는다.
- **Implementation:** `obsidian_writer/mapper.py: FRONTMATTER_ORDER`

## Rationale

1. Dataview 쿼리와 템플릿에서 한글 키는 매번 따옴표가 필요하다 — 실사용 마찰이 크다.
2. 노트에서 사람이 읽는 것은 값이지 키가 아니다. 키의 가독성을 위해 쿼리 편의를
   내줄 이유가 없다.

## Alternatives

### 한글 키 유지 (D-003 원안)

- **Pros:** frontmatter 를 눈으로 볼 때 읽기 쉽다.
- **Cons:** 쿼리·템플릿에서 매번 따옴표가 필요하다.
- **Rejected because:** 실제로 쓰는 쪽(쿼리)의 마찰이 읽는 쪽의 이득보다 크다.

## Consequences

### Positive

- 출력 계약이 영문 필드명을 쓰는 것과 결이 맞는다 — 기계가 읽는 쪽에서 키가 한글일
  이유가 없다 (MARA 계약 §3.2).

### Negative

- **D-003 이전에 만들어진 노트가 있으면 키가 갈린다.** 번복 시점에 쌓인 노트가 적어
  실제 피해는 없었지만, 구조적으로는 마이그레이션이 필요한 변경이다.

### Risks

- 같은 종류의 "쓰기 전에는 옳아 보이는 결정"이 또 나올 수 있다. D-003 을 지우지 않고
  `부분 번복` 으로 남긴 이유가 이것이다 — 무엇을 잘못 예측했는지가 기록으로 남아야 한다.

## Reversibility

- **Reversible:** Partial
- **Rollback:** 키를 되돌리면 이미 쌓인 노트의 frontmatter 를 전량 다시 써야 하고,
  사용자가 손으로 고친 노트와 충돌한다 (ADR-010).
- **Migration Cost:** High

## References

- **Related ADR:** ADR-010(Vault 쓰기 정책)
- **Documentation:** `README.md` 결정 로그 D-003(부분 번복)·D-029,
  `obsidian_writer/mapper.py`
