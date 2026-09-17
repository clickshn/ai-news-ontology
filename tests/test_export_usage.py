"""소스별 usage 집계 (`export/usage.py`)."""

from __future__ import annotations

from export.usage import PRICES, collect_usage, format_report


def _with_usage(payload, *, gate_in=3000, gate_out=80, ex_in=6000, ex_out=500, thinking=250):
    payload["extraction"]["usage"] = {
        "input_tokens": ex_in,
        "output_tokens": ex_out,
        "thinking_tokens": thinking,
    }
    payload["gate"] = {
        "prompt_name": "relevance_gate.v1.md",
        "model": "claude-haiku-4-5-20251001",
        "is_relevant": True,
        "reason": "AI 관련 기사다.",
        "usage": {"input_tokens": gate_in, "output_tokens": gate_out},
    }
    return payload


class TestCollectUsage:
    def test_groups_by_source_name(self, make_payload, make_store):
        store = make_store(
            [
                _with_usage(make_payload(doc_id="a", url="https://e.com/a", source_name="GeekNews")),
                _with_usage(make_payload(doc_id="b", url="https://e.com/b", source_name="GeekNews")),
                _with_usage(make_payload(doc_id="c", url="https://e.com/c", source_name="OpenAI News")),
            ]
        )
        usage = collect_usage(store)
        assert usage["GeekNews"].records == 2
        assert usage["OpenAI News"].records == 1

    def test_cost_uses_the_documented_per_model_prices(self, make_payload, make_store):
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a"), ex_in=1_000_000, ex_out=0,
                         gate_in=0, gate_out=0)]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.extract_cost == PRICES["extraction"]["input"]

    def test_thinking_is_reported_but_not_added_to_cost(self, make_payload, make_store):
        """thinking 은 output_tokens 의 부분집합이다 (D-019)."""
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a"),
                         ex_in=0, ex_out=1_000_000, thinking=400_000, gate_in=0, gate_out=0)]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.thinking == 400_000
        assert usage.extract_cost == PRICES["extraction"]["output"]

    def test_median_body_length_is_reported(self, make_payload, make_store):
        store = make_store(
            [
                _with_usage(make_payload(doc_id="a", url="https://e.com/a", body="x" * 10)),
                _with_usage(make_payload(doc_id="b", url="https://e.com/b", body="x" * 100)),
                _with_usage(make_payload(doc_id="c", url="https://e.com/c", body="x" * 1000)),
            ]
        )
        assert collect_usage(store)["GeekNews"].median_body == 100

    def test_report_lists_every_source(self, make_payload, make_store):
        store = make_store(
            [
                _with_usage(make_payload(doc_id="a", url="https://e.com/a", source_name="GeekNews")),
                _with_usage(make_payload(doc_id="b", url="https://e.com/b", source_name="Hugging Face Blog")),
            ]
        )
        report = format_report(collect_usage(store))
        assert "GeekNews" in report
        assert "Hugging Face Blog" in report
        assert "합계 2건" in report
