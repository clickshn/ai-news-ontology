"""1단계 자체검사 — 계약 §12.2 의 13항목과 §7 위반 케이스.

⚠️ 이 테스트가 확인하는 것은 **검사기가 위반을 잡는가**이지, MARA 로더가 같은
판정을 내리는가가 아니다. 계약 §7 검증의 정본은 MARA 쪽 구현이고, 최종 판정은
0.5b 에서 다시 내린다 (`export/README.md` 의 "이 검사기의 한계").
"""

from __future__ import annotations

import pytest

from export.conformance import (
    FAIL,
    PASS,
    PENDING,
    load_bundle,
    run_checks,
    simulate_merge,
)
from export.exporter import build_records, write_export
from export.runner import prompt_sha256

ARXIV_DUP = "arXiv:2412.05449v1"
ARXIV_DUP_URL = "http://arxiv.org/abs/2412.05449v1"


@pytest.fixture
def snapshot_docs():
    """MARA 스냅샷을 흉내 낸 2건. 실제 스냅샷과 같은 모양(`http://` URL)이다."""
    return [
        {
            "doc_id": ARXIV_DUP,
            "title": "Multi-agent collaboration",
            "text": "MARA 스냅샷 쪽 초록. 피드 발췌보다 길다." * 10,
            "locator": "abstract",
            "url": ARXIV_DUP_URL,
        },
        {
            "doc_id": "arXiv:2605.21404v1",
            "title": "Benchmark audit",
            "text": "또 다른 초록." * 10,
            "locator": "abstract",
            "url": "http://arxiv.org/abs/2605.21404v1",
        },
    ]


def _bundle(tmp_path, store, config_version=1, export_id="fixed"):
    records = build_records(store, config_version=config_version)
    write_export(records, out_dir=tmp_path / "corpus", export_id=export_id, config_version=config_version)
    return load_bundle(tmp_path / "corpus", export_id)


def _checks(bundle, store, mara_root, *, injected=(), expected_total=None):
    return {
        check.number: check
        for check in run_checks(
            bundle,
            store=store,
            mara_root=mara_root,
            injected=list(injected),
            expected_total=expected_total if expected_total is not None else len(bundle.records),
            config_version=1,
        )
    }


class TestMergeSimulation:
    def test_same_paper_does_not_create_a_second_doc_id(self, snapshot_docs, make_payload):
        record = {
            "doc_id": ARXIV_DUP,
            "title": "Multi-agent collaboration",
            "text": "피드 쪽 짧은 발췌",
            "url": "https://arxiv.org/abs/2412.05449v1",
            "locator": "abstract",
            "ontology": {"release_type": "Paper"},
        }
        snapshot = {d["doc_id"]: d for d in snapshot_docs}
        merged, merged_ids = simulate_merge(snapshot, [record])
        assert merged_ids == [ARXIV_DUP]
        assert len(merged) == 2

    def test_merge_keeps_snapshot_text_and_updates_url(self, snapshot_docs):
        snapshot = {d["doc_id"]: d for d in snapshot_docs}
        original_text = snapshot[ARXIV_DUP]["text"]
        record = {
            "doc_id": ARXIV_DUP,
            "title": "t",
            "text": "짧은 발췌",
            "url": "https://arxiv.org/abs/2412.05449v1",
            "locator": "abstract",
            "ontology": {"release_type": "Paper"},
        }
        merged, _ = simulate_merge(snapshot, [record])
        assert merged[ARXIV_DUP]["text"] == original_text
        assert merged[ARXIV_DUP]["url"] == "https://arxiv.org/abs/2412.05449v1"

    def test_new_paper_is_added(self, snapshot_docs):
        snapshot = {d["doc_id"]: d for d in snapshot_docs}
        record = {
            "doc_id": "arXiv:2608.31100v1",
            "title": "S3Gym",
            "text": "새 논문 초록",
            "url": "https://arxiv.org/abs/2608.31100v1",
            "locator": "abstract",
            "ontology": {"release_type": "Paper"},
        }
        merged, merged_ids = simulate_merge(snapshot, [record])
        assert merged_ids == []
        assert len(merged) == 3


class TestHappyPath:
    @pytest.fixture
    def bundle_set(self, tmp_path, make_payload, make_store, make_mara_root, snapshot_docs):
        store = make_store(
            [
                make_payload(
                    doc_id=ARXIV_DUP,
                    url=ARXIV_DUP_URL,
                    body="피드 발췌 본문",
                    source_name="arXiv (id_list API)",
                    tags=("paper",),
                    prompt_sha256=prompt_sha256("extract_ontology.v4.md"),
                ),
                make_payload(
                    doc_id="news:a",
                    url="https://news.hada.io/topic?id=1",
                    body="한국어 본문이다.",
                    prompt_sha256=prompt_sha256("extract_ontology.v4.md"),
                ),
                make_payload(
                    doc_id="news:b",
                    url="https://huggingface.co/blog/x",
                    body="",
                    source_name="Hugging Face Blog",
                    tags=("oss",),
                    published_at=None,
                    prompt_sha256=prompt_sha256("extract_ontology.v4.md"),
                ),
            ]
        )
        mara = make_mara_root(snapshot_docs, [ARXIV_DUP])
        return _bundle(tmp_path, store), store, mara

    def test_all_producer_side_checks_pass(self, bundle_set):
        bundle, store, mara = bundle_set
        checks = _checks(bundle, store, mara, injected=[ARXIV_DUP])
        failed = {n: c.detail for n, c in checks.items() if c.status == FAIL}
        assert failed == {}

    def test_item_13_is_left_pending_for_the_consumer(self, bundle_set):
        bundle, store, mara = bundle_set
        checks = _checks(bundle, store, mara, injected=[ARXIV_DUP])
        assert checks[13].status == PENDING

    def test_golden_set_expectation_survives_the_merge(self, bundle_set):
        bundle, store, mara = bundle_set
        checks = _checks(bundle, store, mara, injected=[ARXIV_DUP])
        assert checks[6].status == PASS

    def test_arxiv_count_is_base_plus_new(self, bundle_set):
        bundle, store, mara = bundle_set
        checks = _checks(bundle, store, mara, injected=[ARXIV_DUP])
        assert "기존 2 + 신규 0 = 2건" in checks[5].detail


