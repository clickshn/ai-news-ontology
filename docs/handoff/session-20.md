# Session 20 핸드오프 — 🟢 MARA 코퍼스 37건 메타 backfill 구현·실행·export (ADR-026 · ADR-027)

- **날짜:** 2026-10-02
- **범위:** session-19 §9 경로 B 의 구현과 실행. 37건 전체를 `gemma-4-31B-it` + `extract_ontology.v4.md` 로 재추출하고 37건만 export. **MARA 레포 쓰기 0 · 운영 폴더 git 명령 0 · 운영 폴더 코드 변경 0**
- **브랜치:** `feat/mara-backfill` (worktree `C:\ai-news-ontology-backfill`, `chore/mara-meta-survey` `b2df527` 에서 분기). **push 안 함 · main 병합 안 함**
- **커밋:** `33d9e59` (§3.3 문구 질문 닫음 · ADR Status 한 낱말) · `ef1aaaf` (backfill 구현) · 이 handoff 커밋
- **테스트:** worktree 전용 `.venv` 에서 `984 passed, 1 skipped` (기준 964 + 새 20)
- **LLM 호출:** 추출 **37회** (소표본 3 + 본 실행 34), 게이트 0, 스키마 재시도 0, 실패 0. 각 단계 6항목 게이트 승인 후 실행
- **감사표:** session-19 §11 에 gemma 라벨 · `schema.md` 정의 · 값별 판정 칸을 채웠다. **감사는 사용자 몫, 아직 안 함**

> **다음 세션이 먼저 읽을 곳:** §5(감사 대기) · §6(MARA 반입 세션에 넘길 것) · §7(병합 판단 때 볼 것)

---

## 1. 진입 조건 (session-19 §9.1)

| # | 결과 | 근거 |
|---|---|---|
| E1 | ✅ | `git worktree list`(worktree 에서) → 운영 폴더 `main` `54dad05`. 운영 폴더 파일을 **읽기만** 해서 `main` 트리 190 파일과 blob 해시 대조 → 수정 `docs/handoff/session-01.md` 1건, 누락 0, 미추적 0. 세션 끝에도 같다 |
| E2 | ✅ | 시작 10:58. 그날 자동 실행 08:10 exit 0 |
| E3 | ✅ | worktree 는 운영 폴더 밖 |
| E4 | ✅ | `sys.prefix` = worktree `.venv`. 벤더 SDK · langfuse 미설치 |
| E5 | ✅ | `.env` 는 사용자가 `VLLM_BASE` · `VLLM_MODEL` 두 줄만 뽑아 만들었다. 엔드포인트 해시 64자가 운영 상시 승인 기록 `fingerprint.endpoint_sha256` 과 같다. OS 환경변수에 `VLLM_*` · Vault · 벤더 키 없음 |
| E6 | ✅ | 패키지 7개 · 경로 상수 8개 · config · `.env` · 승인 파일 경로 전부 worktree. **공유 0** |
| E7 | ✅ | session-19 에서 허용 |
| E8 | ✅ (고친 뒤) | 처음 4 failed — `.env` 부재 3건(`test_rollback_path`) + ADR-026·027 Status 가 닫힌 어휘 밖 1건. Status 를 `Accepted` 한 낱말로 줄이고(`33d9e59`, 사용자 허락) `.env` 생성 뒤 964 passed |

session-19 §12 의 열린 질문 "ADR-027 §3.3 정의 문구"는 **닫았다**: MARA 반입 세션에서 S3 · S4 와 함께, 계약 버전 판단은 그 세션에서 MARA 계약 §9 기준(사용자). ADR-027 Implementation 에 반영.

## 2. 구현 (`ef1aaaf`)

| 파일 | 내용 |
|---|---|
| `export/backfill.py` (새) | `targets` · `plan` · `run` · `export` 4개 하위 명령 |
| `export/mara_backfill_targets.json` (새) | 37건 고정 목록 + 원천 파일 4개의 sha256. 실행마다 MARA 에서 다시 유도해 대조, 다르면 멈춤 |
| `export/runner.py` | `prompt_sha256` = LF 정규화 (ADR-027). v4 = `25b17f44…` |
| `tests/test_export_backfill.py` (새) | 20건 |

