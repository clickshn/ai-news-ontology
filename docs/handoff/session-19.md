# Session 19 핸드오프 — 🔵 조사만: MARA 층 B 메타 채우기 준비 (생산자 쪽 현황)

- **날짜:** 2026-10-02
- **범위:** MARA v1.2 의 "메타 결측 14건"(MARA ADR-029·030, README §5.11)을 채우기 위한 생산자 쪽 현황 조사. **코드 변경 0 · LLM 호출 0 · MARA 레포 쓰기 0**
- **브랜치:** `chore/mara-meta-survey` (main `54dad05` 에서 분기). 커밋 1개(이 handoff), **push 안 함**
- **테스트:** 돌리지 않음 (코드 변경 없음)
- **읽은 것:** 이 레포의 `config.yaml` · `export/` · 보존소(`data/extractions/`, 읽기만) · `data/replays/` · 자동 실행 상태. MARA 의 계약 문서 · `data/corpus/` · `docs/eval/golden-set.json` · ADR-004·022·030 · `src/tools/contract_import.py` (전부 읽기만, MARA `git status` 깨끗한 것 확인)
- **3부 (같은 날):** ADR-026·027 Accepted, 라벨 수정 기각 사유 추가, 결정 2 정정(게이트 이력 보존 + 별도 스킵 필드 → 계약 1.1 사안), E7 허용, `GUARD_FILES` 편입 여부를 병합 판단에 추가 → §7-2 · §9 · §12
- **4부 (같은 날):** 게이트 스킵은 **레코드 필드로 넣지 않는다**, 계약 1.0 유지 — 채우기 전용 보존소의 실행 매니페스트에 남긴다. E9 철회. 3부의 "별도 필드" 안은 대체됐다 → §7-2 · §9 · §12
- **ADR (`adr-skill:adr-recorder`, 3부에서 Accepted):** **ADR-026**(MARA 코퍼스 37건 메타를 한 모델로 backfill) · **ADR-027**(`prompt_sha256` = LF 정규화, 옛 값 대응표). 결정은 사용자, 2026-10-02 (§7)
- **2부 (같은 날):** 사용자 결정 7건 + 운영 제약 → §7 ~ §11. 코드 변경 여전히 0

> **다음 세션이 먼저 읽을 곳:** **§9(진입 조건)**, §10(공유 경로 점검), §11(감사 표). §1 ~ §6 은 결정 전 조사 기록이다.

---

## 1. 현재 상태 요약

| 항목 | 상태 |
|---|---|
| 활성 소스 (6) | arXiv cs.CL (Atom API, `feed_kind: sample`) · OpenAI News · Google DeepMind Blog · NVIDIA Developer · Microsoft Research · AI타임스 |
| 은퇴 소스 (3) | GeekNews · Hugging Face Blog (D-105) · 인공지능신문 (D-107). 수집 안 함, `sources.retired` 에 남김 |
| 추출 프롬프트 기본 | `extract_ontology.v4.md` · 게이트 `relevance_gate.v1.md` (`extraction/extractor.py:49-50`) |
| 모델 | 세 단계 전부 내부 vLLM `gemma-4-31B-it` (ADR-018). 벤더 호출 지점 0 |
| 루브릭 기본 | `summary_quality.v3.md` (`eval/runner.py:64`). v6(코드 계산)은 전환 조건 충족·사람 확인 대기 |
| 자동 실행 | **켜져 있다.** 상시 승인 2026-09-30 → **만료 2026-10-14**. 마지막 2026-10-02 08:10 정상(exit 0). 열린 경보 🟠 3: NVIDIA `not_drained`(연속 3) · AI타임스 `not_drained`(연속 2) · AI타임스 `evicted` 추출 대기 10(연속 1) |
| 보존소 | `data/extractions/` **232건** = `claude-opus-5` 30(MARA export 원천, 2026-09-17) + `gemma-4-31B-it` 202(파이프라인 실행분). 전부 프롬프트 v4 |
| session-18 열린 항목 | AI타임스 추출 상한 15(`evicted_extraction` 관찰 후) · 별칭 추가(기사 3건 기준) · 수동 run 경보 비누적. 이전 세션 대기 항목(session-14/15/16/17)도 그대로 |

**AI타임스 `evicted` 추출 대기 10 은 session-18 §5 가 예고한 실제 손실이다.** 이번 범위 밖이라 손대지 않았다.

## 2. MARA 계약(1.0) 대비 현재 export

### 2.1 형식은 그대로다

`2579783`(MARA 에 들어간 export 의 exporter 커밋) 이후 `export/record.py` · `doc_id.py` · `contract.py` · `exporter.py` **변경 0**. `extraction/schema.py` 는 docstring 2줄만. 어휘 해시 `schema_sha256 = 2b42e09b…` 가 MARA manifest 의 값과 **같다** — 필드·`doc_id` 규칙·통제어휘(14 / 8) 전부 그대로.

### 2.2 "MARA 계약 동기화" 항목 — 흩어져 있던 것을 한 표로

session-14 §7 · session-15 §7 · session-16 §6 에 나뉘어 있던 것을 모았다. **어느 것도 major 인상 사안이 아니다**(계약 §9 의 major 목록 — `doc_id` 규칙·필수 필드·`text_origin`·4,000자 — 에 해당 없음).

