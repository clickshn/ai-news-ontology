"""골든셋 → 예측 경로 (`eval/predict.py`) — **유도한 입력을 조용히 만들지 않는다.**

## 이 파일이 덮는 범위

이 모듈이 존재하는 이유가 하나고, 위험도 거기 있다. 골든셋은 **프롬프트 입력을
전부 보존하지 않는다** — `build_variables` 가 쓰는 `source_name`·`published_at`
이 파일에 없다. 없는 값을 기본값으로 때우면 프롬프트에 무엇이 들어갔는지 모르는
채로 점수가 나오고, **그 점수는 나중에 구분할 방법이 없다.**

그래서 여기서 고정하는 것은 셋이다.

1. 유도가 **맞는 값**을 낸다 (`GeekNews`, `arXiv cs.CL (Atom API)`, 날짜)
2. 유도가 **안 되면 멈춘다** — 못 찾을 때도, 여럿 맞을 때도
3. 유도했다는 **사실이 산출물에 남는다** (`derived_inputs`)

실패 분류(`schema` / `transport`)와 분모 계산은 `export/replay.py` 와 같은 규칙을
쓰므로 같은 성질을 여기서도 잰다 — 두 실행기가 갈리면 같은 사건이 다른 이름으로
집계된다.

**네트워크로 나가지 않는다.** `parse_into` 하나만 있는 대역을 쓴다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from eval.predict import (
    PredictError,
    failure_kind,
    predict_one,
    raw_item_from_golden,
    resolve_published_at,
    resolve_source_name,
    summarize,
)
from eval.schema import GoldenItem
from extraction.extractor import DEFAULT_PROMPT, load_prompt
from extraction.llm import SchemaMismatchError, StructuredResult, Usage
from extraction.schema import NewsOntology

CONFIG = {
    "sources": {
        "rss": [
            {"name": "GeekNews", "url": "https://news.hada.io/rss/news"},
            {
                "name": "arXiv cs.CL (Atom API)",
                "url": "http://export.arxiv.org/api/query?search_query=cat:cs.CL",
            },
        ]
    }
}


def _golden(**overrides: Any) -> GoldenItem:
    payload: dict[str, Any] = {
        "id": "20260829-geeknews-33001-ffmpeg",
        "source_url": "https://news.hada.io/topic?id=33001",
        "status": "confirmed",
        "input": {"제목": "  제목에 공백이 있다  ", "본문": "본문\n\n두 문단"},
        "expected": {
            "기술영역": ["Application/Product"],
            "발표유형": "Community/Discussion",
            "관련기업": [],
            "관련기존기술": [],
            "영향도": {"점수": 1, "근거": "근거"},
        },
        "labeled_by": "netisa",
        "labeled_at": "2026-08-30",
    }
    payload.update(overrides)
    return GoldenItem.model_validate(payload)


# ---------------------------------------------------------------------------
# 1. 유도가 맞는 값을 내는가
# ---------------------------------------------------------------------------
class TestResolveSourceName:
    def test_it_finds_the_configured_name_by_host(self):
        assert resolve_source_name("https://news.hada.io/topic?id=33001", CONFIG) == "GeekNews"

    def test_a_subdomain_matches_the_feed_host(self):
        """피드는 `export.arxiv.org` 인데 기사 URL 은 `arxiv.org` 다.

        뒤 두 라벨로 맞추지 않으면 arXiv 항목이 통째로 유도 실패가 된다.
        """
        assert (
            resolve_source_name("https://arxiv.org/abs/2608.31100v1", CONFIG)
            == "arXiv cs.CL (Atom API)"
        )


class TestResolvePublishedAt:
    def test_it_reads_the_date_prefix_of_the_golden_id(self):
        assert resolve_published_at("20260831-arxiv-2608.31100-s3gym") == date(2026, 8, 31)


# ---------------------------------------------------------------------------
# 2. 유도가 안 되면 멈춘다
# ---------------------------------------------------------------------------
class TestItStopsInsteadOfGuessing:
    """**기본값을 넣고 진행하지 않는다.** 여기서 멈추는 것이 이 모듈의 계약이다."""

    def test_an_unknown_host_raises(self):
        with pytest.raises(PredictError, match="config.yaml"):
            resolve_source_name("https://example.com/post/1", CONFIG)

    def test_an_ambiguous_host_raises_instead_of_picking_one(self):
        """같은 도메인에 소스가 둘이면 **어느 이름이 프롬프트로 나갈지 모른다.**

        하나를 고르면 실행은 되지만, 나중에 그 점수가 어느 입력에서 나온
        것인지 확인할 방법이 없다.
        """
        config = {
            "sources": {
                "rss": [
                    {"name": "GeekNews", "url": "https://news.hada.io/rss/news"},
                    {"name": "GeekNews (topics)", "url": "https://news.hada.io/rss/topics"},
                ]
            }
        }
        with pytest.raises(PredictError, match="여럿"):
            resolve_source_name("https://news.hada.io/topic?id=33001", config)

    def test_an_id_without_a_date_prefix_raises(self):
        with pytest.raises(PredictError, match="published_at"):
            resolve_published_at("ffmpeg-bug")


# ---------------------------------------------------------------------------
# 3. 입력을 손보지 않고, 유도한 사실을 남긴다
# ---------------------------------------------------------------------------
class TestRawItemFromGolden:
    def test_the_title_and_body_are_passed_through_unchanged(self):
        """**한 글자도 손보지 않는다.** 정규화하면 입력이 달라지고 재는 것이 달라진다."""
        golden = _golden()
        item, _ = raw_item_from_golden(golden, CONFIG)
        assert item.title == "  제목에 공백이 있다  "
        assert item.body == "본문\n\n두 문단"

    def test_it_reports_what_it_derived(self):
        _, derived = raw_item_from_golden(_golden(), CONFIG)
        assert set(derived) == {"source_name", "published_at"}
        assert "유도" in derived["source_name"]
        assert "유도" in derived["published_at"]

    def test_the_derivation_is_carried_into_the_row(self):
        """산출물에 남지 않으면 유도값이 보존값과 구분되지 않는다."""
        row = predict_one(_golden(), _Client(_ok()), load_prompt(DEFAULT_PROMPT), CONFIG, _NO_DIR)
        assert row["derived_inputs"]["source_name"].startswith("GeekNews")


# ---------------------------------------------------------------------------
# 4. 실패 분류 — `export/replay.py` 와 같은 규칙
# ---------------------------------------------------------------------------
class _Client:
    """`parse_into` 하나만 있는 최소 대역. **네트워크로 나가지 않는다.**"""

    model = "gemma-4-31B-it"

    def __init__(self, outcome: Any, raws: list[dict[str, Any]] | None = None):
        self._outcome = outcome
        self.last_raw_responses = raws or []

    def parse_into(self, *, system, user, output_model):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _ok() -> StructuredResult[NewsOntology]:
    ontology = NewsOntology.model_validate(
        {
            "기술영역": ["Application/Product"],
            "발표유형": "Community/Discussion",
            "관련기업": [],
            "관련기존기술": [],
            "영향도": {"점수": 1, "근거": "국소적 DoS 결함으로 실무 파급력이 낮다"},
            "요약": "FFmpeg 디먹서에서 0 나누기 버그가 발견됐다. 악성 파일을 여는 애플리케이션이 충돌할 수 있다.",
        }
    )
    return StructuredResult(value=ontology, usage=Usage(), attempts=1)


class _NoDir:
    """`_persist_raw` 가 응답이 없을 때 파일을 만들지 않는지 확인하는 감시자."""

    def __truediv__(self, other):  # pragma: no cover - 불리면 그 자체가 실패다
        raise AssertionError(f"응답이 없는데 raw 경로를 만들려 했다: {other}")


_NO_DIR = _NoDir()


class TestFailureClassification:
    def test_a_schema_mismatch_is_schema(self):
        client = _Client(SchemaMismatchError("파싱 실패", attempts=3, last_error="bad json"))
        row = predict_one(_golden(), client, load_prompt(DEFAULT_PROMPT), CONFIG, _NO_DIR)
        assert failure_kind(row) == "schema"
        assert row["attempts"] == 3
        assert row["ontology"] is None

    def test_a_transport_error_is_transport(self):
        """**모델에게 물어보지 못한 건**이다. 스키마 실패와 같은 칸에 세지 않는다."""
        client = _Client(OSError("연결이 끊겼다"))
        row = predict_one(_golden(), client, load_prompt(DEFAULT_PROMPT), CONFIG, _NO_DIR)
        assert failure_kind(row) == "transport"

    def test_a_success_carries_the_ontology(self):
        row = predict_one(_golden(), _Client(_ok()), load_prompt(DEFAULT_PROMPT), CONFIG, _NO_DIR)
        assert failure_kind(row) is None
        assert row["ontology"]["발표유형"] == "Community/Discussion"

    def test_no_raw_file_is_written_when_there_was_no_response(self):
        """빈 파일을 남기면 다음 실행이 그걸 '응답 0건짜리 결과'로 읽는다.

        `_NO_DIR` 이 경로를 만들려는 시도 자체를 실패로 만든다.
        """
        client = _Client(OSError("끊겼다"), raws=[])
        row = predict_one(_golden(), client, load_prompt(DEFAULT_PROMPT), CONFIG, _NO_DIR)
        assert "finish_reasons" not in row


class TestRawIsWrittenBeforeTheRowIsBuilt:
    def test_the_response_survives_a_schema_failure(self, tmp_path):
        """응답은 **변환보다 먼저** 디스크에 있어야 한다 (D-052).

        스키마 실패는 변환이 터진 경우다. 그때도 원본이 남아 있어야 나중에
        무엇이 왔는지 볼 수 있다 — 다시 부르면 그건 다른 응답이다.
        """
        raws = [{"choices": [{"finish_reason": "length"}]}]
        client = _Client(SchemaMismatchError("실패", attempts=2, last_error="x"), raws=raws)
        row = predict_one(_golden(), client, load_prompt(DEFAULT_PROMPT), CONFIG, tmp_path)
        written = list(tmp_path.glob("*.json"))
        assert len(written) == 1
        assert row["finish_reasons"] == ["length"]


# ---------------------------------------------------------------------------
# 5. 집계 — 전송 실패는 분모에서 빠진다
# ---------------------------------------------------------------------------
def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"item_id": "i", "ok": True, "failure": None, "attempts": 1}
    row.update(overrides)
    return row


class TestSummarize:
    def test_transport_failures_leave_the_denominator(self):
        """남겨 두면 회선이 나쁜 날일수록 실패율이 **낮게** 나온다 (D-050)."""
        rows = [_row(), _row(ok=False, failure="SchemaMismatchError"), _row(ok=False, failure="OSError")]
        summary = summarize(rows)
        assert summary["measured_items"] == 2
        assert summary["schema_failure_rate"] == 0.5

    def test_all_transport_gives_none_not_zero(self):
        """0% 는 '다 성공했다'로 읽힌다. **재지 못한 것을 잰 것처럼 쓰지 않는다.**"""
        summary = summarize([_row(ok=False, failure="OSError")])
        assert summary["measured_items"] == 0
        assert summary["schema_failure_rate"] is None

    def test_truncation_is_counted_separately_from_schema_failure(self):
        """절단은 **디코딩 설정**(`max_tokens`)이지 모델 능력이 아니다."""
        summary = summarize([_row(finish_reasons=["length"])])
        assert summary["truncated"] == 1
