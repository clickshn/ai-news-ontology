# ADR-022: 파이프라인 재실행 중복 판정은 `doc_id` 하나로, 단계 완료는 그 산출물을 가진 층이 판정하고 재시도는 영속 산출물이 없는 첫 단계부터 한다

- **Status:** Accepted
- **Date:** 2026-09-29
- **Decision:** 수집→게이트→추출→보존→적재 오케스트레이션에서 같은 기사의 식별 키는 계약 §4 의 `doc_id` 하나로 두고, 게이트 완료는 신규 원장, 추출 완료는 보존소, 적재 완료는 Vault 색인이 판정한다. 각 단계는 다음 단계 전에 산출물을 영속하며, 실패 시 재시도는 영속된 산출물이 없는 첫 단계부터 시작한다
- **Scope:** `pipeline/` (신규: `ledger.py` · `runner.py`) · `export/runner.py` (항목 처리 로직 공유 함수화) · `obsidian_writer/` (Vault 색인) · `data/pipeline/`
- **Decision Source:** Human

---

## Context

### Problem

수집→게이트→추출→정규화→적재를 한 번에 잇는 실행 경로가 없다. 진입점이 `collectors.rss` · `extraction.extractor` · `eval.runner` 로 갈려 있고 `obsidian_writer.write_note` 는 테스트 밖에서 호출된 적이 없다. #33003 이 추출까지만 돌고 노트로 남지 않은 것이 실제 사례다. 배선하려면 두 가지를 먼저 정해야 했다: 같은 기사를 다시 만났을 때 무엇을 기준으로 건너뛸지, 실패하면 어디서부터 다시 할지.

코드를 읽자 세 층이 이미 서로 다른 답을 내고 있었다.

- **Vault 층의 동일성 판정은 날짜 범위로만 동작한다.** `write_note` 는 파일명을 `processed_at`(기본값 = 지금)의 날짜로 만들고, 같은 파일명(과 `-2`, `-3`)일 때만 `source_url` 을 비교한다. 다음 날 재실행하면 파일명이 달라 같은 기사 노트가 하나 더 생긴다 — D-028 의 `skip` 이 같은 날에만 성립한다.
- **Vault 는 원본 `source_url` 문자열을, 보존소는 정규화 URL 의 `doc_id` 를 비교한다.** 같은 기사가 추적 파라미터를 달고 들어오면 두 층이 갈린다.
- **게이트 스킵과 추출 실패는 영속되지 않는다.** `export.runner.run_collect` 의 `seen` 은 메모리에만 있다. 재실행마다 스킵 항목을 다시 게이트에 넣고, 스키마 실패 항목은 temperature 0 이라 같은 실패를 반복하며 매번 비용을 낸다. `skips.jsonl` 은 감사 로그(D-036)이지 상태가 아니다.

### Constraints

- `doc_id` 규칙은 출력 계약 §4 이고 MARA 와 공유한다. 이쪽에서 정규화 규칙을 늘릴 수 없다.
- Vault 는 사용자 실데이터다. 기본 충돌 정책 `skip` 을 유지한다 (D-028, ADR-010).
- 비싼 결과를 검증 안 된 변환 코드와 같은 트랜잭션에 두지 않는다 (D-052, ADR-015).
- 기존 진입점(`export.runner` 의 plan/collect/export)을 없애지 않는다 — 형식 오류로 다시 도는 것과 추출을 다시 도는 것이 같은 이름을 갖지 않게 나눈 것이다 (계약 §12.3).
- 보존소(`data/extractions/`)에 `extraction: null` 인 항목을 넣으면 `export.exporter.build_records` 와 계약 레코드가 깨진다.
- 부분 실패가 전체를 멈추지 않는다 (collectors 의 per-source 격리와 같은 원칙).

## Decision

### Selected

