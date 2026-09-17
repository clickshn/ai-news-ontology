"""출력 계약 v1 export — `ai-news-ontology` -> `multiagent-research-lab`.

계약 전문은 MARA 레포 `docs/contracts/ai-news-ontology-export-v1.md`,
결정 근거는 같은 레포 `docs/adr/ADR-018-*.md` 에 있다. 이 패키지는 그 계약을
**만족시키는 쪽**이고, 계약 문서를 런타임에 읽지 않는다 — 두 레포는 코드를
공유하지 않고 파일 형식 하나만 공유한다.
"""

from export.contract import CONTRACT_VERSION
from export.doc_id import canonical_url, doc_id_for

__all__ = ["CONTRACT_VERSION", "canonical_url", "doc_id_for"]
