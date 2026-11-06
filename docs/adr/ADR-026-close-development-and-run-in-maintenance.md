# ADR-026: 개발을 닫고 운영 모드로 둔다 — 매일 아침 자동 수집만 유지하고, 미결 항목은 다시 열 조건과 함께 남긴다

- **Status:** Accepted
- **Date:** 2026-10-19
- **Decision:** 이 레포는 개발 종료 · 운영 중(maintenance) 상태다. 새 기능은 넣지 않고 매일 08:10 자동 실행(`python -m pipeline scheduled`, ADR-025)만 유지한다. 사람의 일은 14일마다 상시 승인 갱신과 장애 시 재실행 둘이다. 열려 있던 항목은 README "하지 않기로 한 것 / 조건이 갖춰지면 할 것" 이 정본이며, 각 항목의 다시 열 조건이 갖춰질 때만 개발을 재개한다
- **Scope:** ai-news-ontology 레포 전체 (운영 절차 `docs/operations.md`, 상태 표기 `README.md`)
- **Decision Source:** Human

---

## Context

### Problem

판단 자체는 2026-10-16 의 D-124 에서 내려졌고 README "현재 상태" 와 `docs/operations.md` 로 이미 시행 중이다. 이 ADR 은 그 상태를 **레포의 프로젝트 상태 기록**으로 남긴다 — 무엇을 목표로 했고 어디까지 왔으며, 무엇이 판단해서 멈춘 것이고 무엇이 남은 문제인지를 다음에 여는 사람이 한 문서에서 가를 수 있게 하려는 것이다. 미결을 "남은 일"로만 두면 그 구분이 사라진다 (D-124).

**목표.** AI 뉴스·논문을 자동 수집해 온톨로지 스키마로 구조화하고, LLM-judge eval 로 요약 품질을 검증한 뒤 Obsidian Vault 에 적재한다 (README "왜 만드는가"). 실제로 붙잡은 질문은 **LLM 출력의 품질을 측정 가능한 것으로 만드는 방법**이었고, 그래서 결정이 파이프라인보다 eval 쪽에 몰려 있다 (D-039 ~ D-054).

**끝낸 이유 (D-124).** 남은 항목 대부분이 운영으로는 풀리지 않는다. 골든셋 확장 · 영향도 v5 · 루브릭 v6 은 사람의 라벨링이 선행해야 하고, MARA 계약 묶음은 상대 레포가 마무리돼 한쪽만으로 정할 수 없다. 자동 실행은 2주 넘게 사람 손 없이 돌았고, 유일한 장애(10/12)는 사람이 복구했으며 그 경보 경로도 D-123 으로 고쳤다.

### Constraints

- 승인 게이트 6항목과 상시 승인 대조(ADR-017 · ADR-025)는 운영 모드에서도 그대로다. 상시 승인은 14일 뒤 만료된다.
- 작업 스케줄러가 **메인 체크아웃**을 실행한다. 개발을 재개하면 코드는 별도 worktree 에서 바꾸고, `GUARD_FILES` · config 상한을 바꾸면 재승인이 필요하다 (`docs/operations.md` §3).
- 공개 RSS·Atom 피드 자료만 넣는다는 전제가 로컬 산출물 취급 기준 전체를 받친다 (governance "로컬 산출물 취급").

## Decision

### Selected

- **Architecture:** 운영 중인 것은 **매일 08:10 아침 수집 하나**다 — Windows 작업 스케줄러 → `python -m pipeline scheduled` → 관련성 게이트 · 추출 · Vault 적재. 세 LLM 단계는 전부 내부 vLLM(`VLLM_BASE`, `gemma-4-31B-it`)을 가리키고 벤더 호출 지점은 0개다 (ADR-018). 활성 소스 5개, 은퇴 4개 (README "현재 상태")
- **Implementation:** 사람의 일 ① 14일마다 `python -m pipeline approve-schedule`(대화형, 노트 3건 원문 대조) ② 장애 경보가 오면 원인 확인 후 재실행. 절차·경보 12종·종료 코드는 `docs/operations.md`

## Rationale

