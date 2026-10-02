# ADR-026: MARA 코퍼스 37건 전체의 온톨로지 메타를 한 모델(gemma-4-31B-it · v4)로 다시 추출하고, 재게이트 없이 격리된 backfill 보존소에서 그 37건만 내보낸다

- **Status:** Proposed
- **Date:** 2026-10-02
- **Decision:** MARA 의 현재 색인 코퍼스 37건(스냅샷 arXiv 16 + 계약 export 의 색인 가능 23 − 병합 2)의 온톨로지 메타를 `gemma-4-31B-it` + `extract_ontology.v4.md` 로 **전부** 다시 추출한다. 게이트는 다시 태우지 않는다 — 문서 구성 37건을 고정하고 메타만 바꾸며, 레코드에는 게이트를 거치지 않았음을 `gate_model: null` 로 남긴다. 실행은 운영 체크아웃과 분리된 git worktree 의 별도 보존소에서 backfill 로 표시해 하고, 운영 관측 큐에 섞지 않는다. export 는 그 보존소에서 **MARA 코퍼스 doc_id 37건만** 내보내고 건수 37 을 검증한다. 새로 라벨이 붙는 14건은 MARA 측정 전에 사람이 감사(정확도 기록)만 하고 라벨은 고치지 않는다
- **Scope:** `export/` (backfill 입력 구성·추출·선택 export — worktree 브랜치에서 구현) · 별도 보존소 · MARA 반입용 export 1건. 운영 파이프라인(`pipeline/`)·운영 보존소·Vault 는 범위 밖
- **Decision Source:** Human

---

## Context

### Problem

MARA v1.2 의 필터 정책 판정(MARA ADR-029·030)은 "strict 는 메타 없는 정답을 지우고, null-통과는 메타 없는 무관 문서를 끌어올린다"로 끝났고, 근본 원인을 코퍼스 37건 중 **온톨로지 메타가 없는 14건**(전부 스냅샷 arXiv)으로 지목했다. 층 B 9케이스의 정답 문서 10건이 전부 이 14건 안에 있다.

이 14건이 메타를 갖지 못한 것은 추출을 한 적이 없어서다. 생산자는 arXiv 최근 피드만, MARA 는 주제 질의 결과를 받아 구조적으로 겹치지 않고(MARA ADR-018 Amendment), 0.5a 의 주입은 중복 검증용 2건뿐이었다. 이 레포의 보존소(232건)와 Vault 에 14건의 대응 항목은 0건이다(session-19).

MARA 는 14건만 채우는 방안을 이미 기각했다(MARA ADR-022 Alternatives (가)). 14건만 다른 모델로 채우면 `extraction_model` 이 문서군과 1:1 로 겹쳐(Opus 23 = 비스냅샷, gemma 14 = 스냅샷) 층별 차이를 모델 탓인지 문서군 탓인지 가를 수 없다. 그 ADR 이 적은 재검토 조건이 "전체 37건을 같은 모델로 재추출할 수 있게 되면, 필터 배선과 다른 세션에서 코퍼스 단독 변경으로"다.

### Constraints

- 운영 체크아웃(`<운영 폴더>`)은 매일 08:10 자동 실행 중이다. 작업 스케줄러는 브랜치가 아니라 **폴더**를 실행하므로(`.venv\Scripts\python.exe -m pipeline scheduled`, 작업 디렉터리 = 운영 폴더) 그 폴더의 코드는 바꾸지 않는다.
- 편집 가능 설치(`.venv` 의 editable finder)가 `collectors` · `eval` · `extraction` · `observability` · `obsidian_writer` 를 운영 폴더로 매핑한다. 레포의 모든 데이터 경로는 `Path(__file__)` 기준이라, 모듈이 운영 폴더에서 import 되면 로그·`.env`·config 가 운영 쪽으로 간다.
- MARA 로더는 `data/corpus/<source>/*.jsonl` 을 **전부** 합치고, 스냅샷↔export 외의 `doc_id` 중복은 실패시킨다(계약 §7-2). 기존 30건과 같은 `doc_id` 를 담은 새 export 는 옛 jsonl 을 빼야 들어간다.
- 계약 1.0 안에서 된다. `extraction_model` 값 변경과 `gate_model: null` 은 형식 변경이 아니다(계약 §3.3 은 "게이트를 거치지 않았으면 null"을 허용한다).
- 기존 `export` 는 보존소 전체를 내보낸다. 운영 보존소에는 파이프라인 실행분이 섞여 있다(232건 중 202건).

