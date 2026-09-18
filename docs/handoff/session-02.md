# Session 02 핸드오프 — 벤더 호출 게이트 이식 (MARA session-11 §5)

- **날짜:** 2026-09-18
- **범위:** MARA ADR-021 게이트의 생산자 측 이식. §5.2 이식 항목 6개, §5.3 이식 시
  걸리는 것 3개, §5.2-2 의 **(가) 기본 차단 + 승인 파일** 결정, §5.2-5 의
  **차단 상태 1회 실행**까지.
- **신규 ADR:** ADR-017
- **브랜치:** `feat/external-llm-gate` (**push 하지 않음**)
- **테스트:** `.venv/Scripts/python.exe -m pytest tests/ -q` → **442 passed**
  (기준선 398 + 신규 44). 기존 398건은 손대지 않았다
- **외부 API 호출 0건.** 차단 상태 1회 실행의 **아웃바운드 연결 시도도 0건**이다
  (소켓 트립와이어로 확인, §3)
- **MARA 레포: 읽기만 했다. 쓰기 0건.** session-10 / session-11 / ADR-021 /
  훅·가드·케이스 파일을 읽었고 한 바이트도 고치지 않았다

> **다음 세션이 먼저 읽을 곳:** **§3(벤더 호출 지점 실측 목록 — 이전 범위가 이걸로
> 정해진다)**, §4(이식하면서 뒤집은 것), §5(막지 못하는 것), §6(결정 대기).

---

## 1. 왜 이 세션이 있었나 — 한 문단

MARA Session 0.5a 의 계약 검증 추출이 외부 벤더로 나갔고 MARA 의 전제(외부 LLM 벤더
비의존)를 깼다. **그 실행 코드가 이 레포다.** 승인 게이트는 목적·모델명·건수·비용을
물었고 전부 정확히 답변됐지만 **"대상 엔드포인트가 어디인가"는 아무도 묻지 않았다.**
MARA 는 session-11 에서 자기 쪽 게이트를 고쳤지만 **MARA 의 훅은 MARA 세션에서만
돈다** — 이 레포에는 장치가 없어 같은 위반이 그대로 다시 가능한 상태였다.
전문은 MARA ADR-021, 이식 기록은 이 레포 ADR-017.

## 2. 무엇을 만들었나

| 파일 | 역할 |
|---|---|
| `.claude/hooks/check-external-llm.sh` | 도구 계층. Bash·PowerShell 명령 문자열 검사, exit 2, **파서 부재 시 fail-closed** |
| `.claude/settings.json` | `Bash\|PowerShell` 매처에 PreToolUse 등록 |
| `extraction/egress.py` | 코드 계층. 승인 판정 + 벤더 호스트 판정 |
| `extraction/llm.py` | `AnthropicClient.__init__` 에서 불변식 집행, `stage` 전달 |
| `eval/runner.py` | judge 클라이언트에 `stage="eval_judge"` |
| `docs/governance.md` | 승인 게이트 **6항목 양식** (1번 = 대상 엔드포인트) |
| `.claude/rules/external-llm.md` | 경로별 규칙 + **막지 못하는 것** |
| `tests/gate/external_llm_cases.{sh,txt}` | 훅 케이스 **46건** |
| `tests/test_external_llm_gate.py` | 두 계층 거부 목록 대조 포함 |
| `tests/test_blocked_state_run.py` | **차단 상태 1회 실행**을 검사 항목으로 고정 |
| `docs/adr/ADR-017-*.md` | 이식 기록. MARA ADR-021 을 가리킨다 |
| `README.md` | 결정 로그 **D-061 · D-062** |

**§5.2-2 의 (가) 를 그대로 따랐다 — 기본 차단 + 1회용 승인 파일.**
`.claude/external-llm-approved` 가 있으면 열리고 없으면 막힌다. `.gitignore` 대상이고,
**레포에 남아 있으면 테스트가 잡는다.** 환경변수 우회로는 두지 않았다.

⛔ **(나) 경고·기록만 두는 안은 기각.** 0.5a 의 게이트도 있었고 통과했다. 경고는 답을
요구하지 않는 장치이므로 같은 계열의 실패를 한 번 더 만든다.

## 3. 🚩 차단 상태 1회 실행 — **벤더 호출 지점 실측 목록**

> **이 표가 다음 세션의 vLLM 이전 범위다.** §5.2-5 가 요구한 것이고, 추정이 아니라
> 실행해서 확인한 것이다.

