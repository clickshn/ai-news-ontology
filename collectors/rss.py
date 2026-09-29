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
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
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

# 발행일 원문 끝에 시간대 표기가 있는가. feedparser 는 표기가 없는 값을 UTC 로
# 간주해 파싱하므로(`*_parsed`), 원문을 따로 봐야 "UTC 였다"와 "표기가 없었다"를
# 가를 수 있다. 미국 약어(EST 등)는 RFC 822 가 허용하는 것만.
_EXPLICIT_TZ_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2}|\b(?:GMT|UTC|UT|[ECMP][SD]T))\s*$", re.IGNORECASE)
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")

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


def retired_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    """수집을 멈춘 소스 (`sources.retired`). **수집하지 않는다** — `collect` 는 보지 않는다.

    남겨 두는 이유: 제외는 앞으로만이다(D-105). 이미 들어온 골든셋·노트·보존소 항목은
    그대로이고, `eval.predict` 는 골든셋 URL 의 호스트로 소스 이름을 유도한다. 목록에서
    지우면 GeekNews 골든셋 2건(#33001 · #33003)의 재추출이 "소스를 찾을 수 없다"로 멈춘다.
    """
    retired = (config.get("sources") or {}).get("retired") or []
    if not isinstance(retired, list):
        raise ValueError("config.yaml: sources.retired 는 리스트여야 합니다")
    return retired


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


class InvalidOffsetError(ValueError):
    """`naive_date_offset` 형식이 `+09:00` / `-0500` 꼴이 아니다."""


def parse_offset(value: str | None) -> timedelta | None:
    """config 의 `naive_date_offset`("+09:00") 을 timedelta 로. 없으면 None."""
    if value is None or value == "":
        return None
    match = _OFFSET_RE.match(str(value).strip())
    if not match:
        raise InvalidOffsetError(f"naive_date_offset 형식이 아닙니다: {value!r} (예: \"+09:00\")")
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    return -delta if sign == "-" else delta


def _entry_datetime(entry: Any, naive_offset: timedelta | None = None) -> tuple[date | None, bool]:
    """발행일(UTC 날짜)과 **원문에 시간대 표기가 없었는지**를 돌려준다. `_entry_moment` 참고."""
    moment, naive = _entry_moment(entry, naive_offset)
    return (moment.date() if moment else None), naive


def _entry_moment(entry: Any, naive_offset: timedelta | None = None) -> tuple[datetime | None, bool]:
    """발행 시각(UTC, naive datetime)과 **원문에 시간대 표기가 없었는지**를 돌려준다.

    `published_at` 은 모든 소스에서 **UTC 날짜**다 (ADR-023). 시간대를 주는 피드는
    feedparser 가 이미 UTC 로 바꿔 준다. 표기가 없는 값(`2026-09-29 07:27:02`,
    AI타임스·인공지능신문)은 feedparser 가 그 숫자를 UTC 로 간주하므로, 소스의
    `naive_date_offset` 이 있으면 그만큼 빼서 UTC 로 옮긴다. 없으면 UTC 로 읽고
    naive 였다는 사실만 돌려준다 — 호출자가 경고로 드러낸다.

    피드마다 필드명이 달라 후보를 순회한다.
    """
    for key in ("published", "updated", "created"):
        parsed = getattr(entry, f"{key}_parsed", None)
        if not parsed:
            continue
        try:
            moment = datetime(*parsed[:6])
        except (TypeError, ValueError):
            continue
        raw = entry.get(key) if hasattr(entry, "get") else None
        naive = bool(raw) and not _EXPLICIT_TZ_RE.search(str(raw).strip())
        if naive and naive_offset is not None:
            moment -= naive_offset
        return moment, naive
    return None, False


def _entry_date(entry: Any, naive_offset: timedelta | None = None) -> date | None:
    """발행일(UTC 날짜). `_entry_datetime` 참고."""
    return _entry_datetime(entry, naive_offset)[0]


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
class FeedTooLargeError(ValueError):
    """응답이 `FEED_MAX_BYTES` 를 넘었다. 잘라서 파싱하지 않고 실패로 다룬다."""


class FeedDecodeError(ValueError):
    """`Content-Encoding` 을 풀 수 없다 (모르는 방식이거나 압축 데이터가 깨졌다)."""


