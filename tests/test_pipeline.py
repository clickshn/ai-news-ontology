"""파이프라인 오케스트레이션 — 중복 판정·재시도 지점·부분 실패 (ADR-022).

실제 API·피드·Vault 는 건드리지 않는다. 피드 수집은 소스별 목록으로, LLM 은 호출을
세는 목으로, Vault 는 `tmp_path` 로 갈아 끼운다. 이 파일이 고정하는 것은 **무엇이
몇 번 불리는가**와 **실패가 어디에 남고 다음 실행이 어디서 시작하는가**다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import pytest

from collectors.base import RawItem
from collectors.rss import FeedResult, FetchStatus
from export.doc_id import doc_id_for
from export.store import ExtractionStore
from extraction.egress import ExternalVendorCallError
from extraction.llm import SchemaMismatchError, StructuredResult, Usage
from extraction.schema import NewsOntology, RelevanceGate
from extraction.vllm import VLLMEndpointError
from obsidian_writer.writer import vault_index, write_note
from pipeline import runner as pipeline_runner
from pipeline.ledger import QUARANTINE_AFTER, Ledger
from pipeline.runner import (
    BREAKER_THRESHOLD,
    GATE_SKIPPED,
    QUARANTINED,
    STORED_OUTSIDE,
    Limits,
    RunReport,
    load_pending,
    new_report,
    run_pipeline,
)

GEEK = "GeekNews"
OPENAI = "OpenAI News"


def _item(n: int, source: str = GEEK, *, title: str | None = None) -> RawItem:
    return RawItem(
        url=f"https://example.com/{source.split()[0].lower()}/{n}?utm_source=rss",
        title=title or f"{source} 기사 {n}",
        body="본문",
        source_name=source,
        published_at=date(2026, 9, 28),
        collected_at=date(2026, 9, 29),
    )


def _ontology() -> NewsOntology:
    return NewsOntology(
        요약="테스트용 요약 문장이다. 두 번째 문장도 있다.",
        기술영역=["Agent"],
        발표유형="Paper",
        관련기업=[],
        관련기존기술=["LLM Agent"],
        영향도={"점수": 3, "근거": "테스트를 위한 근거 문장이다."},
    )


class ScriptedClient:
    """호출을 세고, 제목별로 결과·예외를 지정할 수 있는 LLM 목.

    `behaviour(title)` 가 예외를 돌려주면 그것을 올리고, bool 이면 게이트 판정,
    None 이면 기본(게이트 통과 / 정상 추출)이다.
    """

    def __init__(self, model: str, behaviour: Callable[[str], object] | None = None):
        self.model = model
        self.behaviour = behaviour or (lambda title: None)
        self.calls: list[str] = []

    def parse_into(self, *, system: str, user: str, output_model):
        title = next((line for line in user.splitlines() if "기사" in line), user[:40])
        self.calls.append(title)
        outcome = self.behaviour(title)
        if isinstance(outcome, BaseException):
            raise outcome
        if output_model is RelevanceGate:
            relevant = True if outcome is None else bool(outcome)
            value = RelevanceGate(관련있음=relevant, 근거="테스트 판정 근거 문장이다.")
        else:
            value = _ontology()
        return StructuredResult(value=value, usage=Usage(model=self.model), attempts=1)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """피드·클라이언트·경로를 한데 묶는다. `feeds` 를 바꿔 가며 실행을 되풀이한다."""
    feeds: dict[str, object] = {GEEK: [_item(1), _item(2), _item(3)], OPENAI: [_item(1, OPENAI)]}

    def fake_collect(config, *, source_name=None, **kwargs):
        # 목록이면 정상 수집, FeedResult 면 그대로(실패 사유 지정), 예외면 올린다.
        feed = feeds.get(source_name, [])
        if isinstance(feed, BaseException):
            raise feed
        if isinstance(feed, FeedResult):
            return [feed]
        status = FetchStatus.OK if feed else FetchStatus.EMPTY
        return [FeedResult(source_name=source_name, status=status, items=tuple(feed))]

    monkeypatch.setattr(pipeline_runner, "collect_feed_results", fake_collect)

    state = {
        "feeds": feeds,
        "gate": ScriptedClient("gemma-4-31B-it"),
        "extraction": ScriptedClient("gemma-4-31B-it"),
        "ledger": Ledger(tmp_path / "ledger"),
        "store": ExtractionStore(tmp_path / "store"),
        "vault": tmp_path / "vault",
        "runs": tmp_path / "runs",
    }
    state["config"] = {
        "version": 1,
        "sources": {"rss": [{"name": GEEK, "url": "x"}, {"name": OPENAI, "url": "y"}]},
        "llm": {
            "provider": "vllm",
            "relevance_gate": {"model": "gemma-4-31B-it"},
            "extraction": {"model": "gemma-4-31B-it"},
        },
        "output": {"on_conflict": "skip"},
    }

    def factory(config, stage="extraction", **kw):
        return state["gate"] if stage == "relevance_gate" else state["extraction"]

    def run(gate=None, extract=None, **kwargs) -> RunReport:
        limits = Limits(
            gate=gate if gate is not None else {GEEK: 10, OPENAI: 10},
            extract=extract if extract is not None else {GEEK: 10, OPENAI: 10},
        )
        return run_pipeline(
            state["config"],
            limits=limits,
            ledger=state["ledger"],
            store=state["store"],
            output_dir=state["vault"],
            runs_dir=state["runs"],
            client_factory=factory,
            **kwargs,
        )

    state["run"] = run
    return state


def _notes(vault) -> list[str]:
    return sorted(p.name for p in vault.glob("*.md")) if vault.exists() else []


# ---------------------------------------------------------------------------
# 정상 경로
# ---------------------------------------------------------------------------
class TestEndToEnd:
    def test_one_command_flows_from_collection_to_vault_note(self, env):
        report = env["run"]()

        assert len(_notes(env["vault"])) == 4
        assert report.load["written"] == 4
        assert report.exit_code() == 0
        assert len(env["gate"].calls) == 4 and len(env["extraction"].calls) == 4

    def test_note_date_comes_from_store_not_from_now(self, env):
        env["run"]()
        doc_id = doc_id_for(str(_item(1).url))
        extracted_at = env["store"].load(doc_id).extraction["extracted_at"]
        entry = env["ledger"].get(doc_id)
        note = env["vault"] / entry.load["note"]
        assert note.name.startswith(extracted_at[:10])
        assert f"processed_at: '{extracted_at}'" in note.read_text(encoding="utf-8")

    def test_run_summary_is_written(self, env):
        report = env["run"]()
        saved = json.loads((env["runs"] / f"{report.run_id}.json").read_text(encoding="utf-8"))
        assert saved["load"]["written"] == 4
        assert saved["exit_code"] == 0

    def test_load_stage_never_builds_an_llm_client(self, env):
        env["run"]()
        env["gate"].calls.clear()
        env["extraction"].calls.clear()
        report = new_report()
        load_pending(
            env["config"], ledger=env["ledger"], store=env["store"],
            output_dir=env["vault"], report=report,
        )
        assert env["gate"].calls == [] and env["extraction"].calls == []


# ---------------------------------------------------------------------------
# 중복 판정 (결정 1)
# ---------------------------------------------------------------------------
class TestDedup:
    def test_rerun_calls_nothing_and_writes_nothing(self, env):
        env["run"]()
        env["gate"].calls.clear()
        env["extraction"].calls.clear()

        report = env["run"]()

        assert env["gate"].calls == [] and env["extraction"].calls == []
        assert report.load["written"] == 0 and report.load["exists"] == 4
        assert len(_notes(env["vault"])) == 4

    def test_same_article_with_different_tracking_params_is_one_doc(self, env):
        twin = _item(1).model_copy(update={"url": "https://example.com/geeknews/1?utm_medium=x"})
        env["feeds"][GEEK] = [_item(1), twin]
        report = env["run"](gate={GEEK: 5}, extract={GEEK: 5})
        assert len(env["extraction"].calls) == 1
        assert report.sources[GEEK]["duplicate_in_run"] == 1

    def test_existing_note_on_another_date_is_recognised(self, env):
        """파일명 날짜가 달라도 같은 기사다 — `write_note` 만으로는 못 잡던 경우."""
        env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        (note,) = env["vault"].glob("*.md")
        note.rename(note.with_name("1999-01-01-" + note.name.split("-", 3)[3]))
        entry = env["ledger"].get(doc_id_for(str(_item(1).url)))
        entry.load = None  # 원장 기록이 없는 상태로 되돌려 색인만으로 판정하게 한다
        env["ledger"].put(entry)

        report = env["run"](gate={GEEK: 0}, extract={GEEK: 0})

        assert report.load["exists"] == 1 and report.load["written"] == 0
        assert len(_notes(env["vault"])) == 1

    def test_note_deleted_by_user_is_not_recreated(self, env):
        env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        (note,) = env["vault"].glob("*.md")
        note.unlink()

        report = env["run"](gate={GEEK: 0}, extract={GEEK: 0})
        assert _notes(env["vault"]) == []
        assert report.load["deleted_by_user"] == 1
        assert env["ledger"].get(doc_id_for(str(_item(1).url))).load["status"] == "deleted_by_user"

        report = env["run"](gate={GEEK: 0}, extract={GEEK: 0}, rewrite_missing=True)
        assert report.load["written"] == 1 and len(_notes(env["vault"])) == 1

    def test_store_items_outside_the_pipeline_are_neither_gated_nor_loaded(self, env, make_payload):
        """session 0.5a 의 30건처럼 원장 없이 보존소에만 있는 것 (사용자 결정)."""
        doc_id = doc_id_for(str(_item(1).url))
        payload = make_payload(doc_id=doc_id, url=str(_item(1).url))
        env["store"].save(payload)
        env["feeds"][GEEK] = [_item(1)]

        report = env["run"](gate={GEEK: 5}, extract={GEEK: 5})

        assert env["gate"].calls == [] and env["extraction"].calls == []
        assert report.sources[GEEK][STORED_OUTSIDE] == 1
        assert _notes(env["vault"]) == []

    def test_gate_skip_is_not_regated_until_the_gate_changes(self, env):
        env["gate"].behaviour = lambda title: False
        env["run"](gate={GEEK: 5}, extract={GEEK: 5})
        assert len(env["gate"].calls) == 3
        env["gate"].calls.clear()

        report = env["run"](gate={GEEK: 5}, extract={GEEK: 5})
        assert env["gate"].calls == []
        assert report.sources[GEEK][GATE_SKIPPED] == 3

        env["config"]["llm"]["relevance_gate"]["model"] = "other-model"
        env["run"](gate={GEEK: 5}, extract={GEEK: 5})
        assert len(env["gate"].calls) == 3


# ---------------------------------------------------------------------------
# 상한
# ---------------------------------------------------------------------------
class TestLimits:
    def test_per_source_and_per_stage_limits_hold(self, env):
        report = env["run"](gate={GEEK: 2, OPENAI: 1}, extract={GEEK: 1, OPENAI: 0})

        assert report.sources[GEEK]["gate_calls"] == 2
        assert report.sources[GEEK]["extract_calls"] == 1
        assert report.sources[GEEK]["deferred_gate"] == 1
        assert report.sources[GEEK]["deferred_extraction"] == 1
        assert report.sources[OPENAI]["extract_calls"] == 0
        assert len(env["gate"].calls) == 3 and len(env["extraction"].calls) == 1

    def test_global_ceiling_caps_the_sum_of_source_limits(self, env):
        limits = Limits(gate={GEEK: 10, OPENAI: 10}, extract={GEEK: 10, OPENAI: 10},
                        max_gate_calls=2, max_extractions=1)
        run_pipeline(
            env["config"], limits=limits, ledger=env["ledger"], store=env["store"],
            output_dir=env["vault"], runs_dir=env["runs"],
            client_factory=lambda c, stage="extraction", **k: env["gate"] if stage == "relevance_gate" else env["extraction"],
        )
        assert len(env["gate"].calls) == 2 and len(env["extraction"].calls) == 1

    def test_global_ceiling_defaults_to_the_sum_of_source_limits(self):
        """승인받는 숫자(소스별 합)가 코드가 막는 숫자다 — session-15 의 9 → 12 건."""
        limits = Limits(gate={GEEK: 3, OPENAI: 2}, extract={GEEK: 1})
        assert (limits.max_gate_calls, limits.max_extractions) == (5, 1)
        assert Limits(gate={GEEK: 999}, extract={}).max_gate_calls == pipeline_runner.HARD_MAX_GATE_CALLS

    def test_sources_without_a_limit_are_not_collected(self, env):
        report = env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        assert OPENAI not in report.sources

    def test_unknown_source_name_is_rejected_before_any_call(self, env):
        with pytest.raises(ValueError):
            env["run"](gate={"Nope": 1}, extract={})
        assert env["gate"].calls == []

    def test_deferred_gate_pass_resumes_at_extraction_next_run(self, env):
        """게이트는 통과했는데 추출 상한에 걸린 항목 — 다음 실행은 추출부터."""
        env["feeds"][GEEK] = [_item(1)]
        env["run"](gate={GEEK: 1}, extract={GEEK: 0})
        env["gate"].calls.clear()

        env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        assert env["gate"].calls == [] and len(env["extraction"].calls) == 1


# ---------------------------------------------------------------------------
# 부분 실패와 재시도 지점 (결정 2)
# ---------------------------------------------------------------------------
class TestPartialFailure:
    def test_a_dead_source_does_not_stop_the_others(self, env):
        env["feeds"][GEEK] = OSError("feed down")

        report = env["run"]()

        assert report.sources[GEEK]["source_error"] == 1
        assert report.sources[OPENAI]["extracted"] == 1
        assert len(_notes(env["vault"])) == 1
        assert report.exit_code() == 3

    def test_gate_error_is_recorded_and_retried_from_the_gate(self, env):
        env["feeds"][GEEK] = [_item(1)]
        env["gate"].behaviour = lambda title: VLLMEndpointError("HTTP 503")
        report = env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        entry = env["ledger"].get(doc_id_for(str(_item(1).url)))
        assert report.sources[GEEK]["gate_error"] == 1
        assert entry.gate is None and "503" in entry.gate_error
        assert env["extraction"].calls == []

        env["gate"].behaviour = lambda title: None
        report = env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        assert report.sources[GEEK]["extracted"] == 1

    def test_schema_failures_quarantine_after_the_threshold(self, env):
        env["feeds"][GEEK] = [_item(1)]
        env["extraction"].behaviour = lambda title: SchemaMismatchError("bad", attempts=2, last_error="x")
        doc_id = doc_id_for(str(_item(1).url))

        for _ in range(QUARANTINE_AFTER):
            env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        assert env["ledger"].get(doc_id).quarantined
        assert len(env["extraction"].calls) == QUARANTINE_AFTER
        assert len(env["gate"].calls) == 1  # 게이트 판정은 원장에서 재사용된다

        report = env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        assert len(env["extraction"].calls) == QUARANTINE_AFTER
        assert report.sources[GEEK][QUARANTINED] == 1

    def test_release_lifts_quarantine(self, env):
        env["feeds"][GEEK] = [_item(1)]
        env["extraction"].behaviour = lambda title: SchemaMismatchError("bad", attempts=2)
        for _ in range(QUARANTINE_AFTER):
            env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        doc_id = doc_id_for(str(_item(1).url))

        pipeline_runner.main(["release", doc_id, "--ledger-dir", str(env["ledger"].directory)])
        env["extraction"].behaviour = lambda title: None
        report = env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        assert report.sources[GEEK]["extracted"] == 1

    def test_transport_errors_do_not_count_toward_quarantine(self, env):
        env["feeds"][GEEK] = [_item(1)]
        env["extraction"].behaviour = lambda title: VLLMEndpointError("URLError: refused")
        for _ in range(QUARANTINE_AFTER + 1):
            env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        entry = env["ledger"].get(doc_id_for(str(_item(1).url)))
        assert entry.extract_failures == 0 and not entry.quarantined
        assert "refused" in entry.extract_error

    def test_breaker_stops_the_stage_after_consecutive_transport_errors(self, env):
        env["feeds"][GEEK] = [_item(n) for n in range(1, BREAKER_THRESHOLD + 3)]
        env["extraction"].behaviour = lambda title: VLLMEndpointError("timeout")

        report = env["run"](gate={GEEK: 10}, extract={GEEK: 10})

        assert len(env["extraction"].calls) == BREAKER_THRESHOLD
        assert report.breaker_tripped == ["extraction"]
        assert report.sources[GEEK]["deferred_extraction"] == 2

    def test_breaker_does_not_block_loading_already_stored_items(self, env):
        env["feeds"][GEEK] = [_item(1)]
        env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        (env["vault"] / env["ledger"].get(doc_id_for(str(_item(1).url))).load["note"]).unlink()
        entry = env["ledger"].get(doc_id_for(str(_item(1).url)))
        entry.load = {"status": "failed", "error": "x"}
        env["ledger"].put(entry)

        env["feeds"][GEEK] = [_item(n) for n in range(2, 2 + BREAKER_THRESHOLD)]
        env["extraction"].behaviour = lambda title: VLLMEndpointError("timeout")
        report = env["run"](gate={GEEK: 10}, extract={GEEK: 10})

        assert report.breaker_tripped == ["extraction"]
        assert report.load["written"] == 1

    def test_load_failure_keeps_the_extraction_and_recovers_without_llm(self, env, monkeypatch):
        env["feeds"][GEEK] = [_item(1)]

        def broken(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(pipeline_runner, "write_note", broken)
        report = env["run"](gate={GEEK: 1}, extract={GEEK: 1})
        doc_id = doc_id_for(str(_item(1).url))
        assert report.load["failed"] == 1 and report.exit_code() == 3
        assert env["store"].path_for(doc_id).exists()
        assert env["ledger"].get(doc_id).load["status"] == "failed"

        monkeypatch.setattr(pipeline_runner, "write_note", write_note)
        env["gate"].calls.clear()
        env["extraction"].calls.clear()
        report = new_report()
        load_pending(env["config"], ledger=env["ledger"], store=env["store"],
                     output_dir=env["vault"], report=report)
        assert report.load["written"] == 1
        assert env["gate"].calls == [] and env["extraction"].calls == []

    def test_one_item_failure_does_not_stop_the_rest_of_the_source(self, env):
        env["extraction"].behaviour = (
            lambda title: SchemaMismatchError("bad", attempts=2) if "기사 2" in title else None
        )
        report = env["run"](gate={GEEK: 10}, extract={GEEK: 10})
        assert report.sources[GEEK]["extracted"] == 2
        assert report.sources[GEEK]["extract_schema_failed"] == 1

    def test_egress_block_is_not_swallowed_as_a_partial_failure(self, env):
        env["gate"].behaviour = lambda title: ExternalVendorCallError("blocked")
        with pytest.raises(ExternalVendorCallError):
            env["run"]()
        assert list(env["runs"].glob("*.json"))  # 요약은 남긴다

    def test_all_failures_and_nothing_produced_exits_1(self, env):
        env["feeds"][GEEK] = OSError("down")
        env["feeds"][OPENAI] = OSError("down")
        assert env["run"]().exit_code() == 1


# ---------------------------------------------------------------------------
# 수집 실패 vs 정상 0건 (D-103)
# ---------------------------------------------------------------------------
def _failed(status: FetchStatus, detail: str = "x") -> FeedResult:
    return FeedResult(source_name=GEEK, status=status, detail=detail)


class TestFetchFailureIsNotAnEmptyFeed:
    @pytest.mark.parametrize(
        "status",
        [FetchStatus.HTTP_ERROR, FetchStatus.PARSE_ERROR, FetchStatus.TIMEOUT, FetchStatus.NO_USABLE_ENTRIES],
    )
    def test_fetch_failure_counts_as_a_source_error(self, env, status):
        env["feeds"][GEEK] = _failed(status)

        report = env["run"]()

        assert report.sources[GEEK]["source_error"] == 1
        assert report.fetch[GEEK]["status"] == status.value
        assert report.exit_code() == 3  # OpenAI 는 흘렀다

    def test_empty_feed_is_not_a_failure(self, env):
        """arXiv 주말 0건. 예전에는 장애와 같은 모양이었다."""
        env["feeds"][GEEK] = []

        report = env["run"]()

        assert report.sources[GEEK]["source_error"] == 0
        assert report.fetch[GEEK]["status"] == "empty"
        assert report.exit_code() == 0

    def test_one_dead_source_on_a_quiet_day_is_partial_not_total(self, env):
        """새 글이 없는 날(산출 0) 소스 하나가 죽어도 1 이 아니다 — 다른 소스는 받았다."""
        env["run"]()  # 첫 실행에서 전부 적재
        env["feeds"][GEEK] = _failed(FetchStatus.HTTP_ERROR, "HTTP 503")

        report = env["run"]()

        assert report.produced == 0
        assert report.exit_code() == 3

    def test_nothing_produced_with_a_gate_failure_is_still_1(self, env):
        """완화는 수집 실패에만 적용된다. LLM 단계가 실패하고 산출 0이면 1 이다."""
        env["feeds"][GEEK] = [_item(1)]
        env["feeds"][OPENAI] = []
        env["gate"].behaviour = lambda title: VLLMEndpointError("down")

        assert env["run"]().exit_code() == 1

    def test_failure_reason_is_in_the_run_summary(self, env):
        env["feeds"][GEEK] = _failed(FetchStatus.PARSE_ERROR, "파싱 실패 (not well-formed)")

        report = env["run"]()

        saved = json.loads((env["runs"] / f"{report.run_id}.json").read_text(encoding="utf-8"))
        assert saved["fetch"][GEEK] == {"status": "parse_error", "items": 0, "detail": "파싱 실패 (not well-formed)"}
        assert saved["fetch"][OPENAI]["status"] == "ok"


# ---------------------------------------------------------------------------
# obsidian_writer 추가분
# ---------------------------------------------------------------------------
class TestVaultIndex:
    def test_index_ignores_notes_without_frontmatter(self, tmp_path):
        (tmp_path / "welcome.md").write_text("# hi\n", encoding="utf-8")
        assert vault_index(tmp_path, key=doc_id_for) == {}

    def test_index_keys_by_doc_id_across_filenames(self, tmp_path):
        (tmp_path / "2020-01-01-x.md").write_text(
            "---\nsource_url: https://example.com/a?utm_source=z\n---\n", encoding="utf-8"
        )
        index = vault_index(tmp_path, key=doc_id_for)
        assert list(index) == [doc_id_for("https://example.com/a")]

    def test_missing_directory_is_an_empty_index(self, tmp_path):
        assert vault_index(tmp_path / "none", key=doc_id_for) == {}


# ---------------------------------------------------------------------------
# 수용 창 · 갱신 멈춤 · config 상한 (ADR-023)
# ---------------------------------------------------------------------------
from datetime import timedelta  # noqa: E402

from pipeline.runner import limits_from_config, plan, resolve_limits  # noqa: E402

TODAY = date(2026, 9, 29)
WINDOW = {"lookback_days": 7, "overlap_days": 1, "max_lookback_days": 14, "undated_max_per_run": 1}


def _dated(n: int, days_ago: int | None, source: str = GEEK) -> RawItem:
    return RawItem(
        url=f"https://example.com/{source.split()[0].lower()}/d{n}",
        title=f"{source} 기사 d{n}",
        body="본문",
        source_name=source,
        published_at=None if days_ago is None else TODAY - timedelta(days=days_ago),
    )


@pytest.fixture
def windowed(env):
    env["config"]["pipeline"] = {"window": dict(WINDOW)}
    env["feeds"][OPENAI] = []
    return env


class TestAdmissionWindow:
    def test_only_items_inside_the_window_reach_the_gate(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 7), _dated(3, 8), _dated(4, 400)]

        report = windowed["run"](today=TODAY)

        tally = report.sources[GEEK]
        assert tally["gate_calls"] == 2 and tally["out_of_window"] == 2
        assert tally["collected"] == 4  # 수집한 것과 받아들인 것을 따로 남긴다
        assert report.window[GEEK]["cutoff"] == "2026-09-22" and report.window[GEEK]["drained"]

    def test_undated_items_are_capped_per_run(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, None), _dated(2, None), _dated(3, None)]

        report = windowed["run"](today=TODAY)

        assert report.sources[GEEK]["undated_admitted"] == 1
        assert report.sources[GEEK]["undated_deferred"] == 2
        assert not report.window[GEEK]["drained"]

    def test_items_already_in_the_ledger_ignore_the_window(self, windowed):
        """게이트 오류로 재시도를 기다리던 항목이 날짜가 지났다고 버려지면 안 된다."""
        old = _dated(1, 6)
        windowed["feeds"][GEEK] = [old]
        windowed["gate"].behaviour = lambda title: VLLMEndpointError("down")
        windowed["run"](today=TODAY)
        windowed["gate"].behaviour = lambda title: None

        report = windowed["run"](today=TODAY + timedelta(days=5))  # 이제 11일 전 항목

        assert report.sources[GEEK]["out_of_window"] == 0
        assert report.sources[GEEK]["gate_calls"] == 1

    def test_a_pause_widens_the_window_from_the_last_drained_run(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0)]
        windowed["run"](today=TODAY)  # 완결 실행 = 09-29

        later = TODAY + timedelta(days=10)
        # 쉬는 동안(09-29) 발행된 항목 — later 기준 10일 전이라 최소 창(7일)이면 빠진다
        windowed["feeds"][GEEK] = [_dated(2, 0)]
        report = windowed["run"](today=later)

        assert report.window[GEEK]["span_days"] == 11
        assert report.sources[GEEK]["gate_calls"] == 1

    def test_deferred_items_keep_the_anchor_from_moving(self, windowed):
        """상한 때문에 미룬 항목이 있으면 그 실행은 완결이 아니다 — 기준점이 그대로다."""
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        windowed["run"](gate={GEEK: 1}, extract={GEEK: 1}, today=TODAY)

        report = windowed["run"](gate={GEEK: 1}, extract={GEEK: 1}, today=TODAY + timedelta(days=9))

        assert report.window[GEEK]["anchor"] == "none_drained"
        assert report.window[GEEK]["capped"]

    def test_no_window_section_means_everything_is_admitted(self, env):
        env["feeds"][GEEK] = [_dated(1, 400)]
        report = env["run"](today=TODAY)
        assert report.sources[GEEK]["gate_calls"] == 1 and report.window == {}

    def test_plan_counts_the_window_the_same_way(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 30)]
        limits = Limits(gate={GEEK: 5}, extract={GEEK: 5})

        result = plan(
            windowed["config"], limits=limits, ledger=windowed["ledger"], store=windowed["store"],
            runs_dir=windowed["runs"], today=TODAY,
        )

        assert result[GEEK]["states"] == {"needs_gate": 1, "out_of_window": 1}
        assert result[GEEK]["gate_calls_max"] == 1


class TestSilentFeed:
    def test_a_feed_that_stopped_updating_is_a_source_failure(self, env):
        """ZDNet Korea: 200 을 주면서 2024-05-10 에 멈춰 있었다."""
        env["config"]["sources"]["rss"][0]["max_silence_days"] = 3
        env["feeds"][GEEK] = [_dated(1, 20)]

        report = env["run"](today=TODAY)

        assert report.sources[GEEK]["stale_feed"] == 1
        assert report.fetch[GEEK]["silent"]["age_days"] == 20
        assert report.exit_code() == 3  # OpenAI 는 흘렀다

    def test_all_sources_silent_and_nothing_produced_exits_1(self, env):
        for source in env["config"]["sources"]["rss"]:
            source["max_silence_days"] = 3
        env["feeds"][GEEK] = [_dated(1, 20)]
        env["feeds"][OPENAI] = [_dated(1, 20, OPENAI)]
        env["config"]["pipeline"] = {"window": dict(WINDOW)}  # 오래된 항목은 창이 거른다

        report = env["run"](today=TODAY)

        assert report.produced == 0
        assert report.exit_code() == 1

    def test_recent_feed_is_not_silent(self, env):
        env["config"]["sources"]["rss"][0]["max_silence_days"] = 3
        env["feeds"][GEEK] = [_dated(1, 1)]
        assert env["run"](today=TODAY).sources[GEEK]["stale_feed"] == 0


class TestConfigLimits:
    CONFIG = {
        "sources": {
            "rss": [
                {"name": GEEK, "url": "x", "limits": {"gate": 40, "extract": 15}},
                {"name": OPENAI, "url": "y", "limits": {"gate": 10, "extract": 5}},
                {"name": "No Limits", "url": "z"},
            ]
        }
    }

    def test_config_limits_are_used_without_cli_arguments(self):
        limits, mode = resolve_limits(self.CONFIG, [], [])
        assert mode == "config"
        assert limits.gate == {GEEK: 40, OPENAI: 10} and limits.extract == {GEEK: 15, OPENAI: 5}
        assert limits.sources == [GEEK, OPENAI]  # 상한 없는 소스는 돌지 않는다
        assert limits.max_gate_calls == 50  # 전역 천장 기본값은 여전히 소스별 합 (D-102)

    def test_any_cli_limit_replaces_the_config_entirely(self):
        """병합하면 "이 소스 3건"을 승인받은 실행이 나머지 소스를 config 값으로 함께 돈다."""
        limits, mode = resolve_limits(self.CONFIG, [f"{GEEK}=3"], [])
        assert mode == "cli"
        assert limits.gate == {GEEK: 3} and limits.extract == {}
        assert limits.sources == [GEEK]
        assert limits.max_gate_calls == 3 and limits.max_extractions == 0

    def test_limits_from_config_matches_the_shipped_config(self):
        from collectors.rss import load_config

        limits = limits_from_config(load_config())
        assert limits.max_gate_calls == sum(limits.gate.values())
        assert limits.max_extractions == sum(limits.extract.values())


# ---------------------------------------------------------------------------
# 미룬 항목의 행방 · 완결의 두 뜻 (D-111)
# ---------------------------------------------------------------------------
from pipeline import alerts as A  # noqa: E402


def _report(n: int) -> RunReport:
    """실행 요약 파일명이 초 단위라, 한 테스트 안의 실행이 서로를 덮지 않게 한다."""
    return RunReport(run_id=f"pipeline-20260929-0900{n:02d}", started_at="2026-09-29T09:00:00+09:00")


class TestEvictions:
    def test_extraction_backlog_that_leaves_the_feed_is_counted_as_lost(self, windowed):
        """arXiv: 20건 통과에 추출 10 — 나머지는 다음 날 피드에 없다. 그래도 완결로 보였다."""
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0), _dated(3, 0)]
        first = windowed["run"](gate={GEEK: 10}, extract={GEEK: 1}, today=TODAY, report=_report(1))
        assert first.sources[GEEK]["deferred_extraction"] == 2

        windowed["feeds"][GEEK] = [_dated(4, 0)]  # 1~3 은 피드에서 밀려났다
        report = windowed["run"](gate={GEEK: 10}, extract={GEEK: 10}, today=TODAY, report=_report(2))

        assert report.sources[GEEK]["evicted_extraction"] == 2
        kinds = {(e["kind"], e["source"]) for e in A.events_from_report(report.to_dict())}
        assert (A.EVICTED, GEEK) in kinds
        assert A.severity(A.EVICTED, 1) == A.WARNING  # 미룬 것이 아니라 이미 잃은 것

    def test_gate_backlog_that_leaves_the_feed_is_counted_as_lost(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        windowed["run"](gate={GEEK: 1}, extract={GEEK: 10}, today=TODAY, report=_report(1))

        windowed["feeds"][GEEK] = [_dated(3, 0)]
        report = windowed["run"](gate={GEEK: 10}, extract={GEEK: 10}, today=TODAY, report=_report(2))

        assert report.sources[GEEK]["evicted_gate"] == 1
        assert report.sources[GEEK]["evicted_extraction"] == 0

    def test_backlog_still_in_the_feed_is_picked_up_not_lost(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        windowed["run"](gate={GEEK: 1}, extract={GEEK: 1}, today=TODAY, report=_report(1))

        report = windowed["run"](gate={GEEK: 10}, extract={GEEK: 10}, today=TODAY, report=_report(2))

        tally = report.sources[GEEK]
        assert tally["evicted_gate"] == 0 and tally["evicted_extraction"] == 0
        assert tally["extracted"] == 1

    def test_a_loss_is_counted_once(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        windowed["run"](gate={GEEK: 10}, extract={GEEK: 1}, today=TODAY, report=_report(1))
        windowed["feeds"][GEEK] = [_dated(3, 0)]
        windowed["run"](today=TODAY, report=_report(2))

        report = windowed["run"](today=TODAY, report=_report(3))

        assert report.sources[GEEK]["evicted_extraction"] == 0

    def test_a_failed_fetch_in_between_compares_with_the_last_record(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        windowed["run"](gate={GEEK: 10}, extract={GEEK: 1}, today=TODAY, report=_report(1))
        windowed["feeds"][GEEK] = _failed(FetchStatus.NETWORK_ERROR)
        windowed["run"](today=TODAY, report=_report(2))

        windowed["feeds"][GEEK] = [_dated(3, 0)]
        report = windowed["run"](today=TODAY, report=_report(3))

        assert report.sources[GEEK]["evicted_extraction"] == 1

    def test_feed_rollover_is_flagged_without_a_count(self, windowed):
        """직전 맨 앞 항목이 하나도 없다 = 피드 깊이보다 많이 들어왔다. 못 본 것은 셀 수 없다."""
        windowed["feeds"][GEEK] = [_dated(1, 0)]
        windowed["run"](today=TODAY, report=_report(1))

        windowed["feeds"][GEEK] = [_dated(9, 0)]
        report = windowed["run"](today=TODAY, report=_report(2))

        assert report.sources[GEEK]["feed_rollover"] == 1
        assert report.sources[GEEK]["evicted_gate"] == 0
        kinds = {e["kind"] for e in A.events_from_report(report.to_dict())}
        assert A.FEED_ROLLOVER in kinds

    def test_one_head_item_surviving_is_not_a_rollover(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        windowed["run"](today=TODAY, report=_report(1))

        windowed["feeds"][GEEK] = [_dated(3, 0), _dated(2, 0)]
        report = windowed["run"](today=TODAY, report=_report(2))

        assert report.sources[GEEK]["feed_rollover"] == 0

    def test_first_record_has_nothing_to_compare(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0)]
        report = windowed["run"](today=TODAY, report=_report(1))
        assert report.backlog[GEEK] == {"head": [doc_id_for(str(_dated(1, 0).url))], "gate": [], "extraction": []}
        assert report.sources[GEEK]["feed_rollover"] == 0


class TestDrainedMeansNothingDeferred:
    def test_extraction_backlog_is_not_drained_but_the_window_anchor_moves(self, windowed):
        """완결(경보)은 추출 대기까지 보고, 창 기준점은 게이트 쪽만 본다.

        기준점까지 추출 대기에 묶으면 추출 상한이 모자란 소스는 창이 늘 14일 상한에 머물고
        `window_capped`(심각)이 상시 뜬다 — 경보가 정상 상태가 된다.
        """
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        report = windowed["run"](gate={GEEK: 10}, extract={GEEK: 1}, today=TODAY, report=_report(1))

        assert report.window[GEEK]["drained"] is False
        assert report.window[GEEK]["window_drained"] is True
        detail = next(e["detail"] for e in A.events_from_report(report.to_dict()) if e["kind"] == A.NOT_DRAINED)
        assert "추출 1" in detail and "기준점은 간다" in detail

        later = windowed["run"](
            gate={GEEK: 10}, extract={GEEK: 10}, today=TODAY + timedelta(days=9), report=_report(2)
        )
        assert later.window[GEEK]["anchor"] == TODAY.isoformat()
        assert not later.window[GEEK]["capped"]

    def test_gate_backlog_is_neither(self, windowed):
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        report = windowed["run"](gate={GEEK: 1}, extract={GEEK: 10}, today=TODAY, report=_report(1))
        assert report.window[GEEK]["drained"] is False and report.window[GEEK]["window_drained"] is False


class TestRunRecordsQualityBaseline:
    def test_manual_run_writes_quality_metrics(self, env):
        """기준선은 자동 실행을 켜기 전에 있어야 한다 — 수동 run 도 기록한다."""
        report = env["run"]()
        saved = json.loads((env["runs"] / f"{report.run_id}.json").read_text(encoding="utf-8"))
        assert saved["quality"][GEEK]["extracted"] == 3
        assert saved["alerts"] == []  # 경보 누적은 자동 실행의 것이다

    def test_body_length_is_measured_inside_the_window(self, windowed):
        """OpenAI 의 창 밖 1212건이 중앙값을 끌던 것."""
        long_new = _dated(1, 0).model_copy(update={"body": "x" * 500})
        old = [_dated(n, 300).model_copy(update={"body": ""}) for n in range(2, 6)]
        windowed["feeds"][GEEK] = [long_new, *old]

        report = windowed["run"](today=TODAY)

        assert report.inputs[GEEK] == {"items": 1, "body_chars_median": 500, "body_empty": 0, "feed_items": 5}

    def test_without_a_window_the_whole_feed_is_measured(self, env):
        report = env["run"]()
        assert report.inputs[GEEK]["items"] == report.inputs[GEEK]["feed_items"] == 3


# ---------------------------------------------------------------------------
# 창은 미룬 항목이 쓴 cutoff 를 지킨다 · 넓어진 것과 상한에 걸린 것은 다르다 (ADR-023 Amendment 2)
# ---------------------------------------------------------------------------
class TestCarriedCutoff:
    def test_one_undrained_run_does_not_jump_to_the_cap(self, windowed):
        """session-18 OpenAI: 미완결 1회로 창이 14일이 되고 window_capped(심각)이 떴다."""
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        windowed["run"](gate={GEEK: 1}, extract={GEEK: 10}, today=TODAY, report=_report(1))

        report = windowed["run"](gate={GEEK: 10}, extract={GEEK: 10}, today=TODAY + timedelta(days=1), report=_report(2))

        window = report.window[GEEK]
        assert window["cutoff"] == "2026-09-22" and window["span_days"] == 8
        assert not window["capped"]
        assert window["carry_cutoff"] == "2026-09-22"
        kinds = {e["kind"] for e in A.events_from_report(report.to_dict())}
        assert A.WINDOW_CAPPED not in kinds

    def test_deferred_item_stays_admissible_after_a_drained_anchor(self, windowed):
        """예전 규칙의 구멍: 완결 기준점이 있으면 그 뒤 미완결 실행의 더 이른 cutoff 를 몰랐다."""
        windowed["feeds"][GEEK] = []
        windowed["run"](today=TODAY, report=_report(1))  # 완결 (09-29)

        day2 = TODAY + timedelta(days=1)
        edge = _dated(7, 6)  # day2 기준 7일 전 = 09-23, day2 창(7일)의 맨 끝
        windowed["feeds"][GEEK] = [_dated(8, 0), edge]
        windowed["run"](gate={GEEK: 1}, extract={GEEK: 10}, today=day2, report=_report(2))  # edge 를 미룸

        day4 = TODAY + timedelta(days=3)  # 기준점 09-29 → 예전 규칙이면 cutoff 09-25 로 edge 를 거른다
        report = windowed["run"](gate={GEEK: 10}, extract={GEEK: 10}, today=day4, report=_report(3))

        assert report.window[GEEK]["cutoff"] == "2026-09-23"
        assert report.sources[GEEK]["out_of_window"] == 0
        assert report.sources[GEEK]["gate_calls"] == 1

    def test_a_persisting_undrained_state_is_capped(self, windowed):
        """넓어진 상태가 **지속돼** 필요한 cutoff 가 14일 너머로 가면 그때 capped(심각)."""
        windowed["feeds"][GEEK] = [_dated(1, 0), _dated(2, 0)]
        windowed["run"](gate={GEEK: 1}, extract={GEEK: 10}, today=TODAY, report=_report(1))

        still = windowed["run"](gate={GEEK: 0}, extract={GEEK: 10}, today=TODAY + timedelta(days=6), report=_report(2))
        assert not still.window[GEEK]["capped"]  # 필요한 cutoff 09-22 = 10-05 − 13일

        report = windowed["run"](gate={GEEK: 0}, extract={GEEK: 10}, today=TODAY + timedelta(days=8), report=_report(3))
        assert report.window[GEEK]["capped"] and report.window[GEEK]["span_days"] == 14

