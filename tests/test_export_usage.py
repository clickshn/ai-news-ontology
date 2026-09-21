"""소스별 usage 집계 (`export/usage.py`) — 단가는 **모델**에 붙는다 (F4).

이 파일이 재는 축은 셋이다.

1. **단가를 모델명으로 고르는가** — 단계 이름(`gate`/`extraction`)으로 고르면
   vLLM 이전 뒤에도 벤더 달러가 계속 곱해진다. 그게 F4 다.
2. **모르는 모델을 0 으로 적지 않는가** — `0.0` 은 "청구가 없다"는 등록된 사실이고
   미등록은 "모른다"다. 같은 칸에 섞이면 보고서가 없는 확신을 만든다.
3. **부분 합계를 합계로 적지 않는가** — 뺀 건수가 보여야 한다.
"""

from __future__ import annotations

import pytest

from export.usage import UNPRICED, collect_usage, format_report, price_for

OPUS = "claude-opus-5"
HAIKU = "claude-haiku-4-5-20251001"
GEMMA = "gemma-4-31B-it"


def _with_usage(
    payload,
    *,
    gate_in=3000,
    gate_out=80,
    ex_in=6000,
    ex_out=500,
    thinking=250,
    gate_model=HAIKU,
    with_gate=True,
):
    payload["extraction"]["usage"] = {
        "input_tokens": ex_in,
        "output_tokens": ex_out,
        "thinking_tokens": thinking,
    }
    if with_gate:
        payload["gate"] = {
            "prompt_name": "relevance_gate.v1.md",
            "model": gate_model,
            "is_relevant": True,
            "reason": "AI 관련 기사다.",
            "usage": {"input_tokens": gate_in, "output_tokens": gate_out},
        }
    return payload


class TestPriceLookup:
    def test_known_models_have_a_price(self):
        assert price_for(OPUS) == {"input": 5.0, "output": 25.0}
        assert price_for(HAIKU) == {"input": 1.0, "output": 5.0}

    def test_the_date_suffix_does_not_need_its_own_entry(self):
        """`claude-haiku-4-5-20251001` 을 버전마다 등록하지 않는다 (접두사 매칭)."""
        assert price_for("claude-haiku-4-5-20260401") == price_for(HAIKU)

    def test_the_internal_model_is_registered_at_zero(self):
        """`0.0` 은 **등록된 사실**이다 — 내부 vLLM 은 토큰당 청구가 없다."""
        assert price_for(GEMMA) == {"input": 0.0, "output": 0.0}

    @pytest.mark.parametrize("model", ["gpt-9", "llama-4-70b", "", None])
    def test_unknown_models_are_none_not_zero(self, model):
        assert price_for(model) is None


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

    def test_cost_uses_the_price_of_the_model_in_the_record(self, make_payload, make_store):
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a"), ex_in=1_000_000, ex_out=0,
                         gate_in=0, gate_out=0)]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.extract_cost == 5.0  # claude-opus-5 입력 $5/1M

    def test_the_extraction_stage_is_not_always_the_expensive_model(self, make_payload, make_store):
        """**F4 의 핵심.** 단계 이름으로 고르면 여기서 Opus 단가($5)가 곱해진다."""
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a", model=HAIKU),
                         ex_in=1_000_000, ex_out=0, gate_in=0, gate_out=0)]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.extract_cost == 1.0  # Haiku 입력 $1/1M

    def test_the_gate_model_is_priced_separately_from_the_extraction_model(
        self, make_payload, make_store
    ):
        """게이트는 **싼 모델로 거르라고** 만든 단계다 (D-015). 둘을 한 단가로 묶지 않는다."""
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a", model=OPUS),
                         ex_in=1_000_000, ex_out=0, gate_in=1_000_000, gate_out=0)]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.gate_cost == 1.0
        assert usage.extract_cost == 5.0
        assert usage.total_cost == 6.0

    def test_an_internal_run_costs_zero_dollars(self, make_payload, make_store):
        """vLLM 실행에 벤더 달러가 찍히던 것이 F4 다."""
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a", model=GEMMA),
                         gate_model=GEMMA, ex_in=6000, ex_out=500, gate_in=3000, gate_out=80)]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.total_cost == 0.0
        assert usage.cost_per_record == 0.0
        assert usage.unpriced_models == ()

    def test_an_unregistered_model_makes_the_cost_none(self, make_payload, make_store):
        """재지 못한 것을 0 으로 적으면 "공짜로 돌았다"로 읽힌다 (D-050)."""
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a", model="mistral-large"))]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.extract_cost is None
        assert usage.total_cost is None
        assert usage.cost_per_record is None
        assert usage.unpriced_models == ("mistral-large",)

    def test_one_unpriced_model_poisons_the_whole_source(self, make_payload, make_store):
        """아는 부분만 더한 값은 "이 소스의 비용"이 아닌데 이름이 같아지면 구분이 안 된다."""
        store = make_store(
            [
                _with_usage(make_payload(doc_id="a", url="https://e.com/a", model=OPUS)),
                _with_usage(make_payload(doc_id="b", url="https://e.com/b", model="mistral-large")),
            ]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.total_cost is None
        assert usage.unpriced_models == ("mistral-large",)

    def test_models_are_tracked_even_when_they_agree(self, make_payload, make_store):
        """한 소스 안에서도 실행 시점에 따라 모델이 갈린다 — 합쳐 두면 못 곱한다."""
        store = make_store(
            [
                _with_usage(make_payload(doc_id="a", url="https://e.com/a", model=OPUS),
                            ex_in=1_000_000, ex_out=0, gate_in=0, gate_out=0),
                _with_usage(make_payload(doc_id="b", url="https://e.com/b", model=GEMMA),
                            gate_model=GEMMA, ex_in=1_000_000, ex_out=0, gate_in=0, gate_out=0),
            ]
        )
        usage = collect_usage(store)["GeekNews"]
        assert set(usage.models) == {OPUS, GEMMA, HAIKU}  # 게이트 모델까지 센다
        assert usage.extract_cost == 5.0  # gemma 쪽 1M 토큰은 $0
        assert usage.extract_input == 2_000_000

    def test_a_record_without_a_model_name_is_unpriced(self, make_payload, make_store):
        payload = _with_usage(make_payload(doc_id="a", url="https://e.com/a"))
        payload["extraction"].pop("model")
        store = make_store([payload])
        usage = collect_usage(store)["GeekNews"]
        assert usage.total_cost is None
        assert usage.unpriced_models == (None,)

    def test_a_record_without_a_gate_has_no_gate_call(self, make_payload, make_store):
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a"), with_gate=False)]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.gate_calls == 0
        assert usage.gate_cost == 0.0

    def test_thinking_is_reported_but_not_added_to_cost(self, make_payload, make_store):
        """thinking 은 output_tokens 의 부분집합이다 (D-019)."""
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a"),
                         ex_in=0, ex_out=1_000_000, thinking=400_000, gate_in=0, gate_out=0)]
        )
        usage = collect_usage(store)["GeekNews"]
        assert usage.thinking == 400_000
        assert usage.extract_cost == 25.0  # Opus 출력 $25/1M

    def test_median_body_length_is_reported(self, make_payload, make_store):
        store = make_store(
            [
                _with_usage(make_payload(doc_id="a", url="https://e.com/a", body="x" * 10)),
                _with_usage(make_payload(doc_id="b", url="https://e.com/b", body="x" * 100)),
                _with_usage(make_payload(doc_id="c", url="https://e.com/c", body="x" * 1000)),
            ]
        )
        assert collect_usage(store)["GeekNews"].median_body == 100


