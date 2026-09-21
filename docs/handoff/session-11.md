# Session 11 핸드오프 — 🟢 넷과 F12 를 닫았다. **리뷰 결함 잔여 0건**

- **날짜:** 2026-09-21
- **범위:** F6 · F7 · F8 · F10 (session-10 §5 권고 1순위, 한 묶음) + **F12**.
  ADR 없음(§6), 결정 로그 **D-080 ~ D-083**, ADR-020 Reversibility 보강.
- **브랜치:** `feat/f6-f10-batch` (**push 하지 않음**)
- **테스트:** `.venv/Scripts/python.exe -m pytest tests/ -q` → **620 passed** (602 → +18)
- **게이트 검사 46건 통과 0 실패** (변동 없음)
- **LLM API 호출 0건. 외부 네트워크 호출 0건**
- **의존성: 새로 추가 0개.** `anthropic` 을 **코어에서 `[vendor]` extra 로 내렸다** (F7)

> **다음 세션이 먼저 읽을 곳:** **§2(F7 이 롤백 절차를 바꿨다)**, §4(F12 가 실제로
> 찾아낸 것), **§7(결정 대기)**. §5 의 결함 표는 이제 전부 닫혀 있다.

---

## 1. 무엇을 했나

| | 파일 | 변경 |
|---|---|---|
| **F6** | `extraction/llm.py` | `VENDOR_ONLY_PARAMS` 를 **실제 로직에 연결**. `vendor_only_params_in()` 추가, `client_from_config` 이 vLLM 클라이언트에 넘긴다 |
| | `extraction/vllm.py` | `omitted_vendor_params` 를 받아 보관하고 **stderr 로 알린다** |
| | `config.yaml` | 주석을 실제 동작에 맞춤 (`무시된다` → `보내지 않는다`, 알림 경로 명시) |
| **F7** | `pyproject.toml` | `anthropic` 을 코어 → `[project.optional-dependencies] vendor`. "기본 프로바이더" 주석 삭제 |
| | `extraction/llm.py` | 모듈 상단 `import anthropic` → **지연 import** (`_load_vendor_sdk`) + `_is_vendor_sdk_client` |
| | `docs/adr/ADR-020` | Reversibility 에 **`pip install -e .[vendor]` 선행 단계** 추가 |
| **F8** | `obsidian_writer/mapper.py` | `_wikilink` 가 `#` 도 `\|` 와 같이 지운다. `_LINK_DELIMITERS` 로 묶음 |
| **F10** | `docs/governance.md` | 로컬 산출물 표에 `data/replays/` 추가 + 삭제 명령 갱신 |
| **F12** | `tests/test_adr_format.py` | **신규 11건** — ADR 번호·형식 구조 검사 |
| | `docs/adr/ADR-019` | 머리말 2줄 정정 (§4) |
| — | `README.md` · `tests/README.md` | D-080 ~ D-083, "문서를 재는 테스트" 넷으로 |

신규 테스트 **18건**: `test_adr_format` 11 · `test_rollback_path` 3 ·
`test_vllm_client` 2 · `test_obsidian_writer` 2.

---

## 2. 🚩 F7 — **롤백이 더 이상 한 줄이 아니다**

`anthropic` SDK 가 기본 설치에서 빠졌다. 되돌릴 때는 **순서가 있다.**

```bash
pip install -e .[vendor]     # 1. 먼저
# 그 다음에 config.yaml 의 llm.provider 를 "anthropic" 으로
```

순서가 뒤집히면 클라이언트 생성 시점에 `LLMError` 가 난다 —
"`anthropic` SDK 가 설치돼 있지 않습니다… `pip install -e .[vendor]` 를 먼저".

- 절차는 **ADR-020 Reversibility** 에 적었다. `tests/test_rollback_path.py` 가
  그 문장이 거기 있는지까지 잰다 — 절차 문서가 비면 다음 롤백이 오류로 시작한다.
- **모듈 상단 import 를 같이 내리지 않으면 이 변경은 즉시 파손이다.** 선언만 바꾸고
  `import anthropic` 을 남겨 두면 기본 설치가 그 자리에서 깨지는데, 이 레포의
  `.venv` 에는 SDK 가 깔려 있어 **평소에는 드러나지 않는다.** 그래서
  `test_core_path_runs_without_the_vendor_sdk` 는 **별도 프로세스**에서 SDK import 를
  막고 확인한다 (이미 import 된 모듈은 되돌릴 수 없다).
