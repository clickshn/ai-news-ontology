# ADR-002: 통제어휘의 단일 출처를 `schema.py` Enum 으로 두고 프롬프트에는 런타임 주입한다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** 어휘의 정본은 `extraction/schema.py` 의 Enum 이고 `config.yaml`·`schema.md` 는 사람이 읽는 미러다. 프롬프트에는 목록을 적지 않고 Enum 에서 런타임 주입한다.
- **Scope:** ai-news-ontology (온톨로지 스키마 / 프롬프트)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-002**, **D-012**. 같은 결정("어휘는 한 곳에서만 온다")의
> 두 적용 지점이라 하나로 묶었다. 원본 날짜와 근거는 그대로다.

---

## Context

### Problem

같은 어휘 목록이 코드(`schema.py`), 설정(`config.yaml`), 문서(`schema.md`),
프롬프트(`extraction/prompts/*.md`) 네 곳에 등장할 수 있다. 어디를 정본으로 볼지
정하지 않으면 네 곳이 서로 다른 값을 갖게 된다.

### Constraints

- 세 곳 이상에 같은 어휘가 있는 것은 드리프트 위험이다.
- 그렇다고 `config.yaml` 을 정본으로 두고 런타임 로딩하면 **타입 안전성을 잃는다** —
  pydantic 이 검증할 수 있는 Enum 이 사라진다.
- 프롬프트는 테스트하기 어려운 미러라 **가장 먼저 썩는다.**

## Decision

### Selected

- **Technology:** `enum.StrEnum` (Python 3.11+)
- **Architecture:** `schema.py` Enum 이 정본. `config.yaml` 의 `ontology:` 블록과
  `schema.md` 의 표는 사람이 읽고 리뷰하기 위한 미러이며, 코드가 이들을 읽지 않는다.
- **Implementation:** `tests/test_vocab_sync.py` 가 Enum 과 `config.yaml` 의 일치를
  검증한다. 프롬프트에는 `{{tech_domain_list}}` / `{{release_type_list}}` 자리표시자를
  두고 `extraction/extractor.py: build_variables()` 가 Enum 에서 만들어 채운다.

## Rationale

1. 세 곳에 같은 어휘가 있는 건 드리프트 위험이다. 그렇다고 yaml 을 정본으로 하면
   타입 안전성을 잃는다 — Enum 을 정본으로 두고 나머지를 미러로 격하하는 쪽이
   양쪽 손해를 가장 적게 낸다.
2. 프롬프트에 목록을 하드코딩하면 미러가 **네 곳**이 된다. 프롬프트는 자동 검증
   대상으로 만들기 어려워 가장 먼저 썩는 미러다.
3. 미러 동기화를 사람의 주의력이 아니라 테스트로 강제한다. `schema.md` 의 표는
   자동 검증 대상이 아니므로 그 한계를 명시해 둔다.

## Consequences

### Positive

- 어휘 추가·변경이 한 파일에서 시작하고, 어긋나면 테스트가 깨진다.
- 프롬프트를 고치지 않고도 어휘가 늘어난다.

### Negative

- **"값"과 "값의 사용 기준"이 서로 다른 파일에 살게 된다.** 어휘를 추가해도 모델은
  목록에서 값을 받을 뿐 언제 고르는지는 듣지 못한다. 이 성질은 나중에 실제 문제로
  드러났다 (ADR-016).
- `schema.md` 는 여전히 사람이 챙겨야 하는 미러로 남는다.

### Risks

- 출력 계약 도입 이후 이 어휘는 **MARA 의 정본이기도 하다.** 값 삭제·개명·쪼개짐은
  그쪽 import 실패를 일으킨다 (MARA ADR-018, 계약 §5).

## Reversibility

- **Reversible:** Partial
- **Rollback:** 정본 위치를 옮기는 것은 코드 변경이지만, 어휘 값 자체가 바뀌면
  과거 노트 라벨이 소급해서 틀린 것이 된다 (ADR-008).
- **Migration Cost:** Medium

## References

- **Related ADR:** ADR-001(어휘 구조), ADR-008(어휘 변경 정책)
- **Documentation:** `README.md` 결정 로그 D-002·D-012, `tests/test_vocab_sync.py`,
  `extraction/prompts/README.md`