| # | 항목 | 실측 (이번 세션) | 계약 쪽 성격 |
|---|---|---|---|
| S1 | 🔴 **`prompt_sha256` 이 재현되지 않는다** (session-14) | 보존소 232건 **전부** `9ff5a9d3…` = CRLF 작업본 바이트 해시. LF 정규화 값은 `25b17f44…`. 작업본이 `w/crlf`(attr 은 `eol=lf`)이고 `export/runner.py:71` 은 원본 바이트를 센다. **지금 새로 추출해도 같은 CRLF 값이 찍힌다** | 정의 결정 필요("파일 바이트"인가 "LF 정규화"인가). 정의를 바꾸면 같은 프롬프트가 코퍼스 안에서 두 해시로 갈린다 |
| S2 | 🔴 **기존 30건이 `claude-opus-5`** (session-15) | 그대로. 단 **같은 30건의 gemma 재추출이 이미 있다** — `data/replays/main` (2026-09-18, v4, 30/30 성공, 스키마 실패 0) | 계약 위반 아님(provenance 는 자유 문자열). §3.3 문구가 "외부 벤더 모델(`claude-opus-5`)"을 전제로 적혀 있다 → 문구 수정 |
| S3 | `published_at` = **UTC 날짜** (session-16) | 수집기는 전 소스 UTC 날짜로 통일(ADR-023). 계약 §3.1 은 "ISO `YYYY-MM-DD`"만 적고 시간대를 말하지 않는다 | 문구 수정(버전 유지). 기존 30건 값은 안 바뀐다 |
| S4 | `lang` 선언 서술 (신규) | §3.4 "GeekNews 는 `ko`, 나머지는 `en`" — GeekNews 은퇴, 지금 `ko` 는 AI타임스 | 문구 수정 |
| S5 | 🆕 **`export` 가 보존소 전체를 내보낸다** | `build_records(store)` 에 선택이 없다(`export/exporter.py`). 지금 기본 보존소로 `export` 하면 **232건**(AI타임스 등 파이프라인분 포함)이 나간다. 0.5a 때는 보존소 = 표본이었다 | 계약 아님 — 생산자 절차. 채우기 실행은 **별도 `--store-dir`** 로 해야 한다(§4) |
| S6 | 🆕 **MARA 로더는 export 를 전부 합친다** | `contract_import.discover_exports` 가 `data/corpus/<source>/*.jsonl` **모두**를 읽고, 스냅샷↔export 외의 `doc_id` 중복은 실패(§7-2). → **기존 30건과 같은 `doc_id` 를 담은 새 export 는 옛 jsonl 을 빼지 않으면 import 실패** | 계약대로의 동작. 경로 B 의 MARA 쪽 선행 작업이 된다 |

## 3. 메타 없는 14건

### 3.1 무엇이고 왜 없나

MARA 코퍼스 37 = 스냅샷 arXiv 16(`data/corpus/arxiv/{agent-evaluation,multiagent-llm}.json`, MARA 자체 수집) + export 30 중 색인 가능 23(arXiv 10 · news 13) − 병합 2. **결측 14 = 스냅샷 16 − 0.5a 에서 주입한 2건**(`2412.05449v1` · `2605.21404v1`).

| doc_id | 스냅샷 파일 | 발행 | 본문 | MARA 골든셋 |
|---|---|---|---|---|
| arXiv:2401.07324v3 | agent-evaluation | 2024-01-14 | 1567 | GS-019 (B) |
| arXiv:2602.16901v1 | agent-evaluation | 2026-02-18 | 1128 | GS-025 (B) |
| arXiv:2410.09024v3 | agent-evaluation | 2024-10-11 | 1361 | — |
| arXiv:2502.05227v1 | agent-evaluation | 2025-02-06 | 1212 | GS-020 (B) |
| arXiv:2410.09114v2 | agent-evaluation | 2024-10-10 | 1123 | — |
| arXiv:2512.02230v1 | agent-evaluation | 2025-12-01 | 1189 | — |
| arXiv:2406.00215v3 | agent-evaluation | 2024-05-31 | 1283 | — |
| arXiv:2512.06196v1 | multiagent-llm | 2025-12-05 | 1425 | GS-004 (B) |
| arXiv:2512.13930v1 | multiagent-llm | 2025-12-15 | 1199 | GS-007 (B) |
| arXiv:2405.16887v2 | multiagent-llm | 2024-05-27 | 1754 | GS-003 (B) |
| arXiv:2404.01023v1 | multiagent-llm | 2024-04-01 | 1813 | GS-006 (B) |
| arXiv:2412.20138v7 | multiagent-llm | 2024-12-28 | 1316 | GS-005 (B) |
| arXiv:2602.03128v1 | multiagent-llm | 2026-02-03 | 1658 | GS-002 (B) |
| arXiv:2406.01893v2 | multiagent-llm | 2024-06-04 | 1216 | GS-003 (B) |

URL 은 전부 `http://arxiv.org/abs/<doc_id 의 ID>` (스냅샷 표기. 병합 시 MARA 가 정규화 URL 로 갱신한다, 계약 §4.3). **층 B 9케이스의 앵커 10건이 전부 여기 있고**, 나머지 4건은 앵커가 아니다.

**이 레포의 대응 항목: 없다.** 보존소 232건에 0건(doc_id 조회), Vault 204개 파일에 0건(14개 arXiv ID 문자열 grep, 읽기만).