1. 남은 항목이 **사람의 라벨링이나 상대 레포의 재개**를 선행 조건으로 갖는다. 이 조건 없이 개발을 이어 가면(예: 골든셋 3건인 채로 영향도 v5) 사전 등록할 기준이 없어 결과를 해석할 수 없다 (D-124, session-22 §5).
2. 파이프라인은 사람 손 없이 돌 수 있는 상태가 확인됐다 — 2026-10-01 부터 정시 실행, 유일한 장애는 경보 → 사람 재실행으로 복구됐다 (D-122).
3. 쌓이는 노트가 이 레포의 실사용 가치라 자동 실행은 멈추지 않는다 (D-124).

## Evidence

- **Production Data:**
  - Vault 노트 **565건** (2026-10-16), 정시 실행 하루 적재 **15~51건** (10/13~10/16) — README "현재 상태"
  - 벤더 호출 지점 **5개 → 0개**. vLLM 재추출 30건에서 통제어휘 이탈률 **0.00%**(86개 중 0), structured output 실패율 **0.00%**(30건 중 0) — ADR-018 Evidence
  - 10/12 장애 재계산: 예전 시계 "여유 34.1시간 · 경고"(토스트 없음, 7시간 만에 18건 손실) → 새 시계 **−56.0시간 · 심각** — D-122 · D-123
  - 테스트 **971 passed** (session-22 handoff)
- **Experiment:**
  - 루브릭 v3 효과: 완결성 v3 **8/8 회 2점** · v2 **5/5 회 3점**, 사람 1점 (사전 등록 기준 판정) — README "숫자로 확인되는 것", D-048
  - judge 흔들림: 15회 중 완결성·충실성 변동 **0회**, 간결성 8회 중 2회 — D-049
  - 사람-judge 일치: 골든셋 3건에서 완결성 상대 순위 완전 일치 — README, D-060
  - 게이트 재현성: `temperature: 0` 고정 후 **5/5 바이트 동일** — D-032
- **Cost:** 관련성 게이트 비용 절감 **약 32%**(100건 기준 $4.29 → $2.92), 손익분기 스킵률 8.1% · 관측 40% — `observability/README.md` (D-015). 벤더 모델 시절의 실측이다

## Alternatives

### 개발 계속 (영향도 v5 트랙 착수)

- **Pros:** 영향도 2/3 경계(3 이 64%) 같은 알려진 품질 문제를 바로 다룬다
- **Cons:** 골든셋이 3건이라 바뀐 이유를 잴 기준이 없다
- **Rejected because:** 골든셋 없이 시작하면 사전 등록할 기준이 없다 (D-124)
- **Recheck if:** 사람이 경계 사례를 라벨링해 골든셋이 늘 때 · 골든셋에 영향도 라벨이 쌓였을 때 · MARA 계약의 어느 한쪽이 개발을 재개할 때(계약 문서부터) · 갱신 표본 대조에서 arXiv 노트의 근거 없는 채움이 의심될 때 (README "하지 않기로 한 것 / 조건이 갖춰지면 할 것")

### 자동 실행도 중단

- **Pros:** 상시 승인 갱신·장애 대응이라는 사람의 일이 없어진다
- **Cons:** 노트가 더 쌓이지 않는다
- **Rejected because:** 쌓이는 노트가 이 레포의 실사용 가치다 (D-124)

> ⚠️ **정정 (2026-11-06, 아래 Amendment 1).** 상시 승인 만료 후 갱신하지 않고 자동 실행을 중지했다 (D-126).

## Consequences

### Positive

- 무엇이 판단해서 멈춘 것이고 무엇이 미완인지가 README 표 한곳에 다시 열 조건과 함께 있다.
- 운영에 필요한 사람의 일이 두 가지로 고정됐다.

### Negative

남은 알려진 문제다. 미결 6건의 정본은 README 표이고, 여기에는 운영 중에 영향을 주는 것을 함께 적는다.

