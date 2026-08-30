# observability/

파이프라인 실행을 관측 가능하게 만드는 레이어.

| 파일 | 상태 | 역할 |
|---|---|---|
| `events.py` | 구현됨 | 레코드 2종 + `PipelineObserver` Protocol + `Null`/`InMemory`/`Multi`/`JSONL` 구현 |
| `logs/` | 실행 시 생성 | JSON Lines 로그. **`.gitignore` 대상** |
| `tracing.py` | TODO | Langfuse 연동 래퍼. 키가 없으면 **no-op** |
| `metrics.py` | TODO | 실행 단위 집계 — 처리 건수, 실패율, 토큰, 추정 비용 |

## 남기는 것 두 가지

### 1. 스킵 (`SkipRecord`) — D-016

관련성 게이트(`extraction/extractor.py: process_item`)가 버린 항목. 게이트도 LLM
판정이라 틀릴 수 있고, **스킵이 흔적 없이 사라지면 오탐을 검증할 방법이 없다.**

```python
SkipRecord(url, title, source_name, reason, stage, model, prompt_name, decided_at)
```

### 2. 미등록 기업 (`UnknownCompanyRecord`) — D-035

`normalize.py` 가 `company_aliases` 에서 찾지 못한 표기. 오류 기록이 아니라
**사전 보강 큐**다. `normalize.py` 가 추측 매칭을 하지 않기로 한 이상(D-025)
미등록 표기는 계속 나오고, 그중 무엇을 사전에 넣을지는 사람이 정해야 한다.

```python
UnknownCompanyRecord(raw_name, source_article, first_seen, occurrence_count)
```

기록 지점은 `CompanyRef` validator 가 아니라 **파이프라인**이다. validator 는
`source_article` 을 볼 수 없고, 테스트·골든셋 로딩·재검증에서도 돌아서 실제
실행에서 나오지 않은 줄로 `occurrence_count` 를 오염시킨다. (D-035)

## 두 파일의 쓰기 방식이 다르다 — D-036

| 파일 | 방식 | 이유 |
|---|---|---|
| `skips.jsonl` | append-only | **감사 로그.** 한 줄이 한 사건이고 근거 텍스트가 매번 다르다. 표본으로 뽑아 읽는 게 목적이라 사건을 합치면 정보가 사라진다 |
| `unknown_companies.jsonl` | upsert | **작업 큐.** 이름당 한 줄이고 `occurrence_count` 로 우선순위를 매긴다. 합치는 키는 `normalization_key` — 정규화와 같은 동치 관계라 키가 같으면 사전에 한 줄만 넣어도 둘 다 해결된다 |

합칠 때는 **첫 등장이 이긴다.** `raw_name`·`source_article`·`first_seen` 은 처음
본 값을 유지하고 `occurrence_count` 만 늘어난다.

## 켜고 끄기

```yaml
# config.yaml
observability:
  enabled: true                 # false 면 NullObserver 로 폴백
  log_dir: "observability/logs"
  skips_filename: "skips.jsonl"
  unknown_companies_filename: "unknown_companies.jsonl"
```

```python
from observability.events import observer_from_config
observer = observer_from_config(config)     # 꺼져 있으면 NullObserver
```

`process_item(observer=...)` 로 주입한다. 넘기지 않아도 파이프라인은 그대로
돈다 — D-008 원칙.

## 큐 읽기

```python
from observability.events import JSONLObserver
queue = JSONLObserver().load_unknown_companies()
for record in sorted(queue.values(), key=lambda r: -r.occurrence_count):
    print(record.occurrence_count, record.raw_name, record.source_article)
```

## 반드시 남기는 것
| 항목 | 이유 | 상태 |
|---|---|---|
| 스킵된 항목 + 판정 근거 | 게이트 오탐 표본 검토 | 구현됨 |
| `unknown_company` (정규화 실패 표기) | `config.yaml: company_aliases` 보강 큐 | 구현됨 |
| 모델 ID + 프롬프트 파일명 | eval 점수가 움직였을 때 원인 후보를 좁힌다 | 스킵 레코드에만 |
| 입력/출력 토큰 수 | 개인 프로젝트에서 비용은 실질적인 제약이다 | `Usage` 에 있으나 기록 안 함 |
| `ValidationError` 원문 | 스키마가 현실과 안 맞는 지점을 찾는 1차 신호 | TODO |
| 처리 건수 / 스킵 / 실패 | "조용히 0건 처리"를 잡아낸다 | TODO (`metrics.py`) |

## 원칙
관측은 **선택적 의존성**이다. 쓰기 실패(권한, 디스크, 잠긴 파일)는 경고만 남기고
삼킨다 — 관측 때문에 추출 결과를 잃는 건 앞뒤가 바뀐 일이다. 깨진 로그 줄도
건너뛰지, 예외를 올리지 않는다. (D-008)