## Decision

### Selected

- **Technology:** 내부 vLLM `gemma-4-31B-it`, 프롬프트 `extract_ontology.v4.md`(현재 기본값). 게이트 호출 없음.
- **Architecture:** 운영 폴더 밖의 git worktree + 그 worktree 전용 가상환경·`.env`·보존소·관측 경로. 입력은 MARA 레포를 **읽기만** 해서 만든다 — 스냅샷 `data/corpus/arxiv/*.json` 14건 + 계약 export jsonl 의 색인 가능 23건. 운영 보존소는 입력원으로 쓰지 않는다.
- **Implementation:**
  - 37건 doc_id 목록을 MARA 코퍼스에서 결정적으로 유도하고 파일로 고정한다. 유도 결과가 37이 아니면 멈춘다.
  - 보존 레코드에 backfill 표시를 남기고, 게이트 필드는 비운다(레코드의 `gate_model` · `gate_prompt_version` = `null`).
  - 관측은 운영 큐(`observability/logs/`)에 쓰지 않는다.
  - export 는 backfill 보존소에서 고정 목록 37건만 고르고, 건수와 doc_id 집합이 목록과 같지 않으면 실패한다.
  - 사람 감사용 표(doc_id · 제목 · 초록 앞부분 · gemma 라벨)를 14건에 대해 만든다. 감사 결과는 기록만 하고 레코드를 고치지 않는다.

## Rationale

1. MARA ADR-022 (가)의 기각 사유 두 개 중 "실행 불가"는 vLLM 이전(ADR-018)으로 해소됐고, "공선성"은 37건을 한 모델·한 프롬프트로 통일해야만 사라진다. 14건만 채우는 경로는 그 기각을 우회하는 것이 된다.
2. 문서 구성을 고정해야 MARA 의 재측정이 "메타가 생겼다" 하나만 바꾼 비교가 된다. 재게이트에서 스킵이 하나라도 나면 코퍼스 구성까지 바뀌어 원인을 가를 수 없다.
3. 라벨을 고치지 않는 감사는 측정 대상을 "현재 파이프라인이 붙이는 라벨 + 필터"로 유지하면서, 그 라벨의 정확도를 따로 수치로 남긴다. MARA strict 재측정의 결과는 정답 문서 10건에 `Agent` 가 붙는가로 거의 정해지므로(session-19 §5.2), 정확도를 모르고 측정하면 라벨 정확도를 필터 효과로 읽게 된다.
4. 입력을 MARA 레포에서 만들면 추출 입력이 MARA 가 색인하는 텍스트와 같아지고, worktree 가 운영 보존소를 읽을 이유가 없어진다.

## Evidence

- **Experiment:** MARA jsonl 30건 ↔ 운영 보존소 원본 `raw_item` 대조 — text · title · 발행일 · 소스명 **불일치 0/30**. 병합 2건(`2412.05449v1` · `2605.21404v1`)의 jsonl text = 스냅샷 text (2/2). MARA 색인 코퍼스 = 스냅샷 16 ∪ 색인 가능 export 23 = **37** (session-19, 읽기 전용 스크립트).
- **Experiment:** 같은 30건의 Opus ↔ gemma 라벨 일치 (`data/replays/main`, 2026-09-18) — `release_type` 26/30, `tech_domains` 완전 일치 10/30, `Agent` 포함 여부 28/30. 두 모델 사이의 일치이고 정확도가 아니다.
- **Cost:** 추출 37회(게이트 0). 스키마 시도가 항목당 최대 2회라 상한 74회. 입력 약 126k / 출력 약 11k 토큰 추정(보존소 실측 평균 기준). 내부 vLLM 이라 벤더 청구 0.

## Alternatives

### A. 메타 없는 14건만 채운다

- **Pros:** 생산자 코드 변경 없이 된다(`collect --store-dir … --seed-from-mara …`). 호출 최대 28. MARA 쪽은 jsonl 추가만 하면 된다.
- **Cons:** 추출 모델이 문서군과 1:1 로 겹친다.
- **Rejected because:** MARA ADR-022 (가)가 공선성으로 기각한 바로 그 경로다.

### B′. 14건만 새로 추출하고 기존 30건은 2026-09-18 replay 결과를 재사용한다