def _decode_body(data: bytes, content_encoding: str | None) -> bytes:
    """`Content-Encoding` 대로 본문을 푼다. 푼 크기에도 `FEED_MAX_BYTES` 를 건다.

    **요청하지 않아도 압축해서 보내는 서버가 있다.** `Accept-Encoding` 을 보내지
    않았는데 DeepMind(Google Frontend)는 캐시 노드에 따라 `content-encoding: gzip`
    으로 답했다 — 2026-09-29 실측 6회 중 1회, 40회 중 2회째. urllib 은 풀지 않으므로
    feedparser 가 바이너리를 받아 "not well-formed" 를 냈다. session-15 에서 plan 때
    실패하고 run 때 살아난 것이 이것이다 — 피드가 불안정한 게 아니라 **서버가 제대로
    붙인 헤더를 우리가 무시했다** (D-104).

    압축 해제 후 크기에도 상한을 거는 이유: 상한이 압축된 크기에만 걸리면 작은
    gzip 이 메모리를 얼마든지 가져갈 수 있다.
    """
    encoding = (content_encoding or "").strip().lower()
    if encoding in ("", "identity"):
        return data
    if encoding in ("gzip", "x-gzip"):
        wbits = 16 + zlib.MAX_WBITS
    elif encoding == "deflate":
        # 표준은 zlib 래퍼지만 raw deflate 를 보내는 서버가 있다. 헤더로 판별한다.
        wbits = zlib.MAX_WBITS if data[:1] == b"\x78" else -zlib.MAX_WBITS
    else:
        raise FeedDecodeError(f"Content-Encoding {encoding!r} 을 풀 수 없습니다")
    try:
        inflater = zlib.decompressobj(wbits)
        out = inflater.decompress(data, FEED_MAX_BYTES + 1)
    except zlib.error as exc:
        raise FeedDecodeError(f"{encoding} 압축 해제 실패 ({exc})") from exc
    if len(out) > FEED_MAX_BYTES or inflater.unconsumed_tail:
        raise FeedTooLargeError(f"압축을 푼 응답이 상한 {FEED_MAX_BYTES:,} 바이트를 넘었습니다")
    return out


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
        content_encoding = response.headers.get("Content-Encoding")

    if len(data) > FEED_MAX_BYTES:
        raise FeedTooLargeError(f"응답이 상한 {FEED_MAX_BYTES:,} 바이트를 넘었습니다")
    return _decode_body(data, content_encoding), content_type


class FetchStatus(str, Enum):
    """피드 1개를 받은 결과가 **어떤 종류**였는가.

    예전에는 모든 실패가 로그 한 줄과 빈 리스트였고, 호출자에게는 "피드가 비었다"와
    같은 모양으로 보였다. session-15 plan 때 DeepMind 가 파싱 실패로 0건이었는데
    파이프라인에는 `collected=0` 경고로만 보였고 종료 코드는 0 이었다. arXiv 의 주말
    0건(정상)과 모양이 같았기 때문이다. 그래서 **사유를 반환값에 남긴다** (D-103).
    """

    OK = "ok"
    EMPTY = "empty"  # 피드는 정상이고 항목이 0건이다 (arXiv 주말)
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"  # DNS·연결 거부·TLS 등, 타임아웃이 아닌 전송 실패
    TOO_LARGE = "too_large"
    PARSE_ERROR = "parse_error"
    # 엔트리는 있는데 link/title 이 없거나 검증에 걸려 **전부** 버렸다. 피드 형식이
    # 바뀐 신호다. 정상 0건으로 읽으면 안 된다.
    NO_USABLE_ENTRIES = "no_usable_entries"
    CONFIG_ERROR = "config_error"  # url 이 없다


