"""경보 — 실행 요약에서 뽑고, 실행을 넘어 누적하고, 등급을 매긴다 (ADR-025).

## stderr 는 경고가 아니다

자동 실행에서 stderr 는 아무도 안 본다. 그래서 경고의 정본은 **실행 요약의 구조화된
값**이고, 여기서 그것을 사건(`event`)으로 읽어 `data/pipeline/alerts.json` 에 누적한다.
stderr 는 로그 파일로 보존만 한다.

## 누적이 필요한 이유

하루 한 번의 `source_error` 는 흔하고 대응할 필요도 적다. 그런데 **피드가 얕으면 한 번으로
글을 잃는다.** 한 실행만 보는 장치는 이 둘을 구분하지 못한다.

## 수집이 막힌 경보의 등급은 "잃기까지 남은 시간"으로 정한다 (ADR-025 Amendment 1)

예전 등급(3회 = 심각)은 "AI타임스 피드 약 3일치"를 전제로 했다. 실측은 **27.8시간**이었다
(session-18). 소스마다 깊이가 다르므로(arXiv 1.1시간 ~ OpenAI 10년+) 횟수로는 못 가른다.

    여유 = 피드 깊이 − 마지막 성공 수집 이후 시간 − 다음 실행까지(24시간)

여유가 0 이하면 **심각** — 다음 정기 실행 전에 사람이 돌리지 않으면 확실히 잃는다. 여유가
있으면 1회는 정보, 2회 연속부터 경고(깊은 피드라도 죽은 소스는 문제다). 깊이 기록이 없으면
예전 횟수 규칙을 쓴다. `env_not_ready` · `breaker` 는 소스 전체에 걸리므로 **가장 빠듯한
소스**로 잰다. 표본 피드(`feed_kind: sample`)는 깊이 계산에서 뺀다 — 피드가 원래 일부만 준다.

## 평가하지 않은 조건은 닫지 않는다

가부 불일치로 멈춘 실행은 수집을 하지 않았다. 그 실행에 `source_error` 가 없다고
열린 수집 경보를 닫으면 "해소"가 아니라 "안 봤다"를 해소로 적는 것이다. 그래서 갱신은
**이번 실행이 평가한 종류**만 닫는다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
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

#: 정기 실행 간격. 자동 실행은 하루 한 번이다 (`schedule.time`).
RUN_INTERVAL_HOURS = 24
#: 등급을 피드 깊이로 정하는 종류 — 수집이 막혀 글을 잃을 수 있는 것.
DEPTH_KINDS = frozenset({SOURCE_ERROR, ENV_NOT_READY, BREAKER})
#: 깊이 기록을 찾으러 거슬러 읽는 실행 요약 수의 상한.
DEPTH_LOOKBACK_RUNS = 60


def severity(kind: str, consecutive: int, loss: dict[str, Any] | None = None) -> str:
    """등급. `loss` 는 `attach_loss_clocks` 가 붙인 잃기까지의 여유 (모듈 설명).

    - `source_error` · `env_not_ready` · `breaker`: 여유 0 이하 = 심각. 여유가 있으면 1회 정보
      (`breaker` 는 경고), 2회부터 경고. 깊이 기록이 없으면 예전 횟수 규칙
    - `window_capped` = 심각: 필요한 cutoff 가 14일 상한 너머 — 이미 거르고 있다 (ADR-023 Amendment 2)
    - `evicted` · `feed_rollover` = 첫 회부터 경고: 미룬 것이 아니라 **이미 잃은 것**이다.
      `not_drained` 는 아직 피드에 남아 있어 다음 실행이 집을 수 있는 상태라 1회는 정보다
    """
    if kind in (PREFLIGHT_MISMATCH, WINDOW_CAPPED, RUN_ERROR):
        return CRITICAL
    if kind in DEPTH_KINDS and loss is not None:
        if loss["slack_hours"] <= 0:
            return CRITICAL
        return WARNING if consecutive >= 2 or kind == BREAKER else INFO
    # 깊이를 모를 때의 횟수 규칙 (예전 규칙)
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


# ---------------------------------------------------------------------------
# 잃기까지의 여유 (ADR-025 Amendment 1)
# ---------------------------------------------------------------------------
def last_depths(runs_dir: Path, sources: Iterable[str]) -> dict[str, tuple[datetime, float]]:
    """소스별 **마지막 성공 수집**의 시각과 그때 잰 피드 깊이(시간). 표본 피드는 뺀다."""
    wanted = set(sources)
    found: dict[str, tuple[datetime, float]] = {}
    paths = sorted(Path(runs_dir).glob("pipeline-*.json"), reverse=True)[:DEPTH_LOOKBACK_RUNS]
    for path in paths:
        if wanted <= set(found):
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(data["started_at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for name, fetch in (data.get("fetch") or {}).items():
            if name not in wanted or name in found:
                continue
            if fetch.get("status") not in ("ok", "empty") or fetch.get("feed_kind") == "sample":
                continue
            if fetch.get("depth_hours") is None:
                continue
            found[name] = (started, float(fetch["depth_hours"]))
    return found


def loss_clock(
    depths: dict[str, tuple[datetime, float]], *, now: datetime, source: str | None = None
) -> dict[str, Any] | None:
    """`source` 의 여유, 없으면 **가장 빠듯한** 소스의 여유. 깊이 기록이 없으면 None."""
    if source is None:
        candidates = depths
    else:
        candidates = {source: depths[source]} if source in depths else {}
    best: dict[str, Any] | None = None
    for name, (last_ok, depth) in candidates.items():
        since = round((now - last_ok).total_seconds() / 3600, 1)
        slack = round(depth - since - RUN_INTERVAL_HOURS, 1)
        clock = {"source": name, "depth_hours": depth, "hours_since_success": since, "slack_hours": slack}
        if best is None or slack < best["slack_hours"]:
            best = clock
    return best


def attach_loss_clocks(
    events: list[dict[str, Any]], *, runs_dir: Path, sources: Iterable[str], now: datetime
) -> None:
    """수집이 막힌 사건에 여유를 붙이고, 사유에 사람이 읽을 한 줄을 더한다."""
    targets = [ev for ev in events if ev["kind"] in DEPTH_KINDS]
    if not targets:
        return
    depths = last_depths(runs_dir, sources)
    for ev in targets:
        clock = loss_clock(depths, now=now, source=ev.get("source"))
        if clock is None:
            continue
        ev["loss"] = clock
        verdict = "다음 정기 실행 전에 돌리지 않으면 잃는다" if clock["slack_hours"] <= 0 else f"여유 {clock['slack_hours']}시간"
        ev["detail"] = (
            f"{ev['detail']} — {clock['source']} 피드 깊이 {clock['depth_hours']}시간,"
            f" 마지막 성공 {clock['hours_since_success']}시간 전: {verdict}"
        )


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
        # 표본 피드는 매일 넘긴다(arXiv 20건 = 제출 1.1시간치). 기록(tally)은 남기고 경보만 뺀다.
        # `evicted_*` 는 표본 피드에서도 경보다 — 우리가 미룬 항목의 손실이기 때문이다.
        is_sample = ((report.get("fetch") or {}).get(name) or {}).get("feed_kind") == "sample"
        if tally.get("feed_rollover") and not is_sample:
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
        level = severity(ev["kind"], consecutive, ev.get("loss"))
        alert = {
            "kind": ev["kind"],
            "source": ev.get("source"),
            "detail": ev["detail"],
            "first_seen": prev["first_seen"] if prev else now,
            "last_seen": now,
            "last_run": run_id,
            "consecutive": consecutive,
            "severity": level,
            **({"loss": ev["loss"]} if ev.get("loss") else {}),
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
