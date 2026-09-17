"""레코드·manifest 생성 — 계약 §3, §5, §6, §11.3."""

from __future__ import annotations

import json

from export.contract import (
    MIN_TEXT_CHARS,
    TEXT_MAX_CHARS,
    schema_sha256,
    vocab_snapshot,
)
from export.exporter import build_counts, build_records, write_export
from export.record import clamp_text, declared_lang
from export.store import ExtractionStore


def _record(store: ExtractionStore):
    return build_records(store, config_version=1)[0]


class TestTopLevelFields:
    def test_has_exactly_the_17_contract_fields(self, make_payload, make_store):
        store = make_store([make_payload(doc_id="news:x", url="https://news.hada.io/topic?id=1")])
        record = _record(store)
        assert set(record) == {
            "contract_version", "doc_id", "source", "source_name", "url", "title", "lang",
            "text", "text_origin", "text_chars", "text_truncated", "locator",
            "published_at", "collected_at", "ontology", "provenance", "indexable",
        }

    def test_url_is_stored_normalized_not_as_received(self, make_payload, make_store):
        store = make_store(
            [make_payload(doc_id="news:x", url="http://News.Hada.io/topic?utm_source=rss&id=1#x")]
        )
        assert _record(store)["url"] == "https://news.hada.io/topic?id=1"

    def test_missing_published_at_is_null_not_empty_string(self, make_payload, make_store):
        store = make_store(
            [make_payload(doc_id="news:x", url="https://example.com/a", published_at=None)]
        )
        assert _record(store)["published_at"] is None

    def test_lang_is_the_source_declaration_not_a_judgement(self, make_payload, make_store):
        """한글 태그가 붙은 소스의 영어 본문도 `ko` 로 선언된다 (§3.4)."""
        store = make_store(
            [
                make_payload(
                    doc_id="news:x",
                    url="https://news.hada.io/topic?id=1",
                    body="This body is entirely in English.",
                    tags=("ko", "aggregator"),
                )
            ]
        )
        assert _record(store)["lang"] == "ko"

    def test_declared_lang_defaults_to_en(self):
        assert declared_lang(("vendor",)) == "en"
        assert declared_lang(None) == "en"


class TestTextRules:
    def test_text_is_cut_at_the_contract_limit(self, make_payload, make_store):
        store = make_store(
            [make_payload(doc_id="news:x", url="https://example.com/a", body="가" * 5000)]
        )
        record = _record(store)
        assert len(record["text"]) == TEXT_MAX_CHARS
        assert record["text_chars"] == TEXT_MAX_CHARS
        assert record["text_truncated"] is True

    def test_text_chars_is_measured_after_cutting(self, make_payload, make_store):
        store = make_store(
            [make_payload(doc_id="news:x", url="https://example.com/a", body="a" * 4321)]
        )
        record = _record(store)
        assert record["text_chars"] == len(record["text"]) == TEXT_MAX_CHARS

    def test_feed_ellipsis_marks_the_record_truncated(self):
        text, truncated = clamp_text("피드가 잘라 보낸 발췌…")
        assert truncated is True
        assert text.endswith("…")

    def test_untruncated_body_is_not_flagged(self):
        text, truncated = clamp_text("온전한 본문이다.")
        assert truncated is False
        assert text == "온전한 본문이다."

    def test_empty_body_is_allowed_and_not_indexable(self, make_payload, make_store):
        """Hugging Face Blog 는 50/50 건이 본문 0자다 (D-013)."""
        store = make_store(
            [make_payload(doc_id="news:x", url="https://huggingface.co/blog/a", body="")]
        )
        record = _record(store)
        assert record["text"] == ""
        assert record["text_chars"] == 0
        assert record["indexable"] is False

    def test_one_char_body_is_indexable_in_v1(self, make_payload, make_store):
        assert MIN_TEXT_CHARS == 1
        store = make_store([make_payload(doc_id="news:x", url="https://example.com/a", body="a")])
        assert _record(store)["indexable"] is True


class TestOntologyBlock:
    def test_impact_is_flattened_into_two_keys(self, make_payload, make_store):
        store = make_store([make_payload(doc_id="news:x", url="https://example.com/a")])
        ontology = _record(store)["ontology"]
        assert ontology["impact_score"] == 3
        assert "impact" not in ontology
        assert ontology["impact_rationale"]

    def test_keys_are_english_not_korean_aliases(self, make_payload, make_store):
        store = make_store([make_payload(doc_id="news:x", url="https://example.com/a")])
        assert set(_record(store)["ontology"]) == {
            "tech_domains", "release_type", "companies", "prior_art",
            "impact_score", "impact_rationale", "summary",
        }

    def test_company_raw_notation_is_preserved(self, make_payload, make_store):
        payload = make_payload(doc_id="news:x", url="https://example.com/a")
        payload["extraction"]["ontology"]["companies"] = [
            {"raw": "오픈AI", "canonical": "OpenAI", "resolved": True, "role": "발표 주체"}
        ]
        store = make_store([payload])
        company = _record(store)["ontology"]["companies"][0]
        assert company == {
            "raw": "오픈AI", "canonical": "OpenAI", "resolved": True, "role": "발표 주체"
        }


