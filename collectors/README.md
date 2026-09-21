# collectors/

외부 소스 → 원문(raw item) 수집.

| 파일 | 역할 |
|---|---|
| `base.py` | `Collector` 프로토콜. `fetch() -> Iterable[RawItem]` |
| `rss.py` | `config.yaml: sources.rss` 의 피드 수집 (HTTP 는 `urllib`, 파싱은 feedparser) |

**arXiv 논문도 `rss.py` 가 받는다.** 별도 수집기가 아니라 Atom API 를 피드처럼
읽는 것이고(`sources.rss` 안에 있다), `sources.arxiv` 라는 설정 키는 없다.
RSS 공지 피드는 주말·공휴일에 0건이라 API 쿼리를 쓴다 — 상세는 `config.yaml`
주석 참고.

중복 제거(URL 정규화 + 제목 유사도)는 **아직 없다.** 재실행 시 중복 판정 기준을
정하지 않았기 때문이고(README "다음 결정이 필요한 것"), 없는 파일을 있는 것처럼
적어 두면 이 표가 코드가 아니라 계획을 설명하게 된다.

## HTTP 를 feedparser 에 맡기지 않는다

`feedparser.parse(url)` 대신 `urllib` 로 bytes 를 받아 `feedparser.parse(data)` 에
넘긴다. **`parse` 에 timeout 인자가 없기 때문**이다 — 전역 소켓 타임아웃도 기본
`None` 이라 응답하지 않는 서버 하나가 수집 전체를 무기한 붙잡았다 (D-069).

여기서 따라오는 것 둘:

- **UA 를 우리가 붙인다** (`USER_AGENT`). feedparser 가 붙여 주던 자리라서,
  안 붙이면 urllib 기본값으로 나가고 일부 CDN 이 403 으로 막는다.
- **HTTP 상태를 직접 본다.** 4xx/5xx 는 `HTTPError` 로 올라온다 —
  feedparser 가 `feed.status` 를 줄 때도 안 줄 때도 있어서 있던 우회가 없어졌다.

`FETCH_TIMEOUT_S` 는 **소켓 연산 1회**의 상한이지 총 소요 시간의 상한이 아니다.
조금씩 끝없이 흘려보내는 서버는 이 값으로 끊기지 않는다.

`fetch_feed_bytes` 는 **이 모듈 밖에서도 쓴다** — `export/urls.py` 가 arXiv API 에
같은 결함을 갖고 있었고(F13), 전송을 복사하면 위 한계가 두 벌이 된다 (D-072).
**신원(`user_agent`)만 호출자가 정하고 전송 방식은 하나로 둔다.** 재시도 정책은
공유하지 않는다 — arXiv 쪽은 rate limit 때문에 판단이 반대로 간다 (D-071).

## 규칙
- **요약하거나 분류하지 않는다.** LLM 호출은 이 레이어에 없다.
- 소스 추가는 코드가 아니라 `config.yaml` 수정으로 끝나야 한다.
- 실패한 피드 1개가 전체 실행을 죽이지 않게 한다 (per-source try/except + 로그).
- **모든 실패 경로는 한 줄을 남긴다.** 조용히 빈 리스트를 돌려주면 "새 글이
  없었다"와 구분되지 않는다 — F2 가 오래 안 보였던 이유가 그것이다.

## 출력 계약
`extraction/` 에 넘기는 최소 필드: `url`, `title`, `body`(본문/초록), `source_name`, `published_at`.
