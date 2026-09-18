# ADR-017: 벤더 LLM 호출을 기본 차단하고 승인 파일로만 연다 — MARA 게이트의 생산자 측 이식

- **Status:** Accepted
- **Date:** 2026-09-18
- **Decision:** 이 레포의 외부 LLM 벤더 호출을 **기본 차단**으로 바꾸고, 예외는 1회용 승인 파일(`.claude/external-llm-approved`)로만 연다. 집행은 도구 계층(`.claude/hooks/check-external-llm.sh`)과 코드 계층(`extraction/egress.py` + `AnthropicClient.__init__`) 두 곳에서 독립적으로 한다. 승인 게이트 양식에 **"대상 엔드포인트가 내부 vLLM 인가"를 1번 필수 항목**으로 넣는다.
- **Scope:** ai-news-ontology (승인 게이트 / 훅 / LLM 클라이언트 생성 지점)
- **Decision Source:** Human (MARA session-11 §5 의 작업 지시)

> **정본은 MARA 의 ADR-021 이다.** 이 ADR 은 그 결정의 **생산자 측 이식 기록**이고,
> 두 레포의 규칙이 같은 것임을 양쪽에서 확인할 수 있게 하려고 남긴다. 위반 사건
> 자체의 서술·비용·데이터 범위는 ADR-021 에 있고 여기서 반복하지 않는다.

---

## Context

### Problem

**MARA Session 0.5a 에서 계약 검증용 추출이 외부 Anthropic API 로 실행됐고, 그
실행 코드가 이 레포다.** MARA 의 전제는 외부 LLM 벤더 비의존이다. 승인 게이트는
목적·모델명·건수·예상 비용을 물었고 **전부 정확히 답변됐고 승인도 받았다.**
그런데 **"대상 엔드포인트가 어디인가"는 아무도 묻지 않았다.**

> **규칙은 저쪽에 있고 위반은 이쪽에서 났다.**
> MARA 는 session-11 에서 자기 쪽 게이트를 고쳤지만, **MARA 의 훅은 MARA 세션에서만
> 돈다.** 이 레포에는 그 장치가 없었으므로, 이 세션 전까지 **같은 위반이 그대로 다시
> 가능한 상태**였다. ADR-021 이 이것을 자기 Consequences 의 🔴 항목으로 남겨 두고
> 생산자 반영을 차단 항목으로 넘겼다.

### Constraints

- **이 레포는 벤더 의존을 전제로 설계돼 있다.** `config.yaml` 이
  `llm.provider: anthropic` 으로 세 단계를 전부 벤더에 묶어 두고 있고, `anthropic`
  SDK 가 실제 의존성이며 키가 `.env` 에 있다. **MARA 와 제약이 정반대다.**
- 따라서 이 게이트가 표현해야 하는 상태는 "영구 금지"가 아니라
  **"이전이 끝날 때까지 승인받고 쓰는 예외 경로"** 다.
- **이식은 복사가 아니다.** MARA 에서 "벤더 SDK 임포트"는 곧 위반 신호였지만, 여기에는
  그 SDK 를 임포트하는 정상 코드와 테스트가 있다. 또 MARA 훅이 차단하는 "생산자
  파이프라인 실행"은 **여기서 정상 작업**이다 — 그대로 복사하면 자기 파이프라인을
  자기가 막는다.
- 순서를 뒤집을 수 없다. **vLLM 이전 작업 자체가 이 레포에서 LLM 을 호출하는
  일이다.** 장치 없이 시작하면 전제를 고치러 가는 작업 중에 같은 위반이 난다.
  **게이트 이식은 이전의 선행 조건이지 병행 작업이 아니다.**

## Decision

### Selected

- **Technology:** PreToolUse 훅(Bash) + 클라이언트 생성 시점의 불변식. **새 의존성 없음.**
- **Architecture:** 같은 규칙을 **두 계층에서 독립적으로** 집행한다.

  | 계층 | 파일 | 막는 것 |
  |---|---|---|
  | 도구 | `.claude/hooks/check-external-llm.sh` | Bash·PowerShell **명령 문자열**의 벤더 호스트 / 비의존 벤더 SDK 임포트 / 벤더 클라이언트 생성·호출 표현 / API 키 주입 / 외부 모델 CLI. 차단은 exit 2, 파서 부재 시 fail-closed |
  | 코드 | `extraction/egress.py` + `AnthropicClient.__init__` | 우리 코드를 경유하는 **모든** 벤더 클라이언트 생성. 객체 생성 시점에 `ExternalVendorCallError` |

