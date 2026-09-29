# ADR-021: 완결성 점수를 judge 의 요소 판정으로 코드가 계산한다 (루브릭 v6)

- **Status:** Proposed
- **Date:** 2026-09-29
- **Decision:** `summary_quality.v6.md` 부터 judge 는 완결성 요소별 판정(담김/이름만/없음)만 한 줄로 내고, 슬롯 판정과 완결성 점수는 코드가 계산해 기록한다
- **Scope:** `eval/judge_prompts/summary_quality.v6.md` · `eval/completeness.py` · `eval/runner.py` · `eval/schema.py` · `eval/analysis.py`
- **Decision Source:** Human

---

## Context

### Problem

v5 루브릭(D-094)은 judge 에게 세 단계를 한 번에 맡겼다 — 요소 판정, 요소 판정에서
**기계적으로** 나오는 슬롯 판정, 슬롯 판정에 고정 표를 적용한 점수. 뒤의 두 단계는
판단이 아니라 계산인데도 judge 가 했고, 거기서 규칙을 어겼다.

- #33001 ②: 요소 `이름만` → 슬롯 `부분`(규칙상 미충족). 그 틀린 슬롯 판정에 표를
  적용해 3점. 자기 요소 판정대로면 2점.
- S3Gym ④: 요소 `없음 / 이름만` → 슬롯 `부분`(규칙상 미충족). 점수는 요소대로 3점.

둘 다 **10/10 같은 모양**이라 흔들림이 아니라 체계적 이탈이다. 사전 등록 지표
(`score_table_adherence`)는 앞의 것을 통과로, 뒤의 것을 "표 미준수"로 기록했다 —
지표가 원인을 반대로 읽었다 (D-095, D-096).

### Constraints

- **v3 대조군을 교란하지 않는다.** 공유 출력 스키마(`SummaryJudgement`)를 바꾸면 v3 이
  다른 디코딩 제약에서 돌아 대조군이 아니게 된다 (D-090).
- **한 줄 형식을 유지한다.** 여러 줄 형식 지시가 v4 퇴화의 유력한 원인이었다 (D-093, D-094).
- 요소 판정("그것이 요약에 있는가")은 여전히 judge 의 판단이다. 이 결정이 없애는 것은
  **그 뒤의 계산**이지 판단의 비결정성이 아니다.

## Decision

### Selected

- **Technology:** 새 의존성 없음
- **Architecture:** judge 는 완결성 `rationale` 한 줄(요소 판정만)을 낸다. v6 전용 출력
  모델(`ElementJudgement`)은 완결성에 `score` 칸이 없다. 코드가 라벨(`completeness_slots`)
  대비 요소 판정을 엄격히 읽고(`parse_element_verdicts`), 슬롯 판정(`state_from_elements`)과
  점수(`completeness_from_states`)를 계산한다. 읽히지 않으면 기본값으로 채우지 않고
  **judge 실패로 기록**한다
- **Implementation:** 결과 행에 `completeness_scored_by`(`judge` | `code`)와 계산 경로
  (`completeness_computation`)를 남긴다. 코드 계산 행에서는 슬롯 판정어를 읽는 과정 지표
  (`state_adherence`·`score_table_adherence`·`element_slot_consistency`·`denominator_adherence`)를
  `None` 으로 둔다 — 구조상 성립하는 값을 잰 값처럼 1.0 으로 적지 않는다

## Rationale

1. 슬롯 판정과 점수는 요소 판정의 **결정적 함수**다(루브릭 v5 의 표). 결정적 함수를
   샘플링하는 모델에게 맡길 이유가 없고, 맡긴 결과 체계적 이탈이 났다.
2. 계산을 코드로 옮기면 "요소 판정과 어긋난 슬롯 판정"은 **구조상 불가능**해진다. 그 이탈을
   재던 지표(H5)가 필요 없어지는 것이 아니라 **대상이 사라진다.**
3. judge 에게 점수표를 보이지 않으면, 원하는 점수에 맞춰 요소 판정을 고르는 경로도 줄어든다.
4. 출력 모델을 v6 에만 따로 두면 v3 대조군의 스키마·디코딩 조건은 그대로다.

## Evidence