- **목록:** MARA 스냅샷 doc_id ∪ export 의 `indexable=true` doc_id = 스냅샷만 14 + export 만 21 + 양쪽 2 = **37**. 37 이 아니면 예외.
- **입력 (MARA 읽기 전용, 운영 보존소 읽지 않음):** 색인 가능 export 레코드가 있으면(23) 그 레코드에서 — source_name · 제목 · 날짜 · `lang` 그대로, 게이트 이력(`gate_model` · `gate_prompt_version`)을 옮기고 옮기지 않은 키(`is_relevant` · `reason` · `prompt_sha256` · `attempts` · `usage`)를 payload 에 적는다. 양쪽 2건은 jsonl text = 스냅샷 text 확인. 스냅샷에만 있으면(14) `mara_seed` 경로로, `gate: null`.
- **표시:** 항목별 `run_kind: "backfill"` · `backfill_run_id` · `backfill_origin`. 실행 매니페스트 `<보존소>/_runs/<run_id>.json` (보존소의 `*.json` 목록에 안 걸리게 한 단계 아래) — 실행 유형, 게이트 생략과 근거(ADR-026), 이력 보존 / null 목록, 대상 37, 모델, 프롬프트, LF 해시, 엔드포인트 해시, 호출 상한·사용량, 건별 결과. **export 레코드와 `CONTRACT_VERSION` 은 그대로(1.0).**
- **코드로 막은 것:** 운영 보존소(또는 그 아래) 거부 · 07:10 ~ 09:10 시작 거부 · 이미 보존된 doc_id 재호출 거부 · 최악 호출(2 × 건수)이 상한 초과면 0건 호출로 거부 · 관측 큐를 열지 않음(`record_unknown_companies` 를 부르지 않는다. 테스트가 `JSONLObserver` · `observer_from_config` 를 막은 채 미등록 기업 응답으로 확인).
- **선택 export:** 보존소가 정확히 37건 · 전부 backfill · 추출 모델 1개 · 프롬프트 해시 = LF 실측이 아니면 아무것도 쓰지 않는다. 쓴 뒤 계약 §12.2 자체검사(`conformance.run_checks`, 주입·시드 = 스냅샷 arXiv 16, 기대 37).

## 3. 실행

| 실행 | 시작 | 대상 | 성공 | 호출 / 상한 | 재시도 | 토큰 입력 / 출력 |
|---|---|---|---|---|---|---|
| 소표본 `backfill-20261002-112509` | 11:25 | ARCANE(GS-004, 스냅샷) · MUSE(cs.CL 피드) · GeekNews 1 | 3/3 | 3 / 6 | 0 | 9,215 / 710 |
| 본 실행 `backfill-20261002-113334` | 11:33 | 나머지 34 (`--rest`) | 34/34 | 34 / 68 | 0 | 104,347 / 7,945 |

보존소: `data/backfill/mara-37/extractions/` (worktree, gitignore). 37건 전부 `gemma-4-31B-it`.

## 4. Export (11:45, LLM 호출 없음)

| 파일 | 줄 | sha256 |
|---|---|---|
| `data/backfill/mara-37/corpus/arxiv/ontology-20261002-114500.jsonl` | 24 | `c6cb97ea9d13f30f25c5bef0c5e2b099059d138bfa5d81fd28d4cecae6b4bc78` |
| `data/backfill/mara-37/corpus/news/ontology-20261002-114500.jsonl` | 13 | `b1d05b5844821b1cd449377ec046aba9f02af2c19a8895d608b5133766302926` |
| `data/backfill/mara-37/corpus/ontology-20261002-114500.manifest.json` | — | `57ec157f692a992cbb8c96774aea5dbae98cd5d5695dfcd3ec36a0118ae5b8d7` |

