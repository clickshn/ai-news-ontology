# collectors/

외부 소스 → 원문(raw item) 수집.

| 파일 | 역할 |
|---|---|
| `base.py` | `Collector` 프로토콜. `fetch() -> Iterable[RawItem]` |
| `rss.py` | `config.yaml: sources.rss` 의 피드 수집 (feedparser) |

**arXiv 논문도 `rss.py` 가 받는다.** 별도 수집기가 아니라 Atom API 를 피드처럼
읽는 것이고(`sources.rss` 안에 있다), `sources.arxiv` 라는 설정 키는 없다.
RSS 공지 피드는 주말·공휴일에 0건이라 API 쿼리를 쓴다 — 상세는 `config.yaml`
주석 참고.

중복 제거(URL 정규화 + 제목 유사도)는 **아직 없다.** 재실행 시 중복 판정 기준을
정하지 않았기 때문이고(README "다음 결정이 필요한 것"), 없는 파일을 있는 것처럼
적어 두면 이 표가 코드가 아니라 계획을 설명하게 된다.

## 규칙
- **요약하거나 분류하지 않는다.** LLM 호출은 이 레이어에 없다.
- 소스 추가는 코드가 아니라 `config.yaml` 수정으로 끝나야 한다.
- 실패한 피드 1개가 전체 실행을 죽이지 않게 한다 (per-source try/except + 로그).

## 출력 계약
`extraction/` 에 넘기는 최소 필드: `url`, `title`, `body`(본문/초록), `source_name`, `published_at`.