**왜 없나 — 추출을 한 적이 없다.**
1. 이 14건은 MARA 의 **주제 질의 결과**이고 생산자는 arXiv **최근 피드**만 받는다 — 구조적으로 겹치지 않는다(MARA ADR-018 Amendment).
2. 0.5a 의 주입은 계약 §12.1 의 중복 검증용 **2건만** 지정했다.
3. `--urls`(arXiv `id_list`)로는 이 16건이 0건으로 돌아온다(session-01 §6). MARA ADR-004 Verification 이 **16건은 실물**이고 문제는 `id_list` 경로라고 확정했다 → 원문 출처는 `--seed-from-mara`(`export/mara_seed.py`, MARA 스냅샷 읽기 전용) 하나다.
4. **MARA 스스로 채우기를 기각했다** — MARA ADR-022 Alternatives (가). 사유 ①실행 불가(당시 vLLM 이전 전 — **지금은 해소**), ②**공선성**: 14건만 gemma 로 채우면 `extraction_model` 이 "Opus 23 = 비스냅샷 / gemma 14 = 스냅샷"으로 문서군과 1:1 로 겹쳐 층별 차이를 모델 탓인지 문서군 탓인지 가를 수 없다. **재검토 조건: "전체 37건을 같은 모델로 재추출할 수 있게 되면", 필터 배선과 다른 세션에서 코퍼스 단독 변경으로.**

## 4. 메타를 채울 경로 후보

공통: 목적지 내부 vLLM(`gemma-4-31B-it`), 데이터 공개 arXiv 초록·공개 피드 발췌, 벤더 비용 0. 토큰은 보존소 실측 평균(추출 약 3.4k 입력 / 0.3k 출력, 게이트 약 1.4k / 0.06k)으로 잡았다. **어느 경로든 실행은 6항목 게이트 + 소표본(예: 3건) 먼저**다.

| | A. 14건만 | B. 전체를 gemma 로 통일 | B′. B 인데 30건은 기존 replay 재사용 |
|---|---|---|---|
| 내용 | 14건 seed 주입 → 게이트+추출 → 새 export 1개 **추가** | 14건 seed + 기존 30건(색인 불가 7 포함)을 한 실행에서 재추출 → 새 export 가 옛 export **대체** | 14건만 새로 추출, 30건은 `data/replays/main`(09-18) 결과로 레코드 생성 |
| 호출 | **최대 28** (게이트 14 + 추출 14) | 최대 **58** (게이트 14 + 추출 44). 30건 게이트를 다시 태우면 +30 | 최대 28 |
| 토큰 | 약 67k / 5k | 약 170k / 14k | 약 67k / 5k |
| 생산자 코드 | **변경 없음.** `collect --store-dir <별도> --seed-from-mara ../multiagent-research-lab --seed-doc-id …×14 --max-extractions 14` → `export --store-dir <별도>` | **필요.** 기존 30건의 입력(뉴스 20 = 은퇴 소스 15 포함)을 보존소 `raw_item` 에서 다시 넣는 경로가 없다 — seed 는 스냅샷 `.json` 만 읽고, `replay.py` 는 결과를 export 형식이 아닌 `rows.json` 으로 낸다 | **필요.** replay 행 → 보존소 형식(또는 레코드) 변환 |
| provenance | 14건 `extraction_model=gemma-4-31B-it` · `prompt_version=extract_ontology.v4.md` · `gate_model=gemma-4-31B-it`. 나머지 23건은 Opus 그대로 | 44건 전부 gemma · v4. `gate_model` 은 30건이 Haiku 판정으로 들어온 이력이라 **그대로 둘지 새로 태울지** 정해야 한다 | B 와 같고 `extracted_at` 이 두 시점(09-18 / 실행일)으로 갈린다 |
| MARA 반입 | jsonl+manifest 추가 커밋. 14건은 스냅샷과 같은 `doc_id` → **계약 §4.3 병합**(text 는 스냅샷 유지, 메타만). MARA 코드 변경 없음 | 옛 jsonl 제거(manifest 는 어휘 이력이라 남김, MARA ADR-019) 후 새 export 커밋 — **안 빼면 §7-2 중복 실패**(§2.2 S6) | B 와 같음 |
| 계약 버전 | **올릴 필요 없음** (1.0) | 올릴 필요 없음 | 올릴 필요 없음 |
| MARA ADR-022 (가) | 🔴 **기각된 그 경로다** (공선성) | ✅ 재검토 조건 그대로 | 🟡 모델은 같지만 추출 시점·회차가 문서군과 다시 겹친다(14 = 새 회차, 30 = 옛 회차) |

**계약 버전:** 세 경로 모두 1.0 안에서 된다. `extraction_model` 값이 바뀌는 것은 형식 변경이 아니다. §2.2 의 S1·S3·S4·§3.3 문구는 **"문구·설명 수정(버전 유지)"** 이지만, 원칙상 **계약 문서를 먼저 고친다** — MARA 쪽 작업이고 이 레포에서는 건드리지 않는다.

**어느 경로든 공통 주의:**
- **기본 보존소에 쓰지 않는다.** `--store-dir` 로 분리하지 않으면 export 가 232건+ 를 내보내고(S5), 파이프라인 품질 집계·`stored_outside_pipeline` 에도 섞인다.
- `collect` 는 `observer_from_config` 로 **실제 `observability/logs/`** 에 게이트 스킵·미등록 기업을 남긴다 — 파이프라인 운영 큐(D-114 별칭 기준 "기사 3건")에 MARA 표본이 섞인다. 끌지 그대로 둘지 정해야 한다.
- 게이트가 14건 중 하나라도 스킵하면 그 건은 여전히 메타가 없다. 게이트를 태울지(계약은 `gate_model: null` 허용) 정해야 한다.

## 5. 리스크

### 5.1 본문 — 이 14건은 이 레포에서 **가장 좋은 입력 조건**이다

