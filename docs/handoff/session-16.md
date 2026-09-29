# Session 16 핸드오프 — 🟢 소스 재편 · 매일 실행 전제. **7개 소스, 인자 없이 `pipeline run`**

- **날짜:** 2026-09-29
- **범위:** 수집 실패 구분, 수용 범위 제한(발행일 창), 후보 소스 실측, GeekNews·HF 제외, DeepMind 원인 규명
- **브랜치:** `feat/source-rework` — **main 에 병합하지 않았다. push 안 함.** ADR-023 이 사용자 확인 대기라 병합은 그 뒤로 둔다
- **테스트:** `.venv/Scripts/python.exe -m pytest tests/ -q` → **854 passed** (782 → +72)
- **LLM API 호출: 0건.** 공개 피드 GET 과 curl 만 했다 (현재 소스 · 후보 16개 · 공식 Anthropic 페이지 상태 확인)
- **의존성: 새로 추가 0개.** 시간대 처리에서 `tzdata` 를 피한 것이 의도다 (ADR-023)
- **ADR-023** (**Accepted**, 사용자 확인) — `adr-skill:adr-recorder` 로 작성
- 결정 로그 **D-103 · D-104 · D-105 · D-106**. D-014 · D-017 은 `번복됨 → D-105`

> **다음 세션이 먼저 읽을 곳:** **§3(첫 실행 전에)**, **§6(결정 대기)**.

---

## 1. 무엇을 했나

| 커밋 | 내용 |
|---|---|
| `4c18a37` | 수집기가 `FeedResult(status=FetchStatus)` 를 돌려준다 — 정상 0건(`empty`)과 장애 7종을 가른다 (D-103). `Content-Encoding` gzip/deflate 해제 + 해제 후 크기 상한 (D-104) |
| `1a5ec30` | 수용 창 · 시간대 · config 상한 · 갱신 멈춤 (ADR-023, D-106). `pipeline/admission.py` 신규. 수집을 멈춘 소스(`sources.retired`)를 `eval.predict` 가 조회 |
| `10a41f8` | config 를 7개 소스로 재편, GeekNews · HF 은퇴 (D-105). schema.md · schema.py 한 줄 보강 |

변이 검사: 1단계 7건, 2단계 11건을 **임시 복사본**에서 넣었고 전부 잡혔다. 2단계 첫 시도에서 "시간대 표기가 있는 값에도 오프셋 적용" 변이가 **살아남았다** — 테스트 시각이 이중 적용 후에도 같은 UTC 날짜라 드러나지 않았다. 자정을 넘는 시각으로 바꿔 잡았다.

---

## 2. 핵심 발견

### DeepMind 는 불안정한 피드가 아니었다 (D-104)

Google Frontend 가 **요청하지 않은 gzip** 을 캐시 노드에 따라 보냈다(`content-encoding: gzip` 헤더는 정확). urllib 은 풀지 않아 feedparser 가 "not well-formed" 를 냈다. curl 6회 중 1회 원시 바이트가 `1f 8b`. 수정 후 25회 중 gzip 3회 포함 **25/25 정상, 100건**. session-15 의 plan 실패 / run 성공이 이것이다. **제외 후보에서 뺐다.**

### 발행일 실측 — 없는 항목 0%, 문제는 시간대

- 현재·후보 약 2,900개 엔트리에서 발행일 없음 **0건**, 미래 날짜 0건
- AI타임스 일반 판 · 인공지능신문은 **시간대 없이 KST**. 우리 코드는 UTC 로 읽어 ±1일 어긋났다. 같은 기사 `idxno=215734`: 일반 판 → 09-29, gn 판(`+0900`) → 09-28
- 이제 `published_at` 은 **모든 소스에서 UTC 날짜**다. 인공지능신문은 `naive_date_offset: "+09:00"`

### 공식 피드도 조용히 멈춘다

ZDNet Korea 의 유일하게 살아 있는 경로(`Include2/NewsSection0020.xml`)가 **200, 30건, 최신 2024-05-10**. D-103 은 이걸 `ok` 로 본다 → `max_silence_days` (ADR-023).

### Anthropic 비공식 중계 — 넣지 않음 (사용자 동의)

