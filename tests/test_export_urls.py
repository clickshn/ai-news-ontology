"""`export/urls.py` 의 arXiv 요청 경로 — 타임아웃과 재시도 (session-06 F13).

## 왜 별도 파일인가

F13 은 F2("`feedparser.parse(url)` 에 타임아웃을 걸 자리가 없다")와 **같은 결함**
이지만 같은 수정이 아니다. 여기에는 rate limit 때문에 만든 재시도가 얹혀 있어서,
**전송만 갈아끼우면 그 재시도가 정확히 필요한 순간에 안 돈다** — arXiv 의 스로틀은
본문 없는 4xx 로 오는데 `urlopen` 은 그것을 예외로 올리기 때문이다.

그래서 이 파일이 재는 축은 둘이다.

1. **실제로 끊기는가** — F2 와 같다. 인자 이름이 아니라 동작을 잰다.
2. **끊기게 만든 수정이 재시도를 죽이지 않았는가** — F13 에만 있는 축이고,
   `TestRateLimitPathSurvivesTheTimeoutFix` 가 그것이다.

`TestUrlInjection`(tests/test_export_runner.py)은 입력 계약 쪽을 계속 맡는다.
"""

from __future__ import annotations

import socket
import threading
import time
from urllib.error import HTTPError, URLError

import pytest

from collectors import rss
from export import urls as urls_mod
from export.urls import UnsupportedUrlError, collect_urls

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2412.05449v1</id>
    <title>Attention Is Still All You Need</title>
    <summary>&lt;p&gt;초록 본문&lt;/p&gt;</summary>
    <published>2024-12-06T00:00:00Z</published>
    <link href="http://arxiv.org/abs/2412.05449v1" rel="alternate" type="text/html"/>
  </entry>
</feed>""".encode()

ARXIV_ATOM_TWO = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2605.21404v1</id>
    <title>B</title>
    <summary>초록 B</summary>
    <link href="http://arxiv.org/abs/2605.21404v1" rel="alternate" type="text/html"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2412.05449v1</id>
    <title>A</title>
    <summary>초록 A</summary>
    <link href="http://arxiv.org/abs/2412.05449v1" rel="alternate" type="text/html"/>
  </entry>
</feed>""".encode()

EMPTY_ATOM = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

PAPER = "https://arxiv.org/abs/2412.05449v1"
PAPER_B = "https://arxiv.org/abs/2605.21404v1"


@pytest.fixture
def fake_fetch(monkeypatch):
    """`fetch_feed_bytes` 를 갈아끼우고 호출 인자를 기록한다.

    `export.urls` 쪽 이름만 갈아끼운다 — 수집기의 호출까지 같이 덮으면 이 파일이
    `collectors` 의 동작도 함께 재게 되어 경계가 흐려진다.
    """

    def install(*results):
        calls: list[dict] = []
        queue = list(results)

        def _fetch(url, *, timeout, user_agent=None, accept=None):
            calls.append({"url": url, "timeout": timeout, "user_agent": user_agent})
            result = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(result, BaseException):
                raise result
            return result, "application/atom+xml"

        monkeypatch.setattr(urls_mod, "fetch_feed_bytes", _fetch)
        return calls

    return install