- **Implementation:**
  1. **기본 차단 + 1회용 승인 파일.** `.claude/external-llm-approved` 가 있으면
     열리고 없으면 막힌다. `.gitignore` 대상이다.
  2. **승인 게이트 양식 6항목** 을 `docs/governance.md` 와
     `.claude/rules/external-llm.md` 에 적었다. 1번이 엔드포인트이고
     **아니오는 승인 대상이 아니라 규칙 위반**이다. **ADR 에만 적지 않는다.**
  3. **모델명으로 1번을 대신하지 않는다.** 판정 기준은 이름이 아니라 목적지다.
  4. **한계도 함께 이식했다.** `python <파일>` 우회·셸 스크립트·Makefile·난독화는
     여기서도 못 막는다. 산문이 아니라 `tests/gate/external_llm_cases.txt` 에
     **기대값 0(통과)** 으로 넣었다.
  5. **차단 상태 1회 실행을 검사 항목으로 넣었다** (`tests/test_blocked_state_run.py`).
     승인 파일 없이 진입점을 돌려 **벤더 호출 전에 멈추는지**를 고정한다.

### Rejected — (나) 경고·기록만 둔다

⛔ **이번 사고의 원인을 그대로 반복한다.** 0.5a 의 게이트도 **있었고 통과했다.**
실패는 "장치가 없었다"가 아니라 **"장치가 확인할 것을 묻지 않았다"** 였다. 경고는
답을 요구하지 않는 장치이므로 같은 계열의 실패를 한 번 더 만든다 — 로그에 남아도
아무도 멈추지 않으면 그건 게이트가 아니다.

## Rationale

1. **기본 차단만이 상태를 정확히 표현한다.** 이 레포의 벤더 호출은 ADR-021 이후로
   정상 경로가 아니다. 기본값은 금지이고 예외는 매번 사람이 열고 닫는다 — 파일이
   그 모양 그대로다.
2. **환경변수 우회로를 두지 않았다.** 환경변수는 셸 한 줄로 켜지고 그 줄이 어디에도
   남지 않는다. 파일은 만들고 지우는 행위가 필요하고, 남아 있으면 보인다.
   레포에 남은 승인 파일은 테스트가 잡는다.
3. **두 계층을 둔 이유는 서로의 사각을 메우기 때문이다.** 훅은 명령 문자열만 보므로
   스크립트 파일 안을 못 본다. 코드 가드는 우리 코드 안에서만 유효하다. 겹치는
   부분이 아니라 **안 겹치는 부분**을 보고 둘을 두었다.
4. **차단 지점을 호출 시점이 아니라 객체 생성 시점에 두었다.** 클라이언트가 만들어진
   뒤에는 어디서든 부를 수 있어 차단 지점이 흩어진다. Protocol 구현체가 태어나는
   자리가 이 레포에서 벤더로 나가는 유일한 문이다.
5. **`client=` 주입을 통과시키되 진짜 SDK 객체는 막았다.** 목 주입은 네트워크로
   나가지 않으므로 막으면 기존 테스트 398건이 깨진다. 그러나 그 경로를 통째로 열어
   두면 **`client=` 한 글자가 게이트 전체의 우회로**가 된다.
6. **단계 이름을 예외 메시지에 넣었다.** 이것이 없으면 승인 없이 한 번 돌려서
   "벤더를 부르는 지점"의 실측 목록을 만들 수 없다 (아래 Evidence).

## Evidence

- **Experiment:** 게이트 검사 **46건 전부 통과** (`bash tests/gate/external_llm_cases.sh`).
  차단 21 / **오탐 대조 17** / 알려진 우회(고의 통과) 5 / 승인 파일 2 / fail-closed 1.
- **Experiment:** **차단 상태 1회 실행 — 진입점 5개 전부 벤더 호출 전에 멈췄다.**
  통과해서 끝까지 돈 진입점은 **0개**다. 드러난 지점은 아래 표.

  | 단계 | 모델 | 클라이언트 생성 지점 | 실제 호출 지점 |
  |---|---|---|---|
  | `relevance_gate` | `claude-haiku-4-5-20251001` | `extraction/extractor.py:318`, `export/runner.py:195` | `extraction/extractor.py:214` |
  | `extraction` | `claude-opus-5` | `extraction/extractor.py:333`, `export/runner.py:196` | `extraction/extractor.py:145` |
  | `eval_judge` | `claude-opus-5` | `eval/runner.py:316` | `eval/runner.py:258` |

  **그 밖에 발견된 것: 없다.** 설정 파일을 읽어서 알던 셋과 실측 목록이 일치했다 —
  다만 **생성 지점은 셋이 아니라 다섯**이고, `export/` 경로가 앞의 둘을 별도 줄에서
  다시 만든다. 이전 범위를 "단계 3개"가 아니라 **"지점 5개"** 로 세야 한다.
