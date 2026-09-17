"""`doc_id` 생성 규칙 — 계약 §4.

이 파일이 고정하는 것은 **값 자체**다. 규칙이 바뀌면 MARA 골든셋의
`expected_doc_ids` 와 과거 인용 표기가 전부 무효가 되므로(계약 §4, §9),
"어쩌다 바뀌는" 일이 없도록 기대값을 하드코딩해 둔다.
"""

from __future__ import annotations

import hashlib

import pytest

from export.doc_id import (
    canonical_url,
    doc_id_for,
    is_arxiv,
    locator_for,
    source_for,
)


class TestCanonicalUrl:
    def test_scheme_is_forced_to_https(self):
        assert canonical_url("http://arxiv.org/abs/2412.05449v1") == (
            "https://arxiv.org/abs/2412.05449v1"
        )

    def test_host_is_lowercased(self):
        assert canonical_url("https://News.Hada.IO/topic?id=33003") == (
            "https://news.hada.io/topic?id=33003"
        )

    def test_default_ports_are_dropped(self):
        assert canonical_url("http://example.com:80/a") == "https://example.com/a"
        assert canonical_url("https://example.com:443/a") == "https://example.com/a"

    def test_non_default_port_is_kept(self):
        assert canonical_url("https://example.com:8443/a") == "https://example.com:8443/a"

    @pytest.mark.parametrize(
        "param", ["utm_source=x", "utm_medium=y", "ref=hn", "ref_src=twsrc", "fbclid=z", "gclid=w"]
    )
    def test_tracking_params_are_removed(self, param):
        assert canonical_url(f"https://example.com/a?{param}") == "https://example.com/a"

    def test_remaining_params_are_sorted_by_key(self):
        assert canonical_url("https://example.com/a?b=2&a=1&utm_source=x") == (
            "https://example.com/a?a=1&b=2"
        )

    def test_fragment_is_removed(self):
        assert canonical_url("https://example.com/a#section") == "https://example.com/a"

    def test_trailing_slash_is_removed(self):
        assert canonical_url("https://example.com/a/b/") == "https://example.com/a/b"

    def test_root_slash_is_kept(self):
        """경로가 `/` 하나뿐인 경우는 유지한다 (§4.1-6)."""
        assert canonical_url("https://example.com/") == "https://example.com/"

    def test_is_idempotent(self):
        once = canonical_url("HTTP://Example.com:80/a/b/?utm_source=x&b=2&a=1#frag")
        assert canonical_url(once) == once


class TestArxivIds:
    def test_matches_mara_notation_byte_for_byte(self):
        """MARA `scripts/ingest_corpus.py` 가 만드는 표기와 같아야 한다 (§4.3)."""
        assert doc_id_for("http://arxiv.org/abs/2412.05449v1") == "arXiv:2412.05449v1"
        assert doc_id_for("http://arxiv.org/abs/2605.21404v1") == "arXiv:2605.21404v1"

    def test_scheme_and_tracking_do_not_change_the_id(self):
        """`doc_id` 는 스킴과 무관하다 — 병합은 영향받지 않고 표기만 갱신된다 (§4.3)."""
        assert doc_id_for("https://arxiv.org/abs/2412.05449v1?utm_source=x") == (
            doc_id_for("http://arxiv.org/abs/2412.05449v1")
        )

    def test_pdf_path_yields_the_same_paper_id(self):
        assert doc_id_for("https://arxiv.org/pdf/2412.05449v1.pdf") == "arXiv:2412.05449v1"

    def test_legacy_id_drops_archive_prefix_like_mara_does(self):
        """구형 ID 에서 접두사가 떨어지는 동작까지 MARA 와 같게 둔다.

        한쪽만 '더 올바르게' 고치면 같은 논문이 두 키를 갖는다.
        """
        assert doc_id_for("https://arxiv.org/abs/cs/0701001v1") == "arXiv:0701001v1"

    def test_api_host_is_not_treated_as_arxiv(self):
        assert not is_arxiv("http://export.arxiv.org/api/query?id_list=2412.05449v1")


class TestNewsIds:
    def test_hash_is_of_the_canonical_url_bytes(self):
        url = "https://news.hada.io/topic?id=33003"
        expected = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        assert doc_id_for(url) == f"news:{expected}"

    def test_tracking_noise_collapses_to_one_id(self):
        assert doc_id_for("https://news.hada.io/topic?id=33003&utm_source=rss") == doc_id_for(
            "http://news.hada.io/topic?id=33003#top"
        )

    def test_id_length_is_16_hex_chars(self):
        doc_id = doc_id_for("https://openai.com/news/some-post")
        assert doc_id.startswith("news:")
        assert len(doc_id) == len("news:") + 16


class TestSourceAndLocator:
    def test_source_follows_the_same_host_judgement_as_doc_id(self):
        assert source_for("http://arxiv.org/abs/2412.05449v1") == "arxiv"
        assert source_for("https://news.hada.io/topic?id=1") == "news"

    def test_locator_is_abstract_only_for_arxiv(self):
        assert locator_for("http://arxiv.org/abs/2412.05449v1") == "abstract"
        assert locator_for("https://openai.com/news/x") == "feed_excerpt"