- **Experiment:** v5 본 실행(2026-09-29, `20260929T000543Z`, 3항목 × 10회, 실패 0). judge 가 쓴
  점수 3 / 3 / 3. **같은 30회의 요소 판정에 코드 규칙을 적용하면 2 / 3 / 3** (호출 0건 재계산).
  `element_slot_consistency` 0.75 / 1.0 / 0.75, `score_table_adherence` 1.0 / 1.0 / 0.0.
  요소 판정 자체는 19요소 중 18개가 에이전트 손계산과 같고, 다른 하나(#33001 ②(a))는 10/10 `이름만`
- **Cost:** 판정 방식 변경 자체는 호출 0건. 검증은 내부 vLLM (`gemma-4-31B-it`), $0

## Alternatives

### v5 유지 (judge 가 슬롯·점수까지 계산)

- **Pros:** 기록되는 점수가 v4·v5 와 같은 뜻이다. 코드 변경이 없다
- **Cons:** 요소 → 슬롯 이탈이 10/10 체계적으로 난다
- **Rejected because:** 사전 등록 H4·H5 미달(D-096). 계산 단계의 오류를 지표로 잡을 수는 있어도 막을 수 없다

### 요소 판정을 구조화 출력 필드로 받는다

- **Pros:** 텍스트 파서가 필요 없고 파싱이 견고하다
- **Cons:** 항목마다 요소 수가 달라 스키마가 동적이 되고, 구조가 늘어나는 만큼 토큰 사이
  구조적 공백 자리가 늘어난다
- **Rejected because:** 공유 스키마를 바꾸면 v3 대조군이 교란되고(D-090), 한 줄 형식을 유지하라는
  퇴화 방지 원칙(D-094)과 어긋난다

## Consequences

### Positive

- 요소 판정과 어긋난 슬롯 판정·점수가 구조상 나올 수 없다
- 기록된 행마다 점수가 어떤 요소 판정에서 나왔는지 경로가 남는다

### Negative

- **기록되는 완결성 점수의 뜻이 바뀐다.** v5 이하: "judge 가 매긴 완결성". v6: "judge 의 요소
  판정으로 코드가 계산한 값". 같은 축 이름이지만 **직접 비교하지 않는다** — v5 3점과 v6 2점을
  "요약이 나빠졌다"로 읽지 않는다. 추이는 v6 기준선부터 다시 센다
- 요소 판정 파싱 실패가 새 실패 갈래가 된다. 기본값으로 채우지 않으므로 실패율로 드러난다

### Risks

- **남는 비결정성은 그대로다.** "그것이 요약에 있는가"는 judge 판단이고, v5 에서 #33001 ②(a) 가
  10/10 `이름만` 으로 기운 것처럼 **체계적으로 한쪽으로 기울 수 있다.** 계산이 결정적이 되면
  그 기울기가 점수에 결정적으로 박혀 흔들림 없이 드러나지 않는다
- 라벨 오류도 같은 방식으로 결정적으로 박힌다 (`rubric-v4-design.md` §4.2)

## Implementation

- [ ] `summary_quality.v6.md` — 요소 판정만, 한 줄, 점수표 없음
- [ ] `eval/completeness.py` — 엄격 파서와 계산 (정본)
- [ ] runner: v6 출력 모델 선택, 코드 계산, `completeness_scored_by` 기록
- [ ] analysis: 코드 계산 행의 과정 지표를 `None` 으로, 파싱 실패 수를 따로 센다
- [ ] 사전 등록 → 소표본 → 별도 승인 → 본 실행 (`docs/eval/preregistration-rubric-v6.md`)

## Reversibility

- **Reversible:** Yes
- **Rollback:** `--judge-prompt` 를 v5 이하로 돌린다. v6 행은 `completeness_scored_by: code` 로
  구분되므로 섞이지 않는다. v6 파일·코드는 보존한다(실행에 쓰인 프롬프트는 수정하지 않는다)
- **Migration Cost:** Medium — 되돌리면 v6 로 쌓은 추이가 다시 끊긴다

## References

- **Related ADR:** ADR-014 (루브릭 버전 전환과 사전 등록 검증)
- **Documentation:** `docs/eval/rubric-v6-design.md` · `docs/eval/run-log-session-13.md` · README D-090, D-094 ~ D-097

## AI/ML Details

- **Model:** `gemma-4-31B-it` (내부 vLLM, judge)
- **Evaluation:** `docs/eval/preregistration-rubric-v6.md`
- **Inference:** 구조화 출력(`response_format=json_schema`), 한 줄 근거

### Evaluation

| Metric | Before | After | Target |
| ------ | -----: | ----: | -----: |
| 요소 → 슬롯 일관성 (v5 실측 → v6 구조) | 0.75 / 1.0 / 0.75 | 구조상 1 | — |
| 완결성 점수 (v5 judge / v5 요소로 코드 계산) | 3 / 3 / 3 | 2 / 3 / 3 | — |
