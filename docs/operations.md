# 운영 체크리스트

이 레포는 **2026-10-16 개발을 닫고 운영 모드**다 (D-124). 매일 08:10 자동 실행이 돌고,
사람이 할 일은 둘이다 — **14일마다 상시 승인 갱신**, **장애가 나면 원인 확인 후 다시 돌리기.**

설계 근거는 ADR-025(자동 실행·상시 승인·경보), D-115 · D-123(잃기까지의 여유), D-122(재시도는
사람이). 이 문서는 절차만 적는다.

## 1. 14일마다 — 상시 승인 갱신

상시 승인(`.claude/scheduled-run-approved.json`)은 14일 뒤 만료된다. 만료되면 다음 정시 실행부터
LLM 0건으로 멈춘다(종료 코드 4). 만료 시각은 기록의 `expires_at`(UTC)이다.

- [ ] **만료 전에** 대화형 터미널에서 직접 실행한다. 에이전트는 이 기록을 만들지도 고치지도 않는다

      .venv/Scripts/python.exe -m pipeline approve-schedule

- [ ] 화면의 6항목을 확인한다. **1번(목적지가 내부 vLLM 인가)이 "예"가 아니면 멈춘다**
- [ ] 화면에 나온 **노트 3건을 원문과 대조**한다 — 요약·영향도 근거가 원문에 있는가.
      품질 지표로는 근거 없는 채움이 보이지 않고, 이 대조가 그것을 잡는 유일한 장치다 (D-107)
- [ ] 3건의 **소스와 날짜**를 본다. 표본은 마지막 실행 하나에서 나오고 arXiv 는 들지 못한다
      (README "하지 않기로 한 것"). arXiv 노트 1건은 Vault 에서 직접 골라 같이 대조한다
- [ ] 수동 전량 실행은 **다시 필요 없다** — 현재 config 상한과 같은 상한으로 돈 수동 실행
      (`pipeline-20261008-140137`)이 날짜와 무관하게 자격을 유지한다

**재승인이 필요해지는 경우** (만료 전이라도):

| 바꾼 것 | 결과 | 할 일 |
|---|---|---|
| `config.yaml` 의 소스별 상한 | 상시 승인과 불일치 → exit 4 | 수동 전량 실행(`pipeline run`, 6항목 게이트) → 확인 → `approve-schedule` |
| `GUARD_FILES`(`pipeline/approval.py`) 안의 파일 | 코드 해시 불일치 → exit 4 | `approve-schedule` |
| `VLLM_BASE` · 모델 · 프롬프트 | 목적지·모델 불일치 → exit 4 | `approve-schedule` (목적지가 바뀌었다면 1번부터 다시 판단) |

개발을 닫았으므로 이 표의 왼쪽 열은 원칙적으로 일어나지 않는다. 일어났다면 운영이 아니라 개발이다.

## 2. 장애 시 — 경보 확인 → 원인 → 다시 돌리기

자동 재시도는 없다 (D-122). 점검·장애 중엔 재시도해도 실패하고, 끝난 것은 사람이 확인한다.

**시한이 있다.** AI타임스 피드는 28~62시간치다. 마지막 정상 처리부터 그 안에 다시 돌지 못하면
피드 밖으로 나간 글은 되살릴 수 없다. 심각 경보의 "여유 N시간"이 그 시한이다.

1. **경보 확인** — 어디서 보이나
   - 심각 등급: Windows 토스트
   - 모든 등급: Vault `_pipeline-status.md` · Claude Code 세션 첫 화면(SessionStart 훅)
   - 원본: `data/pipeline/alerts.json`(`open` · `closed`), `data/pipeline/runs/{run_id}.json`(실행 요약),
     `data/pipeline/runs/scheduled-*.log`(stdout·stderr 전문)
2. **원인 파악** — 아래 경보 표의 "먼저 볼 것". 엔드포인트 장애는 KT Cloud 점검 공지부터 본다
   (10/12 사례). 원인이 풀렸는지 확인하기 전에는 다시 돌리지 않는다
