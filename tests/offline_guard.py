"""소켓 트립와이어 — 테스트가 루프백 밖으로 나가지 않는지 (session-07 §8).

## 왜 `conftest.py` 가 아니라 여기인가

`conftest.py` 는 pytest 가 **`conftest` 라는 이름으로** import 한다. 같은 파일을
테스트가 `tests.conftest` 로 다시 import 하면 **모듈이 둘이 되고 예외 클래스도
둘이 된다** — `pytest.raises(OutboundNetworkAttempt)` 가 자기가 만든 예외를 못 잡는다.
그래서 판정 로직과 예외는 평범한 모듈에 두고, `conftest.py` 는 픽스처 배선만 한다.

## 이 장치가 보장하는 것과 안 하는 것

**잡는 것은 "나갔다"지 "나가지 않는다"가 아니다.** 목을 거는 층이 바뀌면 여전히
조용히 실제 요청을 만들 수 있고, 다만 그때 **즉시 실패로 드러난다.** session-07 에서
그 차이가 2분짜리 실행과 즉시 실패를 갈랐다.
"""

from __future__ import annotations

import ipaddress
import socket

#: 이름으로 허용하는 목적지. IP 는 `ipaddress` 로 판정하므로 여기 넣지 않는다.
#:
#: `""`·`"0.0.0.0"`·`"::"` 는 **연결 목적지가 아니라 bind 와일드카드**다. 서버를
#: 세우는 쪽에서 `getaddrinfo` 를 타므로 막으면 루프백 서버가 안 선다.
_ALLOWED_HOSTNAMES = frozenset({"localhost", "", "0.0.0.0", "::"})


class OutboundNetworkAttempt(RuntimeError):
    """테스트가 루프백 밖으로 연결을 시도했다. 한 건도 없어야 한다."""


def is_loopback(address: object) -> bool:
    """`address` 가 루프백을 가리키는가. **모르는 형태는 루프백이 아니다.**

    판정을 IP 파싱으로 하는 이유는 접두사 비교가 이름에 속기 때문이다 —
    `"127.0.0.1.evil.example"` 은 `"127."` 로 시작하지만 **DNS 로 어디든 갈 수 있다.**
    """
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX 경로 등 — 파일시스템이라 바깥이 아니다
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    try:
        # IPv6 의 scope id (`fe80::1%eth0`) 는 주소의 일부가 아니다.
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return host in _ALLOWED_HOSTNAMES


#: 패치 **전에** 잡아 둔 원본. 픽스처 안에서 읽으면 개별 테스트가 자기 트립와이어를
#: 또 거는 경우(`test_blocked_state_run.py`)에 원본이 아니라 앞 패치를 붙잡는다.
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_GETADDRINFO = socket.getaddrinfo


def install(monkeypatch) -> list[str]:
    """연결·이름 해석을 가로챈다. 돌려주는 목록에 시도된 주소가 쌓인다.

    이름 해석도 막는 이유: **DNS 만 나가고 연결이 안 되는 경우에도 요청은 이미
    바깥으로 나간 것**이고, 여기서 막으면 연결보다 먼저 드러난다.

    루프백을 열어 두는 이유: `test_export_urls.py` · `test_rss_collector.py` 가
    **한 바이트도 안 보내는 루프백 서버**에 실제로 연결해 "정말 끊기는가"를 잰다
    (D-071). 전량 차단하면 그 검사가 "우리가 던진 예외를 우리가 잡았다"로 되돌아간다.

    ⚠️ **`create_connection` 가드는 측정상 남는 것이 없다.** 변이 검사에서 이 한
    줄만 빼면 실패가 **0건**이다 — `create_connection` 은 언제나 `getaddrinfo` 를
    먼저 타서 거기서 이미 걸린다. 그래도 남겨 둔 이유는 둘이 **서로의 백스톱**이기
    때문이다: `getaddrinfo` 가드를 빼도 실패가 1건뿐인 것이 같은 관계의 반대쪽이다.
    살아남은 변이를 없애려고 이 줄만을 위한 계약을 지어내지 않는다 (session-07 §7).
    """
    attempts: list[str] = []

    def trip(address):
        attempts.append(repr(address))
        raise OutboundNetworkAttempt(
            f"테스트가 루프백 밖으로 연결을 시도했습니다: {address!r}. "
            "전송 계층에 목을 걸었는지 확인하세요 (tests/offline_guard.py)."
        )

    def guarded_connect(self, address, *a, **k):
        if not is_loopback(address):
            trip(address)
        return _REAL_CONNECT(self, address, *a, **k)

    def guarded_connect_ex(self, address, *a, **k):
        if not is_loopback(address):
            trip(address)
        return _REAL_CONNECT_EX(self, address, *a, **k)

    def guarded_create_connection(address, *a, **k):
        if not is_loopback(address):
            trip(address)
        return _REAL_CREATE_CONNECTION(address, *a, **k)

    def guarded_getaddrinfo(host, port, *a, **k):
        if not is_loopback((host, port)):
            trip((host, port))
        return _REAL_GETADDRINFO(host, port, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    return attempts