@pytest.fixture
def silent_server():
    """연결은 받지만 **한 바이트도 보내지 않는** 루프백 서버. 바깥으로 안 나간다."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
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
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/api/query"
    finally:
        stop.set()
        server.join(timeout=2)
        for sock in accepted:
            sock.close()
        listener.close()


# ---------------------------------------------------------------------------
# 축 1 — 실제로 끊기는가
# ---------------------------------------------------------------------------
def test_timeout_actually_fires_against_a_silent_server(monkeypatch, silent_server, capsys):
    """목으로는 "우리가 던진 예외를 우리가 잡았다"까지만 증명된다.

    F13 은 소켓이 영원히 대기하던 문제라서, 실제로 대기시켜 보지 않으면 고쳤다고
    말할 수 없다.
    """
    monkeypatch.setattr(urls_mod, "ARXIV_API", silent_server)
    monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", ())

    started = time.monotonic()
    with pytest.raises(UnsupportedUrlError, match="돌려주지 않았습니다"):
        collect_urls([PAPER], timeout=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 10, f"타임아웃이 걸리지 않았다 ({elapsed:.1f}s 경과)"
    # **아래 둘이 없으면 이 테스트는 연결 거부로도 통과한다.** 그러면 재는 것이
    # 타임아웃이 아니라 "실패하면 에러를 낸다"가 되어 F13 을 놓친다.
    assert elapsed >= 0.5, f"기다리지 않고 끝났다 ({elapsed:.2f}s) — 타임아웃 경로가 아니다"
    assert "[arxiv] 포기 — 마지막 사유: 응답 없음 — 0.5초 타임아웃" in capsys.readouterr().err


def test_a_timeout_consumes_a_retry_slot_and_the_total_stays_bounded(
    monkeypatch, silent_server, capsys
):
    """타임아웃도 재시도 슬롯을 **쓴다**. 끊자마자 다시 걸지 않는다.

    끊는 것(소켓 점유 해제)과 다시 거는 시점(rate limit)은 따로 정해지고, 그래서
    총 소요가 `시도 수 × 타임아웃 + 간격 합`으로 **산수로 묶인다.** 별도 예산
    파라미터를 두지 않은 근거가 이 성질이므로 여기서 고정한다 (D-073).
    """
    monkeypatch.setattr(urls_mod, "ARXIV_API", silent_server)
    monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", (0.0, 0.0))  # 간격만 0 으로

    started = time.monotonic()
    with pytest.raises(UnsupportedUrlError):
        collect_urls([PAPER], timeout=0.4)
    elapsed = time.monotonic() - started

    err = capsys.readouterr().err
    # 3회 시도 = 최초 1 + 재시도 2. 타임아웃을 재시도 대상에서 빼면 1회로 끝난다.
    assert err.count("초 뒤 재시도") == 2, err
    assert elapsed >= 3 * 0.4, f"시도가 3회 다 일어나지 않았다 ({elapsed:.2f}s)"
    assert elapsed < 10, f"묶이지 않았다 ({elapsed:.1f}s 경과)"


# ---------------------------------------------------------------------------
# 축 2 — 끊기게 만든 수정이 재시도를 죽이지 않았는가 (F13 고유)
# ---------------------------------------------------------------------------
class TestRateLimitPathSurvivesTheTimeoutFix:
    """arXiv 의 스로틀은 **본문 없는 4xx** 로 온다 (`export/urls.py` 상단).

    `feedparser.parse(url)` 은 그것을 빈 피드로 삼켜 줬고 재시도 루프가 받아냈다.
    `urlopen` 은 예외로 올린다 — 흡수하지 않으면 **rate limit 때문에 만든 재시도가
    rate limit 이 걸리는 순간 정확히 건너뛰어진다.** F2 수정을 복사하면 여기서
    깨진다.

    변이 검사가 두 분기의 몫을 갈라 줬다. 예외 흡수를 통째로 빼면 7건이 깨지고,
    전용 `HTTPError` 분기만 빼면 2건이 깨진다 — 후자는 재시도가 아니라 **로그의
    상태 코드**를 잃는 것이다(`HTTPError` 가 `URLError` 의 서브클래스라 재시도
    자체는 살아남는다). 그래서 `test_the_retry_log_names_the_reason` 이 따로 있다.
    """

    def test_http_error_is_retried_not_raised(self, monkeypatch, fake_fetch, capsys):
        monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", (0.0, 0.0))
        calls = fake_fetch(HTTPError("u", 406, "Not Acceptable", {}, None))

        # HTTPError 가 그대로 새어 나오면 이 기대가 깨진다 — 그게 회귀다.
        with pytest.raises(UnsupportedUrlError, match="rate limit"):
            collect_urls([PAPER])

        assert len(calls) == 3
        assert "[arxiv] 포기 — 마지막 사유: HTTP 406" in capsys.readouterr().err

    def test_http_error_then_success_recovers(self, monkeypatch, fake_fetch):
        monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", (0.0,))
        calls = fake_fetch(HTTPError("u", 406, "Not Acceptable", {}, None), ARXIV_ATOM)

        items = collect_urls([PAPER])

        assert len(calls) == 2
        assert items[0].title == "Attention Is Still All You Need"

    def test_network_failure_is_retried_too(self, monkeypatch, fake_fetch):
        monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", (0.0,))
        calls = fake_fetch(URLError("connection reset"), ARXIV_ATOM)

        assert collect_urls([PAPER])[0].title.startswith("Attention")
        assert len(calls) == 2

    def test_the_retry_log_names_the_reason(self, monkeypatch, fake_fetch, capsys):
        """빈 응답과 타임아웃을 가르는 것이 rate limit 진단의 유일한 단서다."""
        monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", (0.0,))
        fake_fetch(HTTPError("u", 503, "Service Unavailable", {}, None), ARXIV_ATOM)

        collect_urls([PAPER])

        assert "[arxiv] HTTP 503 — 0초 뒤 재시도 (1/1)" in capsys.readouterr().err

    def test_empty_response_still_says_empty(self, monkeypatch, fake_fetch, capsys):
        """종전 동작 — 200 인데 항목이 없는 경우는 그대로 "빈 응답"이다."""
        monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", (0.0,))
        fake_fetch(EMPTY_ATOM, ARXIV_ATOM)

        collect_urls([PAPER])

        assert "[arxiv] 빈 응답 — 0초 뒤 재시도 (1/1)" in capsys.readouterr().err

    def test_empty_response_is_retried_before_giving_up(self, monkeypatch, fake_fetch):
        """arXiv 스로틀링은 이 파이프라인의 상수 조건이다 (MARA session-03~08).

        session-06 까지 `TestUrlInjection` 에 있던 테스트다. F13 수정으로 목을 거는
        층이 `feedparser.parse` 에서 전송으로 내려가면서 여기로 옮겼다.
        """
        monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", (0.0,))
        calls = fake_fetch(EMPTY_ATOM, ARXIV_ATOM)

        items = collect_urls([PAPER])

        assert len(calls) == 2
        assert items[0].title == "Attention Is Still All You Need"

    def test_missing_paper_is_an_error_not_a_silent_drop(self, monkeypatch, fake_fetch):
        """빈 응답을 조용히 흘리지 않는다 — 표본이 줄어든 채로 비용을 낸다."""
        monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", ())
        fake_fetch(EMPTY_ATOM)

        with pytest.raises(UnsupportedUrlError, match="돌려주지 않았습니다"):
            collect_urls([PAPER])


# ---------------------------------------------------------------------------
# 전달과 신원
# ---------------------------------------------------------------------------
def test_timeout_reaches_the_shared_fetch(fake_fetch):
    calls = fake_fetch(ARXIV_ATOM)
    collect_urls([PAPER], timeout=3.5)
    assert calls[0]["timeout"] == 3.5


def test_default_timeout_is_longer_than_the_feed_collectors(fake_fetch):
    """재시도가 비싼 자리라 **기다리는 편이 다시 거는 편보다 싸다.**

    두 값이 같아지면 "F2 값을 복사했다"는 뜻이고, 그게 이 수정이 피하려던 것이다.
    """
    calls = fake_fetch(ARXIV_ATOM)
    collect_urls([PAPER])
    assert calls[0]["timeout"] == urls_mod.ARXIV_FETCH_TIMEOUT_S
    assert urls_mod.ARXIV_FETCH_TIMEOUT_S > rss.FETCH_TIMEOUT_S


def test_request_carries_the_arxiv_user_agent(monkeypatch):
    """신원은 나뉘고 전송은 하나다 — 공유가 UA 까지 공유하면 안 된다.

    arXiv 는 식별 가능한 UA 를 요구하고, 계약 export 는 피드 수집과 다른
    클라이언트다. 목을 `collectors.rss.urlopen` 에 걸어 **실제 요청 헤더**를 본다 —
    `fetch_feed_bytes` 를 목으로 덮으면 UA 가 헤더에 실리는지는 아무도 안 본다.
    """
    seen: list = []

    class _Response:
        headers = {"Content-Type": "application/atom+xml"}

        def read(self, size):
            return ARXIV_ATOM

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        seen.append(request)
        return _Response()

    monkeypatch.setattr(rss, "urlopen", _urlopen)
    collect_urls([PAPER])

    assert seen[0].get_header("User-agent") == urls_mod.ARXIV_USER_AGENT
    assert seen[0].get_header("User-agent") != rss.USER_AGENT


# ---------------------------------------------------------------------------
# 회귀 — bytes 경로가 같은 결과를 내는가
# ---------------------------------------------------------------------------
def test_bytes_path_produces_the_same_raw_item(fake_fetch):
    fake_fetch(ARXIV_ATOM)
    item = collect_urls([PAPER])[0]

    assert str(item.url) == "http://arxiv.org/abs/2412.05449v1"
    assert item.title == "Attention Is Still All You Need"
    assert item.body == "초록 본문"  # strip_html 이 그대로 걸린다
    assert item.source_name == urls_mod.ARXIV_URL_SOURCE_NAME
    assert item.published_at.isoformat() == "2024-12-06"


def test_returns_items_in_input_order(fake_fetch):
    """API 응답 순서에 맡기면 같은 입력이 실행마다 다른 순서로 나온다."""
    fake_fetch(ARXIV_ATOM_TWO)
    assert [i.title for i in collect_urls([PAPER, PAPER_B])] == ["A", "B"]


def test_missing_content_type_does_not_raise_a_false_parse_failure(monkeypatch):
    """헤더 없는 응답도 그대로 파싱된다.

    수집기 쪽 같은 이름의 테스트와 달리 **이 테스트는 약하다.** 이 모듈은 `bozo`
    로 분기하지 않아서, 빈 Content-Type 을 넘기도록 되돌려도 깨지지 않는다.
    가드가 무엇을 막는지가 아니라 **헤더가 없어도 동작한다**는 것만 고정한다.
    """

    def _fetch(url, *, timeout, user_agent=None, accept=None):
        return ARXIV_ATOM, None

    monkeypatch.setattr(urls_mod, "fetch_feed_bytes", _fetch)
    assert collect_urls([PAPER])[0].title == "Attention Is Still All You Need"


def test_oversized_response_is_a_failure_not_a_truncated_parse(monkeypatch, fake_fetch, capsys):
    monkeypatch.setattr(urls_mod, "ARXIV_RETRY_DELAYS_S", ())
    fake_fetch(rss.FeedTooLargeError("응답이 상한을 넘었습니다"))

    with pytest.raises(UnsupportedUrlError):
        collect_urls([PAPER])

    assert "상한을 넘었습니다" in capsys.readouterr().err
