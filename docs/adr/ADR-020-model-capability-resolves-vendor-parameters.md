# ADR-020: 롤백 경로 — 벤더 파라미터·모델명을 프로바이더가 아니라 모델 능력으로 판정한다

- **Status:** Proposed
- **Date:** 2026-09-21
- **Decision:** `AnthropicClient` 가 모델별 지원 범위 표를 갖고 거부되는 파라미터를 생성 시점에 빼며, 벤더 모델명은 주석이 아니라 `vendor_model` 키로 남긴다
- **Scope:** `extraction/llm.py` · `eval/runner.py` · `config.yaml`
- **Decision Source:** Human

---

## Context

### Problem

ADR-018 은 `anthropic` SDK 를 **롤백 경로로 남긴다**고 적었고, Risks 에 "되돌리기가
너무 쉽다 — `llm.provider` 한 줄이다"라고 적었다. **그 한 줄로는 되돌아가지
않는다.** `llm.provider: anthropic` 으로 바꾼 뒤 세 단계가 만드는 요청을 실제로
찍어 보면 전부 벤더가 거부할 형태다.

파손은 서로 독립인 두 건이다.

1. **파라미터** — vLLM 이전이 세 단계에 `temperature: 0` 을 넣었다. vLLM 은 받지만
   Opus 5 는 400 으로 거부한다(D-033). 지원 범위를 **호출부가 알고 맞춰 넘기는**
   구조였기 때문에, 설정이 vLLM 기준으로 채워지는 순간 그 지식이 어디에도 남지
   않았다.
2. **모델명** — 이전이 세 단계의 `model` 을 `gemma-4-31B-it` 로 **제자리에
   덮어썼고**(ADR-018 Implementation), 벤더 모델명은 `# 이전: claude-opus-5` 라는
   **주석으로만** 남았다. 롤백하면 벤더에 존재하지 않는 모델을 부른다.

2번은 session-04 리뷰의 F1 에도 기록되지 않았다. F1 은 1번만 적었고, 그것만 고치면
3단계 중 1단계만 복구된다.

이 레포가 반복해서 만나는 모양이다 — **안전장치라고 적어 둔 것이 동작하는지 아무도
확인하지 않았다.** ADR-017(게이트가 있었고 통과했는데 확인할 것을 확인하지 않았다),
F11(`paths:` 누락)과 같다. 여기서는 롤백 경로에 테스트가 **0건**이었다.

### Constraints

- **vLLM 기본 경로의 동작이 바뀌면 안 된다.** 이전 직후이고 대조 실행 결과가 이
  설정에 묶여 있다 (ADR-018 Evidence).
- **`eval.judge_model` 이 judge 모델명의 정본이다** — `llm.eval_judge` 로 옮겨 적어
  정본을 둘로 만들지 않는다 (`eval/runner.py: judge_client_from_config`).
- 판정 주체가 모델이므로 프로바이더 단위로 가르면 같은 `anthropic` 안의 모델 차이를
  표현할 수 없다. Opus 5 는 `temperature` 를, Haiku 4.5 는 `output_config.effort` 를
  거부한다 (D-032, D-033, ADR-013).
- 뺀 것을 **조용히** 빼면 안 된다. 파라미터가 사라진 것과 무시된 것은 다르고, 대조
  실행에 "무엇이 달랐나"를 적으려면 목록이 필요하다 (`VENDOR_ONLY_PARAMS` 와 같은 이유).

## Decision

### Selected

- **Technology:** 추가 의존성 없음. 기존 `extraction/llm.py` 안의 모듈 상수와
  `config.yaml` 키 하나.
- **Architecture:** 모델별 지원 범위를 **클라이언트가 소유한다.** 호출부는 설정에
  적힌 값을 그대로 넘기고, 그 모델이 받지 못하는 것은 클라이언트가 뺀다. 벤더 모델명은
  프로바이더 전환 시 살아남아야 하므로 주석이 아니라 **키**로 둔다.