- 1,123 ~ 1,813자, 14/14 마침표로 끝남(말줄임 없음), 4,000자 상한 아래 → `text_truncated=false` · `locator=abstract`.
- seed 경로라 **추출 입력 = MARA 가 색인하는 텍스트 그 자체**다. 병합에서 text 가 스냅샷 쪽으로 유지되므로 "추출이 본 글과 색인된 글이 다르다"는 문제가 없다.
- 본문 커버리지(CLAUDE.md "지금 걸려 있는 것" 첫 줄) 문제와 **무관**하다 — D-013 원문 fetch 도 필요 없다.

### 5.2 추출 품질 근거 — 골든셋 밖이다

- 생산자 골든셋 3건 중 arXiv 는 S3Gym 1건(cs.CL 최근 피드 논문). gemma `field_accuracy` 0.8518(session-12, Opus 와 같은 값). **14건과 같은 성격(2024~2026 에이전트·멀티에이전트·벤치마크 논문)의 확정 라벨은 0건이다.**
- **Opus ↔ gemma 일치 (replays/main 30건, 이번 세션 계산):** `release_type` **26/30** · `tech_domains` 완전 일치 **10/30** · 겹침 30/30 · **`Agent` 포함 여부 일치 28/30**. arXiv 10건만 보면 26 → 9/10, 완전 일치 6/10, `Agent` 10/10. — **이건 두 모델의 일치이지 정확도가 아니다.** 둘이 같은 값으로 틀릴 수 있다.
- 🔴 **이번 측정에서는 라벨이 곧 결과 변수다.** MARA 의 strict 재측정은 "층 B 앵커 10건에 `Agent` 가 붙는가"로 거의 정해진다. 모델이 붙인 라벨로 필터 재현율을 재면 **모델 라벨의 정확도를 필터 효과로 읽게 된다.** D-039("모델 출력은 정답이 아니다")의 같은 모양이다. 앵커 10건의 `tech_domains`·`release_type` 을 사람이 확인할지가 결정 대기다.
- **`release_type` 의 약한 곳:** 14건 중 7건이 `agent-evaluation`(질의에 "benchmark") 쪽이다. Opus→gemma 불일치 4건 중 arXiv 1건이 정확히 `Paper → Benchmark/Report` 였다. `release_type` 필터(MARA 필터 1순위)를 재측정할 때 흔들릴 곳이 여기다.
- MARA 층 정의: 채우고 나면 층 B(`anchor_meta=missing`) 앵커가 메타를 갖게 된다. `anchor_meta` 는 **측정 전에 파일에 박는 값**(MARA ADR-022)이라 MARA 쪽에서 층을 다시 선언해야 한다 — MARA 몫이고 여기서는 알리기만 한다.

## 6. 결정 대기 (사용자) — 1부 기준. **전부 §7 에서 답이 났다**

| 항목 | 답 (§7) |
|---|---|
| ~~경로 A / B / B′~~ | **B** |
| ~~B 라면: 30건 게이트~~ | **재게이트 안 함.** 구성 37건 고정, 게이트 생략을 레코드에 기록 |
| ~~앵커 10건 라벨 사람 확인~~ | **감사만** — 14건 전부, 정확도 기록, 라벨 수정 없음 |
| ~~S1 `prompt_sha256` 정의~~ | **LF 정규화** (ADR-027) |
| ~~계약 문서 문구~~ | S3·S4 는 **MARA 반입 세션에서 함께** |
| ~~관측 로그 오염~~ | **backfill 표시, 운영 큐에 섞지 않음** |
| 이전 세션 대기 항목 | session-18 §5, session-17 §5, session-16 §6, session-14/15 §7 그대로 유효 |

---

## 7. 결정 (사용자, 2026-10-02)

1. **경로 B** — MARA 현재 코퍼스 37건 전체를 `gemma-4-31B-it` + `extract_ontology.v4.md`(현재 기본)로 통일한다.
2. **재게이트하지 않는다.** 문서 구성 37건은 고정하고 메타만 재추출한다. 게이트 생략은 레코드에 기록한다. **(3부 정정)** 원래 게이트를 통과한 이력이 있는 레코드는 그 이력을 **보존**한다 — 23건 `gate_model = claude-haiku-4-5-20251001` · `gate_prompt_version = relevance_gate.v1.md`(MARA jsonl provenance 에서 옮긴다). 게이트를 거친 적이 없는 14건만 `gate_model: null`. **(4부 정정)** 이번 실행의 생략은 export 레코드에 **싣지 않는다** — 계약 1.0 유지. 채우기 전용 보존소의 **실행 매니페스트**에 남긴다: 실행 유형 `backfill`, 게이트 생략과 근거(ADR-026), 대상 doc_id 37건, 추출 모델 · 프롬프트 버전 · LF 해시. 이유: MARA 측정은 게이트 정보를 쓰지 않고, 기존 필드만으로 사실관계가 드러난다(23건 = 원래 게이트 이력 + gemma 재추출, 14건 = 게이트 `null` + gemma 추출). 3부의 "별도 필드" 안은 이 방식으로 대체됐다.
3. **사람 확인은 감사로만.** 새로 라벨이 붙는 14건(정답 10 + 비정답 4)을 MARA 측정 전에 사용자가 확인해 정확도를 기록하고, 라벨은 고치지 않는다. 확인용 표는 §11.
4. **`prompt_sha256` 은 LF 정규화로 정의.** 옛 CRLF 값과의 대응은 ADR-027 의 표.
5. **계약 문서 문구(S3·S4)는 MARA 반입 세션에서 함께** 고친다. ADR-027 의 §3.3 정의 문구도 같은 세션.
6. **채우기 실행은 별도 store-dir 에서 backfill 로 표시**하고 운영 관측 큐에 섞지 않는다.
7. **export 는 채우기 전용 store 에서만, MARA 코퍼스 doc_id 37건만** 내보낸다(S5·S6). 건수 37 을 검증한다.

