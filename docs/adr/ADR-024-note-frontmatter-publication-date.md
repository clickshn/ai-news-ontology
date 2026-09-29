# ADR-024: Obsidian 노트 frontmatter 에 발행일(`published_at`, UTC 날짜)을 싣고, 파일명과 `date`(처리일)는 그대로 둔다

- **Status:** Accepted
- **Date:** 2026-09-29
- **Decision:** 새로 쓰는 노트의 frontmatter 에 `published_at` 키를 `date` 바로 뒤에 추가한다. 값은 수집기가 정한 `RawItem.published_at`(UTC 날짜, ADR-023)이고, 피드에 발행일이 없으면 `null` 을 명시한다. 파일명 템플릿(`{date}-{source}-{slug}.md`)과 `date` · `processed_at` 은 바꾸지 않는다
- **Scope:** `obsidian_writer/mapper.py` (`FRONTMATTER_ORDER`, `build_frontmatter`) · Obsidian Vault 노트 형식
- **Decision Source:** Human

---

## Context

### Problem

노트의 시간 정보는 전부 **처리 시각**이다. `date` 는 보존소의 `extracted_at` 날짜이고(D-101), 파일명의 `{date}` 도 같은 값이며, `processed_at` 은 그 시각이다. 발행일은 보존소(`raw_item.published_at`)와 MARA export 에는 있지만 노트에는 없다.

2026-09-29 소표본 실행에서 Microsoft Research 「Offloaded inference for real-world physical AI robotics」는 **09-23 발행**인데 노트에는 `date: '2026-09-29'` 로만 남았다. Vault 에서 "언제 나온 소식인지"를 볼 수 없고, 지식 위키의 시간축이 처리일 기준이 된다. 수용 창(ADR-023)이 최대 14일 전 항목까지 받으므로 이 차이는 매일 실행에서 흔해진다.

### Constraints

- Vault 는 사용자의 실제 데이터이고 충돌 정책은 `skip` 이다 (ADR-010). 기존 노트를 다시 쓰지 않는다.
- Vault 에는 이미 노트 14건이 `{처리일}-{소스}-{슬러그}.md` 형식으로 있다.
- 적재 동일성은 파일명이 아니라 Vault 색인(`source_url` → `doc_id`)이 판정한다 (ADR-022). frontmatter 키 추가는 동일성 판정에 영향이 없다.
- `published_at` 은 UTC 날짜다 (ADR-023). 날짜 단위라 시각·시간대는 남지 않는다.
- frontmatter 키는 영문이다 (ADR-011).

## Decision

### Selected

- **Technology:** 변경 없음 (PyYAML frontmatter)
- **Architecture:** 노트 형식에 키 하나를 더한다. 발행일의 출처는 보존소의 `raw_item.published_at` 하나다 — 노트를 만드는 층에서 발행일을 다시 계산하지 않는다
- **Implementation:** `FRONTMATTER_ORDER` 에서 `date` 뒤에 `published_at` 을 넣고, `build_frontmatter` 가 `context.item.published_at` 을 ISO 날짜 문자열로 싣는다. 값이 없으면 키를 빼지 않고 `null` 로 싣는다 — 이 ADR 이후의 노트에서 "발행일 없음"과 이 ADR 이전의 노트("키 없음")가 구분된다

## Rationale

1. 시간축은 "언제 나온 소식인가"여야 한다. 처리일은 파이프라인 사정(창, 쉬었던 날, 상한으로 미룬 날)에 따라 발행일보다 최대 14일 늦을 수 있다.
2. **파일명은 바꾸지 않는다.** 바꾸면 기존 노트 14건과 새 노트의 파일명 형식이 갈린다. 동일성은 색인이 판정하므로 파일명은 사람이 읽는 이름일 뿐이고, 발행일은 frontmatter 로 충분히 조회된다(Dataview).
3. `date` 도 바꾸지 않는다. 같은 키의 의미가 노트에 따라 달라지면(처리일/발행일) 쿼리가 조용히 틀린다. 새 의미에는 새 키를 쓴다.
4. 값이 없을 때 `null` 을 명시하는 이유: 키가 빠진 노트는 "이 형식 이전"이라는 뜻으로 남겨야, 발행일을 안 준 피드와 옛 노트가 섞이지 않는다.

## Evidence

- **Production Data:** 2026-09-29 소표본 8건 중 Microsoft Research 1건이 발행 09-23 / 노트 `date` 09-29 (6일 차이). 나머지 7건은 발행 09-28~09-29
- **Production Data:** 수용 창 상한 14일 (ADR-023) — 발행일과 처리일의 차이가 날 수 있는 최대치

## Alternatives

### 현행 유지 (처리일만)

- **Pros:** 노트 형식이 바뀌지 않는다
- **Cons:** Vault 에서 발행 시점을 볼 수 없다
- **Rejected because:** 지식 위키의 시간축이 처리일 기준이면 안 된다 (사용자)

### 파일명(`{date}`)을 발행일로

- **Pros:** 파일 목록만으로 시간순이 보인다
- **Cons:** 기존 노트 14건과 파일명 형식이 갈린다
- **Rejected because:** 기존 노트 14건과 형식이 갈린다 (사용자)

## Consequences

### Positive

- Dataview 로 발행일 기준 조회·정렬이 된다 (`WHERE published_at >= date(...)`)
- 처리일과 발행일의 차이가 노트에서 보인다

### Negative

- **적용 이전 노트에는 `published_at` 이 없다** (2026-09-29 기준 14건 — 이 결정의 계기인 MSR 09-23 노트 포함). **소급하지 않는다.** 파이프라인이 Vault 노트를 직접 고치는 것은 `skip` 정책(ADR-010) 밖이고 "Vault 는 사용자 실데이터"라는 원칙을 깬다 (사용자 결정)
- 값이 UTC 날짜라, 한국 매체 기사 중 KST 00:00~08:59 발행분은 현지 날짜보다 하루 이르게 보인다 (예: AI타임스 `Tue, 29 Sep 2026 07:27:02 +0900` → `2026-09-28`)

### Risks

- 발행일을 주지 않거나 틀리게 주는 피드는 노트에 `null` 이나 틀린 날짜를 남긴다. 수집기의 미확정·미래 날짜 판정(ADR-023)은 수용에만 쓰이고 노트 값을 고치지 않는다

## Implementation

- [ ] `obsidian_writer/mapper.py`: `FRONTMATTER_ORDER` 에 `published_at`, `build_frontmatter` 에 값
- [ ] 테스트: 값이 있을 때 ISO 날짜, 없을 때 `null` 명시, 키 순서, `date` 는 여전히 처리일
- [ ] 문서: `obsidian_writer/README.md` · README 결정 로그

## Reversibility

- **Reversible:** Yes
- **Rollback:** `FRONTMATTER_ORDER` 에서 키를 빼면 새 노트에 실리지 않는다. 이미 쓴 노트의 키는 남는다 (Vault 는 `skip`)
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-023 (`published_at` = UTC 날짜), ADR-022 (Vault 색인으로 동일성 판정), ADR-011 (영문 frontmatter 키), ADR-010 (충돌 정책 `skip`)
- **Documentation:** README 결정 로그 D-108, `docs/handoff/session-16.md` §7
