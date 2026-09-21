# Session 05 핸드오프 — F1: 롤백 경로가 실제로 동작하게 만들었다

- **날짜:** 2026-09-21
- **범위:** session-04 §4 의 **F1** 처리. 재현 → 원인 2건 확인 → 수정 → 변이 검사로 재검증.
  ADR-020 신규, ADR-018 에 Amendment 1.
- **브랜치:** `feat/f1-rollback-fix` (**push 하지 않음**)
- **테스트:** `.venv/Scripts/python.exe -m pytest tests/ -q` → **474 passed** (462 → +12 신규)
- **게이트 검사 46건 통과 0 실패** (변동 없음)
- **외부 벤더 API 호출 0건.** LLM 호출 자체가 0건 — 전부 목 클라이언트로 요청 **형태**만 봤다
- **새 의존성 0개**

> **다음 세션이 먼저 읽을 곳:** **§3(F1 이 기록보다 컸다)**, §5(남은 결함 9건),
> §6(결정 대기 — 새로 2건 늘었다).

---

## 1. 무엇을 했나

| 파일 | 변경 |
|---|---|
| `extraction/llm.py` | `MODEL_UNSUPPORTED_PARAMS` + `unsupported_params()` 추가. `AnthropicClient.__init__` 이 거부 파라미터를 드롭하고 `dropped_params`·stderr 로 알린다. `from_config` 이 `vendor_model` 을 해석 |
| `eval/runner.py` | `judge_client_from_config` 이 `eval.judge_vendor_model` 을 단계 섹션으로 전달 |
| `config.yaml` | `vendor_model` 3곳 추가(주석으로만 있던 이전 모델명을 키로 승격). `temperature` 주석 갱신 |
| `tests/test_rollback_path.py` | **신규 12건.** 롤백 경로 테스트가 0건이었다 |
| `docs/adr/ADR-020-...md` | **신규.** `adr-recorder` 스킬로 작성 |
| `docs/adr/ADR-018-...md` | **Amendment 1** + Risks 정정 표시 + Review Trigger 상태 갱신 |
| `README.md` | 결정 로그 **D-067 · D-068** |

---

## 2. 재현 → 수정 → 재검증

session-04 부록의 확인 명령을 그대로 썼다.

**수정 전** (`llm.provider: anthropic`):

| 단계 | 나가던 요청 | 결과 |
|---|---|---|
| `relevance_gate` | `model: gemma-4-31B-it` + `temperature: 0` | 벤더에 없는 모델 |
| `extraction` | `model: gemma-4-31B-it` + `effort` + `thinking` + `temperature: 0` | 벤더에 없는 모델 |
| `eval_judge` | `model: claude-opus-5` + `temperature: 0` | **400** (D-033) |

**수정 후** (같은 명령):

| 단계 | 나가는 요청 | `dropped_params` |
|---|---|---|
| `relevance_gate` | `claude-haiku-4-5-20251001` + `temperature: 0` | `()` — Haiku 는 수용한다 |
| `extraction` | `claude-opus-5` + `effort` + `thinking` | `('temperature',)` |
| `eval_judge` | `claude-opus-5` + `effort` + `thinking` | `('temperature',)` |

`provider: vllm` 기본 경로는 세 단계 전부 `gemma-4-31B-it` 로 **불변**이다. 대조 실행
결과(ADR-018 Evidence)가 이 설정에 묶여 있어 그쪽이 바뀌면 안 됐다.

### 변이 검사 — 새 테스트가 실제로 잡는지 확인했다

F5 의 교훈("측정 도구가 미검증")을 같은 커밋에서 반복하지 않으려고, 수정을 되돌려
테스트가 빨개지는지 봤다.

| 변이 | 실패 |
|---|---|
| `vendor_model` 해석 제거 | 3건 (`test_rollback_resolves_a_vendor_model[*]`) |
| 파라미터 드롭 제거 (`rejected = ()`) | 4건 |
| 복원 후 | **12/12 통과** |

---

## 3. 🚩 F1 이 기록보다 컸다 — 파손이 2건이었다

**핸드오프에 적힌 F1 은 3단계 중 1단계만 설명한다.** 재현해 보고서야 알았다.

1. **파라미터** — F1 이 기록한 것. `temperature: 0` 이 Opus 5 로 실려 400.
2. **모델명** — **기록되지 않았던 것.** ADR-018 이 세 단계의 `model` 을 gemma 로
   **제자리에 덮어썼고**, 벤더 모델명이 `# 이전: claude-opus-5` 주석으로만 남았다.
   모델 능력 판정으로는 안 고쳐진다 — `unsupported_params("gemma-4-31B-it")` 는
   당연히 비어 있다.

2번을 안 고치면 **"롤백 경로를 복구했다"고 쓸 수 없다.** 사용자 확인을 받고 범위를
둘 다로 잡았다.

