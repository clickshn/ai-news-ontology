# ADR-023: 매일 실행의 소스 수용은 파이프라인이 발행일 창으로 가르고, 시간대 없는 발행일은 소스별 고정 오프셋으로 읽으며, 실행당 상한과 갱신 멈춤 기준은 config 에 둔다

- **Status:** Accepted
- **Date:** 2026-09-29
- **Decision:** 원장에 처음 들어오는 항목만 발행일 창(`max(7일, 소스별 마지막 완결 실행 이후 + 1일)`, 상한 14일)으로 거르고, 발행일이 없거나 미래인 항목은 피드 순서로 소스당 5건까지만 받는다. 창은 수집기가 아니라 파이프라인이 적용한다. 시간대 없는 발행일은 소스별 `naive_date_offset` 으로 해석하고 `published_at` 은 UTC 날짜로 통일한다. 소스별 게이트·추출 상한과 `max_silence_days` 를 config 에 두고, CLI 상한을 주면 config 상한 전체를 대체한다
- **Scope:** `pipeline/runner.py` (창 · 상한 해석 · 갱신 멈춤 판정) · `collectors/rss.py` (시간대 해석) · `config.yaml` (`sources.rss[]` 의 `limits` · `max_silence_days` · `naive_date_offset`, `pipeline.window`) · `data/pipeline/runs/` (창 기록)
- **Decision Source:** Human

---

## Context

### Problem

session-15 에서 오케스트레이션이 섰고, 이제 매일 도는 것을 전제로 소스를 4개에서 7개로 늘린다. 실행에서 두 가지가 드러났다.

- **수집은 피드가 주는 것을 전부 받는다.** OpenAI News 는 2015년부터의 전체 아카이브 1234건을 주고, 전부 `needs_gate` 였다. 자르는 것은 게이트 직전의 `--gate-limit` 뿐이고, 그 숫자를 사람이 매번 명령줄로 준다. 매일 자동 실행에는 맞지 않고, session-15 에서 확인했듯이 **사람이 옮겨 적는 숫자는 어긋난다** (D-102).
- **"최근 N일"만으로는 쉬는 동안의 항목이 영영 빠진다.** 3일 창인데 5일 쉬면 중간 이틀치는 다시 들어오지 않는다.

여기에 실측에서 두 가지가 더해졌다.

- AI타임스(일반 피드)와 인공지능신문은 발행일을 `2026-09-29 07:27:02` 처럼 **시간대 없이 KST 로** 준다. `_entry_date` 는 이를 UTC 로 읽어 최대 ±1일 어긋난다. 같은 AI타임스 기사가 시간대 있는 gn 피드에서는 09-28, 일반 피드에서는 09-29 였다. 발행일이 수용 판정 기준이 된 이상 **조용히 틀리는 자리**다.
- ZDNet Korea 의 유일하게 살아 있는 피드 경로(`NewsSection0020.xml`)는 **200 을 주면서 2024-05-10 에 멈춰 있었다.** D-103 의 수집 실패 구분은 이것을 `ok` 로 본다. 비공식 중계뿐 아니라 공식 피드도 조용히 멈출 수 있다.

### Constraints

- 재실행 중복은 원장이 `doc_id` 로 이미 거른다 (ADR-022). 그래서 **다시 보는 비용은 0 이고**, 창이 실제로 막는 것은 "처음 받아들이는 범위" 하나다.
- 수집기는 판단하지 않는다 (`collectors/README.md`). `collect` 는 `export` · `eval` 도 쓴다.
- 한국 매체 피드는 최근 50건(2~3일치)만 준다. 피드가 더 이상 주지 않는 항목은 어떤 창 설계로도 복구되지 않는다.
- 이 venv(Windows)에는 `tzdata` 가 없어 `zoneinfo.ZoneInfo("Asia/Seoul")` 이 실패한다.
- 승인 게이트는 "승인받는 숫자가 곧 코드가 막는 숫자"여야 한다 (D-102).