class TestFormatReport:
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

    def test_the_report_names_the_models_it_priced(self, make_payload, make_store):
        store = make_store([_with_usage(make_payload(doc_id="a", url="https://e.com/a"))])
        report = format_report(collect_usage(store))
        assert OPUS in report
        assert HAIKU in report

    def test_unpriced_rows_say_so_instead_of_showing_a_dollar_figure(
        self, make_payload, make_store
    ):
        store = make_store(
            [_with_usage(make_payload(doc_id="a", url="https://e.com/a", model="mistral-large"))]
        )
        report = format_report(collect_usage(store))
        assert UNPRICED in report
        # **금액이 한 군데도 찍히면 안 된다.** `$0.0000` 은 "공짜로 돌았다"로 읽힌다.
        assert "$" not in report

    def test_the_total_says_how_many_records_it_left_out(self, make_payload, make_store):
        """부분 합계를 합계로 적으면 **뺀 만큼 싸 보인다.**"""
        store = make_store(
            [
                _with_usage(make_payload(doc_id="a", url="https://e.com/a", source_name="GeekNews"),
                            ex_in=1_000_000, ex_out=0, gate_in=0, gate_out=0),
                _with_usage(make_payload(doc_id="b", url="https://e.com/b", source_name="Other",
                                         model="mistral-large")),
            ]
        )
        report = format_report(collect_usage(store))
        assert "합계 2건" in report
        assert "$5.000" in report  # 아는 1건만
        assert "단가를 아는 1건만" in report
        assert "1건은 단가 미등록" in report
        assert "mistral-large" in report

    def test_a_fully_priced_report_has_no_exclusion_note(self, make_payload, make_store):
        store = make_store([_with_usage(make_payload(doc_id="a", url="https://e.com/a"))])
        report = format_report(collect_usage(store))
        assert UNPRICED not in report