- **Pros:** 호출이 14건분으로 줄어든다.
- **Cons:** 모델은 같아도 추출 시점·회차가 문서군과 다시 겹친다(14 = 새 회차, 30 = 옛 회차). replay 행을 레코드로 바꾸는 코드가 따로 필요하다.
- **Rejected because:** 회차가 문서군과 겹쳐 공선성이 다른 모양으로 남는다.

### 기존 30건을 gemma 게이트로 다시 태운다

- **Pros:** 37건의 provenance 가 게이트까지 한 모델로 통일된다.
- **Cons:** 호출 +30. 스킵이 나오면 그 문서가 코퍼스에서 빠진다.
- **Rejected because:** 문서 구성 37건을 고정하고 메타만 바꾼다는 조건을 깬다.

## Consequences

### Positive

- MARA 코퍼스의 메타가 한 모델·한 프롬프트로 통일되고, 메타 결측이 14 → 0 이 된다.
- 운영 자동 실행과 데이터를 공유하지 않는다 — 실패해도 운영 보존소·큐·Vault 가 오염되지 않는다.
- export 선택과 건수 검증이 생겨, 보존소에 무엇이 섞여 있든 MARA 에 나가는 범위가 고정된다.

### Negative

- 기존 export 의 색인 불가 7건(본문 0자 뉴스)은 새 export 에 들어가지 않는다. MARA manifest 의 `total` 30 → 37, `excluded_empty_text` 7 → 0 이 된다. 색인에는 영향이 없다(원래 색인되지 않았다).
- 기존 23건의 Opus 라벨이 gemma 라벨로 바뀐다. MARA v1.2 의 층 A 측정치는 옛 라벨 위에서 잰 값이 된다.
- MARA 쪽에서 옛 jsonl 을 빼고 골든셋 `anchor_meta`(측정 전에 박는 값)를 다시 선언해야 한다 — MARA 세션의 일이다.
- 레코드의 `gate_model` 이 37건 모두 `null` 이 된다. 30건이 원래 Haiku 게이트로 들어왔다는 이력은 레코드에서 사라지고 이 ADR 과 운영 보존소에만 남는다.

### Risks

- 14건과 같은 성격(에이전트·멀티에이전트·벤치마크 논문)의 확정 라벨이 생산자 골든셋에 0건이다. 감사가 그 빈자리를 메우지만 라벨을 고치지는 않는다.
- 벤치마크 논문 7건에서 `Paper` / `Benchmark/Report` 가 흔들릴 수 있다(Opus→gemma 불일치 중 arXiv 1건이 정확히 이 쌍이었다).
- worktree 가 운영 `.venv` 를 쓰면 editable finder 를 통해 운영 폴더 모듈이 import 될 수 있다 — 실행 전 경로 점검으로 막는다.

## Implementation

- [ ] worktree 생성(운영 폴더 밖) · worktree 전용 가상환경 · `VLLM_BASE` / `VLLM_MODEL` 만 담은 `.env`
- [ ] 실행 전 경로 점검: 모든 패키지 `__file__`, 보존소·관측·config·`.env` 해석 경로가 운영 폴더 밖인지 출력하고 하나라도 겹치면 멈춘다
- [ ] 37건 목록 유도·고정, 입력 구성(MARA 읽기 전용), backfill 표시, 게이트 생략, 관측 분리
- [ ] 선택 export + 건수·집합 검증, mock 테스트
- [ ] 소표본 3건 실행(별도 승인) → 형식 확인 → 나머지 34건 실행(별도 승인)
- [ ] 14건 감사 표 작성 · 사용자 감사 · 정확도 기록

## Reversibility

- **Reversible:** Yes
- **Rollback:** MARA 쪽에서 새 jsonl 을 빼고 옛 jsonl(`ontology-20260917-120733`)을 되돌리면 이전 코퍼스로 돌아간다. 생산자 쪽은 worktree 브랜치를 병합하지 않으면 운영에 흔적이 없다.
- **Migration Cost:** Medium — MARA 측정치를 새 코퍼스 위에서 다시 내야 한다.

## References

- **Related ADR:** ADR-018(vLLM 이전) · ADR-015(응답 먼저 보존) · ADR-027(`prompt_sha256` 정의) · MARA ADR-022(14건 채우기 기각과 재검토 조건) · MARA ADR-029·030(필터 정책 판정) · MARA ADR-018(출력 계약)
- **Documentation:** `docs/handoff/session-19.md` §3 ~ §5 · MARA 계약 `docs/contracts/ai-news-ontology-export-v1.md` §3.3 · §4.3 · §7
