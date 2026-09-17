"""`doc_id` 생성 — 출력 계약 v1 §4.

**이 모듈이 계약에서 가장 되돌리기 비싼 부분이다.** `doc_id` 는 MARA 골든셋의
`expected_doc_ids` 와 인용 표기에 그대로 박히므로, 규칙이 바뀌면 과거 측정값이
전부 무효가 된다. 규칙 변경은 `contract_version` major 인상 사안이다 (계약 §4, §9).

규칙을 여기에 **복사해 고정**한다. 계약 문서(MARA 레포)를 런타임에 읽지 않는다 —
두 레포는 코드를 공유하지 않고 파일 형식 하나만 공유한다는 것이 ADR-018 의 전제다.

## arXiv 특례가 하는 일

MARA `scripts/ingest_corpus.py` 는 Atom `<id>` 의 마지막 경로 조각으로
`arXiv:<id>` 를 만든다. 여기서도 **같은 방식**(URL 경로의 마지막 조각)으로 만들어
바이트 단위로 같은 키가 나오게 한다. 같은 논문이 두 출처로 들어와도 "탐지해야 할
중복"이 아니라 "같은 키의 같은 문서"가 된다 (계약 §4.3).

⚠️ 현재 두 레포의 arXiv 수집 축이 달라(생산자=최근 피드, MARA=주제 질의) **자연
중복은 발생하지 않는다.** 이 규칙은 겹쳤을 때 조용히 깨지는 것을 막는 예방이고,
그래서 검증하려면 URL 을 직접 주입해야 한다 (ADR-018 Amendment, 계약 §12.1).
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# §4.1-3. 추적 파라미터. 접두사 매칭(`utm_*`)과 완전 일치를 나눈다.
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset({"ref", "ref_src", "fbclid", "gclid"})

# §4.2. 해시 길이. 16자(64비트) — 코퍼스가 10^4 규모여도 충돌 확률이 무시 가능하고
# 인용 표기 `[news:89dccc528c2f0457 / feed_excerpt]` 가 한 줄에 들어간다.
HASH_CHARS = 16

ARXIV_HOST = "arxiv.org"

_DEFAULT_PORTS = {"https": "443", "http": "80"}


def _is_tracking(key: str) -> bool:
    return key in TRACKING_PARAMS or any(key.startswith(p) for p in TRACKING_PREFIXES)


def canonical_url(url: str) -> str:
    """계약 §4.1 의 6단계 정규화. `doc_id` 의 안정성이 곧 이 함수의 안정성이다.

    1. 스킴을 `https` 로 고정
    2. 호스트 소문자화, 기본 포트(`:80`/`:443`) 제거
    3. 추적 파라미터 제거 (`utm_*`, `ref`, `ref_src`, `fbclid`, `gclid`)
    4. 남은 쿼리를 키 기준 사전순 정렬
    5. 프래그먼트 제거
    6. 경로 끝의 `/` 제거 (경로가 `/` 하나뿐이면 유지)

    계약에 없는 정규화는 **하지 않는다.** `www.` 제거나 대소문자 경로 변환을
    여기서 인심 쓰듯 추가하면, 그 순간 MARA 와 규칙이 갈리고 같은 문서가 두 키를
    갖게 된다. 규칙을 늘리려면 계약 문서를 먼저 고친다.
    """
    parts = urlsplit(url.strip())

    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and str(port) != _DEFAULT_PORTS.get(parts.scheme.lower(), ""):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k))
    )

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit(("https", netloc, path, query, ""))


def is_arxiv(url: str) -> bool:
    """정규화된 호스트가 정확히 `arxiv.org` 인가.

    `export.arxiv.org`(API 호스트)는 **여기에 해당하지 않는다.** 항목 링크는 항상
    `arxiv.org/abs/...` 로 오고, API 호스트를 논문 URL 로 쓰지 않는다.
    """
    return urlsplit(canonical_url(url)).hostname == ARXIV_HOST


def arxiv_paper_id(url: str) -> str:
    """arXiv URL 에서 `<paper_id><version>` 을 꺼낸다.

    MARA 와 같은 방식이다 — 경로의 마지막 조각을 그대로 쓴다. 구형 ID
    (`/abs/cs/0701001v1`)에서 아카이브 접두사가 떨어지는 동작까지 같다.
    한쪽만 "더 올바르게" 고치면 키가 갈린다.
    """
    segment = urlsplit(canonical_url(url)).path.rsplit("/", 1)[-1]
    segment = segment.removesuffix(".pdf")
    if not segment:
        raise ValueError(f"arXiv URL 에서 논문 ID 를 찾지 못했습니다: {url!r}")
    return segment


def doc_id_for(url: str) -> str:
    """계약 §4.2. 호스트가 `arxiv.org` 면 arXiv 표기, 그 외는 URL 해시."""
    if is_arxiv(url):
        return f"arXiv:{arxiv_paper_id(url)}"
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()
    return f"news:{digest[:HASH_CHARS]}"


def source_for(url: str) -> str:
    """계약 §3.1 `source`. `doc_id` 와 **같은 판정**에서 나오게 묶어 둔다.

    둘을 따로 정하면 `doc_id` 는 `arXiv:` 인데 `source` 는 `news` 인 레코드가
    만들어질 수 있고, 그러면 MARA 의 arXiv 병합 경로를 타지 않는다.
    """
    return "arxiv" if is_arxiv(url) else "news"


def locator_for(url: str) -> str:
    """계약 §3.1 `locator`. arXiv 는 초록 전문이라 `abstract`, 나머지는 피드 발췌."""
    return "abstract" if is_arxiv(url) else "feed_excerpt"