## Decision

### Selected

- **Technology:** 새 의존성 없음. 시간대는 고정 UTC 오프셋 문자열(`"+09:00"`)로 표현한다
- **Architecture:** 수용 판정은 파이프라인 층에 둔다. 수집기는 피드 전체를 넘기고, 파이프라인이 항목 상태를 분류한 뒤 **원장 기록이 없는 항목(`entry is None`)에만** 창을 적용한다. 게이트 오류·추출 대기처럼 원장에 이미 있는 항목은 날짜와 무관하게 이어서 처리한다
- **Implementation:**
  - **창:** `cutoff = today_utc - span`. 기준점(anchor)이 없으면(첫 실행) `span = lookback_days`, 있으면 `span = min(max(lookback_days, (today - anchor) + overlap_days), max_lookback_days)`. 상한에 걸리면 `[warn]` 과 `window_capped` 를 요약에 남긴다. 값은 `pipeline.window` 의 7 / 1 / 14
  - **기준점:** **소스별로** 실행 요약(`data/pipeline/runs/*.json`)에서 가장 최근의 **완결 실행**. 완결은 수집이 실패하지 않았고, 창을 통과한 항목 중 상한·차단기로 미룬 것(`deferred_gate`)과 미확정 초과분이 0 인 실행이다. 미룬 항목이 있으면 기준점이 앞으로 가지 않아, 그 항목들이 창 밖으로 밀려나지 않는다
  - **발행일이 없거나 믿을 수 없는 항목:** `published_at` 이 없거나 `today_utc + 1일` 보다 뒤이면 "미확정"이다. 창 판정 없이 피드 순서 앞에서부터 소스당 `undated_max_per_run`(5)건까지 받고, 나머지는 `undated_deferred` 로 센다
  - **시간대:** 발행일 원문에 오프셋(`Z`, `±hh:mm`, `±hhmm`, `GMT`/`UT`/`UTC`, 미국 시간대 약어)이 있으면 그것을 따른다. 없으면 소스의 `naive_date_offset` 으로 해석하고, 설정이 없으면 **UTC 로 읽되 `FeedResult.warning` 에 건수를 남긴다.** `published_at` 은 모든 소스에서 **UTC 날짜**다
  - **실행당 상한:** `sources.rss[].limits.{gate,extract}`. `pipeline plan/run` 에 `--gate-limit` / `--extract-limit` 이 하나도 없으면 config 상한으로 7개 소스 전부를 돌고, **하나라도 있으면 config 상한 전체를 무시하고 CLI 값만 쓴다**(주지 않은 단계는 0). 전역 천장 기본값은 여전히 소스별 합이고(D-102), 코드의 절대 천장은 게이트 200 / 추출 100 으로 올린다
  - **갱신 멈춤:** `sources.rss[].max_silence_days`. 수집이 정상이고 날짜 있는 항목이 있는데 가장 최근 발행일이 이보다 오래됐으면 `stale_feed` 로 세고, **소스 단위 실패**로 다룬다(D-103 의 종료 코드 완화 대상). 항목은 그대로 흐르며, 창이 오래된 항목을 거른다

## Rationale