- **Technology:** 새 의존성 없음. 원장은 doc_id 당 JSON 파일 1개(원자적 쓰기, upsert)
- **Architecture:**
  - **키:** `export/doc_id.py: doc_id_for` 하나. 제목 유사도는 쓰지 않는다.
  - **단계 완료 판정 층:**

    | 단계 | 판정 층 | 비고 |
    |---|---|---|
    | 게이트 | 신규 원장 `data/pipeline/ledger/{doc_id}.json` | 판정과 게이트 프롬프트·모델을 기록. 프롬프트·모델이 바뀌면 다시 판정 |
    | 추출 | 보존소 `data/extractions/` | 옛 프롬프트·모델 결과는 재추출하지 않고 stale 로 보고만 한다 |
    | 적재 | Vault 색인 — 노트 frontmatter 의 `source_url` → `doc_id` | 날짜와 무관 |

  - **Vault 는 상태 저장소가 아니다.** 원장에 `written` 이 있는데 노트가 없으면 사용자가 지운 것으로 보고 다시 만들지 않는다. 되살리려면 `--rewrite-missing` 을 명시한다.
  - **재시도 지점:**

    | 실패 | 기록 | 다음 실행 |
    |---|---|---|
    | 수집(피드 다운) | 없음 (collectors 로그) | 피드에 남아 있으면 다시 잡힌다 |
    | 게이트 오류 | 원장 `gate_error` | 게이트부터 |
    | 게이트 스킵 | 원장 `skipped` + `skips.jsonl` | 다시 판정하지 않는다 |
    | 추출 스키마 실패 | 원장, 횟수 누적 | 2회면 격리 (temp 0 이라 반복된다) |
    | 추출 전송 오류 | 원장, 횟수 세지 않음 | 추출부터 |
    | 적재 실패 | 원장 `load_failed` | 보존소에서 적재만 (LLM 0건) |

  - 한 LLM 단계에서 전송 오류가 3회 연속이면 그 실행의 LLM 단계를 멈추고(차단기), 이미 보존된 것의 적재는 계속한다.
- **Implementation:**
  - 신규 `pipeline/` — `python -m pipeline plan`(API 없음) / `run`(전 단계) / `load`(보존소 → Vault, API 없음).
  - `export.runner` 의 항목별 게이트→추출→보존 로직은 복사하지 않고 공유 함수로 뽑아 두 진입점이 쓴다. 기존 진입점과 동작은 유지한다.
  - 상한은 코드로 고정한다: `--gate-limit SOURCE=M` · `--extract-limit SOURCE=N` + 전체 하드캡 (승인 게이트 5번을 소스별·단계별로 받는 자리).
  - 노트의 `processed_at` 은 보존소의 `extracted_at` 을 쓴다 — 재적재가 같은 노트를 만든다. frontmatter 키는 바꾸지 않는다.
  - `run` 은 그 실행에서 보존된 항목(과 원장상 적재 실패 항목)만 적재한다. 기존 보존소 30건(`claude-opus-5`, MARA Session 0.5a 사고 산출물)은 적재 대상에서 제외한다.

## Rationale

1. `doc_id` 는 키이고 보존소·Vault 는 층이다. 세 가지는 대안이 아니었다 — 키를 하나로 고정하고, "무엇이 끝났는가"는 그 단계의 산출물을 실제로 가진 층에 물어야 층 사이의 불일치가 판정에 섞이지 않는다.
2. 단계 경계마다 산출물을 먼저 영속하면 실패한 단계보다 앞의 비용을 다시 내지 않는다. D-052 가 보존소를 LLM 쪽과 변환 쪽의 경계로 만들어 둔 구조를 적재 단계까지 연장한 것이다.
3. 스키마 실패와 전송 오류를 나눈 것은 원인이 다르기 때문이다. temperature 0 에서 같은 입력의 스키마 실패는 반복되므로 횟수로 격리하고, 전송 오류는 입력과 무관하므로 격리 사유가 아니다 — 장애 한 번에 기사가 격리되면 안 된다.
4. Vault 존재를 상태로 쓰지 않는 것은 사용자의 삭제를 의사로 존중하기 위해서다. 노트가 없어졌다는 이유로 파이프라인이 다시 만들면 사용자의 편집을 지우지 않는다는 D-028 의 취지와 어긋난다.
5. 기존 30건 제외는 사용자 결정이다. MARA 0.5a 사고 산출물이라 Vault 에 넣으면 노트에 `claude-opus-5` 가 섞여 "이 노트가 어느 체제에서 나왔나"가 흐려지고, 이번 실행의 목적(오케스트레이션 검증)이 무엇을 확인한 것인지 불분명해진다.

## Evidence

- **Production Data:** 보존소 `data/extractions/` 30건 전부 `extract_ontology.v4.md` · `claude-opus-5` (GeekNews 10 · arXiv 8 · HF Blog 5 · OpenAI 3 · MARA seed 2 · DeepMind 2). 실제 Vault 디렉터리의 노트는 `환영합니다!.md` 1개 — README 와 이전 기록이 말하는 #33001 노트는 이 경로에 없다. `observability/logs/` 는 존재하지 않는다 (2026-09-29 확인)