## 8. 운영 제약 (사용자)

- 운영 체크아웃(`<운영 폴더>`)은 매일 08:10 자동 실행 중이므로 **코드를 바꾸지 않는다.**
- 경로 B 의 구현과 실행은 **git worktree 로 만든 별도 폴더의 브랜치에서만** 한다. store-dir · Vault · 큐 · 잠금·상태 파일을 모두 worktree 쪽으로 분리하고 운영 데이터와 공유하지 않는다. **공유되는 경로가 하나라도 있으면 목록으로 보고하고 멈춘다.**
- 채우기 실행은 **08:10 전후 1시간(07:10 ~ 09:10)을 피한다.**
- **main 병합은 하지 않는다.** 병합 여부는 자동 실행 관찰 세션 이후 사용자가 정한다.
- push 금지.

**스케줄러는 브랜치가 아니라 폴더를 실행한다** (2026-10-02 `Get-ScheduledTask` 확인): 작업 `ai-news-ontology daily` · `<운영 폴더>\.venv\Scripts\python.exe -m pipeline scheduled` · 작업 디렉터리 `<운영 폴더>`. → **운영 폴더에 체크아웃된 브랜치가 곧 내일 08:10 에 도는 코드다.** 운영 폴더에서 브랜치를 바꿔 둔 채 세션을 끝내지 않는다.

## 9. 다음 세션 진입 조건과 실행 계획

### 9.1 진입 조건 — 전부 충족돼야 구현을 시작한다