- 게이트 판정(`_is_vendor_sdk_client`)은 **SDK 를 import 하지 않는다.**
  `anthropic.Anthropic` 인스턴스를 들고 있으려면 그 모듈이 이미 `sys.modules` 에
  있어야 하므로, 없으면 그런 객체도 없다. 여기서 import 를 시도하면 목 주입만 하는
  실행이 SDK 설치를 요구하게 되어 F7 이 그 자리에서 무효가 된다.

> ⚠️ **이것이 벤더 경로를 없앤 것은 아니다.** `pip install anthropic` 한 줄이면
> 그만이고, 키는 여전히 `.env` 에 있다. 바뀐 것은 **기본 설치에 없다**는 것뿐이고,
> 그 값은 "그 한 줄이 기록에 남는다"는 데 있다 (ADR-017 의 유예 구조 그대로다).

---

## 3. F6 — 상수를 지우지 않고 **연결**한 이유

더 작은 변경은 상수를 지우는 쪽이었다. 그렇게 하지 않은 이유 하나다.

> 이 엔드포인트는 모르는 파라미터에 **200 을 주고 아무 일도 하지 않는다**
> (ADR-019). 그래서 `effort: high` 가 걸린 실행과 안 걸린 실행이 **응답만 봐서는
> 구분되지 않는다.** 알려 주는 곳이 여기밖에 없다.

- `MODEL_UNSUPPORTED_PARAMS`(D-067)와 **합치지 않았다.** 방향이 반대다 — 저쪽은
  보내면 **400 이 나는** 것이고, 이쪽은 보내도 **200 이 오는** 것이다. 실패 신호가
  하나는 있고 하나는 없다는 것이 두 목록을 나눠 두는 이유이며, 코드 주석에 적었다.
- **끄는 값은 세지 않는다.** `effort: null` · `thinking: "disabled"` 는 알리지 않는다.
  켜지 않은 것을 매 실행 알리면 그 줄이 상시 소음이 되고 **진짜 빠진 경우가 거기
  섞인다.** 지금 레포의 `relevance_gate` 설정이 정확히 그 모양이다.
- 판정은 `AnthropicClient.from_config` 가 같은 키를 읽는 방식과 **맞춰 두었고 테스트가
  그 일치를 잰다.** 갈리면 "빠졌다"고 알린 이름과 롤백 후 실제로 나가는 이름이 달라져
  대조 실행의 "무엇이 달랐나"가 틀린 값을 갖는다.

---

## 4. 🚩 F12 — 검사를 붙이니 **ADR-019 가 실제로 어긋나 있었다**

F12 의 주장은 "형식이 맞은 건 운이고, 맞는지 확인해 주는 것이 없다"였다.
검사를 붙여 보니 **운이 반만 맞았다.**

| ADR | 결과 |
|---|---|
| ADR-018 | 형식 통과. `Amendment 1` 섹션은 템플릿 밖이지만 **governance 가 요구한 것**이라 허용으로 넣었다 |
| **ADR-019** | ❌ **2건 어긋남** |

```
- **Decision Source:** Agent (session-03 실측) / 기록 지시는 Human
                       ^^^^^ 템플릿 어휘는 Human | AI-Inferred | Code-Inferred
- **Confidence:** (없음)   ← Human 이 아니면 필수인데, 어휘 밖 값이라 규칙이 발동할 자리가 없었다
```

### 판단: **재생성하지 않고 머리말 2줄만 정정했다**

`AI-Inferred (session-03 실측) / 기록 지시는 Human` + `Confidence: High`
(Evidence 에 실측 수치가 있으면 High — 스킬 생성 규칙).

재생성을 고르지 않은 이유는 **잃는 것이 크기 때문**이다. ADR-019 의 값은 Context·
Rationale·Evidence 에 있고, 그 Evidence 는 session-03 의 probe 실측(파라미터 4종
대조표, `minLength` 절단, enum 강제)이다. **다시 만들 수 없는 근거**이고, 스킬은
대화 맥락에서 내용을 뽑으므로 이번 세션에서 재생성하면 그 표가 복원된다는 보장이
없다. 어긋난 것은 머리말 필드 둘뿐이고, 그 둘은 **틀린 값이 아니라 어휘 밖 값**이다.

