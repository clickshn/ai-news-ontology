"""export 실행 경로 — `--urls` 주입, 게이트 예산, 보존소 (계약 §12.1, §12.3).

실제 API 는 부르지 않는다. 호출 **여부**와 횟수가 이 파일의 검증 대상이다 —
게이트가 정밀 추출을 막는 구조가 비용 절감의 전부이므로(D-015), 막지 않는
회귀가 생기면 조용히 돈이 샌다.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from collectors.base import RawItem
from export.runner import (
    parse_take,
    prompt_sha256,
    read_urls,
    run_collect,
)
from export.store import ExtractionStore
from export.urls import UnsupportedUrlError, collect_urls
from extraction.llm import StructuredResult, Usage
from extraction.schema import NewsOntology, RelevanceGate


class FakeClient:
    """호출을 세는 LLM 목. 게이트 판정은 주입받는다."""

    def __init__(self, *, model: str, relevant: dict[str, bool] | None = None):
        self.model = model
        self.relevant = relevant or {}
        self.calls: list[str] = []

    def parse_into(self, *, system: str, user: str, output_model):
        self.calls.append(output_model.__name__)
        if output_model is RelevanceGate:
            # 프롬프트 본문에 제목이 어떤 모양으로 실리든 판정을 지정할 수 있게
            # 부분 문자열로 찾는다. 템플릿 형식에 테스트를 묶지 않기 위해서다.
            is_relevant = next((v for k, v in self.relevant.items() if k in user), True)
            value = RelevanceGate(
                관련있음=is_relevant, 근거="테스트 판정 근거 문장이다."
            )
        else:
            value = NewsOntology(
                요약="테스트용 요약 문장이다. 두 번째 문장도 있다.",
                기술영역=["Agent"],
                발표유형="Paper",
                관련기업=[],
                관련기존기술=["LLM Agent"],
                영향도={"점수": 3, "근거": "테스트를 위한 근거 문장이다."},
            )
        return StructuredResult(value=value, usage=Usage(model=self.model), attempts=1)


def _item(url: str, title: str, *, source_name: str = "GeekNews", body: str = "본문") -> RawItem:
    return RawItem(
        url=url,
        title=title,
        body=body,
        source_name=source_name,
        published_at=date(2026, 8, 29),
        collected_at=date(2026, 8, 29),
        tags=("ko",),
    )


@pytest.fixture
def patched(monkeypatch):
    """피드 수집과 LLM 클라이언트를 갈아 끼운다."""
    state: dict[str, object] = {}

    def install(items: list[RawItem], *, injected: list[RawItem] | None = None, relevant=None):
        gate = FakeClient(model="claude-haiku-4-5-20251001", relevant=relevant)
        extraction = FakeClient(model="claude-opus-5")
        state["gate"] = gate
        state["extraction"] = extraction

        monkeypatch.setattr("export.runner.collect_feeds", lambda *a, **k: list(items))
        monkeypatch.setattr("export.runner.collect_urls", lambda urls, **k: list(injected or []))
        # 생성 지점이 `client_from_config` 하나로 모였다 (ADR-018). 프로바이더가
        # 바뀌어도 이 패치 대상은 그대로다 — 그게 팩토리를 둔 이유다.
        monkeypatch.setattr(
            "export.runner.client_from_config",
            lambda config, stage="extraction", **kw: gate
            if stage == "relevance_gate"
            else extraction,
        )
        return gate, extraction

    state["install"] = install
    return state


class TestArgumentParsing:
    def test_take_is_source_name_equals_count(self):
        assert parse_take(["GeekNews=10", "arXiv cs.CL (Atom API)=8"]) == {
            "GeekNews": 10,
            "arXiv cs.CL (Atom API)": 8,
        }

    def test_take_rejects_malformed_values(self):
        with pytest.raises(ValueError):
            parse_take(["GeekNews"])

    def test_urls_file_lines_are_read_and_comments_skipped(self, tmp_path):
        path = tmp_path / "urls.txt"
        path.write_text(
            "# 중복 케이스\nhttps://arxiv.org/abs/2412.05449v1\n\nhttps://arxiv.org/abs/2605.21404v1\n",
            encoding="utf-8",
        )
        assert read_urls([], str(path)) == [
            "https://arxiv.org/abs/2412.05449v1",
            "https://arxiv.org/abs/2605.21404v1",
        ]


class TestPromptHash:
    def test_hashes_the_file_bytes_not_the_parsed_prompt(self):
        import hashlib

        from extraction.extractor import PROMPT_DIR

        expected = hashlib.sha256((PROMPT_DIR / "extract_ontology.v4.md").read_bytes()).hexdigest()
        assert prompt_sha256("extract_ontology.v4.md") == expected


class TestUrlInjection:
    def test_non_arxiv_urls_are_refused(self):
        """임의 기사 페이지 fetch 는 D-013 이고 ADR-004 재검토 사안이다 (계약 §10)."""
        with pytest.raises(UnsupportedUrlError, match="arXiv 이외"):
            collect_urls(["https://news.hada.io/topic?id=1"])

    def test_missing_paper_is_an_error_not_a_silent_drop(self, monkeypatch):
        """빈 응답을 조용히 흘리지 않는다 — 표본이 줄어든 채로 비용을 낸다."""
        monkeypatch.setattr("export.urls.ARXIV_RETRY_DELAYS_S", ())
        monkeypatch.setattr(
            "export.urls.feedparser.parse", lambda url, **kw: type("F", (), {"entries": []})()
        )
        with pytest.raises(UnsupportedUrlError, match="돌려주지 않았습니다"):
            collect_urls(["https://arxiv.org/abs/2412.05449v1"])

    def test_empty_response_is_retried_before_giving_up(self, monkeypatch):
        """arXiv 스로틀링은 이 파이프라인의 상수 조건이다 (MARA session-03~08)."""
        calls = {"n": 0}

        def flaky(url, **kw):
            calls["n"] += 1
            entries = (
                [{"link": "http://arxiv.org/abs/2412.05449v1", "title": "A", "summary": "초록"}]
                if calls["n"] > 1
                else []
            )
            return type("F", (), {"entries": entries})()

        monkeypatch.setattr("export.urls.ARXIV_RETRY_DELAYS_S", (0.0,))
        monkeypatch.setattr("export.urls.feedparser.parse", flaky)
        items = collect_urls(["https://arxiv.org/abs/2412.05449v1"])
        assert calls["n"] == 2
        assert items[0].title == "A"

    def test_returns_items_in_input_order(self, monkeypatch):
        entries = [
            {"link": "http://arxiv.org/abs/2605.21404v1", "title": "B", "summary": "초록 B"},
            {"link": "http://arxiv.org/abs/2412.05449v1", "title": "A", "summary": "초록 A"},
        ]
        monkeypatch.setattr(
            "export.urls.feedparser.parse", lambda url, **kw: type("F", (), {"entries": entries})()
        )
        items = collect_urls(
            ["https://arxiv.org/abs/2412.05449v1", "https://arxiv.org/abs/2605.21404v1"]
        )
        assert [i.title for i in items] == ["A", "B"]


class TestCollect:
    def test_gate_rejection_skips_the_expensive_extraction(self, patched, tmp_path):
        items = [_item("https://news.hada.io/topic?id=1", "비-AI 기사")]
        gate, extraction = patched["install"](items, relevant={"비-AI 기사": False})
        store = ExtractionStore(tmp_path / "s")

        stored = run_collect({}, take={"GeekNews": 1}, urls=[], store=store)

        assert stored == []
        assert gate.calls == ["RelevanceGate"]
        assert extraction.calls == []

    def test_budget_caps_the_number_of_extractions(self, patched, tmp_path):
        items = [_item(f"https://news.hada.io/topic?id={i}", f"기사 {i}") for i in range(10)]
        _, extraction = patched["install"](items)
        store = ExtractionStore(tmp_path / "s")

        stored = run_collect({}, take={"GeekNews": 10}, urls=[], store=store, max_extractions=3)

        assert len(stored) == 3
        assert extraction.calls == ["NewsOntology"] * 3

    def test_already_stored_documents_are_not_paid_for_twice(self, patched, tmp_path):
        items = [_item("https://news.hada.io/topic?id=1", "기사")]
        _, extraction = patched["install"](items)
        store = ExtractionStore(tmp_path / "s")

        run_collect({}, take={"GeekNews": 1}, urls=[], store=store)
        first = len(extraction.calls)
        run_collect({}, take={"GeekNews": 1}, urls=[], store=store)

        assert len(extraction.calls) == first

    def test_injected_urls_are_processed_before_feeds(self, patched, tmp_path):
        feed = [_item("https://news.hada.io/topic?id=1", "피드 기사")]
        injected = [
            _item(
                "http://arxiv.org/abs/2412.05449v1",
                "주입 논문",
                source_name="arXiv (id_list API)",
            )
        ]
        patched["install"](feed, injected=injected)
        store = ExtractionStore(tmp_path / "s")

        stored = run_collect({}, take={"GeekNews": 1}, urls=["x"], store=store)

        assert stored[0] == "arXiv:2412.05449v1"

    def test_stored_payload_keeps_the_raw_item_and_both_stages(self, patched, tmp_path):
        items = [_item("https://news.hada.io/topic?id=1", "기사")]
        patched["install"](items)
        store = ExtractionStore(tmp_path / "s")

        run_collect({}, take={"GeekNews": 1}, urls=[], store=store)
        payload = json.loads(next(iter(store.directory.glob("*.json"))).read_text(encoding="utf-8"))

        assert payload["raw_item"]["title"] == "기사"
        assert payload["gate"]["model"] == "claude-haiku-4-5-20251001"
        assert payload["gate"]["is_relevant"] is True
        assert payload["extraction"]["model"] == "claude-opus-5"
        assert payload["extraction"]["ontology"]["release_type"] == "Paper"

    def test_store_writes_nothing_outside_its_directory(self, patched, tmp_path):
        items = [_item("https://news.hada.io/topic?id=1", "기사")]
        patched["install"](items)
        store = ExtractionStore(tmp_path / "s")

        run_collect({}, take={"GeekNews": 1}, urls=[], store=store)

        assert [p.name for p in tmp_path.iterdir()] == ["s"]


class TestObservability:
    """export 경로도 `process_item` 과 같은 것을 관측한다 (D-016, D-035).

    이 경로로 돌렸다는 이유로 두 큐가 비면, 나중에 "그 실행에서 아무것도 안 걸렸다"와
    "기록 자체가 안 됐다"를 구분할 수 없다.
    """

    def test_gate_skips_are_recorded(self, patched, tmp_path):
        from observability.events import InMemoryObserver

        items = [_item("https://news.hada.io/topic?id=1", "비-AI 기사")]
        patched["install"](items, relevant={"비-AI 기사": False})
        observer = InMemoryObserver()

        run_collect(
            {}, take={"GeekNews": 1}, urls=[], store=ExtractionStore(tmp_path / "s"),
            observer=observer,
        )

        assert len(observer.skips) == 1
        assert observer.skips[0].source_name == "GeekNews"
        assert observer.skips[0].model == "claude-haiku-4-5-20251001"

    def test_unresolved_companies_reach_the_queue(self, patched, tmp_path, monkeypatch):
        from observability.events import InMemoryObserver

        items = [_item("https://news.hada.io/topic?id=1", "기사")]
        gate, extraction = patched["install"](items)

        original = extraction.parse_into

        def with_company(*, system, user, output_model):
            result = original(system=system, user=user, output_model=output_model)
            if output_model is NewsOntology:
                result.value.companies.append(
                    __import__("extraction.schema", fromlist=["CompanyRef"]).CompanyRef(원문표기="Cursor")
                )
            return result

        monkeypatch.setattr(extraction, "parse_into", with_company)
        observer = InMemoryObserver()

        run_collect(
            {}, take={"GeekNews": 1}, urls=[], store=ExtractionStore(tmp_path / "s"),
            observer=observer,
        )

        assert [r.raw_name for r in observer.unknown_companies] == ["Cursor"]

    def test_default_observer_is_a_no_op(self, patched, tmp_path):
        """관측을 끄는 것이 파이프라인을 끄는 것이 되어서는 안 된다 (D-008)."""
        items = [_item("https://news.hada.io/topic?id=1", "기사")]
        patched["install"](items)

        stored = run_collect({}, take={"GeekNews": 1}, urls=[], store=ExtractionStore(tmp_path / "s"))

        assert len(stored) == 1