| 중계 | 관측된 문제 |
|---|---|
| RSSHub (bestblogs) | 본문 전문이지만 **모델 출시 글 누락** (`claude-opus-5-5` · `claude-sonnet-5-5` 등 5건) |
| taobojlen | 출시 글은 있으나 **링크 404** 4건 (중계가 URL 을 만든다), 정렬 안 됨, 본문 44자 |
| Olshansk | RSSHub 와 같은 누락, research 피드 제목 오염(`"Sep 25, 2026Science…"`) |

출시 글은 공식 사이트에서 `/news/` 가 아닌 경로(`/claude-opus-5-5`)에 있어 `/news` 목록을 긁는 중계가 놓치는 것으로 보인다. AI타임스가 Sonnet 5.5 를 당일 보도했다 — 2차 보도로 일부 보완.

---

## 3. ⚠️ 첫 실행 전에 — 새 소스 4개는 게이트를 한 번도 안 거쳤다

`python -m pipeline plan` (2026-09-29, 인자 없음 = config 상한):

| 소스 | 상태 | 게이트 ≤ | 추출 ≤ |
|---|---|---:|---:|
| arXiv cs.CL | needs_gate 20 | 20 | 10 |
| OpenAI News | needs_gate 16 · **out_of_window 1212** · stored 2 · 외 | 10 | 5 |
| DeepMind | needs_extraction 1 · out_of_window 95 · 외 | 0 | 1 |
| NVIDIA Developer | needs_gate 12 · out_of_window 88 | 10 | 5 |
| Microsoft Research | needs_gate 2 · out_of_window 8 | 2 | 2 |
| AI타임스 | needs_gate 50 | 40 | 15 |
| 인공지능신문 | needs_gate 50 | 25 | 10 |
| **합** | | **예상 107 / 승인 대상 상한 115** | **예상 48 / 51** |

- **거버넌스대로 소표본부터 간다.** 인자 없는 `pipeline run` 은 115/51 을 부를 수 있다. 새 소스(특히 한국 매체 둘)의 게이트 통과율·추출 형식을 모르는 상태다 → 먼저 CLI 로 소스당 2~3건(CLI 가 config 상한 **전체를 대체**하므로 명시한 소스만 돈다), 결과를 보고 본 실행은 별도 승인
- 승인 요청 숫자는 `plan` 의 **"승인 대상 상한"** 줄에서 가져온다 (D-102)
- 게이트 6항목 1번(목적지) 확인은 그대로다 — `llm.provider: vllm`

---

## 4. 설계 요지 (ADR-023)

- 창은 **원장에 처음 들어오는 항목에만** 건다. 게이트 오류 재시도·추출 대기는 날짜와 무관
- `span = 첫 실행이면 7`, 아니면 `min(max(7, 소스별 마지막 완결 실행 이후 + 1), 14)`. 완결 = 수집 성공 ∧ `deferred_gate == 0` ∧ `undated_deferred == 0`. 기준점은 `data/pipeline/runs/*.json` 의 `window`
- 발행일 없음·미래 → 피드 순서로 소스당 5건
- CLI 상한을 **하나라도** 주면 config 상한 전체 대체 (병합하면 승인 숫자와 코드 숫자가 갈린다)
- 절대 천장 30/15 → **200/100** (오타 방지선. 승인 대상은 여전히 소스별 합)
- `stale_feed` 는 **소스 단위 실패** — D-103 의 종료 코드 완화(다른 소스가 정상이면 산출 0이어도 3) 대상
- `pipeline.window` 절을 지우면 창이 꺼진다

---

## 5. 소스 구성 (D-105)

**활성 7개:** arXiv cs.CL · OpenAI News · Google DeepMind Blog · NVIDIA Developer · Microsoft Research · AI타임스(gn 판) · 인공지능신문

**은퇴(`sources.retired`, 수집 안 함):** GeekNews · Hugging Face Blog — config 에서 **지우지 않은 이유**: `eval.predict` 가 골든셋 URL 호스트로 소스 이름을 유도한다. 지웠더니 #33001 재추출이 `PredictError` 로 멈추는 것을 확인했다 → `retired` 도 조회하게 했고, 실제 config·실제 골든셋 3건을 도는 테스트를 넣었다.

