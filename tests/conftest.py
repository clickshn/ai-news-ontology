"""테스트 공용 픽스처 — 입력 공장과 **오프라인 보장**.

여기 있는 공장은 **보존소 페이로드를 손으로 만드는 것**이다. 실제 추출
결과(`data/extractions/`)를 테스트가 읽게 하면 테스트가 특정 실행 산출물에
묶이고, 그 산출물이 지워지는 순간 조용히 깨진다.

`no_outbound_network` 는 **전체 테스트에 무조건 걸리는** 소켓 트립와이어다
(autouse). 판정 로직은 `tests/offline_guard.py` 에 있다 — 예외 클래스를 테스트가
import 해야 해서 `conftest.py` 에 두면 모듈이 둘로 갈린다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from export.store import STORE_SCHEMA, ExtractionStore
from tests import offline_guard


def _ontology(**overrides: Any) -> dict[str, Any]:
    ontology = {
        "summary": "테스트용 요약이다. 두 문장으로 쓴다.",
        "tech_domains": ["Agent"],
        "release_type": "Paper",
        "companies": [],
        "prior_art": ["LLM Agent"],
        "impact": {"score": 3, "rationale": "테스트를 위한 근거 문장이다."},
    }
    ontology.update(overrides)
    return ontology


@pytest.fixture
def make_payload() -> Callable[..., dict[str, Any]]:
    """보존소에 넣을 payload 1건을 만든다."""

    def factory(
        *,
        doc_id: str,
        url: str,
        title: str = "제목",
        body: str = "본문 텍스트.",
        source_name: str = "GeekNews",
        tags: tuple[str, ...] = ("ko",),
        published_at: str | None = "2026-08-29",
        collected_at: str = "2026-08-29",
        gate: dict[str, Any] | None = None,
        prompt_name: str = "extract_ontology.v4.md",
        prompt_sha256: str = "0" * 64,
        model: str = "claude-opus-5",
        extracted_at: str = "2026-09-17T14:03:11+09:00",
        ontology: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": STORE_SCHEMA,
            "doc_id": doc_id,
            "stored_at": "2026-09-17T14:03:11+09:00",
            "raw_item": {
                "url": url,
                "title": title,
                "body": body,
                "source_name": source_name,
                "published_at": published_at,
                "collected_at": collected_at,
                "tags": list(tags),
            },
            "gate": gate,
            "extraction": {
                "prompt_name": prompt_name,
                "prompt_sha256": prompt_sha256,
                "model": model,
                "attempts": 1,
                "extracted_at": extracted_at,
                "ontology": ontology if ontology is not None else _ontology(),
                "usage": {"input_tokens": 100, "output_tokens": 200},
            },
        }

    return factory


@pytest.fixture
def make_store(tmp_path) -> Callable[..., ExtractionStore]:
    """`tmp_path` 안에만 쓰는 보존소. 실제 `data/extractions/` 를 건드리지 않는다."""

    def factory(payloads: list[dict[str, Any]] | None = None) -> ExtractionStore:
        store = ExtractionStore(tmp_path / "extractions")
        for payload in payloads or []:
            store.save(payload)
        return store

    return factory


@pytest.fixture
def make_mara_root(tmp_path) -> Callable[..., Any]:
    """MARA 레포를 흉내 낸 **읽기 전용 입력**을 tmp_path 에 만든다.

    실제 MARA 레포를 테스트가 읽게 하면 그쪽 파일이 바뀔 때 이 레포의 테스트가
    깨진다. 두 레포가 코드를 공유하지 않는다는 ADR-018 의 전제와도 어긋난다.
    """

    def factory(documents: list[dict[str, Any]], expected_doc_ids: list[str]) -> Any:
        root = tmp_path / "mara"
        corpus = root / "data" / "corpus" / "arxiv"
        corpus.mkdir(parents=True)
        (corpus / "snapshot.json").write_text(
            json.dumps({"source": "arxiv", "documents": documents}, ensure_ascii=False),
            encoding="utf-8",
        )
        eval_dir = root / "docs" / "eval"
        eval_dir.mkdir(parents=True)
        (eval_dir / "golden-set.json").write_text(
            json.dumps(
                {
                    "cases": [
                        {"id": "GS-001", "expect": {"expected_doc_ids": expected_doc_ids}}
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return root

    return factory


# ---------------------------------------------------------------------------
# 오프라인 보장 — 테스트는 바깥으로 나가지 않는다
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch) -> list[str]:
    """**모든** 테스트에 걸리는 소켓 트립와이어. 판정은 `tests/offline_guard.py`.

    session-07 에서 기존 테스트 3건이 전송 목을 잃고 **진짜 arXiv 로 나갔다.**
    그때도 트립와이어는 있었지만 `test_blocked_state_run.py` 의 픽스처 **안에만**
    있었다 — 벤더 호출 지점을 세는 검사에는 걸려 있고 나머지 499건에는 없었다.
    그래서 여기(autouse)로 올린다. 개별 테스트가 요청하지 않아도 걸린다.
    """
    return offline_guard.install(monkeypatch)