| # | 조건 | 확인 방법 |
|---|---|---|
| E1 | 운영 폴더가 `main` 이고 작업본이 이번 세션 끝 상태와 같다 | 운영 폴더 `git branch --show-current` · `git status --short`(`session-01.md` 1건만) |
| E2 | 작업 시각이 07:10 ~ 09:10 밖이고, 그날 자동 실행이 끝나 있다 | `data/pipeline/status.txt` 의 `last_run_at` (읽기만) |
| E3 | worktree 를 **운영 폴더 밖**에 만든다. 기본안: `git worktree add ../ai-news-ontology-backfill -b feat/mara-backfill chore/mara-meta-survey` (ADR-026·027 이 같이 간다) | `git worktree list` |
| E4 | worktree **전용 가상환경**에 `pip install -e .` — 운영 `.venv` 를 쓰지 않는다 (§10 P1) | worktree 의 `python -c "import sys; print(sys.prefix)"` |
| E5 | worktree **전용 `.env`** — `VLLM_BASE` · `VLLM_MODEL` 만. `OBSIDIAN_VAULT_PATH` · 벤더 키 · Langfuse 키는 넣지 않는다 (§10 P2) | 키 이름만 출력 |
| E6 | **경로 점검 스크립트**가 아래 전부를 출력하고, 하나라도 `<운영 폴더>\` 로 시작하면 **멈추고 목록을 보고한다**: 패키지 7개(`collectors` · `eval` · `export` · `extraction` · `observability` · `obsidian_writer` · `pipeline`)의 `__file__`, `DEFAULT_STORE_DIR` · `DEFAULT_OUT_DIR` · `DEFAULT_REPLAY_DIR` · `DEFAULT_LOG_DIR` · `DEFAULT_LEDGER_DIR` · `DEFAULT_RUNS_DIR` · `PIPELINE_DIR` · `DEFAULT_LOCK_PATH`, config 경로, `.env` 경로, 해석된 vLLM 엔드포인트 해시 | 스크립트 출력 |
| E7 | ✅ **충족** — §10 의 "구조상 공유" 3건(P3 git 저장소 · P4 MARA 레포 읽기 · P5 vLLM 엔드포인트, 시간대 회피로 분리)을 사용자가 허용했다 (2026-10-02) | — |
| ~~E9~~ | **철회 (4부)** — 게이트 스킵을 레코드 필드로 넣지 않으므로 계약 1.1 개정이 필요 없다 | — |
| E8 | 테스트가 worktree 에서 통과한다 (`<worktree venv> -m pytest tests/ -q`) | 기준 965 passed + 새 테스트 |

### 9.2 구현 (worktree 브랜치, LLM 호출 없음)

1. **37건 목록 고정** — MARA `data/corpus/arxiv/*.json` 의 doc_id ∪ `data/corpus/*/*.jsonl` 의 `indexable=true` doc_id. 37 이 아니면 예외. 결과를 레포 파일로 커밋한다(목록이 곧 계약 범위).
2. **입력 구성 (MARA 읽기 전용)** — 스냅샷 16건은 스냅샷 `.json` 에서(14 + 병합 2, 병합 2건은 jsonl text = 스냅샷 text 확인됨), 나머지 21건(arXiv 8 + news 13)은 jsonl 레코드에서 `RawItem` 을 만든다. source_name · title · published_at · collected_at · tags(lang) 는 기존 값 유지. 운영 보존소는 읽지 않는다 — jsonl 30건 = 운영 보존소 원본 30건, 불일치 0 (§10 아래 근거).
3. **추출만** — 게이트 호출 없음. 보존 payload 의 `gate` 는 **원래 이력을 옮긴다**: 23건은 MARA jsonl provenance 의 `gate_model` · `gate_prompt_version`(그 외 `is_relevant` · `reason` · `prompt_sha256` 은 레코드 스키마에 없고 jsonl 에도 없다 — 옮기지 않는다는 것을 payload 에 표시), 14건은 `gate: null`. backfill 표시는 **채우기 전용 보존소 안에만** 둔다 — 항목별 `run_kind: "backfill"` 과 실행 매니페스트(실행 유형 `backfill` · 게이트 생략과 근거 ADR-026 · 대상 doc_id 37건 · 추출 모델 · 프롬프트 버전 · LF 해시). export 레코드와 `CONTRACT_VERSION`(`"1.0"`)은 바꾸지 않는다. observer = `NullObserver`(운영 큐 분리, 결정 6).
   - **스키마 점검 결과 (구현 전 보고, 3부):** 게이트 이력 보존은 **현 스키마로 된다** — `export/record.py: provenance_block` 이 `gate["model"]` · `gate["prompt_name"]` 만 읽고, 23건 값은 MARA jsonl 에 있다. **별도 필드는 계약 1.0 에 없다.** MARA 로더는 major 와 필수 키만 검사해 모르는 키를 거부하지 않으므로(`contract_import.validate_record`) 기술적으로는 통과하지만, 계약 §9 상 선택 필드 추가는 **minor(1.1)** 이고 레코드의 `contract_version` 도 `"1.1"` 이 되어야 한다(`export/contract.py: CONTRACT_VERSION`). 계약 문서를 먼저 고친다는 원칙상 MARA 쪽 개정이 앞선다. → **4부에서 이 필드를 쓰지 않기로 했다**(실행 매니페스트로 대체, 계약 1.0 유지).
4. **`prompt_sha256` LF 정규화** (ADR-027). `pipeline/runner.py` 가 같은 함수를 쓰므로 병합하면 운영 보존소 기록도 바뀐다 — 병합 판단 때 볼 항목으로 handoff 에 남긴다.
5. **선택 export** — backfill 보존소 + 고정 목록. 레코드 수 ≠ 37 이거나 doc_id 집합 ≠ 목록이면 실패. `conformance.py` 로 자체검사.
6. **mock 테스트** — 목록 유도(37 아님 → 실패), 게이트 생략 → `gate_model null`, 선택 export 건수 검증, LF 해시, 운영 경로 미사용.

### 9.3 실행 — 6항목 게이트, 두 단계 별도 승인

| 단계 | 대상 | 추출 호출 | 상한 (스키마 재시도 포함) | 게이트 | 토큰 추정 |
|---|---|---|---|---|---|
| 소표본 | 3건 — 스냅샷 정답 1 · arXiv 피드 1 · news 1 (형식 확정) | 3 | **최대 6** | 0 | 약 10k / 1k |
| 본 실행 | 나머지 34건 | 34 | **최대 68** | 0 | 약 116k / 10k |

- 항목당 스키마 시도 최대 2회(`extraction/vllm.py` `attempts=2`) → 상한 = 2 × 건수. 시도마다 전송 재전송 최대 2회(`max_retries=2`)는 같은 요청의 재전송이라 별도 표기한다.
- 게이트 양식 1번: 목적지 = 내부 vLLM(`VLLM_BASE`, worktree `.env`). 2번: 공개 arXiv 초록 + 공개 RSS 발췌만. 비용: 벤더 0.
- 상한을 넘게 되면 그 자리에서 멈추고 다시 묻는다.
- 실행 뒤: §11 감사 표의 gemma 라벨 칸을 채워 사용자에게 보여 준다 → 사용자 감사 → 정확도 기록. **MARA 반입은 그 뒤, MARA 세션에서**(옛 jsonl 제거 · `anchor_meta` 재선언 · 계약 S3·S4·§3.3 문구).

## 10. 공유 경로 점검 (이번 세션, 읽기만)

레포의 데이터 경로는 전부 `Path(__file__).resolve().parent.parent` 기준이다(`export/store.py` · `observability/events.py` · `extraction/llm.py` 의 `.env` · `pipeline/*`). **worktree 의 모듈이 import 되기만 하면** 보존소 · 관측 큐 · 원장 · 실행 요약 · 잠금(`data/pipeline/scheduled.lock`) · 상태 파일 · config 가 전부 worktree 쪽으로 간다. 그 "되기만 하면"을 깨는 것과, 구조상 나눌 수 없는 것:

| # | 공유 지점 | 상태 | 조치 |
|---|---|---|---|
| **P1** | 🔴 운영 `.venv` 의 editable finder 가 `collectors` · `eval` · `extraction` · `observability` · `obsidian_writer` 를 **`<운영 폴더>\…` 로 매핑**한다 | 운영 `.venv` 로 worktree 를 돌리면 이 5개가 운영 폴더에서 import 될 수 있다 → 관측 큐·`.env`·정규화 사전이 운영 쪽 | **E4: worktree 전용 가상환경.** E6 이 `__file__` 로 확인 |
| **P2** | `.env` | gitignore 라 worktree 에 없다. 운영 `.env` 를 복사하면 `OBSIDIAN_VAULT_PATH`(실제 Vault) · 벤더 키가 따라온다 | **E5: `VLLM_*` 만.** Vault 경로가 없으면 Vault 쓰기는 실패로 끝난다 |
| P3 | git 저장소(`.git` 객체·참조) | worktree 는 구조상 공유한다. 데이터 아님. 운영 폴더의 체크아웃은 바뀌지 않는다 | E7 허용 대상 |
| P4 | MARA 레포 (`../multiagent-research-lab`) | 입력원으로 **읽기만** 한다. 운영 데이터 아님 | E7 허용 대상 |
| P5 | 내부 vLLM 엔드포인트 | 원격 자원. 경로 아님. 자동 실행과 같은 목적지 | 시간 분리(07:10 ~ 09:10 회피). E7 허용 대상 |
| — | 작업 스케줄러 | 운영 폴더만 등록돼 있다. worktree 에서 `register-scheduled-task.ps1` 을 **돌리지 않는다** | — |
| — | Langfuse | 미구현(`observability/events.py:510` TODO). 키를 넣지 않으면 경로 없음 | E5 |
| — | Claude Code 훅 | `.claude/hooks/` 는 추적 파일이라 worktree 에 따라간다. 상태 훅은 worktree 의 `data/pipeline/status.txt` 를 본다 | — |

**판정: 지금 계획대로면 데이터 경로 공유는 P1 · P2 두 곳이고, 둘 다 E4 · E5 로 끊는다.** P3 ~ P5 는 나눌 수 없는 공유라 사용자 허용이 진입 조건(E7)이다. 운영 제약대로 **구현 세션은 E6 출력에서 공유 경로가 하나라도 나오면 멈추고 목록을 보고한다.**

근거 (이번 세션 실측): MARA jsonl 30건 ↔ 운영 보존소 `raw_item` — text · title · 발행일 · 소스명 불일치 **0/30**. MARA 색인 코퍼스 = 스냅샷 16 ∪ 색인 가능 export 23 = **37**. 병합 2건 jsonl text = 스냅샷 text **2/2**.

## 11. 감사 표 (14건) — 사용자 확인용

doc_id · 제목 · 초록 앞부분은 MARA 스냅샷에서 읽었다(공개 arXiv 초록). **gemma 라벨 칸은 본 실행 뒤 채운다.** 사용자는 각 행에 판정(맞음 / 틀림, 틀리면 정답 값)만 적고, 레코드는 고치지 않는다(결정 3). 정확도는 필드별(`release_type` · `tech_domains` 완전 일치 · `Agent` 포함 여부)로 정답 10 / 비정답 4 를 나눠 기록한다.

| # | doc_id | MARA 골든셋 | 제목 | 초록 앞부분 | gemma `release_type` | gemma `tech_domains` | 판정 `release_type` | 판정 `tech_domains` |
|---|---|---|---|---|---|---|---|---|
| 1 | arXiv:2401.07324v3 | GS-019 | Small LLMs Are Weak Tool Learners: A Multi-LLM Agent | Large Language Model (LLM) agents significantly extend the capabilities of standalone LLMs, empowering them to interact with external tools (e.g., APIs, functions) and complete various tasks in a self-directed fashion. T… | | | | |
| 2 | arXiv:2602.16901v1 | GS-025 | AgentLAB: Benchmarking LLM Agents against Long-Horizon Attacks | LLM agents are increasingly deployed in long-horizon, complex environments to solve challenging problems, but this expansion exposes them to long-horizon attacks that exploit multi-turn user-agent-environment interaction… | | | | |
| 3 | arXiv:2410.09024v3 | — | AgentHarm: A Benchmark for Measuring Harmfulness of LLM Agents | The robustness of LLMs to jailbreak attacks, where users design prompts to circumvent safety measures and misuse model capabilities, has been studied primarily for LLMs acting as simple chatbots. Meanwhile, LLM agents --… | | | | |
| 4 | arXiv:2502.05227v1 | GS-020 | Robotouille: An Asynchronous Planning Benchmark for LLM Agents | Effective asynchronous planning, or the ability to efficiently reason and plan over states and actions that must happen in parallel or sequentially, is essential for agents that must account for time delays, reason over … | | | | |
| 5 | arXiv:2410.09114v2 | — | Catastrophic Cyber Capabilities Benchmark (3CB): Robustly Evaluating LLM Agent Cyber Offense Capabilities | LLM agents have the potential to revolutionize defensive cyber operations, but their offensive capabilities are not yet fully understood. To prepare for emerging threats, model developers and governments are evaluating t… | | | | |
| 6 | arXiv:2512.02230v1 | — | Benchmarking LLM Agents for Wealth-Management Workflows | Modern work relies on an assortment of digital collaboration tools, yet routine processes continue to suffer from human error and delay. To address this gap, this dissertation extends TheAgentCompany with a finance-focus… | | | | |
| 7 | arXiv:2406.00215v3 | — | HumanEvalComm: Benchmarking the Communication Competence of Code Generation for LLMs and LLM Agent | Large language models (LLMs) have significantly improved their ability to perform tasks in the field of code generation. However, there is still a gap between LLMs being capable coders and being top-tier software enginee… | | | | |
| 8 | arXiv:2512.06196v1 | GS-004 | ARCANE: A Multi-Agent Framework for Interpretable and Configurable Alignment | As agents based on large language models are increasingly deployed to long-horizon tasks, maintaining their alignment with stakeholder preferences becomes critical. Effective alignment in such settings requires reward mo… | | | | |
| 9 | arXiv:2512.13930v1 | GS-007 | Hierarchical Multi-agent Large Language Model Reasoning for Autonomous Functional Materials Discovery | Artificial intelligence is reshaping scientific exploration, but most methods automate procedural tasks without engaging in scientific reasoning, limiting autonomy in discovery. We introduce Materials Agents for Simulati… | | | | |
| 10 | arXiv:2405.16887v2 | GS-003 | A Large Language Model-based multi-agent manufacturing system for intelligent shopfloor | As customer demand for multi-variety and small-batch production increases, dynamic disturbances place greater demands on manufacturing systems. To address such challenges, researchers proposed the multi-agent manufacturi… | | | | |
| 11 | arXiv:2404.01023v1 | GS-006 | Large Language Model Evaluation Via Multi AI Agents: Preliminary results | As Large Language Models (LLMs) have become integral to both research and daily operations, rigorous evaluation is crucial. This assessment is important not only for individual tasks but also for understanding their soci… | | | | |
| 12 | arXiv:2412.20138v7 | GS-005 | TradingAgents: Multi-Agents LLM Financial Trading Framework | Significant progress has been made in automated problem-solving using societies of agents powered by large language models (LLMs). In finance, efforts have largely focused on single-agent systems handling specific tasks … | | | | |
| 13 | arXiv:2602.03128v1 | GS-002 | Understanding Multi-Agent LLM Frameworks: A Unified Benchmark and Experimental Analysis | Multi-agent LLM frameworks are widely used to accelerate the development of agent systems powered by large language models (LLMs). These frameworks impose distinct architectural structures that govern how agents interact… | | | | |
| 14 | arXiv:2406.01893v2 | GS-003 | Large Language Model-Enabled Multi-Agent Manufacturing Systems | Traditional manufacturing faces challenges adapting to dynamic environments and quickly responding to manufacturing changes. The use of multi-agent systems has improved adaptability and coordination but requires further … | | | | |

## 12. 결정 대기 (2부)

| 항목 | 내용 |
|---|---|
### 3부에서 닫힌 것 (사용자, 2026-10-02)

| 항목 | 답 |
|---|---|
| ~~ADR-026 · ADR-027 수락~~ | ✅ **Accepted.** ADR-027 구현의 main 병합은 별도 판단 |
| ~~ADR-026 "감사 뒤 라벨 수정" 기각 사유~~ | ✅ Alternatives 에 추가 — (a) 측정 대상이 자동 라벨러가 아니라 사람 라벨이 되어 오라클에 가까워진다 (b) 14건(정답 10 포함)만 고치면 정답 문서 쪽으로 기운 선택적 수정. Recheck: 사람 라벨 조건이 필요하면 별도 조건으로 측정 → Review Trigger |
| ~~결정 2 의 해석~~ | ✅ **정정** — 이력 있는 23건은 게이트 이력 보존, 14건만 `null`, 이번 생략은 별도 필드(§7-2, §9.2-3) |
| ~~E7 허용~~ | ✅ P3 · P4 · P5 허용 |

### 남은 것

| 항목 | 내용 |
|---|---|
| ~~게이트 스킵 필드 → 계약 1.1~~ | ✅ **닫음 (4부)** — 레코드 필드로 넣지 않는다, 계약 1.0 유지, 채우기 보존소의 실행 매니페스트에 남긴다. E9 철회. 반입 세션의 계약 작업은 S3·S4 문구 수정 |
| **ADR-027 의 계약 §3.3 정의 문구** | 4부 지시는 "반입 세션에서는 S3·S4 문구 수정만"이다. ADR-027(Accepted)의 Implementation 에는 "MARA 계약 §3.3 `prompt_sha256` 정의 문구 수정"이 남아 있다 — 이것을 반입 세션에 포함할지, 따로 할지 확인 필요. ADR-027 은 고치지 않았다 |
| **worktree 기준 브랜치** | 기본안은 `chore/mara-meta-survey` 에서 분기(ADR 이 따라간다). `main` 에서 분기하면 ADR-026·027 이 구현 브랜치에 없다 |
| **병합 시 볼 것** | ① ADR-027 구현이 `pipeline/runner.py`(`GUARD_FILES`) 가 쓰는 함수를 바꾼다 — 병합하면 운영 보존소 해시가 바뀌는데 승인 대조는 잡지 않는다 ② 🆕 **`export/runner.py` 를 `GUARD_FILES` 에 넣을지** — 넣으면 변경 시 자동 실행이 멈추고 재승인(ADR-025 의 의도한 비용), 안 넣으면 운영 보존소 기록을 바꾸는 코드가 대조 밖에 남는다 병합 판단은 자동 실행 관찰 세션 뒤 사용자 |

## 세션 종료 상태

- 운영 폴더 `<운영 폴더>` → **`main`** 으로 되돌림 (이 handoff 커밋 뒤). `chore/mara-meta-survey` 의 문서 커밋은 브랜치에 그대로 둔다
- 작업 스케줄러: `ai-news-ontology daily` · `<운영 폴더>\.venv\Scripts\python.exe -m pipeline scheduled` · 작업 디렉터리 `<운영 폴더>` → **내일 08:10 은 `main` 코드로 돈다**
- 구현용 worktree 는 만들지 않았다 (구현은 다음 세션)
- 3부 문서 수정은 **운영 폴더를 `main` 에 둔 채** 임시 worktree(`../ai-news-ontology-docs`, `chore/mara-meta-survey`)에서 커밋했고, 커밋 뒤 그 worktree 는 지웠다(브랜치는 남는다)

## 주의

- 🆕 bash 에서 `cat > 파일` 을 **stdin 리다이렉트 없이** 앞에 두면 명령이 stdin 을 기다리며 120초 타임아웃으로 백그라운드로 넘어간다(두 번). 파이썬은 스크립트 파일로 쓰고 `< /dev/null` 을 붙인다
- 보존소 232건을 한 번에 읽는 스크립트는 수 초면 끝난다 — 오래 걸리면 위 문제다
- 자동 실행이 매일 08:10 에 보존소에 쓴다. 조사 스크립트는 읽기만 했지만, 채우기 실행을 같은 시각에 돌리지 않는다
- `docs/handoff/session-01.md` 포매터 변경은 여전히 커밋하지 않는다