3. **다시 돌리기**

       .venv/Scripts/python.exe -m pipeline scheduled

   - 상시 승인 아래에서 정시 실행과 같은 대조·상한으로 돈다. 별도 승인이 필요 없고, **경보 상태를
     갱신해 회복되면 닫는다.** 10/12 복구(15:58 · 16:17 · 10/13 10:28)가 이 방식이었다
   - `pipeline run` 으로도 돌릴 수 있지만 **매번 6항목 게이트 대상**이고, 수동 실행은 **경보를
     닫지 않는다.** 복구에는 `scheduled` 를 쓴다
   - 한 번에 다 못 집으면(미룬 항목이 남으면) 한 번 더 돌린다. 10/12 는 두 번에 끝났다
4. **확인** — 종료 코드 0, `alerts.json` 의 `open` 이 비었는지, 실행 요약의 `evicted_*`(이미 잃은 것)

### 경보 종류

등급: 정보 → 경고 → 심각. **토스트는 심각에만 나간다.** 연속 횟수로 등급이 오르고, 회복된
실행에서 닫힌다. 정의는 `pipeline/alerts.py: severity`.

| 종류 | 뜻 | 등급 | 먼저 볼 것 |
|---|---|---|---|
| `preflight_mismatch` | 상시 승인과 다르다 — 만료, 목적지·모델·프롬프트·상한·코드 해시 불일치, 벤더 승인 파일이 남아 있음. LLM 0건 (exit 4) | 심각 | 만료면 §1. 아니면 무엇이 바뀌었는지 (`git log`, `.claude/external-llm-approved` 존재 여부) |
| `env_not_ready` | 엔드포인트에 TCP 로 닿지 않았다 (exit 5). 차단기와 별개 | 여유 ≤ 0 이면 심각, 아니면 정보 → 2회부터 경고 | VPN · 네트워크 · KT Cloud 점검 |
| `breaker` | 연결은 됐는데 LLM 호출이 연속 실패해 그 실행의 호출을 멈췄다 (10/12: HTTP 404) | 여유 ≤ 0 이면 심각, 아니면 경고 | 엔드포인트 상태 · 점검 공지 · 로그의 HTTP 코드 |
| `source_error` | 소스 수집 실패 (HTTP 오류 · 타임아웃 · 파싱 실패 등). 다른 소스는 계속 돈다 (exit 3) | 여유 ≤ 0 이면 심각, 아니면 정보 → 2회부터 경고 | 실행 요약 `fetch.<소스>.status` 와 사유. 일시적이면 다음 날 회복된다 |
| `stale_feed` | 200 을 주는데 최신 발행일이 `max_silence_days` 보다 오래됐다 — 피드가 멈췄다 | 경고 | 피드 URL 이 옮겨 갔는지 (ZDNet 레거시 경로 사례) |
| `feed_rollover` | 지난 실행 이후 피드가 통째로 넘어가 사이의 글을 잃었다. arXiv 는 끔(`feed_kind: sample`) | 경고 | **월요일이면 대개 주말 PC 꺼짐이다** — 설계대로의 손실. 평일이면 실행 공백을 찾는다 |
| `evicted` | 처리하지 못하고 미룬 글이 피드 밖으로 밀려났다 — **이미 잃은 것** | 경고 | 직전 장애·공백. 상한 문제면 `evicted_extraction`, 장애면 `evicted_gate` |
| `not_drained` | 상한에 걸려 미룬 글이 남았다. 아직 피드 안이라 다음 실행이 집을 수 있다 | 1회 정보 · 2회부터 경고 | 공백 다음 날이면 정상. 연속이면 상한 부족 (개발 사항) |
| `window_capped` | 필요한 수용 창이 14일 상한을 넘었다 — 이미 거르고 있다 | 심각 | 오래 실행되지 않은 소스가 있는지 |
| `quarantined` | 이번 실행에서 격리된 항목이 있다 (검증 실패 반복) | 경고 | 원장의 오류 메시지. 원인이 풀렸으면 `pipeline release <doc_id>` |
| `run_error` | 실행 중 예외 (exit 1) | 심각 | `scheduled-*.log` 의 트레이스백 |
| `lock_held` | 다른 실행이 돌고 있어 시작하지 않았다 (exit 6) | 정보 | 수동 복구와 정시 실행이 겹쳤는지. 오래 남으면 멈춘 프로세스 |

