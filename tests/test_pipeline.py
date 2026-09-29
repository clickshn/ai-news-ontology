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
        feed = feeds.get(source_name, [])
        if isinstance(feed, BaseException):
            raise feed
        return list(feed)

    monkeypatch.setattr(pipeline_runner, "collect_feeds", fake_collect)

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
