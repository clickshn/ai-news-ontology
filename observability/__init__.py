"""관측(Observability) 레이어.

역할: 파이프라인이 "돌긴 도는데 왜 이런 결과인지 모르겠다"가 되지 않게 한다.
     LLM 호출 trace, 토큰/비용, eval 점수 추이, 실패 건을 기록한다.

구성:
    logging.py   구조화 로그(JSON lines) 설정. TODO
    tracing.py   Langfuse 연동 래퍼. 키가 없으면 no-op. TODO
    metrics.py   실행 단위 집계(처리 건수, 실패율, 토큰, 비용). TODO

설계 메모:
    - .env 에 LANGFUSE_* 가 없으면 전부 no-op 으로 동작해야 한다.
      관측 도구가 없다고 파이프라인이 죽으면 안 된다. (결정 로그 D-008)
    - 기록할 최소 항목: 소스 URL, 모델 ID, 프롬프트 버전, 토큰 수,
      ValidationError 원문, 정규화 실패한 기업명(unknown_company).
"""
