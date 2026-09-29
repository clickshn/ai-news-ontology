# pipeline/

수집 → 관련성 게이트 → 추출 → 보존 → Obsidian 적재를 **한 명령으로** 잇는다.
설계는 ADR-022, 결정 로그 D-101 · D-102.

| 파일 | 역할 |
|---|---|
| `ledger.py` | doc_id 당 단계 상태 (`data/pipeline/ledger/`). 게이트 판정·실패 횟수·격리·적재 기록 |
| `runner.py` | `plan` / `run` / `load` / `release` CLI |
| `admission.py` | 수용 창 · 미확정 발행일 · 갱신 멈춤 판정 (ADR-023) |

```bash
python -m pipeline plan                                                          # API 호출 없음. 상한은 config
python -m pipeline run                                                           # 승인 대상
python -m pipeline run  --gate-limit "OpenAI News=3" --extract-limit "OpenAI News=2"   # CLI 가 config 상한 전체를 대체
python -m pipeline load                                                          # 보존소 → Vault 만
python -m pipeline release <doc_id>                                              # 격리 해제
```

## 기존 진입점 위의 층이다

`export.runner` 의 plan / collect / export 는 그대로 있다. 항목 1건의 게이트·추출·보존은
`export.runner.run_gate` / `run_extraction` 을 **같이 쓴다.** 추출 결과는 보존소를 거치므로
MARA export 의 입력에도 그대로 쌓인다.

## 무엇이 "끝났다"를 판정하나

키는 계약 §4 의 `doc_id` 하나다. 단계 완료는 **그 단계의 산출물을 가진 층**이 판정한다.

| 단계 | 판정 층 |
|---|---|
| 게이트 | 원장 — `gate_key`(게이트 프롬프트 · 설정 모델)가 바뀌면 다시 판정 |
| 추출 | 보존소 `data/extractions/` |
| 적재 | Vault 색인 — frontmatter `source_url` → `doc_id`. **날짜와 무관** |

- 원장 없이 보존소에만 있는 항목(`stored_outside_pipeline`)은 **재추출도 적재도 하지 않는다.**
  session 0.5a 의 `claude-opus-5` 30건이 여기 해당한다 (사용자 결정, session-15).
- 원장에 `written` 인데 노트가 없으면 사용자가 지운 것이다. `--rewrite-missing` 없이는 다시 쓰지 않는다.
- 보존소 결과가 현재 프롬프트·모델과 다르면 `stale` 로 보고만 하고 재추출하지 않는다.

## 상한

상한은 `config.yaml` 의 `sources.rss[].limits.{gate,extract}` 가 기본이다 (ADR-023).
**`--gate-limit` / `--extract-limit` 을 하나라도 주면 config 상한 전체를 무시하고 CLI 값만
쓴다** — 주지 않은 단계는 0 이다. 병합하지 않는 이유: "이 소스 3건"을 승인받은 실행이
나머지 소스를 config 값으로 함께 돌면 승인받은 숫자와 코드가 막는 숫자가 갈린다(D-102).
실행은 첫 줄에 `상한 출처: config | cli` 를 찍는다.

상한이 걸린 소스**만** 수집한다. 전역 천장(`--max-gate-calls` / `--max-extractions`)의
기본값은 **소스별 상한의 합**이다 (D-102). 코드의 절대 천장(200/100)은 오타 방지선이다.
**승인받을 숫자는 `plan` 의 "승인 대상 상한" 줄이다** — "현재 피드 기준 예상"은 피드
상태에 달려 실행 때 바뀐다. 상한은 논리 호출 수이고, 스키마 재시도로 요청 수는 최대 2배다.

## 수용 창 (ADR-023)

원장에 **처음 들어오는** 항목만 발행일(UTC 날짜)로 거른다. 원장에 이미 있는 항목(게이트
오류 재시도, 추출 대기)은 날짜와 무관하게 이어서 처리한다 — 다시 보는 비용은 0 이라,
창이 막는 것은 첫 수용 범위 하나다. 설정은 `config.yaml: pipeline.window` 이고, 이 절이
없으면 창이 꺼진다.