- **37줄** (arXiv 24 = 스냅샷 16 + 피드 8, news 13). `counts.total=37 · indexable=37 · excluded_empty_text=0`.
- **§12.2 자체검사: PASS 12 / FAIL 0 / PENDING 1** (13 한국어 색인 — MARA 인덱스 필요). 5번(병합)은 16건 원문이 스냅샷에서 왔으므로 "text 유지"가 자명하게 통과한다는 한계가 판정에 함께 적힌다. 6번 골든셋 expected_doc_ids 24건 전부 보존.
- 레코드: 키 17개(계약 1.0 그대로), `prompt_sha256 = 25b17f44…`, 게이트 이력 23건 `claude-haiku-4-5-20251001` · `relevance_gate.v1.md`, 14건 `null` / `null`.
- 이 파일들은 커밋하지 않았다(`data/`). MARA 반입은 MARA 세션 몫이다.

### 4.1 Opus ↔ gemma (원래 MARA 에 있던 23건) — 두 모델의 일치이지 정확도가 아니다

| 범위 | `release_type` | `tech_domains` 완전 일치 | `Agent` 포함 여부 | 겹침 |
|---|---|---|---|---|
| 전체 23 | 20/23 | 9/23 | 21/23 | 23/23 |
| arXiv 10 | 9/10 | 6/10 | 10/10 | 10/10 |
| news 13 | 11/13 | 3/13 | 11/13 | 13/13 |

- `release_type` 불일치: `arXiv:2609.19088v1` MUSE Paper → Benchmark/Report (session-19 §5.2 의 그 건) · `news:42f467857601213c` Community/Discussion → Benchmark/Report · `news:dfff960c99b416b4` Benchmark/Report → OpenSource
- `Agent` 불일치 2건은 둘 다 gemma 가 뺐다: `news:0252e9379cd9f923` · `news:1047cd7bf162d6e0`
- `tech_domains` 는 주로 gemma 가 덜 붙인다(news 에서 `LLM` 이 빠진 것 5건)

## 5. 감사 대기 (사용자) — session-19 §11

14건(정답 10 + 비정답 4)의 gemma 라벨과 `schema.md` 정의(쓰인 값 11개 + `Agent` 분류 기준)를 표 위에 실었다. 판정 칸은 `release_type` · `Agent` · 나머지 값 ① ② · 빠진 값으로 나눠 **맞음 / 틀림 / 애매**를 적게 비워 뒀다. 레코드는 고치지 않는다(ADR-026 결정 3).

- 14건 전부 `Agent`. `release_type` Paper 8 · Benchmark/Report 5 · OpenSource 1
- `Benchmark/Report` 정의는 "**공식 조직**이 낸 벤치마크 결과"다 — 대학·연구실의 벤치마크 논문을 어느 쪽으로 볼지가 판정이 갈릴 자리(정의만 옮겼고 판단은 안 했다)

## 6. 관찰 — 기록만, 고치지 않음

### 6.1 `companies.role` 이 `""` 와 `null` 로 갈린다

- **계약 1.0 위반은 아니다.** 계약 §3.2 는 `role: string|null`. "누락은 빈 문자열이 아니라 null" 규칙은 §3.1 `published_at` 행과 §12.2-11 에만 있다. MARA 로더(`contract_import.py`)는 `role` 을 검사하지 않는다(기업은 `canonical` / `raw` 만 읽는다). 자체검사도 보지 않는다.
- **규모:** 37건의 기업 29개 중 **11개가 `role: ""`** (4건 — `arXiv:2404.01023v1` 7 · `arXiv:2410.09114v2` 2 · `news:0252e9379cd9f923` 1 · `news:1047cd7bf162d6e0` 1), `null` 0개. 옛 Opus 레코드는 `null` 2 · 문자열 14 · `""` 0.
- 같은 "역할 없음"이 코퍼스 안에서 두 표기가 된다. 정할 곳 후보: 계약 문구(MARA 반입 세션) 또는 생산자 `record.py` 의 `""` → `null` (형식 변경이라 계약 먼저).

### 6.2 기업 칸에 모델·제품명

