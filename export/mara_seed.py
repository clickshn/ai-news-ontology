"""1단계 검증 전용 — MARA 스냅샷에서 중복 케이스의 원문을 읽어 온다 (읽기 전용).

## 이건 파이프라인의 일부가 아니다

정상 경로에서 생산자는 자기 피드와 arXiv API 만 읽는다. 이 모듈은 계약 §12.1 의
중복 2건을 만들기 위한 **검증 전용 우회로**이고, 2단계(코퍼스 확보)에서는 쓰지 않는다.

## 왜 필요해졌나

계약 §12.1 은 중복 케이스로 `arXiv:2412.05449v1`(MARA 골든셋 GS-001 의 기대값)과
`arXiv:2605.21404v1` 을 지정한다. 그런데 **이 두 논문을 포함해 MARA 코퍼스 16건 전부가
arXiv API `id_list` 조회에서 0건으로 돌아온다**(2026-09-17 실측). `--urls` 경로로는
원문을 가져올 수 없다.

원인은 이 세션에서 확정하지 못했다 — 조사 내용과 남은 가설은
`docs/handoff/session-01.md` 에 있다. 확정될 때까지 1단계를 멈추는 대신, **원문 출처를
바꾸고 그 사실을 판정에 명시**하는 쪽을 택했다.

## 이 우회로가 검증하지 못하는 것

`doc_id` 동일화(§4.2)는 **URL 문자열에서만** 계산되므로 원문 출처와 무관하게 검증된다.
병합 후 골든셋 기대값 보존(§12.2-6)도 마찬가지다. 하지만 §4.3 의 **"병합 시 기존
스냅샷의 text 를 유지한다"** 는 여기서 **자명하게 통과한다** — 주입 레코드의 `text` 가
애초에 그 스냅샷에서 온 값이기 때문이다. 판정표에 이 한계를 함께 적는다.

**MARA 레포는 읽기만 한다.**
"""

from __future__ import annotations

import glob
import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from collectors.base import RawItem
from collectors.rss import BODY_MAX_CHARS

# 피드 소스와도, `--urls` 주입분과도 이름을 다르게 둔다. manifest 의
# by_source_name 에서 "이 레코드의 원문은 어디서 왔는가"가 그대로 보여야 한다.
SEED_SOURCE_NAME = "arXiv (MARA snapshot seed)"
SEED_TAGS = ("paper",)


class SeedNotFoundError(LookupError):
    """지정한 doc_id 가 MARA 스냅샷에 없다."""


def load_snapshot(mara_root: Path | str) -> dict[str, dict[str, Any]]:
    """MARA `data/corpus/*/*.json` 을 doc_id -> 문서로 읽는다. **쓰지 않는다.**"""
    docs: dict[str, dict[str, Any]] = {}
    pattern = str(Path(mara_root) / "data" / "corpus" / "*" / "*.json")
    for path in sorted(glob.glob(pattern)):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for doc in payload.get("documents", []):
            docs[doc["doc_id"]] = doc
    return docs


def collect_seed_docs(
    mara_root: Path | str,
    doc_ids: Iterable[str],
    *,
    collected_at: date | None = None,
) -> list[RawItem]:
    """지정한 doc_id 를 `RawItem` 으로. 하나라도 없으면 예외.

    없는 것을 조용히 건너뛰면 표본이 30건이 아닌 채로 추출 비용을 내게 된다.
    """
    snapshot = load_snapshot(mara_root)
    wanted = list(doc_ids)
    missing = [d for d in wanted if d not in snapshot]
    if missing:
        raise SeedNotFoundError(f"MARA 스냅샷에 없는 doc_id: {', '.join(missing)}")

    today = collected_at or date.today()
    items: list[RawItem] = []
    for doc_id in wanted:
        doc = snapshot[doc_id]
        published = (doc.get("published") or "")[:10] or None
        items.append(
            RawItem(
                url=doc["url"],
                title=doc["title"],
                # 계약 상한은 export 가 다시 적용하지만(§6.1), 수집 상한도 그대로
                # 통과시켜 다른 소스와 같은 모양으로 들어오게 한다.
                body=(doc.get("text") or "")[:BODY_MAX_CHARS],
                source_name=SEED_SOURCE_NAME,
                published_at=date.fromisoformat(published) if published else None,
                collected_at=today,
                tags=SEED_TAGS,
            )
        )
    return items
