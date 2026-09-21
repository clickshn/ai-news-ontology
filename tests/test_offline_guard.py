"""오프라인 보장 — 소켓 트립와이어 자체를 잰다 (session-07 §8).

## 왜 이 파일이 따로 필요한가

전체 실행이 **초록불이라는 사실은 트립와이어가 돈다는 증거가 아니다.** 트립와이어가
아예 안 걸려 있어도 초록불은 똑같이 나온다 — 나가는 테스트가 없으면 구분이 안 된다.
session-07 에서 실제로 나간 3건이 그 상태에서 조용히 지나갔다.

그래서 여기서 재는 것은 **장치가 반응하는가**다.

1. 루프백 밖으로 향하면 **막힌다** (연결 성공/실패와 무관하게).
2. 루프백은 **막지 않는다** — 타임아웃 검사가 실소켓을 쓴다 (D-071).
3. 이름 해석도 막는다 — DNS 만 나가고 연결이 안 되는 경우도 나간 것이다.
"""

from __future__ import annotations

import socket

import pytest

from tests.offline_guard import OutboundNetworkAttempt, is_loopback


class TestTheTripwireFires:
    """**모든** 테스트에 걸려 있다 — 이 파일은 픽스처를 따로 요청하지 않는다."""

    def test_connect_to_the_outside_is_blocked(self):
        """도달 불가능한 주소를 골랐다. 막히는 이유가 '연결 실패'면 안 된다."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OutboundNetworkAttempt, match="루프백 밖"):
                sock.connect(("192.0.2.1", 80))  # TEST-NET-1 (RFC 5737)
        finally:
            sock.close()

    def test_create_connection_is_blocked(self):
        with pytest.raises(OutboundNetworkAttempt):
            socket.create_connection(("192.0.2.1", 80), timeout=0.1)

    def test_name_resolution_is_blocked(self):
        """DNS 만 나가도 요청은 이미 바깥으로 나간 것이다."""
        with pytest.raises(OutboundNetworkAttempt):
            socket.getaddrinfo("example.com", 443)

    def test_urlopen_is_blocked_before_it_reaches_the_wire(self):
        """실제 호출부가 쓰는 경로. 여기가 안 막히면 앞의 셋은 의미가 없다."""
        from urllib.request import urlopen

        with pytest.raises(OutboundNetworkAttempt):
            urlopen("https://export.arxiv.org/api/query", timeout=0.1)


class TestLoopbackStaysOpen:
    """전량 차단으로 만들면 타임아웃 검사가 목으로 되돌아간다."""

    def test_a_real_loopback_connection_still_works(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(2)
            client.connect(listener.getsockname())  # 막히면 여기서 예외가 난다
            listener.accept()[0].close()
        finally:
            client.close()
            listener.close()

    def test_loopback_name_resolution_still_works(self):
        assert socket.getaddrinfo("127.0.0.1", 0)


class TestWhatCountsAsLoopback:
    """**모르는 형태는 루프백이 아니다** — 판정이 애매하면 막는 쪽으로 넘어간다."""

    @pytest.mark.parametrize(
        "address",
        [
            ("127.0.0.1", 80),
            ("127.0.0.53", 80),  # 127/8 전체
            ("::1", 80, 0, 0),
            ("::ffff:127.0.0.1", 80),
            ("localhost", 80),
            ("", 0),  # bind 용 와일드카드
        ],
    )
    def test_loopback_forms(self, address):
        assert is_loopback(address)

    @pytest.mark.parametrize(
        "address",
        [
            ("192.0.2.1", 80),
            ("export.arxiv.org", 443),
            ("127.0.0.1.evil.example", 443),  # 접두사만 흉내 낸 이름
            ("1270.0.0.1", 80),
            (None, 80),
            (),
            42,
        ],
    )
    def test_not_loopback_forms(self, address):
        assert not is_loopback(address)
