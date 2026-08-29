# collectors/

외부 소스 → 원문(raw item) 수집.

| 파일 | 역할 |
|---|---|
| `base.py` | `Collector` 프로토콜. `fetch() -> Iterable[RawItem]` |
| `rss.py` | `config.yaml: sources.rss` 의 피드 수집 (feedparser) |
| `arxiv.py` | `config.yaml: sources.arxiv` 의 카테고리별 최근 논문 수집 |
| `dedup.py` | URL 정규화 + 제목 유사도 기반 중복 제거 |

## 규칙
- **요약하거나 분류하지 않는다.** LLM 호출은 이 레이어에 없다.
- 소스 추가는 코드가 아니라 `config.yaml` 수정으로 끝나야 한다.
- 실패한 피드 1개가 전체 실행을 죽이지 않게 한다 (per-source try/except + 로그).

## 출력 계약
`extraction/` 에 넘기는 최소 필드: `url`, `title`, `body`(본문/초록), `source_name`, `published_at`.
