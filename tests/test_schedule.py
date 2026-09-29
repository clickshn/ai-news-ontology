"""매일 자동 실행 — 상시 승인 대조 · 환경 미준비 · 경보 누적 · 전달 (ADR-025).

이 파일이 고정하는 것:

1. **config 상한은 승인이 아니다.** 상시 승인 기록이 없으면 LLM 0건으로 멈춘다.
2. 1번의 고정 답이 깨지는 경로 a–h 가 **각각** 불일치로 드러난다.
3. 멈춘 실행은 멈춘 것으로 보인다 — 종료 코드, 상태 노트, 상태 파일, 토스트.
4. 엔드포인트에 닿지 않는 것(환경 미준비)은 차단기(장애)와 따로 센다.
5. Vault 에는 `_pipeline-status.md` **하나만** 쓴다.

실제 피드·엔드포인트·Vault·승인 파일은 건드리지 않는다. 전부 `tmp_path` 이고, TCP 연결은
주입한 함수다 (`no_outbound_network` 트립와이어가 그대로 걸려 있다).
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from export.store import PROJECT_ROOT, ExtractionStore
from extraction.llm import client_from_config, resolved_vllm_endpoint
from pipeline import alerts as A
from pipeline import approval as P
from pipeline import notify as N
from pipeline import runner as pipeline_runner
from pipeline import schedule as S
from pipeline.ledger import Ledger
from tests.test_pipeline import GEEK, ScriptedClient, _item

URL = "https://vllm.internal.example/v1"
NOW = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
PROMPT_DIR = PROJECT_ROOT / "extraction" / "prompts"


def _config(**llm_overrides):
    llm = {
        "provider": "vllm",
        "relevance_gate": {"model": "gemma-4-31B-it", "vendor_model": "claude-haiku-4-5-20251001"},
        "extraction": {"model": "gemma-4-31B-it", "vendor_model": "claude-opus-5"},
    }
    llm.update(llm_overrides)
    return {
        "version": 1,
        "sources": {"rss": [{"name": GEEK, "url": "https://example.com/feed", "limits": {"gate": 3, "extract": 2}}]},
        "llm": llm,
        "schedule": {"readiness": {"wait_minutes": 1, "interval_seconds": 20, "connect_timeout_seconds": 1}},
        "output": {"on_conflict": "skip", "filename_template": "{date}-{source}-{slug}.md"},
    }


@pytest.fixture
def root(tmp_path):
    """목적지 결정 코드를 복사한 가짜 프로젝트 루트."""
    r = tmp_path / "repo"
    for rel in P.GUARD_FILES:
        dst = r / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT_ROOT / rel, dst)
    (r / ".claude").mkdir(parents=True, exist_ok=True)
    return r


def _fp(config, root, url=URL):
    return P.fingerprint(config, project_root=root, prompt_dir=PROMPT_DIR, endpoint_resolver=lambda c: url)


def _approve(config, root, *, url=URL, now=NOW):
    P.write_record(P.new_record(_fp(config, root, url), now=now - timedelta(days=1), based_on_run="r", sample=[]), root)


def _preflight(config, root, *, url=URL, now=NOW):
    return P.preflight(config, project_root=root, now=now, prompt_dir=PROMPT_DIR, endpoint_resolver=lambda c: url)


def _keys(mismatches):
    return {m.key for m in mismatches}


# ---------------------------------------------------------------------------
# 목적지 해석 — 대조하는 목적지와 부르는 목적지가 같아야 한다
# ---------------------------------------------------------------------------
def test_resolved_endpoint_matches_what_the_client_calls(monkeypatch):
    monkeypatch.setenv("VLLM_BASE", "https://a.internal.example/v1/")
    config = _config()
    assert resolved_vllm_endpoint(config) == client_from_config(config, stage="extraction").base_url


def test_resolved_endpoint_follows_config_base_url_override(monkeypatch):
    monkeypatch.setenv("VLLM_BASE", "https://a.internal.example/v1")
    config = _config(vllm={"base_url": "https://b.internal.example/v1"})
    assert resolved_vllm_endpoint(config) == client_from_config(config, stage="extraction").base_url
    assert "b.internal" in resolved_vllm_endpoint(config)


def test_os_environment_beats_dotenv(monkeypatch):
    """d: 작업 스케줄러 환경변수가 `.env` 를 이긴다 — 대조가 그 값을 본다."""
    monkeypatch.setenv("VLLM_BASE", "https://scheduler-env.example/v1")
    assert resolved_vllm_endpoint(_config()) == "https://scheduler-env.example/v1"


def test_fingerprint_never_contains_the_url(root):
    fp = _fp(_config(), root)
    assert URL not in json.dumps(fp)
    assert "vllm.internal" not in json.dumps(fp)
    assert fp["endpoint_sha256"] == P.endpoint_sha256(URL)


# ---------------------------------------------------------------------------
# 사전 점검 — 경로 a–h
# ---------------------------------------------------------------------------
def test_no_record_is_a_mismatch_config_limits_are_not_approval(root):
    assert _keys(_preflight(_config(), root)) == {"approval"}


def test_matching_record_passes(root):
    config = _config()
    _approve(config, root)
    assert _preflight(config, root) == []


def test_expired_record(root):
    config = _config()
    _approve(config, root, now=NOW - timedelta(days=P.APPROVAL_DAYS))
    assert "approval" in _keys(_preflight(config, root))


def test_a_provider_changed_to_vendor(root):
    config = _config()
    _approve(config, root)
    config["llm"]["provider"] = "anthropic"
    keys = _keys(_preflight(config, root))
    assert "provider" in keys and "model.extraction" in keys


def test_b_vendor_approval_file_left_behind_stops_even_when_all_else_matches(root):
    config = _config()
    _approve(config, root)
    (root / ".claude" / "external-llm-approved").write_text("", encoding="utf-8")
    assert _keys(_preflight(config, root)) == {"vendor_approval"}


@pytest.mark.parametrize("other", [
    "https://private-proxy.example/v1",  # 거부 목록에 없는 호스트 — assert_internal_endpoint 는 통과시킨다
    "https://vllm.internal.example:8443/v1",
    "https://vllm.internal.example/v2",
])
def test_c_d_e_endpoint_changed_even_to_a_non_vendor_host(root, other):
    config = _config()
    _approve(config, root)
    assert _keys(_preflight(config, root, url=other)) == {"endpoint"}


def test_unresolvable_endpoint_is_a_mismatch_not_a_crash(root):
    config = _config()
    _approve(config, root)

    def boom(c):
        raise RuntimeError("VLLM_BASE 없음")

    out = P.preflight(config, project_root=root, now=NOW, prompt_dir=PROMPT_DIR, endpoint_resolver=boom)
    assert _keys(out) == {"endpoint"}


def test_f_model_changed(root):
    config = _config()
    _approve(config, root)
    config["llm"]["extraction"]["model"] = "other-model"
    assert _keys(_preflight(config, root)) == {"model.extraction"}


def test_f_prompt_changed(root, tmp_path):
    prompts = tmp_path / "prompts"
    shutil.copytree(PROMPT_DIR, prompts)
    config = _config()
    fp = P.fingerprint(config, project_root=root, prompt_dir=prompts, endpoint_resolver=lambda c: URL)
    P.write_record(P.new_record(fp, now=NOW - timedelta(days=1), based_on_run="r", sample=[]), root)
    name = next(iter(fp["prompts"]))
    (prompts / name).write_text((prompts / name).read_text(encoding="utf-8") + "\n추가\n", encoding="utf-8")
    out = P.preflight(config, project_root=root, now=NOW, prompt_dir=prompts, endpoint_resolver=lambda c: URL)
    assert _keys(out) == {f"prompt.{name}"}


def test_g_source_added_or_url_changed(root):
    config = _config()
    _approve(config, root)
    config["sources"]["rss"].append({"name": "새 소스", "url": "https://new.example/feed", "limits": {"gate": 1, "extract": 1}})
    assert "sources" in _keys(_preflight(config, root))

    config = _config()
    config["sources"]["rss"][0]["url"] = "https://moved.example/feed"
    assert _keys(_preflight(config, root)) == {"sources"}


def test_source_removed_passes_it_narrows_the_scope(root):
    config = _config()
    config["sources"]["rss"].append({"name": "B", "url": "https://b.example/feed", "limits": {"gate": 1, "extract": 1}})
    _approve(config, root)
    config["sources"]["rss"].pop()
    assert _preflight(config, root) == []


def test_limits_raised_is_a_mismatch_lowered_passes(root):
    config = _config()
    _approve(config, root)
    config["sources"]["rss"][0]["limits"] = {"gate": 1, "extract": 1}
    assert _preflight(config, root) == []
    config["sources"]["rss"][0]["limits"] = {"gate": 4, "extract": 1}
    assert _keys(_preflight(config, root)) == {"limits.gate"}


def test_h_guard_file_changed(root):
    config = _config()
    _approve(config, root)
    path = root / "extraction" / "llm.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# 새 생성 경로\n", encoding="utf-8")
    assert _keys(_preflight(config, root)) == {"guard_file"}


def test_h_line_ending_only_change_is_not_a_mismatch(root):
    config = _config()
    _approve(config, root)
    path = root / "extraction" / "egress.py"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert _preflight(config, root) == []


def test_guard_files_cover_the_check_itself_and_the_client_factory():
    for rel in ("extraction/egress.py", "extraction/llm.py", "extraction/vllm.py", "pipeline/approval.py", "pipeline/schedule.py"):
        assert rel in P.GUARD_FILES


# ---------------------------------------------------------------------------
# 경보 누적
# ---------------------------------------------------------------------------
def test_source_error_escalates_by_consecutive_runs():
    state = A.empty_state()
    levels = []
    for i in range(3):
        A.update_state(state, [A.event(A.SOURCE_ERROR, "x", "AI타임스")], run_id=f"r{i}", now=f"t{i}", evaluated=A.RUN_KINDS)
        levels.append(state["open"]["source_error|AI타임스"]["severity"])
    assert levels == [A.INFO, A.WARNING, A.CRITICAL]


def test_env_not_ready_is_critical_on_second_run():
    assert A.severity(A.ENV_NOT_READY, 1) == A.WARNING
    assert A.severity(A.ENV_NOT_READY, 2) == A.CRITICAL


def test_unevaluated_kinds_are_not_closed():
    """멈춘 실행은 수집을 안 했다 — 수집 경보를 '해소'로 닫으면 안 봤다를 해소로 적는 것이다."""
    state = A.empty_state()
    A.update_state(state, [A.event(A.SOURCE_ERROR, "x", "S")], run_id="r1", now="t1", evaluated=A.RUN_KINDS)
    A.update_state(state, [A.event(A.PREFLIGHT_MISMATCH, "y")], run_id="r2", now="t2", evaluated=A.PREFLIGHT_KINDS)
    assert "source_error|S" in state["open"]
    A.update_state(state, [], run_id="r3", now="t3", evaluated=A.PREFLIGHT_KINDS | A.RUN_KINDS)
    assert state["open"] == {}
    assert {c["kind"] for c in state["closed"]} == {A.SOURCE_ERROR, A.PREFLIGHT_MISMATCH}


def test_events_from_report_reads_structured_values_not_stderr():
    report = {
        "fetch": {"A": {"status": "http_error", "detail": "503"}, "B": {"status": "ok", "silent": {"newest": "2024-05-10", "age_days": 800, "max_silence_days": 7}}},
        "window": {"B": {"capped": True, "drained": False, "cutoff": "2026-09-16", "span_days": 14}},
        "sources": {"B": {"deferred_gate": 5, "quarantined_now": 1}},
        "breaker_tripped": ["extraction"],
    }
    kinds = {(e["kind"], e["source"]) for e in A.events_from_report(report)}
    assert kinds == {
        (A.SOURCE_ERROR, "A"), (A.STALE_FEED, "B"), (A.WINDOW_CAPPED, "B"),
        (A.NOT_DRAINED, "B"), (A.BREAKER, None), (A.QUARANTINED, "B"),
    }


# ---------------------------------------------------------------------------
# 준비 확인
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_readiness_retries_until_deadline_then_gives_up():
    clock = FakeClock()
    calls = []

    def refuse(addr, timeout):
        calls.append(addr)
        raise ConnectionRefusedError("VPN 없음")

    cfg = S.schedule_config(_config())
    ready, attempts, error = S.wait_until_ready(URL, cfg, connect=refuse, sleep=clock.sleep, clock=clock)
    assert not ready and attempts == len(calls) == 4  # 0s, 20s, 40s, 60s — 60s 창의 끝을 포함한다
    assert calls[0] == ("vllm.internal.example", 443)
    assert "ConnectionRefusedError" in error


def test_readiness_succeeds_once_connected():
    clock = FakeClock()
    seq = [OSError("no route"), None]

    class Conn:
        def close(self):
            pass

    def connect(addr, timeout):
        outcome = seq.pop(0)
        if outcome:
            raise outcome
        return Conn()

    ready, attempts, _ = S.wait_until_ready(URL, S.schedule_config(_config()), connect=connect, sleep=clock.sleep, clock=clock)
    assert ready and attempts == 2


# ---------------------------------------------------------------------------
# run_scheduled — 끝에서 끝까지
# ---------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, root, monkeypatch):
    feeds = {GEEK: [_item(1), _item(2)]}

    def fake_collect(config, *, source_name=None, **kwargs):
        from collectors.rss import FeedResult, FetchStatus

        return [FeedResult(source_name=source_name, status=FetchStatus.OK, items=tuple(feeds.get(source_name, [])))]

    monkeypatch.setattr(pipeline_runner, "collect_feed_results", fake_collect)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "기존-노트.md").write_text("사용자 노트", encoding="utf-8")
    w = {
        "root": root,
        "vault": vault,
        "ledger": Ledger(tmp_path / "ledger"),
        "store": ExtractionStore(tmp_path / "store"),
        "paths": S.Paths(
            project_root=root, runs_dir=tmp_path / "runs", alerts=tmp_path / "p" / "alerts.json",
            status=tmp_path / "p" / "status.txt", lock=tmp_path / "p" / "scheduled.lock", prompt_dir=PROMPT_DIR,
        ),
        "clients": [],
        "connects": [],
        "toasts": [],
        "fetches": feeds,
    }

    def factory(config, stage):
        client = ScriptedClient("gemma-4-31B-it")
        w["clients"].append(stage)
        return client

    def connect(addr, timeout):
        w["connects"].append(addr)
        if w.get("offline"):
            raise ConnectionRefusedError("VPN 없음")

        class Conn:
            def close(self):
                pass

        return Conn()

    clock = FakeClock()

    def run(config):
        return S.run_scheduled(
            config, ledger=w["ledger"], store=w["store"], output_dir=vault, paths=w["paths"],
            client_factory=factory, endpoint_resolver=lambda c: URL, connect=connect,
            sleep=clock.sleep, clock=clock, toast=lambda t, b: w["toasts"].append((t, b)), now=lambda: NOW,
        )

    w["run"] = run
    return w


def _state(w):
    return A.load_state(w["paths"].alerts)


def test_without_approval_nothing_runs_and_the_stop_is_visible(world, monkeypatch):
    def no_fetch(*a, **k):
        raise AssertionError("가부 불일치인데 수집했다")

    monkeypatch.setattr(pipeline_runner, "collect_feed_results", no_fetch)
    code = world["run"](_config())
    assert code == S.EXIT_PREFLIGHT
    assert world["clients"] == [] and world["connects"] == []
    note = (world["vault"] / N.STATUS_NOTE_NAME).read_text(encoding="utf-8")
    assert "preflight_mismatch" in note and "가부 불일치" in note
    assert world["toasts"], "심각 등급인데 토스트가 없다"
    status = world["paths"].status.read_text(encoding="utf-8")
    assert "exit_code=4" in status
    summary = json.loads(next(world["paths"].runs_dir.glob("pipeline-*.json")).read_text(encoding="utf-8"))
    assert summary["exit_code"] == 4, "멈춘 실행의 요약이 exit 0 으로 읽히면 안 된다"
    # 노트 안의 시각이 한 시간대여야 한다 — frontmatter 가 UTC, 본문이 KST 였다 (실기 스모크)
    assert "updated: 2026-09-30T09:00:00+09:00" in note


def test_approved_run_goes_through_and_vault_gets_only_one_extra_file(world):
    config = _config()
    _approve(config, world["root"])
    code = world["run"](config)
    assert code == 0
    assert set(world["clients"]) == {"relevance_gate", "extraction"}
    files = {p.name for p in world["vault"].iterdir()}
    assert "기존-노트.md" in files and N.STATUS_NOTE_NAME in files
    assert (world["vault"] / "기존-노트.md").read_text(encoding="utf-8") == "사용자 노트"
    others = files - {"기존-노트.md", N.STATUS_NOTE_NAME}
    assert others and all(not name.startswith("_") for name in others)  # 나머지는 뉴스 노트
    summary = json.loads(next(world["paths"].runs_dir.glob("pipeline-*.json")).read_text(encoding="utf-8"))
    assert summary["limits"]["mode"] == "scheduled"
    assert summary["quality"][GEEK]["extracted"] == 2
    assert summary["inputs"][GEEK]["body_chars_median"] == len("본문")
    assert world["toasts"] == []


def test_env_not_ready_is_not_a_breaker_and_escalates(world):
    config = _config()
    _approve(config, world["root"])
    world["offline"] = True
    assert world["run"](config) == S.EXIT_NOT_READY
    assert world["clients"] == [], "환경 미준비인데 LLM 클라이언트를 만들었다"
    alert = _state(world)["open"]["env_not_ready|-"]
    assert alert["severity"] == A.WARNING
    assert not any(k.startswith("breaker") for k in _state(world)["open"])
    assert world["toasts"] == []
    assert world["run"](config) == S.EXIT_NOT_READY
    assert _state(world)["open"]["env_not_ready|-"]["severity"] == A.CRITICAL
    assert world["toasts"]
    world["offline"] = False
    assert world["run"](config) == 0
    assert "env_not_ready|-" not in _state(world)["open"]


def test_preflight_order_endpoint_check_comes_before_readiness(world):
    config = _config()
    _approve(config, world["root"], url="https://approved.example/v1")
    world["offline"] = True
    assert world["run"](config) == S.EXIT_PREFLIGHT
    assert world["connects"] == [], "오타 호스트가 '준비 안 됨'으로 가려진다"


def test_lock_held_does_nothing(world):
    config = _config()
    _approve(config, world["root"])
    world["paths"].lock.parent.mkdir(parents=True, exist_ok=True)
    world["paths"].lock.write_text("pid=1", encoding="utf-8")
    assert world["run"](config) == S.EXIT_LOCKED
    assert world["clients"] == []
    assert world["paths"].lock.exists(), "남의 잠금을 지웠다"


def test_stale_lock_is_taken_over_and_reported(world):
    import os
    import time

    config = _config()
    _approve(config, world["root"])
    lock = world["paths"].lock
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("pid=1", encoding="utf-8")
    old = time.time() - (S.STALE_LOCK_HOURS + 1) * 3600
    os.utime(lock, (old, old))
    assert world["run"](config) == 0
    assert "lock_held|-" in _state(world)["open"]
    assert not lock.exists()


def test_toast_failure_does_not_change_the_exit_code(world):
    def broken(t, b):
        return "powershell 없음"

    code = S.run_scheduled(
        _config(), ledger=world["ledger"], store=world["store"], output_dir=world["vault"], paths=world["paths"],
        client_factory=lambda c, stage: ScriptedClient("m"), endpoint_resolver=lambda c: URL,
        connect=lambda a, timeout: None, toast=broken, now=lambda: NOW,
    )
    assert code == S.EXIT_PREFLIGHT
    summary = json.loads(next(world["paths"].runs_dir.glob("pipeline-*.json")).read_text(encoding="utf-8"))
    assert summary["scheduled"]["delivery"]["toast"] == "powershell 없음"


def test_missing_vault_is_reported_in_the_status_file(world, tmp_path):
    code = S.run_scheduled(
        _config(), ledger=world["ledger"], store=world["store"], output_dir=tmp_path / "없음", paths=world["paths"],
        endpoint_resolver=lambda c: URL, toast=lambda t, b: None, now=lambda: NOW,
    )
    assert code == S.EXIT_PREFLIGHT
    assert "Vault 상태 노트를 못 썼다" in world["paths"].status.read_text(encoding="utf-8")


def test_notify_has_no_other_vault_write_path():
    """Vault 예외는 `_pipeline-status.md` 하나 — 다른 이름으로 쓰는 경로가 생기면 여기서 깨진다."""
    source = (PROJECT_ROOT / "pipeline" / "notify.py").read_text(encoding="utf-8")
    assert source.count("write_text(") == 2  # 상태 노트 1 · data/pipeline/status.txt 1
    assert 'STATUS_NOTE_NAME = "_pipeline-status.md"' in source
    assert "status_note_path(output_dir)" in source


# ---------------------------------------------------------------------------
# 상시 승인 만들기
# ---------------------------------------------------------------------------
class TTY(io.StringIO):
    def isatty(self):
        return True


def _full_run(runs_dir: Path, config, *, mode="config"):
    runs_dir.mkdir(parents=True, exist_ok=True)
    limits = P.configured_limits(config)
    (runs_dir / f"pipeline-20260929-0000{mode[0]}.json").write_text(json.dumps({
        "run_id": f"pipeline-{mode}", "finished_at": "x", "limits": {"mode": mode, **limits},
    }), encoding="utf-8")


def _seed_notes(ledger, store, make_payload, n=3):
    from pipeline.ledger import LedgerEntry

    for i in range(n):
        doc_id = f"doc{i}"
        store.save(make_payload(doc_id=doc_id, url=f"https://example.com/{i}"))
        ledger.put(LedgerEntry(doc_id=doc_id, url=f"https://example.com/{i}", title=f"t{i}", source_name=GEEK,
                               load={"status": "written", "note": f"n{i}.md"}))


def _approve_cli(world, config, stdin):
    out = io.StringIO()
    code = S.approve_interactive(
        config, ledger=world["ledger"], store=world["store"], paths=world["paths"], stdin=stdin, stdout=out,
        endpoint_resolver=lambda c: URL, now=lambda: NOW,
    )
    return code, out.getvalue()


def test_approve_refuses_without_a_tty(world, make_payload):
    # 다른 선행 조건은 전부 채운다 — TTY 가 **유일한** 거부 사유여야 이 검사를 잰다.
    _seed_notes(world["ledger"], world["store"], make_payload)
    _full_run(world["paths"].runs_dir, _config())
    code, out = _approve_cli(world, _config(), io.StringIO("대조함\n승인\n"))
    assert code == 2 and not P.approval_path(world["root"]).exists()


def test_approve_refuses_before_a_full_manual_run(world, make_payload):
    _seed_notes(world["ledger"], world["store"], make_payload)
    _full_run(world["paths"].runs_dir, _config(), mode="scheduled")  # 자동 실행은 세지 않는다
    code, out = _approve_cli(world, _config(), TTY("대조함\n승인\n"))
    assert code == 2 and "첫 전량 실행" in out
    assert not P.approval_path(world["root"]).exists()


def test_approve_happy_path_writes_a_record_that_passes_preflight(world, make_payload):
    config = _config()
    _seed_notes(world["ledger"], world["store"], make_payload)
    _full_run(world["paths"].runs_dir, config)
    code, out = _approve_cli(world, config, TTY("대조함\n승인\n"))
    assert code == 0, out
    assert "1. 대상 엔드포인트가 내부 vLLM" in out and URL not in out
    record = P.load_record(world["root"])
    assert len(record["sample_reviewed"]) == P.SAMPLE_SIZE
    assert _preflight(config, world["root"]) == []


def test_approve_aborts_on_wrong_phrase(world, make_payload):
    config = _config()
    _seed_notes(world["ledger"], world["store"], make_payload)
    _full_run(world["paths"].runs_dir, config)
    code, _ = _approve_cli(world, config, TTY("네\n"))
    assert code == 1 and not P.approval_path(world["root"]).exists()


def test_approve_refuses_when_vendor_approval_file_exists(world, make_payload):
    config = _config()
    _seed_notes(world["ledger"], world["store"], make_payload)
    _full_run(world["paths"].runs_dir, config)
    (world["root"] / ".claude" / "external-llm-approved").write_text("", encoding="utf-8")
    code, out = _approve_cli(world, config, TTY("대조함\n승인\n"))
    assert code == 2 and "external-llm-approved" in out


def test_approval_record_is_gitignored():
    assert P.APPROVAL_FILENAME in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# SessionStart 훅 — 실행 부재는 읽는 쪽이 잰다
# ---------------------------------------------------------------------------
HOOK = PROJECT_ROOT / ".claude" / "hooks" / "pipeline-status.sh"


def _hook(root: Path) -> str:
    dst = root / ".claude" / "hooks" / "pipeline-status.sh"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(HOOK.read_bytes())
    result = subprocess.run(
        ["bash", ".claude/hooks/pipeline-status.sh"], cwd=root, capture_output=True, timeout=30,
        env={**__import__("os").environ, "CLAUDE_PROJECT_DIR": str(root)},
    )
    assert result.returncode == 0
    return result.stdout.decode("utf-8")


def _status(root: Path, *, epoch: int, expires: str = "2099-01-01T00:00:00+00:00"):
    p = root / "data" / "pipeline" / "status.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"last_run_epoch={epoch}\nlast_run_at=2026-09-29\nexit_code=0\noutcome=정상\n"
        f"absent_after_hours=36\napproval_expires={expires}\n---\n🟠 stale_feed · X — 멈춤\n",
        encoding="utf-8", newline="\n",
    )


def test_hook_silent_when_schedule_not_in_use(tmp_path):
    assert _hook(tmp_path) == ""


def test_hook_warns_when_approved_but_never_ran(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "scheduled-run-approved.json").write_text("{}", encoding="utf-8")
    assert "한 번도 안 돌았을" in _hook(tmp_path)


def test_hook_prints_alerts_and_flags_absence(tmp_path):
    import time

    _status(tmp_path, epoch=int(time.time()))
    out = _hook(tmp_path)
    assert "stale_feed" in out and "오래됐다" not in out
    _status(tmp_path, epoch=int(time.time()) - 40 * 3600)
    assert "오래됐다" in _hook(tmp_path)


def test_hook_flags_expired_approval(tmp_path):
    import time

    _status(tmp_path, epoch=int(time.time()), expires="2000-01-01T00:00:00+00:00")
    assert "만료됐다" in _hook(tmp_path)


def test_hook_is_lf():
    assert b"\r\n" not in HOOK.read_bytes()
