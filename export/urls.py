"""`--urls` 입력 — URL 을 직접 지정해 수집한다 (계약 §12.1).

## 이게 왜 편의 기능이 아닌가

생산자는 arXiv **최근 피드**를, MARA 는 **주제 질의 결과**를 수집한다. 시점 기준과
주제 기준은 구조적으로 겹치지 않아서 **자연 중복이 발생하지 않는다** (ADR-018
Amendment, 2026-09-17). 그래서 `doc_id` 동일화 규칙은 피드만 돌려서는 **영원히
미검증 상태로 "통과한 것처럼" 보인다.** 중복 케이스를 만들 수 있는 유일한 방법이
URL 직접 주입이고, 그래서 이 입력이 1단계 구현 항목이다.

## 지원 범위 — arXiv 만

arXiv URL 은 논문 ID 로 API 를 조회하면 제목과 초록 전문을 받을 수 있다.
**그 외 URL 은 거부한다.** 임의의 기사 페이지를 직접 fetch 하는 것은 열린 항목
D-013 이고, 실행되면 피드 발췌가 아니라 기사 전문이 반입된다 — 계약 형식은
4,000자 상한으로 그 변화를 흡수하지만(§6.1) **"전문을 반입해도 되는가"는 저작권·
반입 범위 판단이라 MARA ADR-004 재검토 사안으로 남아 있다**(계약 §10).
`--urls` 를 만들면서 그 판단을 조용히 통과시키지 않는다.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime
from urllib.error import HTTPError, URLError

import feedparser

from collectors.base import RawItem
from collectors.rss import BODY_MAX_CHARS, FeedTooLargeError, fetch_feed_bytes, strip_html
from export.doc_id import arxiv_paper_id, is_arxiv

ARXIV_API = "http://export.arxiv.org/api/query"

# arXiv 는 요청이 잦으면 본문 없는 응답(301/406)으로 막는다. MARA 가 같은 IP 에서
# 이 제한 때문에 session-03 이후 코퍼스가 16건에 멈춰 있다 — **재시도를 공격적으로
# 두면 그쪽 수집까지 같이 막는다.** 간격을 넉넉히 잡고 횟수를 적게 둔다.
ARXIV_USER_AGENT = "ai-news-ontology/0.1 (contract export; https://github.com/clickshn)"
ARXIV_RETRY_DELAYS_S = (5.0, 20.0, 60.0)

# 시도 1회의 상한. **`collectors.rss.FETCH_TIMEOUT_S`(15초)보다 길다** — 값을
# 복사하지 않은 것이 아니라 방향이 반대라서다.
#
# 타임아웃과 재시도 간격은 **다른 상태를 잰다.** 타임아웃은 "서버가 아무것도 주지
# 않았다"의 상한이고(한 시도가 소켓을 얼마나 붙잡는가), 재시도 간격은 "응답은
# 했는데 비어 있었다"의 대기다(arXiv 를 언제 다시 건드리는가). 둘은 대안이 아니라
# 합쳐진다 — **끊되 곧바로 다시 걸지 않는다.**
#
# 그래서 **타임아웃을 짧게 잡을수록 재시도가 늘어난다.** 수집기는 재시도가 없어
# 짧게 잡아도 손해가 없지만 여기는 재시도 쪽이 비싼 자리라(위 rate limit 주석),
# 기다리는 편이 다시 거는 편보다 싸다 (D-071).
ARXIV_FETCH_TIMEOUT_S = 30.0

# 최악의 총 소요: **4회 × 30초 + 85초 = 205초.** 별도의 총 예산 파라미터를 두지
# 않는다 — 시도 횟수와 간격이 모두 고정이라 총합이 이미 산수로 정해져 있고,
# 마감 시각을 따로 두면 진실의 출처가 둘이 되어 서로 어긋난다. 겪어서 알게 되는
# 대신 여기 적고 로그로 보이게 한다 (D-073).
#
# ⚠️ 단, `fetch_feed_bytes` 의 상한은 **소켓 연산 1회**짜리라 조금씩 끝없이
# 흘려보내는 서버에는 위 산수가 성립하지 않는다. 수집기와 **같은 한계**이고,
# 전송을 한 곳으로 합쳐 둔 이유가 그것이다 (D-072).

# 주입 항목은 config.yaml 의 피드 소스가 아니라 id 조회로 들어온다. 피드 소스와
# 같은 이름을 붙이면 manifest 의 by_source_name 에서 둘이 섞여, 어느 것이
# 주입분인지 나중에 알 수 없다.
ARXIV_URL_SOURCE_NAME = "arXiv (id_list API)"
ARXIV_URL_TAGS = ("paper",)


class UnsupportedUrlError(ValueError):
    """본문을 가져올 경로가 없는 URL."""


def _fetch_arxiv_once(url: str, *, timeout: float):
    """arXiv 응답 1회. 실패를 예외가 아니라 **사유 문자열**로 돌려준다.

    **어떤 실패도 예외로 올려 보내지 않는다.** 수집기 쪽에서는 HTTP 상태를 직접
    보게 된 것이 이득이었지만(D-069) 여기서는 정반대다 — arXiv 의 스로틀이 본문
    없는 4xx 로 오는데(모듈 상단), 그걸 흘려 보내면 **rate limit 때문에 만든
    재시도 루프가 rate limit 이 걸리는 순간 정확히 건너뛰어진다.**
    `feedparser.parse(url)` 이 빈 피드로 삼켜 주던 것을 여기서 되돌려 넣는다.

    두 분기의 몫이 다르다. **재시도를 살리는 것은 `URLError` 계열을 잡는 것 자체**
    이고(`HTTPError` 는 그 서브클래스라 여기에 딸려 온다), 전용 `HTTPError` 분기는
    **상태 코드를 사유에 남기는** 몫이다. 코드가 없으면 로그가 "요청 실패"로 뭉개져
    스로틀(4xx)과 회선 장애가 구분되지 않는다 (D-072).

    Returns:
        (feed, None) 또는 (None, 사유). 항목이 하나도 없으면 실패로 친다 —
        빈 응답과 "정말 그 논문이 없음"을 구분하지 않는 것은 종전과 같다.
    """
    try:
        data, content_type = fetch_feed_bytes(
            url, timeout=timeout, user_agent=ARXIV_USER_AGENT
        )
    except HTTPError as exc:  # URLError 의 서브클래스라 반드시 먼저 잡는다
        return None, f"HTTP {exc.code}"
    except FeedTooLargeError as exc:
        return None, str(exc)
    except (TimeoutError, URLError) as exc:
        # 타임아웃은 연결 중이면 URLError 에 감싸여 오고, 수신 중이면 그대로 온다.
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            return None, f"응답 없음 — {timeout:g}초 타임아웃"
        return None, f"요청 실패 ({type(exc).__name__}: {reason})"
    except Exception as exc:
        return None, f"요청 실패 ({type(exc).__name__}: {exc})"

    # Content-Type 이 있을 때만 넘긴다. 수집기와 같은 형태지만 **이유의 무게가
    # 다르다** — 저쪽은 `bozo` 로 분기해서 빈 문자열이 정상 응답을 파싱 실패로
    # 만들었지만, 여기는 `entries` 만 본다. 그래서 이 가드는 지금 무엇을 막고
    # 있지 않고, 나중에 bozo 분기가 생겨도 이미 옳은 상태로 두려는 것이다.
    # (변이 검사에서 이 줄을 뒤집어도 아무 테스트도 깨지지 않는다 — 의도한 결과다.)
    parse_kwargs = {"response_headers": {"content-type": content_type}} if content_type else {}
    feed = feedparser.parse(data, **parse_kwargs)
    return (feed, None) if feed.entries else (None, "빈 응답")


def _parse_with_retry(
    url: str,
    *,
    delays: Iterable[float] | None = None,
    timeout: float | None = None,
):
    """응답을 못 받거나 비었을 때만 재시도한다.

    빈 응답과 "정말 그 논문이 없음"을 여기서 구분하지 않는다 — 둘 다 재시도해
    보고, 끝까지 비면 호출부가 **에러로** 다룬다. 조용히 빈 목록을 돌려주면
    표본 30건이 28건이 된 채로 진행되고, 그때는 이미 비용을 낸 뒤다.

    **타임아웃도 재시도 슬롯을 소비한다.** 멈춘 arXiv 는 빈 arXiv 보다 나쁜
    신호지 좋은 신호가 아니라서, 끊자마자 다시 거는 것은 이 모듈이 금하는
    공격적 재시도 그 자체다. 끊는 것과 다시 거는 시점은 따로 정한다.

    Returns:
        feed, 또는 모든 시도가 실패하면 `None`.
    """
    attempts = [0.0, *(ARXIV_RETRY_DELAYS_S if delays is None else delays)]
    timeout = ARXIV_FETCH_TIMEOUT_S if timeout is None else timeout
    reason = ""
    for i, delay in enumerate(attempts):
        # 로그를 `delay` 가 아니라 **시도 횟수**에 건다. `if delay:` 로 묶으면
        # 간격이 0 인 구성에서 재시도가 조용히 일어나고, 그러면 관측 가능성이
        # 설정값에 달리게 된다 — 로그가 없으면 "느리다"와 "재시도 중이다"가
        # 구분되지 않는다.
        if i:
            print(
                f"[arxiv] {reason} — {delay:.0f}초 뒤 재시도 ({i}/{len(attempts) - 1})",
                file=sys.stderr,
            )
            if delay:
                time.sleep(delay)
        feed, reason = _fetch_arxiv_once(url, timeout=timeout)
        if feed is not None:
            return feed
    # 조용히 포기하지 않는다. 마지막 사유가 "빈 응답"인지 "타임아웃"인지가
    # rate limit 과 네트워크 장애를 가르는 유일한 단서다.
    print(f"[arxiv] 포기 — 마지막 사유: {reason}", file=sys.stderr)
    return None


def fetch_arxiv_ids(
    paper_ids: Iterable[str],
    *,
    collected_at: date | None = None,
    timeout: float | None = None,
) -> list[RawItem]:
    """arXiv id_list 조회 1회로 여러 논문을 받는다.

    한 번에 묶어 보내는 이유는 rate limit 이다. MARA 는 같은 IP 에서 arXiv 429 로
    session-03 이후 코퍼스가 멈춰 있다 — 요청 수를 늘릴 이유가 없다.
    """
    ids = list(paper_ids)
    if not ids:
        return []

    feed = _parse_with_retry(
        f"{ARXIV_API}?id_list={','.join(ids)}&max_results={len(ids)}", timeout=timeout
    )
    if feed is None:
        return []
    today = collected_at or date.today()

    items: list[RawItem] = []
    for entry in feed.entries:
        link = entry.get("link") or entry.get("id")
        title = strip_html(entry.get("title") or "").strip()
        body = strip_html(entry.get("summary") or "")[:BODY_MAX_CHARS]
        if not link or not title:
            continue

        published = None
        parsed = getattr(entry, "published_parsed", None)
        if parsed:
            published = datetime(*parsed[:6], tzinfo=UTC).date()

        items.append(
            RawItem(
                url=link,
                title=title,
                body=body,
                source_name=ARXIV_URL_SOURCE_NAME,
                published_at=published,
                collected_at=today,
                tags=ARXIV_URL_TAGS,
            )
        )
    return items


def collect_urls(
    urls: Iterable[str],
    *,
    collected_at: date | None = None,
    timeout: float | None = None,
) -> list[RawItem]:
    """URL 목록을 RawItem 으로. arXiv 이외는 `UnsupportedUrlError`.

    반환 순서는 **입력 순서**를 따른다. API 응답 순서에 맡기면 같은 입력이
    실행마다 다른 순서로 나올 수 있고, 그러면 표본 구성이 재현되지 않는다.
    """
    urls = [u.strip() for u in urls if u and u.strip()]
    unsupported = [u for u in urls if not is_arxiv(u)]
    if unsupported:
        raise UnsupportedUrlError(
            "arXiv 이외의 URL 은 본문을 가져올 경로가 없습니다 (D-013 미구현, 계약 §10): "
            + ", ".join(unsupported)
        )

    wanted = [arxiv_paper_id(u) for u in urls]
    fetched = {
        arxiv_paper_id(str(item.url)): item
        for item in fetch_arxiv_ids(wanted, collected_at=collected_at, timeout=timeout)
    }

    missing = [pid for pid in wanted if pid not in fetched]
    if missing:
        raise UnsupportedUrlError(
            "arXiv API 가 다음 논문을 돌려주지 않았습니다 (rate limit 또는 잘못된 ID): "
            + ", ".join(missing)
        )
    return [fetched[pid] for pid in wanted]