1. **창은 파이프라인에 둔다.** 기준점이 원장과 실행 요약에 있고 둘 다 파이프라인의 것이다. 요약에 `collected` 와 `out_of_window` 가 따로 남아 "파이프라인이 무엇을 보았는가"가 기록된다. 수집기에서 자르면 `export` · `eval` 의 동작까지 바뀐다. 파싱 비용은 OpenAI 750KB 1회 수준이다.
2. **창을 "처음 받아들이는 항목"에만 거는 이유:** 다시 보는 비용이 0 이므로 창의 역할은 첫 수용 범위뿐이다. 원장에 이미 있는 항목에 창을 걸면 게이트 오류로 재시도를 기다리던 항목이 날짜가 지났다는 이유로 버려진다.
3. **기준점을 소스별로 두는 이유:** 전역 기준점이면 DeepMind 만 5일 죽어 있던 동안에도 다른 소스가 성공해 기준점이 앞으로 가고, DeepMind 의 그 5일치를 잃는다.
4. **N=7 을 전역으로 두는 이유(소스별 창 없음):** 소스별로 달리 줄 근거는 arXiv 하나였다. arXiv 의 `published` 는 제출일이라 게시보다 3일 이상 늦는다(2026-09-29 화요일에 최신이 09-26). 다시 보는 비용이 0 이므로 전역 7일로 흡수하는 것이 싸다 — D-070 이 타임아웃을 config 에 올리지 않은 것과 같은 계산이다.
5. **미확정 항목을 전부 버리거나 전부 받지 않는 이유:** 전부 버리면 발행일을 안 주는 소스가 통째로 사라지고, 전부 받으면 아카이브형 피드의 첫 실행이 그대로 게이트로 간다. 한 번 받은 항목은 원장에 들어가 다시 부르지 않으므로, 피드 순서로 소량씩 받으면 둘 다 피한다.
6. **시간대를 고정 오프셋으로 두는 이유:** IANA 시간대 이름을 쓰려면 Windows 에서 `tzdata` 라는 새 의존성이 필요하다. 문제의 소스는 둘 다 KST 이고 KST 에는 일광절약시간이 없다. 해석 규칙을 **명시하고**, 설정이 없는 소스의 naive 값은 경고로 드러낸다.
7. **UTC 날짜로 통일하는 이유:** 이미 시간대를 주는 피드(gn 판 포함)는 지금도 UTC 날짜로 들어간다. naive 값만 현지 날짜로 두면 같은 기사가 피드에 따라 다른 날짜가 된다.
8. **상한을 config 에 두는 이유:** 한국 매체 둘을 더하면 하루 신규가 50건대라 매번 CLI 로 줄 수 없다. **CLI 가 config 를 부분 병합하지 않고 대체하는 이유:** "이 소스 3건"을 승인받고 CLI 로 준 실행이 나머지 6개 소스를 config 기본값으로 함께 돌면, 승인받은 숫자와 코드가 막는 숫자가 다시 갈린다 (D-102).
9. **갱신 멈춤을 소스 단위 실패로 두는 이유:** 경고만 남기면 매일 실행에서 아무도 보지 않는다. ZDNet 사례처럼 200 을 계속 주는 고장은 종료 코드에 드러나야 한다. 소스마다 갱신 주기가 다르므로(Microsoft Research 는 10건이 두 달치, AI타임스는 50건이 이틀치) 기준은 소스별이다.

## Evidence

- **Production Data:** 2026-09-29 실측. 현재 5개 + 후보 16개 피드, 약 2,900개 엔트리에서 **발행일 없는 항목 0건**, 미래 날짜 0건. 형식은 소스마다 하나로 고정(RFC 822 또는 ISO 8601)
- **Production Data:** 피드 깊이 — OpenAI 1234건(2015-12~), Hugging Face 869건(2020-02~), DeepMind 100건(약 11개월), NVIDIA Developer 100건(2.5개월), Microsoft Research 10건(2개월), AI타임스 50건(3일), 인공지능신문 50건(8일), arXiv 20건(1일)
- **Production Data:** 7일 내 항목 — OpenAI 19, DeepMind 3, NVIDIA Developer 12, Microsoft Research 2, AI타임스 50, 인공지능신문 50, arXiv 20. 1일 내 — AI타임스 38, 인공지능신문 19
- **Production Data:** 시간대 — AI타임스 기사 `idxno=215734` 가 일반 피드 `2026-09-29 07:27:02` → `09-29`, gn 피드 `Tue, 29 Sep 2026 07:27:02 +0900` → `09-28`
- **Production Data:** ZDNet Korea `Include2/NewsSection0020.xml` — HTTP 200, 30건, 최신 2024-05-10. 같은 디렉터리의 다른 섹션 번호 12개는 404
- **Experiment:** 이 venv 에서 `zoneinfo.ZoneInfo("Asia/Seoul")` → `ZoneInfoNotFoundError` (`No module named 'tzdata'`)