> **왜 안 걸렸나가 사건보다 중요하다.** 롤백 경로에 테스트가 **0건**이었다. 이전
> 자체는 대조 실행까지 측정했는데(ADR-018 Evidence), **되돌아가는 쪽은 한 번도
> 실행되지 않았다.** ADR-017·F11 과 같은 모양이다 — 장치가 있었고 적혀 있었는데,
> 그것이 동작하는지 확인하는 주체가 없었다.
>
> **한 줄 더:** ADR-018 Risks 의 "되돌리기가 너무 쉽다 — `llm.provider` 한 줄이다"는
> 작성 시점에 **사실이 아니었다.** 어려웠던 이유는 마찰이 아니라 **경로가 깨져
> 있었기 때문**이고, 그것은 안전장치가 아니다. 지금은 그 서술이 참이 됐다 —
> **위험이 실제로 커졌다는 뜻**이므로 ADR-020 Risks 에 🔴 로 적었다.

---

## 4. 설계 판단 — 왜 이렇게 했나

세부 근거는 **ADR-020** 에 있다. 여기엔 다음 세션이 걸릴 만한 것만.

- **프로바이더가 아니라 모델로 판정한다.** 400 을 내는 주체가 모델이다. `provider`
  로 가르면 같은 `anthropic` 안의 Opus 5 / Haiku 4.5 차이를 표현할 자리가 없다.
- **드롭은 `from_config` 이 아니라 `__init__` 에서.** 핸드오프 문구는 `from_config`
  였지만, 그러면 `AnthropicClient(...)` 직접 생성 경로(스크립트·probe)가 그대로
  400 을 낸다. `__init__` 이 모든 생성 경로가 지나는 유일한 지점이다.
- **접두사 매칭.** 날짜 접미사(`claude-haiku-4-5-20251001`)를 버전마다 등록하지 않기
  위해서다. **표에 없는 모델은 아무것도 빼지 않는다** — 모르는 모델에 능력을
  가정하지 않는다.
- **`vendor_model` 은 형제 키.** `model: {vllm:, anthropic:}` 맵도 검토했지만
  `section.get("model")` 소비자 전부와 `eval.judge_model` 이 함께 바뀐다. 롤백 하나
  고치려고 출력 계약에 닿는 필드의 타입을 바꾸지 않는다.
- **뺀 것을 조용히 빼지 않는다.** `dropped_params` + stderr. 이 레포에 로깅 계층이
  없어 기존 관례(`print(..., file=sys.stderr)`)를 따랐다.

---

## 5. 🚩 남은 결함 9건 — session-04 §4 에서 F1 만 빠졌다

**session-04 §4 표가 여전히 유일한 기록이다** (리뷰 원문은 레포에 없다 —
session-04 §2). 지우지 말 것.

| | 위치 | 내용 | 심각도 | 상태 |
|---|---|---|---|---|
| ~~F1~~ | `config.yaml` | ~~롤백 경로 파손~~ | 🔴 | ✅ **이번 세션 완료** (파손 2건 모두) |
| **F2** | `collectors/rss.py:138` | `feedparser.parse` 타임아웃 없음 → 무기한 블로킹, 로그도 안 남음 | 🔴 | 미처리 |
| **F3** | `export/replay.py:178-179` | 전송 실패를 스키마 실패로 집계 | 🟡 | 미처리 |
| **F4** | `export/usage.py:23` | 모델명을 안 봄 → gemma 실행에 없는 달러 비용 | 🟡 | 미처리 |
| **F5** | `export/replay.py` | 테스트 0건 — 사전 등록 수치를 만든 측정 도구가 미검증 | 🟡 | 미처리 |
| **F9** | `README.md:204,231` | `D-003` 두 줄, 상태가 서로 다름 | 🟡 | 미처리 (재확인함 — 여전히 중복) |
| **F6** | `extraction/llm.py:363` | `VENDOR_ONLY_PARAMS` 죽은 상수 | 🟢 | 미처리 (§7 참고) |
| **F7** | `pyproject.toml:16` | `anthropic` core dep + 틀린 주석 | 🟢 | 미처리 (§7 참고) |
| **F8** | `obsidian_writer/mapper.py:113` | `_wikilink` 가 `#` 미처리 | 🟢 | 미처리 |
| **F10** | `docs/governance.md` | `data/replays/` 가 로컬 산출물 표에 없음 | 🟢 | 미처리 |

### 권고 순서 (session-04 에서 F1 만 제거)

| 순서 | 항목 | 이유 |
|---|---|---|
| 1 | **F4 + F3** | 둘 다 "틀린 수치가 의사결정에 들어가는" 종류. F4 는 2단계 220건 추정에 직결 |
| 2 | **F2** | 증상이 "조용히 멈춤"이라 진단이 비싸다 |
| 3 | **F5** | 다음 대조 실행 **전에** |
| 4 | **F9** | 결정 로그 무결성은 이 레포의 핵심 자산 |
| 5 | F6 · F7 · F8 · F10 | 묶어서 한 커밋 |

### 확인 명령 (이번 세션 것만 갱신)

```bash
# F1 재발 탐지 — 롤백이 세 단계 전부 벤더 모델을 가리키고 거부 파라미터가 없는가
.venv/Scripts/python.exe -m pytest tests/test_rollback_path.py -q

# 나머지 F2~F10 확인 명령은 session-04 §4 그대로 유효
```

---

## 6. 결정 대기

