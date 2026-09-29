# collectors/

외부 소스 → 원문(raw item) 수집.

| 파일 | 역할 |
|---|---|
| `base.py` | `Collector` 프로토콜. `fetch() -> Iterable[RawItem]` |
| `rss.py` | `config.yaml: sources.rss` 의 피드 수집 (HTTP 는 `urllib`, 파싱은 feedparser). 결과는 `FeedResult` |

**arXiv 논문도 `rss.py` 가 받는다.** 별도 수집기가 아니라 Atom API 를 피드처럼
읽는 것이고(`sources.rss` 안에 있다), `sources.arxiv` 라는 설정 키는 없다.
RSS 공지 피드는 주말·공휴일에 0건이라 API 쿼리를 쓴다 — 상세는 `config.yaml`
주석 참고.

중복 제거는 **이 레이어에 없다.** 재실행 중복 판정은 `pipeline/` 이 계약의 `doc_id`
로 한다(ADR-022). 제목 유사도는 쓰지 않기로 했다 — 수집기는 판단하지 않는다는 원칙과도
맞는다.

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

## 실패 사유를 반환값에 남긴다 (D-103)

`fetch_feed` / `collect_results` 는 `FeedResult` 를 돌려준다 — 항목과 **그 항목 수가
나온 이유**(`FetchStatus`)를 같이 든다. 정상 0건(`empty`)과 장애(`http_error` ·
`timeout` · `network_error` · `too_large` · `parse_error` · `no_usable_entries` ·
`config_error`)가 다른 값이다. 예전 `fetch_source` / `collect` 는 항목만 주는 얇은
래퍼로 남았고, **거기서는 여전히 둘이 같은 `[]` 로 보인다.** 구분이 필요한 호출자
(파이프라인)는 `collect_results` 를 쓴다.

`no_usable_entries` 는 엔트리가 있는데 link/title 누락으로 **전부** 버린 경우다. 피드
형식이 바뀐 신호라 정상 0건으로 세지 않는다.

⚠️ **이것으로 안 잡히는 고장이 있다.** 200 을 계속 주면서 내용이 안 늘어나는 피드
(운영 중단된 중계, 방치된 레거시 경로)는 `ok` 다. 2026-09-29 실측에서 ZDNet Korea 의
`NewsSection0020.xml` 이 정확히 그 상태였다 — 200, 30건, 최신 항목 **2024-05-10**.

## `Content-Encoding` 을 푼다 (D-104)

요청하지 않아도 압축해서 보내는 서버가 있다. DeepMind(Google Frontend)는
`Accept-Encoding` 없이도 캐시 노드에 따라 `content-encoding: gzip` 으로 답했고
(실측 25회 중 3회), urllib 은 풀지 않아 feedparser 가 "not well-formed" 를 냈다.
session-15 의 plan 실패 / run 성공 불일치가 이것이다. `gzip` · `deflate` 를 풀고,
**푼 크기에도** `FEED_MAX_BYTES` 를 건다. 모르는 방식은 `parse_error` 다.

## 발행일은 UTC 날짜다 (ADR-023)

`published_at` 은 모든 소스에서 **UTC 날짜**다. 발행일이 수용 창의 기준이라, 소스마다
다른 기준으로 읽으면 조용히 틀린다.

- 원문에 시간대 표기(`Z`, `±hh:mm`, `±hhmm`, `GMT`/`UT`/`UTC`, 미국 약어)가 있으면 그것을 따른다.
- 없으면 소스의 `naive_date_offset`(`"+09:00"`)으로 읽는다. 설정이 없으면 UTC 로 읽고
  `[warn]` 과 `FeedResult.warning` 에 건수를 남긴다. 이 설정을 쓰던 인공지능신문은
  은퇴했다(D-107) — **지금 활성 소스 중에는 시간대 없는 발행일을 주는 곳이 없다.**
  실데이터 검증: 인공지능신문 50건 중 KST 00~08시 발행 5건이 모두 전날(UTC)로 바뀌었다.
- IANA 이름(`Asia/Seoul`)을 쓰지 않는 이유: Windows 에서 `tzdata` 의존성이 필요하고,
  대상 소스가 모두 일광절약시간 없는 KST 다.

## 수집을 멈춘 소스 (`sources.retired`, D-105)

**수집하지 않는다** — `rss_sources` / `collect` 는 보지 않는다. 제외는 앞으로만이라 이미
들어온 골든셋·노트·보존소 항목은 그대로이고, `eval.predict` 가 골든셋 URL 의 호스트로
소스 이름을 유도할 때 `retired_sources` 를 같이 본다. 지우면 GeekNews 골든셋 2건의
재추출이 "소스를 찾을 수 없다"로 멈춘다.

## 규칙
- **요약하거나 분류하지 않는다.** LLM 호출은 이 레이어에 없다.
- 소스 추가는 코드가 아니라 `config.yaml` 수정으로 끝나야 한다.
- 실패한 피드 1개가 전체 실행을 죽이지 않게 한다 (per-source try/except + 로그).
- **모든 실패 경로는 한 줄을 남긴다.** 조용히 빈 리스트를 돌려주면 "새 글이
  없었다"와 구분되지 않는다 — F2 가 오래 안 보였던 이유가 그것이다.

## 출력 계약
`extraction/` 에 넘기는 최소 필드: `url`, `title`, `body`(본문/초록), `source_name`, `published_at`.
