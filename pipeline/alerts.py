"""경보 — 실행 요약에서 뽑고, 실행을 넘어 누적하고, 등급을 매긴다 (ADR-025).

## stderr 는 경고가 아니다

자동 실행에서 stderr 는 아무도 안 본다. 그래서 경고의 정본은 **실행 요약의 구조화된
값**이고, 여기서 그것을 사건(`event`)으로 읽어 `data/pipeline/alerts.json` 에 누적한다.
stderr 는 로그 파일로 보존만 한다.

## 누적이 필요한 이유

하루 한 번의 `source_error` 는 흔하고 대응할 필요도 적다. **사흘 연속이면 글을 잃는다** —
AI타임스 피드는 약 3일치(50건)만 준다. 한 실행만 보는 장치는 이 둘을 구분하지 못한다.

## 평가하지 않은 조건은 닫지 않는다

가부 불일치로 멈춘 실행은 수집을 하지 않았다. 그 실행에 `source_error` 가 없다고
열린 수집 경보를 닫으면 "해소"가 아니라 "안 봤다"를 해소로 적는 것이다. 그래서 갱신은
**이번 실행이 평가한 종류**만 닫는다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ALERTS_SCHEMA = "pipeline-alerts/1"

INFO, WARNING, CRITICAL = "info", "warning", "critical"
_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}

# 종류
PREFLIGHT_MISMATCH = "preflight_mismatch"
ENV_NOT_READY = "env_not_ready"
LOCK_HELD = "lock_held"
SOURCE_ERROR = "source_error"
STALE_FEED = "stale_feed"
WINDOW_CAPPED = "window_capped"
NOT_DRAINED = "not_drained"
EVICTED = "evicted"
FEED_ROLLOVER = "feed_rollover"
BREAKER = "breaker"
QUARANTINED = "quarantined"
RUN_ERROR = "run_error"

#: 파이프라인이 실제로 돈 실행이 평가하는 종류.
RUN_KINDS = frozenset({
    SOURCE_ERROR, STALE_FEED, WINDOW_CAPPED, NOT_DRAINED, EVICTED, FEED_ROLLOVER, BREAKER, QUARANTINED, RUN_ERROR,
})
#: 사전 점검 단계가 평가하는 종류.
PREFLIGHT_KINDS = frozenset({PREFLIGHT_MISMATCH, LOCK_HELD})
READINESS_KINDS = frozenset({ENV_NOT_READY})

#: 닫힌 경보는 최근 이만큼만 남긴다.
CLOSED_KEEP = 50


def severity(kind: str, consecutive: int) -> str:
    """연속 실행 수에 따른 등급. 날짜 기준에는 이유가 있다 (ADR-025).

    - `source_error` · `breaker` 3회 = 심각: AI타임스 피드 깊이가 약 3일이다
    - `env_not_ready` 2회 = 심각: 같은 이유로, 이틀 못 돌면 사흘째에 잃기 시작한다
    - `window_capped` = 심각: 14일 상한에 닿았다는 것은 이미 잃고 있다는 뜻이다
    - `evicted` · `feed_rollover` = 첫 회부터 경고: 미룬 것이 아니라 **이미 잃은 것**이다.
      `not_drained` 는 아직 피드에 남아 있어 다음 실행이 집을 수 있는 상태라 1회는 정보다
    """
    if kind in (PREFLIGHT_MISMATCH, WINDOW_CAPPED, RUN_ERROR):
        return CRITICAL
    if kind == ENV_NOT_READY:
        return CRITICAL if consecutive >= 2 else WARNING
    if kind in (SOURCE_ERROR, BREAKER):
        if consecutive >= 3:
            return CRITICAL
        return WARNING if consecutive >= 2 or kind == BREAKER else INFO
    if kind == NOT_DRAINED:
        return WARNING if consecutive >= 2 else INFO
    if kind in (STALE_FEED, QUARANTINED, EVICTED, FEED_ROLLOVER):
        return WARNING
    return INFO


def event(kind: str, detail: str, source: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "source": source, "detail": detail}


def key_of(ev: dict[str, Any]) -> str:
    return f"{ev['kind']}|{ev.get('source') or '-'}"


def events_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """실행 요약(`RunReport.to_dict()`)에서 경보 사건을 뽑는다. LLM 호출 없음."""
    out: list[dict[str, Any]] = []
    for name, fetch in (report.get("fetch") or {}).items():
        status = fetch.get("status")
        if status not in (None, "ok", "empty"):
            out.append(event(SOURCE_ERROR, f"수집 실패 [{status}] {fetch.get('detail') or ''}".strip(), name))
        silent = fetch.get("silent")
        if silent:
            out.append(event(
                STALE_FEED,
                f"최신 발행일 {silent.get('newest')} ({silent.get('age_days')}일 전, 기준 {silent.get('max_silence_days')}일)",
                name,
            ))
    for name, window in (report.get("window") or {}).items():
        if window.get("capped"):
            out.append(event(WINDOW_CAPPED, f"창이 상한에 걸렸다 ({window.get('cutoff')}~, {window.get('span_days')}일)", name))
        if window.get("drained") is False:
            tally = (report.get("sources") or {}).get(name) or {}
            anchor = "창 기준점이 앞으로 가지 않는다" if window.get("window_drained") is False else "창 기준점은 간다"
            out.append(event(
                NOT_DRAINED,
                f"미룬 항목 게이트 {tally.get('deferred_gate', 0)} · 미확정 {tally.get('undated_deferred', 0)}"
                f" · 추출 {tally.get('deferred_extraction', 0)} — {anchor}",
                name,
            ))
    for name, tally in (report.get("sources") or {}).items():
        gate, extraction = tally.get("evicted_gate", 0), tally.get("evicted_extraction", 0)
        if gate or extraction:
            out.append(event(
                EVICTED,
                f"처리되지 않고 피드 밖으로 밀려남 — 게이트 대기 {gate} · 추출 대기 {extraction}",
                name,
            ))
        if tally.get("feed_rollover"):
            out.append(event(
                FEED_ROLLOVER,
                "직전 실행의 피드 맨 앞 항목이 모두 사라졌다 — 그 사이 본 적 없는 항목을 잃었을 수 있다(건수 모름)",
                name,
            ))
    for stage in report.get("breaker_tripped") or []:
        out.append(event(BREAKER, f"{stage}: 전송 오류 연속 — 엔드포인트에는 닿았으므로 장애다", None))
    for name, tally in (report.get("sources") or {}).items():
        if tally.get("quarantined_now"):
            out.append(event(QUARANTINED, f"이번 실행에서 격리 {tally['quarantined_now']}건", name))
    return out


# ---------------------------------------------------------------------------
# 누적 상태
# ---------------------------------------------------------------------------
def empty_state() -> dict[str, Any]:
    return {"schema": ALERTS_SCHEMA, "open": {}, "closed": []}


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if data.get("schema") != ALERTS_SCHEMA:
        return empty_state()
    data.setdefault("open", {})
    data.setdefault("closed", [])
    return data


def save_state(state: dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_state(
    state: dict[str, Any],
    events: Iterable[dict[str, Any]],
    *,
    run_id: str,
    now: str,
    evaluated: Iterable[str],
) -> list[dict[str, Any]]:
    """사건을 누적하고 **이번 실행에서 등급이 오르거나 새로 생긴** 경보를 돌려준다.

    `evaluated` 에 든 종류 중 이번에 사건이 없는 열린 경보는 해소로 보고 닫는다.
    평가하지 않은 종류는 그대로 둔다 (모듈 설명).
    """
    evaluated = set(evaluated)
    seen: set[str] = set()
    raised: list[dict[str, Any]] = []
    for ev in events:
        key = key_of(ev)
        if key in seen:
            continue
        seen.add(key)
        prev = state["open"].get(key)
        consecutive = (prev["consecutive"] + 1) if prev else 1
        level = severity(ev["kind"], consecutive)
        alert = {
            "kind": ev["kind"],
            "source": ev.get("source"),
            "detail": ev["detail"],
            "first_seen": prev["first_seen"] if prev else now,
            "last_seen": now,
            "last_run": run_id,
            "consecutive": consecutive,
            "severity": level,
        }
        state["open"][key] = alert
        if prev is None or _RANK[level] > _RANK[prev["severity"]]:
            raised.append(alert)
    for key in [k for k, a in state["open"].items() if a["kind"] in evaluated and k not in seen]:
        alert = state["open"].pop(key)
        state["closed"].append({**alert, "closed_at": now, "closed_by_run": run_id})
    state["closed"] = state["closed"][-CLOSED_KEEP:]
    return raised


def open_alerts(state: dict[str, Any]) -> list[dict[str, Any]]:
    """등급 높은 순, 같으면 오래된 순."""
    return sorted(state["open"].values(), key=lambda a: (-_RANK[a["severity"]], a["first_seen"]))


def worst(state: dict[str, Any]) -> str | None:
    alerts = open_alerts(state)
    return alerts[0]["severity"] if alerts else None
