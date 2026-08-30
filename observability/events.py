"""파이프라인 이벤트 기록.

D-008 원칙: 관측은 **선택적 의존성**이다. 기록 대상이 설정되지 않았어도
파이프라인은 그대로 돌아야 하므로 기본 구현이 `NullObserver` 이고, 파일에 쓰는
`JSONLObserver` 도 쓰기에 실패하면 경고만 남기고 예외를 올리지 않는다.

## 남기는 것 두 가지

**스킵** (`SkipRecord`) — 관련성 게이트가 버린 항목. 그대로 사라지면 게이트가
잘못 버렸는지 확인할 방법이 없고, 재현율을 정밀도보다 우선한다는 결정
(schema.md "수집 대상 범위")을 검증할 수 없다. (D-016)

**미등록 기업** (`UnknownCompanyRecord`) — `normalize.py` 가 `company_aliases`
에서 찾지 못한 표기. 사전을 보강할 대상을 쌓아 두는 **작업 큐**다. (D-035)

## 두 파일의 쓰기 방식이 다르다 (D-036)

| 파일 | 방식 | 이유 |
|---|---|---|
| `skips.jsonl` | append-only | **감사 로그**. 한 줄이 한 사건이고 근거 텍스트가 매번 다르다. 나중에 표본으로 뽑아 읽는 게 목적이라 사건을 합치면 안 된다 |
| `unknown_companies.jsonl` | upsert | **작업 큐**. "어떤 이름을 사전에 넣을까"가 목적이라 이름당 한 줄이 자연스럽고, `occurrence_count` 로 정렬해 우선순위를 매긴다 |
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

# 미등록 기업을 이름당 한 줄로 합칠 때 쓰는 키. 정규화와 **같은 동치 관계**를
# 써야 한다 — `normalization_key` 가 흡수하는 차이(대소문자·공백)는 alias 사전도
# 똑같이 흡수하므로, 같은 키를 가진 두 표기는 사전에 한 줄만 추가하면 둘 다
# 해결된다. 여기서 키 규칙을 따로 적으면 그게 곧 드리프트 지점이 된다.
#
# `extraction/normalize.py` 는 stdlib + yaml 만 쓰는 잎(leaf) 모듈이라 이 import
# 로 순환이 생기지 않는다. **반대 방향(normalize -> observability)은 만들지
# 않는다** — 만드는 순간 여기가 순환이 된다. 미등록 기업을 normalize 안에서
# 기록하지 않는 이유는 그 외에도 더 있다(D-035).
from extraction.normalize import normalization_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "observability" / "logs"
SKIPS_FILENAME = "skips.jsonl"
UNKNOWN_COMPANIES_FILENAME = "unknown_companies.jsonl"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime:
    """ISO 문자열을 되읽는다. 깨져 있으면 지금 시각으로 대체한다.

    손으로 고친 로그 파일 때문에 파이프라인이 죽으면 안 된다.
    """
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return _utcnow()


# ---------------------------------------------------------------------------
# 레코드
# ---------------------------------------------------------------------------
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


@dataclass(frozen=True)
class UnknownCompanyRecord:
    """`company_aliases` 에서 찾지 못한 기업/기관 표기 1건.

    이건 오류 기록이 아니라 **사전 보강 큐**다. `normalize.py` 가 추측 매칭을
    하지 않기로 한 이상(D-025) 미등록 표기는 계속 나오고, 그중 무엇을 사전에
    넣을지는 사람이 정해야 한다. 그 판단에 필요한 게 "몇 번이나 나왔는가"라서
    `occurrence_count` 가 이 레코드의 핵심 필드다.

    합쳐질 때는 **첫 등장이 이긴다** — `raw_name` / `source_article` /
    `first_seen` 은 처음 본 값을 유지하고 `occurrence_count` 만 늘어난다.
    """

    raw_name: str
    source_article: str
    first_seen: datetime = field(default_factory=_utcnow)
    occurrence_count: int = 1

    @property
    def key(self) -> str:
        """같은 이름인지 판정하는 키. 대소문자·공백 차이만 흡수한다."""
        return normalization_key(self.raw_name)

    def to_dict(self) -> dict[str, object]:
        return {
            "raw_name": self.raw_name,
            "source_article": self.source_article,
            "first_seen": self.first_seen.isoformat(),
            "occurrence_count": self.occurrence_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UnknownCompanyRecord":
        """JSONL 한 줄을 되읽는다. upsert 를 위해 필요하다."""
        count = data.get("occurrence_count", 1)
        return cls(
            raw_name=str(data["raw_name"]),
            source_article=str(data.get("source_article") or ""),
            first_seen=_parse_datetime(data.get("first_seen")),
            occurrence_count=int(count) if isinstance(count, (int, float, str)) else 1,
        )


# ---------------------------------------------------------------------------
# 인터페이스
# ---------------------------------------------------------------------------
@runtime_checkable
class PipelineObserver(Protocol):
    """파이프라인이 관측 레이어에 말을 거는 유일한 통로.

    모든 메서드는 **예외를 올리지 않는다.** 관측 실패가 파이프라인 실패가 되면
    D-008 이 무의미해진다.
    """

    def record_skip(self, record: SkipRecord) -> None:
        """게이트에서 걸러진 항목을 기록한다."""
        ...

    def record_unknown_company(self, record: UnknownCompanyRecord) -> None:
        """정규화에 실패한 기업/기관 표기를 기록한다."""
        ...


class NullObserver:
    """아무것도 하지 않는 기본 구현 (D-008)."""

    def record_skip(self, record: SkipRecord) -> None:  # noqa: ARG002
        return None

    def record_unknown_company(self, record: UnknownCompanyRecord) -> None:  # noqa: ARG002
        return None


class InMemoryObserver:
    """테스트와 CLI 확인용. 기록을 메모리에 모아둔다.

    `unknown_companies` 는 파일 구현과 달리 **합치지 않는다** — 한 번의 실행에서
    무엇이 몇 번 나왔는지 그대로 보는 게 CLI 에서는 더 쓸모 있다.
    """

    def __init__(self) -> None:
        self.skips: list[SkipRecord] = []
        self.unknown_companies: list[UnknownCompanyRecord] = []

    def record_skip(self, record: SkipRecord) -> None:
        self.skips.append(record)

    def record_unknown_company(self, record: UnknownCompanyRecord) -> None:
        self.unknown_companies.append(record)


class MultiObserver:
    """여러 observer 에 같은 이벤트를 흘린다.

    CLI 가 화면 출력용 `InMemoryObserver` 와 파일 기록용 `JSONLObserver` 를
    동시에 쓰기 위해 필요하다. 하나가 실패해도 나머지는 계속 받는다.
    """

    def __init__(self, *observers: PipelineObserver) -> None:
        self.observers = observers

    def record_skip(self, record: SkipRecord) -> None:
        for observer in self.observers:
            observer.record_skip(record)

    def record_unknown_company(self, record: UnknownCompanyRecord) -> None:
        for observer in self.observers:
            observer.record_unknown_company(record)


# ---------------------------------------------------------------------------
# JSON Lines 파일 구현
# ---------------------------------------------------------------------------
class JSONLObserver:
    """이벤트를 JSON Lines 파일로 남긴다.

    쓰기 실패(권한, 디스크, 잠긴 파일)는 **경고만 남기고 삼킨다.** 관측 때문에
    추출 결과를 잃는 건 앞뒤가 바뀐 일이다 (D-008).

    스킵은 append, 미등록 기업은 upsert 다. 이유는 모듈 docstring 참고 (D-036).
    """

    def __init__(
        self,
        log_dir: Path | str = DEFAULT_LOG_DIR,
        *,
        skips_filename: str = SKIPS_FILENAME,
        unknown_companies_filename: str = UNKNOWN_COMPANIES_FILENAME,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.skips_path = self.log_dir / skips_filename
        self.unknown_companies_path = self.log_dir / unknown_companies_filename

    # -- 스킵: append-only --------------------------------------------------
    def record_skip(self, record: SkipRecord) -> None:
        self._append(self.skips_path, record.to_dict())

    def load_skips(self) -> list[SkipRecord]:
        """남긴 스킵을 되읽는다 (표본 검토용)."""
        return [
            SkipRecord(
                url=str(row.get("url", "")),
                title=str(row.get("title", "")),
                source_name=str(row.get("source_name", "")),
                reason=str(row.get("reason", "")),
                stage=str(row.get("stage", "relevance_gate")),
                model=row.get("model"),
                prompt_name=row.get("prompt_name"),
                decided_at=_parse_datetime(row.get("decided_at")),
            )
            for row in self._read_lines(self.skips_path)
        ]

    # -- 미등록 기업: upsert ------------------------------------------------
    def record_unknown_company(self, record: UnknownCompanyRecord) -> None:
        """같은 이름이 이미 있으면 `occurrence_count` 만 올린다.

        읽고-고쳐-다시 쓰기라 append 보다 비싸지만, 이 파일은 사람이 보는
        작업 큐이고 규모가 수십 줄이라 비용이 문제가 되지 않는다. 대신 다시
        쓰기는 원자적으로 해서, 중간에 죽어도 큐가 반토막 나지 않게 한다.
        """
        rows = self.load_unknown_companies()
        existing = rows.get(record.key)
        if existing is None:
            rows[record.key] = record
        else:
            # 첫 등장이 이긴다 — 카운트만 누적한다.
            rows[record.key] = replace(
                existing,
                occurrence_count=existing.occurrence_count + record.occurrence_count,
            )
        self._rewrite(self.unknown_companies_path, [r.to_dict() for r in rows.values()])

    def load_unknown_companies(self) -> dict[str, UnknownCompanyRecord]:
        """`{정규화키: 레코드}`. 파일에 적힌 순서(=처음 본 순서)를 유지한다."""
        rows: dict[str, UnknownCompanyRecord] = {}
        for raw in self._read_lines(self.unknown_companies_path):
            try:
                record = UnknownCompanyRecord.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                self._warn(f"{self.unknown_companies_path.name}: 줄을 건너뜁니다 ({exc})")
                continue
            existing = rows.get(record.key)
            # 손으로 편집해 같은 이름이 두 줄이 됐다면 여기서 합쳐 준다.
            rows[record.key] = (
                record
                if existing is None
                else replace(
                    existing,
                    occurrence_count=existing.occurrence_count + record.occurrence_count,
                )
            )
        return rows

    # -- 파일 입출력 --------------------------------------------------------
    def _append(self, path: Path, payload: dict[str, object]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._warn(f"{path} 기록 실패 ({type(exc).__name__}: {exc})")

    def _read_lines(self, path: Path) -> list[dict[str, Any]]:
        """JSONL 을 읽는다. 파일이 없으면 빈 목록, 깨진 줄은 건너뛴다."""
        rows: list[dict[str, Any]] = []
        try:
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        self._warn(f"{path.name}:{lineno} JSON 이 아닙니다. 건너뜁니다")
                        continue
                    if isinstance(parsed, dict):
                        rows.append(parsed)
        except FileNotFoundError:
            return []
        except OSError as exc:
            self._warn(f"{path} 읽기 실패 ({type(exc).__name__}: {exc})")
            return []
        return rows

    def _rewrite(self, path: Path, payloads: list[dict[str, object]]) -> None:
        """같은 디렉터리의 임시 파일에 쓰고 `os.replace` 로 갈아 끼운다.

        같은 디렉터리를 쓰는 이유는 `obsidian_writer.writer.atomic_write` 와
        같다 — `os.replace` 는 같은 볼륨 안에서만 원자적이다.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".jsonl")
        except OSError as exc:
            self._warn(f"{path} 기록 실패 ({type(exc).__name__}: {exc})")
            return

        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                for payload in payloads:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            self._warn(f"{path} 기록 실패 ({type(exc).__name__}: {exc})")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _warn(message: str) -> None:
        print(f"[warn] observability: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 설정 -> observer
# ---------------------------------------------------------------------------
def observer_from_config(
    config: Mapping[str, Any] | None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> PipelineObserver:
    """`config.yaml: observability` 를 읽어 observer 를 만든다.

    `enabled: false` 거나 블록 자체가 없으면 `NullObserver` 로 폴백한다.
    관측을 끄는 것이 파이프라인을 끄는 것이 되어서는 안 된다 (D-008).
    """
    settings = (config or {}).get("observability") or {}
    if not settings.get("enabled", False):
        return NullObserver()

    log_dir = Path(settings.get("log_dir") or DEFAULT_LOG_DIR)
    if not log_dir.is_absolute():
        log_dir = project_root / log_dir

    return JSONLObserver(
        log_dir,
        skips_filename=settings.get("skips_filename") or SKIPS_FILENAME,
        unknown_companies_filename=(
            settings.get("unknown_companies_filename") or UNKNOWN_COMPANIES_FILENAME
        ),
    )


# TODO(tracing.py): Langfuse 로 게이트 판정을 span 으로 남기는 구현.
#   - LANGFUSE_* 가 비어 있으면 NullObserver 로 대체