**넣지 않음:** Google Research(19자) · Mistral(0자 29/50) · NVIDIA Blog(게이밍 혼재) · Meta AI(피드 없음) · ZDNet Korea(멈춤) · Anthropic 중계 · **전자신문 AI 보류**(250자, 인공지능신문과 겹침)

GeekNews 예시: `extract_ontology.v4.md` 는 HTML 주석(변경 이력)에만 소스명이 있고 본문은 "커뮤니티 애그리게이터" — **그대로**(D-010). `schema.md` · `schema.py` 는 한 줄 보강.

---

## 6. 결정 대기

**session-14 §7 · session-15 §7 은 그대로 유효하다** (기본 루브릭 v6 전환, S3Gym ③(a), #33001 ②(a), export 계약 `prompt_sha256`, 작업본 CRLF, D-055, v6 브랜치 병합, 🔴 `judge_human_gap` 무효(D-087), 🔴 기존 보존소 30건 `claude-opus-5`). session-15 의 "피드 실패 종료 코드"는 **D-103 으로 닫았다.**

### 새로 생긴 것

| 항목 | 내용 |
|---|---|
| ~~**ADR-023 확인**~~ | ✅ **닫음** — main 에 ff 병합·push 완료, ADR-023 Accepted (사용자 확인, 2026-09-29) |
| 🔴 **매일 자동 실행 vs 실행 전 승인** | 상한은 config 로 올라갔지만 거버넌스의 승인은 실행마다다. "config 상한 = 상시 승인"으로 읽히면 안 된다. 스케줄링과 함께 정한다 |
| **새 소스 첫 실행** | §3. 소표본 → 결과 보고 → 본 실행 별도 승인 |
| **전자신문 AI** | 인공지능신문 게이트 통과율을 본 뒤 재판단 (사용자) |
| **`max_silence_days` 값** | 하루치 실측(피드 깊이·7일 건수)에서 잡은 첫 값이다. 갱신 주기가 불규칙한 소스(MSR 30일, DeepMind 21일)는 오탐·미탐을 운영으로 봐야 한다 |
| **한국 매체 피드 깊이** | 최근 50건(2~3일치)만 준다. **3일 넘게 쉬면 창과 무관하게 그 사이 글을 잃는다.** 매일 실행이 전제라는 뜻이다 |
| **`published_at` = UTC 날짜** | 코드에서는 통일했다. MARA export 계약 문서에 이 뜻이 적혀 있는지는 **확인하지 않았다** — 계약을 먼저 고치는 원칙상 MARA 쪽과 맞출 항목 |
| `sources.arxiv` | 코드에서 안 읽는 설정 블록이다(`lookback_days: 3` 이 창의 7과 헷갈린다). 주석만 달았다. 지울지 |
| README "현재 상태" 표 | 여전히 Haiku/Opus 서술 · "261 passed" — 이월 |

---

## 주의 (이월 + 추가)

- 🆕 **`python -m pipeline run` 은 이제 인자 없이 7개 소스를 config 상한(115/51)으로 돈다.** 소표본을 원하면 CLI 상한을 준다 — 준 소스만 돈다.
- 🆕 **`fetch_source` / `collect` 는 여전히 실패와 정상 0건을 같은 `[]` 로 준다.** 구분이 필요하면 `fetch_feed` / `collect_results`.
- 🆕 **실행 요약(`data/pipeline/runs/`)이 창의 기준점이다.** 지우면 첫 실행처럼 7일 창으로 돈다.
- 🆕 셸 heredoc 으로 파이썬 정규식을 쓰다가 `\b` 가 백스페이스 문자로 들어갔다. 역슬래시가 든 코드는 Write/Edit 으로 쓴다.
- 승인 요청의 건수는 `plan` 의 "승인 대상 상한" 줄에서 가져온다.
- "같은 명령을 다시 돌리면 0건"이 아니다. 중복 판정을 확인하려면 상한 0 으로 돌린다.
- `ExternalVendorCallError` 는 부분 실패가 아니다.
- 테스트는 가상환경으로. 실제 Vault·`eval/scores/`·보존소·원장에는 쓰지 않는다. 변이 검사는 복사본에서.
