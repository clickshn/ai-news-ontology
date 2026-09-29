"""파이프라인 원장 — doc_id 당 단계 상태 (ADR-022).

## 왜 보존소와 따로 두나

보존소(`data/extractions/`)는 **추출에 성공한 것**만 담는다. 게이트 스킵이나
추출 실패를 거기 넣으려면 `extraction: null` 항목이 생기고, 그 순간 exporter 와
계약 레코드가 깨진다. 그런데 스킵과 실패를 어디에도 남기지 않으면 재실행마다
같은 항목을 다시 게이트에 넣고, 스키마 실패는 temperature 0 이라 **같은 실패를
매번 사서** 반복한다.

그래서 단계 완료는 **그 단계의 산출물을 가진 층**이 판정한다.

| 단계 | 판정 층 |
|---|---|
| 게이트 | 이 원장 (`gate` + `gate_key`) |
| 추출 | 보존소 |
| 적재 | Vault 색인 (`obsidian_writer.vault_index`) |

원장의 `load` 는 판정이 아니라 **기록**이다. 예외 하나 — 원장에 `written` 이
있는데 Vault 에 노트가 없으면 사용자가 지운 것으로 보고 다시 만들지 않는다.

## 취급 기준

게이트 근거 문장과 오류 메시지가 **평문으로 남는다.** `docs/governance.md` 의
로컬 산출물 (가) 기준을 따르고, `data/` 는 `.gitignore` 대상이다. `skips.jsonl`
(append-only 감사 로그, D-036)과 달리 여기는 doc_id 당 upsert 하는 **상태**다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from export.exporter import KST
from export.store import PROJECT_ROOT, safe_filename

DEFAULT_LEDGER_DIR = PROJECT_ROOT / "data" / "pipeline" / "ledger"
LEDGER_SCHEMA = "pipeline-ledger/1"

# temperature 0 에서 같은 입력의 스키마 실패는 반복된다. 두 번 실패하면 세 번째도
# 같은 결과일 가능성이 높으므로 격리하고, 풀려면 사람이 명시한다 (ADR-022).
QUARANTINE_AFTER = 2


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


@dataclass
class LedgerEntry:
    doc_id: str
    url: str
    title: str
    source_name: str
    # 게이트 — `gate_key` 가 현재 설정과 다르면 판정이 없는 것으로 본다.
    gate: dict[str, Any] | None = None
    gate_key: str | None = None
    gate_error: str | None = None
    # 추출 — 스키마 실패만 센다. 전송 오류는 입력과 무관하므로 세지 않는다.
    extract_failures: int = 0
    extract_error: str | None = None
    quarantined: bool = False
    # 적재 — status: written | exists | deleted_by_user | failed
    load: dict[str, Any] | None = None
    updated_at: str = field(default_factory=now_iso)

    def gate_verdict(self, gate_key: str) -> bool | None:
        """현재 게이트 설정에서 유효한 판정. 없거나 설정이 바뀌었으면 None."""
        if self.gate is None or self.gate_key != gate_key:
            return None
        return bool(self.gate["is_relevant"])

    def record_extract_failure(self, error: str) -> None:
        self.extract_failures += 1
        self.extract_error = error
        if self.extract_failures >= QUARANTINE_AFTER:
            self.quarantined = True

    def to_dict(self) -> dict[str, Any]:
        return {"schema": LEDGER_SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerEntry":
        fields = {k: v for k, v in data.items() if k != "schema"}
        return cls(**fields)


class Ledger:
    """`data/pipeline/ledger/` 에 doc_id 당 한 파일. 원자적 upsert."""

    def __init__(self, directory: Path | str = DEFAULT_LEDGER_DIR) -> None:
        self.directory = Path(directory)

    def path_for(self, doc_id: str) -> Path:
        return self.directory / safe_filename(doc_id)

    def get(self, doc_id: str) -> LedgerEntry | None:
        path = self.path_for(doc_id)
        if not path.exists():
            return None
        return LedgerEntry.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def put(self, entry: LedgerEntry) -> None:
        entry.updated_at = now_iso()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(entry.doc_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(entry.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(path)

    def __iter__(self) -> Iterator[LedgerEntry]:
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.json")):
            yield LedgerEntry.from_dict(json.loads(path.read_text(encoding="utf-8")))