- **Cost:** 이 실행의 **아웃바운드 연결 시도 0건.** 소켓 트립와이어로 확인했고,
  피드 수집도 픽스처로 대체해 공개 RSS 호출조차 나가지 않았다.
- **Production Data:** `.venv/Scripts/python.exe -m pytest tests/ -q` → **442 passed**
  (기준선 398 + 신규 44). 기존 398건은 손대지 않았다.

## Alternatives

### (가) 문서에만 적고 사람이 지킨다

- **Pros:** 비용 0. 오탐도 0.
- **Cons:** 규칙은 이미 MARA 문서에 있었고, 그 상태에서 이번 위반이 났다.
- **Rejected because:** 같은 실패를 반복한다.

### (나) 경고·기록만 남긴다

- **Pros:** 오탐이 작업을 막지 않는다.
- **Cons:** 답을 요구하지 않는 장치다.
- **Rejected because:** 위 "Rejected" 절 참고. 이번 사고의 원인 자체다.

### (다) `permissions.deny` 로 하드 차단

- **Pros:** 훅보다 앞단이라 우회 여지가 적다.
- **Cons:** `deny` 는 **승인 파일로도 대화형 승인으로도 풀리지 않는다**(MARA session-07 실증).
- **Rejected because:** 이 규칙의 상태는 "영구 금지"가 아니라 **"승인 없이는 금지"** 다.
  하드 차단은 규칙을 잘못 표현하고, 정당한 승인 경로까지 막아 결국 게이트를 통째로
  끄게 만든다.
- **Recheck if:** 조직이 외부 벤더 사용 불가를 확정하는 경우.

### (라) MARA 의 훅·가드를 그대로 복사한다

- **Pros:** 두 레포의 장치가 문자 그대로 같아진다.
- **Cons:** MARA 훅의 `PRODUCER_RUN` 은 **이 레포의 정상 작업을 막는다.** 그러면
  Implementation 5번의 "차단 상태 1회 실행" 자체가 불가능해진다. "SDK 임포트 = 위반"도
  여기서는 오탐이 된다.
- **Rejected because:** 오탐이 잦은 게이트는 결국 꺼진다. 같은 **판정 기준**을
  이식하되 **패턴**은 이 레포 기준으로 다시 짰다.

## Consequences

### Positive

- 규칙이 **양식·훅·코드 세 곳**에 있다. 한 곳을 안 읽어도 나머지가 걸린다.
- 승인 게이트가 **가부를 가르는 항목**을 갖게 됐다. 이전 양식은 크기만 쟀다.
- **vLLM 이전 범위가 추정이 아니라 실측 목록으로 정해졌다** (Evidence 표).
- 막지 못하는 범위가 **테스트에 적혀 있다.** 다음 사람이 "이건 막히겠지"로 넘어가지 않는다.

### Negative

- **훅이 모든 Bash·PowerShell 호출마다 돈다.** 파서 탐색 순서를 `python` 우선으로 두어
  호출당 비용을 줄였지만 0은 아니다.
- **자기참조 오탐.** 벤더 호출 표현을 예시로 적는 명령은 그 자체가 차단된다. 이 ADR 과
  규칙 문서도 Write 도구로 작성했다 — 회피가 아니라 정상 경로다.
- **테스트가 느려진다.** 케이스마다 훅 프로세스를 실제로 띄우기 때문이고, 모킹하면
  "읽는 것과 돌려 보는 것은 다르다"는 이 게이트의 존재 이유를 스스로 어긴다.
- **파이프라인이 기본적으로 안 돈다.** 의도한 것이지만, 다음 세션이 추출을 돌리려면
  매번 승인 절차를 밟아야 한다.

### Risks

- ⚠️ **스크립트 파일 경유는 막지 못한다.** 명령줄에 벤더 흔적이 없다. 셸 스크립트·
  Makefile·난독화도 같다.
