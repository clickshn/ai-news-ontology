# pipeline/

수집 → 관련성 게이트 → 추출 → 보존 → Obsidian 적재를 **한 명령으로** 잇는다.
설계는 ADR-022, 결정 로그 D-101 · D-102.

| 파일 | 역할 |
|---|---|
| `ledger.py` | doc_id 당 단계 상태 (`data/pipeline/ledger/`). 게이트 판정·실패 횟수·격리·적재 기록 |
| `runner.py` | `plan` / `run` / `load` / `release` CLI |

```bash
python -m pipeline plan --gate-limit "GeekNews=3" --extract-limit "GeekNews=2"   # API 호출 없음
python -m pipeline run  --gate-limit "GeekNews=3" --extract-limit "GeekNews=2"   # 승인 대상
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

`--gate-limit` / `--extract-limit` 이 걸린 소스**만** 수집한다. 전역 천장
(`--max-gate-calls` / `--max-extractions`)의 기본값은 **소스별 상한의 합**이다 (D-102).
**승인받을 숫자는 `plan` 의 "승인 대상 상한" 줄이다** — "현재 피드 기준 예상"은 피드
상태에 달려 실행 때 바뀐다. 상한은 논리 호출 수이고, 스키마 재시도로 요청 수는 최대 2배다.

## 종료 코드

| 코드 | 뜻 |
|---|---|
| `0` | 실패 없음 (**정상 0건 소스는 실패가 아니다** — arXiv 주말) |
| `3` | 부분 실패 — 산출이 있거나, **수집 실패만 있고 다른 소스는 정상으로 받았다** |
| `1` | 실행이 제 역할을 못 했다 — 모든 소스가 수집 실패, 또는 LLM·적재 실패가 있는데 산출 0건 |
| `2` | 인자 오류 |

실행 요약은 `data/pipeline/runs/{run_id}.json`. 소스별 수집 결과는 `fetch` 에
`status`(`ok` · `empty` · `http_error` · `timeout` · `network_error` · `too_large` ·
`parse_error` · `no_usable_entries` · `config_error`)와 사유로 남는다 (D-103).

**수집 실패는 흐름을 멈추지 않고 종료 코드만 바꾼다.** 소스 하나가 죽으면 그 소스를
`source_error` 로 세고 다음 소스로 간다. 새 글이 없는 날(산출 0) 소스 하나가 죽었을 때
`1` 을 내지 않는 이유: 그러면 "소스 하나 장애"와 "전부 장애"가 같은 코드가 된다 —
수집기에서 가른 혼동을 종료 코드에서 다시 만드는 셈이다.

## 테스트

`tests/test_pipeline.py`. 피드·LLM·Vault 를 전부 갈아 끼우고 `tmp_path` 에만 쓴다.