## Alternatives

### 수집기에서 자른다

- **Pros:** 1200건을 파싱하지 않는다
- **Cons:** 파이프라인이 못 본 항목이 생기고 요약에 남지 않는다. `export` · `eval` 의 `collect` 동작이 바뀐다. 기준점(원장·실행 요약)을 수집기가 알아야 한다
- **Rejected because:** 파싱 비용은 LLM 비용이 아니라 작고, "무엇을 보았는가"의 기록과 수집기의 무판단 원칙을 잃는다

### 전역 기준점 (모든 소스에 하나의 "마지막 성공 실행")

- **Pros:** 상태가 하나다
- **Cons:** 한 소스만 죽어 있던 동안 다른 소스의 성공이 기준점을 옮긴다
- **Rejected because:** 죽어 있던 소스의 그 기간 항목을 잃는다

### 발행일 없는 항목을 전부 버리거나 전부 통과시킨다

- **Pros:** 규칙이 단순하다
- **Cons:** 전부 버리면 그 소스가 사라지고, 전부 통과시키면 아카이브형 피드의 첫 실행이 통째로 게이트로 간다
- **Rejected because:** 둘 다 의도와 다르다 (사용자 지적)

### IANA 시간대 이름 (`zoneinfo` + `tzdata`)

- **Pros:** 일광절약시간이 있는 시간대도 정확하다
- **Cons:** Windows 에서 `tzdata` 의존성이 새로 필요하다
- **Rejected because:** 대상 소스가 둘 다 KST(일광절약시간 없음)라 고정 오프셋으로 충분하고, 의존성을 늘릴 근거가 없다

### 소스별 창 길이

- **Pros:** arXiv 의 게시 지연을 소스에 맞춰 흡수한다
- **Cons:** 설정 항목이 늘고, 값마다 근거가 필요하다
- **Rejected because:** 다시 보는 비용이 0 이라 전역 7일로 흡수된다 (D-070 과 같은 계산)

### 실행당 상한을 CLI 로만 준다 (현행)

- **Pros:** 실행마다 사람이 숫자를 본다
- **Cons:** 매일 자동 실행이 안 된다. 사람이 옮겨 적는 숫자는 어긋난다
- **Rejected because:** 한국 매체 추가 후 하루 신규 50건대를 매번 CLI 로 줄 수 없다 (사용자 지시, D-102 와 같은 자리)

### CLI 상한을 config 상한 위에 소스별로 병합

- **Pros:** 한 소스만 바꾸기 쉽다
- **Cons:** CLI 로 한 소스를 준 실행이 나머지 소스를 config 값으로 함께 돈다
- **Rejected because:** 승인받은 숫자와 코드가 막는 숫자가 갈린다 (D-102)

## Consequences

### Positive

- 명령줄 인자 없이 `python -m pipeline run` 이 7개 소스를 config 상한으로 돈다
- OpenAI 1234건 같은 아카이브가 첫 실행에서 게이트로 가지 않는다 (7일 내 19건)
- 며칠 쉬어도, 상한 때문에 미룬 항목이 있어도, 14일 안이면 빠지지 않는다
- 시간대 해석이 명시되고, 해석 규칙이 없는 naive 값은 경고로 드러난다
- 200 을 주면서 멈춘 피드가 종료 코드 3 으로 드러난다

### Negative

