# Session 17 핸드오프 — 🟡 매일 자동 실행: **코드는 섰고, 켜지 않았다** (ADR-025)

- **날짜:** 2026-09-29
- **범위:** 자동 실행과 승인 구조의 관계 설계 → ADR-025 → 구현 (상시 승인 · 사전 점검 · 환경 미준비 · 경보 누적 · 전달 3경로 · 품질 지표 기록)
- **브랜치:** `feat/scheduled-run` — **병합·push 안 함**
- **테스트:** `.venv/Scripts/python.exe -m pytest tests/ -q` → **923 passed** (876 → +47). 훅 케이스 46/46
- **LLM API 호출: 0건.** 피드 GET 도 0건. 실제 Vault·원장·보존소에 쓰지 않았다
- **의존성: 새로 추가 0개**
- **ADR-025** — `adr-skill:adr-recorder` 로 작성, **Proposed** (사용자 확인 대기). 결정 로그 **D-110**

> **다음 세션이 먼저 읽을 곳:** §4(켜는 순서), §5(결정 대기).

---

## 1. 무엇을 정했나 (사용자 확인, 2026-09-29)

| 질문 | 답 |
|---|---|
| config 상한 = 상시 승인? | **아니다.** 크기 항목이다. 대신 6항목의 답을 고정한 **상시 승인 기록**과 매 실행 대조 |
| 1번이 깨지는 경로 | a provider · b 벤더 승인 파일 잔존 · c `.env` VLLM_BASE · d **스케줄러 환경변수**(`override=False` 라 OS 가 `.env` 를 이긴다) · e config `base_url` · f 모델·프롬프트 · g 소스 추가 · h 목적지 결정 코드 |
| 감지하면? | 가부 불일치 = **멈춤 + 알림 둘 다** (경고만 = ADR-017 기각안, 멈춤만 = "새 글 없는 날"과 같은 모양) |
| VPN 전 실행 | **환경 미준비(5)** 를 장애(차단기)와 가른다. TCP 연결만 본다 |
| 알림 경로 | Vault `_pipeline-status.md` **하나만 예외**(확장 금지, ADR 명시) · SessionStart 훅 · 토스트(심각만) |
| 품질 | 기록만. 임계값은 2주 뒤 |
| 만료 | 14일 + 갱신 때 노트 3건 원문 대조 — **만료 기간 = 결과를 아무도 안 보는 기간의 상한** |

핵심 발견: **지금 1번을 지키는 `assert_internal_endpoint` 는 거부 목록이라 1번에 답하지 않는다** ("벤더가 아니다"까지만). 사설 프록시·오타·다른 내부 호스트는 통과. 해석된 URL 해시 대조가 처음으로 1번에 긍정형으로 답한다.

## 2. 무엇을 만들었나

| 파일 | 내용 |
|---|---|
| `pipeline/approval.py` | 지문(provider · URL sha256 · 모델 · 프롬프트 · 소스 · 상한 · `GUARD_FILES` 6개, 전부 LF 정규화 해시), 대조, 사전 점검(순서 고정), 선행 수동 전량 실행 찾기 |
| `pipeline/schedule.py` | `scheduled`: 잠금 → 사전 점검 → 준비 확인(TCP) → `run_pipeline` → 경보 → 전달. `approve-schedule`: **TTY 필수**, 6항목 표시 + 노트 3건 대조 + 확인 문구 2개 |
| `pipeline/alerts.py` | 실행 요약 → 사건, `data/pipeline/alerts.json` 누적, 연속 횟수 등급. **평가하지 않은 종류는 닫지 않는다** |
| `pipeline/notify.py` | 상태 노트(파일명은 코드 상수) · `data/pipeline/status.txt` · 토스트(WinRT, 환경변수로 문구 전달) |
| `pipeline/quality.py` | 본문 길이 중앙값 · 게이트 통과율 · 재시도율 · 미등록 기업 비율 · 분포 — 기록만 |
| `pipeline/runner.py` | 실행 요약에 `limits`(+mode) · `inputs` · `extracted_ids` · `alerts` · `quality`. CLI 두 개 |
| `extraction/llm.py` | `resolved_vllm_endpoint` — 클라이언트와 같은 순서로 목적지 해석 (테스트가 둘을 맞댄다) |
| `.claude/hooks/pipeline-status.sh` | SessionStart. **실행 부재(36h)·승인 만료를 읽는 쪽에서 잰다** |
| `scripts/register-scheduled-task.ps1` | 작업 등록. 승인 기록 없으면 거부. StartWhenAvailable · 로그온 시만 · 동시 실행 금지 |
| `config.yaml: schedule` | `time: null`(사용자가 채움) · `readiness` · `absent_after_hours` · `toast` |

