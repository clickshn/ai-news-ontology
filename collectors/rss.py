"""RSS/Atom 수집기.

config.yaml 의 `sources.rss` 목록을 feedparser 로 파싱해 `RawItem` 으로 변환한다.

이 모듈은 **판단하지 않는다.** 요약도 분류도 하지 않고, 피드가 준 텍스트에서
HTML 태그만 벗겨 그대로 넘긴다. (README 결정 로그 D-001 / collectors/README.md)

CLI 확인용:
    python -m collectors.rss --limit 5
    python -m collectors.rss --source "Anthropic News" --limit 3 --json
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import feedparser
import yaml
from pydantic import ValidationError

from collectors.base import RawItem

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# 피드 본문은 길이가 제각각이다. LLM 입력 비용을 예측 가능하게 두려고 여기서 자른다.
# 잘린 사실은 extractor 프롬프트에서 "본문이 잘렸을 수 있다"로 명시한다.
BODY_MAX_CHARS = 4000

# 소켓 연산(연결 / 1회 수신) 하나의 상한.
#
# **총 소요 시간의 상한이 아니다.** 서버가 조금씩 끊임없이 흘려보내면 수신할
# 때마다 시간이 새로 주어져 전체는 이 값보다 길어질 수 있다. 막으려던 것은
# "응답을 시작하지 않는 서버"였고 그건 이 값으로 끊긴다. 총 예산까지 거는
# 것은 별도 판단이라 handoff 의 결정 대기로 남겼다.
FETCH_TIMEOUT_S = 15.0

# 응답 본문 상한. 끝나지 않는 스트림에 메모리를 내주지 않기 위한 것이다.
# 실제 피드는 arXiv 20건 Atom 이 ~200KB 수준이라 여유가 크다.
FEED_MAX_BYTES = 8 * 1024 * 1024

# urllib 기본 UA(`Python-urllib/3.x`)는 일부 CDN 이 403 으로 막는다. feedparser
# 가 직접 요청할 때는 자기 UA 를 붙여 줬고, 그 자리를 우리가 가져왔으므로
# 이름도 같이 가져온다. `export/urls.py` 의 `ARXIV_USER_AGENT` 와 같은 형식.
USER_AGENT = "ai-news-ontology/0.1 (feed collector; https://github.com/clickshn)"
FEED_ACCEPT = "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.8"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_BLANKLINE_RE = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """config.yaml 을 읽는다. 없거나 깨졌으면 그대로 예외를 올린다.

    설정 파일 문제는 조용히 넘어가면 안 되는 종류의 실패다 — 피드 1개 실패와 달리
    전체 실행의 전제가 무너진 상황이므로 여기서는 방어하지 않는다.
    """
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ValueError(f"config.yaml 의 최상위가 매핑이 아닙니다: {path}")
    return config


def rss_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    """config 에서 RSS 소스 목록을 꺼낸다."""
    sources = (config.get("sources") or {}).get("rss") or []
    if not isinstance(sources, list):
        raise ValueError("config.yaml: sources.rss 는 리스트여야 합니다")
    return sources


# ---------------------------------------------------------------------------
# 정리 유틸
# ---------------------------------------------------------------------------
def strip_html(raw: str) -> str:
    """피드 본문에서 태그를 벗기고 공백을 정리한다.

    완전한 HTML 파서가 아니다. 피드 요약문 수준의 단순 마크업만 다루면 되므로
    의존성을 늘리지 않고 정규식으로 처리한다. 태그 안에 '>' 가 들어간 병리적
    입력은 정확히 처리되지 않지만, 그 경우에도 텍스트가 남으므로 치명적이지 않다.
    """
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANKLINE_RE.sub("\n\n", text)
    return text.strip()


def _entry_body(entry: Any) -> str:
    """엔트리에서 가장 정보량이 많은 텍스트를 고른다.

    content(전문) > summary(요약) 순. arXiv 는 summary 에 초록을 담고,
    블로그 피드는 content 에 전문을 담는 경우가 많다.
    """
    candidates: list[str] = []
    for block in getattr(entry, "content", None) or []:
        value = block.get("value") if isinstance(block, dict) else None
        if value:
            candidates.append(value)
    for key in ("summary", "description"):
        value = entry.get(key) if hasattr(entry, "get") else None
        if value:
            candidates.append(value)

    if not candidates:
        return ""
    best = max((strip_html(c) for c in candidates), key=len)
    return best[:BODY_MAX_CHARS]


def _entry_date(entry: Any) -> date | None:
    """발행일 파싱. 피드마다 필드명이 달라 후보를 순회한다."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).date()
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
class FeedTooLargeError(ValueError):
    """응답이 `FEED_MAX_BYTES` 를 넘었다. 잘라서 파싱하지 않고 실패로 다룬다."""


