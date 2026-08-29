"""관련성 게이트와 2단계 파이프라인 검증 (실제 API 호출 없음).

이 테스트가 지키는 핵심 계약: **게이트가 false 를 내면 정밀 추출 클라이언트가
호출되지 않는다.** 그게 이 구조의 비용 절감 전부이므로 호출 횟수로 고정한다.
"""

from __future__ import annotations

from datetime import date

import pytest

from collectors.base import RawItem
from extraction.extractor import check_relevance, load_prompt, process_item
from extraction.llm import LLMClient, StructuredResult, Usage
from extraction.schema import NewsOntology, RelevanceGate
from observability.events import InMemoryObserver, PipelineObserver, SkipRecord

VALID_ONTOLOGY = {
    "요약": "LLM 보조로 만든 퍼저가 오픈소스 프로젝트에서 버그를 찾아낸 사례다. 재현 조건과 원인 코드가 함께 공개됐다.",
    "기술영역": ["Agent"],
    "발표유형": "Benchmark/Report",
    "관련기업": [],
    "관련기존기술": ["퍼징"],
    "영향도": {"점수": 3, "근거": "AI 퍼저의 실제 적용 사례로 도구 선택에 영향을 준다."},
}


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------
@pytest.fixture
def ai_item() -> RawItem:
    return RawItem(
        url="https://news.hada.io/topic?id=33001",
        title="바이브코딩으로 만든 퍼저가 FFmpeg의 0 나누기 버그를 발견",
        body="FFmpeg의 Sony PS2 VPK 디먹서에서 21바이트 입력으로 재현되는 버그가 발견됨",
        source_name="GeekNews",
        published_at=date(2026, 8, 29),
        collected_at=date(2026, 8, 29),
        tags=("ko", "aggregator"),
    )


@pytest.fixture
def non_ai_item() -> RawItem:
    return RawItem(
        url="https://news.hada.io/topic?id=32998",
        title="EasyEffects를 모든 Linux 배포판에 포함해 노트북 스피커 음질을 개선해야 함",
        body="EasyEffects 는 PipeWire 기반 오디오 이펙트 도구로, 노트북 스피커의 음질을 보정한다.",
        source_name="GeekNews",
        published_at=date(2026, 8, 29),
        collected_at=date(2026, 8, 29),
        tags=("ko", "aggregator"),
    )


class CountingClient:
    """호출 횟수를 세는 가짜 LLM 클라이언트."""

    def __init__(self, payload_by_model: dict[type, dict]) -> None:
        self.payload_by_model = payload_by_model
        self.calls: list[dict] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def parse_into(self, *, system, user, output_model):
        self.calls.append({"system": system, "user": user, "output_model": output_model})
        payload = self.payload_by_model[output_model]
        return StructuredResult(
            value=output_model.model_validate(payload),
            usage=Usage(input_tokens=800, output_tokens=60, model="fake-haiku"),
        )


def gate_client(is_relevant: bool, reason: str) -> CountingClient:
    return CountingClient({RelevanceGate: {"관련있음": is_relevant, "근거": reason}})


def extraction_client() -> CountingClient:
    return CountingClient({NewsOntology: VALID_ONTOLOGY})


# ---------------------------------------------------------------------------
# 1. 게이트 단독
# ---------------------------------------------------------------------------
def test_gate_marks_ai_item_relevant(ai_item):
    client = gate_client(True, "AI 퍼저를 실제 버그 발견에 적용한 사례라 직무에 쓸모가 있다")
    result = check_relevance(ai_item, client)

    assert result.is_relevant is True
    assert result.gate.reason
    assert result.prompt_name == "relevance_gate.v1.md"
    assert client.calls[0]["output_model"] is RelevanceGate


def test_gate_marks_non_ai_item_irrelevant(non_ai_item):
    client = gate_client(False, "리눅스 오디오 도구 소개로 AI나 자동화와 접점이 없다")
    result = check_relevance(non_ai_item, client)

    assert result.is_relevant is False
    assert "AI" in result.gate.reason or "자동화" in result.gate.reason


def test_gate_prompt_includes_body_not_just_title(non_ai_item):
    """제목만으로 판단하지 말라는 지시가 실제로 본문을 프롬프트에 넣는지 확인."""
    client = gate_client(False, "리눅스 오디오 도구 소개로 AI와 접점이 없다")
    check_relevance(non_ai_item, client)

    call = client.calls[0]
    assert non_ai_item.title in call["user"]
    assert non_ai_item.body in call["user"]
    assert "{{" not in call["user"] and "{{" not in call["system"]
    assert "제목만으로 판단하지 않는다" in call["system"]


def test_gate_reason_min_length_is_enforced():
    """min_length 는 품질 보장이 아니라 무응답 방지용 하한선이다."""
    with pytest.raises(Exception):
        RelevanceGate.model_validate({"관련있음": False, "근거": "없음"})
    # 길이만 넘기면 무의미해도 통과한다 — 이 성질을 문서화된 대로 고정해 둔다.
    assert RelevanceGate.model_validate({"관련있음": False, "근거": "가" * 10}).reason