종료 코드 추가: **4** 가부 불일치 · **5** 환경 미준비 · **6** 잠금 보유.

변이 검사(임시 복사본) 18건 → 1차 1건 생존: 비TTY 거부 테스트가 **다른 선행 조건(전량 실행 없음)으로 먼저 거부**되고 있었다. TTY 가 유일한 거부 사유가 되게 고쳐 18/18 잡힘.

구현 중 잡은 버그: 잠금 나이를 **주입된 시계 − 파일 mtime** 으로 빼서 살아 있는 잠금을 죽은 것으로 넘겨받았다. 파일 시스템 시계로 통일.

## 3. 알고 쓰는 한계 (ADR-025 Risks)

- **`GUARD_FILES` 밖에 새 클라이언트 생성 경로가 생기면 못 잡는다.** 자동 실행에는 훅도 없다
- 사람이 승인 기록을 직접 편집하는 것은 못 막는다 (governance 규칙이 막는다 — 에이전트는 만들지도 고치지도 않는다)
- TCP 성공 ≠ 추론 가능. 도중에 VPN 이 끊기면 장애로 센다
- `stop_kinds` 는 파이프라인 보존소에 원본 응답이 없어 품질 지표에 없다
- 토스트는 **실기에서 한 번도 띄워 보지 않았다** (목으로만 검증)

## 4. 켜는 순서 — 전부 사용자 몫이거나 별도 승인

1. **수동 전량 실행** `python -m pipeline run` (인자 없음, config 상한 90/41) — **일반 6항목 게이트로 별도 승인.** 아직 한 번도 안 한 규모다
2. 결과 확인 (노트·요약·경보가 될 값들)
3. 사용자가 **자기 터미널에서** `.venv\Scripts\python.exe -m pipeline approve-schedule` — 3건 원문 대조 포함. 에이전트 Bash 는 stdin 이 비어 있어 통과하지 못한다(의도)
4. `config.yaml: schedule.time` 과 VPN 조건 반영(`readiness.wait_minutes`) — **사용자가 값을 줄 예정**
5. 사용자가 `scripts\register-scheduled-task.ps1` 실행
6. ⚠️ `GUARD_FILES` 나 프롬프트를 커밋할 때마다 3번을 다시 해야 한다. 이 브랜치를 병합한 뒤에 승인해야 한다 — 병합 전 승인은 병합 커밋의 파일 해시와 같을 때만 유효 (ff 병합이면 같다)

## 5. 결정 대기

| 항목 | 내용 |
|---|---|
| **ADR-025 확인** | Proposed. Accepted 로 바꿀지 |
| **실행 시각 · VPN** | 사용자가 따로 알려준다. `schedule.time`, `readiness.wait_minutes` |
| **수동 전량 실행 승인** | §4-1. plan 의 "승인 대상 상한" 줄로 요청 |
| **실기 스모크** | `pipeline scheduled` 를 승인 없이 한 번 돌리면 LLM·피드 0건으로 exit 4 가 나고 **실제 Vault 에 상태 노트 1개 + 토스트 1회**가 생긴다. 전달 경로를 실기에서 보는 유일한 방법 — 할지 |
| 품질 임계값 | 기록 2주 뒤 (2026-10-13 이후) |
| **session-16 §6 은 그대로 유효** | 같은 사건 노트 여러 건, 노트 발행일 UTC, `published_at` MARA 계약 확인, `sources.arxiv`, README 현재 상태 표 등 |
| session-14 §7 · session-15 §7 | 그대로 유효 |

## 주의

- 🆕 **`pipeline run`(수동)은 여전히 매 실행 6항목 게이트다.** 상시 승인은 `scheduled` 에만 쓴다
- 🆕 셸 heredoc 안의 파이썬에서 `"\\r\\n"` 이 `"\r\n"`(실제 CR/LF)로 들어갔다 — session-16 의 `\b` 와 같은 문제. 역슬래시가 든 코드는 Write/Edit 으로
- 🆕 venv 파이썬의 `Path.write_text` 가 이 머신에서 **CRLF 를 쓴 경우가 있었다**(runner.py · notify.py). 스크립트로 파일을 고친 뒤에는 바이트로 `\r\n` 을 세 볼 것 — `grep -c $'\r'` 는 이 셸에서 모든 줄에 걸린다(확장 안 됨)
- 🆕 `docs/handoff/session-01.md` 는 에디터 포매터가 표 정렬만 바꾼 상태다. 커밋하지 않았다
- 테스트는 가상환경으로. 실제 Vault·`eval/scores/`·보존소·원장에는 쓰지 않는다. 변이 검사는 복사본에서