- **Implementation:**
  - `extraction/llm.py` — `MODEL_UNSUPPORTED_PARAMS`(모델명 **접두사** → 거부
    파라미터)와 `unsupported_params()`. 접두사 매칭은 날짜 접미사가 붙는 모델
    (`claude-haiku-4-5-20251001`)을 버전마다 등록하지 않기 위해서다. **표에 없는
    모델은 아무것도 빼지 않는다** — 모르는 모델에 능력을 가정하지 않는다.
  - 드롭은 `AnthropicClient.__init__` 에서 한다. `from_config` 만 덮으면
    `AnthropicClient(...)` 직접 생성 경로가 그대로 남는다. "실리는가"의 판정은
    `_request_kwargs` 와 같은 기준을 쓴다 (`temperature` 는 `is not None`,
    `effort`/`thinking` 은 진리값 — `temperature: 0` 이 falsy 라 한 기준으로 묶을 수 없다).
  - 뺀 이름은 `self.dropped_params` 로 남기고 **stderr 로 알린다.**
  - `AnthropicClient.from_config` — `section.get("vendor_model") or section.get("model", ...)`.
  - `config.yaml` — `llm.relevance_gate.vendor_model`,
    `llm.extraction.vendor_model`, `eval.judge_vendor_model`. judge 쪽은
    `eval/runner.py: judge_client_from_config` 가 `eval:` 블록에서 읽어 단계 섹션으로
    합성한다 — `judge_model` 이 계속 정본이고 `judge_vendor_model` 은 그 짝이다.
  - `tests/test_rollback_path.py` — 레포의 **실제** `config.yaml` 을 읽어 `provider`
    만 뒤집고 세 단계의 요청 형태를 고정한다 (12건).

## Rationale

1. **판정 기준이 실제 거부 주체와 일치한다.** 400 을 내는 것은 프로바이더가 아니라
   모델이다. `provider == "anthropic"` 으로 가르면 Opus 5 와 Haiku 4.5 의 차이를
   표현할 자리가 없고, ADR-013 이 이미 기록한 "단계마다 모델이 다르면 지원 범위도
   다르다"를 코드가 다시 잃는다.
2. **주석은 롤백 시 아무 일도 하지 않는다.** `# 이전: claude-opus-5` 는 사람이 읽고
   손으로 옮겨 적어야 동작하는 장치이고, 그 손이 없으면 조용히 틀린 모델을 부른다.
   키로 올리면 롤백이 설정을 **읽는** 것만으로 성립한다.
3. **설정을 손으로 고치는 단계가 사라진다.** 이전에는 되돌릴 때 `provider` 외에
   `temperature` 3줄과 모델명 3곳을 같이 고쳐야 했다. 그 목록이 어디에도 없었으므로
   "한 줄이다"라는 서술과 실제 절차가 갈라져 있었다.
4. **회귀가 테스트로 잡힌다.** 이 두 파손은 **런타임에만** 드러나고, 롤백은 드물게
   일어나므로 다음 롤백까지 몇 세션이 지난다. 실제 `config.yaml` 을 읽는 테스트라야
   "우리 설정으로 롤백이 되는가"를 본다 — 흉내 낸 픽스처로는 못 본다.

## Evidence

- **Experiment (수정 전, `provider: anthropic`):** 세 단계가 만든 요청
  - `relevance_gate` → `model: gemma-4-31B-it`, `extra_body: {temperature: 0}`
  - `extraction` → `model: gemma-4-31B-it`, `effort`, `thinking`, `extra_body: {temperature: 0}`
  - `eval_judge` → `model: claude-opus-5`, `extra_body: {temperature: 0}`
  → **3/3 단계가 벤더가 거부할 요청.** F1 이 기록한 것은 이 중 `eval_judge` 1건.
- **Experiment (수정 후, 같은 명령):**
  - `relevance_gate` → `claude-haiku-4-5-20251001`, `temperature: 0` 유지(Haiku 는 수용)
  - `extraction` → `claude-opus-5`, `dropped_params=('temperature',)`
  - `eval_judge` → `claude-opus-5`, `dropped_params=('temperature',)`
  → **거부 파라미터 0건.** `provider: vllm` 기본 경로는 세 단계 전부 `gemma-4-31B-it` 로 불변.
- **Experiment (변이 검사):** 새 테스트가 실제로 결함을 잡는지 확인했다.
  `vendor_model` 해석을 제거하면 3건 실패, 파라미터 드롭을 제거하면 4건 실패.
  둘 다 복원 후 12/12 통과.
- **Production Data:** 전체 스위트 462 → **474 passed**(신규 12건), 기존 실패 0.
  게이트 검사 46 통과 0 실패 — 변동 없음.
- **Cost:** LLM 호출 0건. 외부 벤더 호출 0건. 새 의존성 0개.