승인 파일이 **없는 상태로** 진입점 5개를 돌렸다. **전부 벤더 호출 전에 멈췄다.
통과해서 끝까지 돈 진입점은 0개다.** 그 뒤 드러난 지점을 임시로 우회하며 끝까지
훑어 목록을 닫았다.

### 3.1 드러난 지점 — 단계 3개 / **지점 5개**

| 단계 | 모델 | 클라이언트 **생성** 지점 | 실제 **호출** 지점 |
|---|---|---|---|
| `relevance_gate` | `claude-haiku-4-5-20251001` | `extraction/extractor.py:318`<br>`export/runner.py:195` | `extraction/extractor.py:214` |
| `extraction` | `claude-opus-5` | `extraction/extractor.py:333`<br>`export/runner.py:196` | `extraction/extractor.py:145` |
| `eval_judge` | `claude-opus-5` | `eval/runner.py:316` | `eval/runner.py:258` |

**그 밖에 발견된 것: 없다.**

⚠️ **그런데 "없다"가 이 실행의 결론이 아니다.** 지금까지 알던 셋(추출 / 관련성 게이트
/ judge)은 `config.yaml` 을 **읽어서** 안 것이었고, 이번에 **실행해서** 같은 결론이
나왔다. 달라진 것이 하나 있다 — **생성 지점은 셋이 아니라 다섯**이다. `export/`
경로가 게이트와 추출 클라이언트를 **별도 줄에서 다시 만든다.** 이전 범위를
"단계 3개"로 세면 `export/runner.py` 의 두 줄이 조용히 빠진다.

### 3.2 진입점별 멈춘 지점

| 진입점 | 멈춘 곳 |
|---|---|
| `extraction.extractor --gate-only` | `extractor.py:318` (`relevance_gate`) |
| `extraction.extractor` (full) | `extractor.py:318` (`relevance_gate`) |
| `export.runner run_collect` | `export/runner.py:195` (`relevance_gate`) |
| `eval.runner judge_client_from_config` | `eval/runner.py:316` (`eval_judge`) |
| `eval.runner --judge` | `eval/runner.py:316` (`eval_judge`) |

각 진입점은 **첫 지점에서 멈추므로** 한 번에 전부 드러나지 않는다. 뒤쪽 단계
(`extraction`)는 게이트를 지나야 보인다 — 그래서 우회 패스가 필요했다.

### 3.3 이 실행에서 나간 것

**0건이다.**

- 아웃바운드 **연결 시도 0건.** `socket.connect` / `connect_ex` /
  `create_connection` 에 트립와이어를 걸어 시도 자체를 예외로 만들었다.
- **피드 수집도 픽스처로 대체했다.** 공개 RSS 라도 네트워크는 네트워크다.
  수집기는 LLM 을 부르지 않으므로(정적 확인: `parse_into` 호출부 3곳은 전부
  `extraction/` 과 `eval/` 에 있다) 이 대체가 벤더 호출 지점을 가리지 않는다.
- 이 실행은 **일회성 스크립트로 끝내지 않고** `tests/test_blocked_state_run.py` 로
  고정했다. 새 진입점이 게이트를 안 거치면 이 파일이 깨진다.

## 4. ⚠️ 이식하면서 뒤집은 것 — §5.3 대로 복사하지 않았다

**세 가지가 MARA 와 다르다.** 그대로 복사했으면 게이트가 자기 발을 밟았다.

| # | MARA | 여기 | 왜 |
|---|---|---|---|
| 1 | 벤더 SDK 임포트 = 위반 | **임포트는 통과**, 클라이언트 **생성·호출 표현**만 차단 | `anthropic` 이 이 레포의 실제 의존성이고 정상 코드·테스트가 임포트한다. 같은 패턴이 정탐이자 오탐이 된다 |
| 2 | `PRODUCER_RUN` 으로 `python -m extraction.extractor` 차단 | **이식하지 않았다** | 여기서는 그게 정상 작업이다. 복사하면 자기 파이프라인을 자기가 막고, **§5.2-5 의 1회 실행 자체가 불가능해진다** |
| 3 | `ANTHROPIC_API_KEY=` 패턴을 그대로 | `=` **뒤에 값이 붙은 경우만** | 이 레포에는 `.env.example` 이 있어 `grep "..._API_KEY=" .env.example` 같은 **읽기**가 흔하다. MARA 에 없던 오탐이다 |

