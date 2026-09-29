"""미룬 항목의 행방 — 피드 밖으로 밀려나 **처리되지 않고 사라진** 것을 센다.

## 왜 따로 세나

루프는 **이번 피드에 있는 항목**만 돈다. 상한 때문에 미룬 항목(게이트 대기·추출 대기)은
다음 실행에서 피드에 남아 있어야 다시 집힌다. 피드가 얕으면 그 전에 밀려난다 —
AI타임스 피드 50건은 약 28시간치다(2026-09-29 실측). 밀려난 항목은 어느 카운터에도
안 잡히고, 그 다음 실행은 미룬 것이 없으니 "완결"로 보인다. **사라진 것이 다 처리한
것의 모양으로 나온다** (`judge_human_gap`, gzip, ZDNet 에 이은 네 번째 사례).

## 무엇을 세나

- **밀려남(`evicted_*`)**: 직전 기록에서 미뤘던 doc_id 중, 지금 피드에 없고 원장상 여전히
  대기인 것. 손실이 난 **다음 실행에서** 센다 — 난 순간에는 알 수 없다.
- **피드 넘김(`feed_rollover`)**: 직전 기록의 피드 맨 앞 항목들이 지금 피드에 하나도 없다.
  그 사이 피드 깊이보다 많이 들어왔다는 뜻이고, 한 번도 못 본 항목이 있을 수 있다.
  **이건 건수를 모른다** — 본 적이 없으니 셀 대상이 없다. 있다는 사실만 남긴다.

기록은 실행 요약의 `backlog` 에 있다. 수집이 실패한 실행은 기록을 남기지 않으므로,
다음 실행은 그 소스의 **마지막 기록**과 비교한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 피드 넘김을 가르는 데 쓰는 맨 앞 항목 수. 하나만 보면 기사 하나가 내려가도(삭제·수정)
#: 넘김으로 읽힌다.
HEAD_SIZE = 5


@dataclass(frozen=True)
class Evictions:
    gate: tuple[str, ...]
    extraction: tuple[str, ...]
    rollover: bool


def previous_backlog(runs_dir: Path | None, source_name: str) -> dict[str, Any] | None:
    """이 소스의 가장 최근 `backlog` 기록. 없으면 None (첫 기록)."""
    if runs_dir is None or not runs_dir.exists():
        return None
    for path in sorted(runs_dir.glob("pipeline-*.json"), reverse=True):
        try:
            record = (json.loads(path.read_text(encoding="utf-8")).get("backlog") or {}).get(source_name)
        except (OSError, ValueError):
            continue  # 깨진 요약 하나가 판정을 막지 않는다
        if record is not None:
            return record
    return None


def find_evictions(
    previous: dict[str, Any] | None,
    feed_ids: Iterable[str],
    *,
    still_pending: Callable[[str], bool],
) -> Evictions:
    """직전 기록과 지금 피드를 비교한다. `still_pending(doc_id)` 는 원장상 아직 대기인가."""
    if not previous:
        return Evictions((), (), False)
    feed = set(feed_ids)

    def gone(ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(i for i in ids if i not in feed and still_pending(i))

    head = previous.get("head") or []
    rollover = bool(head) and bool(feed) and not feed.intersection(head)
    return Evictions(gone(previous.get("gate") or []), gone(previous.get("extraction") or []), rollover)