| 상황 | 창 |
|---|---|
| 첫 실행 (이 소스의 실행 기록 없음) | `lookback_days` (7) |
| 매일 실행 | `lookback_days` (7) |
| 마지막 완결 실행 뒤 N일 쉼 | `N + overlap_days`, 최대 `max_lookback_days` (14) |
| 마지막 완결 뒤 미완결 실행이 있음 | 위 규칙과 **미완결 실행이 쓴 가장 이른 cutoff**(`carry_cutoff`) 중 이른 쪽 |
| 필요한 cutoff 가 14일 너머 | 14 + `[warn]` + `capped` — 미완결 상태가 **지속돼** 실제로 거르고 있다 |

미완결 1회로 창을 14일로 넓히지 않는다. 미룬 항목에 필요한 것은 그것을 받았던 창의 cutoff
하나다 (D-116, ADR-023 Amendment 2).

- **기준점은 소스별 "마지막 완결 실행"이다** (`data/pipeline/runs/` 요약의 `window`).
  완결은 수집이 실패하지 않았고, 창을 통과한 항목 중 상한·차단기로 미룬 것
  (`deferred_gate`)과 미확정 초과분(`undated_deferred`)이 0 인 실행이다(요약의
  `window_drained`). 미룬 게 있으면 기준점이 앞으로 가지 않는다.
- **`drained` 는 추출 대기까지 본다** — 경보용이다. 기준점은 추출 대기와 무관하다(이미
  원장에 있다). 둘을 묶으면 추출 상한이 모자란 소스의 창이 늘 14일에 머문다 (D-111).
- 발행일이 없거나 내일보다 뒤이면 **미확정**이다. 피드 순서 앞에서부터 소스당
  `undated_max_per_run`(5) 건까지만 받는다 (`undated_admitted` / `undated_deferred`).
- 창 밖은 `out_of_window` 로 센다. `collected` 는 피드가 준 전체다.
- ⚠️ 창은 **피드가 아직 주는 항목**만 되살린다. AI타임스 피드 50건은 실측 약 28시간치다 —
  하루를 거르면 그 사이 글은 창과 무관하게 잃고, **상한 때문에 미룬 항목도 다음 실행 전에
  밀려난다.**

## 밀려난 항목 (D-111, `pipeline/backlog.py`)

루프는 이번 피드에 있는 항목만 돈다. 미룬 항목이 피드에서 밀려나면 어느 카운터에도 안 잡히고
다음 실행은 완결로 보인다. 그래서 실행 요약에 `backlog.<소스> = {head, gate, extraction}` 을
남기고, 다음 실행이 그 소스의 **마지막 기록**과 피드를 비교한다.

| 값 | 뜻 |
|---|---|
| `evicted_gate` · `evicted_extraction` | 미뤘던 것 중 지금 피드에 없고 원장상 여전히 대기 — **잃은 것**. 한 실행 늦게 센다 |
| `feed_rollover` | 직전 맨 앞 5개가 모두 없다 — 피드 깊이보다 많이 들어왔다. **건수는 모른다** |

경보 종류 `evicted` · `feed_rollover` 로 올라가고 첫 회부터 경고다. 종료 코드에는 넣지 않는다
(장애가 아니라 용량 문제다).

## 갱신 멈춤 (ADR-023)

`sources.rss[].max_silence_days` 보다 가장 최근 발행일이 오래됐으면 `stale_feed` 로 세고
**소스 단위 실패**로 다룬다(종료 코드 3). 200 을 주면서 내용이 안 늘어나는 피드는 수집
실패 구분(D-103)에 걸리지 않는다 — ZDNet Korea 의 레거시 경로가 2024-05-10 에 멈춘 채
200 을 주고 있었다. 항목은 그대로 흐르고, 창이 오래된 것을 거른다.

## 종료 코드

| 코드 | 뜻 |
|---|---|
| `0` | 실패 없음 (**정상 0건 소스는 실패가 아니다** — arXiv 주말) |
| `3` | 부분 실패 — 산출이 있거나, **소스 단위 실패(수집 실패 · 갱신 멈춤)만 있고 다른 소스는 정상으로 받았다** |
| `1` | 실행이 제 역할을 못 했다 — 모든 소스가 소스 단위 실패, 또는 LLM·적재 실패가 있는데 산출 0건 |
| `2` | 인자 오류 |
| `4` | (`scheduled`) **가부 불일치** — 상시 승인과 다르다. LLM 0건, 전 단계 정지 (ADR-025) |
| `5` | (`scheduled`) **환경 미준비** — 엔드포인트에 TCP 로 닿지 않았다. 차단기와 별개 |
| `6` | (`scheduled`) 잠금 보유 중 — 다른 실행이 돌고 있다 |