class TestProvenance:
    def test_carries_the_five_required_keys(self, make_payload, make_store):
        store = make_store([make_payload(doc_id="news:x", url="https://example.com/a")])
        prov = _record(store)["provenance"]
        for key in (
            "extraction_model", "prompt_version", "prompt_sha256", "extracted_at", "vocab_version"
        ):
            assert prov[key]

    def test_gate_fields_are_null_when_the_gate_did_not_run(self, make_payload, make_store):
        store = make_store([make_payload(doc_id="news:x", url="https://example.com/a", gate=None)])
        prov = _record(store)["provenance"]
        assert prov["gate_model"] is None
        assert prov["gate_prompt_version"] is None

    def test_gate_fields_are_filled_when_it_did(self, make_payload, make_store):
        gate = {
            "prompt_name": "relevance_gate.v1.md",
            "model": "claude-haiku-4-5-20251001",
            "is_relevant": True,
            "reason": "AI 관련 기사다.",
        }
        store = make_store([make_payload(doc_id="news:x", url="https://example.com/a", gate=gate)])
        prov = _record(store)["provenance"]
        assert prov["gate_model"] == "claude-haiku-4-5-20251001"
        assert prov["gate_prompt_version"] == "relevance_gate.v1.md"


class TestVocabSnapshot:
    def test_snapshot_comes_from_the_schema_enums(self):
        snapshot = vocab_snapshot(1)
        assert len(snapshot["tech_domain"]) == 14
        assert len(snapshot["release_type"]) == 8
        assert snapshot["vocab_version"] == f"config=1;schema_sha256={schema_sha256()}"

    def test_hash_is_stable_across_calls(self):
        assert schema_sha256() == schema_sha256()


class TestManifest:
    def test_counts_split_indexable_and_empty_text(self, make_payload, make_store):
        store = make_store(
            [
                make_payload(doc_id="a", url="https://example.com/a", body="본문"),
                make_payload(doc_id="b", url="https://example.com/b", body=""),
            ]
        )
        counts = build_counts(build_records(store, config_version=1))
        assert counts["total"] == 2
        assert counts["indexable"] == 1
        assert counts["excluded_empty_text"] == 1
        assert counts["indexable"] + counts["excluded_empty_text"] == counts["total"]

    def test_unresolved_companies_are_counted_per_mention(self, make_payload, make_store):
        payload = make_payload(doc_id="a", url="https://example.com/a")
        payload["extraction"]["ontology"]["companies"] = [
            {"raw": "SpaceX", "canonical": "SpaceX", "resolved": False, "role": None},
            {"raw": "Cursor", "canonical": "Cursor", "resolved": False, "role": None},
        ]
        store = make_store([payload])
        counts = build_counts(build_records(store, config_version=1))
        assert counts["companies_unresolved"] == 2

    def test_manifest_has_only_the_contract_keys(self, make_payload, make_store, tmp_path):
        store = make_store([make_payload(doc_id="a", url="https://example.com/a")])
        result = write_export(
            build_records(store, config_version=1), out_dir=tmp_path / "corpus", config_version=1
        )
        assert set(result.manifest) == {
            "contract_version", "export_id", "exported_at", "exporter", "license_note",
            "counts", "vocab",
        }


class TestWriteExport:
    def test_splits_files_by_source(self, make_payload, make_store, tmp_path):
        store = make_store(
            [
                make_payload(doc_id="a", url="http://arxiv.org/abs/2412.05449v1"),
                make_payload(doc_id="b", url="https://news.hada.io/topic?id=1"),
            ]
        )
        out = tmp_path / "corpus"
        result = write_export(build_records(store, config_version=1), out_dir=out, config_version=1)
        assert set(result.jsonl_paths) == {"arxiv", "news"}
        for path in result.jsonl_paths.values():
            assert path.exists()

    def test_does_not_create_a_file_for_an_absent_source(self, make_payload, make_store, tmp_path):
        store = make_store([make_payload(doc_id="b", url="https://news.hada.io/topic?id=1")])
        out = tmp_path / "corpus"
        result = write_export(build_records(store, config_version=1), out_dir=out, config_version=1)
        assert set(result.jsonl_paths) == {"news"}
        assert not (out / "arxiv").exists()

    def test_one_record_per_line_utf8_without_ascii_escaping(self, make_payload, make_store, tmp_path):
        store = make_store(
            [make_payload(doc_id="b", url="https://news.hada.io/topic?id=1", title="한글 제목")]
        )
        out = tmp_path / "corpus"
        result = write_export(build_records(store, config_version=1), out_dir=out, config_version=1)
        raw = result.jsonl_paths["news"].read_bytes().decode("utf-8")
        assert raw.count("\n") == 1
        assert "한글 제목" in raw
        assert json.loads(raw)["title"] == "한글 제목"


class TestReexportWithoutLlm:
    def test_records_are_rebuilt_from_the_store_alone(self, make_payload, make_store, tmp_path):
        """계약 §12.3 — 형식 결함은 LLM 재호출 없이 고쳐야 한다."""
        store = make_store(
            [make_payload(doc_id="a", url="https://example.com/a")]
        )
        reloaded = ExtractionStore(store.directory)
        first = build_records(store, config_version=1)
        second = build_records(reloaded, config_version=1)
        assert first == second

    def test_two_exports_are_byte_identical(self, make_payload, make_store, tmp_path):
        store = make_store(
            [
                make_payload(doc_id="a", url="http://arxiv.org/abs/2412.05449v1"),
                make_payload(doc_id="b", url="https://news.hada.io/topic?id=1"),
            ]
        )
        records = build_records(store, config_version=1)
        first = write_export(records, out_dir=tmp_path / "one", export_id="fixed", config_version=1)
        second = write_export(records, out_dir=tmp_path / "two", export_id="fixed", config_version=1)
        for source, path in first.jsonl_paths.items():
            assert path.read_bytes() == second.jsonl_paths[source].read_bytes()

    def test_record_order_follows_the_store_not_insertion(self, make_payload, make_store):
        store = make_store(
            [
                make_payload(doc_id="zzz", url="https://example.com/z"),
                make_payload(doc_id="aaa", url="https://example.com/a"),
            ]
        )
        assert [s.doc_id for s in store] == ["aaa", "zzz"]