- **오탐 대조를 이 레포 기준으로 새로 짰다** — 17건. 차단 21 / 오탐 대조 17 /
  알려진 우회 5 / 승인 파일 2 / fail-closed 1 = **46건**.
- 파서 탐색 순서(`python` 우선)와 **"있는지가 아니라 도는지"** 검사는 그대로
  가져왔다. 환경 문제라 레포와 무관하게 똑같이 밟는다.
- 2번이 되살아나는 것은 `tests/test_external_llm_gate.py` 가 막는다
  (`PRODUCER_RUN=` 이 훅에 나타나면 실패).

## 5. 🚩 막지 못하는 것 — **여기가 MARA 보다 구멍이 크다**

케이스 파일에 **기대값 0(통과)** 으로 5건 고정했다. 언젠가 exit 2 가 되면 그건
실패가 아니라 개선이고, 그때 기대값을 바꾼다.

- 스크립트 파일 / 셸 스크립트 / Makefile 경유 — 명령줄에 벤더 흔적이 없다
- 난독화(base64 등)
- 에이전트 자신의 모델 호출 · Agent/Task 서브에이전트
- MCP 도구 경유 네트워크 호출 (매처가 `Bash|PowerShell`)
- 거부 목록의 빈칸 — 새 벤더·사설 프록시

> 🔴 **저쪽은 코드 계층이 엔드포인트 하나만 읽는 설계라 이 우회가 실제 호출로
> 이어지기 어려웠다. 여기는 다르다.** `anthropic` SDK 가 설치돼 있고 키가 `.env` 에
> 있어서 **스크립트 파일 하나면 두 계층 밖에서 진짜 호출이 나간다.**
>
> **게이트는 해결이 아니라 유예다.** 경로 자체를 없애는 것은 §6 의 vLLM 이전뿐이다.

### 자기참조 — 여기서도 걸렸다

훅은 명령 문자열을 검사하므로 **벤더 호출 표현을 예시로 적는 명령은 그 자체가
차단된다.** ADR·규칙·케이스 파일을 전부 Write 도구로 작성했다. 회피가 아니라 정상
경로다. **커밋 메시지에도 트리거 문자열을 넣지 않았다.**

## 6. 📌 다음 세션 — 추출 단계를 KT Cloud vLLM 으로 이전

**§5 가 끝났으므로 이제 시작할 수 있다.** 목적은 비용 절감이 아니라 전제 위반 해소다.

- **범위는 추정하지 말고 §3.1 의 표로 정한다.** 단계 3개가 아니라 **지점 5개**다.
- ⚠️ **judge 도 같이 옮겨야 한다.** `eval.judge_model` 이 벤더를 가리키는 한
  **판정하는 행위 자체가 같은 위반**이 된다.
- **판정 기준은 MARA 쪽에 사전 등록돼 있다** —
  `docs/eval/preregistration-extraction-on-vllm.md` (MARA session-11 커밋 `615d0bf`).
  요약: ① 통제어휘 이탈률 ≤ 5% **그리고** ② structured output 실패율 ≤ 10% 이면 합격.
  ③ judge 점수 차이(≤ 0.5)는 기록하되 단독으로 불합격을 만들지 않는다.
- **대조 설계:** 이미 Opus 5 로 뽑은 30건과 **동일한 입력**을 vLLM 으로 다시 돌린다.
  보존소에 원문이 남아 있어 수집을 다시 하지 않는다.
- ⚠️ **대조 성립 조건 4가지**(프롬프트 무변경 / structured output 강제 수준 /
  재시도 정책 / `prompt_sha256`)를 **먼저 확인하고 결과를 적는다.** 여기서 갈리면
  대조를 시작하지 않는다.
- ⚠️ **이 작업이 끝나도 게이트가 필요 없어지지 않는다.** 프로바이더를 바꿔도 SDK 는
  설치돼 있고 설정 한 줄로 되돌아간다.

## 7. ⛔ 차단 항목 (이월, 여전히 유효)

> **2단계 220건(약 $16)은 실행하지 않는다.** 금액 문제가 아니므로 **예산 승인으로
> 풀리지 않는다** (MARA session-11 §4).

- MARA session-10 §8.3 의 **측정 설계상 연기**와 session-11 §4 의 **전제상 차단**이
  겹쳐 있다. 둘은 다른 이유이고 **둘 다 풀려야 실행된다.**
