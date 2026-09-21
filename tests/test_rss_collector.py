"""`collectors/rss.py` 의 수집 실패 경로 — 특히 타임아웃 (session-04 F2).

## 왜 이 파일이 생겼나

`fetch_source` 에는 테스트가 **0건**이었다. F2("`feedparser.parse(url)` 에
타임아웃이 없어 응답 없는 피드 하나가 수집 전체를 무기한 붙잡고, 로그도 남지
않는다")가 리뷰에서 눈으로 발견된 이유가 그것이다.

그래서 여기서 검증하는 것은 **"timeout 인자를 넘겼다"가 아니라 "실제로 끊긴다"**
이다. 전자는 인자 이름만 맞으면 통과하고, F2 는 인자 이름이 아니라 동작의 부재였다.
`test_timeout_actually_fires_against_a_silent_server` 가 그 축이고, 나머지는
그때 남는 로그와 주변 분기다.
"""

from __future__ import annotations

import socket
import threading
import time
from urllib.error import HTTPError, URLError

import pytest

from collectors import rss

FEED_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <title>한글 제목</title>
    <link>https://example.com/a</link>
    <description>&lt;p&gt;본문 텍스트&lt;/p&gt;</description>
    <pubDate>Mon, 21 Sep 2026 00:00:00 GMT</pubDate>
  </item>
</channel></rss>""".encode()

SOURCE = {"name": "Test Feed", "url": "https://example.com/feed.xml", "tags": ["vendor"]}


class _FakeResponse:
    """`urlopen` 이 돌려주는 것 중 `_fetch_feed_bytes` 가 쓰는 부분만."""

    def __init__(self, body: bytes, content_type: str | None = "application/rss+xml"):
        self._body = body
        self.headers = {} if content_type is None else {"Content-Type": content_type}

    def read(self, size: int) -> bytes:
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_urlopen(monkeypatch):
    """`urlopen` 을 갈아끼우고 호출 인자를 기록한다."""

    def install(result):
        calls: list[dict] = []

        def _urlopen(request, timeout=None):
            calls.append({"request": request, "timeout": timeout})
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(rss, "urlopen", _urlopen)
        return calls

    return install


# ---------------------------------------------------------------------------
# 타임아웃 — 이 파일의 본론
# ---------------------------------------------------------------------------
def test_timeout_actually_fires_against_a_silent_server(capsys):
    """연결은 받지만 **한 바이트도 보내지 않는** 서버에 붙어도 끊긴다.

    목으로는 "우리가 던진 예외를 우리가 잡았다"까지만 증명된다. F2 는 소켓이
    영원히 대기하던 문제라서, 실제로 대기시켜 보지 않으면 고쳤다고 말할 수 없다.
    루프백으로만 붙고 바깥 네트워크로 나가지 않는다.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    accepted: list[socket.socket] = []
    stop = threading.Event()

    def hold_open():
        # accept 만 하고 응답하지 않는다. 소켓을 살려 둬야 RST 가 안 나간다.
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[0])
            except OSError:  # settimeout 만료 포함
                continue

    server = threading.Thread(target=hold_open, daemon=True)
    server.start()

    started = time.monotonic()
    try:
        items = rss.fetch_source(
            {"name": "Silent Feed", "url": f"http://127.0.0.1:{port}/feed.xml"},
            timeout=0.5,
        )
    finally:
        stop.set()
        server.join(timeout=2)
        for sock in accepted:
            sock.close()
        listener.close()
    elapsed = time.monotonic() - started

    assert items == []
    # 무기한이 아니라는 것이 요점이다. 상한에 여유를 크게 둬서 느린 CI 에서도
    # 흔들리지 않게 하되, "몇 초 안에 끝난다"는 성질 자체는 고정한다.
    assert elapsed < 10, f"타임아웃이 걸리지 않았다 ({elapsed:.1f}s 경과)"
    # **아래 둘이 없으면 이 테스트는 연결 거부로도 통과한다.** 그러면 재는 것이
    # 타임아웃이 아니라 "실패하면 빈 리스트를 준다"가 되어 F2 를 놓친다.
    assert elapsed >= 0.5, f"기다리지 않고 끝났다 ({elapsed:.2f}s) — 타임아웃 경로가 아니다"
    assert "[fail] Silent Feed: 응답 없음 — 0.5초 타임아웃" in capsys.readouterr().err


def test_timeout_during_read_is_logged_as_timeout(fake_urlopen, capsys):
    """수신 중 타임아웃은 `TimeoutError` 로 그대로 올라온다."""
    fake_urlopen(TimeoutError("timed out"))

    assert rss.fetch_source(SOURCE, timeout=3) == []

    err = capsys.readouterr().err
    assert "[fail] Test Feed" in err
    assert "3초 타임아웃" in err


def test_timeout_during_connect_is_logged_as_timeout(fake_urlopen, capsys):
    """연결 중 타임아웃은 `URLError` 에 감싸여 온다. 같은 줄로 보여야 한다."""
    fake_urlopen(URLError(TimeoutError("timed out")))

    assert rss.fetch_source(SOURCE, timeout=15) == []

    err = capsys.readouterr().err
    assert "[fail] Test Feed" in err
    assert "15초 타임아웃" in err


