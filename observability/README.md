# observability/

파이프라인 실행을 관측 가능하게 만드는 레이어.

| 파일 | 상태 | 역할 |
|---|---|---|
| `events.py` | 인터페이스만 | `SkipRecord` + `PipelineObserver` Protocol + `NullObserver` / `InMemoryObserver` |
| `logging.py` | TODO | 구조화 로그(JSON lines) 설정. `FileObserver` 구현 |
| `tracing.py` | TODO | Langfuse 연동 래퍼. 키가 없으면 **no-op** |
| `metrics.py` | TODO | 실행 단위 집계 — 처리 건수, 실패율, 토큰, 추정 비용 |

## 스킵 기록 (D-016)

관련성 게이트(`extraction/extractor.py: process_item`)가 버린 항목은 `SkipRecord`
로 observer 에 전달된다. 게이트도 LLM 판정이라 틀릴 수 있고, **스킵이 흔적 없이
사라지면 오탐을 검증할 방법이 없다.**

```python
SkipRecord(url, title, source_name, reason, stage, model, prompt_name, decided_at)
```

기본 구현은 `NullObserver`(아무것도 하지 않음)다. `process_item(observer=...)` 로
주입하며, 넘기지 않아도 파이프라인은 그대로 돈다 — D-008 원칙.

## 반드시 남기는 것
| 항목 | 이유 |
|---|---|
| 모델 ID + effort + 프롬프트 파일 해시 | eval 점수가 움직였을 때 원인 후보를 좁힌다 |
| 입력/출력 토큰 수 | 개인 프로젝트에서 비용은 실질적인 제약이다 |
| `ValidationError` 원문 | 스키마가 현실과 안 맞는 지점을 찾는 1차 신호 |
| `unknown_company` (정규화 실패 표기) | `config.yaml: company_aliases` 보강 큐 |
| 처리 건수 / 스킵 / 실패 | "조용히 0건 처리"를 잡아낸다 |

## 원칙
관측은 **선택적 의존성**이다. `LANGFUSE_*` 가 비어 있으면 조용히 no-op 으로
동작해야 하고, 관측 도구의 장애가 파이프라인 장애가 되어서는 안 된다.
