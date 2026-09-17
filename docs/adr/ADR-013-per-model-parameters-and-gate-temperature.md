# ADR-013: 모델별 파라미터를 optional 로 두고 관련성 게이트에 `temperature: 0` 을 고정한다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** `effort`/`thinking` 을 모델별로 끌 수 있게 optional 로 두고, 관련성 게이트에 `temperature: 0` 을 고정한다. `temperature` 는 SDK 명명 인자가 없어 `extra_body` 로 실어 보낸다. 추출 단계는 건드리지 않는다.
- **Scope:** ai-news-ontology (LLM 계층 / 설정)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-018**(파라미터 optional), **D-032**(게이트 temperature 0),
> **D-033**(extra_body 경로). D-033 이 스스로 "D-018 의 두 번째 사례"라고 적고 있어
> 셋을 한 결정으로 묶었다.

---

## Context

### Problem

단계별로 모델을 다르게 두자(ADR-006) **"모든 모델이 같은 파라미터를 받는다"는 가정이
깨졌다.** 그리고 게이트 판정이 같은 기사에서 실행마다 흔들렸다.

### Constraints

- `claude-haiku-4-5-20251001` 은 `output_config.effort` 를 400 으로 거부한다.
- `claude-opus-5` 는 `temperature` 를 400(`deprecated for this model`)으로 거부한다.
- SDK 1.x 의 `messages.create`/`parse` 에는 `temperature` **명명 인자가 아예 없다** —
  최신 모델에서 제거된 파라미터이기 때문이다. 구세대 모델은 여전히 받는다.

## Decision

### Selected

- **Technology:** Anthropic SDK 1.x, `extra_body` 통로
- **Architecture:** 단계별 설정 블록(`config.yaml: llm.{relevance_gate,extraction}`)에서
  파라미터를 개별로 켜고 끈다. 값이 `null` 이면 요청에 아예 싣지 않는다.
- **Implementation:** `extraction/llm.py: AnthropicClient._request_kwargs()`.
  게이트에만 `temperature: 0`, 추출 단계는 그대로 둔다.

## Rationale

1. 게이트 판정이 흔들린 원인은 프롬프트가 아니라 **샘플링이었다.** 설정에 `temperature`
   항목이 없었고 **API 기본값(1.0)이 적용되고 있었다.** 고치기 전에 요청에 실제로
   무엇이 실리는지 먼저 확인했다.
2. 게이트는 이분 판정이라 창의성이 필요 없고, 같은 기사가 실행마다 다르게 처리되면
   파이프라인을 신뢰할 수 없다.
3. 추출 단계는 건드리지 않는다 — Opus 5 가 `temperature` 를 거부하기도 하고,
   한 번에 두 곳을 바꾸면 효과를 가를 수 없다.

## Evidence

- **Experiment:** 적용 전 같은 기사(#33001)가 5회 중 4통과 / 1스킵으로 갈렸다.
  적용 후 5회 재실행에서 판정과 근거 텍스트가 **바이트 단위로 동일**했다.

## Alternatives

### 프롬프트에 "애매하면 통과" 문구 보강

- **Pros:** 설정을 안 건드린다.
- **Cons:** 원인이 프롬프트가 아니었다.
- **Rejected because:** 증상이 아니라 원인을 고치는 쪽이 맞다.

### 다수결로 3회 호출

- **Pros:** 모델과 무관하게 흔들림이 줄어든다.
- **Cons:** 게이트 비용이 3배가 된다.
- **Rejected because:** 게이트를 둔 이유가 비용 절감인데 그 이유를 스스로 깎는다.

### SDK 버전 다운그레이드 / 게이트도 Opus 로 통일

- **Pros:** 파라미터 분기가 사라진다.
- **Cons:** 전자는 최신 모델을 못 쓰고, 후자는 비용 절감이 사라진다.
- **Rejected because:** 둘 다 분기 하나를 없애려고 더 큰 것을 내준다.

## Consequences

### Positive

- 게이트가 재현 가능해져 파이프라인 판정을 신뢰할 수 있다.

### Negative

- 모델을 바꿀 때마다 어떤 파라미터를 받는지 확인해야 한다. 이 사실이 설정 파일과
  코드 주석 양쪽에 남아 있어야 한다.
- judge(Opus 5)는 같은 방법으로 고정할 수 없다 — 비결정성이 남았다 (ADR-014).

## Reversibility

- **Reversible:** Yes
- **Rollback:** 설정값을 되돌리면 된다. 다만 게이트 판정의 재현성이 사라진다.
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-003(LLM 계층), ADR-006(단계별 모델), ADR-014(judge 비결정성)
- **Documentation:** `README.md` 결정 로그 D-018·D-032·D-033, `config.yaml`