gemma 가 `companies` 에 기업이 아닌 것을 넣었다: GPT-3.5 · GPT-3.5 Turbo · GPT-4 · GPT-4 Turbo · Google Bard · LLAMA (`arXiv:2404.01023v1`), GPT-4o · Claude 3.5 Sonnet (`arXiv:2410.09114v2`), Claude (`news:0252e9379cd9f923`, 본문에는 "Claude Code"·"클로드"만 있고 Anthropic 은 없다), `ai-safety-institute` (`arXiv:2410.09024v3`). 감사 대상 필드(`release_type` · `tech_domains`) 밖이다. 운영 실행이었다면 미등록 기업 큐에 들어갔을 값들이고, 이번에는 큐에 아무것도 쓰지 않았다.

### 6.3 소표본 GeekNews 건 (본문 175자, 중간에서 잘림)

`summary` · `release_type`(제목 "Show GN") · `prior_art` 는 본문 근거가 있다. `impact` 근거의 "개인이 구현한"은 본문에 없다(본문은 "사내에서 협업하다… 클로드를 이용하여 100% 구현"). `Agent` 는 Opus 와 달리 빠졌다.

## 7. 병합 판단 때 볼 것 (session-19 §12 그대로 + 이번 것)

- ① ADR-027: `prompt_sha256` 변경이 `pipeline/runner.py` 를 거쳐 운영 보존소 기록을 바꾼다. 자동 실행 승인 대조는 이를 잡지 않는다. ② `export/runner.py` 를 `GUARD_FILES` 에 넣을지.
- 🆕 `export/backfill.py` 는 운영 경로에서 import 되지 않는다(`pipeline/` 이 쓰지 않음). 병합해도 자동 실행 동작은 ① 말고는 바뀌지 않는다.
- 🆕 ruff 가 기존 파일(`export/mara_seed.py` · `export/replay.py` · `export/urls.py`)에서 DTZ011 · UP017 · BLE001 을 잡는다 — 이번 범위 밖이라 그대로 뒀다.

## 8. MARA 반입 세션에 넘길 것

1. 옛 jsonl(`ontology-20260917-120733`) 제거 후 새 export 커밋 — 안 빼면 §7-2 중복 실패(session-19 §2.2 S6). manifest 는 어휘 이력이라 남긴다(MARA ADR-019)
2. 층 B `anchor_meta` 재선언 (MARA ADR-022)
3. 계약 문구: S3(`published_at` = UTC 날짜) · S4(`lang` 선언 서술) · §3.3(`prompt_sha256` 정의 = LF 정규화, ADR-027 대응표). 계약 버전은 MARA 계약 §9 기준으로 그 세션에서 판단
4. §6.1 `role` 표기를 계약에서 정할지
5. 감사(§5) 결과 — 정확도 기록 뒤에 측정

## 세션 종료 상태

- **운영 폴더 `C:\ai-news-ontology-1`:** 체크아웃 **`main`** (`54dad05`). `.git/HEAD` 와 `git worktree list`(worktree 에서 실행)로 읽기만 했다. 작업본은 세션 시작과 같다(수정 `session-01.md` 1건). 운영 폴더에서 git 명령 0
- **작업 스케줄러:** `ai-news-ontology daily` · 상태 Ready · `C:\ai-news-ontology-1\.venv\Scripts\python.exe -m pipeline scheduled` · 작업 디렉터리 `C:\ai-news-ontology-1` · 다음 실행 2026-10-03 08:10 → **`main` 코드로 돈다**
- **worktree:** `C:\ai-news-ontology-backfill` (`feat/mara-backfill`) 그대로 둔다 — 보존소·export·`.venv`·`.env` 가 여기 있다
- MARA `git status` 깨끗함(읽기만)

## 주의

- 🆕 bash 에서 `python - <<'EOF' < /dev/null` 처럼 **빈 heredoc 뒤에 다른 명령을 이으면** python REPL 이 떠서 멈춘다(120초 타임아웃, 출력 수십 MB). 스크립트 파일로 쓴다
- 🆕 Windows 에서 `Path.write_text` 는 기본으로 CRLF 를 쓴다. 이 레포 파일을 파이썬으로 고칠 때는 `newline="\n"` 이나 `write_bytes` 를 쓴다(`export/backfill.py` 는 그렇게 했다)
- `docs/handoff/session-01.md` 포매터 변경은 여전히 커밋하지 않는다
