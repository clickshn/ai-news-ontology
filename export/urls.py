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

from collections.abc import Iterable
from datetime import UTC, date, datetime

import feedparser

from collectors.base import RawItem
from collectors.rss import BODY_MAX_CHARS, strip_html
from export.doc_id import arxiv_paper_id, is_arxiv

ARXIV_API = "http://export.arxiv.org/api/query"

# 주입 항목은 config.yaml 의 피드 소스가 아니라 id 조회로 들어온다. 피드 소스와
# 같은 이름을 붙이면 manifest 의 by_source_name 에서 둘이 섞여, 어느 것이
# 주입분인지 나중에 알 수 없다.
ARXIV_URL_SOURCE_NAME = "arXiv (id_list API)"
ARXIV_URL_TAGS = ("paper",)


class UnsupportedUrlError(ValueError):
    """본문을 가져올 경로가 없는 URL."""


def fetch_arxiv_ids(paper_ids: Iterable[str], *, collected_at: date | None = None) -> list[RawItem]:
    """arXiv id_list 조회 1회로 여러 논문을 받는다.

    한 번에 묶어 보내는 이유는 rate limit 이다. MARA 는 같은 IP 에서 arXiv 429 로
    session-03 이후 코퍼스가 멈춰 있다 — 요청 수를 늘릴 이유가 없다.
    """
    ids = list(paper_ids)
    if not ids:
        return []

    feed = feedparser.parse(f"{ARXIV_API}?id_list={','.join(ids)}&max_results={len(ids)}")
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


def collect_urls(urls: Iterable[str], *, collected_at: date | None = None) -> list[RawItem]:
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
    fetched = {arxiv_paper_id(str(item.url)): item for item in fetch_arxiv_ids(wanted, collected_at=collected_at)}

    missing = [pid for pid in wanted if pid not in fetched]
    if missing:
        raise UnsupportedUrlError(
            "arXiv API 가 다음 논문을 돌려주지 않았습니다 (rate limit 또는 잘못된 ID): "
            + ", ".join(missing)
        )
    return [fetched[pid] for pid in wanted]