def fetch_feed_bytes(
    url: str,
    *,
    timeout: float,
    user_agent: str = USER_AGENT,
    accept: str = FEED_ACCEPT,
) -> tuple[bytes, str | None]:
    """피드/API 본문을 bytes 로 받아온다.

    **`feedparser.parse(url)` 에 URL 을 넘기지 않는 이유는 타임아웃을 걸 자리가
    없기 때문이다.** feedparser 6.x 의 `parse` 시그니처에 timeout 인자가 없고
    전역 소켓 타임아웃도 기본 `None` 이라, 연결만 받고 응답을 시작하지 않는
    서버 하나가 수집 전체를 **무기한** 붙잡는다. 그 상태에서는 로그도 남지
    않아서 어느 피드에서 멈췄는지조차 알 수 없다 (session-04 F2).

    부수 효과가 하나 있다: **HTTP 상태를 직접 보게 된다.** 4xx/5xx 는
    `HTTPError` 로 올라오므로 호출부의 `getattr(feed, "status", None)` 우회가
    필요 없어졌다 — feedparser 가 상태를 노출할 때도 있고 아닐 때도 있어서
    있던 우회다.

    **이 모듈 밖에서도 쓴다.** `export/urls.py` 가 arXiv API 에 같은 결함을
    갖고 있었고(session-06 F13), 전송을 복사하면 상한·크기 제한·slow-drip 한계가
    두 벌이 된다. 신원(`user_agent`)만 호출자가 정하고 **전송 방식은 하나로 둔다**
    (D-072). 의존 방향은 기존과 같다 — `export` → `collectors`.

    Args:
        user_agent: 호출자 신원. arXiv 는 ToS 상 식별 가능한 UA 를 요구하고,
            피드 수집과 계약 export 는 **다른 클라이언트**라 이름을 나눠 둔다.

    Returns:
        (본문 bytes, Content-Type 헤더). 헤더는 feedparser 에 그대로 넘겨
        인코딩 판정 힌트를 잃지 않기 위한 것이다.
    """
    request = Request(url, headers={"User-Agent": user_agent, "Accept": accept})
    with urlopen(request, timeout=timeout) as response:
        # 상한을 넘겼는지 알아야 해서 1바이트 더 읽는다. 잘린 XML 을 그대로
        # 파싱하면 bozo 로 흘러가 원인이 "파싱 실패"로 잘못 기록된다.
        data = response.read(FEED_MAX_BYTES + 1)
        content_type = response.headers.get("Content-Type")

    if len(data) > FEED_MAX_BYTES:
        raise FeedTooLargeError(f"응답이 상한 {FEED_MAX_BYTES:,} 바이트를 넘었습니다")
    return data, content_type