session-03 §7 · session-04 §5 의 항목은 **전부 그대로 유효하다.** 중복해 옮기지
않고 가리킨다. 이번 세션에서 **새로 생기거나 상태가 바뀐 것만** 적는다.

| 항목 | 내용 |
|---|---|
| 🆕 **`dropped_params` 를 관측 로그로 올릴까** | 지금은 stderr 만 간다. 대조 실행에서 "무엇이 달랐나"를 적으려면 산출물에 남는 편이 맞지만, 롤백 시에만 값이 생겨서 평소엔 빈 필드다. ADR-020 Implementation 에 미체크로 남겨 뒀다 |
| 🆕 **`vendor_model` 이 낡는 것을 어떻게 막을까** | `provider: vllm` 인 동안 아무 효과가 없어 갱신되지 않은 채 낡을 수 있고, 낡은 값은 **롤백 시점에야** 드러난다. `test_vocab_sync.py` 같은 동기화 검증 대상이 아니다. 실제 호출로 검증한 적도 없다 |
| **F12 — ADR 스킬 우회** | 변화 없음. session-03 의 ADR-018·019 가 형식에 맞는지는 여전히 아무도 검증하지 않았다. **다만 이번에 ADR-018 을 Amendment 로 고치면서 본문을 읽었고, 템플릿 섹션 구성은 어긋나지 않았다** — 번호·형식 규칙까지 본 것은 아니다 |
| **롤백이 너무 쉽다** | session-04 는 "악화됐다(되돌리면 깨진다)"고 적었는데, 이제 **깨지지 않는다.** 마찰이 실제로 줄었으므로 ADR-018 Risks 의 🔴 가 원래 의도대로 되살아났다. 방어선은 계속 `extraction/egress.py` 기본 차단 + `assert_internal_endpoint` |
| **`VLLM_BASE` 두 레포 중복** | 변화 없음 |

---

## 7. 이번 수정과 붙어 있는 미처리 항목 — 같이 보지 않은 이유

- **F6 (`VENDOR_ONLY_PARAMS` 죽은 상수).** 새로 만든 `MODEL_UNSUPPORTED_PARAMS` 와
  헷갈리기 쉬운데 **다른 것이다** — `VENDOR_ONLY_PARAMS` 는 "vLLM 이 무시하는 벤더
  전용 파라미터", 새 표는 "벤더 모델이 거부하는 파라미터". 방향이 반대다. 합치지 말 것.
  F6 를 처리할 때 이 구분을 먼저 확인한다.
- **F7 (`anthropic` 을 optional extra 로).** F1 과 같은 "롤백 경로" 주제라 묶고
  싶어지지만, 내리면 롤백이 **설치 단계**에서 보이게 되는 별도 효과가 있고
  `pyproject.toml` 변경이다. 이번 커밋에 섞지 않았다.

---

## 8. 승인 게이트

| # | 항목 | 이번 세션 |
|---|---|---|
| 1 | 대상 엔드포인트가 내부 vLLM 인가 | **해당 없음 — LLM 호출 0건.** 전부 `client=object()` 목 주입 |
| 2 | 나가는 데이터 | 없음. 네트워크 호출 0건 |
| 3~6 | 목적 / 모델명 / 건수 / 비용 | 해당 없음 |

`.claude/external-llm-approved` 를 만들지 않았다. 새 의존성 0개.

> 이번 세션이 **벤더 모델명을 설정에 되살렸다**는 점은 짚어 둔다. 게이트 1번의 답을
> 바꾸지는 않는다 — `provider` 는 `vllm` 그대로이고 `vendor_model` 은 그 줄을 바꿔야
> 읽힌다. **다만 되돌리는 손의 부담이 줄었으므로 1번을 묻는 이유가 더 커졌다.**

---

## 9. 상태

| 항목 | 상태 |
|---|---|
| 변경 코드 | `extraction/llm.py`, `eval/runner.py` |
| 변경 설정 | `config.yaml` (`vendor_model` 3곳 + 주석) |
| 신규 테스트 | `tests/test_rollback_path.py` — 12건 |
| 신규 문서 | `docs/adr/ADR-020-...md`, 이 파일 |
| 변경 문서 | `docs/adr/ADR-018-...md` (Amendment 1), `README.md` (D-067·D-068) |
| 테스트 | **474 passed** (462 → +12) / 게이트 검사 **46 통과 0 실패** |
| 외부 벤더 API | **0건** |
| 커밋 | 있음. **push 는 사용자 확인 후** |

## 주의 (이월, 여전히 유효)

- 테스트는 가상환경으로: `.venv/Scripts/python.exe -m pytest tests/ -q`.
- **실제 Obsidian Vault 에 절대 쓰지 않는다.** 관측 로그·보존소·`data/replays/` 도 같다.
- 실행에 쓰인 프롬프트 버전은 **수정하지 않는다.** 새 버전 파일을 만든다.
- 문서·규칙에 벤더 호출 표현을 쓸 때는 **Write/Edit 도구**로 쓴다. 명령줄에 넣으면
  훅이 그 명령 자체를 차단한다.
- **push 전에** `git log --oneline` 으로 확인한다.