## Alternatives

### Vault 노트 존재로 전 단계 판정

- **Pros:** 사용자에게 보이는 결과와 판정이 일치한다
- **Cons:** 사용자 데이터를 파이프라인 상태로 쓴다. 사용자가 지운 노트를 재추출·재생성한다
- **Rejected because:** Vault 는 사용자 실데이터이고 상태 저장소가 아니다

### 보존소 존재만으로 전 단계 판정

- **Pros:** 새 저장소가 필요 없다
- **Cons:** 게이트 스킵·추출 실패를 표현할 수 없다
- **Rejected because:** 스킵·실패를 보존소에 넣으려면 `extraction: null` 항목이 생기고 exporter 와 계약 레코드가 깨진다

### 단일 트랜잭션 파이프라인 (게이트→추출→적재를 한 흐름으로, 중간 영속 없이)

- **Pros:** 구현이 단순하다
- **Cons:** 적재 단계의 오류가 이미 낸 LLM 비용을 날린다
- **Rejected because:** D-052 위반 — 비싼 결과를 검증 안 된 코드와 같은 트랜잭션에 둔다

### 제목 유사도로 중복 판정

- **Pros:** URL 이 달라도 같은 기사를 잡을 수 있다
- **Cons:** 서로 다른 기사가 합쳐질 수 있다
- **Rejected because:** 잘못 합치는 비용이 중복 노트보다 크고, `doc_id` 규칙은 계약 §4 라 이쪽에서 늘릴 수 없다

## Consequences

### Positive

- 교차일 재실행에서 같은 기사 노트가 중복 생성되지 않는다
- 게이트 스킵·스키마 실패 항목에 재실행마다 비용을 내지 않는다
- 적재 실패는 LLM 호출 없이 복구된다 (`pipeline load`)
- 파이프라인 산출물이 보존소를 거치므로 MARA export 입력에도 그대로 쌓인다

### Negative

- 로컬 상태가 한 곳 늘어난다 (`data/pipeline/`). 게이트 근거 문장·오류 메시지가 평문으로 남아 governance 로컬 산출물 표에 추가해야 한다
- 보존소의 stale 결과는 자동으로 갱신되지 않는다 — 재추출은 명시적 판단이 필요하다

### Risks

- 원장과 보존소·Vault 가 수동 삭제로 어긋날 수 있다 (예: 보존소만 지움). 판정 층이 단계마다 다르므로 어긋나도 각 단계는 자기 층을 믿고 진행하지만, 원장만 보고 상태를 읽으면 틀린다
- 사용자가 노트 frontmatter 의 `source_url` 을 지우면 Vault 색인에서 빠져 `--rewrite-missing` 시 중복이 생길 수 있다 (obsidian_writer 알려진 한계와 같다)

## Implementation

- [ ] `pipeline/ledger.py` — doc_id 당 상태 파일, 원자적 upsert
- [ ] `export/runner.py` — 항목 처리 공유 함수 추출, `run_collect` 동작 유지
- [ ] Vault 색인 (`source_url` → `doc_id`), 날짜 무관 skip
- [ ] `pipeline/runner.py` — plan / run / load, 소스별·단계별 상한, 차단기, 실행 요약 `data/pipeline/runs/{run_id}.json`
- [ ] 테스트 — 부분 실패(소스 다운 · 게이트 오류 · 스키마 실패 격리 · 전송 오류 · 적재 실패 복구 · 교차일 재실행), 전부 `tmp_path`
- [ ] 문서 — governance 로컬 산출물 표, README 결정 로그, `pipeline/README.md`

## Reversibility

- **Reversible:** Yes
- **Rollback:** `pipeline/` 과 `data/pipeline/` 을 지우면 기존 진입점(`export.runner`)만 남는다. 공유 함수 추출은 `run_collect` 동작을 바꾸지 않으므로 되돌릴 필요가 없다
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-010 (Vault 충돌 정책) · ADR-015 (API 결과 선영속) · ADR-017 (벤더 호출 게이트) · ADR-018 (내부 vLLM)
- **Documentation:** `export/doc_id.py` · `export/store.py` · `obsidian_writer/writer.py` · `docs/handoff/session-14.md` · 기준점 HEAD `6218021`
