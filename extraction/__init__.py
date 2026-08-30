"""추출(Extraction) 레이어.

역할: 원문 1건 -> 한국어 요약 + 온톨로지 5개 필드(NewsNote) 로 구조화한다.
     이 레이어의 출력이 eval/ 의 채점 대상이자 obsidian_writer/ 의 입력이다.

구성:
    schema.py    온톨로지 스키마 정의 (SSoT). 구현됨.
    llm.py       LLM 프로바이더 추상화 (Protocol) + Anthropic 구현. 구현됨.
    extractor.py 프롬프트 조립 -> 관련성 게이트 -> LLM 호출 -> 검증. 구현됨.
    normalize.py company_aliases 기반 기업명 정규화. 구현됨.

설계 메모:
    - LLM 출력은 신뢰하지 않는다. 반드시 pydantic 으로 검증하고,
      ValidationError 는 1회 재시도(오류 메시지를 프롬프트에 되먹임) 후 격리한다.
    - 통제어휘 필드는 자유서술이 아니라 Enum 이므로, 어휘 밖 값은 실패로 처리한다.
"""