## Alternatives

### F1 만 고친다 — 모델 능력 기반 파라미터 드롭까지

- **Pros:** session-04 핸드오프에 적힌 범위 그대로. 변경 표면이 가장 작다.
- **Cons:** 3단계 중 `eval_judge` 1단계만 복구된다. 나머지 둘은 모델명이 gemma 라
  `unsupported_params()` 가 빈 튜플을 돌려주고 — **의도대로 아무것도 빼지 않는데**
  요청은 여전히 벤더에 없는 모델을 부른다.
- **Rejected because:** 고친 뒤에도 "롤백 경로를 복구했다"가 참이 되지 않는다.
  **동작하지 않는 안전장치를 동작한다고 적는 것이 이 레포가 F1 로 겪은 문제
  자체다.** 같은 상태를 남기면서 기록만 갱신하는 셈이 된다.

### 코드는 F1 범위만 고치고 ADR-018 문구를 사실에 맞춘다

- **Pros:** 문서와 런타임의 불일치가 즉시 없어진다. 코드 변경 표면이 최소다.
- **Cons:** 롤백은 여전히 2/3 단계에서 깨진 채다. "한 줄이 아니라 일곱 군데"라고
  정확히 적어도, 그 일곱 군데를 손으로 고치는 절차는 다음 롤백 때 똑같이 빠뜨릴 수 있다.
- **Rejected because:** 고칠 수 있는 결함을 문서로 감싸는 선택이다. 절차를 사람 손에
  남기는 것은 `# 이전:` 주석이 이미 실패한 방식이다.

### 드롭을 `from_config` 에서만 한다

- **Pros:** 설정에서 오는 경로만 다루므로 영향 범위가 좁고, 명시적으로 파라미터를
  넘긴 호출자의 의도를 덮지 않는다.
- **Cons:** `AnthropicClient(model=..., temperature=0)` 직접 생성 경로가 그대로
  400 을 낸다. 스크립트·테스트·probe 가 쓰는 경로가 그쪽이다.
- **Rejected because:** 지원 범위를 아는 주체가 클라이언트인데 판정만 팩토리에 두면
  같은 지식이 두 자리에 필요해진다. `__init__` 이 모든 생성 경로가 지나는 유일한
  지점이고, ADR-018 이 프로바이더 선택을 한 곳에 모은 것과 같은 이유다.

### `model` 을 프로바이더별 맵으로 바꾼다 (`model: {vllm: ..., anthropic: ...}`)

- **Pros:** 두 이름이 한 키 아래 대칭으로 놓인다. 프로바이더가 늘어도 형태가 같다.
- **Cons:** `model` 의 타입이 문자열에서 매핑으로 바뀌어 `section.get("model")` 을
  읽는 모든 소비자(`client_from_config` 의 vLLM 분기, `judge_client_from_config`,
  보존소 메타데이터)가 함께 바뀐다. `eval.judge_model` 도 같은 변경을 받는다.
- **Rejected because:** 롤백 하나를 고치려고 출력 계약에 닿는 필드의 타입을 바꾼다.
  `vendor_model` 형제 키는 기존 소비자를 하나도 건드리지 않고 같은 결과를 낸다.

## Consequences

### Positive

- **`llm.provider` 한 줄 롤백이 실제로 성립한다.** ADR-018 이 적어 둔 안전장치가
  적힌 대로 동작하는 상태가 됐다.
- **벤더 모델명이 설정에 남았다.** 이전이 덮어쓴 정보가 주석이 아니라 키로 복원돼,
  "무엇으로 되돌아가는가"를 `config.yaml` 만 보고 알 수 있다.
- **모델별 지원 범위가 한 자리에 모였다.** 지금까지 주석 3곳과 설정값 배치로 흩어져
  있던 D-032/D-033 의 지식이 `MODEL_UNSUPPORTED_PARAMS` 하나가 됐다. 모델을 추가할 때
  볼 곳이 하나다.
- **롤백 경로에 테스트가 생겼다.** 0건 → 12건. 실제 `config.yaml` 을 읽으므로 설정을
  고치다 롤백을 깨면 CI 에서 잡힌다.

### Negative

- **`vendor_model` 이 놀고 있는 설정이다.** `provider: vllm` 인 동안 아무 효과가 없어,
  갱신되지 않은 채 낡을 수 있다. 낡은 값은 롤백 시점에야 드러난다.