def fetch_source(
    source: dict[str, Any],
    *,
    limit: int | None = None,
    timeout: float | None = None,
) -> list[RawItem]:
    """피드 1개를 수집한다.

    예외 처리 방침(이번 단계):
      - 네트워크/파싱 실패는 여기서 흡수하고 빈 리스트를 반환한다. 피드 1개가
        전체 실행을 죽이면 안 된다.
      - 재시도는 넣지 않는다. 어떤 실패가 실제로 잦은지 모르는 상태에서 만든
        재시도 정책은 대개 틀린다. 실패 양상을 관찰한 뒤 붙인다.
      - 개별 엔트리의 검증 실패(ValidationError)는 그 엔트리만 건너뛴다.
      - **모든 실패는 한 줄을 남긴다.** 조용히 빈 리스트를 돌려주면 "피드에
        새 글이 없었다"와 구분이 안 된다.

    Args:
        timeout: 소켓 연산 1회의 상한(초). None 이면 `FETCH_TIMEOUT_S`.
    """
    name = source.get("name") or source.get("url", "<unnamed>")
    url = source.get("url")
    if not url:
        print(f"[skip] {name}: url 이 없습니다", file=sys.stderr)
        return []

    timeout = FETCH_TIMEOUT_S if timeout is None else timeout

    try:
        data, content_type = fetch_feed_bytes(url, timeout=timeout)
    except HTTPError as exc:  # URLError 의 서브클래스라 반드시 먼저 잡는다
        print(f"[fail] {name}: HTTP {exc.code}", file=sys.stderr)
        return []
    except FeedTooLargeError as exc:
        print(f"[fail] {name}: {exc}", file=sys.stderr)
        return []
    except (TimeoutError, URLError) as exc:
        # 타임아웃은 연결 중이면 URLError 에 감싸여 오고, 수신 중이면 그대로 온다.
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            print(f"[fail] {name}: 응답 없음 — {timeout:g}초 타임아웃", file=sys.stderr)
        else:
            print(f"[fail] {name}: 피드 요청 실패 ({type(exc).__name__}: {reason})", file=sys.stderr)
        return []
    except Exception as exc:
        print(f"[fail] {name}: 피드 요청 실패 ({type(exc).__name__}: {exc})", file=sys.stderr)
        return []

    # Content-Type 이 있을 때만 넘긴다. 빈 문자열을 넘기면 feedparser 가
    # "is not an XML media type" 으로 bozo 를 세워 파싱 경고가 가짜로 뜬다.
    parse_kwargs = {"response_headers": {"content-type": content_type}} if content_type else {}
    try:
        feed = feedparser.parse(data, **parse_kwargs)
    except Exception as exc:  # feedparser 는 대개 삼키지만 방어적으로 잡는다
        print(f"[fail] {name}: 파싱 실패 ({type(exc).__name__}: {exc})", file=sys.stderr)
        return []

    if getattr(feed, "bozo", False) and not feed.entries:
        reason = getattr(feed, "bozo_exception", "unknown")
        print(f"[fail] {name}: 파싱 실패 ({reason})", file=sys.stderr)
        return []

    entries = feed.entries[:limit] if limit else feed.entries
    tags = tuple(source.get("tags") or ())
    today = date.today()

    items: list[RawItem] = []
    for entry in entries:
        link = entry.get("link")
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        try:
            items.append(
                RawItem(
                    url=link,
                    title=title,
                    body=_entry_body(entry),
                    source_name=name,
                    published_at=_entry_date(entry),
                    collected_at=today,
                    tags=tags,
                )
            )
        except ValidationError as exc:
            print(f"[skip] {name}: 엔트리 검증 실패 {link} ({exc.error_count()}건)", file=sys.stderr)
            continue

    return items


def collect(
    config: dict[str, Any] | None = None,
    *,
    source_name: str | None = None,
    limit_per_source: int | None = None,
    timeout: float | None = None,
) -> Iterator[RawItem]:
    """설정의 모든 RSS 소스를 순회하며 RawItem 을 흘려보낸다.

    Args:
        config: 미리 읽은 설정. None 이면 기본 경로에서 읽는다.
        source_name: 지정하면 해당 이름의 소스만 수집한다.
        limit_per_source: 소스당 최대 건수.
        timeout: 소켓 연산 1회의 상한(초). 소스**마다** 적용된다 — 전체 실행의
            상한이 아니다. None 이면 `FETCH_TIMEOUT_S`.
    """
    config = config if config is not None else load_config()
    for source in rss_sources(config):
        if source_name and source.get("name") != source_name:
            continue
        yield from fetch_source(source, limit=limit_per_source, timeout=timeout)


# ---------------------------------------------------------------------------
# CLI (동작 확인용)
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSS 수집 결과를 표준출력에 찍는다 (확인용)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config.yaml 경로")
    parser.add_argument("--source", default=None, help="소스 이름으로 필터")
    parser.add_argument("--limit", type=int, default=5, help="소스당 최대 건수")
    parser.add_argument("--json", action="store_true", help="JSON Lines 로 출력")
    parser.add_argument(
        "--timeout",
        type=float,
        default=FETCH_TIMEOUT_S,
        help=f"소켓 연산 1회의 상한(초). 기본 {FETCH_TIMEOUT_S:g}",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    items = list(
        collect(
            config,
            source_name=args.source,
            limit_per_source=args.limit,
            timeout=args.timeout,
        )
    )

    for item in items:
        if args.json:
            print(json.dumps(item.model_dump(mode="json"), ensure_ascii=False))
        else:
            preview = item.body[:160].replace("\n", " ")
            print(f"[{item.source_name}] {item.published_at} {item.title}")
            print(f"  {item.url}")
            print(f"  {preview}{'...' if len(item.body) > 160 else ''}\n")

    print(f"총 {len(items)}건", file=sys.stderr)
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
