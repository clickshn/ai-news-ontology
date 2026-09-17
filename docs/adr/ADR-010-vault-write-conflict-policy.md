# ADR-010: 같은 기사 노트가 이미 있으면 기본 동작은 `skip` 이고, 슬러그 충돌은 별개로 다룬다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** 동일성은 frontmatter `source_url` 로 판정하고, 이미 있으면 기본적으로 건너뛴다. 덮어쓰기는 `config.yaml` 에서 명시적으로만 켠다. 제목이 비슷해 슬러그만 겹치는 경우는 별개 노트이므로 `-2`, `-3` 을 붙인다.
- **Scope:** ai-news-ontology (obsidian_writer)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-028**.

---

## Context

### Problem

파이프라인을 다시 돌리면 같은 기사가 다시 나온다. 기존 노트를 어떻게 다룰지 정해야 했다.

### Constraints

- **Vault 는 사용자의 실제 데이터다.** Obsidian 에서 손으로 고쳤을 수 있고, 재실행이
  그 편집을 지우면 안 된다.
- 제목이 비슷하면 파일명 슬러그가 겹치는데, 그건 **다른 기사**다.

## Decision

### Selected

- **Technology:** frontmatter `source_url` 비교 + 원자적 쓰기
- **Architecture:** 두 상황을 **다른 정책으로** 다룬다. 같은 기사(`source_url` 일치)는
  `skip`, 슬러그만 겹치는 다른 기사는 `-2`, `-3` 접미사.
- **Implementation:** `obsidian_writer/writer.py: resolve_path()`,
  `config.yaml: output.on_conflict`(`skip` | `overwrite` | `version`).

## Rationale

1. 재실행이 사용자의 편집을 지우면 안 된다. 파괴적 동작은 기본값이 될 수 없다.
2. 두 상황을 같은 정책으로 다루면 **서로 다른 기사가 서로를 덮어쓴다.**

## Alternatives

### 항상 덮어쓰기

- **Pros:** 최신 추출 결과가 항상 반영된다.
- **Cons:** 사용자가 손으로 고친 내용이 사라진다.
- **Rejected because:** Vault 는 사용자의 데이터이고 복구 경로가 없다.

### 슬러그 충돌도 skip

- **Pros:** 파일이 늘지 않는다.
- **Cons:** 제목이 비슷한 **다른 기사**가 조용히 누락된다.
- **Rejected because:** 동일성 판정을 파일명에 맡기는 셈이 된다.

## Consequences

### Positive

- 재실행이 안전해져 파이프라인을 자주 돌릴 수 있다.

### Negative

- 추출을 개선해도 기존 노트는 갱신되지 않는다. 갱신하려면 설정을 바꾸고 명시적으로
  돌려야 한다.
- 동일성 판정이 `source_url` 한 줄 비교라 YAML 전체를 파싱하지 않는다 — 사용자가 손으로
  고쳐 파싱이 깨지는 상황을 피하기 위한 것이지만, 그만큼 판정이 단순하다.

## Reversibility

- **Reversible:** Yes
- **Rollback:** `on_conflict` 설정값을 바꾸면 된다.
- **Migration Cost:** Low

## References

- **Documentation:** `README.md` 결정 로그 D-028, `obsidian_writer/README.md`