- **설정 키가 정본과 짝으로 늘었다.** `judge_model`/`judge_vendor_model` 처럼 같이
  고쳐야 하는 짝이 하나 더 생겼고, 이 짝은 `tests/test_vocab_sync.py` 같은 동기화
  검증 대상이 아니다.
- **stderr 출력이 라이브러리 계층에서 나간다.** 이 레포에 로깅 계층이 없어 기존
  관례(`print(..., file=sys.stderr)`)를 따랐지만, 관측 로그로 모이지는 않는다.

### Risks

- 🔴 **롤백을 "쉽다"고 느끼게 만든다.** ADR-018 Risks 의 🔴 항목이 강화됐다 — 깨져
  있던 경로가 동작하게 됐으므로 마찰이 실제로 줄었다. **`llm.provider` 를 되돌리는
  것은 설정 변경이 아니라 MARA 전제의 변경이고 조직 결정이다.** 이 ADR 은 그 판단을
  바꾸지 않는다. 실제 방어선은 계속 `extraction/egress.py` 의 기본 차단과
  `assert_internal_endpoint` 다.
- 🟡 **접두사 매칭이 미래 모델을 잘못 덮을 수 있다.** `claude-opus-5` 는
  `claude-opus-5-1` 도 매칭한다. 지금은 원하는 동작이지만, 후속 모델이
  `temperature` 를 다시 받으면 조용히 빼게 된다 — **실린 것이 사라지는 방향이라
  400 이 나지 않아 증상이 안 보인다.**
- 🟡 **`vendor_model` 값을 실제 호출로 검증한 적이 없다.** 벤더 호출이 0건이므로
  테스트가 보는 것은 "요청 형태가 거부당하지 않을 모양인가"까지다. 그 모델명이
  지금도 벤더에 존재하는지는 롤백 시점에 확인된다.

## Implementation

- [x] `MODEL_UNSUPPORTED_PARAMS` · `unsupported_params()` 추가
- [x] `AnthropicClient.__init__` 드롭 + `dropped_params` + stderr 통지
- [x] `AnthropicClient.from_config` 의 `vendor_model` 해석
- [x] `eval/runner.py` — `judge_vendor_model` 전달
- [x] `config.yaml` — `vendor_model` 3곳, `temperature` 주석 갱신
- [x] 테스트 `tests/test_rollback_path.py` 12건 + 변이 검사
- [ ] 모니터링 — `dropped_params` 를 관측 로그로 올릴지는 미정 (지금은 stderr 만)
- [ ] ADR-018 Amendment — "한 줄이다" 서술과 Risks 갱신

## Reversibility

- **Reversible:** Yes
- **Rollback:** `extraction/llm.py` 의 표·드롭·`vendor_model` 해석, `eval/runner.py`
  한 블록, `config.yaml` 의 키 3개를 되돌린다. 산출물·라벨·측정값에 닿지 않는다 —
  `provider: vllm` 경로의 요청이 바이트 단위로 같기 때문이다.
- **Migration Cost:** Low

> ⚠️ **벤더로 되돌리는 절차 자체는 한 줄이 아니다 (2026-09-21, F7).** `anthropic`
> SDK 가 코어 의존성에서 `[project.optional-dependencies] vendor` 로 내려갔다.
> 되돌릴 때는 **`pip install -e .[vendor]` 를 먼저** 실행하고 그 다음에
> `config.yaml: llm.provider` 를 바꾼다. 순서가 뒤집히면 클라이언트 생성 시점에
> `LLMError` 가 난다 (`extraction/llm.py: _load_vendor_sdk`). 이 ADR 이 정정한
> ADR-018 의 서술("한 줄이면 되돌아간다")과 같은 종류의 낙관이라서, 여기 적는다.
> 절차가 동작하는지는 `tests/test_rollback_path.py` 가 잰다 (D-080).

## References

- **Related ADR:** ADR-013(모델별 파라미터와 게이트 `temperature: 0` — 이 결정이
  코드로 옮기는 지식), ADR-018(롤백 경로를 안전장치로 내세운 결정 — 이 ADR 이 그
  서술을 정정한다), ADR-017(벤더 호출 게이트 — 롤백 경로가 존재하는 한 유효)
- **Documentation:** `docs/handoff/session-04.md` §4 F1 ·
  `tests/test_rollback_path.py` · README 결정 로그 D-067
