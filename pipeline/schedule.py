"""매일 자동 실행 — `python -m pipeline scheduled` (ADR-025).

## 순서 (고정)

    잠금 → 사전 점검(가부) → 준비 확인(TCP) → run_pipeline → 경보 누적 → 상태 노트 · 상태 파일 · 토스트

| 판정 | 동작 | 종료 코드 |
|---|---|---|
| 가부 불일치 | LLM 0건, **수집·적재까지 전 단계 정지** | 4 |
| 환경 미준비 | LLM 0건, 전 단계 정지. 차단기와 별개 종류 | 5 |
| 잠금 보유 중 | 아무것도 하지 않음 | 6 |
| 통과 | `run_pipeline` 그대로 | 0 / 3 / 1 |

가부 불일치 때 수집·적재까지 멈추는 이유: 일부라도 진행하면 겉으로 "돌았다"로 보인다.

## 이 실행에는 훅이 없다

작업 스케줄러가 부르는 프로세스는 Claude Code 도구 호출이 아니다. `check-external-llm.sh`
는 여기 걸리지 않고, 남는 방어선은 코드 계층(`egress.py`)과 이 모듈의 사전 점검뿐이다.

## 환경 미준비와 장애

VPN 이 필요한 환경이면 스케줄러가 VPN 연결 전에 돌 수 있다. 그대로 두면 전송 오류 3회로
차단기가 걸려 엔드포인트 장애와 같은 신호가 된다. 대응이 반대이므로 가른다:

- 준비 확인은 엔드포인트 호스트·포트로의 **TCP 연결만** 한다. HTTP 요청도 페이로드도
  없다 — LLM 호출이 아니고 데이터를 내보내지 않는다.
- `schedule.readiness.wait_minutes` 동안 `interval_seconds` 간격으로 다시 시도한다.
  끝까지 안 닿으면 환경 미준비(5)다.
- 닿은 뒤의 전송 오류는 지금처럼 장애(차단기)다. TCP 연결 성공은 추론 가능을 뜻하지
  않는다 — 실행 도중 VPN 이 끊기면 장애로 센다.
- 목적지 대조가 준비 확인보다 **먼저**다. 오타 호스트가 "영원히 준비 안 됨"으로 가려지지 않는다.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.parse
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from export.store import PROJECT_ROOT, ExtractionStore
from extraction.egress import ExternalVendorCallError
from extraction.llm import LLMClient, client_from_config, resolved_vllm_endpoint
from observability.events import PipelineObserver
from pipeline import alerts as A
from pipeline import approval as P
from pipeline import notify as N
from pipeline.ledger import Ledger, now_iso
from pipeline.quality import quality_metrics
from pipeline.runner import (
    DEFAULT_RUNS_DIR,
    RunReport,
    limits_from_config,
    new_report,
    run_pipeline,
    write_report,
)

EXIT_PREFLIGHT = 4
EXIT_NOT_READY = 5
EXIT_LOCKED = 6

PIPELINE_DIR = PROJECT_ROOT / "data" / "pipeline"
DEFAULT_ALERTS_PATH = PIPELINE_DIR / "alerts.json"
DEFAULT_STATUS_PATH = PIPELINE_DIR / "status.txt"
DEFAULT_LOCK_PATH = PIPELINE_DIR / "scheduled.lock"

#: 이보다 오래된 잠금은 죽은 실행이 남긴 것으로 보고 넘겨받는다 (경보는 남긴다).
STALE_LOCK_HOURS = 6


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScheduleConfig:
    time: str | None
    wait_minutes: float
    interval_seconds: float
    connect_timeout_seconds: float
    absent_after_hours: int
    toast: bool


def schedule_config(config: dict[str, Any]) -> ScheduleConfig:
    section = config.get("schedule") or {}
    readiness = section.get("readiness") or {}
    return ScheduleConfig(
        time=section.get("time") or None,
        wait_minutes=float(readiness.get("wait_minutes", 30)),
        interval_seconds=float(readiness.get("interval_seconds", 60)),
        connect_timeout_seconds=float(readiness.get("connect_timeout_seconds", 5)),
        absent_after_hours=int(section.get("absent_after_hours", 36)),
        toast=bool(section.get("toast", True)),
    )


# ---------------------------------------------------------------------------
# 잠금
# ---------------------------------------------------------------------------
class LockHeld(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path, *, now: datetime, stale_hours: float = STALE_LOCK_HOURS) -> Iterator[bool]:
    """배타 잠금. 넘겨받은 잠금이면 True 를 준다(죽은 실행 흔적 — 경보 대상)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    took_over = False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 잠금의 나이는 **파일 시스템 시계**로 잰다. mtime 과 주입된 `now` 를 섞으면
        # 시계가 다른 두 값을 빼게 되어 살아 있는 잠금을 죽은 것으로 넘겨받는다.
        age = time.time() - path.stat().st_mtime
        if age < stale_hours * 3600:
            raise LockHeld(f"다른 실행이 잠금을 쥐고 있다 ({path.name}, {int(age // 60)}분 전)")
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        took_over = True
    try:
        os.write(fd, f"pid={os.getpid()} at={now.isoformat()}\n".encode())
        os.close(fd)
        yield took_over
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 준비 확인
# ---------------------------------------------------------------------------
def endpoint_address(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def wait_until_ready(
    url: str,
    cfg: ScheduleConfig,
    *,
    connect: Callable[..., Any] = socket.create_connection,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, int, str | None]:
    """(준비됨, 시도 횟수, 마지막 오류). TCP 연결만 하고 바로 닫는다."""
    host, port = endpoint_address(url)
    deadline = clock() + cfg.wait_minutes * 60
    attempts = 0
    last_error: str | None = None
    while True:
        attempts += 1
        try:
            conn = connect((host, port), timeout=cfg.connect_timeout_seconds)
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:200]
        else:
            try:
                conn.close()
            except Exception:
                pass
            return True, attempts, None
        if clock() + cfg.interval_seconds > deadline:
            return False, attempts, last_error
        sleep(cfg.interval_seconds)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