실행 요약은 `data/pipeline/runs/{run_id}.json`. 소스별 수집 결과는 `fetch` 에
`status`(`ok` · `empty` · `http_error` · `timeout` · `network_error` · `too_large` ·
`parse_error` · `no_usable_entries` · `config_error`)와 사유로 남는다 (D-103).

**수집 실패는 흐름을 멈추지 않고 종료 코드만 바꾼다.** 소스 하나가 죽으면 그 소스를
`source_error` 로 세고 다음 소스로 간다. 새 글이 없는 날(산출 0) 소스 하나가 죽었을 때
`1` 을 내지 않는 이유: 그러면 "소스 하나 장애"와 "전부 장애"가 같은 코드가 된다 —
수집기에서 가른 혼동을 종료 코드에서 다시 만드는 셈이다.

## 매일 자동 실행 (ADR-025)

```
python -m pipeline approve-schedule     # 사람만. 대화형 터미널. 6항목 확인 + 노트 3건 원문 대조
python -m pipeline scheduled            # 작업 스케줄러가 부른다 (scripts/register-scheduled-task.ps1)
```

    잠금 → 사전 점검(가부) → 준비 확인(TCP) → run_pipeline → 경보 누적 → 상태 노트 · 상태 파일 · 토스트

- **config 상한은 승인이 아니다.** `scheduled` 는 `.claude/scheduled-run-approved.json` 과
  provider · 해석된 엔드포인트 URL 해시 · 모델 · 프롬프트 · 소스 · 상한 · 목적지 결정 코드
  해시를 대조하고, 벤더 승인 파일이 남아 있으면 그것만으로 멈춘다 (`pipeline/approval.py`).
- **환경 미준비 ≠ 장애.** 준비 확인은 TCP 연결만 한다(HTTP·페이로드 없음). 대기 시간은
  `config.yaml: schedule.readiness`. 연결된 뒤의 전송 오류는 지금처럼 차단기다.
- **경보는 실행 요약의 구조화 값에서 뽑는다** (`pipeline/alerts.py`). `data/pipeline/alerts.json`
  에 누적되고 연속 횟수로 등급이 오른다. 이번 실행이 평가하지 않은 종류는 닫지 않는다.
- **수집이 막힌 경보(`source_error` · `env_not_ready` · `breaker`)는 잃기까지의 여유로 등급을 정한다**
  (D-115). `여유 = 피드 깊이(depth_hours) − 마지막 성공 이후 − 24시간` 이 0 이하면 심각. 전역 종류는
  가장 빠듯한 소스로 잰다. 깊이 기록이 없으면 횟수 규칙.
- **`feed_kind: sample`**(arXiv 만, D-117): 끄는 것 — `feed_rollover` 경보, 깊이 계산 대상.
  끄지 않는 것 — `evicted_*`. 다른 소스에 붙이면 그 소스의 넘김 손실이 조용히 사라진다.
- **닿는 곳:** Vault `_pipeline-status.md`(유일한 비뉴스 파일, 매 실행 덮어씀) · SessionStart 훅
  (`.claude/hooks/pipeline-status.sh`, **실행 부재를 여기서 잰다**) · 심각 등급 토스트.
- **품질 지표는 기록만** (`pipeline/quality.py`) — 본문 길이 중앙값, 게이트 통과율, 재시도율,
  미등록 기업 비율, 분포. 임계값은 기록 2주 뒤. **수동 `run` 도 기록한다** — 기준선은 자동
  실행을 켜기 전에 있어야 한다. 본문 길이는 **창 안 항목**으로 잰다(`inputs.<소스>.feed_items`
  가 피드 전체 건수) (D-113). 경보 누적은 자동 실행만 한다.

## 테스트

`tests/test_pipeline.py`, 자동 실행은 `tests/test_schedule.py`. 피드·LLM·Vault 를 전부 갈아 끼우고 `tmp_path` 에만 쓴다.
