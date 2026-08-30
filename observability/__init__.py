"""관측(Observability) 레이어.

역할: 파이프라인이 "돌긴 도는데 왜 이런 결과인지 모르겠다"가 되지 않게 한다.
     LLM 호출 trace, 토큰/비용, eval 점수 추이, 실패 건을 기록한다.

구성:
    events.py    레코드 정의 + PipelineObserver Protocol + JSONL 구현. 구현됨.
    logs/        JSON Lines 로그 (.gitignore 대상). 실행 시 생성.
    tracing.py   Langfuse 연동 래퍼. 키가 없으면 no-op. TODO
    metrics.py   실행 단위 집계(처리 건수, 실패율, 토큰, 비용). TODO

설계 메모:
    - 관측이 꺼져 있거나 쓰기에 실패해도 파이프라인은 그대로 돌아야 한다.
      관측 도구가 없다고 파이프라인이 죽으면 안 된다. (결정 로그 D-008)
    - 스킵은 append-only(감사 로그), 미등록 기업은 upsert(작업 큐). 목적이
      다르면 쓰기 방식도 다르다. (결정 로그 D-036)
    - 정규화 실패의 **기록**은 파이프라인 층에서 한다. 스키마 validator 는
      테스트·재검증에서도 돌아서 누적 카운트를 오염시킨다. (결정 로그 D-035)
"""
