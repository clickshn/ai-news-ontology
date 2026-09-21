"""대조 실행기의 집계 (`export/replay.py`) — 전송 실패는 스키마 실패가 아니다 (F3).

## 이 파일이 덮는 범위

F3 하나다. 여기서 고정하는 것은 **사전 등록 §3② 수치를 만드는 계산**뿐이고,
`run()` 의 보존소 순회 · `raw/` 선기록(D-052) · 원자적 쓰기 · CLI 는
**`test_export_replay_run.py`** 에 있다 (F5 의 잔여분, session-09). 목이 달라서
갈랐다 — 이쪽은 행 딕셔너리만 있으면 되고, 저쪽은 보존소와 transport 와
파일시스템이 다 필요하다.

## 왜 이 계산만 먼저인가

이 수치는 "모델을 바꿔도 되는가"의 근거로 쓰인다. 전송 실패를 스키마 실패로 세면
**회선이 나쁜 날의 실행이 "모델이 계약을 못 지킨다"로 기록되고**, 그 기록은 나중에
구분할 방법이 없다 — 실패 사유가 이미 한 칸으로 합쳐진 뒤다.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from export.replay import failure_kind, replay_one, summarize_rows
from extraction.extractor import DEFAULT_PROMPT, load_prompt
from extraction.llm import SchemaMismatchError, StructuredResult, Usage


def _row(**overrides: Any) -> dict[str, Any]:
    row = {"doc_id": "d", "ok": True, "failure": None, "attempts": 1}
    row.update(overrides)
    return row


SCHEMA_ROW = _row(ok=False, failure="SchemaMismatchError", attempts=3)
TRANSPORT_ROW = _row(ok=False, failure="URLError")


class TestFailureKind:
    def test_a_successful_row_has_no_failure(self):
        assert failure_kind(_row()) is None

    def test_only_a_schema_mismatch_counts_as_a_schema_failure(self):
        assert failure_kind(SCHEMA_ROW) == "schema"

    @pytest.mark.parametrize(
        "failure", ["URLError", "TimeoutError", "ExternalVendorCallError", "KeyError", None]
    )
    def test_everything_else_is_a_transport_failure(self, failure):
        """**모르는 실패는 전송 쪽으로 넘긴다.** 스키마 실패율은 과대 계상되면 안 된다."""
        assert failure_kind(_row(ok=False, failure=failure)) == "transport"


class TestSummarizeRows:
    def test_transport_failures_are_counted_separately(self):
        summary = summarize_rows([_row(), SCHEMA_ROW, TRANSPORT_ROW])
        assert summary["item_count"] == 3
        assert summary["schema_failures"] == 1
        assert summary["transport_failures"] == 1

    def test_the_rate_numerator_is_schema_failures_only(self):
        """전송 실패가 분자에 들어가면 회선 장애가 모델 품질로 기록된다."""
        summary = summarize_rows([_row(), SCHEMA_ROW, TRANSPORT_ROW, TRANSPORT_ROW])
        # 분모는 2건(성공 1 + 스키마 실패 1). 4건으로 나누면 0.25 가 나온다.
        assert summary["measured_items"] == 2
        assert summary["schema_failure_rate"] == 0.5

    def test_a_bad_line_does_not_make_the_model_look_better(self):
        """전송 실패를 분모에 남기면 **회선이 나쁜 날일수록 실패율이 낮게** 나온다."""
        clean = summarize_rows([_row(), SCHEMA_ROW])
        noisy = summarize_rows([_row(), SCHEMA_ROW] + [TRANSPORT_ROW] * 8)
        assert noisy["schema_failure_rate"] == clean["schema_failure_rate"] == 0.5

    def test_an_all_transport_run_measured_nothing(self):
        """0.0 으로 적으면 "한 건도 안 틀렸다"가 된다 — 한 건도 **못 물어봤다** (D-050)."""
        summary = summarize_rows([TRANSPORT_ROW, TRANSPORT_ROW])
        assert summary["measured_items"] == 0
        assert summary["schema_failure_rate"] is None
        assert summary["transport_failures"] == 2

    def test_an_empty_run_has_no_rate(self):
        assert summarize_rows([])["schema_failure_rate"] is None

    def test_the_denominator_is_reported_next_to_the_rate(self):
        """몇 건을 재고 나온 비율인지 없으면 0.5 가 2건인지 200건인지 알 수 없다."""
        assert "measured_items" in summarize_rows([_row()])

    def test_retries_and_truncation_are_still_counted(self):
        rows = [
            _row(attempts=2),
            _row(finish_reasons=["length"]),
            TRANSPORT_ROW,  # attempts 도 finish_reasons 도 없다
        ]
        summary = summarize_rows(rows)
        assert summary["retried"] == 1
        assert summary["truncated"] == 1


# ---------------------------------------------------------------------------
# `replay_one` 이 적는 이름과 `failure_kind` 가 읽는 이름이 같은가
# ---------------------------------------------------------------------------
class _Client:
    """`parse_into` 하나만 있는 최소 대역. **네트워크로 나가지 않는다.**"""

    model = "gemma-4-31B-it"
    last_raw_responses: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, outcome):
        self._outcome = outcome

    def parse_into(self, *, system, user, output_model):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


@pytest.fixture
def stored(make_payload):
    return make_payload(doc_id="arXiv_2412.05449v1", url="https://arxiv.org/abs/2412.05449v1")


class TestReplayOneWritesTheNameTheSummaryReads:
    """둘이 문자열 하나로 이어져 있다 — **한쪽만 바꾸면 비율이 조용히 틀린다.**"""

    def test_a_schema_mismatch_is_classified_as_schema(self, stored, tmp_path):
        client = _Client(SchemaMismatchError("파싱 실패", attempts=3, last_error="bad json"))
        row = replay_one(stored, client, load_prompt(DEFAULT_PROMPT), tmp_path)
        assert row["ok"] is False
        assert failure_kind(row) == "schema"
        assert row["attempts"] == 3

    def test_a_transport_error_is_classified_as_transport(self, stored, tmp_path):
        client = _Client(OSError("연결이 끊겼다"))
        row = replay_one(stored, client, load_prompt(DEFAULT_PROMPT), tmp_path)
        assert row["ok"] is False
        assert failure_kind(row) == "transport"

    def test_a_success_is_not_a_failure(self, stored, tmp_path):
        from extraction.schema import NewsOntology

        ontology = NewsOntology.model_validate(stored["extraction"]["ontology"])
        client = _Client(StructuredResult(value=ontology, usage=Usage(), attempts=1))
        row = replay_one(stored, client, load_prompt(DEFAULT_PROMPT), tmp_path)
        assert row["ok"] is True
        assert failure_kind(row) is None
