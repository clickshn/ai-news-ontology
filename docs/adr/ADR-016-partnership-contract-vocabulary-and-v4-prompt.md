# ADR-016: `발표유형` 에 `Partnership/Contract` 를 추가하고 추출 프롬프트를 v4 로 올린다

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** `발표유형` 에 `Partnership/Contract`(조직 간 거래 관계의 체결·변경·종료)를 추가하고(7개 → 8개), D-021 의 "누가" 규칙을 사건의 주체 기준으로 명확화하며, `역할` 예시를 보강해 `DEFAULT_PROMPT` 를 v4 로 올린다.
- **Scope:** ai-news-ontology (온톨로지 어휘 / 추출 프롬프트)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-056**(어휘 추가), **D-057**("누가" 규칙 명확화),
> **D-058**(`역할` 예시 추가), **D-059**(DEFAULT_PROMPT v4). 어휘 추가 하나가
> 프롬프트 전환까지 끌고 간 한 덩어리라 묶었다.

---

## Context

### Problem

골든셋 후보 #33003 이 `Funding/M&A` 로 밀려났는데 **1차 사건은 인수가 아니라 인수
이후의 모델 공급 계약 종료**였다. 어휘에 "거래 관계가 바뀌었다"를 담을 값이 없었다.
이 빈 칸이 골든셋 확장을 실제로 막고 있었다 — **어휘 결함이 데이터셋 성장의 병목이
된 첫 사례**다.

### Constraints

- `GoldenExpectation.release_type` 은 필수라 "이 필드는 채점 제외" 탈출구가 없다.
  틀린 라벨로 확정하면 **모델과 정답이 같은 값으로 틀린 채 `field_accuracy` 가 1.0 으로
  통과한다** — 골든셋이 막으려던 실패 모드이고 커밋되면 영구 오염이다.
- 어휘 목록은 프롬프트에 하드코딩되지 않고 `schema.py` Enum 에서 **런타임 주입**된다
  (ADR-002). 프롬프트를 그대로 두면 모델은 새 값을 **목록에서는 받지만 언제 고르는지는
  듣지 못한다.**
- GeekNews 같은 애그리게이터에서는 커뮤니티가 링크를 올리지만 사건의 주체는 기업일 수
  있다. D-021 의 "누가" 를 문자 그대로 읽으면 **GeekNews 항목이 전부
  `Community/Discussion` 이 되어 그 소스에서 어휘가 무의미해진다.**

## Decision

### Selected

- **Technology:** `ReleaseType` Enum 값 추가 + 새 프롬프트 버전
- **Architecture:** 가르는 축은 D-021 의 "누가"가 아니라 **"무슨 사건인가"** 다 —
  **소유권이 옮겨가면 `Funding/M&A`, 거래 관계가 바뀌면 `Partnership/Contract`.**
  둘 다 나오면 기존 "1차 사건을 고른다" 규칙이 판정한다. "누가"는 **링크를 올린
  사람이 아니라 사건의 주체**로 읽는다 — 스키마는 그대로 두고 문서·프롬프트에서만
  좁힌다 (ADR-008 의 정책 그대로).
- **Implementation:** `extraction/schema.py` + `config.yaml` 미러 + `schema.md`,
  `extract_ontology.v4.md`. `역할` 예시에 `인수 주체` 추가. `DEFAULT_PROMPT` 를 v4 로.

## Rationale

1. 어휘 결함이 골든셋 확장을 막고 있었다. 값을 추가하는 것은 과거 라벨을 무효화하지
   않으므로 ADR-008 의 허용 범위 안이다.
2. **`Contract/Termination`(종료 전용)을 택하지 않은 이유:** 그러면 "두 회사가 공급
   계약을 체결했다"가 갈 곳이 없어 같은 종류의 빈 칸을 하나 더 만든다.
3. "누가" 규칙 명확화는 **번복이 아니라 적용 범위 명확화**다. 사례가 #33001 1건일 때는
   드러나지 않던 모호함이고, 어휘를 늘리면서 바로 부딪혔다.
4. `역할` 예시 추가는 앞선 "현행 유지" 판단을 **전제가 바뀌어 다시 계산한 결과**다.
   그때 근거는 "`역할` 은 채점되지 않으므로 이것만으로 프롬프트 버전을 올릴 이유가
   없다" 였는데, D-056 으로 v4 를 만들게 되면서 **추가 비용이 0이 됐다.**
5. v3 을 유지하면 값만 늘고 기준이 없는, **어휘 추가 전보다 나쁜 상태**가 된다.
   런타임 주입이 미러 드리프트를 막아 주는 대신 "값"과 "값의 사용 기준"이 서로 다른
   파일에 살게 된다는 성질이 여기서 드러났다.

## Consequences

### Positive

- 골든셋 3번째 항목(#33003)이 확정됐다. 재추출에서 `발표유형` 이
  `Funding/M&A` → `Partnership/Contract`, SpaceX 역할이 `경쟁사` → `인수 주체` 로
  바뀌었고, GeekNews 소스인데도 `Community/Discussion` 으로 밀리지 않아 "누가" 규칙
  명확화가 함께 검증됐다.

### Negative

- 골든셋 2건의 예측은 각각 v2·v3 산출물이라 **같은 기사를 v4 로 재추출하면 결과가
  달라질 수 있고, 그 대조는 아직 하지 않았다.** 대조 없이 추이를 읽으면 변화가
  프롬프트 때문인지 모델 때문인지 구분되지 않는다.

### Risks

- 어휘가 단조 증가한다 (ADR-008 Risks).
- 출력 계약 도입 이후 어휘 추가는 MARA 쪽에서 "통과(로그만)" 로 처리되지만,
  `vocab_version` 해시가 바뀌므로 그쪽 manifest 대조에 변화가 기록된다.

## Reversibility

- **Reversible:** No
- **Rollback:** 값을 되돌리면 `Partnership/Contract` 로 라벨링된 노트와 골든셋 항목이
  갈 곳을 잃는다. 프롬프트 버전은 되돌릴 수 있지만 v4 로 뽑은 산출물은 남는다.
- **Migration Cost:** High

## References

- **Related ADR:** ADR-002(런타임 주입), ADR-004(프롬프트 버저닝), ADR-008(어휘 변경 정책),
  ADR-012(골든셋 확정 게이트)
- **Documentation:** `README.md` 결정 로그 D-053·D-056·D-057·D-058·D-059·D-060,
  `extraction/prompts/README.md`