- 전제상 차단이 풀리는 경로는 둘 중 하나다. ① §6 의 vLLM 이전이 합격 → 승인 자체가
  없어진다. ② 불합격 → "외부 벤더 사용을 승인할 것인가"를 **조직 결정**으로 올린다.

## 8. 결정 대기

| 항목 | 상태 |
|---|---|
| **승인 게이트를 실제로 통과시킬 일이 생기면** | 6항목을 전부 제시한다. **1번의 답이 "아니오"라는 것을 알고도 진행할지**가 사용자 결정이다 — 이 레포의 현재 상태에서 1번은 언제나 "아니오"다 |
| **`export/` 의 두 생성 지점을 하나로 합칠 것인가** | §3.1 에서 드러난 것. 합치면 이전 범위가 줄지만 `extractor` 와 `export` 의 독립성이 깨진다. **이전 작업 중에 같이 정한다** |
| 본문 커버리지 (D-013) | 이월. 원문 fetch 는 저작권·반입 범위 판단이고 MARA ADR-004 전제를 건드린다 |
| 골든셋 3건 | 이월. 경계 사례 위주라 증가 속도가 느리다 (D-039) |
| judge 비결정성 (D-042/D-049/D-060) | 이월. 루브릭 v4 를 만들 때 같이 본다 |

## 9. 승인 게이트

`docs/governance.md` 의 **새 6항목 양식**으로 이번 세션을 적으면:

| # | 항목 | 이번 세션 |
|---|---|---|
| 1 | 대상 엔드포인트가 내부 vLLM 인가 | **해당 없음 — LLM 호출 0건** |
| 2 | 나가는 데이터 | 없음. 네트워크 호출 0건 |
| 3~6 | 목적 / 모델명 / 건수 / 비용 | 해당 없음 |

- **`.claude/external-llm-approved` 를 만들지 않았다.** 게이트 검사는 임시
  디렉터리에 가짜 프로젝트 루트를 만들어 돌리므로 **레포에 승인 파일이 생기지 않는다.**
- 새 의존성 **0개.** 훅은 bash + 표준 python, 가드는 stdlib 뿐이다.
- 커밋 전 확인: `.env` 미포함, staged diff 에 API 키 패턴·개인 로컬 경로 없음,
  author 이메일 noreply, 실행 산출물 미포함.

## 10. 상태

| 항목 | 상태 |
|---|---|
| 신규 코드 | `extraction/egress.py`, `.claude/hooks/check-external-llm.sh`, `tests/gate/external_llm_cases.{sh,txt}`, `tests/test_external_llm_gate.py`, `tests/test_blocked_state_run.py` |
| 변경 코드 | `extraction/llm.py`(생성 시점 불변식·`stage`), `eval/runner.py`(judge `stage`), `.claude/settings.json`, `.gitignore` |
| 신규 문서 | ADR-017, `.claude/rules/external-llm.md` |
| 변경 문서 | `docs/governance.md`(6항목 양식), `README.md`(D-061·D-062, 결정 로그 표 안의 빈 줄 제거 — D-059/D-060 사이에 이미 있던 렌더 결함), `CLAUDE.md`(맥락 메모 — **`.gitignore` 대상이라 커밋에 안 나온다**) |
| 테스트 | **442 passed** / 게이트 검사 **46 통과 0 실패** |
| 파이프라인·추출 로직 | **변경 0줄** (`extraction/extractor.py`, `export/` 의 추출 로직, 프롬프트, 골든셋 무변경) |
| 외부 API | **0건** |
| 커밋 | 논리 단위로 분리. **push 는 사용자가 검토 후 직접 한다** |

## 주의 (이월, 여전히 유효)

- 테스트는 가상환경으로: `.venv/Scripts/python.exe -m pytest tests/ -q`.
- **실제 Obsidian Vault 에 절대 쓰지 않는다.** 관측 로그·보존소도 같다.
- **다른 레포를 테스트 입력으로 읽지 않는다.** MARA 코퍼스·골든셋이 필요하면
  `tmp_path` 에 픽스처를 만든다.
- 셸 스크립트는 **LF 로 체크아웃되어야 한다** (`.gitattributes`). CRLF 면 셔뱅이
  깨져 훅이 **조용히 안 돈다** — `tests/test_external_llm_gate.py` 가 이제 이것도 잡는다.
- 실행에 쓰인 프롬프트 버전은 **수정하지 않는다.** 새 버전 파일을 만든다.
- **push 전에** `git log --oneline` 으로 확인한다.