@dataclass
class Paths:
    project_root: Path = PROJECT_ROOT
    runs_dir: Path = DEFAULT_RUNS_DIR
    alerts: Path = DEFAULT_ALERTS_PATH
    status: Path = DEFAULT_STATUS_PATH
    lock: Path = DEFAULT_LOCK_PATH
    prompt_dir: Path | None = None


def run_scheduled(
    config: dict[str, Any],
    *,
    ledger: Ledger,
    store: ExtractionStore,
    output_dir: Path,
    paths: Paths | None = None,
    observer: PipelineObserver | None = None,
    client_factory: Callable[..., LLMClient] = client_from_config,
    endpoint_resolver: Callable[[dict[str, Any]], str] = resolved_vllm_endpoint,
    connect: Callable[..., Any] = socket.create_connection,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    toast: Callable[[str, str], str | None] = N.send_toast,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    paths = paths or Paths()
    cfg = schedule_config(config)
    started = now()
    try:
        with run_lock(paths.lock, now=started) as took_over:
            return _run_locked(
                config, cfg, ledger=ledger, store=store, output_dir=output_dir, paths=paths,
                observer=observer, client_factory=client_factory, endpoint_resolver=endpoint_resolver,
                connect=connect, sleep=sleep, clock=clock, toast=toast, now=now, took_over=took_over,
            )
    except LockHeld as exc:
        # 다른 실행을 방해하지 않는다 — 경보 상태도 건드리지 않고 로그만 남긴다.
        print(f"[scheduled] {exc}", file=sys.stderr)
        return EXIT_LOCKED


def _run_locked(
    config: dict[str, Any],
    cfg: ScheduleConfig,
    *,
    ledger: Ledger,
    store: ExtractionStore,
    output_dir: Path,
    paths: Paths,
    observer: PipelineObserver | None,
    client_factory: Callable[..., LLMClient],
    endpoint_resolver: Callable[[dict[str, Any]], str],
    connect: Callable[..., Any],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    toast: Callable[[str, str], str | None],
    now: Callable[[], datetime],
    took_over: bool,
) -> int:
    report = new_report()
    events: list[dict[str, Any]] = []
    evaluated: set[str] = set(A.PREFLIGHT_KINDS)
    if took_over:
        events.append(A.event(A.LOCK_HELD, f"이전 실행의 잠금이 {STALE_LOCK_HOURS}시간 넘게 남아 있어 넘겨받았다 — 그 실행은 비정상 종료했다"))

    mismatches = P.preflight(
        config, project_root=paths.project_root, now=now(),
        prompt_dir=paths.prompt_dir, endpoint_resolver=endpoint_resolver,
    )
    if mismatches:
        for m in mismatches:
            events.append(A.event(A.PREFLIGHT_MISMATCH, f"[{m.key}] {m.detail}"))
            print(f"[preflight] {m.key}: {m.detail}", file=sys.stderr)
        return _finish(config, cfg, report, events, evaluated, paths=paths, output_dir=output_dir,
                       store=store, toast=toast, now=now, exit_code=EXIT_PREFLIGHT, outcome="가부 불일치로 정지")

    evaluated |= A.READINESS_KINDS
    ready, attempts, error = wait_until_ready(
        endpoint_resolver(config), cfg, connect=connect, sleep=sleep, clock=clock,
    )
    if not ready:
        detail = f"엔드포인트에 TCP 로 닿지 않았다 ({attempts}회, {cfg.wait_minutes:g}분) — VPN·네트워크 준비 전일 수 있다. {error or ''}".strip()
        events.append(A.event(A.ENV_NOT_READY, detail))
        print(f"[readiness] {detail}", file=sys.stderr)
        return _finish(config, cfg, report, events, evaluated, paths=paths, output_dir=output_dir,
                       store=store, toast=toast, now=now, exit_code=EXIT_NOT_READY, outcome="환경 미준비로 정지")

    evaluated |= A.RUN_KINDS
    try:
        run_pipeline(
            config,
            limits=limits_from_config(config),
            limits_mode="scheduled",
            ledger=ledger,
            store=store,
            output_dir=output_dir,
            runs_dir=paths.runs_dir,
            observer=observer,
            client_factory=client_factory,
            report=report,
        )
    except ExternalVendorCallError as exc:
        # 사전 점검을 통과했는데 여기 왔다면 점검이 놓친 경로다 (ADR-025 Risks).
        events.append(A.event(A.PREFLIGHT_MISMATCH, f"사전 점검 뒤 벤더 경로에 닿았다: {type(exc).__name__}"))
        return _finish(config, cfg, report, events, evaluated, paths=paths, output_dir=output_dir,
                       store=store, toast=toast, now=now, exit_code=EXIT_PREFLIGHT, outcome="벤더 경로 차단으로 정지")
    except Exception as exc:  # noqa: BLE001 — 실행이 죽어도 경보는 남긴다
        events.append(A.event(A.RUN_ERROR, f"실행 중 예외: {type(exc).__name__}: {exc}"[:300]))
        return _finish(config, cfg, report, events, evaluated, paths=paths, output_dir=output_dir,
                       store=store, toast=toast, now=now, exit_code=1, outcome="실행 중 예외")

    run_dict = report.to_dict()
    events += A.events_from_report(run_dict)
    report.quality = quality_metrics(run_dict, store)
    code = report.exit_code()
    outcome = {0: "정상", 3: "부분 실패", 1: "실패"}.get(code, str(code))
    return _finish(config, cfg, report, events, evaluated, paths=paths, output_dir=output_dir,
                   store=store, toast=toast, now=now, exit_code=code, outcome=outcome)


def _finish(
    config: dict[str, Any],
    cfg: ScheduleConfig,
    report: RunReport,
    events: list[dict[str, Any]],
    evaluated: set[str],
    *,
    paths: Paths,
    output_dir: Path,
    store: ExtractionStore,
    toast: Callable[[str, str], str | None],
    now: Callable[[], datetime],
    exit_code: int,
    outcome: str,
) -> int:
    """경보 누적 → 실행 요약 → 상태 노트 · 상태 파일 · 토스트. 전달 실패는 서로를 막지 않는다."""
    finished = now()
    state = A.load_state(paths.alerts)
    raised = A.update_state(state, events, run_id=report.run_id, now=finished.isoformat(timespec="seconds"), evaluated=evaluated)
    A.save_state(state, paths.alerts)

    report.alerts = events
    if not report.limits:
        report.limits = {"mode": "scheduled"}
    delivery: dict[str, Any] = {}
    run = {**report.to_dict(), "exit_code": exit_code, "outcome": outcome, "finished_at": finished.isoformat(timespec="seconds")}
    approval = P.load_record(paths.project_root)

    try:
        N.write_status_note(output_dir, N.render_status_note(
            state=state, run=run, approval=approval, absent_after_hours=cfg.absent_after_hours,
        ))
        delivery["status_note"] = "ok"
    except Exception as exc:  # noqa: BLE001
        delivery["status_note"] = f"{type(exc).__name__}: {exc}"[:300]

    if cfg.toast:
        text = N.toast_text(state)
        if text is not None:
            delivery["toast"] = toast(*text) or "ok"

    # 상태 파일은 마지막에 쓴다 — 노트·토스트 실패도 여기 실려 훅에 보이게.
    status_state = state
    if delivery.get("status_note") not in (None, "ok"):
        status_state = {**state, "open": {**state["open"], "delivery|-": {
            "kind": "delivery_failed", "source": None, "severity": A.WARNING, "consecutive": 1,
            "first_seen": finished.isoformat(timespec="seconds"),
            "detail": f"Vault 상태 노트를 못 썼다: {delivery['status_note']}",
        }}}
    N.write_status_file(paths.status, N.render_status_file(
        state=status_state, run=run, approval=approval, absent_after_hours=cfg.absent_after_hours, now=finished,
    ))

    summary = write_report(report, paths.runs_dir)
    data = json.loads(summary.read_text(encoding="utf-8"))
    # `run_pipeline` 의 종료 코드가 아니라 이 실행의 종료 코드다 — 사전 점검에서 멈춘
    # 실행의 요약이 "exit_code: 0" 으로 읽히면 멈춤이 정상 실행의 모양이 된다.
    data["exit_code"] = exit_code
    data.update({"scheduled": {"exit_code": exit_code, "outcome": outcome, "raised": raised, "delivery": delivery}})
    summary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[scheduled] {outcome} exit={exit_code} 열린 경보 {len(state['open'])}건 · 전달 {delivery}", file=sys.stderr)
    return exit_code


# ---------------------------------------------------------------------------
# 로그 — stderr 는 자동 실행에서 아무도 안 본다. 보존만 한다
# ---------------------------------------------------------------------------
class _Tee:
    def __init__(self, *streams: Any) -> None:
        self._streams = [st for st in streams if st is not None]

    def write(self, text: str) -> int:
        for st in self._streams:
            try:
                st.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


@contextmanager
def tee_output(log_path: Path) -> Iterator[Path]:
    """stdout·stderr 를 그대로 흘리면서 `log_path` 에도 남긴다."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(old_out, fh), _Tee(old_err, fh)
        try:
            yield log_path
        finally:
            sys.stdout, sys.stderr = old_out, old_err


def log_path_for(runs_dir: Path, when: datetime) -> Path:
    return Path(runs_dir) / f"scheduled-{when.strftime('%Y%m%d-%H%M%S')}.log"


# ---------------------------------------------------------------------------
# 상시 승인 만들기 — 사람만
# ---------------------------------------------------------------------------
CONFIRM_SAMPLE = "대조함"
CONFIRM_APPROVE = "승인"


def sample_for_review(ledger: Ledger, store: ExtractionStore, *, size: int, rng=None) -> list[dict[str, Any]]:
    """원문과 대조할 노트 후보. 적재까지 된 최근 항목에서 무작위로 고른다."""
    import random

    rng = rng or random.Random()
    candidates = [
        e for e in ledger
        if store.path_for(e.doc_id).exists() and (e.load or {}).get("status") in ("written", "exists")
    ]
    candidates.sort(key=lambda e: e.updated_at, reverse=True)
    pool = candidates[: max(size * 5, size)]
    picked = rng.sample(pool, k=min(size, len(pool)))
    return [
        {"doc_id": e.doc_id, "title": e.title, "url": e.url, "source": e.source_name, "note": (e.load or {}).get("note")}
        for e in picked
    ]


def approve_interactive(
    config: dict[str, Any],
    *,
    ledger: Ledger,
    store: ExtractionStore,
    paths: Paths | None = None,
    stdin=None,
    stdout=None,
    endpoint_resolver: Callable[[dict[str, Any]], str] = resolved_vllm_endpoint,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    rng=None,
) -> int:
    """6항목을 현재 상태로 계산해 보여주고, 사람이 **직접 입력**해야 기록을 만든다.

    TTY 가 아니면 거부한다 — 에이전트의 Bash 도구는 stdin 이 비어 있어 여기를 통과하지
    못한다. 의도한 제약이다 (ADR-025).
    """
    paths = paths or Paths()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def say(text: str = "") -> None:
        print(text, file=stdout)

    if not (hasattr(stdin, "isatty") and stdin.isatty()):
        say("대화형 터미널에서만 만든다 — 사람이 직접 답해야 하는 승인이다 (ADR-025).")
        return 2
    fp = P.fingerprint(config, project_root=paths.project_root, prompt_dir=paths.prompt_dir, endpoint_resolver=endpoint_resolver)
    blockers: list[str] = []
    if fp["provider"] != "vllm":
        blockers.append(f"llm.provider 가 {fp['provider']!r} — 자동 실행은 내부 vLLM 에서만 승인한다")
    if fp["endpoint_sha256"] is None:
        blockers.append("엔드포인트를 해석하지 못했다 (VLLM_BASE)")
    if (paths.project_root / ".claude" / "external-llm-approved").is_file():
        blockers.append("`.claude/external-llm-approved` 가 남아 있다 — 먼저 지운다")
    base = P.full_manual_run(paths.runs_dir, fp["limits"])
    if base is None:
        blockers.append(
            "현재 config 상한으로 돈 수동 실행(`python -m pipeline run`, 인자 없음)이 없다. "
            "상시 승인이 첫 전량 실행이 되면 안 된다 — 일반 게이트로 한 번 돌리고 결과를 본 뒤 승인한다"
        )
    if blockers:
        for b in blockers:
            say(f"⛔ {b}")
        return 2

    from extraction.egress import match_external_vendor

    url = endpoint_resolver(config)
    limits = fp["limits"]
    say("=== 상시 승인 — 게이트 6항목 (ADR-025) ===")
    say(f"1. 대상 엔드포인트가 내부 vLLM(VLLM_BASE)인가: provider={fp['provider']}, "
        f"해석된 URL sha256={fp['endpoint_sha256'][:12]}…, 벤더 거부 목록 일치={match_external_vendor(url) or '없음'}")
    say("   → 이 해시가 앞으로 매 실행의 대조 기준이다. 다른 목적지면 멈춘다")
    say(f"2. 데이터 범위: 공개 RSS/Atom 피드 {len(fp['sources'])}개 — " + ", ".join(n for n, _ in fp["sources"]))
    say("3. 목적: 매일 자동 실행 (수집 → 게이트 → 추출 → 보존 → 적재)")
    say(f"4. 모델: 게이트 {fp['models']['relevance_gate']} / 추출 {fp['models']['extraction']}")
    say(f"5. 건수(실행당 상한): 게이트 {sum(limits['gate'].values())} / 추출 {sum(limits['extract'].values())} "
        "(논리 호출, 스키마 재시도로 각각 최대 2배)")
    say("6. 비용: 내부 vLLM — 벤더 과금 없음")
    say(f"   기준 수동 실행: {base.get('run_id')} · 만료: {P.APPROVAL_DAYS}일 뒤")
    say()

    sample = sample_for_review(ledger, store, size=P.SAMPLE_SIZE, rng=rng)
    if len(sample) < P.SAMPLE_SIZE:
        say(f"⛔ 대조할 노트가 {len(sample)}건뿐이다 ({P.SAMPLE_SIZE}건 필요)")
        return 2
    say(f"=== 원문 대조 {P.SAMPLE_SIZE}건 — 노트의 필드가 원문에 근거하는지 본다 (D-107) ===")
    for s in sample:
        say(f"- [{s['source']}] {s['title']}\n  원문: {s['url']}\n  노트: {s['note'] or '(경로 기록 없음)'}")
    say()
    say(f"세 건을 원문과 대조했으면 '{CONFIRM_SAMPLE}' 을 입력:")
    if stdin.readline().strip() != CONFIRM_SAMPLE:
        say("중단 — 기록을 만들지 않았다")
        return 1
    say(f"위 6항목으로 상시 승인하려면 '{CONFIRM_APPROVE}' 을 입력:")
    if stdin.readline().strip() != CONFIRM_APPROVE:
        say("중단 — 기록을 만들지 않았다")
        return 1
    record = P.new_record(fp, now=now(), based_on_run=base.get("run_id", ""), sample=[s["doc_id"] for s in sample])
    path = P.write_record(record, paths.project_root)
    say(f"기록함: {path.name} (만료 {record['expires_at']})")
    return 0