- **브레이커 경보 → 알림 전달 (D-122 · D-123).** 10/12 브레이커 경보가 심각 등급까지 올라가지 않아 토스트가 나가지 않았다. 원인(여유 시계가 마지막 성공 수집부터 출발)은 D-123 에서 고쳐 10/12 를 재계산하면 심각이 나오지만, **토스트까지 이어지는 경로는 테스트로만 확인했다.** 다음 실기 장애 때 `scheduled.delivery` 에 `toast: ok` 가 남는지 봐야 한다 (`docs/operations.md` §4).
- **현재 상태만 보는 첫 화면.** 회복된 실패와 지나간 공백은 SessionStart 훅·`status.txt` 에 보이지 않는다. 흔적은 `alerts.json` 의 `closed` 와 실행 요약뿐이다 (D-118, session-21).
- **PC 가 꺼진 기간의 손실은 되살릴 수 없다.** AI타임스 피드 깊이는 28~62시간이라 그보다 긴 공백(주말 포함)의 글은 잃는다. 상한으로는 못 막고, 월요일의 `feed_rollover` 경고는 설계대로의 동작이다 (D-115 · D-119).
- **`StartWhenAvailable` 따라잡기는 실기 미확인이다** — 작업 스케줄러 운영 로그가 꺼져 있다 (D-118).
- **품질 지표는 추세가 아니라 관측치다.** 골든셋 확정 3건에서 필드 하나의 레버리지(0.111)가 임계값까지 거리(0.048)보다 크다. `judge_human_gap` 은 무효다 (D-087). 루브릭 v6 은 Proposed 로 남고 기본값은 v3 다 (ADR-021).
- **영향도 2/3 경계** — 14일 분포에서 3 이 64%. 모델이 아니라 정의 문제로 기운다 (D-121).
- **MARA 계약 묶음** (`prompt_sha256` 재현성 · `published_at` 계약 명시 · 보존소 30건) 은 양쪽 정지 상태로 남는다.

### Risks

- `anthropic` SDK 와 `llm.provider` 설정이 남아 있어, 설정 한 줄로 벤더 경로가 되돌아올 수 있다. 막는 것은 `extraction/egress.py` 기본 차단과 상시 승인 대조다 (ADR-017 · ADR-025).
- `GUARD_FILES` 밖에 새 클라이언트 생성 경로가 생기면 상시 승인 대조가 잡지 못한다 (ADR-025).
- 상시 승인 갱신을 놓치면 자동 실행이 LLM 0건으로 멈춘다(종료 코드 4) — 손실이 아니라 정지 쪽으로 실패한다.

## Reversibility

- **Reversible:** Yes
- **Rollback:** 개발을 재개하려면 governance → README "하지 않기로 한 것" → `docs/operations.md` → ADR(018 · 022 · 023 · 025, eval 은 014 · 021) 순으로 읽고, 재개할 항목의 다시 열 조건이 갖춰졌는지부터 본다 (session-22 §5). 운영을 멈추려면 `docs/operations.md` 의 운영 중단 절차를 따른다
- **Migration Cost:** Low

## Amendment 1 — 자동 수집도 멈췄다: "자동 수집만 유지"라는 전제가 더는 성립하지 않는다 (2026-11-06)

- **Status:** Accepted
- **Decision Source:** Human

### Context

**무엇이 바뀌었나.** 이 ADR 은 "매일 08:10 자동 수집만 유지한다"를 전제로 했고, Alternatives 에서
"자동 실행도 중단"을 "쌓이는 노트가 이 레포의 실사용 가치다"로 기각했다. 2026-10-22 상시 승인이
만료된 뒤 갱신하지 않았고, 2026-11-06 Windows 작업 스케줄러 작업 `ai-news-ontology daily` 를
Disabled 로 바꿔 자동 실행을 중지했다 (D-126). 마지막 기사 노트는 2026-10-22 다.

중지 시점 열린 경보는 2건이다 — `preflight_mismatch`(중지 원인), `not_drained`(OpenAI News, 미룬
추출 4건 — 피드에서 밀려나 복구 불가, 재가동 시 쫓지 않는다).

### Decision

- 자동 실행을 중지한다. 작업은 해제(Unregister)가 아니라 **비활성(Disabled)** 으로 두어 등록을 남긴다
- **결정의 본체는 유지된다** — 개발 종료, 새 기능을 넣지 않는다, 열려 있던 항목은 README "하지 않기로 한 것 / 조건이 갖춰지면 할 것"이 정본이고 각 항목의 다시 열 조건이 갖춰질 때만 개발을 재개한다
- 바뀌는 것은 운영 범위뿐이다 — "사람의 일 두 가지(14일 갱신 · 장애 시 재실행)"는 중지 중에는 없고, 재가동하면 다시 생긴다. 재가동 절차는 `docs/operations.md` §7