- 인공지능신문처럼 시간대 없는 소스의 `published_at` 이 전보다 하루 이를 수 있다 (KST 00:00~08:59 발행분). 기존 노트·보존소는 바꾸지 않는다
- 기준점 판정이 실행 요약 파일에 의존한다. 요약을 지우면 기준점이 사라져 첫 실행처럼 7일 창으로 돈다
- 갱신 주기가 불규칙한 소스는 `max_silence_days` 를 넉넉히 잡아야 해서, 그만큼 늦게 잡힌다

### Risks

- 게시 지연이 7일보다 긴 소스가 들어오면 조용히 빠진다. `out_of_window` 건수로만 보인다
- 한국 매체 피드는 2~3일치만 주므로, 3일 넘게 쉬면 창과 무관하게 그 사이 항목을 잃는다
  > ⚠️ **정정 (2026-09-29, 아래 Amendment 1).** AI타임스 gn 판 50건은 실측 **약 28시간치**다.
  > 하루를 거르면 잃고, **상한 때문에 미룬 항목도 다음 실행 전에 밀려난다** — 창의 기준점이
  > 막아 주는 것은 창 밖으로 밀려나는 것뿐이고, 피드 밖으로 밀려나는 것은 막지 못한다.
- config 상한 기본값으로 도는 실행도 승인 게이트 대상이다. 매일 자동 실행과 실행 전 승인의 관계는 스케줄링과 함께 정한다 (이번 범위 밖)

## Implementation

- [ ] `collectors/rss.py`: 발행일 원문의 오프셋 판별, `naive_date_offset` 적용, 미설정 naive 건수 경고
- [ ] `pipeline/runner.py`: 창 계산 · 소스별 기준점 · 미확정 상한 · `stale_feed` · config 상한 해석과 CLI 대체 · 절대 천장 200/100
- [ ] 테스트: 창 경계, 기준점 이동/정지, 상한 걸림, 원장 기존 항목 면제, 미확정 상한, 시간대, `stale_feed` 와 종료 코드, CLI 대체
- [ ] `config.yaml`: 7개 소스(`limits` · `max_silence_days` · `naive_date_offset`), `pipeline.window`
- [ ] 문서: `pipeline/README.md` · `collectors/README.md` · README 결정 로그

## Reversibility

- **Reversible:** Yes
- **Rollback:** `config.yaml` 에서 `pipeline.window` 를 빼면 창이 꺼지고(전량 수용), `max_silence_days` 를 빼면 갱신 멈춤 판정이 꺼진다. CLI 상한은 그대로 쓸 수 있다. 시간대 해석은 `naive_date_offset` 을 빼면 예전처럼 UTC 로 읽는다
- **Migration Cost:** Low

---

## Amendment 1 — "완결"이 추출 대기를 보지 않았고, 피드 밖으로 밀려난 항목은 아무도 세지 않았다 (2026-09-29)

- **Status:** Accepted (사용자, 2026-09-29)
- **Decision Source:** Human

### Context

**무엇이 틀렸나.** Decision 의 "완결"은 게이트 쪽(`deferred_gate`·미확정 초과분)만 봤다.
추출 상한 때문에 미룬 항목은 원장에 있어 창과 무관하게 이어서 처리된다고 봤는데, **그
전제는 항목이 다음 실행 때 피드에 남아 있을 때만 성립한다.** 루프는 이번 피드에 있는
항목만 돈다. 얕은 피드에서 미룬 항목은 다음 실행 전에 밀려나고, 밀려난 항목은 어느
카운터에도 안 잡힌다. 그래서 다음 실행은 미룬 것이 없어 완결로 보인다.

**사라진 것이 다 처리한 것의 모양으로 나온다** — `judge_human_gap`(D-087), 무시한 gzip
헤더(D-104), 200 을 주며 멈춘 ZDNet(이 ADR 의 `max_silence_days`)에 이은 네 번째 사례다.

### Decision

