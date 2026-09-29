# Session 18 핸드오프 — 🟡 첫 수동 전량 실행 → **실패가 정상값의 모양으로 나오는 자리 셋**을 막았다

- **날짜:** 2026-09-29
- **범위:** 수동 전량 실행(일반 6항목 게이트, 승인 90/41) → 결과 확인 → ① drained 정의 분리 · 밀려남 신호 · 상한 조정 · 수동 run 품질 · 창 안 본문 길이 → ② 별칭 큐 수정 → ③ 경보 등급을 피드 깊이로 · 창의 carry cutoff · 표본 피드 선언
- **브랜치:** `feat/full-run-baseline`(4 커밋) → 그 위에 `feat/alert-premises`(+4). **main 병합·push 안 함** — `main..feat/alert-premises` 가 전부다
- **테스트:** `.venv/Scripts/python.exe -m pytest tests/ -q` → **965 passed** (923 → +42). 변이 검사 11/11 · 18/18 (임시 복사본)
- **LLM API 호출:** 게이트 **80** · 추출 **36** (재시도 0), 전부 내부 vLLM(`gemma-4-31B-it`). 승인 상한 90/41 안. 그 뒤 LLM 0건 (피드 GET 만)
- **ADR (`adr-skill:adr-recorder`, 전부 Accepted — 사용자, 2026-09-29):** ADR-023 **Amendment 1**(완결 분리·밀려남) · **Amendment 2**(carry cutoff) · ADR-025 **Amendment 1**(깊이 기반 등급·표본 피드). 결정 로그 **D-111 ~ D-117**
- **의존성:** 새로 추가 0개

> **다음 세션이 먼저 읽을 곳:** §4(켜는 순서), §5(결정 대기).

---

## 1. 전량 실행 결과 (`pipeline-20260929-152223`)

| | 값 |
|---|---|
| 종료 코드 · 실패 | 0 · 0 |
| 소요 | **275초** (LLM 지연 합이 거의 전부 — 게이트 94s + 추출 179s, 순차) |
| 토큰 | 게이트 약 113k 입력 / 4.5k 출력 · 추출 121.7k / 9.6k — **합 약 235k / 14k** (사전 추정 225k / 15k) |
| Vault | 21 → 57 (**+36, 변경 0, 삭제 0**, sha256 스냅샷 비교). 새 노트 36/36 에 `published_at` |

| 소스 | 게이트 | 통과 | 스킵 | 미룬 게이트 | 추출 | 미룬 추출 |
|---|---|---|---|---|---|---|
| arXiv | 20 | 20 | 0 | 0 | 10 | **10** |
| OpenAI | 10 | 9 | 1 | 6 | 5 | 4 |
| DeepMind | 0 | — | — | 0 | 1 | 0 |
| NVIDIA Dev | 10 | 10 | 0 | 10 | 5 | 6 |
| MSR | 0 | — | — | 0 | 0 | 0 (창 안 신규 없음) |
| AI타임스 | 40 (상한) | 25 | 15 | **7** | 15 (상한) | **11** |

- 괄호 병기 정규화: 실제 추출에서 괄호형은 `쿠웨이트투자청(KIA)` 1건(양쪽 미등록 → 미해결, 충돌 0). 모델이 `아마존웹서비스(AWS)` 를 `아마존웹서비스` 로 괄호를 떼고 낸다 — **D-109 경로가 실데이터에서 거의 안 탄다**
- AI타임스 미해결 기업 비율 0.76 의 원인은 괄호가 아니라 **사전 크기**(키 24개)

**피드 깊이 실측 (GET 만):** arXiv **1.1시간**(20건) · AI타임스 **27.8시간**(50건) · MSR 60일 · NVIDIA 76일 · DeepMind 334일 · OpenAI 10년+. 레포에 적혀 있던 "AI타임스 2~3일치"는 실측이 아니었고, 그 숫자 위에 경보 등급이 서 있었다.

## 2. 무엇을 바꿨나 (전부 사용자 결정)

| 결정 | 내용 |
|---|---|
| **D-111** | `window_drained`(기준점) / `drained`(경보, + 미룬 추출 0). `pipeline/backlog.py` — 다음 실행이 `evicted_gate` · `evicted_extraction` · `feed_rollover` 를 센다 |
| **D-112** | AI타임스 게이트 40 → **50**(피드 깊이) · arXiv 추출 10 → **20**. 천장 **100 / 51** |
| **D-113** | 수동 `run` 도 품질 지표 기록 · 본문 길이는 창 안 항목(`feed_items` 별도) |
| **D-114** | 별칭 큐: **서로 다른 기사 3건** 기준(`articles` · `article_count`), 지금 풀리는 줄은 기록 때마다 뺀다. `python -m observability.events queue / prune`. 실제 큐 45 → 43(`엔비디아(NVIDIA)` · `마이크로소프트(MS)` 제거, 백업은 세션 scratch), **후보 0** |
| **D-115** | `source_error` · `env_not_ready` · `breaker` 등급 = **여유**(`깊이 − 마지막 성공 이후 − 24h`) ≤ 0 이면 심각. AI타임스 첫 실패에 심각. 수집기가 `depth_hours` 를 싣는다(`RawItem` 불변) |
| **D-116** | 창은 미완결 실행이 쓴 **가장 이른 cutoff** 까지. `capped` = 그 cutoff 가 14일 너머(지속). 예전엔 미완결 1회로 14일 + 심각, 기준점 뒤 미룬 항목은 오히려 못 지켰다 |
| **D-117** | `feed_kind: sample` — **arXiv 에만.** 끄는 것: `feed_rollover` 경보 · 깊이 계산. **끄지 않는 것: `evicted_*`.** 출하 config 에서 sample 이 arXiv 하나뿐임을 테스트가 고정 |