> ⚠️ **그래도 이것이 "직접 써도 된다"는 선례가 아니다.** 이번에 고친 것은 **이미
> 있는 ADR 의 형식 결함**이고, 새 결정을 남기는 것은 여전히 `adr-skill:adr-recorder`
> 로 간다. 테스트는 형식만 재고, 스킬이 들고 있는 "무엇을 되묻고 무엇을 추측하지
> 않을지"는 파일을 보고 잴 수 없다.

### 이 검사가 보장하지 **않는** 것

- **구조만 잰다.** 결정이 옳은지, Status 가 실제 구현 상태와 맞는지는 모른다
  (ADR-020 이 지금도 `Proposed` 인데 구현은 끝나 있다 — §7).
- **`Reversibility` 를 요구하지 않는다.** 템플릿이 `<!-- 가능하면 항상 -->` 이라
  조건부로 뒀다. ADR-018·019 에 그 섹션이 없다 (§7).
- **`docs/adr/README.md` 의 이관 표**(D 번호 ↔ ADR 대응)는 재지 않는다.

---

## 5. 리뷰 결함 — **잔여 0건**

**session-04 §4 표가 여전히 유일한 기록이다** (리뷰 원문은 레포에 없다). 지우지 말 것.

| | 위치 | 내용 | 심각도 | 상태 |
|---|---|---|---|---|
| ~~F1~~ | `config.yaml` | ~~롤백 경로 파손~~ | 🔴 | ✅ session-05 |
| ~~F2~~ | `collectors/rss.py` | ~~타임아웃 없음~~ | 🔴 | ✅ session-06 (부분 한계 존속) |
| ~~F13~~ | `export/urls.py` | ~~같은 결함 + 재시도~~ | 🔴 | ✅ session-07 (부분 한계 존속) |
| ~~F4~~ | `export/usage.py` | ~~모델명을 안 봄~~ | 🟡 | ✅ session-08 |
| ~~F3~~ | `export/replay.py` | ~~전송 실패를 스키마 실패로 집계~~ | 🟡 | ✅ session-08 |
| ~~F5~~ | `export/replay.py` | ~~테스트 0건~~ | 🟡 | ✅ session-09 |
| ~~F9~~ | `README.md` | ~~`D-003` 두 줄~~ | 🟡 | ✅ session-10 |
| ~~F6~~ | `extraction/llm.py` | ~~`VENDOR_ONLY_PARAMS` 죽은 상수~~ | 🟢 | ✅ **이번 세션** (§3) |
| ~~F7~~ | `pyproject.toml` | ~~`anthropic` core dep + 틀린 주석~~ | 🟢 | ✅ **이번 세션** (§2) |
| ~~F8~~ | `obsidian_writer/mapper.py` | ~~`_wikilink` 가 `#` 미처리~~ | 🟢 | ✅ **이번 세션** |
| ~~F10~~ | `docs/governance.md` | ~~`data/replays/` 누락~~ | 🟢 | ✅ **이번 세션** |
| ~~F11~~ | `.claude/rules/` | ~~`paths:` 누락~~ | — | ✅ session-04 |
| ~~F12~~ | `docs/adr/` | ~~ADR 을 스킬 없이 직접 씀 — 확인 장치 없음~~ | — | ✅ **이번 세션** (§4) |

> **🎯 리뷰 결함 잔여 0건.** 다음 작업은 더 이상 결함에 막히지 않는다.
> 단, **F2·F13 의 "부분 한계 존속"은 그대로다** (session-06 §3 · session-07 §4).

### F8 이 남긴 한계 (결함 아님, 설계 선택)

`C#` 같은 이름은 `C` 가 되어 **다른 노드와 합쳐진다.** Obsidian 위키링크 대상에는
`#` 를 담을 방법이 없다(이스케이프 문법이 없다). 되돌리려면 마크다운 링크
(`[C#](C%23.md)`)로 바꿔야 하고 그건 `관련 개념` 블록 전체의 표현 변경이다 (D-082).

### 확인 명령 (이번 세션 것만)