class TestContractViolations:
    """session-09 §3 의 위반 케이스 5종. 전부 **실패**해야 한다."""

    @pytest.fixture
    def mara(self, make_mara_root, snapshot_docs):
        return make_mara_root(snapshot_docs, [ARXIV_DUP])

    def _mutated_bundle(self, tmp_path, store, mutate, export_id="fixed"):
        records = build_records(store, config_version=1)
        for record in records:
            mutate(record)
        write_export(records, out_dir=tmp_path / "corpus", export_id=export_id, config_version=1)
        return load_bundle(tmp_path / "corpus", export_id)

    def test_vocabulary_outside_the_manifest_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])

        def mutate(record):
            record["ontology"]["release_type"] = "Rumor"

        bundle = self._mutated_bundle(tmp_path, store, mutate)
        assert _checks(bundle, store, mara)[10].status == FAIL

    def test_duplicate_doc_id_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store(
            [
                make_payload(doc_id="a", url="https://example.com/a"),
                make_payload(doc_id="b", url="https://example.com/b"),
            ]
        )

        def mutate(record):
            record["doc_id"] = "news:same"

        bundle = self._mutated_bundle(tmp_path, store, mutate)
        checks = _checks(bundle, store, mara)
        assert checks[1].status == FAIL
        assert "doc_id 중복" in checks[1].detail

    def test_missing_provenance_key_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])

        def mutate(record):
            record["provenance"]["prompt_sha256"] = ""

        bundle = self._mutated_bundle(tmp_path, store, mutate)
        assert _checks(bundle, store, mara)[2].status == FAIL

    def test_indexable_disagreeing_with_the_rule_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a", body="")])

        def mutate(record):
            record["indexable"] = True  # 본문 0자인데 색인 가능하다고 주장

        bundle = self._mutated_bundle(tmp_path, store, mutate)
        assert _checks(bundle, store, mara)[9].status == FAIL

    def test_text_over_the_limit_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])

        def mutate(record):
            record["text"] = "a" * 4001
            record["text_chars"] = 4001

        bundle = self._mutated_bundle(tmp_path, store, mutate)
        assert _checks(bundle, store, mara)[8].status == FAIL

    def test_text_chars_mismatch_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])

        def mutate(record):
            record["text_chars"] = record["text_chars"] + 1

        bundle = self._mutated_bundle(tmp_path, store, mutate)
        assert _checks(bundle, store, mara)[8].status == FAIL

    def test_empty_string_published_at_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])

        def mutate(record):
            record["published_at"] = ""

        bundle = self._mutated_bundle(tmp_path, store, mutate)
        assert _checks(bundle, store, mara)[11].status == FAIL

    def test_placeholder_prompt_hash_fails(self, tmp_path, make_payload, make_store, mara):
        """계약 §12.2-3 — 하드코딩된 자리표시자를 통과시키지 않는다."""
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])
        bundle = self._mutated_bundle(tmp_path, store, lambda record: None)
        assert _checks(bundle, store, mara)[3].status == FAIL

    def test_wrong_text_origin_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])

        def mutate(record):
            record["text_origin"] = "summary"

        bundle = self._mutated_bundle(tmp_path, store, mutate)
        assert _checks(bundle, store, mara)[4].status == FAIL

    def test_manifest_total_mismatch_fails(self, tmp_path, make_payload, make_store, mara):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])
        bundle = self._mutated_bundle(tmp_path, store, lambda record: None)
        checks = _checks(bundle, store, mara, expected_total=30)
        assert checks[12].status == FAIL
        assert checks[1].status == FAIL

    def test_injected_id_absent_from_the_snapshot_fails(self, tmp_path, make_payload, make_store, mara):
        """주입한 URL 이 새 doc_id 를 만들면 동일화가 검증되지 않은 것이다."""
        store = make_store(
            [make_payload(doc_id="arXiv:9999.99999v1", url="http://arxiv.org/abs/9999.99999v1")]
        )
        bundle = self._mutated_bundle(tmp_path, store, lambda record: None)
        checks = _checks(bundle, store, mara, injected=["arXiv:9999.99999v1"])
        assert checks[5].status == FAIL


class TestReportFormat:
    def test_report_lists_all_thirteen_items(self, tmp_path, make_payload, make_store, make_mara_root, snapshot_docs):
        from export.conformance import format_report

        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])
        bundle = _bundle(tmp_path, store)
        mara = make_mara_root(snapshot_docs, [ARXIV_DUP])
        checks = run_checks(
            bundle, store=store, mara_root=mara, injected=[], expected_total=1, config_version=1
        )
        report = format_report(checks)
        assert report.count("\n|") >= 13
        assert len(checks) == 13
