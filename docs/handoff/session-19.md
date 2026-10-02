# Session 19 핸드오프 — 🔵 조사만: MARA 층 B 메타 채우기 준비 (생산자 쪽 현황)

- **날짜:** 2026-10-02
- **범위:** MARA v1.2 의 "메타 결측 14건"(MARA ADR-029·030, README §5.11)을 채우기 위한 생산자 쪽 현황 조사. **코드 변경 0 · LLM 호출 0 · MARA 레포 쓰기 0**
- **브랜치:** `chore/mara-meta-survey` (main `54dad05` 에서 분기). 커밋 1개(이 handoff), **push 안 함**
- **테스트:** 돌리지 않음 (코드 변경 없음)
- **읽은 것:** 이 레포의 `config.yaml` · `export/` · 보존소(`data/extractions/`, 읽기만) · `data/replays/` · 자동 실행 상태. MARA 의 계약 문서 · `data/corpus/` · `docs/eval/golden-set.json` · ADR-004·022·030 · `src/tools/contract_import.py` (전부 읽기만, MARA `git status` 깨끗한 것 확인)
- **ADR:** 없음 — 결정을 내리지 않았다. 아래 §4 의 경로 선택이 나면 그 자리에서 `adr-skill:adr-recorder`

> **다음 세션이 먼저 읽을 곳:** §4(경로 후보), §6(결정 대기). **§3 의 발견 하나가 경로를 좁힌다 — MARA 는 이미 "14건만 채우기"를 기각했다(MARA ADR-022 (가)).**

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

## 6. 결정 대기 (사용자)

| 항목 | 내용 |
|---|---|
| **경로 A / B / B′** | §4. A 는 코드 0 이지만 MARA 가 기각한 공선성 경로. B 는 MARA 재검토 조건과 맞지만 생산자 코드 + MARA 쪽 옛 jsonl 제거가 선행 |
| **B 라면: 30건 게이트** | Haiku 이력을 그대로 둘지, gemma 로 다시 태울지(+30 호출). 재게이트에서 스킵이 나오면 코퍼스 구성이 바뀐다 |
| **앵커 10건 라벨 사람 확인** | §5.2. 하면 MARA 재측정이 "필터"를 재고, 안 하면 "gemma 라벨 + 필터"를 잰다 |
| **S1 `prompt_sha256` 정의** | B 로 가면 전 레코드가 새로 찍히므로 **정의를 정할 마지막 좋은 시점**이다. A 로 가면 옛 값과 섞인다 |
| **계약 문서 문구(S1·S3·S4·§3.3)** | MARA 세션에서 먼저. 생산자 코드는 그 뒤 |
| **관측 로그 오염** | 채우기 `collect` 의 게이트 스킵·미등록 기업이 운영 큐에 섞이는 것을 막을지 |
| 이전 세션 대기 항목 | session-18 §5, session-17 §5, session-16 §6, session-14/15 §7 그대로 유효 |

## 주의

- 🆕 bash 에서 `cat > 파일` 을 **stdin 리다이렉트 없이** 앞에 두면 명령이 stdin 을 기다리며 120초 타임아웃으로 백그라운드로 넘어간다(두 번). 파이썬은 스크립트 파일로 쓰고 `< /dev/null` 을 붙인다
- 보존소 232건을 한 번에 읽는 스크립트는 수 초면 끝난다 — 오래 걸리면 위 문제다
- 자동 실행이 매일 08:10 에 보존소에 쓴다. 조사 스크립트는 읽기만 했지만, 채우기 실행을 같은 시각에 돌리지 않는다
- `docs/handoff/session-01.md` 포매터 변경은 여전히 커밋하지 않는다