```bash
# ADR 형식 — F12 재발 탐지
.venv/Scripts/python.exe -m pytest tests/test_adr_format.py -q

# 롤백 절차가 실제로 성립하는가 (SDK 를 막은 별도 프로세스 포함)
.venv/Scripts/python.exe -m pytest tests/test_rollback_path.py -q

# 벤더 전용 파라미터 알림
.venv/Scripts/python.exe -m pytest tests/test_vllm_client.py -q

# 나머지는 session-04 §4 · 07 §5 · 08 §6 · 09 §6 · 10 §5 그대로 유효
```

---

## 6. 설계 판단 — 왜 새 ADR 이 아닌가

**F7 이 경계에 가장 가깝다** — 의존성을 건드렸고, `adr-recorder` 의 발동 조건에
"의존성 파일 변경"이 있다. 그럼에도 새 ADR 을 만들지 않았다.

- **새 의존성이 0개다.** 있던 것을 코어에서 extra 로 **내린 것**이고, 되돌리는 데
  드는 것은 `pyproject.toml` 한 줄과 import 한 줄이다. 산출물·라벨·측정값에 닿지
  않는다 (MARA 가 읽는 JSONL·manifest 불변).
- **이미 그 결정을 담은 ADR 이 있다.** 롤백 경로는 **ADR-020 의 주제**이고,
  governance 는 "기존 결정의 전제가 바뀌면 새 ADR 로 덮지 말라"고 한다. 그래서
  절차를 그쪽 Reversibility 에 적었다.
- F6·F8·F10·F12 는 각각 로직 연결 · 문자 치환 · 문서 표 한 줄 · 테스트 추가다.

되돌리기 비용이 있는 판단이었으면 `adr-skill:adr-recorder` 로 갔어야 한다 —
**그 규칙을 검사로 만든 것이 이번 세션의 F12 다.**

---

## 7. 결정 대기

**이월분이 전부 그대로 유효하다.** session-03 §7 · 04 §5 · 05 §6 · 07 §6 · 08 §7 ·
09 §7 · 10 §6. 중복해 옮기지 않고 가리킨다. 다음 둘은 **다음 대조 실행 전에** 답이
필요하다 (session-09 §7 그대로).

| 항목 | 왜 지금인가 |
|---|---|
| **`gemma` 실행의 비용을 `0` 말고 무엇으로 셀 것인가** (session-08 §7) | 다음 실행이 vLLM 실행이고, 그 실행의 usage 보고서가 전부 `$0.000` 으로 나온다 |
| **`observability/README.md` 의 100건 시나리오** (session-08 §7) | 위와 **같은 질문**이다. 따로 답하지 말 것 |

### 이번 세션에서 새로 생긴 것

| 항목 | 내용 |
|---|---|
| **`ADR-020` 의 Status 를 `Accepted` 로 올릴 것인가** | 구현은 session-05 에 끝났는데 아직 `Proposed` 다. 스킬 규칙상 **새 결정은 코드가 바뀌어 있어도 Proposed 로 시작**하므로 틀린 상태는 아니지만, 언제 올릴지는 정해 두지 않았다. `test_adr_format.py` 는 어휘만 재고 이 판단은 하지 못한다 |
| **`Reversibility` 를 필수로 올릴 것인가** | ADR-018·019 에 그 섹션이 없다. 템플릿이 `<!-- 가능하면 항상 -->` 이라 이번엔 조건부로 뒀는데, **롤백 방법이 안 적힌 ADR 이 F1 이 난 자리**였다. 필수로 올리면 두 문서에 사후 작성이 필요하고, 그건 "원본에 없는 정보를 새로 만들지 않는다"는 이관 원칙과 부딪힌다 |
| **`omitted_vendor_params` 도 관측 로그로 올릴까** | session-05 의 `dropped_params` 항목과 **같은 질문이다. 따로 답하지 말 것.** 이번에 같은 성질의 생산자가 하나 더 늘었다 — 지금은 둘 다 stderr 로만 가고, 파이프를 안 받으면 사라진다 |

---

## 8. 변이 검사 — **복사본으로 복원했다** (12건)

