"""MARA 스냅샷 주입 — 1단계 검증 전용 경로 (`export/mara_seed.py`).

실제 MARA 레포를 읽지 않는다. `tmp_path` 에 그 모양을 흉내 낸 픽스처를 만든다 —
남의 레포 파일이 바뀔 때 이쪽 테스트가 깨지면 두 레포가 코드를 공유하지 않는다는
전제가 테스트에서부터 깨진다 (`docs/governance.md`).
"""

from __future__ import annotations

from datetime import date

import pytest

from export.doc_id import doc_id_for
from export.mara_seed import SEED_SOURCE_NAME, SeedNotFoundError, collect_seed_docs

DOC_ID = "arXiv:2412.05449v1"


@pytest.fixture
def mara(make_mara_root):
    return make_mara_root(
        [
            {
                "doc_id": DOC_ID,
                "title": "Multi-agent collaboration",
                "text": "스냅샷 쪽 초록 본문이다. " * 20,
                "locator": "abstract",
                "url": "http://arxiv.org/abs/2412.05449v1",
                "published": "2024-12-07",
            }
        ],
        [DOC_ID],
    )


class TestCollectSeedDocs:
    def test_builds_a_raw_item_from_the_snapshot(self, mara):
        item = collect_seed_docs(mara, [DOC_ID], collected_at=date(2026, 9, 17))[0]
        assert item.title == "Multi-agent collaboration"
        assert item.body.startswith("스냅샷 쪽 초록")
        assert item.published_at == date(2024, 12, 7)
        assert item.collected_at == date(2026, 9, 17)

    def test_doc_id_is_recomputed_from_the_url_not_copied(self, mara):
        """계약 §4.2 는 URL 에서 doc_id 를 만든다. 스냅샷의 값을 베끼면 규칙이 검증되지 않는다."""
        item = collect_seed_docs(mara, [DOC_ID])[0]
        assert doc_id_for(str(item.url)) == DOC_ID

    def test_source_name_marks_the_record_as_seeded(self, mara):
        """manifest 의 by_source_name 에서 원문 출처가 보여야 한다."""
        item = collect_seed_docs(mara, [DOC_ID])[0]
        assert item.source_name == SEED_SOURCE_NAME

    def test_missing_doc_id_raises_instead_of_shrinking_the_sample(self, mara):
        with pytest.raises(SeedNotFoundError, match="arXiv:9999"):
            collect_seed_docs(mara, [DOC_ID, "arXiv:9999.99999v1"])

    def test_input_order_is_preserved(self, make_mara_root):
        docs = [
            {"doc_id": "arXiv:1v1", "title": "A", "text": "a", "locator": "abstract",
             "url": "http://arxiv.org/abs/1v1", "published": "2026-01-01"},
            {"doc_id": "arXiv:2v1", "title": "B", "text": "b", "locator": "abstract",
             "url": "http://arxiv.org/abs/2v1", "published": "2026-01-02"},
        ]
        root = make_mara_root(docs, [])
        items = collect_seed_docs(root, ["arXiv:2v1", "arXiv:1v1"])
        assert [i.title for i in items] == ["B", "A"]


class TestConformanceCaveat:
    def test_seeded_ids_are_flagged_in_the_merge_verdict(self, mara):
        """계약 §4.3 의 'text 유지'가 자명하게 통과한다는 사실을 판정에 남긴다."""
        from export.conformance import PASS, check_05_arxiv_merge
        from export.mara_seed import load_snapshot

        snapshot = load_snapshot(mara)
        record = {
            "doc_id": DOC_ID,
            "source": "arxiv",
            "title": "t",
            "text": snapshot[DOC_ID]["text"],
            "url": "https://arxiv.org/abs/2412.05449v1",
            "locator": "abstract",
            "ontology": {"release_type": "Paper"},
        }
        bundle = type("B", (), {"records": [record]})()
        check = check_05_arxiv_merge(bundle, snapshot, [DOC_ID], seeded=[DOC_ID])
        assert check.status == PASS
        assert "자명하게 통과" in check.detail
