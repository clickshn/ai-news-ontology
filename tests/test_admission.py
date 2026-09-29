"""수용 창 · 미확정 발행일 · 갱신 멈춤 (ADR-023) — 파이프라인과 분리된 판정 단위."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from pipeline.admission import (
    IN_WINDOW,
    NO_DRAINED_RUN,
    OUT_OF_WINDOW,
    UNDATED_ADMITTED,
    UNDATED_DEFERRED,
    Admission,
    WindowPolicy,
    check_silence,
    last_drained,
    source_window,
    window_policy,
)

TODAY = date(2026, 9, 29)
POLICY = WindowPolicy(lookback_days=7, overlap_days=1, max_lookback_days=14, undated_max_per_run=5)


def _days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


class TestWindowSpan:
    def test_first_run_uses_lookback_not_the_cap(self):
        """첫 실행이 상한(14)까지 가면 아카이브형 피드가 그만큼 게이트로 간다."""
        window = source_window(POLICY, today=TODAY, anchor=None)
        assert window.span_days == 7 and window.cutoff == _days_ago(7) and not window.capped

    def test_daily_runs_keep_the_minimum_window(self):
        window = source_window(POLICY, today=TODAY, anchor=_days_ago(1))
        assert window.span_days == 7

    def test_a_long_pause_widens_the_window_past_the_anchor(self):
        """7일 창에서 10일 쉬면 중간 3일이 영영 빠지던 문제."""
        window = source_window(POLICY, today=TODAY, anchor=_days_ago(10))
        assert window.span_days == 11  # 10 + overlap 1
        assert window.cutoff == _days_ago(11)

    def test_the_cap_holds_and_says_so(self):
        window = source_window(POLICY, today=TODAY, anchor=_days_ago(30))
        assert window.span_days == 14 and window.capped

    def test_history_without_a_drained_run_goes_to_the_cap(self):
        window = source_window(POLICY, today=TODAY, anchor=NO_DRAINED_RUN)
        assert window.span_days == 14 and window.capped
        assert window.summary()["anchor"] == "none_drained"


class TestAdmission:
    def test_window_boundary_is_inclusive(self):
        admission = Admission(POLICY, source_window(POLICY, today=TODAY, anchor=None))
        assert admission.check(_days_ago(7)) == IN_WINDOW
        assert admission.check(_days_ago(8)) == OUT_OF_WINDOW

    def test_undated_items_are_admitted_up_to_the_cap_in_feed_order(self):
        admission = Admission(POLICY, source_window(POLICY, today=TODAY, anchor=None))
        verdicts = [admission.check(None) for _ in range(7)]
        assert verdicts == [UNDATED_ADMITTED] * 5 + [UNDATED_DEFERRED] * 2

    def test_future_dates_are_treated_as_undated(self):
        """내일까지는 시간대 차이로 정상이다. 그 뒤는 믿을 수 없다."""
        admission = Admission(POLICY, source_window(POLICY, today=TODAY, anchor=None))
        assert admission.check(TODAY + timedelta(days=1)) == IN_WINDOW
        assert admission.check(TODAY + timedelta(days=2)) == UNDATED_ADMITTED


class TestLastDrained:
    def _run(self, runs, name: str, source: str, day: date, drained: bool):
        runs.mkdir(exist_ok=True)
        record = {"window": {source: {"today": day.isoformat(), "drained": drained}}}
        (runs / f"pipeline-{name}.json").write_text(json.dumps(record), encoding="utf-8")

    def test_no_history_is_a_first_run(self, tmp_path):
        assert last_drained(tmp_path / "runs", "A", today=TODAY, policy=POLICY) is None

    def test_newest_drained_run_of_that_source(self, tmp_path):
        runs = tmp_path / "runs"
        self._run(runs, "20260925-090000", "A", _days_ago(4), True)
        self._run(runs, "20260927-090000", "A", _days_ago(2), False)  # 미룬 항목이 있었다
        self._run(runs, "20260928-090000", "B", _days_ago(1), True)  # 다른 소스
        assert last_drained(runs, "A", today=TODAY, policy=POLICY) == _days_ago(4)

    def test_history_but_nothing_drained_within_the_cap(self, tmp_path):
        runs = tmp_path / "runs"
        self._run(runs, "20260928-090000", "A", _days_ago(1), False)
        assert last_drained(runs, "A", today=TODAY, policy=POLICY) == NO_DRAINED_RUN

    def test_a_broken_summary_does_not_block(self, tmp_path):
        runs = tmp_path / "runs"
        self._run(runs, "20260927-090000", "A", _days_ago(2), True)
        (runs / "pipeline-20260928-090000.json").write_text("{not json", encoding="utf-8")
        assert last_drained(runs, "A", today=TODAY, policy=POLICY) == _days_ago(2)


class TestWindowDrainedField:
    def test_window_drained_wins_over_drained(self, tmp_path):
        """추출 대기가 있으면 drained=false 지만 기준점은 간다 (D-111)."""
        runs = tmp_path / "runs"
        runs.mkdir()
        record = {"window": {"A": {"today": _days_ago(2).isoformat(), "drained": False, "window_drained": True}}}
        (runs / "pipeline-20260927-090000.json").write_text(json.dumps(record), encoding="utf-8")
        assert last_drained(runs, "A", today=TODAY, policy=POLICY) == _days_ago(2)

    def test_contains_agrees_with_check(self):
        window = source_window(POLICY, today=TODAY, anchor=None)
        assert window.contains(_days_ago(7)) and not window.contains(_days_ago(8))
        assert window.contains(None) and window.contains(TODAY + timedelta(days=5))


class TestSilence:
    def test_old_newest_item_is_silent(self):
        """ZDNet Korea: 200, 30건, 최신 2024-05-10."""
        silence = check_silence([date(2024, 5, 10), date(2024, 5, 9)], today=TODAY, max_silence_days=3)
        assert silence == {"newest": "2024-05-10", "age_days": (TODAY - date(2024, 5, 10)).days, "max_silence_days": 3}

    def test_recent_item_is_fine_and_boundary_is_inclusive(self):
        assert check_silence([_days_ago(3)], today=TODAY, max_silence_days=3) is None
        assert check_silence([_days_ago(4)], today=TODAY, max_silence_days=3) is not None

    def test_no_threshold_or_no_dates_means_no_judgement(self):
        assert check_silence([date(2020, 1, 1)], today=TODAY, max_silence_days=None) is None
        assert check_silence([None, None], today=TODAY, max_silence_days=3) is None

    def test_future_dates_do_not_mask_silence(self):
        assert check_silence([date(2099, 1, 1), date(2024, 5, 10)], today=TODAY, max_silence_days=3) is not None


class TestPolicyFromConfig:
    def test_missing_section_turns_the_window_off(self):
        assert window_policy({}) is None

    def test_values_are_read(self):
        config = {"pipeline": {"window": {"lookback_days": 7, "overlap_days": 1, "max_lookback_days": 14, "undated_max_per_run": 5}}}
        assert window_policy(config) == POLICY

    def test_lookback_longer_than_cap_is_rejected(self):
        with pytest.raises(ValueError):
            WindowPolicy(lookback_days=15, overlap_days=1, max_lookback_days=14, undated_max_per_run=5)