### 종료 코드

`0` 실패 없음 · `3` 부분 실패(소스 하나 장애 등) · `1` 실행이 제 역할을 못 함 · `2` 인자 오류 ·
`4` 가부 불일치 · `5` 환경 미준비 · `6` 잠금 보유 중. 상세는 `pipeline/README.md`.

## 3. 알고 있는 동작 — 놀라지 않을 것

- **월요일 `feed_rollover`(AI타임스)** — 주말에 PC 를 끄면 토·일 실행이 없고, 피드 깊이 밖으로
  나간 주말 기사가 빠진다. 막는 방법은 PC 를 켜 두는 것뿐이다. 상한으로는 못 고친다
- **첫 화면이 깨끗해도 지난 기간이 깨끗했던 것은 아니다.** 상태 파일은 현재 상태만 보여 준다.
  회복된 실패와 지나간 공백은 `alerts.json` 의 `closed` 와 `runs/` 목록에만 남는다 (D-118)
- **arXiv 는 경보가 적다** — 표본형 피드라 `feed_rollover` 를 세지 않는다 (D-117). 신규 0 인 날도 있다
- **작업 스케줄러는 메인 체크아웃을 그대로 실행한다.** 이 디렉터리에서 브랜치를 바꿔 두면 다음
  08:10 은 그 브랜치 코드로 돈다. 작업이 끝나면 `main` 으로 돌려 둔다

## 4. 아직 실기로 확인되지 않은 것

다음 해당 사건 때 확인한다. 확인되면 이 목록에서 지운다.

- [ ] **브레이커 → 심각 → 토스트** (D-123). 다음 엔드포인트 장애 때 실행 요약 `scheduled.delivery` 에
      `toast: ok` 가 있는지, 사유에 "마지막 정상 처리 N시간 전"이 붙는지
- [ ] **`StartWhenAvailable` 따라잡기.** 08:10 에 PC 가 꺼져 있다가 켜졌을 때 바로 도는지.
      작업 스케줄러 운영 로그(`Microsoft-Windows-TaskScheduler/Operational`)가 꺼져 있어 지금은 확인할
      수 없다 — 켜 두면 다음 기회에 보인다

## 5. 자주 쓰는 명령

```bash
# 상태 보기 (API 호출 없음)
cat data/pipeline/status.txt
.venv/Scripts/python.exe -c "import json;d=json.load(open('data/pipeline/alerts.json',encoding='utf-8'));print(json.dumps(d['open'],ensure_ascii=False,indent=1))"
ls data/pipeline/runs/ | tail                               # 최근 실행
.venv/Scripts/python.exe -m pipeline plan                   # 다음 실행이 무엇을 할지, 상한

# 운영
.venv/Scripts/python.exe -m pipeline approve-schedule       # 14일마다, 사람만, 대화형
.venv/Scripts/python.exe -m pipeline scheduled              # 장애 복구용 재실행
.venv/Scripts/python.exe -m pipeline release <doc_id>       # 격리 해제
.venv/Scripts/python.exe -m pipeline load                   # 보존소 → Vault 만 (LLM 없음)

# 미등록 기업 큐 (별칭 보강 기준: 서로 다른 기사 3건, D-114)
.venv/Scripts/python.exe -m observability.events queue

# 테스트
.venv/Scripts/python.exe -m pytest tests/ -q
```

```powershell
# 작업 스케줄러
Get-ScheduledTask -TaskName "ai-news-ontology daily" | Get-ScheduledTaskInfo   # 마지막·다음 실행, 결과
powershell -ExecutionPolicy Bypass -File scripts\register-scheduled-task.ps1   # 재등록 (사람이)
```

## 6. 운영을 멈출 때

- 작업 해제: `scripts\register-scheduled-task.ps1 -Unregister`
- 상시 승인 기록은 만료되게 두거나 지운다. 남겨 두면 다시 등록했을 때 그대로 돈다
- 로컬 산출물(원장·보존소·실행 요약)의 보존·삭제는 `docs/governance.md` "로컬 산출물 취급"
