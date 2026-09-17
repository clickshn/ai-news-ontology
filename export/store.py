"""추출 결과 원본 보존소 — 계약 §12.3.

## 왜 있는가

파이프라인은 `NewsOntology` 를 Obsidian 노트 외에 어디에도 남기지 않는다. 그래서
export 형식이 틀렸다는 것만으로 **LLM 을 다시 부르게 된다.** 1단계 30건에서 형식
결함이 나오면 30건 비용을 또 내는 구조다.

보존소는 그 구조를 끊는다. 형식 결함은 여기 있는 원본에서 **재-export 만** 하면
되고, LLM 재호출은 **추출 로직 자체가 바뀐 경우에만** 한다.

`runner.py` 는 실행 모드와 무관하게 항상 이 보존소를 거쳐 레코드를 만든다.
"방금 추출한 결과로 바로 export" 하는 지름길을 두지 않는 이유는, 그 지름길이
있으면 재-export 경로가 실제로는 한 번도 실행되지 않은 채 남기 때문이다.

## 취급 기준

프롬프트와 LLM 응답 본문이 **평문으로 디스크에 남는다.** 투입 데이터가 공개
자료뿐이라는 전제(MARA ADR-004) 하에서 `docs/governance.md` 의 **로컬 산출물 (가)
기준**을 따른다 — 보존기간 정해진 일수 없음, 별도 접근통제 없음(로컬 파일시스템
권한), 필요 없어지면 수동 삭제. `data/` 는 `.gitignore` 대상이라 커밋되지 않지만,
**커밋되지 않는 것과 디스크에 없는 것은 다르다.**
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORE_DIR = PROJECT_ROOT / "data" / "extractions"

STORE_SCHEMA = "extraction-store/1"

# doc_id 의 ':' 는 Windows 파일명에 쓸 수 없다. 대체 규칙을 단순하게 두고
# 원래 doc_id 는 파일 **안에** 그대로 싣는다 — 파일명에서 역산하지 않는다.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(doc_id: str) -> str:
    return _UNSAFE_RE.sub("_", doc_id) + ".json"


@dataclass(frozen=True)
class StoredExtraction:
    """보존된 추출 1건. export 는 이 객체만 보고 레코드를 만든다."""

    doc_id: str
    payload: dict[str, Any]
    path: Path

    @property
    def raw_item(self) -> dict[str, Any]:
        return self.payload["raw_item"]

    @property
    def extraction(self) -> dict[str, Any]:
        return self.payload["extraction"]

    @property
    def gate(self) -> dict[str, Any] | None:
        return self.payload.get("gate")


class ExtractionStore:
    """`data/extractions/` 에 doc_id 당 한 파일.

    append 가 아니라 **doc_id 당 upsert** 다. 같은 문서를 다시 추출하면 최신
    결과가 이긴다 — 여기 있는 것은 감사 로그가 아니라 "재-export 의 입력"이고,
    한 문서에 두 버전이 있으면 어느 쪽으로 export 했는지가 모호해진다.
    (관측 로그의 append-only 원칙 D-036 과 성격이 다르다.)
    """

    def __init__(self, directory: Path | str = DEFAULT_STORE_DIR) -> None:
        self.directory = Path(directory)

    def path_for(self, doc_id: str) -> Path:
        return self.directory / safe_filename(doc_id)

    def save(self, payload: dict[str, Any]) -> Path:
        """원본을 **받은 즉시** 쓴다. 변환은 그 뒤에 한다 (D-052).

        중간에 죽어도 이미 낸 비용은 디스크에 남아 있어야 한다.
        """
        doc_id = payload["doc_id"]
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(doc_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def load(self, doc_id: str) -> StoredExtraction:
        path = self.path_for(doc_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return StoredExtraction(doc_id=payload["doc_id"], payload=payload, path=path)

    def __iter__(self) -> Iterator[StoredExtraction]:
        """저장 순서가 아니라 **파일명 정렬 순서**로 흘린다.

        export 출력의 줄 순서를 결정적으로 만들기 위해서다. 계약 §12.2-7 의
        "2회 export 결과가 바이트 단위로 같다"는 doc_id 집합만이 아니라 파일
        자체가 같아야 확인하기 쉽다.
        """
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            yield StoredExtraction(doc_id=payload["doc_id"], payload=payload, path=path)

    def doc_ids(self) -> list[str]:
        return [stored.doc_id for stored in self]
