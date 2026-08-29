"""수집(Ingestion) 레이어.

역할: 외부 소스에서 원문을 가져와 정규화된 dict/RawItem 형태로 넘긴다.
     여기서는 **판단을 하지 않는다** — 요약·분류는 extraction/ 의 책임이다.

구성 예정:
    rss.py       feedparser 기반 RSS/Atom 수집
    arxiv.py     arXiv API 기반 논문 수집 (config.yaml 의 categories)
    dedup.py     URL 정규화 + 제목 유사도 기반 중복 제거
    base.py      Collector 프로토콜 (fetch() -> Iterable[RawItem])

설계 메모:
    - 소스 목록은 코드가 아니라 config.yaml 에 둔다. 소스 추가가 코드 변경이
      되지 않게 하기 위함.
    - 재실행 안전성(idempotency)은 URL 해시 기준으로 obsidian_writer 단계에서
      최종 판정한다. 수집 단계는 중복을 허용한다.
"""