- 🔴 **이 구멍이 MARA 보다 크다.** 저쪽은 코드 계층이 엔드포인트 하나만 읽는
  설계였지만, 여기에는 SDK 가 설치돼 있고 키가 `.env` 에 있다. **스크립트 파일 하나면
  두 계층 밖에서 진짜 호출이 나간다.** 게이트는 재발을 늦출 뿐이고, 경로 자체를
  없애는 것은 **vLLM 이전뿐이다.**
- ⚠️ **거부 목록은 구조상 불완전하다.** 새 벤더·사설 프록시는 통과한다.
- ⚠️ **에이전트 자신의 모델 호출과 서브에이전트, MCP 경유 호출은 범위 밖이다.**
- **이미 나간 데이터는 회수할 수 없다.** 이 ADR 은 재발을 막을 뿐이다 (ADR-021 과 동일).

## Implementation

- [x] `.claude/hooks/check-external-llm.sh` — 도구 계층 차단 (fail-closed)
- [x] `.claude/settings.json` — `Bash|PowerShell` 매처에 훅 등록
- [x] `extraction/egress.py` — 승인 판정 + 벤더 호스트 판정
- [x] `extraction/llm.py` — `AnthropicClient.__init__` 에서 불변식 집행, `stage` 전달
- [x] `eval/runner.py` — judge 클라이언트에 `stage="eval_judge"`
- [x] `docs/governance.md` — 승인 게이트 6항목 양식 (1번 = 대상 엔드포인트)
- [x] `.claude/rules/external-llm.md` — 경로별 규칙과 **막지 못하는 것**
- [x] `tests/gate/external_llm_cases.{sh,txt}` — 46건 (오탐 대조·알려진 우회 포함)
- [x] `tests/test_external_llm_gate.py` — 두 계층의 거부 목록 대조 포함
- [x] `tests/test_blocked_state_run.py` — 차단 상태 1회 실행
- [x] `.gitignore` — `.claude/external-llm-approved`
- [ ] **추출·게이트·judge 를 KT Cloud vLLM 으로 이전** — **다음 세션 과제.**
      범위는 위 Evidence 표의 **지점 5개**다. 판정 기준은 MARA 쪽에 사전 등록돼 있다
      (`docs/eval/preregistration-extraction-on-vllm.md`)
- [ ] **2단계 220건** — 전제 정리 전까지 실행하지 않는다 (MARA session-11 §4)

## Reversibility

- **Reversible:** Partial
- **Rollback:** 훅 등록 한 줄과 `__init__` 의 가드 한 블록을 지우면 장치는 없어진다.
  **그러나 이미 나간 49건과 프롬프트는 되돌릴 수 없다** — 되돌릴 수 있는 것은
  장치뿐이고, 위반의 결과는 롤백 대상이 아니다.
- **Migration Cost:** Low

## Review Trigger

- **vLLM 이전이 끝났을 때** — 위반 경로 자체가 줄어드므로 게이트의 적용 범위를 다시
  본다. ⚠️ **필요 없어지지는 않는다.** 프로바이더를 바꿔도 `anthropic` SDK 는 이
  레포에 설치돼 있고 설정 한 줄로 되돌아간다.
- **2단계 220건 실행을 다시 논의할 때** — 금액과 무관하게 전제부터 본다.
- **새 LLM 호출 지점이 생길 때** — `tests/test_blocked_state_run.py` 의 진입점 목록에
  추가한다. 추가하지 않으면 그 지점은 실측 목록 밖에 있게 된다.
- **조직이 외부 벤더 사용 불가를 확정할 때** — (다)의 `permissions.deny` 가 맞는 표현이 된다.

## References

- **Related ADR:** **MARA ADR-021**(정본 — 위반 기록과 게이트), ADR-003(LLM 계층 Protocol — 차단 지점이 여기다), ADR-006(저비용 관련성 게이트 — 벤더 호출 지점 중 하나), ADR-013(모델별 파라미터), MARA ADR-002(서빙을 vLLM 으로 고정 — 이 전제의 출처), MARA ADR-004(코퍼스 공개 자료 한정 — 피해를 줄여 준 규칙)
- **Documentation:** `docs/governance.md` "승인 게이트 — API 호출", `.claude/rules/external-llm.md`, `docs/handoff/session-02.md`
- **Documentation:** MARA `docs/handoff/session-11.md` §5(이식 지시), `docs/eval/preregistration-extraction-on-vllm.md`