원본을 스크래치패드에 복사해 두고 작업본을 변이시킨 뒤 **그 사본으로 되돌렸다.**
git 을 쓰지 않는다 (governance — `git checkout` 이 같이 있던 미커밋 변경까지 되돌린다).

| # | 변이 | 실패 |
|---|---|---|
| M1 | F6 배선 제거 (`omitted_vendor_params` 를 안 넘긴다) | 1건 |
| M2 | 끄는 값도 켜진 것으로 센다 | 2건 |
| M3 | F7 원상복귀 — 모듈 상단 `import` 복원 | 1건 |
| M4 | F7 원상복귀 — `anthropic` 을 코어 의존성으로 | 1건 |
| M5 | F8 원상복귀 — `#` 를 구분자 목록에서 제거 | 2건 |
| M6 | **F12 원상복귀 — ADR-019 머리말을 `Agent` 로** | **2건** |
| M7 | ADR 번호 중복 | 2건 |
| M8 | ADR 번호에 구멍 | 2건 |
| M9 | 제목 번호와 파일명 불일치 | 1건 |
| M10 | 템플릿 밖 섹션 추가 | 1건 |
| M11 | 필수 섹션 삭제 (`Rationale`) | 1건 |
| M12 | 머리말 필드 순서 뒤집기 | 1건 |
| — | **복원 후** | **전부 통과** |

---

## 9. 승인 게이트

| # | 항목 | 이번 세션 |
|---|---|---|
| 1 | **대상 엔드포인트가 내부 vLLM 인가** | **해당 없음 — LLM 호출 0건.** `provider` 를 바꾸지 않았다 |
| 2 | 나가는 데이터 | **없음** |
| 3~6 | 목적 / 모델명 / 건수 / 비용 | 해당 없음 |

`.claude/external-llm-approved` 를 만들지 않았다. **`data/` 를 한 번도 열지 않았다.**
`pyproject.toml` 을 고쳤지만 **설치를 실행하지 않았다** — `.venv` 는 그대로다.

---

## 10. 상태

| 항목 | 상태 |
|---|---|
| 변경 코드 | `extraction/llm.py` · `extraction/vllm.py` · `obsidian_writer/mapper.py` |
| 변경 설정 | `pyproject.toml`(의존성 이동) · `config.yaml`(주석만) |
| 신규 테스트 | **18건** (`test_adr_format.py` 11 신규 파일 + 기존 3파일에 7건) |
| 변경 문서 | `README.md`(D-080~083) · `tests/README.md` · `docs/governance.md` · ADR-019 · ADR-020 |
| ADR | **새 ADR 없음.** ADR-020 Reversibility 보강 + ADR-019 머리말 정정 — 근거는 §6·§4 |
| 테스트 | **620 passed** (602 → +18) / 게이트 검사 **46 통과 0 실패** |
| LLM API 호출 | **0건** / 외부 네트워크 호출 **0건** |
| 커밋 | 있음. **push 는 사용자 확인 후** |

## 주의 (이월, 여전히 유효 — 한 줄 추가됐다)

- 🆕 **`pip install -e .` 만으로는 벤더 롤백이 안 된다.** `[vendor]` 를 먼저 설치한다 (§2).
- 테스트는 가상환경으로: `.venv/Scripts/python.exe -m pytest tests/ -q`.
- **실제 Obsidian Vault 에 절대 쓰지 않는다.** 관측 로그·보존소·`data/replays/` 도 같다.
- **변이 검사는 복사본이나 별도 worktree 에서.** 작업본에 `git checkout` 을 쓰지 않는다.
- 실행에 쓰인 프롬프트 버전은 **수정하지 않는다.** 새 버전 파일을 만든다.
- **결정 로그 표 안에 예시용 가짜 D 번호를 쓰지 않는다.** 테스트가 끊어진 참조로 읽는다.
  표 안의 `|` 문자는 `\|` 로 이스케이프한다 — 안 하면 칸이 하나 더 생긴다 (D-082 에서 겪음).
- 문서·규칙에 벤더 호출 표현을 쓸 때는 **Write/Edit 도구**로 쓴다. 명령줄에 넣으면
  훅이 그 명령 자체를 차단한다.
- 셸 스크립트는 **LF** 로 유지한다. CRLF 면 훅이 조용히 안 돈다.
- **push 전에** `git log --oneline` 으로 확인한다.