def test_timeout_reaches_urlopen(fake_urlopen):
    """기본값과 명시값 모두 `urlopen` 까지 내려간다."""
    calls = fake_urlopen(_FakeResponse(FEED_XML))

    rss.fetch_source(SOURCE)
    rss.fetch_source(SOURCE, timeout=2.5)

    assert [call["timeout"] for call in calls] == [rss.FETCH_TIMEOUT_S, 2.5]


def test_collect_passes_timeout_to_each_source(fake_urlopen):
    """`collect` 가 소스마다 타임아웃을 흘려보낸다."""
    calls = fake_urlopen(_FakeResponse(FEED_XML))
    config = {"sources": {"rss": [SOURCE, {"name": "B", "url": "https://b.example/f.xml"}]}}

    list(rss.collect(config, timeout=7.0))

    assert [call["timeout"] for call in calls] == [7.0, 7.0]


def test_one_dead_feed_does_not_stop_the_rest(monkeypatch, capsys):
    """F2 의 증상은 "피드 1개가 전체를 붙잡는다"였다. 그 성질을 고정한다."""

    def _urlopen(request, timeout=None):
        if "dead" in request.full_url:
            raise TimeoutError("timed out")
        return _FakeResponse(FEED_XML)

    monkeypatch.setattr(rss, "urlopen", _urlopen)
    config = {
        "sources": {
            "rss": [
                {"name": "Dead", "url": "https://dead.example/feed.xml"},
                {"name": "Alive", "url": "https://alive.example/feed.xml"},
            ]
        }
    }

    items = list(rss.collect(config, timeout=1.0))

    assert [item.source_name for item in items] == ["Alive"]
    assert "[fail] Dead" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# HTTP 상태 — `getattr(feed, "status", None)` 우회를 대체한 경로
# ---------------------------------------------------------------------------
def test_http_error_is_logged_with_its_code(fake_urlopen, capsys):
    """4xx/5xx 는 `HTTPError` 로 올라온다. feedparser 가 상태를 노출해 주기를
    기다리지 않아도 된다."""
    fake_urlopen(HTTPError("https://example.com/feed.xml", 503, "unavailable", {}, None))

    assert rss.fetch_source(SOURCE) == []
    assert "[fail] Test Feed: HTTP 503" in capsys.readouterr().err


def test_generic_url_error_is_not_reported_as_timeout(fake_urlopen, capsys):
    """DNS 실패 같은 것을 타임아웃으로 적으면 진단이 엉뚱한 곳으로 간다."""
    fake_urlopen(URLError("Name or service not known"))

    assert rss.fetch_source(SOURCE) == []

    err = capsys.readouterr().err
    assert "[fail] Test Feed: 피드 요청 실패" in err
    assert "타임아웃" not in err


def test_oversized_response_fails_instead_of_parsing_a_truncated_feed(fake_urlopen, capsys):
    """잘라서 파싱하면 원인이 "파싱 실패"로 잘못 기록된다."""
    fake_urlopen(_FakeResponse(b"<rss>" + b"x" * rss.FEED_MAX_BYTES))

    assert rss.fetch_source(SOURCE) == []

    err = capsys.readouterr().err
    assert "[fail] Test Feed" in err
    assert "상한" in err


# ---------------------------------------------------------------------------
# 정상 경로 — bytes 를 넘겨도 예전과 같은 결과가 나오는가
# ---------------------------------------------------------------------------
def test_bytes_path_produces_the_same_raw_items(fake_urlopen):
    fake_urlopen(_FakeResponse(FEED_XML))

    items = rss.fetch_source(SOURCE)

    assert len(items) == 1
    item = items[0]
    assert item.title == "한글 제목"
    assert item.body == "본문 텍스트"
    assert str(item.url) == "https://example.com/a"
    assert item.source_name == "Test Feed"
    assert item.tags == ("vendor",)
    assert item.published_at.isoformat() == "2026-09-21"


def test_request_carries_our_user_agent(fake_urlopen):
    """urllib 기본 UA 는 일부 CDN 이 403 으로 막는다. feedparser 가 붙여 주던
    자리를 우리가 가져왔으므로 UA 도 같이 가져와야 한다."""
    calls = fake_urlopen(_FakeResponse(FEED_XML))

    rss.fetch_source(SOURCE)

    headers = calls[0]["request"].headers
    assert headers["User-agent"] == rss.USER_AGENT
    assert "Accept" in headers


def test_missing_content_type_does_not_raise_a_false_parse_failure(fake_urlopen, capsys):
    """빈 Content-Type 을 feedparser 에 넘기면 "is not an XML media type" 으로
    bozo 가 서서, 본문 없는 피드가 파싱 실패로 오인된다."""
    empty_feed = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b"<rss version=\"2.0\"><channel><title>T</title></channel></rss>"
    )
    fake_urlopen(_FakeResponse(empty_feed, content_type=None))

    assert rss.fetch_source(SOURCE) == []
    assert "파싱 실패" not in capsys.readouterr().err


def test_limit_still_applies(fake_urlopen):
    two = FEED_XML.replace(
        b"</channel>",
        b"<item><title>B</title><link>https://example.com/b</link></item></channel>",
    )
    fake_urlopen(_FakeResponse(two))

    assert len(rss.fetch_source(SOURCE, limit=1)) == 1
