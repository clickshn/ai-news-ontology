# ADR-006: 정밀 추출 앞에 저비용 관련성 게이트를 두고, 게이트에 분류는 시키지 않는다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** `llm:` 설정을 `relevance_gate`(Haiku 4.5) / `extraction`(Opus 5)으로 나누고, 앞단 게이트는 "분석할 가치가 있는가"만 이분 판정한다. 수집 단계에서는 필터링하지 않고 전량 수집한다.
- **Scope:** ai-news-ontology (extraction 파이프라인 / 비용 구조)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-015**, **D-017**. "걸러내는 일을 어디서 할 것인가"라는
> 한 결정의 두 면(게이트를 둔다 / 수집에서는 거르지 않는다)이라 하나로 묶었다.

---

## Context

### Problem

GeekNews 는 AI 전용 소스가 아니다. 최근 50건 중 약 60%만 AI 관련이고(ADR-005),
나머지를 Opus 5 · effort high 로 넘기면 **버려질 결과에 비용을 쓴다.**

### Constraints

- 제목에 AI 관련성이 드러나지 않는 글이 많아 수집 단계 키워드 필터는 놓치는 게 많다.
- GeekNews 는 개발자 커뮤니티에서 실제로 많이 참고하는 소스라 소스 자체를 빼는 것도
  손해다.
- 온톨로지가 AI 어휘만 갖고 있어 비-AI 항목은 담을 칸이 없다.

## Decision

### Selected

- **Technology:** `claude-haiku-4-5-20251001`(게이트) / `claude-opus-5`(추출)
- **Architecture:** 게이트는 `RelevanceGate` 모델만 반환한다 — 온톨로지 필드를 하나도
  포함하지 않는다. 게이트가 `false` 를 내면 **정밀 추출 클라이언트를 호출하지 않는다.**
- **Implementation:** `extraction/extractor.py: process_item()`,
  `config.yaml: llm.{relevance_gate,extraction}`. 호출되지 않는다는 사실을 테스트로
  고정한다 (`tests/test_relevance_gate.py`) — 이 구조가 비용 절감의 전부이기 때문이다.

## Rationale

1. 게이트는 "분석할 가치가 있는가" 이분 판정이라 저비용 모델로 충분하다.
2. **게이트에 분류까지 시키지 않는다.** 저비용 모델의 분류가 정밀 추출 결과와 섞이면
   어느 쪽 품질을 재는지 알 수 없어진다. 비용 최적화가 측정 가능성을 깨면 안 된다.
3. 판정은 본문을 읽을 수 있는 게이트로 미룬다. 수집 단계 키워드 필터는 제목만 보고
   판단하게 되어 놓치는 게 많다.
4. 게이트는 **재현율을 우선한다**("애매하면 통과") — 잘못 버린 기사는 되돌아오지
   않는다. 기업명 정규화가 정밀도를 우선하는 것과 정반대 방향이다 (ADR-009).

## Evidence

- **Cost:** 게이트 도입으로 100건당 약 $4.29 → **$2.92** (약 32% 절감).

## Alternatives

### 수집 단계에서 키워드로 필터링

- **Pros:** LLM 호출이 아예 없다.
- **Cons:** 제목에 관련성이 드러나지 않는 글을 놓친다.
- **Rejected because:** 본문을 못 보는 위치에서 판정하게 된다.

### 전량 정밀 추출

- **Pros:** 게이트 오탐이 없다.
- **Cons:** 버려질 결과에 Opus 5 비용을 쓴다.
- **Rejected because:** 비용이 이 프로젝트의 실질적 제약이다.

### 게이트가 분류까지 수행

- **Pros:** 호출 1회로 끝난다.
- **Cons:** 두 모델의 출력이 섞여 어느 쪽 품질을 재는지 알 수 없다.
- **Rejected because:** 측정 가능성을 비용과 바꾸는 거래다.

### GeekNews 소스 제외

- **Pros:** 비-AI 항목 문제가 사라진다.
- **Cons:** 본문 커버리지와 한국어 유입을 잃는다.
- **Rejected because:** 소스의 값이 필터링 비용보다 크다.

## Consequences

### Positive

- 스킵된 항목도 `SkipRecord` 로 남아 게이트 오탐을 나중에 검증할 수 있다
  (README D-016).

### Negative

- 파이프라인에 모델이 둘이 되어 "모든 모델이 같은 파라미터를 받는다"는 가정이 깨졌다
  (ADR-013).

### Risks

- 게이트 판정이 실행마다 흔들리면 파이프라인을 신뢰할 수 없다. 실제로 발생했고
  원인은 프롬프트가 아니라 샘플링이었다 (ADR-013).

## Reversibility

- **Reversible:** Yes
- **Rollback:** `config.yaml` 에서 단계를 합치면 전량 정밀 추출로 돌아간다. 이미 만든
  노트는 영향받지 않는다.
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-005(소스 구성), ADR-009(정규화의 반대 방향), ADR-013(파라미터)
- **Documentation:** `README.md` 결정 로그 D-015·D-016·D-017, `extraction/README.md`