- **완결을 둘로 가른다.** 실행 요약 `window.<소스>` 에
  - `window_drained` — 창 기준점용. **기존 정의 그대로**(`deferred_gate == 0` 이고 미확정 초과분 0). `last_drained` 가 이것을 읽고, 이 필드가 없는 옛 기록은 `drained` 를 같은 뜻으로 읽는다
  - `drained` — 경보용. 위에 **`deferred_extraction == 0`** 을 더한다. `not_drained` 경보의 사유에 추출 대기 건수와 "기준점이 가는가"를 함께 적는다
- **밀려난 항목을 센다.** 실행 요약에 `backlog.<소스> = {head, gate, extraction}` (피드 맨 앞 doc_id 5개, 이번에 미룬 doc_id) 를 남긴다. 다음 실행이 그 소스의 **마지막 기록**과 지금 피드를 비교해
  - `evicted_gate` / `evicted_extraction` — 미뤘던 것 중 지금 피드에 없고 원장상 여전히 대기인 것. 손실이 난 **다음 실행에서** 센다
  - `feed_rollover` — 직전 맨 앞 5개가 하나도 없다. 그 사이 피드 깊이보다 많이 들어왔다는 뜻이고, **건수는 모른다**(본 적 없는 항목은 셀 대상이 없다)
- 두 신호는 경보 종류 `evicted` · `feed_rollover` 로 올리고 **첫 회부터 경고**다. `not_drained` 는 항목이 아직 피드에 남아 있어 1회는 정보다. 둘 다 실패(종료 코드)로 세지 않는다 — 용량 문제이지 장애가 아니다
- 함께 바뀐 설정·기록 (사용자 결정, 2026-09-29): AI타임스 게이트 상한 40 → **50(피드 깊이)** · arXiv 추출 10 → **20** · 수동 `run` 도 품질 지표를 기록 · 본문 길이 중앙값은 **창 안 항목**으로 잰다(`inputs.<소스>.feed_items` 에 피드 전체 건수)

### Rationale

1. 기준점까지 추출 대기에 묶지 않는다. 추출 대기는 이미 원장에 있어 창과 무관하고, 묶으면 추출 상한이 모자란 소스는 기준점이 영영 안 움직여 창이 늘 14일 상한에 머문다 — `window_capped`(심각)이 상시 떠서 **경보가 정상 상태가 된다**
2. 게이트 상한을 피드 깊이로 두면 게이트 쪽 밀려남은 구조적으로 0 이다. 추출 상한은 "얼마나 많이 다룰 것인가"라서 주제 범위 판단이고, 손실 규모(`evicted_extraction`)를 본 뒤 정한다 (사용자)
3. arXiv 는 피드 20건·통과율 100% 에서 추출 10 이면 절반이 확정 손실이고, 논문이라 가치가 높다 (사용자)

### Evidence

- **Production Data:** 2026-09-29 첫 수동 전량 실행(`pipeline-20260929-152223`, 게이트 80 · 추출 36 · 실패 0). arXiv 게이트 20/20 통과 · 추출 10 · 미룬 추출 10 인데 `drained: true`. AI타임스 게이트 40(상한) · 통과 25 · 미룬 게이트 7 · 미룬 추출 11. NVIDIA 미룬 게이트 10 · 추출 6, OpenAI 미룬 게이트 6 · 추출 4 (둘 다 피드가 깊어 밀려나지 않는다)
- **Experiment:** AI타임스 gn 판 50건 = 09-28 11:05 ~ 09-29 14:45 KST, **27.7시간** · 환산 하루 42.5건 · 최근 24시간 35건. 루프가 피드 순서(최신 먼저)로 돌아 미룬 항목은 가장 오래된 것이다 — 하루 새 글이 약 38건이면 다음 실행 전에 밀려난다
- **Production Data:** 같은 실행의 OpenAI 본문 길이는 피드 전체 1234건 기준(0자 106건)이었고 그중 1212건이 창 밖이다

### Alternatives

