"""매일 실행의 수용 판정 — 발행일 창 · 미확정 발행일 · 갱신 멈춤 (ADR-023).

## 창은 "처음 받아들이는 항목"에만 건다

재실행 중복은 원장이 `doc_id` 로 이미 거른다(ADR-022). 그래서 다시 보는 비용은 0 이고,
창이 실제로 막는 것은 **원장에 처음 들어오는 범위** 하나다. 원장에 이미 있는 항목
(게이트 오류로 재시도를 기다리는 것, 추출 대기)은 날짜와 무관하게 이어서 처리한다 —
창을 걸면 재시도를 기다리던 항목이 날짜가 지났다는 이유로 버려진다.

## 창 길이

    기준점 없음(첫 실행)       span = lookback_days
    기준점 있음                span = min(max(lookback_days, 기준점 이후 일수 + overlap_days), max_lookback_days)

**기준점은 소스별 "마지막 완결 실행"이다.** 완결은 수집이 실패하지 않았고, 창을 통과한
항목 중 상한·차단기로 **게이트를** 미룬 것과 미확정 초과분이 0 인 실행이다(실행 요약의
`window_drained`). 미룬 항목이 있으면 기준점이 앞으로 가지 않아 그 항목들이 창 밖으로
밀려나지 않는다. 추출 대기는 보지 않는다 — 이미 원장에 있어 창과 무관하다. 추출 대기까지
보는 완결은 `drained` 이고, 그건 경보용이다 (pipeline.backlog). 전역 기준점이 아닌
이유: 한 소스만 죽어 있던 동안 다른 소스의 성공이 기준점을 옮기면 그 기간을 잃는다.

날짜는 전부 **UTC 날짜**다 — `published_at` 이 UTC 날짜이기 때문이다 (ADR-023).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 창 판정 결과. 앞의 둘은 받아들이고, 뒤의 둘은 이번 실행에서 거른다.
IN_WINDOW = "in_window"
UNDATED_ADMITTED = "undated_admitted"
OUT_OF_WINDOW = "out_of_window"
UNDATED_DEFERRED = "undated_deferred"
REFUSED = (OUT_OF_WINDOW, UNDATED_DEFERRED)

# 기록은 있지만 완결 실행이 창 상한 안에 없다 — 상한까지 넓힌다.
NO_DRAINED_RUN = date.min


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class WindowPolicy:
    lookback_days: int
    overlap_days: int
    max_lookback_days: int
    undated_max_per_run: int

    def __post_init__(self) -> None:
        for name in ("lookback_days", "overlap_days", "max_lookback_days", "undated_max_per_run"):
            if getattr(self, name) < 0:
                raise ValueError(f"pipeline.window.{name} 는 0 이상이어야 합니다")
        if self.lookback_days > self.max_lookback_days:
            raise ValueError("pipeline.window: lookback_days 가 max_lookback_days 보다 큽니다")


def window_policy(config: dict[str, Any]) -> WindowPolicy | None:
    """config 의 `pipeline.window`. **없으면 창이 꺼진다**(전량 수용) — 되돌리는 방법이다."""
    section = (config.get("pipeline") or {}).get("window")
    if not section:
        return None
    return WindowPolicy(
        lookback_days=int(section["lookback_days"]),
        overlap_days=int(section["overlap_days"]),
        max_lookback_days=int(section["max_lookback_days"]),
        undated_max_per_run=int(section["undated_max_per_run"]),
    )


@dataclass(frozen=True)
class SourceWindow:
    today: date
    cutoff: date
    span_days: int
    anchor: date | None
    capped: bool

    def summary(self) -> dict[str, Any]:
        if self.anchor is None:
            anchor = None
        elif self.anchor == NO_DRAINED_RUN:
            anchor = "none_drained"
        else:
            anchor = self.anchor.isoformat()
        return {
            "today": self.today.isoformat(),
            "cutoff": self.cutoff.isoformat(),
            "span_days": self.span_days,
            "anchor": anchor,
            "capped": self.capped,
        }


def source_window(policy: WindowPolicy, *, today: date, anchor: date | None) -> SourceWindow:
    if anchor is None:
        span, capped = policy.lookback_days, False
    else:
        since = (today - anchor).days if anchor != NO_DRAINED_RUN else policy.max_lookback_days + 1
        wanted = max(policy.lookback_days, since + policy.overlap_days)
        span, capped = min(wanted, policy.max_lookback_days), wanted > policy.max_lookback_days
    return SourceWindow(today=today, cutoff=today - timedelta(days=span), span_days=span, anchor=anchor, capped=capped)


def last_drained(runs_dir: Path | None, source_name: str, *, today: date, policy: WindowPolicy) -> date | None:
    """이 소스의 마지막 완결 실행 날짜(UTC). 기록 자체가 없으면 None(첫 실행).

    기록은 있는데 창 상한 안에 완결 실행이 없으면 `NO_DRAINED_RUN` — 상한까지 넓힌다.
    상한보다 오래된 실행은 창 길이에 영향이 없으므로 거기서 읽기를 멈춘다.
    """
    if runs_dir is None or not runs_dir.exists():
        return None
    floor = today - timedelta(days=policy.max_lookback_days + policy.overlap_days)
    seen = False
    for path in sorted(runs_dir.glob("pipeline-*.json"), reverse=True):
        try:
            record = (json.loads(path.read_text(encoding="utf-8")).get("window") or {}).get(source_name)
        except (OSError, ValueError):
            continue  # 깨진 요약 하나가 창 계산을 막지 않는다
        if not record:
            continue
        seen = True
        run_day = date.fromisoformat(record["today"])
        if run_day < floor:
            break
        # `window_drained` 가 생기기 전의 기록은 `drained` 가 같은 뜻(게이트 쪽 완결)이다.
        if record.get("window_drained", record.get("drained")):
            return run_day
    return NO_DRAINED_RUN if seen else None


class Admission:
    """한 실행·한 소스의 수용 판정. 미확정 발행일 건수를 센다."""

    def __init__(self, policy: WindowPolicy, window: SourceWindow):
        self.policy = policy
        self.window = window
        self.undated_admitted = 0

    def check(self, published_at: date | None) -> str:
        """원장에 없는 항목 하나를 판정한다. `REFUSED` 에 든 값이면 이번 실행에서 거른다.

        발행일이 없거나 내일보다 뒤이면 **미확정**이다. 창 판정 없이 피드 순서 앞에서부터
        `undated_max_per_run` 건까지 받는다 — 한 번 받은 항목은 원장에 들어가 다시
        부르지 않으므로, 전부 버리지도(소스 소실) 전부 받지도(아카이브 폭주) 않는다.
        """
        if published_at is None or published_at > self.window.today + timedelta(days=1):
            if self.undated_admitted < self.policy.undated_max_per_run:
                self.undated_admitted += 1
                return UNDATED_ADMITTED
            return UNDATED_DEFERRED
        return IN_WINDOW if published_at >= self.window.cutoff else OUT_OF_WINDOW


def check_silence(
    published: Iterable[date | None], *, today: date, max_silence_days: int | None
) -> dict[str, Any] | None:
    """가장 최근 발행일이 `max_silence_days` 보다 오래됐으면 그 사실을, 아니면 None.

    200 을 주면서 내용이 안 늘어나는 피드는 수집 실패 구분(D-103)에 걸리지 않는다.
    ZDNet Korea 의 레거시 경로가 2024-05-10 에 멈춘 채 200 을 주고 있었다 (ADR-023).
    날짜 있는 항목이 없으면 판정하지 않는다 — 그건 미확정 발행일 쪽 문제다.
    """
    if max_silence_days is None:
        return None
    dated = [d for d in published if d is not None and d <= today + timedelta(days=1)]
    if not dated:
        return None
    newest = max(dated)
    age = (today - newest).days
    if age <= int(max_silence_days):
        return None
    return {"newest": newest.isoformat(), "age_days": age, "max_silence_days": int(max_silence_days)}