@dataclass(frozen=True)
class FeedResult:
    """피드 1개의 수집 결과. 항목과 **그 항목 수가 나온 이유**를 같이 든다."""

    source_name: str
    status: FetchStatus
    items: tuple[RawItem, ...] = ()
    detail: str = ""
    http_status: int | None = None
    entries_in_feed: int = 0  # 파싱된 엔트리 수 (limit 적용 전)
    dropped: int = 0  # link/title 누락·검증 실패로 버린 엔트리 수
    # 엔트리는 건졌지만 XML 이 온전하지 않았다(feedparser bozo). 실패는 아니다.
    # 형식이 흔들리는 피드를 알아보려고 남긴다.
    warning: str = ""
    # 시간대 표기 없이 온 발행일 수. `naive_date_offset` 이 없으면 UTC 로 읽었다는 뜻이다.
    naive_dates: int = 0
    # 피드가 주는 기간(가장 이른 ~ 가장 늦은 발행 시각, 시간). 이보다 오래 못 돌면 그 사이
    # 글을 잃는다 — 수집 실패 경보의 등급이 이것으로 정해진다 (ADR-025 Amendment 1).
    # `published_at` 은 날짜뿐이라 여기서만 시각으로 잰다. 발행 시각이 둘 미만이면 None.
    depth_hours: float | None = None

    @property
    def failed(self) -> bool:
        return self.status not in (FetchStatus.OK, FetchStatus.EMPTY)

    def summary(self) -> dict[str, Any]:
        """실행 요약(JSON)에 남기는 모양."""
        out: dict[str, Any] = {"status": self.status.value, "items": len(self.items)}
        for key in ("detail", "http_status", "warning", "dropped"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.depth_hours is not None:
            out["depth_hours"] = self.depth_hours
        return out


def _failure(name: str, status: FetchStatus, detail: str, **kw: Any) -> FeedResult:
    # **모든 실패는 한 줄을 남긴다** — 반환값에 사유가 생긴 뒤에도 유지한다.
    # stderr 만 보는 사람에게도 같은 정보가 보여야 한다.
    print(f"[fail] {name}: {detail}", file=sys.stderr)
    return FeedResult(source_name=name, status=status, detail=detail, **kw)


def fetch_feed(
    source: dict[str, Any],
    *,
    limit: int | None = None,
    timeout: float | None = None,
) -> FeedResult:
    """피드 1개를 수집하고, 결과와 **그 결과가 나온 이유**를 돌려준다.

    예외 처리 방침:
      - 네트워크/파싱 실패는 여기서 흡수하고 `FeedResult.status` 로 돌려준다.
        피드 1개가 전체 실행을 죽이면 안 된다. **흡수하되 지우지는 않는다** —
        실패를 종료 코드에 반영할지는 호출자가 정한다.
      - 재시도는 넣지 않는다. 어떤 실패가 실제로 잦은지 모르는 상태에서 만든
        재시도 정책은 대개 틀린다. 실패 양상을 관찰한 뒤 붙인다.
      - 개별 엔트리의 검증 실패(ValidationError)는 그 엔트리만 건너뛴다. 다만
        **전부** 버려졌으면 `NO_USABLE_ENTRIES` 다.

    Args:
        timeout: 소켓 연산 1회의 상한(초). None 이면 `FETCH_TIMEOUT_S`.
    """
    name = source.get("name") or source.get("url", "<unnamed>")
    url = source.get("url")
    if not url:
        return _failure(name, FetchStatus.CONFIG_ERROR, "url 이 없습니다")

    timeout = FETCH_TIMEOUT_S if timeout is None else timeout
    try:
        naive_offset = parse_offset(source.get("naive_date_offset"))
    except InvalidOffsetError as exc:
        return _failure(name, FetchStatus.CONFIG_ERROR, str(exc))

    try:
        data, content_type = fetch_feed_bytes(url, timeout=timeout)
    except HTTPError as exc:  # URLError 의 서브클래스라 반드시 먼저 잡는다
        return _failure(name, FetchStatus.HTTP_ERROR, f"HTTP {exc.code}", http_status=exc.code)
    except FeedTooLargeError as exc:
        return _failure(name, FetchStatus.TOO_LARGE, str(exc))
    except FeedDecodeError as exc:
        return _failure(name, FetchStatus.PARSE_ERROR, str(exc))
    except (TimeoutError, URLError) as exc:
        # 타임아웃은 연결 중이면 URLError 에 감싸여 오고, 수신 중이면 그대로 온다.
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            return _failure(name, FetchStatus.TIMEOUT, f"응답 없음 — {timeout:g}초 타임아웃")
        return _failure(
            name, FetchStatus.NETWORK_ERROR, f"피드 요청 실패 ({type(exc).__name__}: {reason})"
        )
    except Exception as exc:
        return _failure(
            name, FetchStatus.NETWORK_ERROR, f"피드 요청 실패 ({type(exc).__name__}: {exc})"
        )

    # Content-Type 이 있을 때만 넘긴다. 빈 문자열을 넘기면 feedparser 가
    # "is not an XML media type" 으로 bozo 를 세워 파싱 경고가 가짜로 뜬다.
    parse_kwargs = {"response_headers": {"content-type": content_type}} if content_type else {}
    try:
        feed = feedparser.parse(data, **parse_kwargs)
    except Exception as exc:  # feedparser 는 대개 삼키지만 방어적으로 잡는다
        return _failure(name, FetchStatus.PARSE_ERROR, f"파싱 실패 ({type(exc).__name__}: {exc})")

    bozo = bool(getattr(feed, "bozo", False))
    if bozo and not feed.entries:
        reason = getattr(feed, "bozo_exception", "unknown")
        return _failure(name, FetchStatus.PARSE_ERROR, f"파싱 실패 ({reason})")
    warning = f"XML 이 온전하지 않음 ({getattr(feed, 'bozo_exception', 'unknown')})" if bozo else ""

    entries = feed.entries[:limit] if limit else feed.entries
    tags = tuple(source.get("tags") or ())
    today = date.today()

    items: list[RawItem] = []
    dropped = 0
    naive_dates = 0
    moments: list[datetime] = []
    for entry in entries:
        link = entry.get("link")
        title = (entry.get("title") or "").strip()
        if not link or not title:
            dropped += 1
            continue
        moment, naive = _entry_moment(entry, naive_offset)
        published_at = moment.date() if moment else None
        naive_dates += naive
        if moment:
            moments.append(moment)
        try:
            items.append(
                RawItem(
                    url=link,
                    title=title,
                    body=_entry_body(entry),
                    source_name=name,
                    published_at=published_at,
                    collected_at=today,
                    tags=tags,
                )
            )
        except ValidationError as exc:
            print(f"[skip] {name}: 엔트리 검증 실패 {link} ({exc.error_count()}건)", file=sys.stderr)
            dropped += 1

    if naive_dates and naive_offset is None:
        # 조용히 틀리는 자리다 — 발행일이 수용 판정 기준이므로 드러낸다 (ADR-023).
        note = f"시간대 없는 발행일 {naive_dates}건을 UTC 로 읽음 (naive_date_offset 미설정)"
        print(f"[warn] {name}: {note}", file=sys.stderr)
        warning = f"{warning}; {note}" if warning else note
    counts: dict[str, Any] = {
        "entries_in_feed": len(feed.entries),
        "dropped": dropped,
        "warning": warning,
        "naive_dates": naive_dates,
        "depth_hours": round((max(moments) - min(moments)).total_seconds() / 3600, 1) if len(moments) >= 2 else None,
    }
    if not items and dropped:
        return _failure(
            name,
            FetchStatus.NO_USABLE_ENTRIES,
            f"엔트리 {dropped}건을 전부 버렸습니다 (link/title 누락 또는 검증 실패)",
            **counts,
        )
    status = FetchStatus.OK if items else FetchStatus.EMPTY
    return FeedResult(source_name=name, status=status, items=tuple(items), **counts)


def fetch_source(
    source: dict[str, Any],
    *,
    limit: int | None = None,
    timeout: float | None = None,
) -> list[RawItem]:
    """피드 1개의 항목만 돌려준다. 실패 사유가 필요 없는 호출자용.

    **실패와 정상 0건이 똑같이 `[]` 로 보인다.** 그 구분이 필요한 곳(파이프라인)은
    `fetch_feed` / `collect_results` 를 쓴다.
    """
    return list(fetch_feed(source, limit=limit, timeout=timeout).items)


def collect_results(
    config: dict[str, Any] | None = None,
    *,
    source_name: str | None = None,
    limit_per_source: int | None = None,
    timeout: float | None = None,
) -> Iterator[FeedResult]:
    """설정의 RSS 소스마다 `FeedResult` 를 하나씩 낸다. 인자는 `collect` 와 같다."""
    config = config if config is not None else load_config()
    for source in rss_sources(config):
        if source_name and source.get("name") != source_name:
            continue
        yield fetch_feed(source, limit=limit_per_source, timeout=timeout)


def collect(
    config: dict[str, Any] | None = None,
    *,
    source_name: str | None = None,
    limit_per_source: int | None = None,
    timeout: float | None = None,
) -> Iterator[RawItem]:
    """설정의 모든 RSS 소스를 순회하며 RawItem 을 흘려보낸다.

    실패 사유는 버려진다 — 필요하면 `collect_results` 를 쓴다.

    Args:
        config: 미리 읽은 설정. None 이면 기본 경로에서 읽는다.
        source_name: 지정하면 해당 이름의 소스만 수집한다.
        limit_per_source: 소스당 최대 건수.
        timeout: 소켓 연산 1회의 상한(초). 소스**마다** 적용된다 — 전체 실행의
            상한이 아니다. None 이면 `FETCH_TIMEOUT_S`.
    """
    for result in collect_results(
        config, source_name=source_name, limit_per_source=limit_per_source, timeout=timeout
    ):
        yield from result.items


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