#### `drained` 하나에 추출 대기까지 포함 (기준점도 그것으로)

- **Pros:** 필드가 하나다
- **Cons:** 추출 상한이 모자란 소스의 창이 늘 14일 상한에 머문다
- **Rejected because:** `window_capped`(심각)이 상시 떠서 경보의 의미가 사라진다 (Rationale 1)

#### 원장 항목에 밀려남 표시 필드를 둔다

- **Pros:** 실행 요약을 지워도 남는다
- **Cons:** 원장 형식이 바뀐다
- **Rejected because:** 원장 형식 변경(ADR-022)을 부른다. 실행 요약은 이미 창 기준점의 근거라 같은 자리에 둔다

#### 피드 전체 doc_id 를 남겨 넘김 건수를 계산

- **Pros:** 넘김 전후의 겹침을 정확히 본다
- **Cons:** OpenAI 는 1234건이다
- **Rejected because:** 그래도 **한 번도 못 본 항목은 셀 수 없다.** 넘김은 있다는 사실만 남기면 된다

### Consequences

- **Positive:** arXiv 처럼 매일 잃으면서 완결을 내던 소스가 `not_drained` · `evicted` 로 드러난다. AI타임스 추출 상한을 정할 근거(`evicted_extraction`)가 쌓인다. 수동 실행에서도 품질 기준선이 쌓여, 자동 실행 전에 임계값을 정할 수 있다
- **Negative:** AI타임스는 추출 상한 15 가 하루 통과(약 24건)보다 작아, 상한을 정할 때까지 `not_drained`(2회째부터 경고)와 `evicted` 경고가 **매 실행 뜬다.** 이건 거짓 경보가 아니라 실제 손실이다
- **Risks:** 밀려남은 **한 실행 늦게** 센다. 기록이 생기기 전(이 Amendment 이전)의 손실은 소급해서 세지 못한다. 실행 요약을 지우면 비교 기준이 사라져 그 다음 실행은 아무것도 세지 않는다. 기사 삭제만으로도 맨 앞 5개가 사라지면 `feed_rollover` 가 거짓으로 뜰 수 있다

### Implementation

- [x] `pipeline/backlog.py` (신규) · `pipeline/runner.py` (`backlog` 기록 · 두 완결 · 창 안 본문 길이 · `run` 의 품질 지표) · `pipeline/admission.py` (`window_drained` 우선 · `SourceWindow.contains`) · `pipeline/alerts.py` (`evicted` · `feed_rollover`) · `pipeline/schedule.py` (품질 계산을 `run_pipeline` 으로 이동)
- [x] 테스트 15건 + 변이 검사 11/11 (임시 복사본)
- [x] `config.yaml`: AI타임스 `gate: 50` · arXiv `extract: 20` · 피드 깊이 주석 정정

## Review Trigger

- **Recheck if:** `evicted_extraction` 이 AI타임스에서 수 회 기록된 뒤 — 추출 상한 15 를 정하는 근거로 쓰고, 그 결정 뒤 이 Amendment 를 다시 본다 (사용자)
- **Status of this ADR:** **Accepted 유지 — "완결"의 정의와 한국 매체 피드 깊이 서술은 Amendment 1 로 정정됐다.** 창 결정 자체는 번복되지 않았다. 창 기준점의 정의도 그대로다(`window_drained`)

## References

- **Related ADR:** ADR-022 (원장 · `doc_id` 중복 판정), ADR-005 (소스 구성과 본문 커버리지), ADR-025 (경보 누적 — Amendment 1 의 두 경보 종류가 여기에 들어간다)
- **Documentation:** README 결정 로그 D-106(이 결정) · D-105(소스 재편) · D-102 · D-103 · D-104 · **D-111 · D-112 · D-113 (Amendment 1)**, `docs/handoff/session-16.md` · `docs/handoff/session-18.md`, `pipeline/admission.py` · `pipeline/backlog.py`
