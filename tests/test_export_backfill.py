"""MARA 코퍼스 backfill — 대상 고정, 게이트 생략, 운영 경로 분리, 37건 선택 export (ADR-026 · ADR-027).

실제 API 도, 실제 MARA 레포도 읽지 않는다. MARA 는 tmp_path 에 흉내 내고, 대상 건수
상수는 그 크기에 맞춰 바꾼다. 커밋된 37건 목록 파일만은 실물을 읽는다 — 그 파일이
곧 ADR-026 의 범위다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from export import backfill
from export.backfill import BackfillError
from export.conformance import REQUIRED_TOP_LEVEL
from export.contract import CONTRACT_VERSION
from export.runner import prompt_sha256
from export.store import DEFAULT_STORE_DIR, ExtractionStore
from extraction.llm import SchemaMismatchError, StructuredResult, Usage
from extraction.schema import NewsOntology, RelevanceGate
from observability import events

DAYTIME = datetime(2026, 10, 2, 14, 0, tzinfo=backfill.KST)

SNAP_ONLY = "arXiv:2401.07324v3"
BOTH = "arXiv:2412.05449v1"


# ---------------------------------------------------------------------------
# 흉내 낸 MARA
# ---------------------------------------------------------------------------
def _snapshot_doc(doc_id: str, text: str) -> dict:
    arxiv_id = doc_id.split(":", 1)[1]
    return {
        "doc_id": doc_id,
        "title": f"제목 {arxiv_id}",
        "text": text,
        "locator": "abstract",
        "url": f"http://arxiv.org/abs/{arxiv_id}",
        "published": "2024-01-14",
        "authors": "A, B",
    }


def _record(doc_id: str, url: str, *, source: str, text: str, indexable: bool = True, lang: str = "en",
            source_name: str = "arXiv (MARA snapshot seed)") -> dict:
    return {
        "contract_version": "1.0",
        "doc_id": doc_id,
        "source": source,
        "source_name": source_name,
        "url": url,
        "title": f"레코드 {doc_id}",
        "lang": lang,
        "text": text,
        "text_origin": "source_feed",
        "text_chars": len(text),
        "text_truncated": False,
        "locator": "abstract" if source == "arxiv" else "feed_excerpt",
        "published_at": "2026-09-17",
        "collected_at": "2026-09-17",
        "indexable": indexable,
        "ontology": {},
        "provenance": {
            "extraction_model": "claude-opus-5",
            "prompt_version": "extract_ontology.v4.md",
            "prompt_sha256": "9ff5a9d3" + "0" * 56,
            "extracted_at": "2026-09-17T11:59:09+09:00",
            "gate_model": "claude-haiku-4-5-20251001",
            "gate_prompt_version": "relevance_gate.v1.md",
            "vocab_version": "config=1;schema_sha256=x",
        },
    }


@pytest.fixture
def mara(tmp_path) -> Path:
    """스냅샷 2건(그중 1건은 export 에도 있음) + export 3건(색인 가능 2, 불가 1) = 대상 3건."""
    from export.doc_id import doc_id_for

    root = tmp_path / "mara"
    arxiv = root / "data" / "corpus" / "arxiv"
    news = root / "data" / "corpus" / "news"
    arxiv.mkdir(parents=True)
    news.mkdir(parents=True)
    snap = {"source": "arxiv", "query": "q", "license_note": "", "documents": [
        _snapshot_doc(SNAP_ONLY, "스냅샷에만 있는 초록이다."),
        _snapshot_doc(BOTH, "양쪽에 있는 초록이다."),
    ]}
    (arxiv / "agent-evaluation.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

    news_url = "https://news.hada.io/topic?id=1"
    empty_url = "https://news.hada.io/topic?id=2"
    arxiv_records = [_record(BOTH, "https://arxiv.org/abs/2412.05449v1", source="arxiv", text="양쪽에 있는 초록이다.")]
    news_records = [
        _record(doc_id_for(news_url), news_url, source="news", text="뉴스 발췌다.", lang="ko", source_name="GeekNews"),
        _record(doc_id_for(empty_url), empty_url, source="news", text="", indexable=False, source_name="GeekNews"),
    ]
    for path, rows in ((arxiv / "ontology-x.jsonl", arxiv_records), (news / "ontology-x.jsonl", news_records)):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return root


@pytest.fixture
def small(monkeypatch, mara) -> dict:
    """대상 건수 상수를 흉내 낸 MARA 크기(3)로 맞추고 목록 파일을 tmp 에 쓴다."""
    monkeypatch.setattr(backfill, "EXPECTED_TARGETS", 3)
    targets = backfill.derive_targets(mara)
    path = mara.parent / "targets.json"
    path.write_text(json.dumps(targets, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(backfill, "TARGETS_PATH", path)
    return targets


class FakeClient:
    """추출만 받는다. 게이트 스키마로 불리면 그 자체가 실패다."""

    def __init__(self, *, model: str = "gemma-4-31B-it", fail_on: str | None = None, companies=()):
        self.model = model
        self.fail_on = fail_on
        self.companies = list(companies)
        self.calls: list[str] = []

    def parse_into(self, *, system: str, user: str, output_model):
        self.calls.append(output_model.__name__)
        assert output_model is not RelevanceGate, "backfill 은 게이트를 부르지 않는다"
        if self.fail_on and self.fail_on in user:
            raise SchemaMismatchError("실패", attempts=2, last_error="형식 오류")
        value = NewsOntology(
            요약="테스트용 요약 문장이다. 두 번째 문장도 있다.",
            기술영역=["Agent"],
            발표유형="Paper",
            관련기업=self.companies,
            관련기존기술=["LLM Agent"],
            영향도={"점수": 3, "근거": "테스트를 위한 근거 문장이다."},
        )
        return StructuredResult(value=value, usage=Usage(model=self.model, input_tokens=10, output_tokens=5), attempts=1)


def _run(small, mara, store, client, *, ids=None, max_calls=None, now=DAYTIME):
    ids = ids or backfill.target_ids(small)
    inputs = backfill.build_inputs(mara, ids)
    return backfill.run_backfill(
        inputs,
        targets=small,
        store=store,
        client=client,
        max_calls=max_calls if max_calls is not None else 2 * len(inputs),
        endpoint_hash="e" * 64,
        now=lambda: now,
    )


# ---------------------------------------------------------------------------
# 1. 대상 목록
# ---------------------------------------------------------------------------
def test_committed_target_list_is_the_adr026_range():
    targets = backfill.load_targets()
    ids = backfill.target_ids(targets)
    assert len(ids) == len(set(ids)) == backfill.EXPECTED_TARGETS == 37
    assert targets["counts"] == {
        "total": 37, "snapshot": 16, "export_indexable": 23, "both": 2,
        "gate_history": 23, "gate_null": 14,
    }
    # 감사 표(§11)의 14건 = 게이트 null 인 스냅샷 전용
    snapshot_only = [e["doc_id"] for e in targets["doc_ids"] if e["origin"] == "snapshot"]
    assert len(snapshot_only) == 14 and SNAP_ONLY in snapshot_only


def test_derive_targets_is_snapshot_union_indexable(small):
    assert small["counts"] == {
        "total": 3, "snapshot": 2, "export_indexable": 2, "both": 1, "gate_history": 2, "gate_null": 1,
    }
    origins = {e["doc_id"]: e["origin"] for e in small["doc_ids"]}
    assert origins[SNAP_ONLY] == "snapshot" and origins[BOTH] == "snapshot+export"


def test_derive_targets_fails_when_not_the_expected_size(monkeypatch, mara):
    monkeypatch.setattr(backfill, "EXPECTED_TARGETS", 37)
    with pytest.raises(BackfillError, match="3건"):
        backfill.derive_targets(mara)


def test_check_targets_catches_a_changed_mara(small, mara):
    path = mara / "data" / "corpus" / "arxiv" / "agent-evaluation.json"
    path.write_text(path.read_text(encoding="utf-8").replace("스냅샷에만", "바뀐"), encoding="utf-8")
    with pytest.raises(BackfillError, match="agent-evaluation.json"):
        backfill.check_targets(mara)


# ---------------------------------------------------------------------------
# 2. 입력 구성
# ---------------------------------------------------------------------------
def test_inputs_carry_gate_history_only_where_it_existed(small, mara):
    inputs = {e.doc_id: e for e in backfill.build_inputs(mara, backfill.target_ids(small))}
    assert inputs[SNAP_ONLY].gate is None
    for doc_id in (BOTH, *[d for d in inputs if d.startswith("news:")]):
        gate = inputs[doc_id].gate
        assert gate["model"] == "claude-haiku-4-5-20251001"
        assert gate["prompt_name"] == "relevance_gate.v1.md"
        assert "is_relevant" in gate["not_carried"]
    news = next(e for d, e in inputs.items() if d.startswith("news:"))
    assert news.item.source_name == "GeekNews" and news.item.tags == ("ko",)
    assert str(news.item.collected_at) == "2026-09-17"


def test_inputs_refuse_a_merged_doc_whose_text_differs(small, mara):
    path = mara / "data" / "corpus" / "arxiv" / "ontology-x.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("양쪽에 있는", "다른"), encoding="utf-8")
    with pytest.raises(BackfillError, match="스냅샷 text"):
        backfill.build_inputs(mara, [BOTH])


# ---------------------------------------------------------------------------
# 3. 실행 — 게이트 0, 이력 보존, 매니페스트
# ---------------------------------------------------------------------------
def test_run_extracts_only_and_writes_the_manifest(small, mara, tmp_path):
    store = ExtractionStore(tmp_path / "bf" / "extractions")
    client = FakeClient()
    manifest = _run(small, mara, store, client)

    assert client.calls == ["NewsOntology"] * 3
    assert manifest["status"] == "done" and manifest["calls_used"] == 3
    assert manifest["run_kind"] == "backfill"
    assert manifest["gate"]["called"] is False and "ADR-026" in manifest["gate"]["reason"]
    assert manifest["gate"]["history_null"] == [SNAP_ONLY]
    assert manifest["targets"]["doc_ids"] == backfill.target_ids(small)
    assert manifest["extraction"]["model"] == "gemma-4-31B-it"
    assert manifest["extraction"]["prompt_sha256"] == prompt_sha256("extract_ontology.v4.md")
    assert manifest["contract_version"] == CONTRACT_VERSION == "1.0"

    on_disk = json.loads((backfill.runs_dir(store) / f"{manifest['run_id']}.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
    # 매니페스트는 보존소 목록에 섞이지 않는다
    assert sorted(store.doc_ids()) == sorted(backfill.target_ids(small))

    snap = store.load(SNAP_ONLY).payload
    assert snap["gate"] is None and snap["run_kind"] == "backfill"
    assert store.load(BOTH).payload["gate"]["model"] == "claude-haiku-4-5-20251001"


def test_call_cap_is_checked_before_any_call(small, mara, tmp_path):
    client = FakeClient()
    with pytest.raises(BackfillError, match="상한"):
        _run(small, mara, ExtractionStore(tmp_path / "bf"), client, max_calls=5)
    assert client.calls == []


def test_quiet_window_blocks_the_start(small, mara, tmp_path):
    client = FakeClient()
    with pytest.raises(BackfillError, match="07:10"):
        _run(small, mara, ExtractionStore(tmp_path / "bf"), client,
             now=datetime(2026, 10, 2, 8, 30, tzinfo=backfill.KST))
    assert client.calls == []


def test_operational_store_is_refused(small, mara):
    client = FakeClient()
    for directory in (DEFAULT_STORE_DIR, DEFAULT_STORE_DIR / "sub"):
        with pytest.raises(BackfillError, match="운영 보존소"):
            _run(small, mara, ExtractionStore(directory), client)
    assert client.calls == []


def test_already_stored_docs_are_not_called_again(small, mara, tmp_path):
    store = ExtractionStore(tmp_path / "bf")
    _run(small, mara, store, FakeClient(), ids=[SNAP_ONLY])
    client = FakeClient()
    with pytest.raises(BackfillError, match="이미 보존"):
        _run(small, mara, store, client, ids=[SNAP_ONLY])
    assert client.calls == []


def test_schema_failure_is_counted_and_not_stored(small, mara, tmp_path):
    store = ExtractionStore(tmp_path / "bf")
    manifest = _run(small, mara, store, FakeClient(fail_on="스냅샷에만"))
    assert manifest["calls_used"] == 2 + 1 + 1
    row = next(r for r in manifest["results"] if r["doc_id"] == SNAP_ONLY)
    assert row["status"] == "schema_fail" and SNAP_ONLY not in store.doc_ids()


def test_run_never_touches_the_operational_queues(small, mara, tmp_path, monkeypatch):
    """미등록 기업이 나와도 운영 관측 큐(JSONL)에 아무것도 가지 않는다."""

    def forbidden(*args, **kwargs):
        raise AssertionError("backfill 이 운영 관측 큐를 열었다")

    monkeypatch.setattr(events.JSONLObserver, "__init__", forbidden)
    monkeypatch.setattr(events, "observer_from_config", forbidden)
    client = FakeClient(companies=[{"원문표기": "Unknown Startup Zeta", "역할": "개발"}])
    manifest = _run(small, mara, ExtractionStore(tmp_path / "bf"), client)
    assert manifest["status"] == "done"
    assert "기록 안 함" in manifest["observability"]


# ---------------------------------------------------------------------------
# 4. 선택 export
# ---------------------------------------------------------------------------
def test_export_writes_exactly_the_targets_with_contract_1_0(small, mara, tmp_path):
    store = ExtractionStore(tmp_path / "bf" / "extractions")
    _run(small, mara, store, FakeClient())
    result = backfill.export_backfill(store, small, out_dir=tmp_path / "out", config_version=1)

    assert sorted(r["doc_id"] for r in result.records) == sorted(backfill.target_ids(small))
    assert result.manifest["counts"]["total"] == 3
    by_id = {r["doc_id"]: r for r in result.records}
    for record in result.records:
        assert set(record) == set(REQUIRED_TOP_LEVEL)  # 게이트 생략 필드 같은 새 키가 없다
        assert record["contract_version"] == "1.0"
        assert record["provenance"]["extraction_model"] == "gemma-4-31B-it"
        assert record["provenance"]["prompt_sha256"] == prompt_sha256("extract_ontology.v4.md")
    assert by_id[SNAP_ONLY]["provenance"]["gate_model"] is None
    assert by_id[SNAP_ONLY]["provenance"]["gate_prompt_version"] is None
    assert by_id[BOTH]["provenance"]["gate_model"] == "claude-haiku-4-5-20251001"
    news = next(r for r in result.records if r["source"] == "news")
    assert news["lang"] == "ko" and news["source_name"] == "GeekNews"


def test_export_refuses_a_partial_store(small, mara, tmp_path):
    store = ExtractionStore(tmp_path / "bf")
    _run(small, mara, store, FakeClient(), ids=[SNAP_ONLY])
    with pytest.raises(BackfillError, match="빠진 것"):
        backfill.export_backfill(store, small, out_dir=tmp_path / "out", config_version=1)
    assert not (tmp_path / "out").exists()


def test_export_refuses_entries_outside_the_list_or_not_backfill(small, mara, tmp_path, make_payload):
    store = ExtractionStore(tmp_path / "bf")
    _run(small, mara, store, FakeClient())
    store.save(make_payload(doc_id="news:ffff", url="https://news.hada.io/topic?id=9"))
    with pytest.raises(BackfillError, match="목록 밖") as excinfo:
        backfill.export_backfill(store, small, out_dir=tmp_path / "out", config_version=1)
    assert "backfill 표시 없음" in str(excinfo.value)


def test_export_refuses_mixed_models(small, mara, tmp_path):
    store = ExtractionStore(tmp_path / "bf")
    ids = backfill.target_ids(small)
    _run(small, mara, store, FakeClient(), ids=ids[:2])
    _run(small, mara, store, FakeClient(model="other-model"), ids=ids[2:])
    with pytest.raises(BackfillError, match="모델이 하나가 아닙니다"):
        backfill.export_backfill(store, small, out_dir=tmp_path / "out", config_version=1)


def test_self_check_passes_on_the_backfill_export(small, mara, tmp_path):
    store = ExtractionStore(tmp_path / "bf")
    _run(small, mara, store, FakeClient())
    (mara / "docs" / "eval").mkdir(parents=True)
    (mara / "docs" / "eval" / "golden-set.json").write_text(
        json.dumps({"cases": [{"id": "GS-1", "expect": {"expected_doc_ids": [SNAP_ONLY]}}]}), encoding="utf-8"
    )
    out = tmp_path / "out"
    result = backfill.export_backfill(store, small, out_dir=out, config_version=1)
    checks = backfill.self_check(result, store=store, mara_root=mara, out_dir=out, config_version=1)
    failed = [(c.number, c.detail) for c in checks if c.status == "FAIL"]
    assert failed == []


# ---------------------------------------------------------------------------
# 5. prompt_sha256 = LF 정규화 (ADR-027)
# ---------------------------------------------------------------------------
def test_prompt_sha256_ignores_line_endings(tmp_path):
    (tmp_path / "lf.md").write_bytes(b"# System\nA\n# User\nB\n")
    (tmp_path / "crlf.md").write_bytes(b"# System\r\nA\r\n# User\r\nB\r\n")
    assert prompt_sha256("lf.md", prompt_dir=tmp_path) == prompt_sha256("crlf.md", prompt_dir=tmp_path)


def test_prompt_sha256_v4_is_the_adr027_value():
    assert prompt_sha256("extract_ontology.v4.md") == (
        "25b17f445bec91a2fa4336b20dbac214b09c87fcb1c8493c17fcdd5b65e57bc8"
    )