# ---------------------------------------------------------------------------
# 2. 2단계 흐름 — 비용 절감의 핵심 계약
# ---------------------------------------------------------------------------
def test_irrelevant_item_never_calls_extraction_client(non_ai_item):
    gate = gate_client(False, "리눅스 오디오 도구 소개로 AI나 자동화와 접점이 없다")
    extract = extraction_client()

    result = process_item(non_ai_item, gate_client=gate, extraction_client=extract)

    assert result.skipped is True
    assert result.extraction is None
    assert gate.call_count == 1
    assert extract.call_count == 0, "게이트에서 걸러졌는데 정밀 추출이 호출됐다"


def test_relevant_item_calls_extraction_client_once(ai_item):
    gate = gate_client(True, "AI 퍼저를 실제 버그 발견에 적용한 사례다")
    extract = extraction_client()

    result = process_item(ai_item, gate_client=gate, extraction_client=extract)

    assert result.skipped is False
    assert isinstance(result.extraction.ontology, NewsOntology)
    assert gate.call_count == 1
    assert extract.call_count == 1
    assert extract.calls[0]["output_model"] is NewsOntology


def test_gate_and_extraction_use_different_prompts(ai_item):
    gate = gate_client(True, "AI 에이전트 관련 내용이 본문에 있다")
    extract = extraction_client()

    result = process_item(ai_item, gate_client=gate, extraction_client=extract)

    assert result.relevance.prompt_name == "relevance_gate.v1.md"
    assert result.extraction.prompt_name == "extract_ontology.v3.md"
    assert gate.calls[0]["system"] != extract.calls[0]["system"]


# ---------------------------------------------------------------------------
# 3. 스킵 기록 (observability)
# ---------------------------------------------------------------------------
def test_skip_is_recorded_with_enough_context(non_ai_item):
    gate = gate_client(False, "리눅스 오디오 도구 소개로 AI나 자동화와 접점이 없다")
    observer = InMemoryObserver()

    process_item(
        non_ai_item,
        gate_client=gate,
        extraction_client=extraction_client(),
        observer=observer,
    )

    assert len(observer.skips) == 1
    record = observer.skips[0]
    # 나중에 오탐을 표본 검토하려면 아래 정보가 모두 필요하다 (D-016).
    assert record.url == str(non_ai_item.url)
    assert record.title == non_ai_item.title
    assert record.source_name == "GeekNews"
    assert record.reason
    assert record.stage == "relevance_gate"
    assert record.model == "fake-haiku"
    assert record.prompt_name == "relevance_gate.v1.md"
    assert record.decided_at.tzinfo is not None, "타임스탬프는 tz-aware 여야 한다"


def test_relevant_item_records_no_skip(ai_item):
    observer = InMemoryObserver()
    process_item(
        ai_item,
        gate_client=gate_client(True, "AI 적용 사례라 직무에 쓸모가 있다"),
        extraction_client=extraction_client(),
        observer=observer,
    )
    assert observer.skips == []


def test_observer_is_optional(non_ai_item):
    """observer 를 넘기지 않아도 파이프라인이 그대로 돈다 (D-008)."""
    result = process_item(
        non_ai_item,
        gate_client=gate_client(False, "AI와 접점이 없는 일반 개발 글이다"),
        extraction_client=extraction_client(),
    )
    assert result.skipped is True


def test_in_memory_observer_satisfies_protocol():
    assert isinstance(InMemoryObserver(), PipelineObserver)


def test_skip_record_serializes_for_jsonl():
    record = SkipRecord(
        url="https://news.hada.io/topic?id=1",
        title="제목",
        source_name="GeekNews",
        reason="AI와 접점이 없다",
    )
    data = record.to_dict()
    assert set(data) == {
        "stage", "url", "title", "source_name", "reason", "model", "prompt_name", "decided_at",
    }
    assert isinstance(data["decided_at"], str)


# ---------------------------------------------------------------------------
# 4. 단계별 모델 설정
# ---------------------------------------------------------------------------
def test_from_config_reads_per_stage_models():
    from extraction.llm import AnthropicClient

    config = {
        "llm": {
            "relevance_gate": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "effort": None,
                "thinking": "disabled",
            },
            "extraction": {
                "model": "claude-opus-5",
                "max_tokens": 8000,
                "effort": "high",
                "thinking": "adaptive",
            },
        }
    }
    gate = AnthropicClient.from_config(config, stage="relevance_gate", client=object())
    extract = AnthropicClient.from_config(config, stage="extraction", client=object())

    assert gate.model == "claude-haiku-4-5-20251001"
    assert gate.effort is None
    assert gate.thinking is False
    assert extract.model == "claude-opus-5"
    assert extract.effort == "high"
    assert extract.thinking is True


def test_effort_omitted_from_request_when_none():
    """Haiku 4.5 는 output_config.effort 를 거부하므로 아예 보내지 않아야 한다."""
    from extraction.llm import AnthropicClient

    gate = AnthropicClient(model="claude-haiku-4-5-20251001", effort=None, thinking=False,
                           client=object())
    kwargs = gate._request_kwargs()
    assert "output_config" not in kwargs
    assert "thinking" not in kwargs

    extract = AnthropicClient(model="claude-opus-5", effort="high", client=object())
    kwargs = extract._request_kwargs()
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["thinking"] == {"type": "adaptive"}


def test_from_config_falls_back_to_flat_llm_block():
    """하위 블록이 없는 구버전 설정과도 호환된다."""
    from extraction.llm import AnthropicClient

    client = AnthropicClient.from_config(
        {"llm": {"model": "claude-opus-5", "effort": "high"}},
        stage="relevance_gate",
        client=object(),
    )
    assert client.model == "claude-opus-5"