변경 후 `plan`: OpenAI 창 14일 → **7일**(`needs_gate` 17 → 7). NVIDIA · AI타임스는 14일이지만 **capped 아님** — 오늘 실행이 09-15 cutoff 로 받아 미룬 항목의 carry 다.

## 3. 커밋 (`main..feat/alert-premises`)

```
b77ed17 feat(pipeline): grade fetch-blocked alerts by time left before items are lost
5394e25 fix(pipeline): keep the cutoff deferred items were admitted under
3de4cfa feat(observability): count distinct articles and drop resolved names from the queue
405ccd4 docs: record ADR-023 Amendment 1, decisions D-111 to D-113 and session 18
b6461a1 chore(config): raise AI Times gate limit to feed depth and arXiv extraction
2954d48 feat(pipeline): record quality metrics on manual runs, inside the window
554b66a feat(pipeline): count deferred items that leave the feed unprocessed
```

+ 이 handoff · ADR-023 Amendment 2 · ADR-025 Amendment 1 · D-115~117 docs 커밋. 논리 단위가 한 파일에 섞인 커밋(`runner.py` · `test_pipeline.py`)은 중간 상태를 따로 만들어 테스트한 뒤 커밋했다(B 는 HEAD worktree 에서 947 passed, 나머지 3건은 worktree 에 `.env` 가 없어 `VLLM_BASE` 로 실패 — 변경 무관).

## 4. 켜는 순서

1. **100/51 수동 전량 실행** — 일반 6항목 게이트로 별도 승인 (**다음 할 일**). `approval.full_manual_run` 은 소스별 상한이 현재 config 와 **정확히 같은** `mode == "config"` 실행만 받는다 — 오늘의 90/41 실행은 인정되지 않는다(확인함). 새 코드의 첫 실기 검증이다: AI타임스 `evicted_*`(오늘의 미룬 7 · 11 과 비교), arXiv `feed_rollover` 가 tally 에만 남고 경보가 없는지, `depth_hours`, `carry_cutoff`, 수동 run 의 `quality`
2. 브랜치 main ff 병합 (`GUARD_FILES` 인 `runner.py` · `schedule.py` 가 바뀌었다). push 는 사용자
3. 사용자가 자기 터미널에서 `approve-schedule` (노트 3건 대조) — 병합 후
4. `schedule.time` · `readiness.wait_minutes` → 5. `register-scheduled-task.ps1`

## 5. 결정 대기

| 항목 | 내용 |
|---|---|
| **AI타임스 추출 상한 15** | `evicted_extraction` 을 수 회 본 뒤 (D-112, ADR-023 Amendment 1 Review Trigger). 그때까지 AI타임스는 `not_drained`(2회째부터 경고) · `evicted`(경고)가 매 실행 뜬다 — **실제 손실**이다 |
| 별칭 추가 | 기준(서로 다른 기사 3건)에 닿는 이름이 생기면 넣는다. 기간 없음 (D-114) |
| 수동 run 은 경보 누적 안 함 | `alerts.json` 의 🔴 `preflight_mismatch`(연속 2) 가 그대로 열려 있다. 첫 통과 자동 실행이 닫는다 |
| 이전 세션 대기 항목 | session-17 §5, session-16 §6, session-14 §7, session-15 §7 그대로 유효 |

## 주의

- 🆕 bash heredoc 으로 한글·`'''` 이 든 긴 파이썬을 넘기면 `unexpected EOF` 로 **아무것도 안 된다**(두 번). 긴 코드는 Edit 로, 스크립트는 파일로 써서 돌린다
- 🆕 테스트 안에서 실행 두 번이 같은 초에 돌면 **실행 요약 파일이 서로를 덮는다**(run_id 가 초 단위). 이력에 의존하는 테스트는 `report=RunReport(run_id=...)` 를 준다 (`tests/test_pipeline.py: _report`)
- 🆕 큐 테스트는 실제 사전에 기대지 않는다 — 풀리는 줄이 기록 때 빠지므로, 충돌 사례는 `conflict_index` 픽스처 아래서 돈다
- `docs/handoff/session-01.md` 포매터 변경은 여전히 커밋하지 않는다