### Rationale

1. 개발 종료 후 14일 주기 승인 갱신(노트 3건 원문 대조 포함)의 운영 부담이 이 시점의 산출물 가치보다 크다 (사용자)
2. 상시 승인이 설계대로 **손실이 아니라 정지 쪽으로** 실패했다(Risks 3번째 항목) — 만료 뒤 실행은 LLM 0건으로 멈췄고, 벤더 호출이나 근거 없이 쌓인 노트는 없다

### Evidence

- **Production Data:** 2026-10-23 ~ 11-06 사이 정시 실행 **11회 전부 exit 4**(가부 불일치) · LLM 0건 — `alerts.json` `preflight_mismatch` 연속 11회. Vault 노트 **761건**, 마지막 2026-10-22 (11/06 집계)

### Alternatives

#### 승인을 갱신하고 계속 운영 (이 ADR 의 원래 선택)

- **Pros:** 노트가 계속 쌓인다
- **Cons:** 14일마다 사람의 대조 작업이 든다
- **Rejected because:** 그 운영 부담이 이 시점의 산출물 가치보다 크다 (사용자, D-126)

#### 작업 해제 (`register-scheduled-task.ps1 -Unregister`, `docs/operations.md` §6)

- **Pros:** 남은 등록이 없어 실수로 켜질 여지가 없다
- **Cons:** 재가동 때 재등록이 필요하다
- **Rejected because:** 비활성은 `Enable-ScheduledTask` 한 줄로 되돌린다 (D-126)

### Consequences

- **Negative:** 2026-10-22 이후의 글은 수집되지 않는다. 피드 깊이(AI타임스 28~62시간) 밖으로 나간 글은 재가동해도 되살릴 수 없다
- **Negative:** Vault 루트 `_pipeline-status.md` 는 중지 중 갱신되지 않는다 — 중지 당시 손으로 넣은 안내 줄과 프론트매터 `stopped` 속성이 남아 있고, 재가동하면 다음 실행이 둘 다 덮어쓴다
- **Negative:** 위 Negative 의 "브레이커 → 토스트 실기 확인"과 "`StartWhenAvailable` 따라잡기"는 실행이 없으므로 중지 중에는 확인할 기회가 없다
- **Risk:** 재가동 시 `approve-schedule` 은 `full_manual_run()` 자격(현재 config 상한과 같은 상한의 수동 실행)에 기댄다. 함수는 날짜를 보지 않지만 2주 이상 공백이므로 그 전제를 손으로 확인한다 (`docs/operations.md` §7)

### Reversibility

- **Reversible:** Yes
- **Rollback:** `docs/operations.md` §7 — `Enable-ScheduledTask -TaskName "ai-news-ontology daily"` → 자격 확인 → `approve-schedule`(TTY) → 노트 3건 + arXiv 1건 대조
- **Migration Cost:** Low

## Review Trigger

- 개발 계속: 사람이 경계 사례를 라벨링해 골든셋이 늘 때 · 골든셋에 영향도 라벨이 쌓였을 때 · MARA 계약의 어느 한쪽이 개발을 재개할 때 · 갱신 표본 대조에서 arXiv 노트의 근거 없는 채움이 의심될 때
- **Status of this ADR:** **Accepted 유지 — "자동 수집만 유지"라는 운영 전제는 Amendment 1 로 정정됐다.** 자동 실행은 2026-11-06 중지됐다(D-126). 개발 종료와 미결을 "하지 않기로 한 것"으로 두는 결정 본체는 그대로다

## References

- **Related ADR:** ADR-017 · ADR-018 · ADR-021 · ADR-022 · ADR-023 · ADR-025
- **Documentation:** README D-122 · D-123 · D-124 · D-126, "현재 상태", "하지 않기로 한 것 / 조건이 갖춰지면 할 것" · `docs/operations.md` · `docs/handoff/session-21.md` · `docs/handoff/session-22.md`
