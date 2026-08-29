"""파이프라인 이벤트 기록 인터페이스.

이번 단계에서는 **인터페이스와 no-op 구현만** 둔다. 실제 기록(JSON Lines,
Langfuse trace)은 `logging.py` / `tracing.py` 구현 시 붙인다.

D-008 원칙: 관측은 선택적 의존성이다. 기록 대상이 설정되지 않았어도 파이프라인은
그대로 돌아야 하므로, 기본 구현이 `NullObserver` 이고 아무것도 하지 않는다.

관련성 게이트가 버린 항목은 **그대로 사라지면 안 된다.** 게이트가 잘못 버렸는지
확인할 방법이 없으면 재현율을 정밀도보다 우선한다는 결정(schema.md "수집 대상
범위")을 검증할 수 없다. 그래서 스킵도 이벤트로 남긴다. (결정 로그 D-016)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SkipRecord:
    """관련성 게이트에서 걸러진 항목 1건.

    나중에 표본 검토로 게이트의 오탐(잘못 버림)을 찾아내려면 최소한
    "무엇을, 왜, 어떤 모델이, 언제" 버렸는지가 있어야 한다.
    """

    url: str
    title: str
    source_name: str
    reason: str
    stage: str = "relevance_gate"
    model: str | None = None
    prompt_name: str | None = None
    decided_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, object]:
        """JSON Lines 로 남기기 위한 평평한 표현."""
        return {
            "stage": self.stage,
            "url": self.url,
            "title": self.title,
            "source_name": self.source_name,
            "reason": self.reason,
            "model": self.model,
            "prompt_name": self.prompt_name,
            "decided_at": self.decided_at.isoformat(),
        }


@runtime_checkable
class PipelineObserver(Protocol):
    """파이프라인이 관측 레이어에 말을 거는 유일한 통로."""

    def record_skip(self, record: SkipRecord) -> None:
        """게이트에서 걸러진 항목을 기록한다. 실패해도 예외를 올리지 않는다."""
        ...


class NullObserver:
    """아무것도 하지 않는 기본 구현 (D-008)."""

    def record_skip(self, record: SkipRecord) -> None:  # noqa: ARG002
        return None


class InMemoryObserver:
    """테스트와 CLI 확인용. 기록을 메모리에 모아둔다."""

    def __init__(self) -> None:
        self.skips: list[SkipRecord] = []

    def record_skip(self, record: SkipRecord) -> None:
        self.skips.append(record)


# TODO(logging.py): JSON Lines 파일로 남기는 FileObserver.
#   - 경로는 config.yaml 에서 주입, 기본은 logs/skips.jsonl
#   - 쓰기 실패가 파이프라인을 죽이면 안 된다 (try/except + stderr 경고)
# TODO(tracing.py): Langfuse 로 게이트 판정을 span 으로 남기는 구현.
#   - LANGFUSE_* 가 비어 있으면 NullObserver 로 대체
